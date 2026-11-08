# public_01: full controller vs the no-op baseline

Measured on this Mac (11 CPUs, 18 GB) on 2026-11-08 on the final code (HEAD of `controller`, ADR 0035 included), Docker
unavailable (the daemon hangs), so the documented no-Docker path was used: `CTL_NO_DOCKER=1 ./run.sh` runs
`scripts/run_local.sh`, the same restart loop, progress rule and exit codes as the container path, against a
local Postgres 17.6. No manual step between start and grade. The raw grader output is in the last section
(the README's name for this report is `reports/public_01.md`; `make grade` would name it `reports/public_01_dev.md`).

## Commands

```bash
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base_public_01_dev      # already present, reused (deterministic)
python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/final_p01 --port 8760 --force &
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55479 \
    ./run.sh http://127.0.0.1:8760            # exit 0, scenario finished, no DEGRADED line
python -m sim grade --run runs/final_p01 --baseline runs/base_public_01_dev --out reports/public_01_dev.md
```

## Result in numbers

- Wall-clock of `./run.sh` (history ingest plus the 8 run days to the end of the scenario): **103 s**, exit code 0, no DEGRADED line.
- Conclusion rate (leads concluded / dials, the business outcome): **+8.5%** overall (0.403 vs baseline). Pickup +5.5%, engagement +2.0%. Engagement gap captured 7%.
- Per workflow, conclusion rate: ks_cancel +8.5%, nw_kyc +7.9%, nw_renewal +8.8%. Pickup: +5.1%, +6.9%, +5.4%. Engagement: +2.7%, +0.5%, +2.0%.
- Constraint violations: **0**. Approval requests 26 (22 approved, 4 declined by the approver, 0 expired); 28 changes applied (21 via approval, 7 auto), 0 lapsed approvals, 0 apply conflicts, 0 rejected auto-applies, 0 harmful changes (the grader classes 21 neutral and 7 beneficial). Peak requests per 24h (ks_cancel 3, nw_kyc 2, nw_renewal 3) are at or under every workflow's `max_requests_per_24h` (4, 2, 3).
- Audit trail check (run schema `run_51cc279ffa02_14`): 617 decision rows, 408 audit rows, 33 proposals, 31 config versions, 0 intents left `started`; `python -m controller.audit replay-sample --n 20`: 20 of 20 sampled decisions replayed equal. Alerts: 16 harm-gate blocks, 6 arm removals, 2 futility drops, 1 KPI drift (all digest), 1 `DID_CHANGE_UNVERIFIED` (page, handled, nothing rolled back).

## Reading

- The controller mostly moved pickup (caller-ID probes and spreads, call-time schedule); welcome and voice experiments found a little more than in earlier runs but little in absolute terms within 8 days.
- `nw_kyc` engagement is +0.5% this run (it was -2.4% in the previous report); its primary is pickup and engagement is the guardrail. No workflow is negative on engagement here.
- `nw_renewal` carries a decaying welcome variant (W3): its share on the variant stays 0.25 to 0.30 through D7 in this run (see the decaying-variants block below). This is the known weakness in LIVE_REVIEW section 5.
- Variance: the previous report scored +7.4% on this scenario and an earlier build +11.1%. The controller's outcome depends on the random draws of its experiments and on code changes between runs; one run is one sample, not a point estimate (multi-seed spread: `docs/measurements/did-gate-multiseed.md`).

## Grade output (verbatim, from `sim grade`)

### Grade report - public_01_dev

### Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.371 | +8.5% | 0.257 | +5.1% | 0.505 | +2.7% | 7% | 65 (57) |
| nw_kyc | 0.396 | +7.9% | 0.259 | +6.9% | 0.600 | +0.5% | 0% | 33 (21) |
| nw_renewal | 0.453 | +8.8% | 0.279 | +5.4% | 0.552 | +2.0% | 11% | 53 (54) |
| ALL | 0.403 | +8.5% | 0.265 | +5.5% | 0.540 | +2.0% | 7% | 151 (132) |

### Changes applied by the controller

28 applied - neutral: 21, beneficial: 7

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=-0.000 didΔ=+0.004 → **neutral**
- nw_renewal v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- nw_renewal v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.001 didΔ=+0.044 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nw_renewal v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- ks_cancel v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- nw_renewal v13 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.223 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v14 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_approved) dims=['welcome_message'] engageΔ=-0.007 didΔ=+0.000 → **neutral**
- nw_renewal v15 (controller_approved) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_renewal v16 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- ks_cancel v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**

### Governance

```
{
  "approval_requests": 26,
  "decisions": {
    "approved": 22,
    "rejected": 4,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 4
  },
  "applied_via_approval": 21,
  "auto_applied": 7,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 3,
    "nw_kyc": 2,
    "nw_renewal": 3
  }
}
```

### Incidents

```
[]
```

### Decaying variants - share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.303,
    "4": 0.295,
    "5": 0.304,
    "6": 0.252,
    "7": 0.265
  }
}
```

Credits used: 19 · validation runs: 4 · LLM fix calls: 0