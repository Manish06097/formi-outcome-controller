"""Effective winner boundaries, null crossing probabilities and power (DESIGN.md 5.5).

Owner: track T3 (milestone M5).

The winner rule W1 is Bayesian: declare when `P(theta > 0 | data) >= c` under a prior
`theta ~ N(0, v)` and an estimate `theta_hat` with variance `s^2`. The posterior is normal with
mean `theta_hat * v / (v + s^2)` and variance `v s^2 / (v + s^2)`, so W1 is the frequentist test

    z = theta_hat / s >= z_c * sqrt(1 + s^2 / v),      z_c = Phi^-1(c)

whose boundary widens when information is low (the prior shrinks the estimate). Information
`I = 1 / s^2`; with unequal sizes the arm dominates, `s^2 = (1 - e) / (e n) = 0.82 / n` at
e = 0.55. These functions reproduce the 5.5 tables and are what `tests/unit/test_boundaries.py`
checks against the design's numbers. The union bound over looks is an upper bound on the
false-winner rate of one null hypothesis; looks are correlated, so the true rate is lower
(measured by tests/unit/test_false_winner_rate.py).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from controller.stats.ratios import norm_cdf, norm_ppf, norm_sf


def arm_variance(n: float, e: float) -> float:
    """Variance of a log rate ratio dominated by the arm: (1 - e) / (e n) (5.5, unequal sizes)."""
    return (1.0 - e) / (e * n)


def effective_boundary(info: float, tau: float, p: float) -> float:
    """The z boundary equivalent to `P(theta > 0) >= p` with prior sd `tau` and information
    `info = 1 / s^2`: `z_p * sqrt(1 + 1 / (info * tau^2))`."""
    if info <= 0:
        return math.inf
    return norm_ppf(p) * math.sqrt(1.0 + 1.0 / (info * tau * tau))


def null_crossing(info: float, tau: float, p: float) -> float:
    """P(z >= boundary) at one look when theta = 0 (z ~ N(0, 1))."""
    return norm_sf(effective_boundary(info, tau, p))


def union_bound(looks: Iterable[float], tau: float, p: float) -> float:
    """Sum of per-look null crossing probabilities over the looks' information levels."""
    return float(sum(null_crossing(i, tau, p) for i in looks))


def doubling_looks(first_n: int, growth: float, k: int) -> list[int]:
    """The information-doubling look schedule in arm calls: 200, 400, 800, 1,600 (5.5)."""
    return [int(round(first_n * growth ** i)) for i in range(k)]


def look_informations(ns: Sequence[int], e: float) -> list[float]:
    return [1.0 / arm_variance(n, e) for n in ns]


def power_at_look(theta: float, n: float, e: float, tau: float, p: float) -> float:
    """Marginal P(W1 crosses at the look with n arm calls) for a true effect theta (5.5 table):
    `Phi(theta / s - boundary)`. A lower bound on 'declared by then'."""
    s = math.sqrt(arm_variance(n, e))
    return norm_cdf(theta / s - effective_boundary(1.0 / (s * s), tau, p))


def posterior_sd(n: float, tau: float, var_per_call: float) -> float:
    """Posterior sd of an arm effect after n arm calls: 1 / sqrt(1/tau^2 + n / var_per_call)
    (5.4 ramp-speed paragraph; var_per_call = (1 - e) / e = 0.82 at e = 0.55)."""
    return 1.0 / math.sqrt(1.0 / (tau * tau) + n / var_per_call)


def first_exploration_share(tau: float, n_arms: int, line: float, alpha: float) -> float:
    """Largest equal share s per untested arm with `P(de < -line) <= alpha` under the prior
    (mean 0, sd tau per arm): `z_{1-alpha} * tau * sqrt(n_arms) * s <= line` (5.4)."""
    return line / (norm_ppf(1.0 - alpha) * tau * math.sqrt(n_arms))
