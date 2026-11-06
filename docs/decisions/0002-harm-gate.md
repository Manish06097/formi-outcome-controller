# 0002. Harm gate mirrors the grader, plus a cohort line

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/harm.py`)

## Context
- The grader judges every controller change at apply time, over the active leads, against the
  config it replaced: harmful if expected engagement falls by more than 1% or expected caller-ID
  health by more than 2% (grader.py:67-73, world.py:634-655). Exploration arms, ramps,
  promotions, rollbacks, DID probes and drains are all such changes.
- The grader judges the population only. Compliance ("worse for a cohort of our customers, even
  briefly") and the CTO ("an improvement that hurts a cohort is worth less than nothing") judge
  cohorts. Uniform 3.8% arms can push one cohort below -1% while the population stays neutral.
- We do not know the true multipliers; we have a posterior over them.

## Decision
One function judges every change except a P0 prompt fix, from our own posterior draws:

    population:  P(de   < -1%)  <= alpha_H     (2.5%; 10% for rollbacks)
    per cohort:  P(de_k < -x_k) <= alpha_H     for every cohort k of the NEW config (and default)
    caller ID:   P(dd   < -2%)  <= alpha_D     (5%)
    guardrail:   predicted drop <= 0 and P(drop > x) <= 2.5%   (ADR 0007)

- `de = E_new / E_cur - 1` with `E_X = sum_c leads_c * base_c * prod_dim sum_o p_X(cohort_X(c),
  dim)[o] * exp(L_{dim,o}(c))`. This is the simulator's own expected-engagement formula
  (world.py:288-305) with our effect draws in place of the hidden multipliers, averaged over our
  tracked active leads, the population the grader samples.
- `x_k` comes from the effective policy: 2% by default, 1% where a directive asks for it
  (Northwind). The line is ours, stricter than the grader.
- `dd` uses the DID posterior (`DidPosterior`, track T4). A caller-ID change with no DID
  posterior **fails closed** (`did_unmodeled`), never a silent pass.
- An option without an effect map (the control, or an unmodelled option) has multiplier 1.
- Every number in the report is stored with the decision (`HarmReport.to_json`), so a reviewer
  can replay "why was this allowed".
- (Added during M5, found by the decaying-winner simulation.) The grader judges at APPLY time,
  so the allocator and the winner rule also judge a change at its expected apply time. When an
  arm's last four segments show a credibly falling trend, the change must pass on the EW draws
  AND on draws whose effect is extrapolated to the apply time. Both are required: a pessimistic
  prediction alone is not conservative, because it makes cutting a still-good arm look
  harmless (seen in the simulation: a -4% cut).

What the gate implies, computed (and checked by `tests/unit/test_harm_gate.py`):
- First exploration, 5 untested arms, tau 0.06: `1.96 x 0.06 x sqrt(5) x s <= 0.01` gives
  s = 3.8% per arm. The Monte Carlo gate gives 3.8% to 4.0% (the exp() adds a small Jensen term).
- A 2% cohort line does not shrink it; a 1% line halves it (1.9%), because a cohort's prior sd
  is about 0.12, twice the population's.

## Alternatives
- **Point-estimate gate** (expected de >= -1%): passes half of the changes whose true effect is
  right at the line; the grader would see them as harmful.
- **Population line only**: what the grader checks, but fails the cohort promise in Compliance's
  memo and lets cohort-specific harm hide inside a neutral average.
- **One alpha for everything**: a rollback's target is a known baseline and a decaying variant
  costs engagement every hour, so a 2.5% tail would leave decaying variants live too long; 10%
  is the deliberate asymmetry.
- **Gate on the stationary posterior**: a peaked-then-decaying variant would pass on stale
  evidence (judge finding F1). The gate uses the recency-weighted (EW) draws.

## Consequences
- Exploration is small while posteriors are wide (about 4% per arm at launch, 2% under a 1%
  cohort line) and grows as evidence accumulates. That is the "slow and right" pace Northwind
  asked for, and it costs about 1.5 days of time to the first look under the 1% line.
- A calibrated posterior gate is wrong at about its tail rate. In the decaying-winner
  simulation, 2 of 343 applied changes were harmful by truth (both mild cuts of a +5% arm on
  posterior noise), inside the 2.5% design tail. Lowering alpha_H trades that for slower ramps.
- P0 prompt fixes are the one exemption (7.1): an unsafe prompt is a compliance failure, so its
  gate is the safety verdict plus the guardrail check.
- The gate needs the active-lead population and cell features in the snapshot. They started as
  a local extension (`snapshot.LearningSnapshot`) and are `WorkflowSnapshot` fields since step I1
  (ADR 0028).
