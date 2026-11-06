"""Sequential tests with serialisable state: Wald SPRT, Bernoulli CUSUM, Page CUSUM.

Owners: track T4 (milestones M4 and M6) and track T3 (milestone M5). Implements the sequential
statistics of DESIGN.md 5.7 (one-sided decay Page CUSUM, `PageCusum`), 5.9 (dial-count SPRT for
probed caller IDs), 7.1 (live hallucination-flag CUSUM) and 7.5 (two-sided KPI drift Page CUSUM,
`DriftPageCusum`), plus the closed-form operating characteristics those sections quote (Wald's E[N],
Siegmund's ARL), so tests and ADRs can check simulation against the stated numbers.

Every statistic is a frozen dataclass: `update(...)` returns a new value, `to_json()` and
`from_json()` round-trip it through the store (`did_state.sprt_llr`, `flag_monitor`,
`drift_monitor`). Nothing here knows about workflows, events or the simulator.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Mapping

SprtDecision = Literal["continue", "accept_h1", "accept_h0"]

# =============================================================================== Wald SPRT


@dataclass(frozen=True)
class BernoulliRatioSprt:
    """Wald's SPRT of `H0: r = r0` against `H1: r = r1` for a Bernoulli outcome whose success
    probability is `r x p_s`, with `p_s` a comparator probability given per observation.

    DESIGN.md 5.9 uses it with `r0 = 0.70` (unhealthy), `r1 = 1.0` (healthy), `alpha = P(accept H1
    | H0) = 1%`, `beta = P(accept H0 | H1) = 5%`:

        success:  LLR += ln(r1 / r0)
        failure:  LLR += ln((1 - r1 p_s) / (1 - r0 p_s))
        accept H1 when LLR >= ln((1 - beta) / alpha); accept H0 when LLR <= ln(beta / (1 - alpha))

    `update_counts` adds a batch of `n` observations with `c` successes that share one `p_s`. The
    controller sees dials aggregated per tick (`r_did`), so it tests at tick boundaries: the
    boundaries are checked after each batch, which is Wald's test observed at batch ends (a
    little more overshoot, never a different decision rule).
    """

    r0: float
    r1: float
    alpha: float
    beta: float
    llr: float = 0.0
    n: int = 0
    successes: int = 0
    decision: SprtDecision = "continue"

    @property
    def upper(self) -> float:
        return math.log((1.0 - self.beta) / self.alpha)

    @property
    def lower(self) -> float:
        return math.log(self.beta / (1.0 - self.alpha))

    def step_success(self) -> float:
        return math.log(self.r1 / self.r0)

    def step_failure(self, p_s: float) -> float:
        p_s = min(max(p_s, 1e-9), 0.999 / max(self.r0, self.r1))
        return math.log((1.0 - self.r1 * p_s) / (1.0 - self.r0 * p_s))

    def update_counts(self, n: int, c: int, p_s: float) -> "BernoulliRatioSprt":
        """Add `n` observations with `c` successes at comparator probability `p_s`. A decided
        test is returned unchanged (the decision is final)."""
        if self.decision != "continue" or n <= 0:
            return self
        llr = self.llr + c * self.step_success() + (n - c) * self.step_failure(p_s)
        dec: SprtDecision = "continue"
        if llr >= self.upper:
            dec = "accept_h1"
        elif llr <= self.lower:
            dec = "accept_h0"
        return replace(self, llr=llr, n=self.n + n, successes=self.successes + c, decision=dec)

    def drift(self, r_true: float, p_s: float) -> float:
        """Expected LLR increment per observation when the true ratio is `r_true`."""
        p_d = r_true * p_s
        return p_d * self.step_success() + (1.0 - p_d) * self.step_failure(p_s)

    def expected_n(self, r_true: float, p_s: float) -> float:
        """E[N] as DESIGN.md 5.9 tabulates it: the boundary the drift points at divided by
        |drift|; at zero drift Wald's `A x |B| / var` (the indifference worst case)."""
        d = self.drift(r_true, p_s)
        if abs(d) < 1e-12:
            p_d = r_true * p_s
            a, b = self.step_success(), self.step_failure(p_s)
            var = p_d * a * a + (1.0 - p_d) * b * b
            return self.upper * abs(self.lower) / var
        return (self.upper if d > 0 else abs(self.lower)) / abs(d)

    def expected_n_wald(self, r_true: float, p_s: float) -> float:
        """Wald's fuller approximation `E[N] = (P_up A + (1 - P_up) B) / drift`, with the
        operating characteristic P_up from Wald's h-root (no overshoot correction)."""
        d = self.drift(r_true, p_s)
        p_up = self.p_accept_h1(r_true, p_s)
        if abs(d) < 1e-12:
            return self.expected_n(r_true, p_s)
        return (p_up * self.upper + (1.0 - p_up) * self.lower) / d

    def p_accept_h1(self, r_true: float, p_s: float) -> float:
        """Wald's OC: P(accept H1) at `r_true`, from the nonzero root h of E[exp(h x step)] = 1."""
        p_d = r_true * p_s
        a, b = self.step_success(), self.step_failure(p_s)

        def mgf(h: float) -> float:
            return p_d * math.exp(h * a) + (1.0 - p_d) * math.exp(h * b) - 1.0

        d = p_d * a + (1.0 - p_d) * b
        if abs(d) < 1e-12:
            return abs(self.lower) / (self.upper + abs(self.lower))
        # The nonzero root has the opposite sign of the drift.
        lo, hi = (-60.0, -1e-9) if d > 0 else (1e-9, 60.0)
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if (mgf(mid) > 0) == (mgf(lo) > 0):
                lo = mid
            else:
                hi = mid
        h = 0.5 * (lo + hi)
        ea, eb = math.exp(h * self.upper), math.exp(h * self.lower)
        return (1.0 - eb) / (ea - eb)

    def to_json(self) -> dict[str, Any]:
        return {"r0": self.r0, "r1": self.r1, "alpha": self.alpha, "beta": self.beta,
                "llr": self.llr, "n": self.n, "successes": self.successes,
                "decision": self.decision}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "BernoulliRatioSprt":
        return cls(r0=float(d["r0"]), r1=float(d["r1"]), alpha=float(d["alpha"]),
                   beta=float(d["beta"]), llr=float(d.get("llr", 0.0)), n=int(d.get("n", 0)),
                   successes=int(d.get("successes", 0)),
                   decision=d.get("decision", "continue"))


