#!/usr/bin/env python3
"""Outcome-first controller for the Formi simulator.

Only the documented HTTP API is used for world state. Durable controller state,
event cursors and idempotency records live in SQLite (CONTROLLER_DB).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from typing import Any

import httpx

LOG = logging.getLogger("formi.controller")
DIMENSIONS = ("caller_id", "voice", "welcome_message")
STEP_MINUTES = 15


def stable_id(*parts: Any) -> str:
    raw = "|".join(str(x) for x in parts).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS arms (
          workflow TEXT NOT NULL, cohort TEXT NOT NULL, dimension TEXT NOT NULL,
          option TEXT NOT NULL, dials INTEGER NOT NULL DEFAULT 0,
          connected INTEGER NOT NULL DEFAULT 0, engaged INTEGER NOT NULL DEFAULT 0,
          hallucinations INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(workflow,cohort,dimension,option));
        CREATE TABLE IF NOT EXISTS proposals (
          proposal_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, approval_id TEXT,
          base_version INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
          payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, workflow TEXT, kind TEXT,
          detail TEXT NOT NULL);
        """)
        self.db.commit()

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))
        self.db.commit()

    def event(self, ev: dict) -> None:
        if ev.get("type") != "call":
            if ev.get("type") in ("config_changed", "approval_decided"):
                self.decision(ev.get("ts"), ev.get("workflow_id"), ev["type"], ev)
            return
        wf, cohort = ev.get("workflow_id"), ev.get("cohort_id") or "default"
        variant = ev.get("variant") or {}
        connected, engaged = int(bool(ev.get("connected"))), int(bool(ev.get("engaged")))
        qa = ev.get("qa") or {}
        for dim in DIMENSIONS:
            option = variant.get(dim)
            if not option:
                continue
            self.db.execute("""
              INSERT INTO arms(workflow,cohort,dimension,option,dials,connected,engaged,hallucinations)
              VALUES(?,?,?,?,1,?,?,?)
              ON CONFLICT(workflow,cohort,dimension,option) DO UPDATE SET
                dials=dials+1, connected=connected+excluded.connected,
                engaged=engaged+excluded.engaged,
                hallucinations=hallucinations+excluded.hallucinations
            """, (wf, cohort, dim, str(option), connected, engaged,
                  int(bool(qa.get("hallucination_flag")))))
        self.db.commit()

    def arms(self, wf: str, dimension: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT option,SUM(dials) dials,SUM(connected) connected,SUM(engaged) engaged,"
            "SUM(hallucinations) hallucinations FROM arms WHERE workflow=? AND dimension=? GROUP BY option",
            (wf, dimension)).fetchall()

    def decision(self, ts: str | None, wf: str | None, kind: str, detail: Any) -> None:
        self.db.execute("INSERT INTO decisions(ts,workflow,kind,detail) VALUES(?,?,?,?)",
                        (ts, wf, kind, json.dumps(detail, sort_keys=True)))
        self.db.commit()

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM proposals WHERE status IN ('pending','approved')").fetchall()

    def save_proposal(self, pid: str, wf: str, aid: str | None, version: int,
                      kind: str, status: str, payload: dict) -> None:
        self.db.execute("""
          INSERT INTO proposals(proposal_id,workflow,approval_id,base_version,kind,status,payload)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET
          approval_id=excluded.approval_id,status=excluded.status,updated_at=CURRENT_TIMESTAMP
        """, (pid, wf, aid, version, kind, status, json.dumps(payload, sort_keys=True)))
        self.db.commit()


