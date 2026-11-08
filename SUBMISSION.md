# Outcome-First Agent Loop — Submission

**Name:** Harshith Pendem  
**Date:** 2026-11-08  
**Hours spent:** In progress; record actual time before final submission.  
**Stack:** Python 3.10+, HTTPX, SQLite WAL.

> Validation status: the controller, launcher, tests and design notes are committed. The public simulator runs, grade reports, load measurements and kill/restart demonstration have NOT yet been executed. No results are fabricated. Complete the checklist in reports/README.md before submitting.

## 0. TL;DR
An HTTP-only controller with durable local state, call-arm aggregation, approval reconciliation, a population-level prompt safety incident path and policy-gated caller-ID rotation. Pickup and engagement remain separate. Statistical welcome/voice winner promotion and official scenario evidence remain outstanding.

## 1. Assumptions
1. The controller uses only documented HTTP endpoints; it never reads scenario YAML, simulator internals or run truth.
2. The simulator stays alive during controller restarts and the same SQLite DB path is reused.
3. Event chunks are append-only and globally increasing.
4. Validation pass rate measures safety, not engagement.
5. SQLite is a prototype store; production would use existing Postgres and workflow leases.

## 2. Scope decisions

| Capability | Status | Rationale / remaining work |
|---|---|---|
| C1 Event ingestion | Partial | Sequential chunk ingestion and cursor; add backpressure, event-ID dedupe and load proof |
| C2 Shared cohorts | Partial | Preserves event cohort IDs; needs explicit shared registry and supervisor view |
| C3 History bootstrap | Partial | Replays history API chunks; verify joins and historical schema |
| C4 Call-time optimisation | Cut | Needs reliable person-hour learning and causal evaluation |
| C5 Caller-ID health | Partial | Population rate heuristic and policy-gated 10pp rotation; add confidence intervals, cooldown and guardrail |
| C6 Welcome/voice experiments | Cut | No defensible winner selection yet; implement stratification, sequential tests and multiplicity control |
| C7 Promotion/decay rollback | Cut | Depends on validated experiment lifecycle |
| C8 Hallucination response | Partial | Population threshold → LLM suggestion → validation → approval; add post-apply verification |
| C9 Governance | Partial | Approval polling, apply and stale/lapsed handling; add batching, approver-hours and full retry paths |
| C10 Impact/dry run | Partial | Proposal fields and validation evidence; add affected-lead estimates and historical replay |
| C11 Supervisor view | Cut | Build after audit model stabilises |
| C12 Audit store | Partial | SQLite decisions, proposals and arm aggregates; add query CLI and per-lead trace |
| C13 Crash recovery | Partial | Durable DB and reconciliation; kill/restart proof still required |
| C14 Observability | Partial | Structured logs and final event/credit counts; add metrics and alerts |
| C15 Fair credit allocation | Cut | Needs tenant-aware scheduler |

The priority is a safe control-plane foundation, not a shallow implementation of every wishlist item. A noisy arm rate is not presented as a statistically proven winner.

## 3. Stakeholders

### 01 — Sales
- Accepted: workflow-level outcomes and explainability.
- Declined: blended KPI, instant 100% rollout, universal auto-apply and cross-client prompt reuse.
- Policy correction: auto-apply is workflow-specific; Northwind KYC explicitly disables it.
- Reply: "We'll show pickup, engagement and concluded-lead outcomes separately. A lunchtime fluctuation will not trigger a global rollout. Customer-facing changes follow each client's approval policy, and prompt changes need validation. We will move quickly only within the signed policy and won't reuse one client's prompt result for another client."

### 02 — Northwind Compliance
- Accepted: human approval for customer-facing changes, validation evidence, variant audit and approver-hour awareness.
- Declined: treating an LLM suggestion as evidence.
- Policy correction: renewal auto-apply permits only schedule/caller-ID; KYC has auto-apply disabled.
- Reply: "Every call's cohort, variant and config version are retained. Customer-facing changes go through your approval path. Prompt proposals include a validation run meeting your configured threshold. Operational changes remain inside the workflow allowlist and are summarised with rationale."

### 03 — Platform/SRE
- Accepted: durable local state, deterministic proposal IDs, bounded retries and structured logs.
- Declined: claiming scale or restart success without measurements.
- Reply: "The prototype persists state and reconciles pending approvals after restart. Load and kill/restart tests are still outstanding, so I won't claim the ten-minute target. Production should use existing Postgres, workflow leases and metrics for ingest lag, decision lag, credit burn and retries."

### 04 — Kestrel Operations
- Accepted: supervisor edits remain authoritative; check base version before applying; keep changes policy-gated.
- Declined: unvalidated prompt changes.
- Policy correction: memo preferences are not all simulator-enforced policy; actual workflow governance is authoritative.
- Reply: "Your edits are not silently overwritten. We compare the live config version before applying an approval; mismatches become stale. We will consolidate proposals and expose pending, approved and applied changes. We won't replace a script with verbatim compliance text merely because a safety detector improves."

