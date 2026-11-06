"""Decay monitor (Page CUSUM per arm and scope) and rollback timing (DESIGN.md 5.7)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.learning.allocator import open_experiments
from controller.learning.decay import PageDecayMonitor
from controller.learning.engagement import BetaBinomialModel
from controller.snapshot import segment_start_minute
from controller.types import DecayMonitorState, P1_ROLLBACK
from tests.support.t3_world import base_config, make_spec, uniform_world

SPEC = make_spec()
M = BetaBinomialModel()


def _decaying(onset_days=2.0, over_days=2.0, peak=1.20, to=0.85):
    def mult(dim, o, cell, days):
        if (dim, o) != ("welcome_message", "W2"):
            return 1.0
        if days <= onset_days:
            return peak
        f = min((days - onset_days) / over_days, 1.0)
        return peak + (to - peak) * f
    return mult


def _run(mult, segments, seed=1, per_day=3000, share=0.25):
    w = uniform_world(seed=seed, connected_per_day=per_day, mult=mult)
    cfg0 = base_config(SPEC)
    exps = open_experiments(w.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "welcome_message",
                               {"W1": 1 - share, "W2": share})
    mon = PageDecayMonitor()
    alarm_seg = None
    for s in range(segments):
        w.simulate_segment(cfg, s)
        ws = w.snapshot(cfg, s + 1, experiments=exps)
        stat = M.draws(M.fit(ws, "stationary"), 5) if s == 2 else None
        if s >= 2:
            f = mon.update(ws, s, stat)
            if f and alarm_seg is None:
                alarm_seg = s
    return w, cfg, exps, mon, alarm_seg


def test_a_decaying_arm_raises_an_alarm_after_onset_and_a_stable_one_does_not():
    _, _, _, mon, alarm = _run(_decaying(), 16)
    assert alarm is not None and 4 <= alarm <= 9          # onset at segment 4
    st = {(s.scope, s.arm): s for s in mon.states()}[("population", "W2")]
    # theta_ref is the shrunk stationary posterior mean at the start (true log 1.2 = 0.18)
    assert st.alarm and 0.05 < st.theta_ref < math.log(1.2) + 0.05
    _, _, _, calm, none = _run(lambda *a: 1.0, 16, seed=2)
    assert none is None and not any(s.alarm for s in calm.states())


def test_update_is_idempotent_and_resumes_from_persisted_state():
    w, cfg, exps, mon, _ = _run(_decaying(), 8)
    ws = w.snapshot(cfg, 8, experiments=exps)
    before = mon.states()
    assert mon.update(ws, 7) == [] and mon.states() == before   # segment 7 already folded
    resumed = PageDecayMonitor(states=before)
    w.simulate_segment(cfg, 8)
    ws9 = w.snapshot(cfg, 9, experiments=exps)
    resumed.update(ws9, 8)
    mon.update(ws9, 8)
    assert resumed.states() == mon.states()


def _alarmed_state(theta_ref=0.18, seg_last=0):
    return DecayMonitorState(wf="wf_test", scope="population", dim="welcome_message", arm="W2",
                             cusum=4.2, seg_last=seg_last, theta_ref=theta_ref, alarm=True)


def _world_with_data(mult, segments, seed=3, share=0.25):
    w = uniform_world(seed=seed, connected_per_day=6000, mult=mult)
    cfg0 = base_config(SPEC)
    exps = open_experiments(w.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "welcome_message", {"W1": 1 - share, "W2": share})
    for s in range(segments):
        w.simulate_segment(cfg, s)
    return w, cfg, exps


def test_no_rollback_at_onset_while_the_arm_is_still_at_full_strength():
    w, cfg, exps = _world_with_data(_decaying(onset_days=3.0), 6)      # still at the peak
    ws = w.snapshot(cfg, 6, experiments=exps)
    mon = PageDecayMonitor(states=[_alarmed_state(seg_last=5)])
    ew = M.draws(M.fit(ws, "ew"), 21)
    assert mon.rollback_plan(ws, ew, ws.now) == []


def test_rollback_once_the_arm_is_below_the_control():
    w, cfg, exps = _world_with_data(_decaying(onset_days=1.0, over_days=1.0, to=0.85), 10)
    ws = w.snapshot(cfg, 10, experiments=exps)
    mon = PageDecayMonitor(states=[_alarmed_state(seg_last=9)])
    ew = M.draws(M.fit(ws, "ew"), 22)
    plan = mon.rollback_plan(ws, ew, ws.now, decision_id=7)
    assert plan and all(p.cls == P1_ROLLBACK and p.decision_id == 7 for p in plan)
    new = dict(plan[0].value)
    assert new.get("W2", 0.0) < 0.25 and new["W1"] > 0.75
    info = plan[0].evidence["rollbacks"][0]
    assert info["theta_ta_mean"] < 0 and info["de_mean"] > 0
    assert info["p_de_below_1pct"] <= 0.10


def test_rollback_is_timed_to_the_predicted_apply_time():
    """An arm still slightly above the control but falling fast: proposing for an apply now
    would be graded harmful, proposing for an apply a day later is beneficial. The plan rolls
    back only for the later apply time."""
    def falling(dim, o, cell, days):
        if (dim, o) != ("welcome_message", "W2"):
            return 1.0
        return max(0.70, 1.25 - 0.10 * days)          # crosses 1.0 at 2.5 days of exposure
    w, cfg, exps = _world_with_data(falling, 4, seed=5)
    ws = w.snapshot(cfg, 4, experiments=exps)
    mon = PageDecayMonitor(states=[_alarmed_state(seg_last=3)])
    ew = M.draws(M.fit(ws, "ew"), 23)
    assert mon.rollback_plan(ws, ew, ws.now) == []
    later = segment_start_minute(8, SPEC.dialling_window) + 30
    plan = mon.rollback_plan(ws, ew, later)
    assert plan
    info = plan[0].evidence["rollbacks"][0]
    assert info["slope"] < 0 and info["delta_segments"] > 3
    assert w.grade(cfg, cfg.with_allocation("default", "welcome_message", {"W1": 1.0}), 8.0)[0] \
        != "harmful"
