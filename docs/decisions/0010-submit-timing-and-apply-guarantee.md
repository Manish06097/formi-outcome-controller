# 0010. Submit timing and the apply guarantee

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- The simulator fixes an approval's outcome, decide minute and expiry at the moment we POST it
  (world.py `request_approval`): `decide = next_working(t + L)` with L uniform on the integers of
  `approver_latency_minutes`, and the request expires iff `decide > t + int(60 x TTL)`. Nothing
  we do later can rescue a request that was going to expire.
- An expiry is graded as governance noise, and Product treats it as a rejection. With TTL 12h
  (ks_cancel) a request sent from 19:00 to 21:59 always expires; the naive example sends at 20:00.
- Two J2 attack policies have no safe in-hours submit at all for a naive rule: TTL 12h with
  latency 60-480 (approvers 10-18), and approvers 14-16 with TTL 4h.
- An approved change must be applied within `apply_window_hours` of the hidden decide minute, and
  a decision becomes visible only at the end of the tick that processes it. A lapse (410) is
  graded and is lazy: the approval keeps reading `approved` until we try to apply it.

## Decision
- **Exact expiry probability.** `timing.p_expire(t, pol)` counts, over every integer latency, the
  decide minutes later than `t + int(60 x TTL)`. `expires_in_hours` is left at the policy TTL
  (F11). A submit is allowed only when `p_expire <= 1%` [T] (`submit_expiry_p_max`).
- **Routine slots (P2, P3)** are fixed minutes per day, in three tiers:
  1. the preferred slot (`max(open, not_before)` with a directive, else
     `open + min(60, 25% of the approver window)` floored to the tick), if expiry-safe;
  2. else the latest expiry-safe in-hours tick between approver open and the preferred slot;
  3. else the latest expiry-safe tick before approvers open, unless the directive forbids
     pre-open submits; a hard `not_before` binds tiers 2 and 3 too.
  Slot 2 (`slot1 + 240` [T]) exists when the routine budget is at least 2 and it is safe.
  Worked results (unit fixtures): renewal-like 11:00 and 15:00; TTL 12 with latency 60-480 gives
  10:00 (p = 1/421); approvers 14-16 with TTL 4h gives 12:00 (p = 1/211); approvers 10-19 with TTL
  4h gives 11:00.
- **Urgent (P0, P1)** search every tick of the next 24h; inside approver hours first (earliest
  expected apply), else outside hours (earliest expected apply, ties to the latest minute).
- **`choose_submit(now, pol, used, cls, slot1_terminal)`** is the one signature (11.2); thresholds
  come from a keyword-only `t`, falling back to the environment so a forgotten argument cannot
  drop a `CTL_T_*` override.
- **Apply guarantee.** Pre-apply checks run before any HTTP call, in this order: `base_version`
  equals a fresh `GET .../config` version (else STALE), `now <= apply_by - tick` (else
  LAPSED_LOCAL), and a decision more than 20h [T] after submit is re-gated on fresh data (else
  ABANDONED). The step bound `S_watch_max = tick x floor((W - tick + 1) / tick)` lives in
  `world/clock.py` for the stepper (track T1).

## Alternatives
- *Submit whenever work is ready, inside approver hours.* Fails both J2 cases and every evening
  ks_cancel request.
- *Short `expires_in_hours`.* It only shrinks the window; zero margin (F11).
- *Tier 3 straight after the preferred slot (the literal 6.3 pseudo-code).* For TTL 12h with
  latency 60-480 it picks 09:45 although 10:00 is safe in hours; the 6.3 text says pre-open is a
  last resort ("requests sent at night sit in a queue"), so tier 2 was added.

## Consequences
- Expiry is impossible by construction at the 1% bar; at the bar itself it can still happen (1 in
  421 for the TTL-12h case), so an expiry pages and requeues fresh.
- A P0 found after approver close is submitted the next morning in hours, not at night, although
  a night submit would be decided at the opening. Cost on a 10-19 / 30-240 policy: about 2h of
  MTTR for an evening incident. This follows the 6.3 rule ("in-hours first"); changing it is a
  one-line choice in `choose_submit` if the lead prefers MTTR over the queue concern.
- `routine_slots` can return no slot (NO_SAFE_WINDOW), for example TTL 12h with latency 60-480
  and a hard `not_before` of 11:00.
