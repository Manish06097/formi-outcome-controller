# 0020. Impact score definition

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- `POST /approvals` needs an `impact_score` in [0, 1]; the simulator accepts any number there.
- Product: "the impact score must mean something". Pickup and engagement are never blended, and
  workflows differ in their primary KPI (KYC is pickup).
- A probe or a first exploration share buys information, not lift.

## Decision
- `impact_score = clip(Q20 of the expected relative change, over the next 7 days, of the KPI the
  lever moves, 0, 1)`, stored with its label `impact_kpi`:

| Lever | impact_kpi | Value |
|---|---|---|
| welcome, voice | engagement_rate | Q20 of `de` from the stratified posterior replay |
| caller ID | pickup_rate | Q20 of `dd` from the per-DID relative pickup draws |
| schedule | pickup_rate | Q20 from the analytic hour-curve expectation (track T4 input) |
| prompt fix | engagement_rate | measured deficit `(flag_now - flag_base) x (e_unflagged - e_flagged) / e_now` |
| cohorts | primary KPI | 0: a structural change that buys targeting |
| bundle | the highest-class item's | one score per lever in `dry_run.impact.per_lever`, never summed |

- Q20 means "at least this much with 80% confidence"; 0.042 labelled `pickup_rate` reads "at
  least about +4.2% pickup on this workflow".
- The dry run is a stratified posterior replay (F14): per cell, welcome and voice multiply (A-1, no
  interaction), cells weighted by the next-24h connected mix; it reports mean, 90% interval,
  `P(de < -1%)` and Q20, plus `leads_hearing_a_different_variant_24h`.
- Missing inputs give 0 with a stated source ("no posterior", "not estimated"), never an invented
  number. The supervisor queue sorts by class first, then impact.

## Alternatives
- *Expected value instead of Q20.* Rewards uncertain arms; Q20 is honest about evidence.
- *One blended score.* Forbidden (pickup and engagement never blended) and meaningless across
  KPIs.
- *SNIPS off-policy estimate.* Cut for complexity and variance (F14).

## Consequences
- Probes and first arms score about 0: correct, they buy information.
- A DID spread on an engagement-primary workflow scores its real pickup gain instead of 0, and a
  prompt fix on a pickup-primary workflow scores the engagement it restores.
- Two KPIs are compared only for ordering inside a class, never added or averaged.
