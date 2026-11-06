# 0014. Local policy replica

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- `PUT /workflows/{id}/config` succeeds only if auto-apply is enabled, every changed dim is in
  `allowed_dims`, the largest unweighted total-variation shift of any cohort's welcome, voice or
  caller-ID distribution is at most `max_shift` (+1e-9), something changed and a rationale is
  present. Every refusal is logged as `auto_apply_rejected` and counts against governance hygiene,
  so probing by trial and error is not an option.
- The simulator's `changed_dims` has quirks: the cohort list is compared in order; distributions
  are compared through the `default` fallback over the union of allocation keys; any schedule
  difference, `lead_preferred_hour` included, is a `schedule` change. It re-normalises every body
  it receives, and about 23% of normalised weight vectors do not sum to exactly 1.0, so a stored
  distribution sent back unchanged can move in its last bits and count as changed (ADR 0027).

## Decision
- `governance/policy.py` has two layers.
  1. `sim_verdict(aa, live, proposed_stored)` replicates the simulator's check exactly, returning
     the changed dims, the not-permitted dims in the simulator's order, the max shift, "no change"
     and the rationale check, and can print the simulator's own problem strings.
     `changed_dims` delegates to `Config.changed_dims` (M0, the simulator's diff); `max_shift`
     ranges over `default` plus every old and new cohort id.
  2. `check_auto_apply` / `auto_apply_permitted` is our rule: the replica with a **1e-6 margin**
     (`max_shift - 1e-6` [T]) and directive-narrowed `allowed_dims`, plus directives, risk labels,
     the velocity budget, chaining, one change in flight, holds and the notice period. It can only
     refuse more than the simulator.
- The proposed config is always compared in its stored form (`proposed.as_stored()`), so a
  last-bit drift is predicted, not discovered.
- A composition whose stored diff differs from the dims it was labelled with is refused
  ("composition stale").
- **Differential test** (`tests/e2e/test_policy_differential.py`): 2,100 generated bodies against a
  live simulator with three policies (disabled, every dim with max_shift 0.05, the shipped cancel
  policy), biased to the edges (shifts at `max_shift` plus or minus 1e-7, 2e-6, 1e-3; scaled
  weights; zero weights; reordered, added and removed cohorts; overrides equal to the default;
  hour tables and per-person hours). Every answer agrees: 614 accepted with identical changed
  dims and a bit-identical stored config, 1,469 refused with identical problem lists (725 on the
  shift, value to 3 decimals), 17 predicted 422s confirmed. The one-sided guarantee holds: our
  rule never permitted a request the simulator refused. A planted bug (ignoring
  `lead_preferred_hour`) makes the test fail on dozens of pairs.
- A 403 in production means the replica is wrong: page `AUTO_APPLY_REJECTED`, mark the proposal
  FAILED_POLICY, route that workflow's auto-applies to approval until the policy is re-read, and
  never retry the body.

## Alternatives
- *Probe auto-apply and fall back to approval on 403.* Each probe is a graded rejection.
- *Use the same 1e-9 tolerance as the simulator.* The simulator sums TV terms in set-iteration
  order, which changes with the process hash seed; at the boundary the last ulp can go either way.
  1e-6 is far above that noise and costs nothing measurable (a shift within 1e-6 of the cap routes
  to approval; 130 such pairs in the differential run).

## Consequences
- Simulator changes to the policy rules show up as differential-test failures before a graded run.
- Directive narrowing appears as "dims not permitted" in our reasons, which is what the supervisor
  view shows as "policy permits, client agreement forbids" (ADR 0012).
