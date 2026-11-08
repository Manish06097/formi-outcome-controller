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
