"""Simulated-time helpers: tick arithmetic, approver working time, expiry probability and the
apply-window step bound.

Owners: track T1 (milestone M2) and track T2 (milestone M3, the governance helpers at the end).
Implements DESIGN.md 4.2 (`s_watch_max`, decision visibility) and 6.3 (`next_working`,
`p_expire`, submit candidates).

Every function is pure and takes its parameters from the API (tick, approver hours, latency,
TTL, apply window); nothing here is a public-scenario constant. Minutes are simulated minutes
since D0 00:00 (negative in history), so day and minute-of-day use floor division.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Iterable, Iterator, Sequence

MINUTES_PER_DAY = 1440


# =============================================================================== tick arithmetic

def floor_to(minute: int, step: int) -> int:
    """Largest multiple of `step` that is <= minute (works for negative minutes)."""
    return (minute // step) * step


def ceil_to(minute: int, step: int) -> int:
    """Smallest multiple of `step` that is >= minute."""
    return -((-minute) // step) * step


def day_of(minute: int) -> int:
    return minute // MINUTES_PER_DAY


def minute_of_day(minute: int) -> int:
    return minute % MINUTES_PER_DAY


def at_day(day: int, minute_in_day: int) -> int:
    return day * MINUTES_PER_DAY + minute_in_day


def next_daily(now: int, minute_in_day: int) -> int:
    """The first minute strictly after `now` that has the given minute of day."""
    t = at_day(day_of(now), minute_in_day)
    return t if t > now else t + MINUTES_PER_DAY


# =============================================================================== approver time

def in_hours(minute: int, hours: Sequence[int]) -> bool:
    """True if the minute of day lies in [open, close) hours."""
    lo, hi = hours
    return lo * 60 <= minute_of_day(minute) < hi * 60


def next_working(t: int, hours: Sequence[int]) -> int:
    """The approver's decide minute for a raw time t: t itself inside [open, close), else the next
    opening. Mirrors the simulator's contract (UNDERSTANDING.md 4); weekends do not exist."""
    lo, hi = hours
    day, mod = divmod(t, MINUTES_PER_DAY)
    if mod < lo * 60:
        return day * MINUTES_PER_DAY + lo * 60
    if mod >= hi * 60:
        return (day + 1) * MINUTES_PER_DAY + lo * 60
    return t


def decision_visible_at(decide_minute: int, tick: int) -> int:
    """A decision at minute d is processed in the first tick starting at or after d and becomes
    visible when an advance ends at or after the end of that tick (DESIGN.md 4.2)."""
    return ceil_to(decide_minute, tick) + tick


def p_expire_exact(t: int, latency: Sequence[int], ttl_hours: float,
                   hours: Sequence[int]) -> Fraction:
    """Exact probability that a request submitted at minute t expires (DESIGN.md 6.3).

    decide = next_working(t + L) with L uniform on the integers [lo, hi]; it expires iff
    decide > t + 60 x TTL. `expires_in_hours` is left at the policy TTL (F11)."""
    lo, hi = int(latency[0]), int(latency[1])
    deadline = t + int(ttl_hours * 60)
    bad = sum(1 for L in range(lo, hi + 1) if next_working(t + L, hours) > deadline)
    return Fraction(bad, hi - lo + 1)


def p_expire(t: int, latency: Sequence[int], ttl_hours: float, hours: Sequence[int]) -> float:
    return float(p_expire_exact(t, latency, ttl_hours, hours))


def submit_candidates(start: int, end: int, tick: int, latency: Sequence[int], ttl_hours: float,
                      hours: Sequence[int], p_max: float) -> list[int]:
    """Tick-aligned minutes in [start, end) whose expiry probability is at most p_max."""
    out = []
    t = ceil_to(start, tick)
    while t < end:
        if p_expire(t, latency, ttl_hours, hours) <= p_max:
            out.append(t)
        t += tick
    return out


# =============================================================================== step bound

