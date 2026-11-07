"""The connect expectation behind the guardrail forecast (DESIGN.md 5.8, 5.10), on hand-made rollups.

Two cells share one workflow: cell 1 connects at 0.4 in the morning hour and 0.1 in the evening,
cell 2 the other way round, and an hour table moves cell 1's dials. The forecasts below are the
5.8 formula worked by hand, so the module cannot drift from it silently.
"""
from __future__ import annotations

import numpy as np
import pytest

from controller.learning import engagement_plan as ep
from controller.learning.connects import ConnectModel
from controller.settings import Thresholds
from controller.snapshot import build_snapshot, engage_from_columns
from controller.types import (ActivePopulation, Cell, CohortDef, FrozenDict, PatchRequest,
                              PickupArrays, freeze)
from tests.support.t3_world import base_config, make_spec

T = Thresholds()
HOURS = (9, 10)
BIG = 100_000


def _spec(primary: str = "pickup_rate"):
    return make_spec(primary=primary, window=(9, 11), dials_per_hour=100)


CELLS = {1: Cell(1, "en", "t1", "18-30", "low"), 2: Cell(2, "hi", "t1", "18-30", "low")}
COHORT = CohortDef.make("en~000001", {"lang": ["en"]})


def _pickup() -> PickupArrays:
    """Enough dials per (cell, hour) that the 200-dial prior moves nothing visibly."""
    rows = [(1, 9, 0.4), (1, 10, 0.1), (2, 9, 0.1), (2, 10, 0.4)]
    return PickupArrays(np.full(4, -1), np.array([h for _, h, _ in rows]),
                        np.array([c for c, _, _ in rows]), np.ones(4, dtype=np.int64),
                        np.full(4, BIG), np.array([int(BIG * p) for *_, p in rows]))


def _engage(rate1: float, rate2: float):
    n = 10_000
    return engage_from_columns({"seg": [0, 0], "cell": [1, 2], "config_version": [1, 1],
                                "prompt": [0, 0], "w": [0, 0], "v": [0, 0], "connected": [n, n],
                                "engaged": [int(n * rate1), int(n * rate2)], "flags": [0, 0]})


def _ws(spec, live, rates=(0.6, 0.6), did_state=None):
    active = ActivePopulation(cells=np.array([1, 2]), leads=np.array([500.0, 500.0]),
                              mix=np.array([0.5, 0.5]), cohort_of_cell=FrozenDict())
    return build_snapshot(spec=spec, policy=None, live=live, live_version=1, now=720,
                          engage=_engage(*rates), pickup=_pickup(), cells=CELLS, active=active,
                          did_state=did_state or {}, end_minute=1440 * 8, run_id="t")


def _live(spec):
    return base_config(spec, [COHORT])


def test_equal_weights_split_capacity_by_eligible_leads():
    spec = _spec()
    m = ConnectModel(_ws(spec, _live(spec)), T)
    assert m.dials(_live(spec))[1] == pytest.approx(100.0)        # 50 in each of two hours
    # connects: 50 x 0.4 + 50 x 0.1 = 25 for each cell
    assert m.connects(_live(spec))[1] == pytest.approx(25.0, rel=1e-2)
    assert m.pickup(_live(spec))[1] == pytest.approx(0.25, rel=1e-2)


def test_an_hour_table_moves_dials_to_the_cells_best_hour():
    spec = _spec()
    live = _live(spec)
    m = ConnectModel(_ws(spec, live), T)
    new = live.with_hour_weights(COHORT.id, {9: 3.0, 10: 1.0})
    # hour 9: q_1 = 3 / (3 + 1) = 0.75; hour 10: q_1 = 1 / (1 + 1) = 0.5
    assert m.dials(new)[1] == pytest.approx(125.0)
    assert m.dials(new)[2] == pytest.approx(75.0)
    # connects_1 = 75 x 0.4 + 50 x 0.1 = 35; connects_2 = 25 x 0.1 + 50 x 0.4 = 22.5
    assert m.connects(new)[1] == pytest.approx(35.0, rel=1e-2)
    assert m.connects(new)[2] == pytest.approx(22.5, rel=1e-2)


def test_a_missing_hour_gets_weight_zero():
    spec = _spec()
    live = _live(spec)
    m = ConnectModel(_ws(spec, live), T)
    new = live.with_hour_weights(COHORT.id, {9: 1.0})
    assert m.dials(new)[1] == pytest.approx(50.0)      # none of the 100 dials of hour 10
    assert m.dials(new)[2] == pytest.approx(150.0)


def test_the_did_factor_scales_connects_by_the_ratio_of_mixes():
    from controller.types import DidState
    spec = _spec()
    live = _live(spec).with_allocation("default", "caller_id", {"D1": 1.0})
    states = {"D1": DidState("wf_test", "D1", "healthy", r_hat=1.0),
              "D2": DidState("wf_test", "D2", "healthy", r_hat=0.5)}
    m = ConnectModel(_ws(spec, live, did_state=states), T)
    new = live.with_allocation("default", "caller_id", {"D1": 0.5, "D2": 0.5})
    assert m.connects(new)[1] == pytest.approx(0.75 * m.connects(live)[1])
    assert m.dials(new)[1] == pytest.approx(m.dials(live)[1])