### 05 — Product
- Accepted: separate KPIs, stable event cohort IDs, proposal evidence and workflow-local changes.
- Declined: treating unrun experiments as evidence or expiry as approval.
- Reply: "The event store retains the simulator cohort ID and separates pickup, engagement and QA flags. Expired requests are terminal and never applied. The next increment is a shared cohort registry and statistically controlled welcome/voice experiments before adding a UI or per-person scheduling."

### 06 — CTO
- Accepted: minimise LLM spend, design for scale, explain trade-offs.
- Declined: treating every stakeholder wishlist as P0.
- Reply: "I prioritised persistence, governance and incident groundwork. I deferred call-time optimisation, factorial experiments, decay rollback and UI because they depend on reliable cohort and statistical layers. I will validate public scenarios and measure load before expanding autonomous scope."

## 4. Architecture

```text
Simulator HTTP API
  /workflows, /events/chunks, /sim/state
            |
      chunk ingestion ------> SQLite WAL
            |                   |-- arm aggregates
            |                   |-- proposals / approvals
            |                   |-- decisions / audit
            |                   '-- cursor
            v
    population detector
       |             |
 caller-ID      hallucination
 policy gate    LLM suggestion -> validation -> /approvals
       |                                  |
 auto-apply                         approval poll/apply
```

On restart, the controller opens the same DB, bootstraps history if needed, reconciles approvals and checks the live config version before applying. The simulator must remain running.

## 5. Learning loop
- Cohorts: aggregate using the cohort ID on each call; no separate competing definition.
- Caller-ID eligibility: at least 100 dials per arm, a heuristic floor rather than a significance claim.
- Welcome/voice factorial design and winner promotion: not implemented.
- Wilson lower-bound helper is present but not wired into a winner decision; no false-winner rate is claimed.
- Decay rollback and call-time optimisation: deferred.
- Guardrail: no welcome/voice promotion occurs; caller-ID rotation still needs explicit guardrail evaluation before production.

## 6. Governance
Proposals have deterministic IDs, base versions, approval IDs and persisted payloads. The controller polls status and applies only if the live version still matches. Stale/lapsed approvals are not force-applied. Prompt proposals carry a validation_run_id. Auto-apply is attempted only when the workflow's own policy enables caller-ID changes. Batching, approver-hour scheduling, expiry-aware re-proposal and accurate impact/dry-run replay remain gaps.

## 7. Incidents
Hallucination detection requires at least 120 connected calls and a cumulative flag rate of 4.5%. It checks remaining credits, asks for up to two prompt suggestions, validates on 500 synthetic conversations, and proposes only if pass rate meets policy. Validation is a safety gate, not engagement evidence. Threshold calibration and post-apply verification remain outstanding. A public_02 timeline must be added after execution.

## 8. Scale
Not measured. The current implementation is sequential and SQLite-backed. At 10×, SQLite write contention and single-process ingestion are expected first bottlenecks. Production direction: tenant/workflow partitioning, Postgres batched writes, bounded workers and per-partition cursors. Do not claim the 10-minute target until measured on 4 vCPU / 8 GB.

## 9. Reliability
SQLite WAL and FULL synchronous mode are enabled. Proposal IDs are deterministic; 5xx/network errors get bounded retries and 4xx responses are not blindly retried. Reuse the same DB path after restart. The kill/restart demonstration and strict event-ID deduplication test remain outstanding.

## 10. Audit & observability
Example query:
```sql
SELECT ts, workflow, kind, detail
FROM decisions
WHERE workflow='nw_renewal'
ORDER BY id;
```
Proposal state is in `proposals`; option aggregates are in `arms`. Page on repeated API failures, apply failures, persistent ingestion lag and credit exhaustion—not one noisy QA flag.

## 11. Results
Pending official execution. Generate public_01 and public_02 grader reports using the commands in reports/README.md. No KPI lift, harmful-change count or time-to-fix is invented.

## 12. Trade-offs
| Option | Rejected because |
|---|---|
| LLM chooses winner | LLM may suggest; statistical evidence decides |
| Immediate full exploitation | No sample/guardrail proof |
| New stateful infrastructure | Prototype uses SQLite; production should use existing Postgres |
| Implement every feature shallowly | Prioritised a reliable control plane |

## 13. Known weaknesses
1. No public scenario grades or measured load yet.
2. Event-ID deduplication is required for strict replay safety.
3. Caller-ID logic uses cumulative rates, no confidence interval/cooldown and no explicit guardrail check.
4. Hallucination threshold needs rolling-window calibration.
5. Prompt dry-run is validation-only; affected-lead estimates and historical KPI replay are absent.
6. Statistical experiments, decay rollback, call-time optimisation and supervisor UI are deferred.

## 14. First two production weeks
Week 1: add event-ID idempotency and transactional checkpoints; migrate to Postgres; add workflow leases; contract-test the API; complete public scenario and restart tests. Week 2: implement stratified guardrail-aware sequential experiments, decay rollback and post-apply verification; publish audit/supervisor views and metrics; load-test at 1× and 10× before enabling customer-facing autonomy.
