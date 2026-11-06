# 0011. Fatigue reserve and fixed slots

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- The approver rejects any request beyond `max_requests_per_24h`, counted as every request record
  of the workflow with `created_minute > now - 1440` (strict), whatever its status. An over-cap
  request is accepted as pending, burns a slot and is certain to be rejected.
- Prompt fixes always need an approval, so an incident needs a free slot exactly when routine work
  may have used them all.
- Kestrel's cancellation supervisor asks for two requests a day, never before 11:00 (memo 04);
  her line's policy allows four.
- The J2 cap-1 case: a `cap - 1` reserve rule leaves zero routine slots, so the D0 foundation
  (cohorts, schedule, DID probes) would never ship.

## Decision
```
C_eff   = min(C_policy, directive cap)
reserve = 1 if C_eff >= 2 else 0
routine = C_eff - reserve                       (budget of P2 and P3)
P0, P1: used(t) + 1 <= C_policy                 (may exceed a directive cap, never the policy cap)
P2, P3: used(t) + 1 <= routine, and at C_eff = 1 only if none of ours in the last 48h [T]
used(t) = our requests with created > t - 1440  (our store, cross-checked with GET /approvals)
```
P2 and P3 are submitted only at the exact slot minutes of ADR 0010, enforced inside
`choose_submit` and again in `ProposalLifecycle.submit_blockers` (`is_slot_minute`).

## Alternatives
- *`reserve = cap - 1`.* Zero routine at cap 1 (F2).
- *No reserve.* An incident on a busy day waits up to 24h for a slot.
- *"Any tick at or after the slot".* A late submit shifts the 24h window, so tomorrow's slot
  request would still see today's late one and be refused or over the cap; fixed minutes keep the
  count exact (yesterday's 11:00 request stops counting at exactly 11:00 today).

## Consequences
- Kestrel cancel: C_eff 2, reserve 1, one routine request a day at 11:00, two slots left for
  incidents under the policy cap of 4.
- Cap 1: routine on alternate days, the single slot free for incidents half the time; an incident
  arriving inside our routine request's 24h window waits for it to roll off (known weakness 13.4).
  The e2e test shows D0 and D2 approvals applied and no two requests within 48h.
- Fatigue is checked at the minute of the request, so a P0/P1 search over the next 24h finds the
  first minute an old request rolls off (unit fixture: cap 1, request at 11:30, next P0 at 11:30
  the next day).
