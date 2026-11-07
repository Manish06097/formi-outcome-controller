"""Engagement decisions replay from what the audit store keeps (DESIGN.md 9.6, ADR 0031), and
the sparse-strata variance fix of step I4 (stats/ratios.py).

The recomputers get only what `posterior_snapshot` and `decision` rows hold: the encoded
snapshot, the fitted mean and covariance, the seed and the decision's scalar inputs. Each must
reproduce exactly the outcome the loop's own call computed.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.audit.replay import (ReplayContext, SnapshotRecord, compare, decode_inputs,
                                     encode_inputs, find_recomputer, hash_inputs)
from controller.learning import engagement_plan as ep
from controller.learning import replay_engagement  # noqa: F401  (registers the recomputers)
from controller.learning.allocator import open_experiments
from controller.learning.ws_codec import decode_snapshot, encode_snapshot
from controller.settings import Thresholds
from controller.stats.ratios import mantel_haenszel
from tests.support.t3_world import base_config, cohorts_by_age, make_spec, uniform_world

T = Thresholds()
SPEC = make_spec()


# ============================================================================== variance fix

def test_sparse_strata_do_not_make_the_log_ratio_look_certain():
    """Reproduces the step I4 defect: an arm seen once per stratum (a few percent of a small
    cohort, one call per segment) had a delta-method variance of zero, because each stratum's
    a (1 - a / n1) is 0 when n1 = 1. Ten such strata (8 of 10 engaged) vs a large control."""
    k = 10
    a = np.array([1.0] * 8 + [0.0] * 2)
    n1 = np.ones(k)
    c = np.full(k, 55.0)
    n0 = np.full(k, 100.0)
    r = mantel_haenszel(a, n1, c, n0)
    q1, q0 = 8.5 / 11, 550.5 / 1001       # half-event smoothed rates of each side
    floor = (1 - q1) / (10 * q1) + (1 - q0) / (1000 * q0)
    assert r.var >= floor - 1e-12
    assert r.var > 0.025           # the old estimator gave about 0.0008


def test_one_stratum_variance_is_the_unequal_size_formula():
    r = mantel_haenszel(np.array([60.0]), np.array([100.0]), np.array([500.0]),
                        np.array([1000.0]))
    want = (1 - 0.6) / (100 * 0.6) + (1 - 0.5) / (1000 * 0.5)
    q1, q0 = 60.5 / 101, 500.5 / 1001
    floor = (1 - q1) / (100 * q1) + (1 - q0) / (1000 * q0)
    assert math.isclose(r.var, max(want, floor), rel_tol=1e-12)
    assert abs(r.var - want) / want < 0.01      # the floor is a small-sample guard only
    assert math.isclose(r.log_rr, math.log(0.6 / 0.5), rel_tol=1e-12)


def test_greenland_robins_on_hand_computed_strata():
    a, n1 = np.array([6.0, 3.0]), np.array([10.0, 5.0])
    c, n0 = np.array([40.0, 20.0]), np.array([100.0, 50.0])
    N = n1 + n0
    A = (a * n0 / N).sum()
    C = (c * n1 / N).sum()
    gr = ((n1 * n0 * (a + c) / N - a * c) / N).sum() / (A * C)
    r = mantel_haenszel(a, n1, c, n0)
    q1, q0 = 9.5 / 16, 60.5 / 151
    floor = (1 - q1) / (15 * q1) + (1 - q0) / (150 * q0)
    assert math.isclose(r.var, max(gr, floor), rel_tol=1e-12)


# ============================================================================== replay

def _mult(dim, o, cell, days):
    return {("welcome_message", "W2"): 1.12,
            ("voice", "V3"): 1.2 if cell.age == "60+" else 0.9}.get((dim, o), 1.0)


def _snapshot():
    w = uniform_world(seed=7, connected_per_day=3000, mult=_mult)
    cfg = base_config(SPEC, cohorts_by_age(SPEC))
    exps = open_experiments(w.snapshot(cfg, 0))
    cfg = cfg.with_allocation("default", "welcome_message", {"W1": 0.8, "W2": 0.1, "W3": 0.1})
    cfg = cfg.with_allocation("default", "voice", {"V1": 0.7, "V2": 0.1, "V3": 0.1, "V4": 0.1})
    for s in range(8):
        w.simulate_segment(cfg, s)
    return w.snapshot(cfg, 8, experiments=exps)


def _records(ws, fits, extra):
    arrays = encode_snapshot(ws, {**extra, "model": fits.model})
    blob = encode_inputs(arrays)
    out = []
    for i, kind in enumerate(sorted(fits.post)):
        p = fits.post[kind]
        out.append(SnapshotRecord(snapshot_id=i + 1, wf=1, kind=kind,
                                  inputs=decode_inputs(blob), inputs_hash=hash_inputs(arrays),
                                  mean=p.mean, cov=p.cov, summary={}, seed=fits.seeds[kind],
                                  code_version="dev", settings_hash=""))
    return tuple(out)


def _ctx(module, rule, inputs, snaps):
    return ReplayContext(decision_id=1, module=module, kind="x", rule_id=rule, sim_minute=0,
                         wf=1, inputs_json=inputs, seed=0, snapshots=snaps, refit=True)


def _plain(x):
    from controller.loop.engagement import _plain as p
    return p(x)


def test_the_snapshot_codec_round_trips_what_the_model_reads():
    ws = _snapshot()
    back = decode_snapshot(decode_inputs(encode_inputs(encode_snapshot(ws))))
    for kind in ("stationary", "ew"):
        a = ep.model_named("beta_binomial", T).fit(ws, kind)
        b = ep.model_named("beta_binomial", T).fit(back, kind)
        assert np.array_equal(a.mean, b.mean) and np.array_equal(a.cov, b.cov)
    assert back.live == ws.live and back.experiments == ws.experiments
    assert back.policy == ws.policy and back.now == ws.now


@pytest.mark.parametrize("model", ["beta_binomial", "ridge_logit"])
def test_allocation_and_looks_replay_exactly(model):
    ws = _snapshot()
    fits = ep.fit(ws, model, T, "plan")
    ta = ws.now + 120
    plan = ep.run_allocation(ws, fits, T, ta)
    stored = _plain({**ep.allocation_outcome(plan), "action": plan.kind})
    snaps = _records(ws, fits, {"purpose": "plan", "t_apply": ta})
    fn = find_recomputer("learning.allocator", "5.4/plan_allocation")
    ctx = _ctx("learning.allocator", "5.4/plan_allocation",
               {"t_apply": ta, "in_freeze": False, "ramp_stop": False}, snaps)
    got = _plain(fn(ctx))
    assert not ctx.diffs
    assert compare(stored, got["outcome"], "outcome") == []
    looks = ep.run_looks(ws, fits, T, ta)
    fn = find_recomputer("learning.winners", "5.5/evaluate_looks")
    ctx = _ctx("learning.winners", "5.5/evaluate_looks", {"t_apply": ta}, snaps)
    assert compare(_plain(ep.looks_outcome(looks)), _plain(fn(ctx))["outcome"]) == []


def test_the_composed_gate_replays_exactly():
    ws = _snapshot()
    fits = ep.fit(ws, "beta_binomial", T, "plan", kinds=("ew",))
    cur = ws.live
    new = cur.with_allocation("default", "welcome_message", {"W1": 0.6, "W2": 0.3, "W3": 0.1})
    v = ep.run_gate(ws, fits.ew, cur, new, T, T.alpha_h, ep.seed_for(ws, "gate", "did"))
    snaps = _records(ws, fits, {"purpose": "gate", "cur": cur.stored(),
                                "new": new.as_stored().stored(), "alpha": T.alpha_h})
    fn = find_recomputer("learning.harm", "0.1/harm_gate")
    got = _plain(fn(_ctx("learning.harm", "0.1/harm_gate", {}, snaps)))
    assert compare(_plain(v.outcome()), got["outcome"]) == []
    stored = _plain({k: getattr(v.report, k) for k in vars(v.report)})
    assert compare(stored, got["harm_report"]) == []


def test_a_changed_input_is_a_diff_not_a_silent_pass():
    ws = _snapshot()
    fits = ep.fit(ws, "beta_binomial", T, "plan")
    ta = ws.now + 120
    plan = ep.run_allocation(ws, fits, T, ta)
    stored = _plain({**ep.allocation_outcome(plan), "action": plan.kind})
    snaps = _records(ws, fits, {"purpose": "plan", "t_apply": ta})
    fn = find_recomputer("learning.allocator", "5.4/plan_allocation")
    other = _ctx("learning.allocator", "5.4/plan_allocation",
                 {"t_apply": ta + 6 * 60 * 24, "in_freeze": False, "ramp_stop": True}, snaps)
    assert compare(stored, _plain(fn(other))["outcome"], "outcome") != []


def test_a_gate_over_a_pickup_lever_replays_from_the_stored_pickup_sums():
    """The guardrail forecast of a schedule change reads r_pickup: the snapshot keeps it summed
    per (cell, hour), and the replayed forecast equals the one the day-level rows gave."""
    import dataclasses

    from controller.types import PickupArrays
    ws = _snapshot()
    cells = sorted(ws.cells)
    rows = [(d, h, c, 1 + (i % 3), 40 + i % 7, 10 + (i * c) % 9)
            for i, (d, h, c) in enumerate((d, h, c) for d in (-2, -1, 0) for h in range(9, 21)
                                          for c in cells)]
    pick = PickupArrays(*[np.array(x, dtype=np.int64) for x in zip(*rows)])
    ws = dataclasses.replace(ws, pickup=pick)
    fits = ep.fit(ws, "beta_binomial", T, "plan", kinds=("ew",))
    cur = ws.live
    cohort = cur.cohort_ids()[0]
    new = cur.with_hour_weights(cohort, {h: 1.0 + (h % 2) for h in range(9, 21)}).with_allocation(
        "default", "welcome_message", {"W1": 0.6, "W2": 0.3, "W3": 0.1})
    v = ep.run_gate(ws, fits.ew, cur, new, T, T.alpha_h, ep.seed_for(ws, "gate", "did"), ws.now + 60)
    arrays = encode_snapshot(ws, {"purpose": "gate", "cur": cur.stored(), "alpha": T.alpha_h,
                                  "new": new.as_stored().stored(), "t_apply": ws.now + 60,
                                  "model": fits.model}, with_pickup=True)
    snaps = (SnapshotRecord(snapshot_id=1, wf=1, kind="ew", inputs=decode_inputs(encode_inputs(arrays)),
                            inputs_hash=hash_inputs(arrays), mean=fits.post["ew"].mean,
                            cov=fits.post["ew"].cov, summary={}, seed=fits.seeds["ew"],
                            code_version="dev", settings_hash=""),)
    fn = find_recomputer("learning.harm", "0.1/harm_gate")
    got = _plain(fn(_ctx("learning.harm", "0.1/harm_gate", {}, snaps)))
    assert compare(_plain(v.outcome()), got["outcome"]) == []
    stored = _plain({k: getattr(v.report, k) for k in vars(v.report)})
    assert compare(stored, got["harm_report"]) == []
    assert v.guard is not None and v.guard.kpi == "pickup_rate"