def s_watch_max(tick: int, apply_window_min: int) -> int:
    """Largest stop spacing that still lets every decision be applied inside its apply window.

    Worst case: a decision at d = s0 - tick + 1 is processed at the tick starting s0, visible at
    the next stop s0 + S, and lapses after d + W. So S <= W - tick + 1, rounded down to the tick:
    S_watch_max = tick * floor((W - tick + 1) / tick)   (W = 120, tick = 15 -> 105; DESIGN.md 4.2).
    """
    return tick * ((apply_window_min - tick + 1) // tick)


def approver_minutes(start: int, end: int, hours: Sequence[int]) -> Iterator[tuple[int, int]]:
    """Yield the [a, b) sub-intervals of [start, end) that lie inside approver hours."""
    lo, hi = hours
    if end <= start:
        return
    d = day_of(start)
    while at_day(d, 0) < end:
        a = max(start, at_day(d, lo * 60))
        b = min(end, at_day(d, hi * 60))
        if a < b:
            yield a, b
        d += 1


def may_decide_between(start: int, end: int, hours: Sequence[int]) -> bool:
    """True if any minute in [start, end) is approver working time, so a pending approval could
    be decided in that interval."""
    return any(True for _ in approver_minutes(start, end, hours))


# =============================================================================== governance helpers (T2)
# Contract-equivalent helpers written by track T2 for governance/timing.py and release.py.

def ceil_to_tick(m: int, tick: int) -> int:
    return -(-m // tick) * tick


def floor_to_tick(m: int, tick: int) -> int:
    return (m // tick) * tick


def day_start(m: int) -> int:
    return (m // MINUTES_PER_DAY) * MINUTES_PER_DAY


def in_approver_hours(t: int, approver_hours: tuple[int, int]) -> bool:
    lo, hi = approver_hours
    return lo * 60 <= minute_of_day(t) < hi * 60


def next_open(t: int, approver_hours: tuple[int, int]) -> int:
    """The first approver-open minute at or after t (t itself if t is exactly the opening)."""
    lo, _ = approver_hours
    start = day_start(t) + lo * 60
    return start if start >= t else start + MINUTES_PER_DAY


def ttl_minutes(ttl_hours: float) -> int:
    """The simulator's `int(ttl * 60)` (expires_in_hours is left at the policy TTL, F11)."""
    return int(ttl_hours * 60)


def latencies(latency_minutes: tuple[int, int]) -> range:
    """Every latency the approver can draw: uniform on the integers [lo, hi]."""
    lo, hi = latency_minutes
    return range(lo, hi + 1)


def decide_minutes(t: int, latency_minutes: tuple[int, int],
                   approver_hours: tuple[int, int]) -> list[int]:
    """The decide minute for every possible latency, in latency order."""
    return [next_working(t + L, approver_hours) for L in latencies(latency_minutes)]


def p_expire_raw(t: int, latency_minutes: tuple[int, int], approver_hours: tuple[int, int],
                 ttl_min: int) -> float:
    """Exact P(expire) of a request submitted at minute t (DESIGN.md 6.3, F3)."""
    ds = decide_minutes(t, latency_minutes, approver_hours)
    bad = sum(1 for d in ds if d > t + ttl_min)
    return bad / len(ds)


def worst_decide_minute(t: int, latency_minutes: tuple[int, int],
                        approver_hours: tuple[int, int]) -> int:
    """Latest possible decide minute (next_working is monotone, so it is at L = hi)."""
    return next_working(t + latency_minutes[1], approver_hours)


def visible_minute(decide: int, tick: int) -> int:
    """First stop at which a decision at minute `decide` can be seen."""
    return ceil_to_tick(decide, tick) + tick


def expected_visible_minute(t: int, latency_minutes: tuple[int, int],
                            approver_hours: tuple[int, int], tick: int) -> float:
    ds = decide_minutes(t, latency_minutes, approver_hours)
    return sum(visible_minute(d, tick) for d in ds) / len(ds)


def apply_window_minutes(apply_window_hours: float) -> int:
    """Latest whole minute offset after the decision at which an apply still succeeds: the
    simulator lapses when `now > decided + 60 x hours`, so floor of that (exact for whole and
    half hours, conservative otherwise)."""
    return int(math.floor(apply_window_hours * 60))


def latest_safe_apply(apply_by: int, tick: int) -> int:
    """We apply only while `now <= apply_by - tick` (one tick of margin, DESIGN.md 6.1)."""
    return apply_by - tick


def tick_grid(start: int, end: int, tick: int) -> Iterable[int]:
    """Tick-aligned minutes in [start, end)."""
    return range(ceil_to_tick(start, tick), end, tick)


# =============================================================================== end-game freeze

def freeze_start(end_minute: int | None, min_days: float, frac: float) -> int | None:
    """First minute of the end-game freeze, `end - max(min_days, frac x scenario)` (DESIGN.md
    4.2), rounded up so `now >= freeze_start` matches the exact real-valued rule for whole-minute
    `now`. None when the end is unknown (no freeze). The one formula the DID prober and the
    cohort-scope projection share."""
    if end_minute is None:
        return None
    span = max(end_minute, 0)
    return math.ceil(end_minute - max(min_days * MINUTES_PER_DAY, frac * span))
