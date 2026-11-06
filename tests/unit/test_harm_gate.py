"""The harm gate (DESIGN.md 0.1, 5.4): population, cohort, caller-ID and guardrail lines."""
from __future__ import annotations

import numpy as np
import pytest

from controller.learning.engagement import BetaBinomialModel
from controller.learning.harm import (GateContext, harm_gate, largest_passing,
                                      uniform_exploration)
from controller.types import CohortDef, DidPosterior, FrozenDict, GuardrailForecast
from tests.support.t3_world import base_config, make_policy, make_spec, uniform_world

SPEC = make_spec()
ARMS = {"welcome_message": ("W1", ["W2", "W3"]), "voice": ("V1", ["V2", "V3", "V4"])}


def _small_cohorts(world, n):
    keys = sorted({(c.lang, c.tier, c.age) for c in world.cells.values()})[:n]
    return [CohortDef.make(f"c{i}~000000", {"lang": [l], "city_tier": [t], "age_band": [a]})
            for i, (l, t, a) in enumerate(keys)]


def _first_share(n_cohorts: int, line: float, seed: int = 7) -> float:
    """Largest equal share for the 5 untested arms in every cohort that passes the gate."""
    w = uniform_world()
    cohorts = _small_cohorts(w, n_cohorts)
    cfg = base_config(SPEC, cohorts)
    ws = w.snapshot(cfg, 0, policy=make_policy(line))
    model = BetaBinomialModel()
    ctx = GateContext(model.draws(model.fit(ws, "ew"), seed), ws.active, ws.cells)
    pol = make_policy(line)
    def passes(s: float) -> bool:
        new = cfg
        for k in [c.id for c in cohorts] + ["default"]:
            new = uniform_exploration(new, ARMS, s, k)
        return ctx.evaluate(cfg, new, pol).report.passes
    return largest_passing(passes, 12, hi=0.15)


def test_first_exploration_is_about_3_8_percent_per_arm():
    """5 untested arms at tau 0.06: 3.8% on the linearised gate; exp(theta) adds a small
    Jensen term (+tau^2/2 per arm) and 4,000 draws a Monte Carlo error of about 2%."""
    shares = [_first_share(0, 0.02, seed) for seed in (1, 2, 3)]
    assert np.mean(shares) == pytest.approx(0.038, abs=0.002)


def test_cohort_line_of_two_percent_does_not_shrink_the_first_exploration():
    pop = np.mean([_first_share(0, 0.02, s) for s in (1, 2, 3)])
    two = np.mean([_first_share(19, 0.02, s) for s in (1, 2, 3)])
    assert two == pytest.approx(pop, rel=0.05)


def test_cohort_line_of_one_percent_halves_the_first_exploration():
    pop = np.mean([_first_share(0, 0.02, s) for s in (1, 2, 3)])
    one = np.mean([_first_share(19, 0.01, s) for s in (1, 2, 3)])
    assert one / pop == pytest.approx(0.5, abs=0.06)
    assert one == pytest.approx(0.019, abs=0.002)


def _ctx_and_cfg(n_cohorts=0):
    w = uniform_world()
    cfg = base_config(SPEC, _small_cohorts(w, n_cohorts))
    ws = w.snapshot(cfg, 0)
    m = BetaBinomialModel()
    return GateContext(m.draws(m.fit(ws, "ew"), 5), ws.active, ws.cells), cfg, ws


def test_a_twenty_percent_arm_on_untested_options_fails_the_population_line():
    ctx, cfg, _ = _ctx_and_cfg()
    new = uniform_exploration(cfg, {"voice": ("V1", ["V2"])}, 0.20)
    r = ctx.evaluate(cfg, new, make_policy()).report
    assert not r.passes and "population" in r.failed
    assert r.p_de_below_1pct > 0.025


def test_no_change_passes_with_zero_tails():
    ctx, cfg, _ = _ctx_and_cfg(3)
    r = ctx.evaluate(cfg, cfg, make_policy()).report
    assert r.passes and r.de_mean == 0.0 and r.p_de_below_1pct == 0.0
    assert all(v == (0.0, 0.0) for v in r.de_k.values())


def test_a_cohort_only_change_is_judged_by_its_own_cohort_line():
    ctx, cfg, ws = _ctx_and_cfg(5)
    k = cfg.cohort_ids()[0]
    new = uniform_exploration(cfg, ARMS, 0.08, cohort=k)   # far above 1.9% for one cohort
    r = ctx.evaluate(cfg, new, make_policy(0.01)).report
    assert f"cohort:{k}" in r.failed and "population" not in r.failed
    assert set(r.de_k) >= {k, "default"} and r.de_k["default"] == (0.0, 0.0)


def test_caller_id_change_without_a_did_posterior_fails_closed():
    ctx, cfg, _ = _ctx_and_cfg()
    new = cfg.with_allocation("default", "caller_id", {"D1": 0.5, "D2": 0.5})
    r = ctx.evaluate(cfg, new, make_policy()).report
    assert not r.passes and "did_unmodeled" in r.failed


def test_caller_id_line_mirrors_the_grader():
    """10% onto a 0.55-health DID is about -3.6% DID health: harmful (UNDERSTANDING 5)."""
    ctx, cfg, ws = _ctx_and_cfg()
    cfg = cfg.with_allocation("default", "caller_id", {"D1": 0.5, "D2": 0.5})
    draws = np.tile(np.array([[1.0, 1.0, 0.55 / 0.95 * 1.0]]), (4000, 1))
    did = DidPosterior(wf="wf_test", dids=("D1", "D2", "D3"), draws=draws,
                       status=FrozenDict({"D1": "healthy", "D2": "healthy", "D3": "unknown"}))
    probe = cfg.with_allocation("default", "caller_id", {"D1": 0.45, "D2": 0.45, "D3": 0.10})
    r = ctx.evaluate(cfg, probe, make_policy(), did_post=did).report
    assert r.dd_mean == pytest.approx(0.1 * (0.55 / 0.95 - 1.0), abs=1e-9)
    assert not r.passes and "did" in r.failed
    small = cfg.with_allocation("default", "caller_id", {"D1": 0.485, "D2": 0.485, "D3": 0.03})
    assert ctx.evaluate(cfg, small, make_policy(), did_post=did).report.passes


def test_guardrail_forecast_is_part_of_the_gate():
    ctx, cfg, _ = _ctx_and_cfg()
    bad = GuardrailForecast(kpi="engagement_rate", current=0.5, proposed=0.49, max_drop_abs=0.02,
                            drop_mean=0.01, p_breach=0.0)
    r = ctx.evaluate(cfg, cfg, make_policy(), guard=bad).report
    assert "guardrail" in r.failed


def test_harm_gate_function_matches_the_context():
    ctx, cfg, ws = _ctx_and_cfg(2)
    new = uniform_exploration(cfg, ARMS, 0.03)
    a = harm_gate(cfg, new, ctx.cache.draws, None, None, ws.active, make_policy(), 0.025, 0.05,
                  cells=ws.cells)
    b = ctx.evaluate(cfg, new, make_policy()).report
    assert a == b
