# public_01: full controller vs the no-op baseline

Measured on this Mac (11 CPUs, 18 GB) on 2026-11-07, Docker unavailable (the daemon hangs), so the
documented no-Docker path was used: `CTL_NO_DOCKER=1 ./run.sh` runs `scripts/run_local.sh`, the same
restart loop, progress rule and exit codes as the container path, against a local Postgres 17.6.
No manual step between start and grade.

## Commands

```bash
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base_public_01_dev
python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/mine_public_01_dev --port 8760 --force &
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" \
    ./run.sh http://127.0.0.1:8760            # exit 0, scenario finished, no DEGRADED line
python -m sim grade --run runs/mine_public_01_dev --baseline runs/base_public_01_dev --out reports/public_01_dev.md
```

`make grade` (default `SCENARIO=scenarios/public_01_dev.yaml`) writes `reports/public_01_dev.md`: the Makefile
names runs and reports by the scenario file name, so the raw grader output is
`reports/public_01_dev.md`. The README says `reports/public_01.md`; this file is that name and carries
the same grade tables, with the reading below. With Docker the only change is dropping
`CTL_NO_DOCKER` and `PG_DSN`.

## Reading

- Conclusion rate (leads concluded / dials, the business outcome) is up 7.4% overall, pickup 6.3%,
  engagement 0.5%. Engagement gap captured is 6%: the controller mostly moved pickup (caller-ID
  probes, call-time schedule), and welcome and voice experiments found little in this scenario
  within the 8 days.
- `nw_kyc` engagement is -2.4% against the baseline. Its primary is pickup and engagement is the
  guardrail; the likely cause is the mix effect of reaching more, harder-to-engage people (not
  verified here). It is a negative number and is reported as one.
- Governance: 25 approval requests, 21 approved, 4 declined by the approver, 0 expired, 0 lapsed,
  0 apply conflicts, 6 auto-applies, 0 auto-apply rejections. Peak requests per 24h (1, 2, 2) are
  within every workflow's `max_requests_per_24h`.
- Audit trail (run schema `run_51cc279ffa02_13`): 527 decision rows, 403 audit rows, 32 proposals,
  30 config versions, 0 intents left `started`. Alerts: 27 harm-gate blocks and 2 futility drops
  (digest), 1 `DID_CHANGE_UNVERIFIED` page (a caller-ID change that did not verify and was handled).
- Variance: the previous merged build (before decision seeds were made run-independent, see
  `docs/design/build-reports/I6.md`) scored +11.1% on the same scenario. The controller's outcome
  depends on the random draws of its experiments; one run is one sample, not a point estimate.


## Grade output (verbatim, `reports/public_01_dev.md`)

### Grade report — public_01_dev

### Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.364 | +6.6% | 0.257 | +5.2% | 0.498 | +1.2% | 5% | 46 (57) |
| nw_kyc | 0.377 | +2.9% | 0.256 | +5.8% | 0.583 | -2.4% | 0% | 29 (21) |
| nw_renewal | 0.462 | +10.9% | 0.285 | +8.0% | 0.548 | +1.2% | 10% | 49 (54) |
| ALL | 0.399 | +7.4% | 0.267 | +6.3% | 0.532 | +0.5% | 6% | 124 (132) |

### Changes applied by the controller

27 applied — neutral: 21, beneficial: 6

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=-0.000 didΔ=+0.004 → **neutral**
- nw_renewal v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- nw_renewal v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.001 didΔ=+0.044 → **beneficial**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.047 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- ks_cancel v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- nw_renewal v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_renewal v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v13 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_kyc v6 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.208 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v14 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- nw_renewal v15 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_kyc v7 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**

### Governance

```
{
  "approval_requests": 25,
  "decisions": {
    "approved": 21,
    "rejected": 4,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 4
  },
  "applied_via_approval": 21,
  "auto_applied": 6,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1,
    "nw_kyc": 2,
    "nw_renewal": 2
  }
}
```

### Incidents

```
[]
```

### Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.274,
    "4": 0.311,
    "5": 0.358,
    "6": 0.339,
    "7": 0.321
  }
}
```

Credits used: 19 · validation runs: 4 · LLM fix calls: 0