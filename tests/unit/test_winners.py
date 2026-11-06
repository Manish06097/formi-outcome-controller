"""Winner rule W1 to W6, look schedule, futility and holdout sizing (DESIGN.md 5.5, 5.6)."""
from __future__ import annotations

import pytest

from controller.learning.allocator import open_experiments
from controller.learning.engagement import BetaBinomialModel
from controller.learning.winners import (evaluate_looks, holdout_se, holdout_size, look_due,
                                         promotion_config)
from controller.types import DecayMonitorState, LookRecord
from tests.support.t3_world import base_config, make_spec, uniform_world

SPEC = make_spec()
M = BetaBinomialModel()


@pytest.mark.parametrize("n_k,h,se", [(950, 0.20, 0.0725), (475, 0.20, 0.103),
                                      (13500, 0.05, 0.035)])
def test_holdout_table(n_k, h, se):
    assert holdout_size(n_k, 0.555) == h
    assert holdout_se(n_k, 0.555, h) == pytest.approx(se, abs=0.001)


def test_look_schedule_first_at_200_over_two_days_then_doubling():
    assert not look_due(250, 1, [])
    assert not look_due(150, 3, [])
    assert look_due(200, 2, [])
    prev = [LookRecord("e", "W2", "population", 1, 230, 0)]
    assert not look_due(459, 5, prev)
    assert look_due(460, 5, prev)


def test_look_max_caps_the_looks_of_one_hypothesis():
    import dataclasses
    from controller.settings import Thresholds
    prev = [LookRecord("e", "W2", "population", i + 1, 200 * 2 ** i, 0) for i in range(4)]
    assert look_due(10 ** 6, 9, prev)                                   # default: no cap
    capped = dataclasses.replace(Thresholds(), look_max=4)
    assert not look_due(10 ** 6, 9, prev, capped)
    assert look_due(10 ** 6, 9, prev[:3], capped)


def _world(mult, days=4, share=0.25, seed=1, per_day=3000):
    w = uniform_world(seed=seed, connected_per_day=per_day, mult=mult)
    cfg0 = base_config(SPEC)
    exps = open_experiments(w.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "welcome_message",
                               {"W1": 1 - 2 * share, "W2": share, "W3": share})
    for s in range(2 * days):
        w.simulate_segment(cfg, s)
    return w, cfg, exps


def _evaluate(w, cfg, exps, seg, **kw):
    ws = w.snapshot(cfg, seg, experiments=exps, **kw)
    stat = M.draws(M.fit(ws, "stationary"), 10)
    ew = M.draws(M.fit(ws, "ew"), 11)
    return evaluate_looks(ws, stat, ew), ws


def _strong(dim, o, cell, d):
    return 1.20 if (dim, o) == ("welcome_message", "W2") else 1.0


def test_a_strong_stable_arm_wins_the_population_at_its_first_look():
    w, cfg, exps = _world(_strong)
    out, ws = _evaluate(w, cfg, exps, 8)
    pop = [w_ for w_ in out.winners if w_.scope == "population"]
    assert len(pop) == 1 and pop[0].arm == "W2" and pop[0].look_no == 1
    assert pop[0].p_pos >= 0.995 and pop[0].p_best >= 0.95 and pop[0].harm.passes
    looks = {(l.arm, l.scope): l for l in out.looks}
    assert looks[("W2", "population")].passed and not looks[("W3", "population")].passed
    promo = out.promotions[("welcome_message", "population")]
    d = promo.dist("default", "welcome_message")
    # 3,000 connected a day: h = 5% gives SE 0.076 > 0.075, so h = 10%
    assert set(d) == {"W1", "W2"} and d["W1"] == pytest.approx(0.10)


def test_looks_are_not_repeated_until_the_count_doubles():
    w, cfg, exps = _world(_strong)
    out, _ = _evaluate(w, cfg, exps, 8)
    again, _ = _evaluate(w, cfg, exps, 8, looks=out.looks)
    assert again.looks == () and again.winners == ()


def test_null_arms_do_not_win():
    w, cfg, exps = _world(lambda *a: 1.0, seed=2)
    out, _ = _evaluate(w, cfg, exps, 8)
    assert out.winners == ()
    assert out.looks and all(not l.passed for l in out.looks)


def test_a_peaked_then_decaying_arm_fails_the_recency_check():
    def decaying(dim, o, cell, days):     # +30% for two days of exposure, then -10%
        if (dim, o) != ("welcome_message", "W2"):
            return 1.0
        return 1.30 if days < 2.0 else 0.90
    w, cfg, exps = _world(decaying, days=4, seed=3)
    out, _ = _evaluate(w, cfg, exps, 8)
    d = {l["scope"]: l for l in out.detail if l["arm"] == "W2"}["population"]
    assert d["w1"] >= 0.995                # the stationary evidence alone would declare it
    assert not d["w3"] and not d["passed"] and out.winners == ()


def test_a_decay_alarm_blocks_a_winner():
    w, cfg, exps = _world(_strong)
    alarm = DecayMonitorState(wf="wf_test", scope="population", dim="welcome_message", arm="W2",
                              cusum=4.5, seg_last=7, theta_ref=0.18, alarm=True)
    out, _ = _evaluate(w, cfg, exps, 8, decay=[alarm])
    assert not any(x.arm == "W2" for x in out.winners)


def test_a_clearly_worse_arm_is_futile_at_its_look():
    def bad(dim, o, cell, d):
        return 0.85 if (dim, o) == ("welcome_message", "W3") else 1.0
    w, cfg, exps = _world(bad, seed=4)
    out, _ = _evaluate(w, cfg, exps, 8)
    assert any(arm == "W3" and scope == "population" for _, arm, scope in out.futile)


def test_promotion_config_for_a_cohort_scope_touches_only_that_cohort():
    from tests.support.t3_world import cohorts_by_age
    cfg = base_config(SPEC, cohorts_by_age(SPEC))
    p = promotion_config(cfg, "voice", "V3", "V1", "age46p~000001", 0.2)
    assert dict(p.dist("age46p~000001", "voice")) == pytest.approx({"V3": 0.8, "V1": 0.2})
    assert dict(p.dist("default", "voice")) == {"V1": 1.0}
