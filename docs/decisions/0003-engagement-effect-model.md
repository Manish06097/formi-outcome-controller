# 0003. Engagement effect model: Beta-binomial first, ridge-logit second

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/engagement.py`).
Section 17.2 of DESIGN.md overrides the original single-model decision of 5.3.

## Context
- Welcome and voice effects are multiplicative, often cohort-specific and sometimes sign
  flipping (a voice x1.22 for 60+ and x0.88 for 18-30). They are sampled independently per dial
  from the lead's cohort distribution, so every allocation is a full factorial.
- Volumes are thin at dev scale (about 950 connected a day on nw_renewal), and allocations
  change over time. Day-level shocks (a supervisor prompt edit) can coincide with an arm ramping.
- The harm gate, allocator, winner rule and decay monitor all need the same thing: per-cell log
  engagement ratios of each option against its dimension's control, as seeded draws.
- 17.2: build the simple model first and fully test it. The ridge-logit becomes the default only
  if it beats the Beta-binomial on the private scenario variants (gap captured, harmful count)
  with no harmful change. Both stay behind one setting.

## Decision
Both implement `types.EngagementModel` and are chosen by `CTL_ENGAGEMENT_MODEL`
(`beta_binomial` is the default; `make_engagement_model`). Consumers see only `Draws` and
`EffectCache`, so swapping is one setting.

**1. Per-cohort Beta-binomial (ships first).** Per dimension, cohort k and non-control arm a:
- The observation is the Mantel-Haenszel log rate ratio of a vs the control, stratified by
  (segment, prompt), with its delta-method variance and the covariance that arms of the same
  cohort get from the shared control. With one stratum this is exactly the log ratio of two
  Beta-binomial rates with variance `(1-p1)/(n1 p1) + (1-p0)/(n0 p0)`, the unequal-size
  variance of 5.5. The strata matter under adaptive allocation: a prompt edit during a ramp
  fakes a 14% loss when pooled and shows none when stratified (`test_engagement_models.py`).
- Effect of a in cohort k: `theta_a + u_{a,k}`, with `theta_a ~ N(0, tau^2)` the
  lead-weighted population effect and `u_{a,.} ~ N(0, 3 tau^2 P)`. P projects out the
  lead-weight direction, so the deviations average to zero and the population prior sd is
  exactly tau = 0.06. A cohort's prior sd is about 0.12, the design's "theta plus three deltas".
  Those two numbers are what the 5.4 cost table and the 5.5 boundaries assume.
- The posterior is Gaussian in closed form (Kalman form, which handles the degenerate prior).
  The exact pooled Beta parameters per (option, cohort) are kept in `Posterior.extra` for the
  supervisor view.
- STATIONARY fit: segment weight 1, prior tau = 0.06. EW fit: counts scaled by 2^(-age/2) (a
  power likelihood, half-life 12 dialling hours) and prior `tau_H = max(0.06, tau_EB)`, where
  tau_EB is the method-of-moments dispersion of informative arms. The EW posterior feeds the
  gate and allocation, and more dispersion makes it stricter. The stationary prior stays fixed,
  so the winner rule keeps the boundaries it was sized with.

**2. Factorial ridge-logit (5.3).** `logit P(engaged) = beta[cell] + gamma[segment] +
pi[prompt] + theta_W[a] + sum_f delta_W[a, f, level_f] + theta_V[b] + sum_f delta_V[b, f,
level_f]`, f over lang, age_band, city_tier. theta and delta ~ N(0, 0.13^2) on the logit scale;
intercepts N(., 2^2). Ridge IRLS on aggregated counts, `X'WX` by bincount over flattened index
pairs, Laplace posterior. The per-cell log ratio is `log sigmoid(base + delta) - log
sigmoid(base)`. It recovers the per-age voice flip that the Beta-binomial averages within a
cohort.

**Selection rule (17.2), owned by the private-variant harness, not by code:** run both models
on every private variant. Switch the default to `ridge_logit` only if, summed over variants, it
captures more of the engagement gap AND has no more harmful changes AND has zero harmful
changes on every variant. Otherwise the Beta-binomial ships and the ridge-logit is documented as
the next step. The setting enters `settings_hash`, so every decision row records which model
made it.

## Alternatives
- **Ridge-logit only (the original 5.3):** more power through feature-level pooling, but 300 to
  450 parameters, an IRLS loop and a Laplace approximation are more to defend and debug. 17.2
  makes it earn its place.
- **Unstratified Beta-binomial per (arm, cohort):** simplest, but biased by Simpson's paradox
  whenever allocation moves over time while day-level engagement moves (shown by the test).
- **Independent cohort effects without the sum-to-zero constraint:** the population prior sd
  would depend on how many cohorts the grid has (0.12 with one cohort, about 0.064 with twenty),
  so the launch exploration share would depend on the grid. The constraint pins it at 0.06.
- **Thompson sampling from Beta posteriors directly:** random per decision (constraint 4).

## Consequences
- Measured at public_03 scale (252 cells, 13,500 connected a day, 20,606 rows, Apple M3 Pro,
  `scripts/bench_engagement_fit.py`): Beta-binomial fit 0.04 s stationary / 0.05 s EW (75
  parameters); ridge-logit 0.14 s (353 parameters), inside the 100 to 300 ms estimate; 4,000
  draws 2 ms / 12 ms. Neither is the bottleneck: one full allocation plan over 14 cohorts takes
  1.3 to 1.6 s.
- The Beta-binomial learns cohort effects only for cohorts of the live grid. Off-grid
  heterogeneity (an age effect inside a lang cohort) is invisible to it and visible to the
  ridge-logit. This is the main reason to run the comparison.
- Cohort membership comes from the live config's first-match cohorts over the cell catalogue.
  Calls made under an older cohort structure are attributed to today's cohorts, which is exact
  for allocation effects as long as allocations were uniform within today's cohorts.
- Not done: empirical-Bayes widening for the ridge-logit prior (tau_E is fixed), and the
  interaction LR test (cut, 5.3).

## Decision update: the default after the 17.2 comparison (lead, 2026-11-07)
Status: accepted. The default stays the stratified Beta-binomial. Ridge-logit stays available
behind `CTL_ENGAGEMENT_MODEL=ridge_logit` and is not removed.

Evidence (`docs/measurements/engagement-model-selection.md`, 13 scenarios, each graded against the
no-op baseline of the same scenario): neither model made a harmful engagement change in any
scenario (0 and 0). Mean gap captured is +6.0% for the Beta-binomial and +2.8% for the ridge-logit.
The rule fixed in advance (17.2) said the ridge-logit becomes the default only if it captures more
gap and is no more harmful; it did not capture more, so the rule keeps the simpler model.

What this does not show, stated so it is not oversold:
- The ridge-logit is the safer model on the public_02 family: nw_renewal gap is -14.2% (public_02),
  -16.4% and -6.3% (seeds) for the Beta-binomial against -1.2%, -5.4% and -1.6% for the ridge-logit.
  Both are negative there, so the cause is not the model. The step I4 hypothesis is the
  supervisor's mid-run edit of the default voice; it is not verified (open item, see the I4 report).
- The Beta-binomial cannot see heterogeneity inside a cohort of the live grid. The ridge-logit can,
  and no scenario in the private set exercises that strongly. That is a real gap in the evidence.
- Wall time: the ridge-logit cell takes 1.3x to 1.7x longer end to end on this machine.

Consequences: the simpler model is the one to explain in the live review. If a hidden scenario
shows off-grid heterogeneity, switching is one environment variable and a re-run of the
comparison; the model interface is the same.
