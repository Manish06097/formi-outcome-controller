# 0008. Caller-ID probing, classification, spread and drains

Status: accepted (2026-11-06), milestone M4, track T4

## Context
Caller-ID health is the largest pickup lever (DESIGN.md 5.9) and a flagged DID is an incident
with no event (7.3). The grader judges every change at apply time and calls it harmful if
expected caller-ID health falls by more than 2%, so probing an unknown DID is itself a risk: a
10% probe onto a flagged DID is about -3.6% health. We must learn which DIDs are healthy, spread
load over them, and pull share off a DID that goes bad mid-run, all from per-DID pickup.

Two facts make the statistics clean: DIDs are sampled per dial independently of the lead, so a
DID's pickup against the other DIDs dialled in the same hour and cohort is a concurrent and
unbiased comparison; and `r_did` is keyed by tick, so any trailing window is exact.

## Decision
1. **Ratio.** `r_d` = Mantel-Haenszel risk ratio of DID d against the pooled other live DIDs,
   stratified by (cohort, hour of day), with the Greenland-Robins variance of `log r`. With
   zero connects on d the log-scale SE is dominated by the continuity correction and understates
   the evidence, so the Cochran-Mantel-Haenszel score statistic is used as z instead (a dead DID
   with 100 dials against a 25% comparator is z of about -5.8 by score, -2.9 by log ratio).
2. **Probe size** from the DID harm gate, then volume: an unknown DID is flagged with prior 0.2 at
   r = 0.35, so a probe must be safe even if flagged, `s (1 - 0.35) <= 0.018`, giving 2.8%;
   `s = max(0.5%, min(2.8%, 400 / dials_per_day))`; at most two unknown DIDs probing at a time
   (P(at least two of three flagged) = 0.104 > 5%). No new probes in the end-game freeze and
   none inside an incident drain (a drain must stay risk-reducing).
3. **Classification by a dial-count SPRT**, not a day timeout: Wald's test of r = 0.70 against
   r = 1.0, alpha 1%, beta 5%, each batch using the comparator pickup of the same (day, hour,
   cohort) block, truncated at 1,000 dials (healthy iff r_hat >= 0.85). The controller feeds
   per-tick batches; decisions are checked at batch ends (Wald's test observed at batch ends).
   Measured through the production code path on synthetic `r_did` at renewal-like volume:
   about 248 dials at r = 1.0 (table 245), 148 at r = 1.1 (149), 133 at r = 0.64 (123), 50 at
   r = 0.36 (52); P(unhealthy | r = 1.0) about 3%, P(healthy | r = 0.70) under 1%.
4. **Spread**: equal shares over healthy DIDs (with equal health, equal split maximises mean
   health under any convex load penalty); proportional to r_hat only when one healthy DID's LCB is
   above another's UCB. Unhealthy and degraded DIDs get 0 and are re-admitted as a probe after 3
   days. With no healthy DID: keep the allocation, probe unknown DIDs, page `NO_HEALTHY_DID`.
5. **Slow path**: a healthy DID is degraded if the two-sided 95% upper bound of r_d is below
   0.85 in a trailing 12, 24 or 48 dialling-hour window (covers the 0.75 to 0.85 band the fast
   detector leaves open); the response is a P3 spread, which is risk-reducing by definition.
6. **Fast detector** (7.3): alarm in the 2, 4 or 8 h window at dials >= 100, z <= -4, r <= 0.75;
   suspect in the 1 h window at dials >= 50, z <= -2.5 (tick-sized steps, P2/P3 deferred).
7. **Drain**: where caller_id auto-applies, a P0b chain of steps of at most `max_shift - 1e-6`,
   each moving share ONLY off the drained DID onto healthy DIDs below their target, so every step
   is risk-reducing and the residual shares are exactly the `(s - j m)` terms of the 7.3 flag-day
   formula; elsewhere one full-step P0 approval. The plan is recomputed from the live config at
   every stop, so a crash between steps resumes the chain where the simulator says it is.

## Alternatives
- A fixed-width interval with a 3-day timeout: needs 415 dials at r = 1.0 and drops healthy DIDs
  on slow workflows (415 dials at a 2.8% probe on renewal volume is 4.1 days).
- Thompson sampling over DIDs: it would move share onto unknown DIDs beyond the gate size, which
  the grader can call harmful at apply.
- Crude pickup ratios: confounded by hour and cohort mix (a DID dialled more in the evening looks
  worse), which MH stratification removes (unit-tested).
- Interpolating drain steps toward the final target: also rebalances healthy DIDs, which is a
  spread (exposure-increasing), and drains the flagged DID more slowly than `max_shift` a step.

## Consequences
- Classification takes 1.1 to 2.4 days on dev workflows and 3 to 5 days on a KYC-like
  `next_campaign_only` workflow; that slow case is stated, not hidden.
- The flag-day share depends on the pre-flag share and is asserted by the formula, not as a
  fixed "under 0.10" (that fails for pre-flag shares above about 0.39).
- MH and the SPRT live in `learning/did.py` and `stats/sequential.py`; `stats/ratios.py` (T3)
  may later host the shared MH helper.
