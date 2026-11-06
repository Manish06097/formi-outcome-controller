# 0021. Credit budget

Status: accepted (2026-11-06), milestone M6, track T4

## Context
Credits are finite (200 on the dev scenarios, 400 on load) and running out mid-incident is a
failure (constraint 7). Spending has two kinds: incident spend after a detector fired (verdicts
on a supervisor's new prompt, CUSUM re-checks, fix candidates, evidence runs, one suggest-fixes
call) and planned spend (D0 screens of business-default prompts). Several tenants share one
balance. The worst case of one incident is computable from the API: a FULL verdict (13 credits)
on the new prompt and on every other catalog prompt, plus one evidence run each if the client
gate is tight.

## Decision
Ordered pools, filled once at start from `C0 = credits_remaining` and `K` = the largest prompt
catalog among the workflows, persisted in `credit_envelope`, every spend naming its pool in
`credit_ledger`:

```
W = 13 K (+ K if the gate is tight)          K = 5: W = 65 (70 tight)
    (+ 3 for the one suggest-fixes call)     section 17 override 1: W = 68 with CTL_LLM_SUGGEST on
R = min(0.8 C0, max(0.3 C0, 2 W))            incident reserve, shared; pays incident purposes only
S = min(0.25 C0, C0 - R)                     D0 screens, split across tenants by workflow count
U = C0 - R - S                               unallocated; tops R back up to W after an incident;
                                             unspent screen credit returns here
mode: NORMAL iff R + U >= W (and the API balance >= W), else LOW
LOW:  every verdict STAGE1_ONLY (3 credits; SAFE iff f1 <= 9), W_low = 3 K (+ K) = 15;
      no D0 screens except those the free cross-workflow flag check escalates; no suggest-fixes
402:  page; no non-incident spend afterwards; the DID path needs no credits
```

| C0 | R | S | U | Mode at start (7.2 table, W = 65) | With the 17.1 call (W = 68) |
|---|---|---|---|---|---|
| 50 | 40 | 10 | 0 | LOW (0.8 C0 < W whenever C0 < 82 at K = 5) | LOW (C0 < 85) |
| 100 | 80 | 20 | 0 | NORMAL | NORMAL (R 80, S 20, U 0) |
| 200 | 130 | 50 | 20 | NORMAL | NORMAL (R 136, S 50, U 14) |
| 400 | 130 | 100 | 170 | NORMAL | NORMAL (R 136, S 100, U 164) |

Why W includes the suggest-fixes call: section 17 makes that call part of every hallucination
incident and pays it from R, so a reserve sized without it would be 3 credits short of the worst
incident it promises to cover. The 7.2 table (W = 65) stays reproducible with
`plan_pools(..., llm_suggest=False)`, which is also what `CTL_LLM_SUGGEST=0` means.

`authorize` debits in memory before any call is made (a refused spend is never attempted); an
incident spend draws from R, then U (`authorize_incident`). `record` writes the ledger row and the
pool total inside the caller's intent transaction. The mode is re-evaluated before every incident
spend and also honours the API's balance, so a crash that lost a ledger row cannot leave us
believing we have credit the simulator does not.

## Alternatives
- One global balance with a minimum: screens could eat the incident money on a many-workflow
  tenant mix, and nothing says how much "minimum" is; W makes it computable.
- Per-tenant reserves: an incident is rare and its cost does not depend on the tenant; splitting
  R would leave every tenant short of one worst incident at C0 = 200.
- Skipping D0 screens entirely: saves about 20 credits on public_02 but loses the only detector
  for an unsafe business default (F7), which relative detectors cannot see.

## Consequences
- Expected public_02 spend is about 26 credits (three screens at about 6.8 from S, 3 for the
  FULL verdict on P2 from R, 3 for the suggest-fixes call from R), well above the reserve floor.
- At C0 = 50 the controller starts LOW: verdicts are stage 1 only, so a P4-like prompt passes our
  bar more often (SAFE iff f1 <= 9); the live flag CUSUM remains the backstop.
- A tight gate adds K to W and the threshold for LOW moves accordingly; g is read from the API.
