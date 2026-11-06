"""Segment arithmetic and the in-memory snapshot builder (controller/snapshot.py)."""
from __future__ import annotations

import numpy as np
import pytest

from controller.snapshot import (active_population, build_snapshot, engage_from_columns,
                                 ew_weights, segment_of, segment_position, segment_start_minute)
from controller.types import WorkflowSnapshot
from tests.support.t3_world import base_config, cohorts_by_age, make_cells, make_policy, make_spec

W = (9, 21)   # half-window boundary at 15:00


@pytest.mark.parametrize("minute,seg", [(9 * 60, 0), (15 * 60 - 1, 0), (15 * 60, 1),
                                        (23 * 60, 1), (1440 + 9 * 60, 2), (-1440 + 16 * 60, -1),
                                        (-2 * 1440 + 10 * 60, -4)])
def test_segment_of(minute, seg):
    assert segment_of(minute, W) == seg


def test_segment_position_counts_dialling_time_only():
    assert segment_position(9 * 60, W) == 0.0
    assert segment_position(12 * 60, W) == 0.5
    assert segment_position(15 * 60, W) == 1.0
    assert segment_position(22 * 60, W) == 2.0               # after the window: next day's start
    assert segment_position(1440 + 3 * 60, W) == 2.0         # overnight adds nothing
    assert segment_start_minute(3, W) == 1440 + 15 * 60


def test_ew_weights_halve_every_two_segments():
    w = ew_weights(np.array([10, 8, 6, 11]), 10, 2.0)
    assert w.tolist() == [1.0, 0.5, 0.25, 1.0]


def test_build_snapshot_is_a_workflow_snapshot_with_the_extension_fields():
    spec = make_spec()
    cells = make_cells()
    ws = build_snapshot(spec=spec, policy=make_policy(), live=base_config(spec), live_version=3,
                        now=700, cells=cells, end_minute=8 * 1440)
    assert type(ws) is WorkflowSnapshot
    assert segment_of(ws.now, ws.window) == 0 and len(ws.engage) == 0 and ws.cells[0] == cells[0]
    assert ws.end_minute == 8 * 1440 and ws.looks == () and ws.active is None


def test_active_population_falls_back_to_recent_connected_calls():
    spec = make_spec()
    cells = make_cells()
    cfg = base_config(spec, cohorts_by_age(spec))
    e = engage_from_columns({"seg": [0, 9, 10], "cell": [3, 3, 40], "w": [0, 0, 0],
                             "v": [0, 0, 0], "connected": [100, 7, 5], "engaged": [50, 3, 2]})
    ws = build_snapshot(spec=spec, policy=make_policy(), live=cfg, live_version=1,
                        now=5 * 1440 + 10 * 60, engage=e, cells=cells)
    pop = active_population(ws)
    assert pop.cells.tolist() == [3, 40] and pop.leads.tolist() == [7.0, 5.0]
    assert pop.cohort_of_cell[40] == cfg.cohort_of(cells[40].features())
