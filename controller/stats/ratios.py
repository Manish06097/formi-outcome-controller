"""Rates, ratios and tail probabilities (pure numpy and math, no scipy).

Owner: track T3 (milestone M5). Implements the small statistical primitives that DESIGN.md
sections 5.4 to 5.7, 5.9, 5.10 and 7.3 compute with:

- `norm_cdf`, `norm_sf`, `norm_ppf`: the standard normal (boundaries, power, z tests).
- `binom_tail`, `poisson_tail`: exact upper tails, computed in log space so they stay accurate
  for the small probabilities the detectors and verdicts use.
- `wilson`: the Wilson score interval of a proportion.
- `mantel_haenszel`: the stratified log rate ratio of an arm against a control, with its
  delta-method variance, and the covariance of two arms that share the control. This is the
  likelihood behind the Beta-binomial engagement model (learning/engagement.py) and the
  per-segment ratios of the decay monitor (5.7). With one stratum it is the plain log ratio of
  two Beta-binomial rates, `log(a/n1) - log(c/n0)`, with variance `(1-p1)/(n1 p1) + (1-p0)/(n0 p0)`.

Everything here is deterministic and side-effect free.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ------------------------------------------------------------------------------ the normal

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """P(Z <= x) for a standard normal Z."""
    return 0.5 * math.erfc(-x / _SQRT2)


def norm_sf(x: float) -> float:
    """P(Z > x), accurate in the far tail (uses erfc, not 1 - cdf)."""
    return 0.5 * math.erfc(x / _SQRT2)


def norm_cdf_array(x: np.ndarray) -> np.ndarray:
    """Vectorised `norm_cdf` (numpy has no erf; this uses math.erfc element-wise)."""
    flat = np.asarray(x, dtype=float).ravel()
    out = np.fromiter((0.5 * math.erfc(-v / _SQRT2) for v in flat), dtype=float,
                      count=flat.size)
    return out.reshape(np.shape(x))


def norm_ppf(p: float) -> float:
    """The quantile z with P(Z <= z) = p, for 0 < p < 1.

    Acklam's rational approximation (relative error about 1e-9) followed by two Newton steps
    on `norm_cdf`, which brings it to machine precision for every p used in the design.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf needs 0 < p < 1, got {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        z = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    elif p > 1 - lo:
        q = math.sqrt(-2 * math.log(1 - p))
        z = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    else:
        q = p - 0.5
        r = q * q
        z = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    for _ in range(2):  # Newton on the cdf
        err = norm_cdf(z) - p
        z -= err / (math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi))
    return z


# ------------------------------------------------------------------------------ exact tails

