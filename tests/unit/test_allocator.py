"""Harm-gated probability matching (DESIGN.md 5.4)."""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from controller.settings import Thresholds

from controller.learning.allocator import (harm_stops, open_experiments, plan_allocation_detail,
                                           plan_allocations, project_shares)
from controller.learning.engagement import BetaBinomialModel
from controller.types import DecayMonitorState, P1_ROLLBACK, P3_ROUTINE
from tests.support.t3_world import (base_config, cohorts_by_age, make_policy, make_spec,
                                    uniform_world)

SPEC = make_spec()
MODEL = BetaBinomialModel()


def _draws(ws, seed=1, kind="ew"):
    return MODEL.draws(MODEL.fit(ws, kind), seed)


def test_project_shares_respects_floors_ceilings_and_sums_to_one():
    x = project_shares({"c": 1e-6, "a": 1.0, "b": 0.3}, {"c": 0.02, "a": 0.02, "b": 0.0},
                       {"c": 1.0, "a": 0.5, "b": 0.0})
    assert x["a"] == pytest.approx(0.5) and x["b"] == 0.0 and x["c"] == pytest.approx(0.5)
    y = project_shares({"c": 0.2, "a": 0.5, "b": 0.3}, {"c": 0.02, "a": 0.02, "b": 0.02},
                       {"c": 1.0, "a": 0.5, "b": 0.5})
    assert sum(y.values()) == pytest.approx(1.0) and y == pytest.approx({"c": 0.2, "a": 0.5,
                                                                         "b": 0.3})


def test_launch_is_an_exploration_that_passes_the_gate_with_small_equal_arms():
    w = uniform_world()
    cfg = base_config(SPEC, cohorts_by_age(SPEC))
    ws = w.snapshot(cfg, 0)
    plan = plan_allocation_detail(ws, _draws(ws))
    assert plan.kind == "exploration" and plan.harm.passes
    assert [e.dim for e in plan.new_experiments] == ["welcome_message", "voice"]
    assert plan.new_experiments[0].control == "W1"
    for k in ("default", "age46p~000001"):
        d = plan.proposed.dist(k, "welcome_message")
        assert 0.03 < d["W2"] < 0.07 and d["W2"] == pytest.approx(d["W3"], abs=0.01)
        v = plan.proposed.dist(k, "voice")
        assert 0.025 < v["V2"] < 0.06
    assert all(p.cls == P3_ROUTINE and p.path[0] == "alloc" for p in plan.patches)
    assert plan.harm.p_de_below_1pct <= 0.025


def test_no_engagement_experiments_on_pickup_primary_workflows():
    spec = make_spec(primary="pickup_rate")
    w = uniform_world(spec=spec)
    ws = w.snapshot(base_config(spec), 0)
    assert open_experiments(ws) == ()
    assert plan_allocations(ws, _draws(ws)) == []


def _ramped_world(seed=1, days=3, mult=None):
    def default_mult(dim, o, cell, d):
        return {("welcome_message", "W2"): 1.15, ("welcome_message", "W3"): 0.85}.get((dim, o), 1.0)
    w = uniform_world(seed=seed, connected_per_day=3000, mult=mult or default_mult)
    cfg = base_config(SPEC).with_allocation("default", "welcome_message",
                                            {"W1": 0.7, "W2": 0.15, "W3": 0.15})
    ws0 = w.snapshot(base_config(SPEC), 0)
    exps = open_experiments(ws0)
    for s in range(2 * days):
        w.simulate_segment(cfg, s)
    return w, cfg, exps


def _clean_monitor(arm: str, first: int = 2, last: int = 5, alarm: bool = False):
    return DecayMonitorState(wf="wf_test", scope="population", dim="welcome_message", arm=arm,
                             cusum=0.5, seg_last=last, theta_ref=0.1, alarm=alarm, seg_first=first)


def test_plan_moves_toward_the_better_arm_and_removes_the_bad_one():
    w, cfg, exps = _ramped_world()
    ws = w.snapshot(cfg, 6, experiments=exps, decay=[_clean_monitor("W2"), _clean_monitor("W3")])
    plan = plan_allocation_detail(ws, _draws(ws))
    d = plan.proposed.dist("default", "welcome_message")
    assert d["W2"] > 0.15 and d["W2"] <= 0.5 + 1e-9
    assert d.get("W3", 0.0) < 0.15
    assert ("population", "welcome_message", "W3") in plan.removed
    assert plan.harm.passes


def test_same_data_and_seed_give_the_same_plan():
    w, cfg, exps = _ramped_world(seed=2)
    ws = w.snapshot(cfg, 6, experiments=exps)
    a = plan_allocation_detail(ws, _draws(ws, seed=99))
    b = plan_allocation_detail(ws, _draws(ws, seed=99))
    assert a.proposed == b.proposed and a.patches == b.patches and a.lambdas == b.lambdas


