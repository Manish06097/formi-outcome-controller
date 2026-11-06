"""Per-cohort call-time weights (DESIGN.md 5.8): shrinkage, the weight formula, every in-window
hour positive, refresh threshold, ownership, no-history rule, credible-band verification."""
from __future__ import annotations

import numpy as np
import pytest

from controller.cohorts.grid import GridInputs, build_grid
from controller.learning.schedule import (HourCurvesModel, hour_weights_from_curve,
                                          normalised_l1, predictive_lower, verify_schedule)
from controller.settings import Thresholds
from controller.types import CohortDef
from tests.support import synth
from tests.unit.test_cohorts import HOURS, history

T = Thresholds()


def grid_snapshot(seed: int = 1, live=None, biz=()):
    cat, pk, leads = history(seed)
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, business_cohorts=tuple(biz),
                               leads_by_cell=leads))
    spec = synth.make_spec()
    return synth.snapshot(spec, cohorts=gs, pickup=pk, live=live), gs


def test_weight_formula_hand_computed():
    curve = {9: 0.2, 10: 0.4}
    w = hour_weights_from_curve(curve, (9, 10), T)
    # p / pbar = (2/3, 4/3); squared = (4/9, 16/9); mean = 10/9 -> (0.4, 1.6)
    assert w == {9: 0.4, 10: 1.6}
    clipped = hour_weights_from_curve({9: 0.01, 10: 0.5, 11: 0.5}, (9, 10, 11), T)
    assert clipped[9] == T.hour_weight_min


@pytest.mark.parametrize("seed", range(40))
def test_every_in_window_hour_gets_a_positive_weight(seed):
    """Random curves, including zero-pickup and missing hours: no in-window hour is ever 0."""
    rng = np.random.default_rng(seed)
    hours = tuple(range(int(rng.integers(6, 10)), int(rng.integers(17, 23))))
    curve = {h: float(rng.choice([0.0, rng.uniform(0, 0.6)])) for h in hours
             if rng.random() > 0.2}
    w = hour_weights_from_curve(curve, hours, T)
    assert set(w) == set(hours)
    assert all(T.hour_weight_min <= v <= T.hour_weight_max for v in w.values())


def test_curves_are_shrunk_toward_the_pooled_curve():
    ws, gs = grid_snapshot()
    curves = HourCurvesModel().fit(ws)
    assert set(curves.curves) == {*gs.keys(), "default"}
    k = gs.entries[0].key
    d = curves.dials[k][9]
    # the cohort's raw first-attempt rate is pulled toward the pooled rate by 200 / (d + 200)
    pk = ws.pickup
    cells = gs.effective_cells()[k]
    m = np.isin(pk.cell, list(cells)) & (pk.hour == 9)
    raw = pk.connected[m].sum() / pk.dials[m].sum()
    pooled = curves.pooled[9]
    expect = (raw * d + 200 * pooled) / (d + 200)
    assert curves.curves[k][9] == pytest.approx(expect, rel=1e-9)


def test_morning_cohorts_get_morning_weight():
    ws, gs = grid_snapshot()
    model = HourCurvesModel()
    reqs = model.plan_hour_weights(ws, model.fit(ws))
    by = {r.path[1]: r for r in reqs}
    young = next(e.key for e in gs.entries if dict(e.definition.where)["age_band"] == ("18-30",))
    assert young in by
    w = by[young].value
    assert w[19] > 1.0 > w[9]
    for r in reqs:
        assert r.cls == 3 and r.p0_rank is None and r.path[0] == "hour_weights"
        assert set(r.value) == set(HOURS) and min(r.value.values()) > 0
        assert r.evidence["needs_cohort_defined"] is True


def test_refresh_only_when_the_table_moves_more_than_l1_0_1():
    ws, gs = grid_snapshot()
    model = HourCurvesModel()
    curves = model.fit(ws)
    reqs = model.plan_hour_weights(ws, curves)
    live = synth.config(hour_weights={r.path[1]: dict(r.value) for r in reqs},
                        cohorts=gs.to_config())
    ws2 = synth.snapshot(ws.spec, cohorts=gs, pickup=ws.pickup, live=live)
    assert model.plan_hour_weights(ws2, curves) == []
    assert normalised_l1({9: 1, 10: 1}, None, (9, 10)) == 0.0
    assert normalised_l1({9: 1}, None, (9, 10)) == pytest.approx(1.0)


def test_business_cohorts_and_default_are_left_alone():
    biz = [CohortDef.make("vip", {"value_band": ["high"]})]
    ws, gs = grid_snapshot(biz=biz)
    model = HourCurvesModel()
    reqs = model.plan_hour_weights(ws, model.fit(ws))
    paths = {r.path[1] for r in reqs}
    assert "vip" not in paths and "default" not in paths
    assert paths <= set(gs.keys())


def test_no_history_needs_three_dialling_days():
    ws, gs = grid_snapshot()
    pk = ws.pickup
    own_rows = [(d, int(h), int(c), int(b), int(n), int(k))
                for d0, h, c, b, n, k in zip(pk.sim_day, pk.hour, pk.cell, pk.attempt_bucket,
                                             pk.dials, pk.connected)
                for d in [int(d0) + 14] if d < 2]
    two_days = synth.pickup_arrays(own_rows)
    ws2 = synth.snapshot(ws.spec, cohorts=gs, pickup=two_days)
    model = HourCurvesModel()
    assert model.plan_hour_weights(ws2, model.fit(ws2)) == []
    three = synth.pickup_arrays([(d, h, c, b, n, k) for d, h, c, b, n, k in
                                 [(r[0] + 1, *r[1:]) for r in own_rows] + own_rows])
    ws3 = synth.snapshot(ws.spec, cohorts=gs, pickup=three)
    assert model.enough_data(ws3)


def test_predictive_band_and_verification():
    assert predictive_lower(100, 0.25, 1e6) == pytest.approx(17, abs=1)  # binomial 2.5% point
    ws, gs = grid_snapshot()
    model = HourCurvesModel()
    curves = model.fit(ws)
    # data after the change = a fresh draw from the same world: verified
    cat, pk_new, _ = history(seed=99, days=1)
    rows = list(zip(pk_new.sim_day + 1, pk_new.hour, pk_new.cell, pk_new.attempt_bucket,
                    pk_new.dials, pk_new.connected))
    allrows = list(zip(ws.pickup.sim_day, ws.pickup.hour, ws.pickup.cell,
                       ws.pickup.attempt_bucket, ws.pickup.dials, ws.pickup.connected)) + rows
    ws_ok = synth.snapshot(ws.spec, cohorts=gs, pickup=synth.pickup_arrays(allrows))
    f = verify_schedule(ws_ok, curves, since_day=0)[0]
    assert f.kind == "verified"
    # pickup collapses after the change: fails
    bad = [(d, h, c, b, n, int(k * 0.5)) for d, h, c, b, n, k in rows]
    ws_bad = synth.snapshot(ws.spec, cohorts=gs, pickup=synth.pickup_arrays(
        [r for r in allrows if r[0] < 0] + bad))
    f2 = verify_schedule(ws_bad, curves, since_day=0)[0]
    assert f2.kind == "verify_failed" and f2.stat > T.schedule_verify_fail_frac
