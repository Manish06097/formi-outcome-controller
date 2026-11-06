"""Same data and seed give the same decision (DESIGN.md 8.6, constraint 4, ADR 0004).

Every learning decision is a pure function of (snapshot, settings, seed). These tests rebuild
the inputs from scratch twice and require bit-identical outputs, for both engagement models and
for every decision the learning loop makes (allocation, winners, decay, rollback, harm stop),
and an identical change log for a whole 6-day loop.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from controller.learning.allocator import harm_stops, plan_allocation_detail
from controller.learning.decay import PageDecayMonitor
from controller.learning.engagement import BetaBinomialModel, RidgeLogitModel
from controller.learning.winners import evaluate_looks
from controller.settings import Thresholds
from controller.stats.draws import derive_seed
from tests.support.t3_loop import run_loop
from tests.support.t3_world import base_config, cohorts_by_age, make_cells, make_spec, uniform_world

SPEC = make_spec()


def _mult(dim, o, cell, days):
    return {("welcome_message", "W2"): 1.15 if days < 2 else 0.95,
            ("voice", "V3"): 1.2 if cell.age == "60+" else 0.9}.get((dim, o), 1.0)


def _snapshot():
    """A fresh world and snapshot every call: nothing is shared between the two runs."""
    w = uniform_world(seed=42, connected_per_day=3000, mult=_mult)
    cfg = base_config(SPEC, cohorts_by_age(SPEC))
    from controller.learning.allocator import open_experiments
    exps = open_experiments(w.snapshot(cfg, 0))
    cfg = cfg.with_allocation("default", "welcome_message", {"W1": 0.6, "W2": 0.3, "W3": 0.1})
    cfg = cfg.with_allocation("default", "voice", {"V1": 0.5, "V2": 0.1, "V3": 0.3, "V4": 0.1})
    for s in range(8):
        w.simulate_segment(cfg, s)
    return w.snapshot(cfg, 8, experiments=exps)


@pytest.mark.parametrize("model_cls", [BetaBinomialModel, RidgeLogitModel])
def test_every_learning_decision_is_reproducible(model_cls):
    def decide():
        ws = _snapshot()
        m = model_cls()
        seed_s = derive_seed(ws.run_id, ws.spec.workflow_id, 1234, "stationary")
        seed_e = derive_seed(ws.run_id, ws.spec.workflow_id, 1234, "ew")
        stat = m.draws(m.fit(ws, "stationary"), seed_s)
        ew = m.draws(m.fit(ws, "ew"), seed_e)
        plan = plan_allocation_detail(ws, ew, decision_id=1234)
        looks = evaluate_looks(ws, stat, ew)
        mon = PageDecayMonitor()
        for seg in range(3, 8):
            mon.update(ws, seg, stat)
        rb = mon.rollback_plan(ws, ew, ws.now + 120, decision_id=1234)
        stops = harm_stops(ws, ew, decision_id=1234)
        return stat, ew, plan, looks, mon.states(), rb, stops
    a, b = decide(), decide()
    assert np.array_equal(a[0].params, b[0].params) and np.array_equal(a[1].params, b[1].params)
    assert a[2].proposed == b[2].proposed and a[2].patches == b[2].patches
    assert a[2].lambdas == b[2].lambdas and a[2].harm == b[2].harm
    assert a[3].looks == b[3].looks and a[3].winners == b[3].winners
    assert a[3].detail == b[3].detail
    assert a[4] == b[4] and a[5] == b[5] and a[6] == b[6]


def test_a_different_seed_changes_the_draws_but_the_decision_record_says_which():
    ws = _snapshot()
    m = BetaBinomialModel()
    post = m.fit(ws, "ew")
    d1, d2 = m.draws(post, 1), m.draws(post, 2)
    assert d1.seed == 1 and d2.seed == 2
    assert not np.array_equal(d1.params, d2.params)


def test_a_whole_learning_loop_replays_identically():
    t = dataclasses.replace(Thresholds(), n_draws=1000)
    cells = make_cells(langs=("en", "hi"), tiers=("t1",), ages=("18-30", "60+"), values=("low",))
    def run():
        w = uniform_world(seed=9, connected_per_day=2000, mult=_mult, cells=cells)
        r = run_loop(w, base_config(SPEC, cohorts_by_age(SPEC)), 6, seed=3, thresholds=t)
        return [(c.pos_applied, c.kind, c.verdict, c.de) for c in r.changes], r.final
    (log_a, cfg_a), (log_b, cfg_b) = run(), run()
    assert log_a and log_a == log_b and cfg_a == cfg_b