# =============================================================================== Bernoulli CUSUM


@dataclass(frozen=True)
class BernoulliCusum:
    """One-sided Bernoulli CUSUM for a rise of a rate from `p0` to `p1` (DESIGN.md 7.1).

        on event:     S += ln(p1 / p0)
        on no event:  S += ln((1 - p1) / (1 - p0))
        S = max(0, S); alarm when S >= h and at least `min_events` events since the reset

    `update_counts(n=..., k=...)` adds `n` trials with `k` events (keyword-only, because two
    tracks once disagreed on the argument order). The flag series is hourly (`r_flags`),
    so the reflecting barrier is applied once per hour: the batch increment is exact, and the
    within-hour order (unknown) can only move S by the part of the path that dipped below zero.
    """

    p0: float
    p1: float
    h: float
    min_events: int = 5
    stat: float = 0.0
    events_since_reset: int = 0
    trials: int = 0
    alarm: bool = False

    @property
    def up(self) -> float:
        return math.log(self.p1 / self.p0)

    @property
    def down(self) -> float:
        return math.log((1.0 - self.p1) / (1.0 - self.p0))

    def update_counts(self, *, n: int, k: int) -> "BernoulliCusum":
        if n <= 0:
            return self
        s = max(0.0, self.stat + k * self.up + (n - k) * self.down)
        ev = self.events_since_reset + k
        return replace(self, stat=s, events_since_reset=ev, trials=self.trials + n,
                       alarm=self.alarm or (s >= self.h and ev >= self.min_events))

    def reset(self, p0: float | None = None, ratio: float | None = None) -> "BernoulliCusum":
        p0 = self.p0 if p0 is None else p0
        p1 = p0 * (ratio if ratio is not None else self.p1 / self.p0)
        return replace(self, p0=p0, p1=p1, stat=0.0, events_since_reset=0, trials=0,
                       alarm=False)

    def drift(self, p_true: float) -> float:
        return p_true * self.up + (1.0 - p_true) * self.down

    def expected_delay(self, p_true: float) -> float:
        """`h / drift` (DESIGN.md 7.1 delay table); inf when the drift is not positive."""
        d = self.drift(p_true)
        return self.h / d if d > 0 else math.inf


def flag_cusum_h(horizon_days: float, connected_per_day: float) -> float:
    """h = ln(horizon_days x connected per day): an in-control ARL of at least that many calls
    (DESIGN.md 7.1; ARL0 >= e^h for a Bernoulli CUSUM)."""
    return math.log(max(horizon_days * connected_per_day, math.e))


# =============================================================================== Page CUSUM


