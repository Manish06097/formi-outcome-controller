"""Ridge logistic regression on aggregated binomial rows, Laplace posterior (DESIGN.md 5.3).

Owner: track T3 (milestone M5).

The design matrix is sparse and almost entirely indicators: every aggregated `r_engage` row
switches on one cell intercept, one segment intercept, one prompt intercept and, per non-control
arm, the arm's main effect and one interaction per feature (at most 11 non-zeros). So the matrix
is passed as `SparseDesign`: an (n_rows, m) array of column indices, -1 for padding, and an
optional matching array of values (1 when omitted).

`X' W X` is accumulated with one `np.bincount` per pair of index positions over flattened
`(i, j)` indices, which is the 5.3 cost model (about 2.4M adds at public_03 scale), then Newton
(IRLS) steps solve `(X' W X + P) step = X'(y - n mu) w - P beta` by Cholesky. At the mode the
Laplace posterior is `N(beta_hat, (X' W X + P)^-1)`.

Row weights multiply the likelihood (a power likelihood): that is how the recency-weighted (EW)
fit of 5.3 discounts old segments without discarding them.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, eq=False)
class SparseDesign:
    """Rows of a sparse design matrix as padded column indices (and optional values)."""

    cols: np.ndarray
    """(n_rows, m) int, -1 = no entry."""
    n_params: int
    vals: np.ndarray | None = None
    """(n_rows, m) float, or None for an all-ones (indicator) design."""

    def values(self) -> np.ndarray:
        v = np.ones(self.cols.shape, dtype=float) if self.vals is None else self.vals
        return np.where(self.cols >= 0, v, 0.0)

    def matvec(self, beta: np.ndarray) -> np.ndarray:
        safe = np.where(self.cols >= 0, self.cols, 0)
        return (beta[safe] * self.values()).sum(axis=1)

    def rmatvec(self, r: np.ndarray) -> np.ndarray:
        """X' r."""
        v = self.values()
        mask = self.cols >= 0
        return np.bincount(self.cols[mask], weights=(v * r[:, None])[mask],
                           minlength=self.n_params)

    def gram(self, w: np.ndarray) -> np.ndarray:
        """X' diag(w) X, accumulated pair of positions by pair of positions."""
        p = self.n_params
        v = self.values()
        m = self.cols.shape[1]
        out = np.zeros(p * p)
        for a in range(m):
            ca, va = self.cols[:, a], v[:, a]
            for b in range(a, m):
                cb, vb = self.cols[:, b], v[:, b]
                mask = (ca >= 0) & (cb >= 0)
                if not mask.any():
                    continue
                wt = (w * va * vb)[mask]
                idx = ca[mask] * p + cb[mask]
                acc = np.bincount(idx, weights=wt, minlength=p * p)
                out += acc
                if a != b:  # the symmetric (b, a) entries
                    out += np.bincount(cb[mask] * p + ca[mask], weights=wt, minlength=p * p)
        return out.reshape(p, p)


@dataclass(frozen=True, eq=False)
class RidgeFit:
    mean: np.ndarray
    cov: np.ndarray
    iterations: int
    converged: bool
    loglik: float


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * x))


def fit_ridge_logit(design: SparseDesign, successes: np.ndarray, trials: np.ndarray,
                    weights: np.ndarray, prior_sd: np.ndarray, prior_mean: np.ndarray | None = None,
                    max_iter: int = 30, tol: float = 1e-8) -> RidgeFit:
    """MAP fit of `logit P(success) = X beta` with `beta ~ N(prior_mean, diag(prior_sd^2))`.

    `successes`, `trials` are aggregated counts per row and `weights` multiply each row's
    log-likelihood. Deterministic: no randomness, a fixed iteration order, Cholesky solves.
    """
    y = np.asarray(successes, dtype=float)
    n = np.asarray(trials, dtype=float)
    wr = np.asarray(weights, dtype=float)
    p = design.n_params
    prec = 1.0 / np.asarray(prior_sd, dtype=float) ** 2
    m0 = np.zeros(p) if prior_mean is None else np.asarray(prior_mean, dtype=float)
    beta = m0.copy()
    converged = False
    it = 0
    H = np.diag(prec)
    for it in range(1, max_iter + 1):
        eta = design.matvec(beta)
        mu = _sigmoid(eta)
        grad = design.rmatvec(wr * (y - n * mu)) - prec * (beta - m0)
        H = design.gram(wr * n * mu * (1.0 - mu)) + np.diag(prec)
        L = np.linalg.cholesky(H)
        step = np.linalg.solve(L.T, np.linalg.solve(L, grad))
        beta = beta + step
        if float(np.max(np.abs(step))) < tol:
            converged = True
            break
    eta = design.matvec(beta)
    mu = _sigmoid(eta)
    H = design.gram(wr * n * mu * (1.0 - mu)) + np.diag(prec)
    L = np.linalg.cholesky(H)
    Linv = np.linalg.solve(L, np.eye(p))
    cov = Linv.T @ Linv
    with np.errstate(divide="ignore", invalid="ignore"):
        ll = float((wr * (y * np.log(np.clip(mu, 1e-300, 1)) +
                          (n - y) * np.log(np.clip(1 - mu, 1e-300, 1)))).sum())
    return RidgeFit(mean=beta, cov=0.5 * (cov + cov.T), iterations=it, converged=converged,
                    loglik=ll)
