"""Submit timing: expiry probability, fatigue counting, fixed routine slots, choose_submit.

Owner: track T2 (milestone M3). Implements DESIGN.md 6.3 (submit-window search) and 6.4 (fatigue
caps, voluntary caps and slot reservation). ADRs 0010 and 0011.

The approval's outcome, decide minute and expiry are fixed at submit time, so the expiry
probability of a submit at minute t is exact and computable from API parameters (`p_expire`).
Fatigue is the simulator's rolling count: every request record of the workflow with
`created_minute > t - 1440`, whatever its status (`used_at`). Routine (P2, P3) requests go only at
fixed slot minutes, so yesterday's slot request stops counting at exactly the same minute today.

`choose_submit(now, pol, used, cls, slot1_terminal)` keeps the one signature of DESIGN.md 11.2.
Its thresholds come from the keyword-only `t` (the caller passes `settings.t`); when omitted they
are read once from the environment (`CTL_T_*` overrides), so a forgotten argument cannot silently
drop an override.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Sequence

from controller.settings import Thresholds, thresholds_from_env
from controller.types import (P0_INCIDENT, P1_ROLLBACK, P2_PROMOTION, P3_ROUTINE,
                              EffectivePolicy)
from controller.world.clock import (MINUTES_PER_DAY, ceil_to_tick, decide_minutes,
                                    expected_visible_minute, in_approver_hours, p_expire_raw)

ROUTINE_CLASSES = (P2_PROMOTION, P3_ROUTINE)
URGENT_CLASSES = (P0_INCIDENT, P1_ROLLBACK)


@functools.lru_cache(maxsize=1)
def _env_thresholds() -> Thresholds:
    return thresholds_from_env(os.environ)


def _t(t: Thresholds | None) -> Thresholds:
    return t if t is not None else _env_thresholds()


# ---------------------------------------------------------------- expiry (6.3)
def p_expire(t: int, pol: EffectivePolicy) -> float:
    """Exact P(expire) of a request submitted at minute t with `expires_in_hours` left at the
    policy TTL: the share of latencies L whose `next_working(t + L)` is later than `t + TTL`."""
    return p_expire_raw(t, pol.latency_minutes, pol.approver_hours, pol.ttl_min)


def expected_apply_minute(t: int, pol: EffectivePolicy) -> float:
    """Mean over L of the first stop at which the decision is visible (we apply there).

    The `next_campaign_only` +1440 of DESIGN.md 6.3 depends on the change kind, which this
    signature does not carry; `expected_effect_minute` adds it for allocation changes."""
    return expected_visible_minute(t, pol.latency_minutes, pol.approver_hours, pol.tick)


def expected_effect_minute(t: int, pol: EffectivePolicy, daily_batch_at_min: int,
                           allocation_change: bool) -> float:
    """When the change reaches calls: on `next_campaign_only`, an allocation change applied
    after the day's batch waits for the next batch (adds a day for those latencies)."""
    vis = [ceil_to_tick(d, pol.tick) + pol.tick
           for d in decide_minutes(t, pol.latency_minutes, pol.approver_hours)]
    if allocation_change and pol.mid_flight == "next_campaign_only":
        vis = [v + MINUTES_PER_DAY if v % MINUTES_PER_DAY > daily_batch_at_min else v
               for v in vis]
    return sum(vis) / len(vis)


# ---------------------------------------------------------------- fatigue (6.4)
def used_at(t: int, used: Sequence[int], window: int = MINUTES_PER_DAY) -> int:
    """The simulator's rolling count at minute t: requests with `created > t - window`
    (strict), created at or before t."""
    return sum(1 for c in used if t - window < c <= t)


def fatigue_ok(t: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
               th: Thresholds | None = None) -> bool:
    """May a request of class `cls` be created at minute t (DESIGN.md 6.4)?

    P0 and P1 may use the reserve and may exceed a directive cap, never the policy cap.
    P2 and P3 stay within the routine budget; at C_eff = 1 only if none of ours in the last 48h.
    """
    n = used_at(t, used)
    if cls in URGENT_CLASSES:
        return n + 1 <= pol.c_policy
    if n + 1 > pol.routine_budget:
        return False
    if pol.c_eff == 1 and used_at(t, used, _t(th).cap1_spacing_min) > 0:
        return False
    return True


def safe(t: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
         th: Thresholds | None = None) -> bool:
    return p_expire(t, pol) <= _t(th).submit_expiry_p_max and fatigue_ok(t, pol, used, cls, th)


