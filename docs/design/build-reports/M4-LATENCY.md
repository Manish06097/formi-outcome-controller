# M4-LATENCY: caller-ID incident opens 150 min after the flag (WIP)

Test: tests/e2e/test_m4_pickup_live.py::test_flag_day_share_within_the_7_3_bound_and_zero_after[d_max_shift_005]
Assertion: `incident[0][1] - at <= T_ALARM_H * 60` -> `3630 - 3480 = 150 > 120`.

## Bisect (single case, worktree per commit, controller imported from the worktree)

| commit | what | result |
|---|---|---|
| 430dc9d | I3 report | pass |
| e920c0e | merge I4 | pass |
| 01f4aee | merge I6 (run-independent seeds) | pass |
| 32b600b | decay_clean_segments = 0 switches arm cap off (code only) | pass |
| 336b20d | arm cap default off (decay_clean_segments 2 -> 0) | FAIL |
| 81c3d1e | ADR 0034 | FAIL |
| aad1140 | merge ENG-GAP | FAIL |
| 3d83c81 | live review sheet | FAIL |
| 9fb1b29 | PERF RollupCache | FAIL |

First bad commit: 336b20d. RollupCache (9fb1b29) is not the cause: the failure predates it.

## Verdict: (b), not a code regression

The first bad commit 336b20d only flips a default (`decay_clean_segments` 2 -> 0, the arm cap off, ADR 0034).
It does not touch the flag detector. It changes the trajectory of the run (who holds which share when),
so the seed-101 draw lands on a different alarm tick. Evidence:

- RollupCache (9fb1b29) is cleared: the failure predates it. The detector reads fresh rollups (the cache key
  is the ingested-chunk state).
- Alarm delay after the flag (minutes) is a spread of 15-minute ticks, in both configurations. Driver:
  full run of max_shift_005_did_third_p01 with only `seed` overridden, `CTL_LEARNER=probe`, cap off is the
  HEAD default, cap on is `CTL_T_DECAY_CLEAN_SEGMENTS=2`.

| seed | arm cap off (HEAD) | arm cap on (as at 01f4aee) |
|---|---|---|
| 101 (the scenario's) | 150 | 60 |
| 102 | 105 | 105 |
| 103 | 90 | 150 |
| 104 | 75 | 135 |
| 105 | 120 | 75 |
| 106 | 150 | 135 |
| 107 | 90 | not run |

Cap off: 7 seeds, mean 111, 2 of 7 above 120. Cap on: 6 seeds, mean 110, 3 of 6 above 120 (150, 135, 135).
So the arm cap does not make detection faster or slower on average; seed 101 simply passed with it
(60 min) and fails without it (150 min). I3's "alarm after 120 min" was one draw as well.
- Why it varies: the fast detector (DESIGN 7.3) needs >= 100 dials on the DID in a 2, 4 or 8 h window, z <= -4
  and r <= 0.75. At a one-third share the z statistic crosses -4 after a random number of dials, so the alarm
  tick is random with a spread of about 75 to 150 minutes. The 2-hour assertion sits at the middle of that
  spread, so the test passes or fails by the draw. The flag-day share assertions of the same test (bound and
  zero on later days) pass; only the alarm-time line fails, so the harm bound is not at stake.

No code or threshold was changed.

## Test changes
- None for the M4 test (a design call, see Decisions).
- tests/e2e/test_m3_governance_live.py::test_peak_requests_within_c_eff now asserts routine (P2, P3) count in
  any rolling 24 h <= c_eff and total <= policy cap, and prints the class mix on failure. See Task B below.

## Task B: policy cap check
All request creation goes through `Lifecycle._blockers` -> `timing.fatigue_ok` (also for supersedes);
`used()` counts every record the simulator lists plus ours not yet listed. `fatigue_ok`: urgent classes
(P0, P1) need `n + 1 <= c_policy`; routine need `n + 1 <= routine_budget (c_eff - reserve)`. The only other
caller of `request_approval` is the intent replay of the same stored body. So the controller cannot exceed the
policy cap for any class, and P0/P1 exceed only a directive (memo) cap through that documented path. No bug.
docs/LIVE_REVIEW.md section 4 has the weakness note.

## Commands
- Single case: `CTL_T1_SIM_PORT=8795 CTL_TEST_PG_DSN=... .venv/bin/python -m pytest tests/e2e/test_m4_pickup_live.py -k "d_max_shift_005 and flag_day_share"`
  (about 2 min); bisect = same command inside `git worktree add --detach <dir> <commit>` with the main venv
  (import path checked: controller is imported from the worktree).
- Seeds: a driver that runs the sim with `seed` overridden and the controller with `CTL_LEARNER=probe`, then reads
  `incident` (kind did_degraded) from the run schema.

## Decisions for the lead
1. Keep the 2 h bound as a typical value and make the test statistical, or change it to the 95th percentile of
   the observed delay (about 150 min = 10 ticks at this scenario) and keep the flag-day share formula at its
   own T_ALARM_H? The share bound at 150 min is looser, so also decide whether DESIGN 7.3's "2 hours at a
   one-third share" is a mean or a promise.
2. Or make the detector faster (for example a 1 h window or lower min dials): a design and threshold change,
   not made here.

## Final test run (HEAD plus these commits)
- tests/e2e/test_m3_governance_live.py: 54 passed (11 min).
- tests/e2e/test_m4_pickup_live.py: 29 passed, 15 skipped, 1 failed (d_max_shift_005 alarm-time assertion, the knife edge above).
