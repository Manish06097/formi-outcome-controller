"""Sequential statistics with serialisable state: Page CUSUM, Bernoulli CUSUM, Wald SPRT.

Owner: track T3 (milestone M5) for `PageCusum` (the decay monitor of DESIGN.md 5.7); the
Bernoulli CUSUM (7.1) and the SPRT (5.9) are the generic primitives track T4's detectors build
on (see the T3 report: the M0 placeholder named T4 as owner of this file).

Every state is a small frozen dataclass with `to_json` / `from_json`, persisted after every step
(decay_monitor, flag_monitor, did_state), so a restart never forgets path-dependent statistics
(constraint 8). Updates are pure functions of (state, observation) -> new state.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


# ------------------------------------------------------------------------------ Page CUSUM

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


# ------------------------------------------------------------------------------ Bernoulli CUSUM

@dataclass(frozen=True)
class BernoulliCusum:
    """Upward Bernoulli CUSUM for a rate change p0 -> p1 (p1 > p0), log-likelihood-ratio form:

        S <- max(0, S + x log(p1/p0) + (1 - x) log((1-p1)/(1-p0)))

    `update_counts(k, n)` folds n trials with k successes at once, which is exact when the
    statistic stays positive throughout and otherwise an upper bound on the per-trial path; the
    flag detector (7.1) folds per sim hour, so this matches the per-call statistic to within
    one hour's resets.
    """

    p0: float
    p1: float
    h: float
    stat: float = 0.0
    alarm: bool = False
    successes: int = 0
    trials: int = 0

    @property
    def llr_success(self) -> float:
        return math.log(self.p1 / self.p0)

    @property
    def llr_failure(self) -> float:
        return math.log((1.0 - self.p1) / (1.0 - self.p0))

    def update_counts(self, k: int, n: int) -> "BernoulliCusum":
        s = max(0.0, self.stat + k * self.llr_success + (n - k) * self.llr_failure)
        return replace(self, stat=s, alarm=self.alarm or s >= self.h,
                       successes=self.successes + k, trials=self.trials + n)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "BernoulliCusum":
        return cls(**dict(d))


# ------------------------------------------------------------------------------ SPRT

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
