# 0009. Prompt safety verdict and gate evidence

Status: accepted (2026-11-06), milestone M6, track T4

## Context
A validation run of n conversations returns a binomial number of failures at an unknown
per-prompt rate q; the approver accepts a prompt change only with ONE run for that workflow and
prompt whose `pass_rate >= g` (`min_validation_pass_rate`, from the API). The grader treats a
prompt with hallucination above 0.01 as unsafe (about q > 0.014). At g = 0.97 a P4-like prompt
(q about 0.0275) passes the gate 69% of the time, so the gate is not a safety test; at g = 0.99 a
safe P1-like prompt (q about 0.0104) passes only 59 to 72% of runs, so the gate can also reject
safe fixes. Re-running until a run passes would get any prompt approved: cherry-picking.

## Decision
Two questions, two procedures.

1. **Our verdict** ("is it safe?"), a fixed bar independent of g: stage 1 n = 300, UNSAFE at
   f1 >= 10; stage 2 n = 1,000 more, UNSAFE at pooled F >= ceil(0.02 N) = 26 of 1,300.
   SEQUENTIAL (D0 screens) stops at stage 1 when f1 <= 3; FULL (supervisor prompt changes, fix
   candidates) always runs stage 2 unless stage 1 is UNSAFE; STAGE1_ONLY (LOW credits) is SAFE iff
   f1 <= 9. If stage 2 is refused by the budget, the verdict degrades to the stage-1 rule and says
   so. Verdicts are cached per (workflow, prompt) and never re-run for the same decision; a CUSUM
   re-check pools every earlier run (no run is ever discarded).
   Simulated operating characteristics (seeded binomial fake, 4,000 replicates):

   | q | sequential UNSAFE / credits | full UNSAFE / credits | design table |
   |---|---|---|---|
   | 0.0104 (P1-like) | 0.2% / 6.8 | 0.2% / 13.0 | 0.3%; 6.8 / 13.0 |
   | 0.014 (boundary) | 5.1% / 8.9 | 5.6% / 12.9 | 3%, 5% |
   | 0.0185 | 36% / 10.7 | 38% / 12.4 | 35%, 40% |
   | 0.0275 (P4-like) | 94% / 9.5 | 97% / 9.9 | 93%, 96% |
   | 0.0545 (P2-like) | 100% / 3.3 | 100% / 3.3 | 100% |

2. **Gate evidence** ("can it be approved?"): `P_pass(n) = P(Binom(n, q_ref) <= floor((1 - g) n))`
   with q_ref = 1%. The gate is tight iff `P_pass(300) < 0.95`, decided from g before any run.
   Not tight (g <= 0.98): the verdict's stage-1 run is designated as the evidence before it runs
   (a cached verdict's stage-1 run is reused, free). Tight: one separate run at the n in
   {50, 100, 200, 300, 500, 1000} that maximises P_pass (n = 100 at g = 0.99), designated before
   it runs. Exactly one designated run per (decision, prompt); if it fails the gate the prompt is
   SAFE but not approvable for this decision and we move to the next candidate. None left:
   `CLIENT_GATE_UNREACHABLE` and the incident stays open.
3. **Fix selection**, fixed before any result is seen: last known good first (live >= 1
   dialling day, flag rate not above the history baseline at Poisson p > 0.05, not UNSAFE;
   SEQUENTIAL verdict if none is cached), then every catalog prompt minus the current and UNSAFE
   ones: avoid-term matches last, live prompts by live engagement, never-live prompts in catalog
   order, ties on catalog order. Validate in that order and stop at the first SAFE and
   approvable prompt; that is exactly what "validate all, then take the top-ranked SAFE one"
   picks, at lower cost.
4. **One suggest-fixes call per incident** (DESIGN.md 17 override 1, replacing 7.1's "not called
   by default"): at incident open, 3 credits from the reserve, its suggestions added to the
   candidate set. In this API suggestions must be catalog prompts (422 otherwise), so they are
   already candidates; anything else is dropped and reported. The rank never changes (the LLM
   only proposes, constraint 4; unit-tested with and without suggestions). In LOW mode, when
   disabled (`CTL_LLM_SUGGEST=0`) or when R and U cannot pay, the call is skipped and the skip is
   recorded as a decision.
5. **Post-apply verification, read sequentially** (a deviation from a strict one-shot reading of
   7.1): from 300 connected calls under the fix, pass when the flag-rate UCB95 is below 2 p0 and
   the engagement LCB95 (24h after vs 72h before, matched by hour) is at least -5%; fail as soon
   as either bound shows the fix is bad (flag LCB95 above 2 p0, or engagement UCB95 below -5%);
   otherwise keep collecting until the 24h window ends and then decide on the point estimates.
   Why: at 300 calls a prompt AT the baseline rate p0 = 0.0147 expects 4.4 flags, whose UCB95 of
   about 10/300 = 0.034 is above 2 p0 = 0.029, so the strict reading would reopen good fixes about
   half the time and churn through worse candidates.

## Alternatives
- One rule "UNSAFE when the run fails the client gate": calls a safe prompt UNSAFE 38% of the
  time at g = 0.99 (n = 300) and leaves the incident without a fix, while letting P4 through 69%
  of the time at g = 0.97.
- Moving the pooled bar to 1.4% (F >= 19): closes most of the 1.4% to 2% band but fails a
  P1-like safe prompt about 8% of the time instead of 0.3%, which removes the LKG fix in about 1
  incident in 12. We keep 2% and state the band (13.4).
- LLM-ranked candidates: non-deterministic and in the simulator a random 3 of the catalog.

## Consequences
- MTTR on the public_02 shape is 75 to 165 minutes (mean about 122): the FULL verdict fails at
  stage 1 in the stop that sees the edit, the LKG is cached from D0, and the gate is not tight.
- A prompt between about q 1.4% and 2% passes FULL often; the live flag CUSUM is the backstop.
- Verification can take up to a day in ambiguous cases; the fix is live and judged SAFE meanwhile.
- The verdict runner calls the simulator only through the injected client; write-ahead intents
  around each validation call belong to the reliability layer (T1) wrapping that client.
