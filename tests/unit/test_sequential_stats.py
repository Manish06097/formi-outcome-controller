"""Sequential statistics (DESIGN.md 5.9, 7.1, 7.5): SPRT sample sizes and error rates, the flag
CUSUM delays, and the drift Page CUSUM ARLs, each checked by seeded simulation against the
numbers the design states (10.1 test_did_sprt, test_drift_cusum)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.settings import Thresholds
from controller.stats.sequential import (BernoulliCusum, BernoulliRatioSprt, DriftPageCusum,
                                         flag_cusum_h, siegmund_arl)

T = Thresholds()
P = 0.26   # the one pickup rate of the 5.9 worked numbers (sanity label, dev renewal-like)


def sprt() -> BernoulliRatioSprt:
    return BernoulliRatioSprt(T.sprt_r_unhealthy, T.sprt_r_healthy, T.sprt_alpha, T.sprt_beta)


def simulate_sprt(r: float, reps: int, seed: int, n_max: int = 3000) -> tuple[np.ndarray,
                                                                              np.ndarray]:
    """Per-dial SPRT exactly as 5.9 writes it: (dials to decision, decided healthy)."""
    s = sprt()
    a, b = s.step_success(), s.step_failure(P)
    rng = np.random.default_rng(seed)
    x = rng.random((reps, n_max)) < r * P
    c = np.cumsum(np.where(x, a, b), axis=1)
    up, lo = c >= s.upper, c <= s.lower
    fu = np.where(up.any(1), up.argmax(1), n_max)
    fl = np.where(lo.any(1), lo.argmax(1), n_max)
    return np.minimum(fu, fl) + 1, fu < fl


def test_sprt_boundaries_and_steps_match_the_design():
    s = sprt()
    assert s.upper == pytest.approx(math.log(95), abs=1e-12)        # +4.55
    assert s.lower == pytest.approx(math.log(0.05 / 0.99), abs=1e-12)  # -2.99
    assert s.step_success() == pytest.approx(0.357, abs=1e-3)
    assert s.step_failure(P) == pytest.approx(-0.100, abs=1e-3)


@pytest.mark.parametrize("r,table", [(1.10, 149), (1.00, 245), (0.64, 123), (0.36, 52)])
def test_sprt_expected_dials_match_the_5_9_table(r, table):
    """Closed form equals the table; simulated E[N] within 10% of it (10.1 test_did_sprt)."""
    s = sprt()
    assert s.expected_n(r, P) == pytest.approx(table, rel=0.02)
    n, _ = simulate_sprt(r, reps=20_000, seed=int(r * 100))
    assert n.mean() == pytest.approx(table, rel=0.10)


def test_sprt_indifference_point_needs_about_380_dials():
    s = sprt()
    # at the drift-zero ratio Wald's A |B| / var gives the worst case of the table
    lo, hi = 0.5, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if s.drift(mid, P) < 0 else (lo, mid)
    assert lo == pytest.approx(0.84, abs=0.01)
    assert s.expected_n(lo, P) == pytest.approx(380, rel=0.05)


def test_sprt_error_rates():
    """P(healthy | r = 0.70) <= 1% and P(unhealthy | r = 1.0) <= 5%."""
    _, healthy_at_h0 = simulate_sprt(0.70, reps=40_000, seed=7)
    _, healthy_at_h1 = simulate_sprt(1.00, reps=40_000, seed=8)
    se0 = math.sqrt(0.01 * 0.99 / 40_000)
    se1 = math.sqrt(0.05 * 0.95 / 40_000)
    assert healthy_at_h0.mean() <= 0.01 + 3 * se0
    assert 1 - healthy_at_h1.mean() <= 0.05 + 3 * se1
    assert sprt().p_accept_h1(0.70, P) == pytest.approx(0.01, abs=0.003)


def test_sprt_batches_reach_the_same_decision_as_single_dials():
    """The controller feeds per-tick batches; decisions agree with the per-dial test except for
    the overshoot inside the last batch."""
    rng = np.random.default_rng(3)
    agree = 0
    for _ in range(300):
        x = rng.random(2000) < 1.0 * P
        single = sprt()
        for v in x:
            single = single.update_counts(1, int(v), P)
            if single.decision != "continue":
                break
        batched = sprt()
        for i in range(0, 2000, 25):
            batched = batched.update_counts(25, int(x[i:i + 25].sum()), P)
            if batched.decision != "continue":
                break
        agree += single.decision == batched.decision
        assert batched.n >= single.n or batched.decision != single.decision
    assert agree / 300 >= 0.95


def test_sprt_state_round_trips():
    s = sprt().update_counts(100, 30, P)
    assert BernoulliRatioSprt.from_json(s.to_json()) == s
    done = sprt().update_counts(1000, 400, P)
    assert done.decision == "accept_h1"
    assert done.update_counts(100, 0, P) == done   # a decision is final


# ------------------------------------------------------------------------------- flag CUSUM (7.1)

def test_flag_cusum_h_and_delays_match_the_7_1_table():
    assert flag_cusum_h(30, 1200) == pytest.approx(math.log(36_000))      # 10.5
    assert flag_cusum_h(30, 13_500) == pytest.approx(math.log(405_000))   # 12.9
    c = BernoulliCusum(0.0147, 0.0294, flag_cusum_h(30, 1200))
    assert c.down == pytest.approx(-0.01503, abs=2e-5)
    assert c.drift(0.0147) < 0
    assert c.expected_delay(0.0298) == pytest.approx(1730, rel=0.05)
    assert c.expected_delay(0.0535) == pytest.approx(460, rel=0.05)


def test_flag_cusum_detects_a_doubling_and_stays_quiet_in_control():
    rng = np.random.default_rng(11)
    h = flag_cusum_h(30, 1200)
    delays = []
    for _ in range(200):
        c = BernoulliCusum(0.0147, 0.0294, h)
        n = 0
        while not c.alarm and n < 20_000:
            k = rng.binomial(100, 0.0535)          # hourly batches of 100 connected calls
            c = c.update_counts(n=100, k=int(k))
            n += 100
        delays.append(n)
    assert np.mean(delays) == pytest.approx(460, rel=0.35)   # hourly granularity adds overshoot
    # in control: 30 days at 1,200 calls a day rarely alarms (ARL0 >= e^h = 36,000 calls)
    alarms = 0
    for _ in range(200):
        c = BernoulliCusum(0.0147, 0.0294, h)
        for _ in range(360):                         # 30 days x 12 hourly batches
            c = c.update_counts(n=100, k=int(rng.binomial(100, 0.0147)))
        alarms += c.alarm
    assert alarms / 200 <= 0.5


def test_flag_cusum_needs_five_flags():
    c = BernoulliCusum(0.0001, 0.0002, h=0.5, min_events=5)
    c = c.update_counts(n=10, k=4)
    assert c.stat >= c.h and not c.alarm
    c = c.update_counts(n=1, k=1)
    assert c.alarm


# ------------------------------------------------------------------------------- drift CUSUM (7.5)

def simulate_page(delta: float, reps: int, seed: int, max_blocks: int) -> np.ndarray:
    """Run lengths of the downward Page CUSUM on z ~ N(-delta, 1) blocks (vectorised over reps)."""
    rng = np.random.default_rng(seed)
    k, h = T.drift_k, T.drift_h
    s = np.zeros(reps)
    run = np.full(reps, max_blocks)
    alive = np.ones(reps, dtype=bool)
    for b in range(max_blocks):
        z = rng.standard_normal(reps) - delta
        s = np.maximum(0.0, s - z - k)
        hit = alive & (s >= h)
        run[hit] = b + 1
        alive &= ~hit
        if not alive.any():
            break
    return run


def test_siegmund_matches_the_7_5_numbers():
    assert siegmund_arl(0.5, 6.0) == pytest.approx(2570, rel=0.01)
    assert siegmund_arl(0.5, 6.0, 1.15) == pytest.approx(10, rel=0.05)
    assert siegmund_arl(0.5, 6.0, 2.31) == pytest.approx(3.8, rel=0.05)
    assert siegmund_arl(0.5, 6.0, 0.94) == pytest.approx(14, rel=0.05)
    assert siegmund_arl(0.5, 6.0, 0.77) == pytest.approx(20, rel=0.05)


@pytest.mark.slow
def test_drift_cusum_arl0_by_simulation():
    """In-control ARL at least 2,000 blocks at k 0.5, h 6 (10.1 test_drift_cusum)."""
    run = simulate_page(0.0, reps=4000, seed=21, max_blocks=40_000)
    assert run.mean() >= 2000
    assert run.mean() == pytest.approx(siegmund_arl(0.5, 6.0), rel=0.15)


@pytest.mark.parametrize("delta,table", [(1.15, 10.0), (2.31, 3.8), (0.94, 14.0), (0.77, 20.0)])
def test_drift_cusum_delays_within_20_percent(delta, table):
    run = simulate_page(delta, reps=20_000, seed=int(delta * 100), max_blocks=500)
    assert run.mean() == pytest.approx(table, rel=0.20)


def test_page_cusum_object_matches_the_vectorised_rule():
    rng = np.random.default_rng(5)
    zs = rng.standard_normal(500) - 0.8
    c = DriftPageCusum(T.drift_k, T.drift_h)
    s = 0.0
    first = None
    for i, z in enumerate(zs):
        c = c.update(float(z))
        s = max(0.0, s - z - T.drift_k)
        assert c.down == pytest.approx(s)
        if c.alarm and first is None:
            first = i
            c = c.restart()
            s = 0.0
    assert first is not None
