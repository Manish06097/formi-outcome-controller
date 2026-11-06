"""Clock maths of the approval contract: next_working, p_expire, visibility, s_watch_max.

Owner: track T2 (milestone M3). Implements DESIGN.md 4.2 (apply bound) and 6.3 (expiry maths).

Every function is pure and reads its parameters from arguments (ultimately from the API through
`EffectivePolicy`), never from constants. The simulator's rules mirrored here (never imported):

- An approval requested at minute t with latency L is decided at `next_working(t + L)`, where
  `next_working` moves a minute outside approver hours to the next opening, with no weekends
  (`World._next_working`).
- It expires iff that decide minute is later than `t + int(60 x ttl_hours)` (`request_approval`).
- A decision at minute d is processed in the tick that starts at `ceil_to_tick(d)` and becomes
  visible when an advance ends at or after `ceil_to_tick(d) + tick` (`World._run`).
- An approved change lapses if applied after `d + apply_window` (`apply_approval`).
"""
from __future__ import annotations

import math
from typing import Iterable

MINUTES_PER_DAY = 1440


# ---------------------------------------------------------------- grid and day helpers
def ceil_to_tick(m: int, tick: int) -> int:
    return -(-m // tick) * tick


def floor_to_tick(m: int, tick: int) -> int:
    return (m // tick) * tick


def minute_of_day(m: int) -> int:
    return m % MINUTES_PER_DAY


def day_start(m: int) -> int:
    return (m // MINUTES_PER_DAY) * MINUTES_PER_DAY


# ---------------------------------------------------------------- approver hours
def next_working(t: int, approver_hours: tuple[int, int]) -> int:
    """The simulator's `_next_working`: t itself inside [open, close), else the next opening."""
    lo, hi = approver_hours
    day, mod = divmod(t, MINUTES_PER_DAY)
    if mod < lo * 60:
        return day * MINUTES_PER_DAY + lo * 60
    if mod >= hi * 60:
        return (day + 1) * MINUTES_PER_DAY + lo * 60
    return t


def in_approver_hours(t: int, approver_hours: tuple[int, int]) -> bool:
    lo, hi = approver_hours
    return lo * 60 <= minute_of_day(t) < hi * 60


def next_open(t: int, approver_hours: tuple[int, int]) -> int:
    """The first approver-open minute at or after t (t itself if t is exactly the opening)."""
    lo, _ = approver_hours
    start = day_start(t) + lo * 60
    return start if start >= t else start + MINUTES_PER_DAY


# ---------------------------------------------------------------- expiry
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


# ---------------------------------------------------------------- visibility and apply bound
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


def s_watch_max(tick: int, apply_window_min: int) -> int:
    """Largest step that still sees and applies every decision in time (DESIGN.md 4.2, F12):
    `tick x floor((W - tick + 1) / tick)`. W = 120, tick = 15 gives 105."""
    return tick * ((apply_window_min - tick + 1) // tick)


def latest_safe_apply(apply_by: int, tick: int) -> int:
    """We apply only while `now <= apply_by - tick` (one tick of margin, DESIGN.md 6.1)."""
    return apply_by - tick


def tick_grid(start: int, end: int, tick: int) -> Iterable[int]:
    """Tick-aligned minutes in [start, end)."""
    return range(ceil_to_tick(start, tick), end, tick)
