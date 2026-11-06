# 0005. Winner rule and false-winner control

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/winners.py`,
`controller/stats/boundaries.py`); measured by `tests/unit/test_false_winner_rate.py`

## Context
- A winner promotion moves most of a scope's traffic onto one arm. A false winner (a null arm)
  costs an approval slot and is graded neutral. A negative winner is harmful and is what W5
  must stop.
- Under adaptive allocation the arm has much less traffic than the control, so the variance of
  the log ratio is the arm's, `s^2 = (1-e)/(e n) = 0.82/n` at e = 0.55, not the equal-size
  1.64/n.
- A cohort effect has a wider prior (sd 0.12) than a population effect (0.06), so the same
  posterior threshold is a looser test at cohort scope.
- Hypotheses per workflow: arms x (1 + eligible cohort scopes). Looking every planning point
  would multiply chances to cross.

## Decision
- **Looks** on an information-doubling schedule: the first look once the arm has >= 200
  connected calls in scope over >= 2 calendar days, then each time the count doubles. Every
  look is a persisted `winner_look` row (`LookRecord`), so the schedule survives a crash.
- **Rule** at a look:
  - W1: STATIONARY `P(theta > 0) >= c_k`, with c = 0.995 for the population and 0.998 for a
    cohort.
  - W2: `P(best) >= 0.95`.
  - W3: EW `P(theta > 0) >= 0.90` and `E_EW >= 0.5 E_stat`.
  - W4: no decay alarm, CUSUM below h/2.
  - W5: the promotion passes the harm gates on EW draws.
  - W6: the guardrail forecast passes.
- **Futility** at a look: `P(theta > ln 1.02) <= 5%`.
- **Winner scope**: population, or a cohort with >= 15% of daily connected calls.
- **(Found in M5) W1 reads the tail from the scope effect's moments**, `Phi(mean / sd)`, not by
  counting draws. A 0.5% tail counted from 4,000 draws has a Monte Carlo sd of about 0.11
  points (22% relative), and a posterior whose true tail is 1% shows <= 0.5% often enough to
  inflate false winners measurably. The scope effect is Gaussian for the Beta-binomial (a sum
  of Gaussian parameters) and very nearly so for the ridge-logit. W2 and W3 sit at 5% and 10%
  tails, where counting is accurate enough.
- **(Found in M5) W3 and W5 also hold at the expected apply time** when the arm's trend is
  credibly falling (ADR 0002).
- A declared winner closes its hypothesis. Its other arms are removed in that scope.

**The bound.** W1 is the test `z >= z_c sqrt(1 + s^2/v)` (`effective_boundary`). Over looks at
200, 400, 800 and 1,600 calls, the per-hypothesis union bound is 0.54% at population scope and
0.47% at cohort scope (`test_boundaries.py` reproduces the 5.5 tables). A single 0.99 threshold
would give 1.28% and 2.8%.

**Measured** (300 null replicates x 15 hypotheses, real model, allocator, gates, promotions and
rollbacks, 10 days at 3,000 connected a day):

| | Population | Cohort |
|---|---|---|
| Full rule (declared winners) | 0 / 1,500 = 0% | 2 / 3,000 = 0.07% |
| W1 alone, first four looks | 0.73% | 0.20% |
| Union bound at the looks that actually happened | 0.69% | 0.32% |

## What the measurement taught (honest)
- At 3,000 connected a day the first look comes later than 200 calls (an arm is past 200 before
  the 2-day floor), so the looks are at larger n, where the boundary is lower. The union bound
  at the actual looks is 0.69%, not 0.54%. W1 alone matches it.
- The doubling continues past 1,600 at load volume (largest look about 9,000 calls). Each
  further look adds about 0.35% of null crossings to W1 alone, so at public_03 volume "0.54%
  per hypothesis" understates W1's rate. W2 and W3 keep the full rule far below 1% anyway.
  If the lead wants W1 alone bounded too: cap looks at 4, or spend alpha (raise c_k at looks
  5+). That is a [T] settings change (`look_max`), listed in the T3 report.
- Before the moments change, W1 counted from 1,000 draws crossed for 0.93% of population
  hypotheses. That is why the moments version exists.

## Alternatives
- **c = 0.95 or 0.99 everywhere**: more power at the 800 look (0.87 or 0.63 vs 0.52 for a +10%
  effect), 18x or 2.4x the null bound. The cost of a false winner (a slot, a neutral change) is
  low, but the cost of a promoted negative arm is not, and the rule is also our answer to
  "winning by lunch".
- **Group-sequential alpha spending (O'Brien-Fleming)**: tighter and exact, but needs a
  planned maximum information that adaptive allocation does not give. Doubling looks with
  per-scope thresholds and a computed bound are simpler to defend.
- **Frequentist z test without a prior**: ignores the shrinkage that makes small-n crossings
  rare, and is harder to combine with the harm gate's posterior.

## Consequences
- Power at population scope for a +10% effect: 0.52 at the 800 look, 0.93 at 1,600. At dev
  volume a population winner is possible from about D4, a 50% cohort winner from about D6.
- Holdout on promotion: the smallest of {5, 10, 15, 20}% with a one-day SE of log(winner /
  holdout) <= 0.075, capped at 20% (950 a day: 20%; 13,500 a day: 5%).
