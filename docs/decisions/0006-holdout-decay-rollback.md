# 0006. Holdout, decay monitor and rollback timing

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/decay.py`,
`controller/learning/winners.py:holdout_size`); tested by `tests/unit/test_decay.py` and
`tests/unit/test_decaying_winner.py`

## Context
- Variants can decay: the multiplier starts falling some time after the variant's first dial
  anywhere, then declines gradually (UNDERSTANDING 5). The grader's decay metric counts the
  share of dials still on the variant every day from onset.
- A rollback at onset is graded harmful: at onset the variant is still at full strength, so
  moving traffic off it lowers expected engagement.
- After a promotion, the only concurrent comparator for the winner is a holdout on the control.

## Decision
- **Holdout**: h = the smallest of {5, 10, 15, 20}% whose one-day SE of log(winner / holdout),
  `sqrt((1-e)/(e N h) + (1-e)/(e N (1-h)))`, is <= 0.075; capped at 20%. Never removed late.
- **Monitor**: every non-control arm with >= 10% share in a scope, and every promoted winner
  against its holdout. Per completed half-window segment: `z_j = (log RR_j - theta_ref) /
  SE_j`, where RR_j is Mantel-Haenszel across (cell, prompt) strata of the scope. Page CUSUM
  for a decrease, `S_j = max(0, S_{j-1} - z_j - 0.5)`, alarm at 4. Folding is idempotent by
  `seg_last`, so a restart or a repeated call never folds a segment twice.
- **(Found in M5) The reference must be an estimate, not the prior.** A monitor starts only once
  the arm has the 200-call evidence floor in the scope, with `theta_ref` = the stationary
  posterior mean then. `restart()` re-takes the reference at promotion, as 5.7 specifies. Before
  this fix, a monitor started at 10% share on day 0 had `theta_ref` about -0.02 (prior), so a
  real decay from x1.15 to x0.93 read as about one SE per segment and never alarmed.
- **An alarm only freezes increases** (allocator) and starts the rollback evaluation. It never
  rolls back by itself.
- **Rollback timing**: predict the arm's effect to the expected apply time t_a. The prediction
  is the EW posterior plus a local linear trend over the last 4 segments, with the slope drawn
  from its standard error (seeded). Roll the arm's share in the scope back to the control when
  `E[de_rollback(t_a)] > 0` and `P(de_rollback(t_a) < -1%) <= 10%`. Otherwise cut the largest
  share that passes the 10% tail.
- The same apply-time prediction guards ramps and promotions (ADR 0002): a credibly falling arm
  must also pass the gate at its predicted effect then.

## Numbers corrected by the tests
- In-control ARL of the one-sided CUSUM (k 0.5, h 4) is about **336** segments (simulated 300
  to 380; the standard one-sided value). DESIGN.md 5.7 says 168, which is the two-sided value.
  So a false alarm per monitored arm in a 10-day run is about 6%, not 12%. The out-of-control
  ARL (8.4 at 1 SE) is the one-sided value and stands.

## Alternatives
- **Roll back at the alarm**: graded harmful when the alarm comes near onset (the variant is
  still above the control), and the alarm is a change-point signal, not a sign test.
- **Compare against the arm's peak**: detects decay but not whether rollback helps yet; the
  decision must be about the predicted level vs the control at apply time.
- **A fixed reference from launch**: the reference would be the prior (see the fix above).
- **Staggered exploration so decay clocks start when we can exploit**: tuning to a simulator
  mechanism; real novelty decay would not care.

## Consequences
- In the synthetic decaying-winner test (4 decay shapes x 4 seeds, 10 days), across 46
  rollbacks none was harmful and most were beneficial. The rollback lands as the variant crosses
  below the control, not at onset.
- Detection lag at dev volume is 1.5 to 2 days for a slow decay (5.7). During that time W3 and
  the EW gate, now judged at apply time, keep the arm from being ramped or promoted. The
  simulation showed no harmful ramp or promotion of the decaying arm.
- A decline that happens entirely between the last data point and the apply time cannot be
  seen. The loop must plan on current data (continuous ingest) and apply within the approver
  latency. With 6-hour-stale data and a 6-hour apply lag, the first version of the test loop
  promoted a winner that decayed in between (-6.5%). That is a statement about the
  orchestrator's cadence (T5): plan on fresh data.
- Persistence: `decay_monitor` holds (scope, dim, arm, cusum, seg_last, theta_ref, alarm). The
  per-segment z history is recomputed from rollups, so nothing else needs storing.