class Controller:
    def __init__(self, base_url: str, db_path: str):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=90.0)
        self.store = Store(db_path)
        self.workflows: dict[str, dict] = {}
        self.started = time.monotonic()
        self.events_seen = 0

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last = None
        for attempt in range(5):
            try:
                response = self.http.request(method, path, **kwargs)
                if response.status_code >= 500:
                    last = RuntimeError(f"{method} {path}: {response.status_code} {response.text[:300]}")
                    time.sleep(min(0.25 * (2 ** attempt), 2))
                    continue
                return response
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                time.sleep(min(0.25 * (2 ** attempt), 2))
        raise RuntimeError(f"API unavailable: {method} {path}") from last

    def get_json(self, path: str, **params) -> Any:
        r = self.request("GET", path, params=params)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: dict) -> httpx.Response:
        return self.request("POST", path, json=body)

    def bootstrap(self) -> None:
        health = self.get_json("/health")
        if not health.get("ok"):
            raise RuntimeError("simulator health check failed")
        self.workflows = {w["workflow_id"]: w for w in self.get_json("/workflows")}
        # Replay API-exposed history exactly once. The cursor is committed only after
        # the events in a chunk have been persisted, so a crash merely replays a chunk.
        if self.store.get("history_loaded") != "1":
            chunks = self.get_json("/events/chunks", kind="history", after=-1)
            for chunk in sorted(chunks, key=lambda x: x["chunk"]):
                self.consume_chunk(chunk["chunk"])
            self.store.set("history_loaded", "1")
        LOG.info("connected: %d workflows", len(self.workflows))

    def consume_chunk(self, number: int) -> None:
        r = self.request("GET", f"/events/chunks/{number}")
        r.raise_for_status()
        for line in r.text.splitlines():
            if line.strip():
                self.store.event(json.loads(line))
                self.events_seen += 1
        self.store.set("last_chunk", max(number, int(self.store.get("last_chunk", "-1"))))

    def ingest_new(self, new_chunks: list[dict]) -> None:
        last = int(self.store.get("last_chunk", "-1"))
        for ch in sorted(new_chunks, key=lambda x: x["chunk"]):
            if int(ch["chunk"]) > last:
                self.consume_chunk(int(ch["chunk"]))
                last = int(ch["chunk"])

    @staticmethod
    def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
        if total <= 0:
            return 0.0
        p = successes / total
        den = 1 + z*z/total
        centre = p + z*z/(2*total)
        margin = z*((p*(1-p)/total + z*z/(4*total*total)) ** 0.5)
        return (centre-margin)/den

    def reconcile(self) -> None:
        for row in self.store.pending():
            wf, aid, pid = row["workflow"], row["approval_id"], row["proposal_id"]
            if not aid:
                continue
            r = self.request("GET", f"/approvals/{aid}")
            if r.status_code == 404:
                self.store.save_proposal(pid, wf, aid, row["base_version"], row["kind"], "missing",
                                         json.loads(row["payload"]))
                continue
            r.raise_for_status()
            approval = r.json()
            status = approval.get("status", "pending")
            if status == "approved":
                current = self.get_json(f"/workflows/{wf}/config")
                if int(current["version"]) != int(row["base_version"]):
                    self.store.save_proposal(pid, wf, aid, row["base_version"], row["kind"], "stale",
                                             json.loads(row["payload"]))
                    self.store.decision(None, wf, "stale_approval", {"approval_id": aid})
                    continue
                applied = self.post_json(f"/workflows/{wf}/apply", {"approval_id": aid})
                if applied.status_code in (200, 201):
                    self.store.save_proposal(pid, wf, aid, row["base_version"], row["kind"], "applied",
                                             json.loads(row["payload"]))
                    self.store.decision(None, wf, "approval_applied", {"approval_id": aid})
                elif applied.status_code in (409, 410):
                    self.store.save_proposal(pid, wf, aid, row["base_version"], row["kind"], "stale_or_lapsed",
                                             json.loads(row["payload"]))
                else:
                    LOG.warning("apply %s failed: %s %s", wf, applied.status_code, applied.text[:200])
            elif status in ("rejected", "expired", "lapsed"):
                self.store.save_proposal(pid, wf, aid, row["base_version"], row["kind"], status,
                                         json.loads(row["payload"]))

    def make_prompt_proposal(self, wf: str, spec: dict, current: dict, cfg: dict,
                             incident_rate: float) -> None:
        gov = spec.get("governance", {})
        min_pass = float(gov.get("min_validation_pass_rate", 0.97))
        state = self.get_json("/sim/state")
        if int(state.get("credits_remaining", 0)) < 8:
            LOG.warning("%s: insufficient credits for safe prompt validation", wf)
            return
        suggestion = self.post_json("/llm/suggest-fixes",
                                    {"workflow_id": wf, "problem":
                                     f"Observed hallucination flag rate {incident_rate:.3f}; "
                                     "suggest a grounded, compliance-safe prompt alternative."})
        if suggestion.status_code != 200:
            LOG.warning("%s: prompt suggestion unavailable: %s", wf, suggestion.text[:200])
            return
        fixes = suggestion.json().get("fixes", [])
        if not fixes:
            return
        # Validation is a safety gate only. It is not treated as evidence of engagement lift.
        selected = None
        for fix in fixes[:2]:
            prompt = fix.get("prompt_version")
            if not prompt or prompt == cfg.get("prompt_version"):
                continue
            valid = self.post_json("/validation/runs",
                                   {"workflow_id": wf, "prompt_version": prompt, "n": 500})
            if valid.status_code != 200:
                continue
            result = valid.json()
            if float(result.get("pass_rate", 0)) >= min_pass:
                selected = (prompt, result)
                break
        if not selected:
            LOG.warning("%s: no suggested prompt passed %.3f safety threshold", wf, min_pass)
            return
        prompt, validation = selected
        proposed = json.loads(json.dumps(cfg))
        proposed["prompt_version"] = prompt
        self.submit_approval(wf, current["version"], proposed, "prompt_safety",
                             {"validation_run_id": validation["validation_run_id"],
                              "observed_hallucination_rate": incident_rate,
                              "validation_pass_rate": validation["pass_rate"]})

    def submit_approval(self, wf: str, version: int, proposed: dict, kind: str, evidence: dict) -> None:
        pid = stable_id(wf, version, kind, json.dumps(proposed, sort_keys=True))
        if self.store.db.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)).fetchone():
            return
        diff = {"from_version": version, "kind": kind, "proposed_config": proposed}
        body = {"proposal_id": pid, "workflow_id": wf, "base_version": version,
                "proposed_config": proposed, "change_summary": "Apply a statistically unsupported but safety-validated prompt correction",
                "diff": diff, "affected_leads": 0,
                "dry_run": {"method": "safety validation on 500 synthetic conversations",
                            "validation": evidence},
                "impact_score": 0.5,
                "rationale": "A population-level hallucination signal exceeded the incident threshold; "
                             "the proposed prompt passed the workflow's configured safety threshold.",
                "evidence": {"validation_run_id": evidence["validation_run_id"]}}
        r = self.post_json("/approvals", body)
        if r.status_code == 409:
            # Recover the create-before-local-commit crash window by finding our idempotency key.
            rows = self.get_json("/approvals", workflow_id=wf)
            match = next((x for x in rows if x.get("proposal_id") == pid), None)
            if match:
                self.store.save_proposal(pid, wf, match["approval_id"], version, kind,
                                         match.get("status", "pending"), body)
            return
        if r.status_code not in (200, 201):
            LOG.warning("%s: approval request rejected (%s): %s", wf, r.status_code, r.text[:300])
            self.store.decision(None, wf, "proposal_rejected_by_api",
                                {"kind": kind, "status": r.status_code, "body": r.text[:300]})
            return
        data = r.json()
        self.store.save_proposal(pid, wf, data.get("approval_id"), version, kind, "pending", body)
        self.store.decision(None, wf, "proposal_created", {"proposal_id": pid, "approval_id": data.get("approval_id"),
                                                             "kind": kind})

    def detect_incidents(self) -> None:
        for wf, spec in self.workflows.items():
            current = self.get_json(f"/workflows/{wf}/config")
            cfg = current["config"]
            # Use connected calls as denominator; detector flags are noisy, so require
            # a meaningful population and a conservative rate before spending credits.
            calls = self.store.db.execute(
                "SELECT SUM(connected) connected,SUM(hallucinations) h FROM arms "
                "WHERE workflow=? AND dimension='caller_id'", (wf,)).fetchone()
            n, h = int(calls["connected"] or 0), int(calls["h"] or 0)
            if n < 120 or h / max(n, 1) < 0.045:
                continue
            # A pending prompt proposal for this workflow suppresses duplicate LLM spend.
            pending = self.store.db.execute(
                "SELECT 1 FROM proposals WHERE workflow=? AND kind='prompt_safety' "
                "AND status IN ('pending','approved')", (wf,)).fetchone()
            if not pending:
                self.make_prompt_proposal(wf, spec, current, cfg, h / n)

    def maybe_rotate_caller_ids(self) -> None:
        for wf, spec in self.workflows.items():
            gov = spec.get("governance", {}).get("auto_apply", {})
            if not gov.get("enabled") or "caller_id" not in gov.get("allowed_dims", []):
                continue
            rows = self.store.arms(wf, "caller_id")
            eligible = [r for r in rows if int(r["dials"] or 0) >= 100]
            if len(eligible) < 2:
                continue
            total_connected = sum(int(r["connected"]) for r in eligible)
            total_dials = sum(int(r["dials"]) for r in eligible)
            baseline = total_connected / max(total_dials, 1)
            worst = min(eligible, key=lambda r: int(r["connected"]) / max(int(r["dials"]), 1))
            best = max(eligible, key=lambda r: int(r["connected"]) / max(int(r["dials"]), 1))
            bad_rate = int(worst["connected"]) / max(int(worst["dials"]), 1)
            if baseline - bad_rate < 0.06 or worst["option"] == best["option"]:
                continue
            current = self.get_json(f"/workflows/{wf}/config")
            cfg = current["config"]
            dist = cfg["allocations"]["default"]["caller_id"]
            if worst["option"] not in dist or dist[worst["option"]] < 0.10:
                continue
            shift = min(float(dist[worst["option"]]), 0.10)
            dist[worst["option"]] -= shift
            dist[best["option"]] = dist.get(best["option"], 0.0) + shift
            if dist[worst["option"]] <= 1e-9:
                del dist[worst["option"]]
            pid = stable_id(wf, current["version"], "caller_id_rotation", cfg)
            if self.store.db.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)).fetchone():
                continue
            body = {"proposal_id": pid, "base_version": current["version"],
                    "proposed_config": cfg, "rationale": "Population-level caller-ID pickup degradation; "
                    "shift only 10 percentage points toward the best observed ID."}
            r = self.request("PUT", f"/workflows/{wf}/config", json=body)
            if r.status_code in (200, 201):
                self.store.save_proposal(pid, wf, None, current["version"], "caller_id_rotation", "applied", body)
                self.store.decision(None, wf, "caller_id_auto_apply",
                                    {"from": worst["option"], "to": best["option"],
                                     "baseline_pickup": baseline, "degraded_pickup": bad_rate})
            else:
                LOG.info("%s: caller-ID rotation not permitted (%s)", wf, r.status_code)

    def run(self) -> None:
        self.bootstrap()
        while True:
            self.reconcile()
            self.detect_incidents()
            self.maybe_rotate_caller_ids()
            r = self.post_json("/sim/advance", {"minutes": STEP_MINUTES})
            if r.status_code == 409:
                break
            r.raise_for_status()
            data = r.json()
            self.ingest_new(data.get("new_chunks", []))
            if data.get("finished"):
                break
        self.reconcile()
        state = self.get_json("/sim/state")
        elapsed = time.monotonic() - self.started
        LOG.info("finished=%s events=%d elapsed_seconds=%.2f credits_remaining=%s",
                 state.get("finished"), self.events_seen, elapsed, state.get("credits_remaining"))
        self.store.decision(data.get("ts") if 'data' in locals() else None, None, "run_finished",
                            {"events_seen": self.events_seen, "wall_seconds": round(elapsed, 2),
                             "credits_remaining": state.get("credits_remaining")})
        self.http.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    parser.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "controller.sqlite3"))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Controller(args.url, args.db).run()


if __name__ == "__main__":
    main()
