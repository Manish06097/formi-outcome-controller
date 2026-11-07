# 0031. Engagement learning in the real loop

Status: accepted (2026-11-07), integration step I4, milestone M5.

## Context
T3 built the engagement statistics as pure functions and T2 built governance on ports with no-op
defaults. AUDIT-1 found that nothing in the live path ran a harm gate, a guardrail forecast or a
verifier: findings 2 (no re-gate, VERIFYING never resolves), 3 (the guardrail is not enforced on what
ships) and 9 (a held patch returns unchecked). This step wires all of it with one rule: every change
passes one computation before it is routed, and every decision replays from the audit store.

Running the wiring end to end also found defects the unit fixtures could not see. They are below,
because each one changes what the design's numbers mean.

## Decision

**Where the code lives.** `loop/engagement.py` holds the stage hooks, `learning/engagement_plan.py`
one function per rule that both the loop and the replay recomputers call, so a replayed decision runs
the code that made it. `learning/ws_codec.py` stores what a rule reads with the posterior it fitted,
and `learning/replay_engagement.py` registers the recomputers. `loop/governance.py` only registers the
hooks. The release train gets one new port, `gate(composition, now)`.

**The gate holds a lever, not a composition.** A failing line holds the patches of the lever it
judges: an engagement line holds welcome and voice, the guardrail line holds the pickup levers (only
they move who connects), and the rest of the composition is recomposed and re-judged. So a failing
allocation never delays a caller-ID change riding with it. P0 work is judged and recorded, never held.

**Rollbacks are judged at the apply time, ramps both now and then.** The grader judges a change when
it applies. A ramp or promotion must pass on the EW draws and on the draws predicted to the apply
time, because a pessimistic prediction alone would make cutting a good arm look harmless. A rollback
is judged at the apply time only, because a falling arm is the reason for it.

**A rollback is cut to fit the whole gate.** The planner first limited a rollback by the population
line only, and the composed gate then refused it on the cohort lines: on public_01 every population
rollback of the decaying variant failed the cohort line of every small cohort, and the variant kept
its share to the end of the run. Now each cohort's cut is the largest its own line allows, then one
common factor for the population line, on the same draws the gate will use. Northwind's 1% cohort line
makes these cuts partial at first; the next step cuts the rest.

**The decay statistic is stratified by cohort, not cell.** Shares are set per cohort, so the cohort
removes the confounding. At dev volume a segment has a few calls per cell, so stratifying by cell
dropped every cell missing one side and roughly doubled the segment's SE, which pushed the alarm from
the 1.5 to 2 days of 5.7 to more than 3.

**The trend enters only when it is credibly falling, and a rollback needs current evidence.** The
slope over 4 segments is noise at dev volume (SE about 0.06 per segment), and a healthy arm with a
noisy negative slope read as a rollback case. It now enters as in `apply_time_shifts`, when
`slope + z SE < 0`. An alarm still freezes increases for good, but a rollback needs the CUSUM still at
or above h/2: a latched false alarm otherwise gets a rollback test at every step for the rest of the run.

**The Mantel-Haenszel variance is Greenland-Robins, floored.** An arm at a few percent of a small
cohort has about one call per stratum, where the delta method gives zero variance (`a (1 - a / n) = 0`
at `n = 1`). A posterior fed that variance was far too sure of itself and fired 33 harm stops on
public_01. Greenland-Robins (1985) stays consistent with many sparse strata, and the floor (the
unstratified variance with half-event smoothing) keeps a handful of calls from looking like evidence.

**The guardrail forecast needs a connect model, and now has one.** `learning/connects.py` is the 5.8
formula per cell and hour. It runs in `guardrail_filter` before a pickup-lever patch enters the overlay
(AUDIT-1 finding 3, every workflow, pickup-primary included), in the composed gate, in the re-gate, and
it sets `BuildContext.guardrail` so the request states it. A request the forecast refuses is a recorded
decision not to act.

**"Predict no drop" has a resolution.** 5.10 says the point estimate must be at most 0. Taken
literally a neutral table with a predicted drop of 0.0002 is refused, which is a coin flip per table.
`guardrail_drop_tol` is 0.1 points [T]: cell rates carry about that much error (50 calls of prior per
cell), and it is a twentieth of the 2-point limit.

**The caller-ID line is recorded everywhere and enforced only on unproven DIDs.** The DID posterior of
a DID the SPRT cleared is wide: P(dd < -2%) is about 0.48 for an ordinary spread, so enforcing the line
held almost every spread and cost about 2% pickup on public_01. It holds a composition only when the
change raises the share of a DID whose state is unknown, probing, degraded, unhealthy or suspect. Step
I3's own DID gate owns the calibration for the rest; the merge decides whether to tighten this.

**Verifiers.** `allocation` is 6.1 (a) to (c), with two choices. (a) needs at least 100 calls in the
(cohort, dim), because 3 SE across many cohorts and arms pages by multiplicity alone. (b) compares the
upper end of the arm's 90% observation interval with the dry run's 5% point, because two segments of
a small arm are mostly sampling noise. The dry run's interval is refit on the data before the apply.
`schedule` is the 5.8 band with the DID factor of the live mix against the default's. `caller_id`
records the DID states and resolves: the DID detectors see every DID at every step, and a second check
here would only disagree with them. A failed allocation or schedule verification reverts that change's
increases as a P1.

**The held-patch re-check is the gate.** `release_ok` runs the same gate and forecast on today's data
against the config live now and writes a decision row. Failing it holds the patch again, and after 2
holds it is dropped, as 6.1 says.

## Alternatives
- **Fail the guardrail closed on pickup levers until M4's own model exists.** Simple, and it would
  freeze every schedule and caller-ID change on every workflow until the merge.
- **Enforce the caller-ID line on every change.** Measured above: it costs more pickup than the harm it
  prevents with the I2 learner.
- **A separate cohort gate only in the composed step.** That is what the planner did first; a rollback
  that can never pass is planned again at every step and never ships.
- **Lower the decay `h` to fire earlier.** It treats the symptom: the alarm was late because the
  statistic threw away half its calls.

## Consequences
- `CTL_LEARNER=probe` now runs the engagement learner as well, `probe_only` is the M3 learner. The M3
  live tests and the crash matrix therefore exercise the larger learner.
- A failed verification can revert a change's increases even when later changes have moved on: the
  revert caps the arm at its old share and gives the rest to the control, so it never adds exposure.
- `prompt` proposals stay VERIFYING until step I5 registers its verifier in the same map.
- Replay stores pickup rollups summed per (cell, hour) on gate decisions only; the forecast reads
  nothing finer, so the sums give the same number at a fraction of the rows.
- Harmful rollbacks are allowed inside the stated tail: 5.7 accepts P(de < -1%) <= 10%, and a false
  decay alarm can pass that test. `tests/unit/test_decaying_winner.py` holds the rate to the tail.
