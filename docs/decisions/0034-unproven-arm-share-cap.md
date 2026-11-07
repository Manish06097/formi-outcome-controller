# 0034. Cap an unproven arm's share at the monitoring floor (built, not enabled)

Status: implemented behind `decay_clean_segments` (default 0 = off). Measured, and rejected as a
default (2026-11-07). Follows `docs/design/build-reports/ENG-GAP.md`.

## Context
In the p02 family `nw_renewal` ends below doing nothing. Controlled runs on public_02 (engagement
learner off: +5%; supervisor edit removed: -11%; W3's decay removed from the scenario: +21%) show
the driver is a decaying welcome arm. The allocator ramps W3 on the pooled posterior while it still
looks good (5% on D0, 30% in cohorts by D3). The decay monitor arms at 10% share and needs 200
calls, so the CUSUM alarm came at D6.6, about four days after onset, and the loss was already taken.
ENG-GAP fixed the gate defects that delayed the rollback afterwards (a plan at the edge of its own
gate failed the release gate on noise and held the whole lever). What is left is the ramp before
detection. I4 Decision 3 option (b) proposed the cap below; the lead asked for it.

## Decision
`allocator._bounds` keeps an experiment arm at or below `max(decay_arm_share, its current share)`
(the monitoring floor, 10%) until its decay monitor has folded `decay_clean_segments` segments
without an alarm (`decay_monitor.seg_first`, migration 0040, `allocator._decay_watched`). Declared
winners are exempt. `decay_clean_segments = 0` switches the rule off. **The default is 0.**

The default is off because the stated guard failed. Same code, seeds from the fingerprint (I6 merged),
`CTL_T_DECAY_CLEAN_SEGMENTS` 0 against 2, `engagement_gap_captured` in points:

| scenario | ks_cancel off / on | nw_renewal off / on | harmful off / on |
|---|---|---|---|
| public_01 | +3.4 / +1.3 | +9.9 / +5.1 | 0 / 0 |
| seed_p01_s1101 | +10.4 / +2.9 | +10.7 / +5.0 | 0 / 0 |
| seed_p01_s1102 | +9.9 / +0.8 | +13.2 / +4.7 | 0 / 0 |
| effects_large_p01 | +16.1 / +2.3 | +14.1 / +5.3 | 0 / 0 |
| effects_small_p01 | +5.6 / +0.8 | +3.1 / +3.0 | 0 / 0 |
| decaying_winner_dev_p01 | +8.1 / +3.8 | +11.3 / +3.7 | 0 / 0 |
| decaying_winner_highvol_p01 | +10.8 / +1.2 | +13.1 / +5.2 | 0 / 0 |
| public_02 | +41.3 / +34.3 | -7.0 / -6.2 | 0 / 0 |
| effects_flip_resize_p02 | +42.2 / +42.2 | +7.2 / +2.4 | 0 / 0 |
| seed_p02_s2201 | +39.5 / +34.1 | -10.8 / -3.8 | 0 / 0 |
| seed_p02_s2202 | +40.4 / +34.8 | -5.4 / -5.2 | 0 / 0 |

Stable worlds (the seven p01 scenarios): the cap costs 7.3 points on ks_cancel and 6.2 on nw_renewal
on average, against a bar of about 2. On the p02 family it gains 0.8 points on nw_renewal and loses
4.5 on ks_cancel, and nw_renewal stays negative on public_02 and s2202. The 200-call evidence floor
plus two segments at 10% takes about three days at dev volume, longer than the 1.5 days estimated,
and a real winner is the thing it delays.

## Alternatives
2. Start the decay monitor at the arm's first dial instead of at 10% share. Detects earlier, leaves
   the arm at its ramp; not built (lead: do not).
3. Hold only the failing cohort's patches when an engagement line fails. Not built; with the planning
   margin and shared draws the case is rare (ENG-GAP).
4. Accept the residual: a small negative nw_renewal gap in the p02 family (about 2% of engagement),
   strongly positive elsewhere. This is what the default now does.

## Consequences
- The cap and its tests (`tests/unit/test_allocator.py`) stay in the tree as a measured, switchable
  rule; one setting turns it on. Nothing in the audit store changes by default except the extra
  `seg_first` column on `decay_monitor`.
- If the cap is ever enabled, a genuine winner is exploited about 1.5 to 3 days later (200 calls at
  10% share, then two segments), which is what the table above prices.
- A cheaper variant would lift the cap on the evidence floor alone (no clean segments), or cap only
  arms whose early effect is large (a decay needs a novelty effect to start from). Neither is tried;
  either is a design change to 5.4 and 5.7 and needs its own measurement on the table above.
