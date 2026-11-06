# 0007. Guardrail definition

Status: accepted (2026-11-06), implemented in M5 (`controller/learning/guardrail.py`); tested by
`tests/unit/test_guardrail.py`

## Context
- Each workflow declares `objective.guardrail.<kpi>_max_drop: 0.02` on the KPI that is not
  primary. The contract does not say whether 0.02 is absolute (2 points) or relative (2%), or
  over what window. The grader does not check guardrails; the rubric does.
- Pickup and engagement must never be blended (constraint 3), so the guardrail is a separate
  hard limit, not a term in an objective.
- The guardrail is the raw rate. Schedule and caller-ID changes change WHO connects, so on a
  KYC-like workflow (pickup primary, engagement guardrail) the raw engagement rate can fall
  through the mix while nobody's engagement changes.

## Decision
- **Definition (assumption A-3)**: an absolute drop of x points in the guardrail rate, over a
  rolling 2-day window, against the trailing 3-day baseline (or a concurrent comparator where
  one exists).
- **Forecast before every proposal** (`MixGuardrailMonitor.predict`): `E(cfg) = sum_c
  units_c(cfg) rate_c(cfg) / sum_c units_c(cfg)`, with units = connected calls (engagement
  guardrail) or dials (pickup guardrail) per cell.
  - Welcome and voice move only engagement: `rate_c(new) = rate_c(cur) M_new(c) / M_cur(c)`
    with M the effect multipliers of the engagement draws, so the forecast is a distribution.
  - Schedule and caller-ID changes need the caller's connect and pickup models (5.8, 5.9, track
    T4). Without them the forecast **fails closed** (p_breach = 1).
  - A proposal must predict no drop (mean <= 0) and `P(drop > x) <= 2.5%` (part of the harm
    gate, ADR 0002).
- **Decomposition** into mix and within terms, `sum_k (c'_k - c_k) e_k + sum_k c'_k (e'_k -
  e_k)`, with k = the **cell** (deviation from 5.10, which says cohort): a cohort decomposition
  is a coarsening that hides a mix shift inside a cohort, and on a workflow without grid
  cohorts it would hide every mix shift. The test that found this had a pickup gain bought with
  young, low-engagement leads, invisible in a by-cohort split.
- **Breach check** (`check`): breach when the lower 97.5% bound of the raw drop exceeds x AND
  the drop is attributable to a controller change applied inside the window:
  - welcome / voice changes: by the concurrent arm-vs-control comparison (the arms'
    share-weighted deficit explains >= half the drop);
  - schedule / caller-ID changes: when the mix term explains >= half the drop and the mix shift
    started within 2 segments of the change.

  An attributable breach is a `Finding(detector="guardrail", kind="breach")` naming the version
  to roll back (a P1, risk-reducing). An unattributable drop is `kind="kpi_drift"`: a digest
  item that triggers the DID and prompt checks and changes nothing by itself.

## Alternatives
- **Relative reading (2% of the rate)**: 2% of a 0.26 pickup is 0.5 points. At dev volume the
  2-day SE of a pickup rate is about 0.55 points, so a relative guardrail could not be measured
  and would be unenforced. A limit we cannot measure is not a limit.
- **Guardrail as a penalty in the objective**: blends the two KPIs (constraint 3).
- **Monitoring only, no forecast**: an engagement guardrail on KYC (about 930 connected in 2
  days) has a 2-day SE of 1.7 points, so a 2-point drop is only 1.2 SE. Monitoring alone
  catches it late. The forecast carries most of the protection, which is also why
  engagement-affecting experiments do not run on pickup-primary workflows.

## Consequences
- Every allocation proposal's guardrail forecast is stored with its harm report.
- Track T4's schedule and caller-ID planners must pass `connects_fn` / `pickup_fn`, or their
  changes are blocked on pickup-primary workflows by the fail-closed rule. This is deliberate
  and listed in the T3 report.
- Stated in SUBMISSION as assumption A-3, with the reason the relative reading was rejected.
