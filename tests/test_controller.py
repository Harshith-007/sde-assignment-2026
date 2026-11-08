import json
import sqlite3

from controller import Store, Controller, stable_id


def test_stable_id_is_deterministic():
    assert stable_id("wf", 2, "voice") == stable_id("wf", 2, "voice")
    assert stable_id("wf", 2, "voice") != stable_id("wf", 3, "voice")


def test_call_events_aggregate_separate_kpis(tmp_path):
    store = Store(str(tmp_path / "state.sqlite"))
    event = {
        "type": "call", "workflow_id": "wf", "cohort_id": "senior",
        "connected": True, "engaged": False,
        "variant": {"caller_id": "D1", "voice": "V2", "welcome_message": "W1"},
        "qa": {"hallucination_flag": True},
    }
    store.event(event)
    for dim, option in (("caller_id", "D1"), ("voice", "V2"), ("welcome_message", "W1")):
        row = store.db.execute(
            "SELECT dials,connected,engaged,hallucinations FROM arms "
            "WHERE workflow=? AND cohort=? AND dimension=? AND option=?",
            ("wf", "senior", dim, option)).fetchone()
        assert tuple(row) == (1, 1, 0, 1)


def test_wilson_lower_bound_is_conservative():
    assert Controller.wilson_lower(0, 100) == 0
    assert 0 < Controller.wilson_lower(80, 100) < 0.8
    assert Controller.wilson_lower(80, 100) > Controller.wilson_lower(50, 100)


def test_proposal_store_survives_reopen(tmp_path):
    path = str(tmp_path / "state.sqlite")
    store = Store(path)
    body = {"base_version": 4, "config": {"prompt_version": "P3"}}
    store.save_proposal("pid", "wf", "apr_1", 4, "prompt", "pending", body)
    store.db.close()
    reopened = Store(path)
    row = reopened.pending()[0]
    assert row["approval_id"] == "apr_1"
    assert json.loads(row["payload"]) == body
