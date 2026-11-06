# 0018. Crash safety protocol: write-ahead intents, reconciliation by kind, crash equivalence

Status: accepted (2026-11-06), milestone M2

## Context
- Constraint 8: killing the controller at any point and restarting must never double-apply, lose
  a pending approval or forget learned state. Constraint 6: every decision is in our store before
  or atomically with acting on it.
- The simulator's calls differ in what they forgive (DESIGN.md 8.1). `POST /sim/advance` is not
  idempotent: a blind retry skips simulated time. Approval ids are consumed when a record is
  created, auto-apply ids only on success, an applied approval refuses a second apply, and
  validation and suggest-fixes calls have no key or listing at all.
- A crash can fall between any two of: the decision, the HTTP call, and recording its result.

## Decision
1. **Write-ahead intents** (`reliability/intents.py: IntentExecutor.execute`). T1: one transaction
   with the decision row (outcome "intended"), the intent row (status `started`, the
   deterministic idempotency key, the exact request), the kind's "before" transition and an
   audit row. Then the HTTP call, never blindly retried. T2: one transaction with the intent's
   response and status `done`, the kind's domain transition, the decision's outcome and an audit
   row. A definite error response is recorded as `failed` with its code.
2. **Reconcile before any new side effect.** On start, and immediately after a transport timeout,
   `Reconciler` resolves every `started` intent from the simulator's own answer:
   advance by `GET /sim/state` (which waits on the simulator lock, so it answers after a running
   advance): clock at `from` means it never ran, at `min(to, end)` means done, anything else
   pages and the loop follows the simulator's clock; submit by `GET /approvals` and the proposal
   id; auto-apply by config history and the proposal id; apply by the approval's status and the
   history's approval id; validation and suggest-fixes by the credit balance against the
   `credits_before` and `cost` the request recorded (fell by the cost: ran, result lost).
   Resolutions are `done` (complete T2 with the simulator's answer), `aborted` (never ran: the
   key is renamed `<key>#aborted<id>` so the deterministic re-plan can use it again), `lost` or
   `unknown` (pages `INTENT_UNKNOWN`).
3. **Domain transitions are pluggable** (`IntentRegistry.attach(kind, before=, done=, failed=,
   aborted=, lost=)`). The platform owns the calls and lookups of every kind in 8.3; the owning
   tracks attach proposal, incident and credit transitions. The advance's `done` hook commits
   the simulator's clock with the intent, so clock and intent can never disagree after a crash.
4. **Ingest needs no intent**: a chunk's facts, rollups and `ingested_chunk` row commit together
   (ADR 0016), and REFRESH works from `config_event` rows that have no `config_refreshed` audit
   row yet, so events ingested by a process that died before its REFRESH are still handled.
5. **Fault injection at every boundary**: `CTL_FAULT=<kind>:<n>:<phase>` exits with 137 (as
   SIGKILL) the n-th time a point is reached; a fault fires once per run (an audit row records it,
   a restarted process stays disarmed). Points: every intent kind at before_t1, after_t1,
   after_http, in_t2; chunk_copy mid; chunk_transform before_t1, in_t2, after_t2; audit_write;
   bootstrap.
6. **Acceptance is crash equivalence**: a crashed and restarted run must leave the simulator and
   our facts and rollups identical to a clean run, because a restart issues no extra simulator
   call (the simulator's dial randomness depends on its request sequence). The two exceptions of
   DESIGN.md 10.2 (validation and suggest-fixes lost at after_http) are checked by invariants
   instead, when those kinds exist (M6).

## Alternatives
- **Retry advance on timeout.** Skips simulated time silently; never acceptable.
- **Make everything idempotent by checking the simulator first, with no intent rows.** Loses the
  decision record for actions that crashed, and cannot tell "never sent" from "sent, response
  lost" for the credit-spending calls.
- **Two-phase commit with the simulator.** It has no such interface.

## Consequences
- Measured in `tests/e2e/platform/test_crash_equivalence.py`: 17 cells (advance at all four
  phases with n = 1 and 3, chunk transform at three phases, mid-COPY, audit write, bootstrap)
  each finish with the same advances in the same order, exact event counts and identical facts,
  rollups and catalog; `scripts/kill_test.sh` adds random SIGKILLs of the container.
- Every advance writes four rows (decision, intent, two audit rows): about 60 per sim day, cheap.
- `intent.idem_key` stays unique across retries; an aborted intent keeps its record under the
  renamed key, so the audit trail shows it.
