# public_02: full controller vs the no-op baseline

Measured on this Mac (11 CPUs, 18 GB) on 2026-11-08 on the final code (HEAD of `controller`, ADR 0035 included), Docker
unavailable (the daemon hangs), so the documented no-Docker path was used: `CTL_NO_DOCKER=1 ./run.sh` runs
`scripts/run_local.sh`, the same restart loop, progress rule and exit codes as the container path, against a
local Postgres 17.6. No manual step between start and grade. The raw grader output is in the last section
(the README's name for this report is `reports/public_02.md`; `make grade` would name it `reports/public_02_incidents.md`).

## Commands

```bash
python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base_public_02_incidents      # already present, reused (deterministic)
python -m sim serve    --scenario scenarios/public_02_incidents.yaml --run-dir runs/final_p02 --port 8761 --force &
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55479 \
    ./run.sh http://127.0.0.1:8761            # exit 0, scenario finished, no DEGRADED line
python -m sim grade --run runs/final_p02 --baseline runs/base_public_02_incidents --out reports/public_02_incidents.md
```

## Result in numbers

- Wall-clock of `./run.sh`: **145 s**, exit code 0, no DEGRADED line.
- Conclusion rate: **+27.1%** overall (0.420 vs baseline). Pickup +22.1%, engagement +1.7%. Engagement gap captured 23%.
- Per workflow, conclusion rate: ks_cancel +58.8%, nw_kyc +13.1%, nw_renewal +11.2%. Pickup: +39.2%, +14.5%, +10.2%. Engagement: +8.0%, -1.9%, +0.0%.
- Incidents: the flagged caller ID of ks_cancel (D2 10:00) is gone from D3 on (flagged-DID share 0.071 on D2, 0.0 afterwards). The supervisor's bad prompt of D3 11:30 was fixed by an approved P1 prompt change at D3 13:15: `mttr_minutes` 105, `fixed_by` controller_approved. Hallucinated calls fall from 431 to 179 overall (ks_cancel 348 to 93).
- Constraint violations: **0**. Approval requests 24 (23 approved, 1 declined, 0 expired); 32 changes applied (21 via approval, 11 auto), 0 lapsed, 0 apply conflicts, 0 rejected auto-applies, 0 harmful changes (26 neutral, 6 beneficial). Peak requests per 24h (ks_cancel 2, nw_kyc 1, nw_renewal 3) are within the caps (4, 2, 3).
- Audit trail check (run schema `run_4bdc1ff90f7d_4`): 686 decision rows, 530 audit rows, 38 proposals, 37 config versions, 0 intents left `started`; `replay-sample --n 20`: 20 of 20 replayed equal. `why-changed --workflow ks_cancel --at "D3 14:00"` names the P1 prompt fix (proposal `4bdc1f-ks_cancel-D3T1200-0-0007`, approval apr_00011 decided D3 12:51, applied D3 13:15). Alerts: pages `DID_DEGRADED` (1, the flagged caller ID, drained), and **one `COHORT_MISMATCH` page** (see below); digest: 13 harm-gate blocks, 4 arm removals, 2 futility drops, 2 KPI drift, 2 supervisor edits, 1 experiment interrupted.
- The audit queries on the previous public_02 run are in `reports/audit_public_02.md` (not regenerated).

## Reading

- `nw_renewal` engagement is +0.0% (gap captured -2%), up from -2.1% / -10% in the previous report; hallucinated calls 54 against 54 for the baseline. The supervisor's mid-experiment edit at D5 15:00 interrupted our voice experiment (`EXPERIMENT_INTERRUPTED`, rebased, nothing overwritten). The decaying welcome variant W3 is at 0.04 to 0.12 share from D2 on in this run (it was 0.29 at D6 in the previous run). `nw_kyc` engagement is -1.9% (primary pickup, +14.5%).
- **New alert, investigated (sampling noise, see docs/measurements/crash-extra-rows-and-cohort-page.md):** `COHORT_MISMATCH` (page, event) fired once, for proposal `4bdc1f-ks_cancel-D7T1100-3-0012`: "(a) age1830_hi~5396af/voice/V2: realised 0.123 vs configured 0.062 (SE 0.0194)", about 3.1 standard errors. The previous public_02 run had no such alert. Investigated: 334 shares were tested in that run (about 1 expected false page), allocation is calibrated, the next window was fine.
- Pickup on `nw_kyc` is +14.5% against +17.7% in the previous report and ks_cancel +39.2% against +40.7%: the stricter caller-ID line of ADR 0035 may account for part of this, but one run cannot separate it from draw noise (see `docs/measurements/did-gate-multiseed.md`).

## Grade output (verbatim, from `sim grade`)

### Grade report - public_02_incidents

### Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.374 | +58.8% | 0.252 | +39.2% | 0.505 | +8.0% | 40% | 93 (348) |
| nw_kyc | 0.423 | +13.1% | 0.268 | +14.5% | 0.597 | -1.9% | 0% | 32 (29) |
| nw_renewal | 0.481 | +11.2% | 0.289 | +10.2% | 0.555 | +0.0% | -2% | 54 (54) |
| ALL | 0.420 | +27.1% | 0.268 | +22.1% | 0.541 | +1.7% | 23% | 179 (431) |

### Changes applied by the controller

32 applied - neutral: 26, beneficial: 6

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.003 didΔ=+0.004 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.013 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.046 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.006 didΔ=+0.044 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.171 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.104 → **beneficial**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.008 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.001 → **neutral**
- nw_renewal v12 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.002 → **neutral**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.155 → **beneficial**
- ks_cancel v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_renewal v13 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- nw_renewal v15 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v16 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_renewal v17 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v18 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v19 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**

### Governance

```
{
  "approval_requests": 24,
  "decisions": {
    "approved": 23,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 21,
  "auto_applied": 11,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 2,
    "nw_kyc": 1,
    "nw_renewal": 3
  }
}
```

### Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.071,
      "3": 0.0,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 105,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

### Decaying variants - share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.116,
    "3": 0.039,
    "4": 0.046,
    "5": 0.055,
    "6": 0.076,
    "7": 0.055,
    "8": 0.06,
    "9": 0.062
  }
}
```

Credits used: 25 · validation runs: 5 · LLM fix calls: 1