def _log_binom_pmf(k: np.ndarray, n: int, p: float) -> np.ndarray:
    k = np.asarray(k, dtype=float)
    lg = np.vectorize(math.lgamma)
    return (lg(n + 1.0) - lg(k + 1.0) - lg(n - k + 1.0)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact (log-space sum)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    ks = np.arange(k, n + 1)
    lp = _log_binom_pmf(ks, n, p)
    m = float(lp.max())
    return float(min(1.0, math.exp(m) * np.exp(lp - m).sum()))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exact."""
    return 1.0 - binom_tail(k + 1, n, p) if k < n else 1.0


def poisson_tail(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu), exact (1 - lower sum in log space)."""
    if k <= 0:
        return 1.0
    if mu <= 0.0:
        return 0.0
    ks = np.arange(0, k)
    lg = np.vectorize(math.lgamma)
    lp = ks * math.log(mu) - mu - lg(ks + 1.0)
    lower = float(np.exp(lp).sum())
    if lower < 0.5:
        return 1.0 - lower
    # upper sum directly when the tail is small, for precision
    hi = int(max(k + 50, mu + 20 * math.sqrt(mu) + 50))
    ks2 = np.arange(k, hi)
    lp2 = ks2 * math.log(mu) - mu - lg(ks2 + 1.0)
    return float(np.exp(lp2).sum())


# ------------------------------------------------------------------------------ intervals

def wilson(successes: float, n: float, conf: float = 0.95) -> tuple[float, float]:
    """Two-sided Wilson score interval for a proportion. n = 0 gives (0, 1)."""
    if n <= 0:
        return 0.0, 1.0
    z = norm_ppf(0.5 + conf / 2.0)
    p = successes / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


# ------------------------------------------------------------------------------ Mantel-Haenszel

@dataclass(frozen=True)
class RatioEstimate:
    """A stratified log rate ratio of an arm vs the control."""

    log_rr: float
    var: float
    n_arm: float
    n_ctrl: float
    a_std: float
    """Mantel-Haenszel standardised arm events sum_j a_j n0_j / N_j (for covariances)."""
    c_std: float
    """Standardised control events sum_j c_j n1_j / N_j."""

    @property
    def se(self) -> float:
        return math.sqrt(self.var)


def mantel_haenszel(a: np.ndarray, n1: np.ndarray, c: np.ndarray,
                    n0: np.ndarray) -> RatioEstimate | None:
    """Stratified log rate ratio of an arm (a events of n1 per stratum) vs a control (c of n0).

    Strata where either side has no calls carry no comparison and are dropped. Counts may be
    fractional (recency-weighted counts are scaled before they get here, which is the power
    likelihood the EW posterior uses, DESIGN.md 5.3).

    Variance by the delta method treating the stratum sizes as fixed:
      var(log A) = sum_j (n0_j/N_j)^2 a_j (1 - a_j/n1_j) / A^2,   A = sum_j a_j n0_j / N_j
      var(log C) = sum_j (n1_j/N_j)^2 c_j (1 - c_j/n0_j) / C^2,   C = sum_j c_j n1_j / N_j
    With one stratum this is (1-p1)/(n1 p1) + (1-p0)/(n0 p0), the unequal-size variance of 5.5.
    A side with zero events gets a 0.5 continuity correction. Returns None without any stratum
    holding both sides.
    """
    a, n1, c, n0 = (np.asarray(x, dtype=float) for x in (a, n1, c, n0))
    keep = (n1 > 0) & (n0 > 0)
    if not keep.any():
        return None
    a, n1, c, n0 = a[keep], n1[keep], c[keep], n0[keep]
    N = n1 + n0
    w1, w0 = n0 / N, n1 / N
    A = float((a * w1).sum())
    C = float((c * w0).sum())
    va = float((w1 * w1 * a * (1.0 - a / n1)).sum())
    vc = float((w0 * w0 * c * (1.0 - c / n0)).sum())
    if A <= 0.0:
        A, va = 0.5, max(va, 0.25)
    if C <= 0.0:
        C, vc = 0.5, max(vc, 0.25)
    var = va / (A * A) + vc / (C * C)
    return RatioEstimate(log_rr=math.log(A / C), var=max(var, 1e-12), n_arm=float(n1.sum()),
                         n_ctrl=float(n0.sum()), a_std=A, c_std=C)


def shared_control_cov(n1_a: np.ndarray, n1_b: np.ndarray, c: np.ndarray, n0: np.ndarray,
                       c_std_a: float, c_std_b: float) -> float:
    """Covariance of two arms' log ratios that share the control in the same strata.

    Both log ratios contain `-log C_x` with `C_x = sum_j c_j n1x_j / (n1x_j + n0_j)`, so by the
    delta method cov = sum_j (n1a_j/Na_j)(n1b_j/Nb_j) c_j (1 - c_j/n0_j) / (C_a C_b). Strata are
    aligned arrays (zero where an arm had no calls). Probability matching and P(best) need this:
    with a small control the arms move together.
    """
    n1_a, n1_b, c, n0 = (np.asarray(x, dtype=float) for x in (n1_a, n1_b, c, n0))
    keep = (n0 > 0) & (n1_a > 0) & (n1_b > 0)
    if not keep.any() or c_std_a <= 0 or c_std_b <= 0:
        return 0.0
    n1_a, n1_b, c, n0 = n1_a[keep], n1_b[keep], c[keep], n0[keep]
    wa = n1_a / (n1_a + n0)
    wb = n1_b / (n1_b + n0)
    return float((wa * wb * c * (1.0 - c / n0)).sum() / (c_std_a * c_std_b))
