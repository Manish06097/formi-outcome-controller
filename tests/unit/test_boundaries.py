"""The 5.4 and 5.5 numbers of DESIGN.md, reproduced from stats/boundaries.py."""
from __future__ import annotations

import pytest

from controller.settings import Thresholds
from controller.stats.boundaries import (arm_variance, doubling_looks, effective_boundary,
                                         first_exploration_share, look_informations,
                                         null_crossing, posterior_sd, power_at_look, union_bound)

T = Thresholds()
E = 0.55
POP_TAU, COH_TAU = 0.06, 0.12
LOOKS = doubling_looks(T.look_first_n, T.look_growth, 4)


def test_look_schedule_is_information_doubling():
    assert LOOKS == [200, 400, 800, 1600]


@pytest.mark.parametrize("n,pop99,pop995,coh99,coh998", [
    (200, 3.402, 3.767, 2.636, 3.262),
    (400, 2.914, 3.227, 2.486, 3.076),
    (800, 2.636, 2.920, 2.407, 2.979),
    (1600, 2.486, 2.753, 2.367, 2.929)])
def test_effective_boundaries_match_the_design_table(n, pop99, pop995, coh99, coh998):
    info = 1.0 / arm_variance(n, E)
    assert arm_variance(n, E) == pytest.approx(0.82 / n, rel=0.01)
    assert effective_boundary(info, POP_TAU, 0.99) == pytest.approx(pop99, abs=0.02)
    assert effective_boundary(info, POP_TAU, T.w1_population) == pytest.approx(pop995, abs=0.02)
    assert effective_boundary(info, COH_TAU, 0.99) == pytest.approx(coh99, abs=0.02)
    assert effective_boundary(info, COH_TAU, T.w1_cohort) == pytest.approx(coh998, abs=0.02)


def test_union_bounds_over_four_looks_match_the_design():
    infos = look_informations(LOOKS, E)
    assert union_bound(infos, POP_TAU, T.w1_population) == pytest.approx(0.0054, abs=0.0003)
    assert union_bound(infos, COH_TAU, T.w1_cohort) == pytest.approx(0.0047, abs=0.0003)
    assert union_bound(infos, POP_TAU, 0.99) == pytest.approx(0.0128, abs=0.0005)
    assert union_bound(infos, COH_TAU, 0.99) == pytest.approx(0.028, abs=0.001)


def test_null_crossing_probabilities_per_look():
    infos = look_informations(LOOKS, E)
    pop = [null_crossing(i, POP_TAU, T.w1_population) for i in infos]
    assert pop == pytest.approx([0.0000826, 0.000625, 0.00175, 0.00295], rel=0.06)
    coh = [null_crossing(i, COH_TAU, T.w1_cohort) for i in infos]
    assert coh == pytest.approx([0.000552, 0.00105, 0.00145, 0.00170], rel=0.06)


@pytest.mark.parametrize("c,p800,p1600", [(0.95, 0.87, 0.99), (0.975, 0.78, 0.98),
                                          (0.99, 0.63, 0.96), (0.995, 0.52, 0.93),
                                          (0.998, 0.39, 0.87)])
def test_power_table_for_a_ten_percent_effect(c, p800, p1600):
    theta = 0.0953
    assert power_at_look(theta, 800, E, POP_TAU, c) == pytest.approx(p800, abs=0.015)
    assert power_at_look(theta, 1600, E, POP_TAU, c) == pytest.approx(p1600, abs=0.015)


def test_cohort_power_for_a_22_percent_effect_at_the_400_look():
    assert power_at_look(0.199, 400, E, COH_TAU, T.w1_cohort) == pytest.approx(0.91, abs=0.02)


@pytest.mark.parametrize("n,sd", [(36, 0.056), (150, 0.047), (500, 0.034)])
def test_posterior_sd_after_n_arm_calls(n, sd):
    """5.4 quotes 0.035 after 500 calls; the formula it states gives 0.0336 (a rounding slip)."""
    assert posterior_sd(n, POP_TAU, 0.82) == pytest.approx(sd, abs=0.0006)


@pytest.mark.parametrize("arms,share", [(5, 0.038), (3, 0.049), (2, 0.060), (1, 0.085)])
def test_first_exploration_share_from_the_gate(arms, share):
    s = first_exploration_share(T.tau_h_floor, arms, T.grader_engagement_line, T.alpha_h)
    assert s == pytest.approx(share, abs=0.0006)


def test_cohort_line_one_percent_halves_the_first_exploration():
    pop = first_exploration_share(POP_TAU, 5, 0.01, T.alpha_h)
    two = first_exploration_share(COH_TAU, 5, 0.02, T.alpha_h)
    one = first_exploration_share(COH_TAU, 5, 0.01, T.alpha_h)
    assert two == pytest.approx(pop)          # x_k = 2%: no cost at launch
    assert one == pytest.approx(pop / 2)      # x_k = 1%: 1.9% per arm
    assert one == pytest.approx(0.019, abs=0.0005)
