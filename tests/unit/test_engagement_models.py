"""Both EngagementModel implementations (DESIGN.md 5.3, 17.2) on hand-built and synthetic data."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.learning.engagement import (BetaBinomialModel, EffectCache, RidgeLogitModel,
                                            dim_arms, make_engagement_model)
from controller.settings import EnvSettings, Settings
from controller.snapshot import build_snapshot, engage_from_columns
from controller.stats.boundaries import posterior_sd
from controller.types import Cell, CohortDef, EngagementModel
from tests.support.t3_world import (base_config, cohorts_by_age, make_cells, make_policy,
                                    make_spec, uniform_world)

SPEC = make_spec()


def _two_arm_snapshot(n_arm: int, n_ctrl: int, p_arm: float, p_ctrl: float, cohorts=()):
    cells = {0: Cell(0, "en", "t1", "31-45", "low")}
    cols = {"seg": [0, 0], "cell": [0, 0], "w": [1, 0], "v": [0, 0],
            "connected": [n_arm, n_ctrl],
            "engaged": [round(p_arm * n_arm), round(p_ctrl * n_ctrl)]}
    cfg = base_config(SPEC, cohorts)
    return build_snapshot(spec=SPEC, policy=make_policy(), live=cfg, live_version=1, now=700,
                          engage=engage_from_columns(cols), cells=cells)


def _pop_sd(post, dim="welcome_message", arm="W2"):
    i = post.index.names.index(f"{dim}|{arm}|population")
    return math.sqrt(post.cov[i, i]), post.mean[i]


def test_both_models_satisfy_the_engagement_model_protocol():
    assert isinstance(BetaBinomialModel(), EngagementModel)
    assert isinstance(RidgeLogitModel(), EngagementModel)
    assert make_engagement_model(Settings()).name == "beta_binomial"
    ridge = Settings(env=EnvSettings(engagement_model="ridge_logit"))
    assert make_engagement_model(ridge).name == "ridge_logit"


@pytest.mark.parametrize("n,expected", [(36, 0.056), (150, 0.047), (500, 0.034)])
def test_beta_binomial_posterior_sd_after_n_arm_calls(n, expected):
    """5.4: sd = 1 / sqrt(1/tau^2 + n / 0.82) with the control much larger than the arm."""
    ws = _two_arm_snapshot(n, 200_000, 0.55, 0.55)
    sd, _ = _pop_sd(BetaBinomialModel().fit(ws, "stationary"))
    assert sd == pytest.approx(posterior_sd(n, 0.06, 0.45 / 0.55), abs=0.0005)
    assert sd == pytest.approx(expected, abs=0.0008)


def test_beta_binomial_prior_population_sd_is_tau_and_cohort_sd_is_about_twice():
    w = uniform_world()
    cfg = base_config(SPEC, [CohortDef.make("en_t1_young~1", {"lang": ["en"], "city_tier": ["t1"],
                                                              "age_band": ["18-30"]})])
    post = BetaBinomialModel().fit(w.snapshot(cfg, 0), "ew")
    sd, mean = _pop_sd(post)
    assert sd == pytest.approx(0.06) and mean == 0.0
    d = BetaBinomialModel().draws(post, 3, 20000)
    small = [c for c, cell in w.cells.items() if cell.lang == "en" and cell.tier == "t1"
             and cell.age == "18-30"]
    eff = EffectCache(d).scope_effect("welcome_message", "W2", small, np.ones(len(small)))
    # a 1/36 cohort: sd = sqrt(tau^2 + 3 tau^2 (1 - w^2 / sum w^2)) ~ 0.12
    assert eff.std() == pytest.approx(0.12, abs=0.004)


def test_models_recover_planted_effects_and_ridge_finds_the_age_flip():
    def mult(dim, o, cell, days):
        if dim == "welcome_message" and o == "W2":
            return 1.10
        if dim == "voice" and o == "V3":
            return {"60+": 1.22, "18-30": 0.88}.get(cell.age, 1.0)
        return 1.0
    w = uniform_world(seed=5, connected_per_day=6000, mult=mult)
    cfg = base_config(SPEC, cohorts_by_age(SPEC))
    cfg = cfg.with_allocation("default", "welcome_message", {"W1": .5, "W2": .25, "W3": .25})
    cfg = cfg.with_allocation("default", "voice", {"V1": .4, "V2": .2, "V3": .2, "V4": .2})
    for s in range(8):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 8)
    ids = sorted(w.cells)
    old = [c for c in ids if w.cells[c].age == "60+"]
    young = [c for c in ids if w.cells[c].age == "18-30"]
    for model in (BetaBinomialModel(), RidgeLogitModel()):
        cache = EffectCache(model.draws(model.fit(ws, "stationary"), 11, 4000))
        w2 = cache.scope_effect("welcome_message", "W2", ids, np.ones(len(ids)))
        assert abs(w2.mean() - math.log(1.10)) < 2.5 * w2.std(), model.name
        w3 = cache.scope_effect("welcome_message", "W3", ids, np.ones(len(ids)))
        assert abs(w3.mean()) < 2.5 * w3.std(), model.name
    ridge = EffectCache(RidgeLogitModel().draws(RidgeLogitModel().fit(ws, "stationary"), 11))
    v_old = ridge.scope_effect("voice", "V3", old, np.ones(len(old)))
    v_young = ridge.scope_effect("voice", "V3", young, np.ones(len(young)))
    assert v_old.mean() > 0.1 and v_young.mean() < -0.05


@pytest.mark.parametrize("model_cls", [BetaBinomialModel, RidgeLogitModel])
def test_effect_cache_equals_the_m0_draws_contract(model_cls):
    w = uniform_world(seed=2, connected_per_day=2000)
    cfg = base_config(SPEC, cohorts_by_age(SPEC)).with_allocation(
        "default", "voice", {"V1": .7, "V2": .1, "V3": .1, "V4": .1})
    for s in range(4):
        w.simulate_segment(cfg, s)
    m = model_cls()
    d = m.draws(m.fit(w.snapshot(cfg, 4), "ew"), 9, 500)
    cache = EffectCache(d)
    for arm in ("V2", "V3", "V4"):
        assert np.allclose(cache.effects("voice", arm), d.cell_effects("voice", arm))


@pytest.mark.parametrize("model_cls", [BetaBinomialModel, RidgeLogitModel])
def test_fit_and_draws_are_deterministic(model_cls):
    w = uniform_world(seed=4, connected_per_day=2000)
    cfg = base_config(SPEC).with_allocation("default", "welcome_message",
                                            {"W1": .8, "W2": .1, "W3": .1})
    for s in range(4):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 4)
    m = model_cls()
    a, b = m.fit(ws, "ew"), m.fit(ws, "ew")
    assert a.inputs_hash == b.inputs_hash
    assert np.array_equal(a.mean, b.mean) and np.array_equal(a.cov, b.cov)
    assert np.array_equal(m.draws(a, 77).params, m.draws(b, 77).params)
    assert not np.array_equal(m.draws(a, 77).params, m.draws(a, 78).params)


def test_beta_binomial_strata_absorb_a_prompt_shock_during_a_ramp():
    """A supervisor prompt edit lowers everyone's engagement while the (null) arm ramps up.
    Pooling would blame the arm; the (segment, prompt) strata do not."""
    rows = {k: [] for k in ("seg", "cell", "prompt", "w", "v", "connected", "engaged")}
    for seg, share, prompt, rate in ((0, 0.05, 0, 0.60), (1, 0.05, 0, 0.60),
                                     (2, 0.40, 1, 0.45), (3, 0.40, 1, 0.45)):
        for w_code, n in ((1, int(4000 * share)), (0, int(4000 * (1 - share)))):
            for key, val in (("seg", seg), ("cell", 0), ("prompt", prompt), ("w", w_code),
                             ("v", 0), ("connected", n), ("engaged", round(rate * n))):
                rows[key].append(val)
    ws = build_snapshot(spec=SPEC, policy=make_policy(), live=base_config(SPEC),
                        live_version=1, now=4 * 720, engage=engage_from_columns(rows),
                        cells={0: Cell(0, "en", "t1", "31-45", "low")})
    sd, mean = _pop_sd(BetaBinomialModel().fit(ws, "stationary"))
    assert abs(mean) < 0.005
    arm = np.array(rows["w"]) == 1
    n, g = np.array(rows["connected"]), np.array(rows["engaged"])
    pooled = math.log((g[arm].sum() / n[arm].sum()) / (g[~arm].sum() / n[~arm].sum()))
    assert pooled < -0.12          # a fake 14% loss


def test_ew_fit_discounts_old_segments():
    def mult(dim, o, cell, days):     # W2 strong for its first day, then null
        return (1.25 if days < 1.0 else 1.0) if (dim, o) == ("welcome_message", "W2") else 1.0
    w = uniform_world(seed=8, connected_per_day=4000, mult=mult)
    cfg = base_config(SPEC).with_allocation("default", "welcome_message",
                                            {"W1": .6, "W2": .2, "W3": .2})
    for s in range(8):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 8)
    m = BetaBinomialModel()
    _, stat = _pop_sd(m.fit(ws, "stationary"))
    _, ew = _pop_sd(m.fit(ws, "ew"))
    assert stat > 0.03 and ew < 0.5 * stat


def test_empirical_bayes_widens_only_the_ew_prior():
    def mult(dim, o, cell, days):
        return {("voice", "V2"): 1.3, ("voice", "V3"): 0.75, ("voice", "V4"): 1.25}.get((dim, o), 1.0)
    w = uniform_world(seed=3, connected_per_day=8000, mult=mult)
    cfg = base_config(SPEC).with_allocation("default", "voice",
                                            {"V1": .4, "V2": .2, "V3": .2, "V4": .2})
    for s in range(6):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 6)
    m = BetaBinomialModel()
    assert m.fit(ws, "stationary").summary["tau"] == pytest.approx(0.06)
    ew = m.fit(ws, "ew").summary
    assert ew["tau_eb"] > 0.15 and ew["tau"] == pytest.approx(ew["tau_eb"])


def test_control_is_the_experiment_control_else_the_incumbent():
    cfg = base_config(SPEC, control_w="W2")
    ws = build_snapshot(spec=SPEC, policy=make_policy(), live=cfg, live_version=1, now=0)
    a = dim_arms(ws, "welcome_message")
    assert a.control == "W2" and a.arms == ("W1", "W3")