@dataclass(frozen=True)
class DriftPageCusum:
    """Two one-sided Page CUSUMs on standardised block statistics z_b (DESIGN.md 7.5).

        down:  S_b = max(0, S_{b-1} - z_b - k)   alarm at S_b >= h
        up:    U_b = max(0, U_{b-1} + z_b - k)   view only ("better than expected")
    """

    k: float
    h: float
    down: float = 0.0
    up: float = 0.0
    alarm: bool = False
    blocks: int = 0

    def update(self, z: float) -> "DriftPageCusum":
        d = max(0.0, self.down - z - self.k)
        u = max(0.0, self.up + z - self.k)
        return replace(self, down=d, up=u, alarm=d >= self.h, blocks=self.blocks + 1)

    def restart(self) -> "DriftPageCusum":
        """After an alarm is reported the downward arm restarts from 0 (a persisting drift then
        alarms again after another h)."""
        return replace(self, down=0.0, alarm=False)



# =============================================================================== decay Page CUSUM (T3)


@dataclass(frozen=True)
class PageCusum:
    """One-sided Page CUSUM for a DECREASE in a standardised series z_j:

        S_j = max(0, S_{j-1} - z_j - k),  alarm when S_j >= h.

    With k = 0.5 and h = 4 the in-control average run length is about 168 observations and
    the out-of-control run length about 8.4 at a 1-SE shift and 3.3 at 2 SE (5.7). `last` is
    the index of the last folded observation, so folding is idempotent across restarts.
    """

    k: float
    h: float
    stat: float = 0.0
    last: int | None = None
    alarm: bool = False
    n: int = 0

    def update(self, z: float, index: int | None = None) -> "PageCusum":
        """Fold one observation. An index at or before `last` is ignored (already folded)."""
        if index is not None and self.last is not None and index <= self.last:
            return self
        s = max(0.0, self.stat - z - self.k)
        return replace(self, stat=s, last=index if index is not None else self.last,
                       alarm=self.alarm or s >= self.h, n=self.n + 1)

    def reset(self) -> "PageCusum":
        return replace(self, stat=0.0, alarm=False, n=0)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "PageCusum":
        return cls(**dict(d))


# =============================================================================== two-rate SPRT (T3)


@dataclass(frozen=True)
class Sprt:
    """Wald SPRT between two Bernoulli rates p_h (H_H) and p_u (H_U), truncated at n_max.

    `alpha` = P(accept H_H | H_U true), `beta` = P(accept H_U | H_H true), as in 5.9. The
    statistic is llr = log L(H_U) / L(H_H) (it rises with failures when p_u < p_h); Wald's
    bounds accept H_U at llr >= log((1 - alpha) / beta) and H_H at llr <= log(alpha / (1 - beta)).
    `decision` is None while undecided, "H_U" or "H_H" once a bound is crossed or at truncation
    (truncation picks the side of the indifference rate `p_mid`).
    """

    p_h: float
    p_u: float
    alpha: float
    beta: float
    n_max: int
    p_mid: float
    llr: float = 0.0
    successes: int = 0
    trials: int = 0
    decision: str | None = None

    @property
    def upper(self) -> float:
        """Accept H_U when llr >= upper."""
        return math.log((1.0 - self.alpha) / self.beta)

    @property
    def lower(self) -> float:
        """Accept H_H when llr <= lower."""
        return math.log(self.alpha / (1.0 - self.beta))

    def update_counts(self, k: int, n: int) -> "Sprt":
        if self.decision is not None:
            return self
        llr = self.llr + k * math.log(self.p_u / self.p_h) + \
            (n - k) * math.log((1.0 - self.p_u) / (1.0 - self.p_h))
        s, t = self.successes + k, self.trials + n
        decision = None
        if llr >= self.upper:
            decision = "H_U"
        elif llr <= self.lower:
            decision = "H_H"
        elif t >= self.n_max:
            decision = "H_H" if s / t >= self.p_mid else "H_U"
        return replace(self, llr=llr, successes=s, trials=t, decision=decision)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Sprt":
        return cls(**dict(d))


def siegmund_arl(k: float, h: float, delta: float = 0.0) -> float:
    """Siegmund's approximation of the ARL of a one-sided Page CUSUM on N(delta, 1) blocks
    (detecting a shift in the direction of delta), b = h + 1.166:

        Delta = delta - k;  ARL = (exp(-2 Delta b) + 2 Delta b - 1) / (2 Delta^2),  b^2 at Delta = 0

    ARL0 is the value at delta = 0 (DESIGN.md 7.5: about 2,570 blocks at k 0.5, h 6)."""
    b = h + 1.166
    dl = delta - k
    if abs(dl) < 1e-12:
        return b * b
    return (math.exp(-2.0 * dl * b) + 2.0 * dl * b - 1.0) / (2.0 * dl * dl)


__all__ = ["BernoulliRatioSprt", "BernoulliCusum", "DriftPageCusum", "PageCusum", "Sprt",
           "siegmund_arl", "flag_cusum_h", "SprtDecision"]
