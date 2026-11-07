# public_02: full controller vs the no-op baseline

Measured on this Mac (11 CPUs, 18 GB) on 2026-11-07, Docker unavailable (the daemon hangs), so the
documented no-Docker path was used: `CTL_NO_DOCKER=1 ./run.sh` runs `scripts/run_local.sh`, the same
restart loop, progress rule and exit codes as the container path, against a local Postgres 17.6.
No manual step between start and grade.

## Commands

```bash
python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base_public_02_incidents
python -m sim serve    --scenario scenarios/public_02_incidents.yaml --run-dir runs/mine_public_02_incidents --port 8761 --force &
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" \
    ./run.sh http://127.0.0.1:8761            # exit 0, scenario finished, no DEGRADED line
python -m sim grade --run runs/mine_public_02_incidents --baseline runs/base_public_02_incidents --out reports/public_02_incidents.md
```

`make grade` (default `SCENARIO=scenarios/public_02_incidents.yaml`) writes `reports/public_02_incidents.md`: the Makefile
names runs and reports by the scenario file name, so the raw grader output is
`reports/public_02_incidents.md`. The README says `reports/public_02.md`; this file is that name and carries
the same grade tables, with the reading below. With Docker the only change is dropping
`CTL_NO_DOCKER` and `PG_DSN`.

## Reading

- Conclusion rate is up 26.9% overall; `ks_cancel` +61.7% (the caller ID flagged at D2 10:00 was
  drained and the supervisor's bad prompt of D3 11:30 was fixed). Hallucinated calls fall from 431
  to 178 overall (`ks_cancel` 348 to 81).
- Incident time to fix: the bad prompt shipped at D3 11:30 was replaced by an approved P1 prompt at
  D3 13:15: `mttr_minutes` 105, `fixed_by` controller_approved. Flagged-caller-ID share fell to 0.0
  from D3 on.
- `nw_renewal` is the weak spot: engagement -2.1% (gap captured -10%) and hallucinated calls 65
  against 54 for the baseline. The supervisor's mid-experiment edit at D5 15:00 interrupted our
  voice experiment (`EXPERIMENT_INTERRUPTED`, rebased, nothing overwritten). The decaying welcome
  variant W3 is rolled back: its share drops from 0.29 at D6 to 0.12 at D7 and 0 at D8.
  `nw_kyc` engagement is -2.2% (primary pickup, +17.7%).
- Governance: 24 approval requests, all approved, 0 expired, 0 lapsed, 0 apply conflicts, 10
  auto-applies, 0 auto-apply rejections; peak requests per 24h (1, 1, 3) are within the caps (4, 2, 3).
- Audit trail (run schema `run_4bdc1ff90f7d_3`): 893 decision rows, 540 audit rows, 36 proposals,
  0 intents left `started`. Pages: `DID_DEGRADED` (the flagged caller ID, drained) and one
  `DID_CHANGE_UNVERIFIED`. Digest: 54 harm-gate blocks, 9 arms removed, 2 supervisor edits.
- The audit queries on this run are in `reports/audit_public_02.md`.


## Grade output (verbatim, `reports/public_02_incidents.md`)

### Grade report — public_02_incidents

### Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.381 | +61.7% | 0.255 | +40.7% | 0.509 | +8.7% | 43% | 81 (348) |
| nw_kyc | 0.431 | +15.4% | 0.276 | +17.7% | 0.595 | -2.2% | 0% | 32 (29) |
| nw_renewal | 0.465 | +7.3% | 0.286 | +9.0% | 0.543 | -2.1% | -10% | 65 (54) |
| ALL | 0.420 | +26.9% | 0.270 | +22.8% | 0.538 | +1.1% | 22% | 178 (431) |

### Changes applied by the controller

34 applied — neutral: 26, beneficial: 8

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.003 didΔ=+0.004 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.013 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.046 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.007 didΔ=+0.044 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.171 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.097 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nw_renewal v13 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.198 → **beneficial**
- ks_cancel v10 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.009 didΔ=+0.002 → **neutral**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.048 → **beneficial**
- ks_cancel v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nw_renewal v15 (controller_approved) dims=['welcome_message'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nw_renewal v16 (controller_approved) dims=['welcome_message'] engageΔ=+0.026 didΔ=+0.000 → **beneficial**
- nw_renewal v17 (controller_approved) dims=['welcome_message'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nw_renewal v18 (controller_approved) dims=['voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_renewal v19 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v20 (controller_approved) dims=['voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.003 → **neutral**
- ks_cancel v14 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**

### Governance

```
{
  "approval_requests": 24,
  "decisions": {
    "approved": 24,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 24,
  "auto_applied": 10,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1,
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
      "2": 0.069,
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

### Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.234,
    "3": 0.281,
    "4": 0.305,
    "5": 0.305,
    "6": 0.294,
    "7": 0.122,
    "8": 0.0,
    "9": 0.0
  }
}
```

Credits used: 25 · validation runs: 5 · LLM fix calls: 1