def _table(hours) -> PatchRequest:
    return PatchRequest(wf="wf_test", owner="learning.schedule", path=("hour_weights", COHORT.id),
                        value=FrozenDict(hours), cls=3, p0_rank=None, evidence=freeze({}))


def test_a_table_that_shifts_connects_to_a_low_engagement_cell_is_refused_on_any_workflow():
    """AUDIT-1 finding 3: pickup primary, engagement guardrail. The table raises cell 1's connects
    (35 vs 25) and lowers cell 2's (22.5 vs 25); cell 1 engages at 0.3, cell 2 at 0.7, so the raw
    rate falls from 0.5 to (35 x 0.3 + 22.5 x 0.7) / 57.5 = 0.457, a 4.3-point predicted drop."""
    spec = _spec("pickup_rate")
    assert spec.objective.guardrail_kpi == "engagement_rate"
    ws = _ws(spec, _live(spec), rates=(0.3, 0.7))
    f = ep.filter_by_guardrail(ws, [_table({9: 3.0, 10: 1.0})], T)
    assert f.keep == ()
    assert f.dropped[0][0] == ("hour_weights", COHORT.id)
    assert f.dropped[0][1] == pytest.approx(0.043, abs=2e-3)


def test_an_incident_class_change_is_never_refused_by_the_forecast():
    """AUDIT-2 F2: the same breaching change as above, but incident class (a drain off a flagged
    DID): kept, the breach only recorded, and it still counts in the running config."""
    from dataclasses import replace
    spec = _spec("pickup_rate")
    ws = _ws(spec, _live(spec), rates=(0.3, 0.7))
    drain = replace(_table({9: 3.0, 10: 1.0}), cls=0, owner="learning.did")
    f = ep.filter_by_guardrail(ws, [drain], T)
    assert [r.path for r in f.keep] == [("hour_weights", COHORT.id)]
    assert f.dropped == () and f.exempt[0][0] == ("hour_weights", COHORT.id)
    assert f.exempt[0][1] == pytest.approx(0.043, abs=2e-3)


def test_the_same_table_passes_when_it_shifts_connects_to_a_high_engagement_cell():
    spec = _spec("pickup_rate")
    ws = _ws(spec, _live(spec), rates=(0.7, 0.3))
    f = ep.filter_by_guardrail(ws, [_table({9: 3.0, 10: 1.0})], T)
    assert [r.path for r in f.keep] == [("hour_weights", COHORT.id)]
    assert f.dropped == ()
    assert f.forecast.drop_mean < 0


def test_a_cohort_list_rides_only_with_a_surviving_table():
    spec = _spec("pickup_rate")
    bare = _live(spec).with_cohorts(())
    cohorts = PatchRequest(wf="wf_test", owner="cohorts.grid", path=("cohorts",),
                           value=(COHORT.body(),), cls=3, p0_rank=None, evidence=freeze({}))
    ws = _ws(spec, bare, rates=(0.3, 0.7))
    assert ep.filter_by_guardrail(ws, [cohorts, _table({9: 3.0, 10: 1.0})], T).keep == ()
    ws = _ws(spec, bare, rates=(0.7, 0.3))
    kept = ep.filter_by_guardrail(ws, [cohorts, _table({9: 3.0, 10: 1.0})], T).keep
    assert [r.path[0] for r in kept] == ["cohorts", "hour_weights"]


def test_a_pickup_guardrail_is_forecast_from_dials_and_pickup():
    """Engagement primary, pickup guardrail: the table that raises pickup passes."""
    spec = _spec("engagement_rate")
    assert spec.objective.guardrail_kpi == "pickup_rate"
    ws = _ws(spec, _live(spec))
    new = _live(spec).with_hour_weights(COHORT.id, {9: 3.0, 10: 1.0})
    g = ep.forecast(ws, None, _live(spec), new, T)
    # current 50 / 200 = 0.25; new (35 + 22.5) / 200 = 0.2875
    assert g.current == pytest.approx(0.25, rel=1e-2)
    assert g.proposed == pytest.approx(0.2875, rel=1e-2)
    assert g.drop_mean < 0 and not ep.forecast_breaches(g, T)


def test_the_gate_holds_the_lines_it_is_given():
    spec = _spec("pickup_rate")
    live = _live(spec)
    ws = _ws(spec, live, rates=(0.3, 0.7))
    new = live.with_hour_weights(COHORT.id, {9: 3.0, 10: 1.0})
    v = ep.run_gate(ws, None, live, new, T, T.alpha_h, 1, None)
    assert "guardrail" in v.report.failed
    assert v.guard is not None and v.guard.drop_mean > T.guardrail_drop_tol
