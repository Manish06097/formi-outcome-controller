"""The step rule in the real loop (DESIGN.md 4.2, 15.1 M2): while a synthetic pending-approval
marker is set, every step that a decision could fall into is at most S_watch_max - tick, no
overnight jump happens before approver close + tick, and with a local APPROVED marker there is
no overnight jump at all and a stop lands on apply_by - tick.

The markers (CTL_SYNTHETIC_MARKERS) are test-only: the step rule treats them exactly like a
pending or approved proposal of the store (settings.EnvSettings.synthetic_markers).
"""
from __future__ import annotations

import pytest

from controller.simclient import SimClient
from controller.world.clock import may_decide_between, s_watch_max
from tests.e2e.platform.helpers import SHORT, latest_schema, query, run_controller

pytestmark = [pytest.mark.e2e, pytest.mark.pg]

D = 1440
PENDING = ("nw_renewal", 9 * 60, D + 12 * 60)          # D0 09:00 to D1 12:00
APPROVED = ("ks_cancel", D + 18 * 60, D + 21 * 60 + 30)  # D1 18:00, apply_by D1 21:30


def test_markers_bound_steps_and_block_jumps(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    c = SimClient(sim.url)
    tick = c.state().tick_minutes
    gov = c.workflow(PENDING[0]).governance
    hours = gov.approver_hours
    bound = s_watch_max(tick, int(gov.apply_window_hours * 60)) - tick
    markers = (f"pending:{PENDING[0]}:{PENDING[1]}:{PENDING[2]},"
               f"approved:{APPROVED[0]}:{APPROVED[1]}:{APPROVED[2]}")
    # A base step above the bound, so the bound is what limits the step (with the default 60
    # it never binds in hours).
    base = 180
    run = run_controller(sim.url, db_dsn, tmp_path / "controller.log",
                         {"CTL_SYNTHETIC_MARKERS": markers, "CTL_T_BASE_STEP_MIN": str(base)})
    assert run.exit_codes == [0], run.log[-2000:]
    steps = query(db_dsn, latest_schema(db_dsn),
                  "SELECT from_minute, to_minute FROM loop_step ORDER BY step_no")

    checked = 0
    for a, b in steps:
        if PENDING[1] <= a < PENDING[2] and may_decide_between(a - tick + 1, a + base, hours):
            assert b - a <= bound, (a, b)
            checked += 1
    assert checked >= 8
    # after the marker ends, in-hours steps are longer than the bound again
    assert any(b - a > bound for a, b in steps if PENDING[2] <= a < D + hours[1] * 60)
    # the evening of D0: the last stop before the overnight jump is at or after close + tick
    close = hours[1] * 60
    jump_from = [a for a, b in steps if PENDING[1] <= a < D and b - a > 4 * 60]
    assert jump_from and min(jump_from) >= close + tick
    # D1 evening: approved marker -> a stop at apply_by - tick, no jump before apply_by
    stops = {b for _, b in steps}
    assert APPROVED[2] - tick in stops
    for a, b in steps:
        if APPROVED[1] <= a < APPROVED[2]:
            assert b <= APPROVED[2], (a, b)
    # without markers the same window jumps from 21:00 (sanity: markers changed the plan)
    assert (D + 21 * 60, D + 21 * 60 + tick) in steps
