"""Pure statistics primitives (controller/stats): hand-computed fixtures and brute-force checks."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.stats.draws import cholesky_psd, derive_seed, seeded_draws
from controller.stats.glm import SparseDesign, fit_ridge_logit
from controller.stats.ratios import (binom_cdf, binom_tail, mantel_haenszel, norm_cdf, norm_ppf,
                                     norm_sf, poisson_tail, shared_control_cov, wilson)
from controller.stats.sequential import BernoulliCusum, PageCusum, Sprt


# ------------------------------------------------------------------------------ normal
@pytest.mark.parametrize("p,z", [(0.975, 1.959963984540054), (0.995, 2.5758293035489004),
                                 (0.998, 2.878161739095476), (0.9, 1.2815515655446004),
                                 (1e-6, -4.753424308822899), (0.5, 0.0)])
def test_norm_ppf_matches_reference_quantiles(p, z):
    assert norm_ppf(p) == pytest.approx(z, abs=1e-9)
    assert norm_cdf(norm_ppf(p)) == pytest.approx(p, rel=1e-9)


def test_norm_sf_is_accurate_in_the_far_tail():
    # 1 - Phi(6) = 9.8659e-10; computing it as 1 - cdf would lose most digits
    assert norm_sf(6.0) == pytest.approx(9.865876450377e-10, rel=1e-9)


# ------------------------------------------------------------------------------ exact tails
def test_binom_tail_matches_brute_force():
    n, p = 30, 0.07
    pmf = [math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(n + 1)]
    for k in (0, 1, 3, 7, 30):
        assert binom_tail(k, n, p) == pytest.approx(sum(pmf[k:]), rel=1e-10)
    assert binom_cdf(3, n, p) == pytest.approx(sum(pmf[:4]), rel=1e-10)


def test_poisson_tail_matches_brute_force_including_small_tails():
    mu = 3.2
    pmf = [math.exp(-mu) * mu ** k / math.factorial(k) for k in range(80)]
    for k in (0, 1, 4, 12, 20):
        assert poisson_tail(k, mu) == pytest.approx(sum(pmf[k:]), rel=1e-8)


def test_wilson_interval_hand_computed():
    lo, hi = wilson(10, 100, 0.95)
    # p = 0.1, z = 1.96: centre 0.1176, half-width 0.0603
    assert lo == pytest.approx(0.05523, abs=1e-4)
    assert hi == pytest.approx(0.17437, abs=1e-4)
    assert wilson(0, 0) == (0.0, 1.0)


# ------------------------------------------------------------------------------ Mantel-Haenszel
def test_mh_single_stratum_is_the_beta_binomial_log_ratio_with_unequal_size_variance():
    a, n1, c, n0 = 110, 200, 5500, 10000
    r = mantel_haenszel(np.array([a]), np.array([n1]), np.array([c]), np.array([n0]))
    p1, p0 = a / n1, c / n0
    assert r.log_rr == pytest.approx(math.log(p1 / p0))
    assert r.var == pytest.approx((1 - p1) / (n1 * p1) + (1 - p0) / (n0 * p0))


def test_mh_is_robust_to_simpsons_paradox_across_strata():
    # Arm and control have the same rate within each stratum, but most arm calls fall in the
    # high-rate stratum (an arm ramped up on a good day): pooling shows a fake 27% gain.
    a = np.array([40.0, 120.0])
    n1 = np.array([100.0, 200.0])      # stratum rates 0.4 and 0.6
    c = np.array([400.0, 60.0])
    n0 = np.array([1000.0, 100.0])
    r = mantel_haenszel(a, n1, c, n0)
    assert r.log_rr == pytest.approx(0.0, abs=1e-12)
    pooled = math.log((a.sum() / n1.sum()) / (c.sum() / n0.sum()))
    assert pooled > 0.2


def test_mh_drops_strata_without_both_sides_and_returns_none_without_any():
    assert mantel_haenszel(np.array([3.0]), np.array([5.0]), np.array([0.0]),
                           np.array([0.0])) is None
    r = mantel_haenszel(np.array([3.0, 9.0]), np.array([5.0, 0.0]), np.array([30.0, 1.0]),
                        np.array([50.0, 2.0]))
    assert r.n_arm == 5.0 and r.n_ctrl == 50.0


def test_shared_control_covariance_is_the_control_term_of_each_variance():
    n1a, n1b = np.array([200.0]), np.array([200.0])
    c, n0 = np.array([550.0]), np.array([1000.0])
    ra = mantel_haenszel(np.array([110.0]), n1a, c, n0)
    rb = mantel_haenszel(np.array([115.0]), n1b, c, n0)
    cov = shared_control_cov(n1a, n1b, c, n0, ra.c_std, rb.c_std)
    p0 = 0.55
    assert cov == pytest.approx((1 - p0) / (1000 * p0), rel=1e-12)


# ------------------------------------------------------------------------------ sequential
def test_page_cusum_folds_each_index_once_and_alarms_at_h():
    c = PageCusum(k=0.5, h=4.0)
    for j, z in enumerate([-1.5, -1.5, -1.5, -1.5]):
        c = c.update(z, j)
    assert c.stat == pytest.approx(4.0) and c.alarm
    again = c.update(-10.0, 3)          # already folded index: ignored
    assert again.stat == c.stat
    assert PageCusum.from_json(c.to_json()) == c
    up = PageCusum(k=0.5, h=4.0).update(2.0, 0)
    assert up.stat == 0.0               # increases never accumulate in a decrease detector


def test_page_cusum_in_control_run_length():
    """One-sided k = 0.5, h = 4: ARL0 is about 336 segments (Siegmund's approximation and the
    standard tables). DESIGN.md 5.7 quotes 168, which is the TWO-sided value; the one-sided
    detector we run false-alarms half as often as the design states (ADR 0006)."""
    rng = np.random.default_rng(1)
    lengths = []
    for _ in range(600):
        s, n = 0.0, 0
        while s < 4.0:
            s = max(0.0, s - rng.standard_normal() - 0.5)
            n += 1
        lengths.append(n)
    assert 300 < np.mean(lengths) < 380


def test_bernoulli_cusum_and_sprt_decide_on_hand_computed_llrs():
    b = BernoulliCusum(p0=0.01, p1=0.02, h=3.0).update_counts(n=100, k=5)
    assert b.stat == pytest.approx(5 * math.log(2) + 95 * math.log(0.98 / 0.99))
    assert not b.alarm
    s = Sprt(p_h=0.26, p_u=0.182, alpha=0.01, beta=0.05, n_max=1000, p_mid=0.221)
    healthy = s.update_counts(26 * 4, 400)
    assert healthy.decision == "H_H"
    sick = s.update_counts(15, 150)
    assert sick.decision == "H_U"
    assert Sprt.from_json(sick.to_json()) == sick


# ------------------------------------------------------------------------------ draws
def test_seeded_draws_are_bit_identical_for_the_same_seed():
    mean = np.array([0.1, -0.2, 0.0])
    cov = np.array([[0.01, 0.002, 0.0], [0.002, 0.02, 0.0], [0.0, 0.0, 0.0]])
    a = seeded_draws(mean, cov, 42, 4000)
    b = seeded_draws(mean, cov, 42, 4000)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, seeded_draws(mean, cov, 43, 4000))
    assert np.all(a[:, 2] == 0.0)                    # a zero-variance direction stays at its mean
    assert np.cov(a[:, :2].T) == pytest.approx(cov[:2, :2], abs=2e-3)


def test_derive_seed_is_stable_and_fits_a_bigint():
    s = derive_seed("run_abc_1", "nw_renewal", 17, "ew")
    assert s == derive_seed("run_abc_1", "nw_renewal", 17, "ew")
    assert 0 <= s < 2 ** 63
    assert s != derive_seed("run_abc_1", "nw_renewal", 18, "ew")


def test_cholesky_psd_handles_singular_projection_matrices():
    w = np.array([0.5, 0.3, 0.2])
    P = np.eye(3) - np.outer(w, w) / (w @ w)
    L = cholesky_psd(0.01 * P)
    assert np.allclose(L @ L.T, 0.01 * P, atol=1e-10)


# ------------------------------------------------------------------------------ ridge logit
def test_ridge_logit_recovers_rates_and_shrinks_toward_the_prior():
    # rows: intercept only, and intercept + arm; aggregated counts
    cols = np.array([[0, -1], [0, 1]])
    design = SparseDesign(cols=cols, n_params=2)
    succ = np.array([5500.0, 1200.0])
    trials = np.array([10000.0, 2000.0])
    fit = fit_ridge_logit(design, succ, trials, np.ones(2), prior_sd=np.array([10.0, 10.0]))
    assert fit.converged
    b0 = math.log(0.55 / 0.45)
    assert fit.mean[0] == pytest.approx(b0, abs=2e-3)
    assert fit.mean[1] == pytest.approx(math.log(0.6 / 0.4) - b0, abs=5e-3)
    tight = fit_ridge_logit(design, succ, trials, np.ones(2), prior_sd=np.array([10.0, 0.01]))
    assert abs(tight.mean[1]) < 0.01                 # strong prior pulls the arm to 0
    # Laplace sd of the arm term: about sqrt(1/(n p q) + 1/(n0 p0 q0))
    sd = math.sqrt(fit.cov[1, 1])
    assert sd == pytest.approx(math.sqrt(1 / (2000 * .6 * .4) + 1 / (10000 * .55 * .45)), rel=0.05)


def test_ridge_logit_weights_are_a_power_likelihood():
    cols = np.array([[0, 1]])
    design = SparseDesign(cols=cols, n_params=2)
    full = fit_ridge_logit(design, np.array([600.0]), np.array([1000.0]), np.array([1.0]),
                           prior_sd=np.array([10.0, 10.0]))
    half = fit_ridge_logit(design, np.array([600.0]), np.array([1000.0]), np.array([0.5]),
                           prior_sd=np.array([10.0, 10.0]))
    assert half.cov[0, 0] > full.cov[0, 0]
