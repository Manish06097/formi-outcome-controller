# 0004. Probability matching with seeded draws

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/allocator.py`,
`controller/stats/draws.py`)

## Context
- Every allocation change is a governance action (an approval slot, or an auto-apply within
  `max_shift`) and is graded at apply time. Moves must be few, bounded in harm, and explainable.
- Winners must be chosen by deterministic statistics (constraint 4). A decision must replay
  exactly from the audit store (9.6).
- Arms are explored all at once (factorial), so arms listed late are tested as early as arms
  listed first.

## Decision
At each planning point, per cohort k and engagement dimension:

1. **Target**: probability matching on the EW draws, `share_a proportional to P(a is best in
   k)`. P(best) uses the cohort-level log ratio of each arm vs control per draw, with the
   control at 0. It is projected onto bounds by water-filling (bisection on a scale c with
   `x_i = clip(c raw_i, lo_i, hi_i)`):
   - every live undecided arm >= 2%;
   - the control >= max(holdout, 2%);
   - an undeclared arm <= 50%;
   - an arm with a decay alarm may not increase;
   - removed and futile arms 0;
   - options outside the experiment unchanged.
2. **Step**: `proposal = current + lambda_k (target - current)`. lambda_k is the largest value
   passing the population gate, this cohort's gate and the guardrail forecast (and TV <=
   `max_shift` when routed to auto-apply). It is found by 12 deterministic halvings, cohorts in
   config order, default last. Then every lambda_k is scaled by the largest common factor that
   passes the gate with all cohorts moved together.
3. **Layering** (5.2): `default` carries its own distribution; a cohort gets an override only
   when it diverges.
4. **Removal**: an arm with EW `P(theta < 0) >= 0.8` in a scope is removed there (risk-reducing).
5. **Minimum gain**: an exploitation move whose expected relative engagement gain over the cells
   it touches is below 0.25% is skipped. Exploration (an arm gaining its first share) and
   risk-reducing moves (no arm gains) are exempt. If removals were bundled into a skipped move,
   they go out alone.
6. **Harm stop** (every step): EW `P(theta < 0) >= 0.95` and expected loss `share x |E theta|
   >= 0.5%` gives a P1 move of that arm's share to the control.
7. **The experiment row pins the control.** The allocator opens one experiment per dimension
   (returned in `AllocationPlan.new_experiments` for the caller to persist) with the incumbent
   as control. (Found in M5: without a persisted control the "incumbent" was re-derived from
   the live allocation and flipped to a challenger once that challenger led.)
8. **Seeds**: draws come from PCG64 with seed `derive_seed(run, workflow, decision_id,
   fit_kind)` (sha256, 63 bits). The same data and seed give bit-identical plans
   (`tests/unit/test_t3_determinism.py`).

## Alternatives
- **Fixed split (10% per arm)**: fails the gate on day 0. One x0.88 arm at 10% makes the launch
  about -1.2%, graded harmful.
- **Plain Thompson sampling**: random per decision (constraint 4). It moves traffic every step
  (each move a governance action) and has no bound on per-change harm.
- **Successive elimination / fixed-horizon A/B**: needs equal sizes and a known horizon, and
  wastes traffic on clearly bad arms until the horizon.
- **UCB**: deterministic but optimistic. It pushes traffic to the least known arm, which is
  exactly what the harm gate must restrain, so the two would fight.

Probability matching is Thompson's allocation rule computed as a deterministic expectation. It
reads as "each arm gets traffic in proportion to the chance it is best, limited by the harm
gate".

## Consequences
- Launch: about 4% per challenger at the 2% cohort line; welcome arms (2 challengers) and voice
  arms (3 challengers) get the same lambda, so welcome arms start a little larger than voice
  arms (P(best) is 1/3 vs 1/4).
- Null arms drift around under P(best) noise. The minimum-gain rule stops most of that churn
  from costing approval slots (`test_minimum_gain_skips_a_tiny_exploitation_move`).
- Cost: one full plan over 14 cohorts at public_03 scale takes 1.3 to 1.6 s (about 300 gate
  evaluations). Fine at a few planning points a day per workflow. If needed, the cohort
  bisections are independent and can run in parallel.
- Removal and harm stop read the EW posterior, so a decaying arm loses traffic before any
  alarm.
- The end-game freeze (4.2) and routing are the planner's job. The allocator takes `max_tv` and
  `t_apply` from it.
