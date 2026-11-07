# 0030. Pickup levers in the loop

Status: accepted (2026-11-07), integration step I3 (milestone M4)

## Context
I2 ran governance with a probe-only learner. On public_02 it made one harmful change: a caller-ID
probe onto a DID that the model had re-admitted after it was flagged (didΔ -2.1%). Nothing
checked a caller-ID change against a harm gate, flagged DIDs were only caught by the slow path,
and a drain could only be planned at a routine slot (AUDIT-1 findings 1, 2, 8).

## Decision
1. **Caller-ID harm gate before the overlay.** `DidHealthModel.plan_caller_ids` draws `did_posterior`
   and cuts every change with T3's `did_gate` (P(dd < -2%) <= alpha_d). A change cut to nothing is a
   `no_action` decision plus a `HARM_GATE_BLOCK` alert. Probe sizes come out of the gate itself.
2. **Why I2 was harmful, and the fix.** A DID degraded by the fast path, below the load floor, or
   judged unhealthy by the SPRT was re-admitted as a probe after 3 days. A probe carries almost no
   load, so load cannot explain a low reading: re-probing only re-measures a bad DID on real calls.
   Now only slow-path or history degradations are re-admitted (`READMIT_PATHS`), and a re-admitted
   probe goes through the gate with the prior estimate it was degraded with.
3. **Flagged-DID detector and P0b drain chain in the loop** (`loop/pickup.py`). DETECT runs the
   multi-window detector and the drift CUSUMs after the SPRT and slow path; incidents, DID states
   and drift states are rows (`incident`, `did_state`, `drift_monitor`), restored on every start.
   Every stop, DECIDE asks each draining incident for its next chain step (one `max_shift` step),
   so a drain does not wait for a routine slot (AUDIT-1 #1). A DID that turned degraded or unhealthy
   in DETECT is also planned in the same stop.
4. **Verification.** `LifecycleDeps.verifiers` gets caller-ID (7.3 `caller_change_check`) and
   schedule (5.8 band) verifiers, so VERIFYING proposals resolve and a failed one rolls back
   through a P1 patch (AUDIT-1 #2).
5. **Slow path.** It needs 100 dials on the DID and on its comparator (`did_slow_min_dials`,
   AUDIT-1 #8). A slow-path shortfall does not degrade a DID by default
   (`did_slow_path_degrades=False`): in the loop, a DID carrying its old share after a spread read
   0.6 to 0.8 for days and was degraded, all traffic moved to the other DID, which overloaded
   (ping-pong on public_02). Only a UCB under 0.5 (`did_slow_floor_r`, load cannot explain it)
   degrades. Healthy DIDs get equal shares unless one carried at least 0.9 of the other's dials.
6. **Fast detector reference window** (`did_fast_ref_h`): an alarm needs the DID credibly healthy in
   the 8 hours before the alarm window. **KPI drift** judges a block against the trailing 3 dialling
   days (`drift_baseline_days`), because history starts with no caller-ID load and read 10% high.
   A sole DID marked suspect by drift lapses after `suspect_defer_min`.
7. **Cohort grid at bootstrap** is recorded in `cohort_def` with a `5.2/build_grid` decision, and
   reaches the config through governance together with at least one hour table. Hour tables give
   every in-window hour a positive weight (floor 0.25).
8. **Workflow appearing mid-run** (AUDIT-1 #13): ids not in the run catalog are reported once and
   skipped. `Thresholds.schedule_revert_lookback_min` is read, no duplicate constant (#14).

## Alternatives
- Gate only at submit: rejected, a late approval or notice would skip it.
- Keep re-admitting every degraded DID: rejected, it is the I2 harm.
- Degrade on the slow path always: rejected, it ping-ponged load (point 5).

## Consequences
No schema change was needed: `did_state`, `incident`, `drift_monitor` and `cohort_def` already
existed, so migrations 0016 to 0019 are unused. A sole DID with no comparator is detected by drift
only after a KPI drop, not by the comparator-based detector.
