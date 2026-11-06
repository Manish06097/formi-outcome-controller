"""Seeded posterior draws and seed derivation (DESIGN.md 5.3, 8.6, ADR 0004).

Owner: track T3 (milestone M5).

Every random number a decision uses comes from here, from a seed derived from stable inputs:
`derive_seed(run, workflow, decision_id, fit_kind)`. The same seed and the same posterior give
bit-identical draws on any machine with the same numpy (PCG64 is a fixed algorithm, and the
transform below uses only matrix products), so a decision replays exactly (9.6) and winner
selection is deterministic (constraint 4).

Draws are `mean + z @ L.T` with `L` the Cholesky factor of the covariance. A covariance that is
not numerically positive definite gets a growing diagonal jitter; a degenerate direction (zero
variance, for example a cohort deviation forced to zero) stays exactly at its mean.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

SEED_MOD = 2 ** 63
"""Seeds are stored in a Postgres bigint (`posterior_snapshot.seed`)."""


def derive_seed(*parts: Any) -> int:
    """A stable 63-bit seed from any printable parts (run id, workflow, decision id, kind)."""
    blob = "\x1f".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big") % SEED_MOD


def rng(seed: int) -> np.random.Generator:
    """The one generator constructor the controller uses (PCG64, fixed algorithm)."""
    return np.random.Generator(np.random.PCG64(int(seed) % SEED_MOD))


def cholesky_psd(cov: np.ndarray) -> np.ndarray:
    """A lower factor L with L L' = cov for a symmetric positive semi-definite matrix.

    Zero-variance rows (an exactly degenerate direction) are factored separately and get zero
    rows in L. Otherwise a diagonal jitter 1e-12 x mean diag, growing by 10x, is added until the
    factorisation succeeds.
    """
    cov = np.asarray(cov, dtype=float)
    n = cov.shape[0]
    if n == 0:
        return np.zeros((0, 0))
    cov = 0.5 * (cov + cov.T)
    d = np.diag(cov)
    live = d > 0
    L = np.zeros_like(cov)
    if not live.any():
        return L
    sub = cov[np.ix_(live, live)]
    scale = float(np.mean(np.diag(sub)))
    jitter = 0.0
    for _ in range(12):
        try:
            Ls = np.linalg.cholesky(sub + jitter * np.eye(sub.shape[0]))
            break
        except np.linalg.LinAlgError:
            jitter = scale * 1e-12 if jitter == 0.0 else jitter * 10.0
    else:  # pragma: no cover - only for pathological input
        w, V = np.linalg.eigh(sub)
        Ls = V @ np.diag(np.sqrt(np.clip(w, 0.0, None)))
    idx = np.flatnonzero(live)
    L[np.ix_(idx, idx)] = Ls
    return L


def seeded_draws(mean: np.ndarray, cov: np.ndarray, seed: int, n: int) -> np.ndarray:
    """(n, k) draws from N(mean, cov) with a seeded PCG64 generator."""
    mean = np.asarray(mean, dtype=float)
    k = mean.shape[0]
    z = rng(seed).standard_normal((n, k))
    return mean[None, :] + z @ cholesky_psd(cov).T