def test_an_arm_the_decay_monitor_has_not_seen_cannot_rise_above_the_monitoring_floor():
    """ADR 0034 (ENG-GAP): the better arm W2 is held at the monitoring floor until its monitor
    has folded two segments; with them it ramps. One folded segment is not enough."""
    w, cfg, exps = _ramped_world()
    t2 = dataclasses.replace(Thresholds(), decay_clean_segments=2)
    floor = t2.decay_arm_share
    unseen = w.snapshot(cfg, 6, experiments=exps)
    one = w.snapshot(cfg, 6, experiments=exps, decay=[_clean_monitor("W2", first=5, last=5)])
    seen = w.snapshot(cfg, 6, experiments=exps, decay=[_clean_monitor("W2")])
    for ws in (unseen, one):
        d = plan_allocation_detail(ws, _draws(ws), settings=t2).proposed.dist("default", "welcome_message")
        assert d["W2"] <= max(floor, 0.15) + 1e-9
    d = plan_allocation_detail(seen, _draws(seen), settings=t2).proposed.dist("default", "welcome_message")
    assert d["W2"] > 0.15


def test_a_decay_alarm_freezes_increases():
    w, cfg, exps = _ramped_world(seed=3)
    alarm = DecayMonitorState(wf="wf_test", scope="population", dim="welcome_message", arm="W2",
                              cusum=5.0, seg_last=5, theta_ref=0.1, alarm=True)
    ws = w.snapshot(cfg, 6, experiments=exps, decay=[alarm])
    plan = plan_allocation_detail(ws, _draws(ws))
    assert plan.proposed.dist("default", "welcome_message").get("W2", 0.0) <= 0.15 + 1e-9


def test_minimum_gain_skips_a_tiny_exploitation_move():
    """Every arm observed at exactly the control's rate: probability matching wants to even the
    shares out, but the expected gain is about zero, so the move is not worth a slot."""
    from controller.snapshot import build_snapshot, engage_from_columns
    w = uniform_world()
    cfg0 = base_config(SPEC)
    exps = open_experiments(w.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "welcome_message", {"W1": 0.5, "W2": 0.25, "W3": 0.25})
    cfg = cfg.with_allocation("default", "voice", {"V1": 0.4, "V2": 0.2, "V3": 0.2, "V4": 0.2})
    rows = {k: [] for k in ("seg", "cell", "w", "v", "connected", "engaged")}
    for seg in range(6):
        for wi, pw in enumerate((0.5, 0.25, 0.25)):
            for vi, pv in enumerate((0.4, 0.2, 0.2, 0.2)):
                n = int(4000 * pw * pv)
                for key, val in (("seg", seg), ("cell", 0), ("w", wi), ("v", vi),
                                 ("connected", n), ("engaged", round(0.55 * n))):
                    rows[key].append(val)
    ws = build_snapshot(spec=SPEC, policy=make_policy(), live=cfg, live_version=2,
                        now=6 * 720, engage=engage_from_columns(rows),
                        cells={0: w.cells[0]}, experiments=exps, decay=[
                            DecayMonitorState(wf="wf_test", scope="population", dim=d, arm=o,
                                              cusum=0.0, seg_last=5, theta_ref=0.0, alarm=False,
                                              seg_first=2)
                            for d, o in (("welcome_message", "W2"), ("welcome_message", "W3"),
                                         ("voice", "V2"), ("voice", "V3"), ("voice", "V4"))])
    plan = plan_allocation_detail(ws, _draws(ws))
    assert plan.kind == "skipped_min_gain" and plan.patches == ()
    assert abs(plan.expected_gain) < 0.0025


def test_harm_stop_moves_a_clearly_bad_arm_to_the_control():
    def mult(dim, o, cell, d):
        return 0.75 if (dim, o) == ("voice", "V2") else 1.0
    w = uniform_world(seed=5, connected_per_day=4000, mult=mult)
    cfg0 = base_config(SPEC)
    exps = open_experiments(w.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "voice", {"V1": 0.7, "V2": 0.2, "V3": 0.05, "V4": 0.05})
    for s in range(4):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 4, experiments=exps)
    stops = harm_stops(ws, _draws(ws))
    assert stops and all(p.cls == P1_ROLLBACK for p in stops)
    new = dict(stops[0].value)
    assert "V2" not in new and new["V1"] == pytest.approx(0.9, abs=1e-4)
    assert stops[0].evidence["kind"] == "harm_stop"


def test_tv_cap_limits_the_move_when_routed_to_auto_apply():
    w, cfg, exps = _ramped_world(seed=6)
    ws = w.snapshot(cfg, 6, experiments=exps)
    plan = plan_allocation_detail(ws, _draws(ws), max_tv=0.05)
    cur = cfg.dist("default", "welcome_message")
    new = plan.proposed.dist("default", "welcome_message")
    tv = 0.5 * sum(abs(cur.get(o, 0) - new.get(o, 0)) for o in set(cur) | set(new))
    assert tv <= 0.05 + 1e-3