# ---------------------------------------------------------------- routine slots (6.3)
def routine_slots(d: int, pol: EffectivePolicy, *, t: Thresholds | None = None) -> list[int]:
    """Fixed submit minutes for P2 and P3 on sim day d. Deterministic, so the rolling-24h
    count stays exact. Empty means no expiry-safe minute exists that day (alert NO_SAFE_WINDOW).

    Slot 1, in tiers (ADR 0010):
    1. the preferred slot, if expiry-safe;
    2. else the latest expiry-safe in-hours tick between approver open and the preferred slot
       (the 6.3 text: "pre-open only when no in-hours time is safe"; TTL 12h with latency
       60-480 gives 10:00, p = 1/421);
    3. else the latest tick before approvers open with P(expire) = 0, else the latest
       expiry-safe one, unless the directive forbids pre-open submits (approvers 14-16 with
       TTL 4h and latency 30-240 gives 11:45: 12:00 would expire at latency 240, p = 1/211).
    Slot 2 (routine budget of 2 or more) is slot 1 + 240 [T] when that is expiry-safe."""
    th = _t(t)
    p_max = th.submit_expiry_p_max
    s1 = _slot1(d * MINUTES_PER_DAY, pol, p_max)
    if s1 is None:
        return []
    slots = [s1]
    s2 = s1 + th.slot2_offset_min
    if (pol.routine_budget >= 2 and s2 < (d + 1) * MINUTES_PER_DAY
            and p_expire(s2, pol) <= p_max):
        slots.append(s2)
    return slots


def _slot1(base: int, pol: EffectivePolicy, p_max: float) -> int | None:
    preferred = base + pol.preferred_slot_min
    if p_expire(preferred, pol) <= p_max:
        return preferred
    nb = pol.not_before_min

    def ok(m: int) -> bool:   # a hard not_before also binds the fallback tiers
        return (nb is None or m - base >= nb) and p_expire(m, pol) <= p_max

    open_min = base + pol.approver_open_min
    in_hours = [m for m in range(open_min, preferred, pol.tick) if ok(m)]
    if in_hours:
        return max(in_hours)
    if pol.pre_open_submits == "never":
        return None
    pre_open = [m for m in range(base, open_min, pol.tick) if ok(m)]
    # Before the approvers open the decision lands at the open minute or later whatever the
    # submit minute, so a few minutes earlier cost nothing: take the latest tick whose expiry
    # probability is exactly zero when there is one, rather than a 1/211 risk that would turn
    # into a real expiry on some run (the expiry draw is a hash of the proposal id).
    riskless = [m for m in pre_open if p_expire(m, pol) == 0.0]
    return max(riskless) if riskless else (max(pre_open) if pre_open else None)


def is_slot_minute(m: int, pol: EffectivePolicy, *, t: Thresholds | None = None) -> bool:
    """True iff m is one of the day's fixed routine slot minutes (P2/P3 lands only there)."""
    return m in routine_slots(m // MINUTES_PER_DAY, pol, t=t)


# ---------------------------------------------------------------- choose_submit (6.3)
def choose_submit(now: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
                  slot1_terminal: bool, *, t: Thresholds | None = None) -> int | None:
    """The minute at which to submit a request of class `cls`, or None (alert NO_SAFE_WINDOW).

    P2 and P3: only the exact slot minutes of today and tomorrow (never "any tick after the
    slot"), slot 2 only once slot 1's proposal is terminal, never before a hard `not_before`.
    P0 and P1: any tick in the next 24h that is expiry- and fatigue-safe; inside approver hours
    first (earliest expected apply), else outside hours (earliest expected apply, then the latest
    such minute, which minimises queue time)."""
    th = _t(t)
    if cls in ROUTINE_CLASSES:
        day = now // MINUTES_PER_DAY
        for d in (day, day + 1):
            for i, m in enumerate(routine_slots(d, pol, t=th)):
                if m < now or (i == 1 and not slot1_terminal):
                    continue
                if pol.not_before_min is not None and m % MINUTES_PER_DAY < pol.not_before_min:
                    continue
                if safe(m, pol, used, cls, th):
                    return m
        return None
    start = ceil_to_tick(now, pol.tick)
    cands = [m for m in range(start, start + MINUTES_PER_DAY, pol.tick)
             if safe(m, pol, used, cls, th)]
    inside = [m for m in cands if in_approver_hours(m, pol.approver_hours)]
    if inside:
        return min(inside, key=lambda m: (expected_apply_minute(m, pol), m))
    if cands:
        return min(cands, key=lambda m: (expected_apply_minute(m, pol), -m))
    return None


@dataclass(frozen=True)
class SubmitWindowReport:
    """Why `choose_submit` answered what it did (for the decision row and NO_SAFE_WINDOW)."""

    now: int
    cls: int
    chosen: int | None
    p_expire_at_chosen: float | None
    used_at_chosen: int | None
    slots_today: tuple[int, ...]
    reason: str


def explain_submit(now: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
                   slot1_terminal: bool, *, t: Thresholds | None = None) -> SubmitWindowReport:
    th = _t(t)
    chosen = choose_submit(now, pol, used, cls, slot1_terminal, t=th)
    slots = tuple(routine_slots(now // MINUTES_PER_DAY, pol, t=th))
    if chosen is None:
        if cls in ROUTINE_CLASSES and not slots:
            reason = "no expiry-safe routine slot today (NO_SAFE_WINDOW)"
        elif cls in ROUTINE_CLASSES:
            reason = "routine budget used or slot passed; next slot is beyond tomorrow"
        else:
            reason = "no expiry- and fatigue-safe tick in the next 24h (NO_SAFE_WINDOW)"
        return SubmitWindowReport(now, cls, None, None, None, slots, reason)
    return SubmitWindowReport(now, cls, chosen, p_expire(chosen, pol), used_at(chosen, used),
                              slots, "slot" if cls in ROUTINE_CLASSES else "earliest safe apply")
