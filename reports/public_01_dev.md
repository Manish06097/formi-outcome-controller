# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.364 | +6.6% | 0.257 | +5.2% | 0.498 | +1.2% | 5% | 46 (57) |
| nw_kyc | 0.377 | +2.9% | 0.256 | +5.8% | 0.583 | -2.4% | 0% | 29 (21) |
| nw_renewal | 0.462 | +10.9% | 0.285 | +8.0% | 0.548 | +1.2% | 10% | 49 (54) |
| ALL | 0.399 | +7.4% | 0.267 | +6.3% | 0.532 | +0.5% | 6% | 124 (132) |

## Changes applied by the controller

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

## Governance

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

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

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