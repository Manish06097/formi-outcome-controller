"""Simulated-time helpers: tick arithmetic, approver working time, expiry probability and the
apply-window step bound.

Owner: track T1 (milestone M2); T2 builds `governance/timing.py` (slots, choose_submit) on top.
Implements DESIGN.md 4.2 (`s_watch_max`, decision visibility) and 6.3 (`next_working`,
`p_expire`, submit candidates).

Every function is pure and takes its parameters from the API (tick, approver hours, latency,
TTL, apply window); nothing here is a public-scenario constant. Minutes are simulated minutes
since D0 00:00 (negative in history), so day and minute-of-day use floor division.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterator, Sequence

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
