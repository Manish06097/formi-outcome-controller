"""Run identity (DESIGN.md 8.5, 15.1 M1 (3)): restarting only the controller resumes the same run
schema; restarting the simulator (a new world with the same fingerprint) creates a new one.
"""
from __future__ import annotations

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import SHORT, done_advances, query, registry, run_controller

pytestmark = [pytest.mark.e2e, pytest.mark.pg]


def _contiguous(keys: list[str]) -> bool:
    spans = [tuple(int(x) for x in k.removeprefix("adv:").split("->")) for k in keys]
    return all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_controller_restart_resumes_simulator_restart_starts_fresh(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "controller.log"
    stop5 = {"CTL_STOP_AFTER_STEPS": "5"}

    first = run_controller(sim.url, db_dsn, log, stop5)
    assert first.exit_codes == [1]
    (schema, fp, status, _), = registry(db_dsn)
    assert status == "active" and len(done_advances(db_dsn, schema)) == 5

    second = run_controller(sim.url, db_dsn, log, stop5)
    assert second.exit_codes == [1]
    assert [r[0] for r in registry(db_dsn)] == [schema], "a controller restart resumes"
    keys = done_advances(db_dsn, schema)
    assert len(keys) == 10 and _contiguous(keys) and keys[0].startswith("adv:0->")
    assert f"run {schema} resumed" in second.log

    # The simulator restarts: same scenario, same fingerprint, but its clock and ids are behind.
    sim2 = sim_factory(overrides=SHORT, name="restarted")
    third = run_controller(sim2.url, db_dsn, log)
    assert third.exit_codes == [0], third.log[-2000:]
    rows = registry(db_dsn)
    assert [r[0] for r in rows][0] == schema and len(rows) == 2
    assert rows[0][1] == rows[1][1] == fp, "same world identity by fingerprint"
    assert rows[0][2] == "abandoned" and rows[1][2] == "done"
    new = rows[1][0]
    assert new != schema and new.rsplit("_", 1)[1] == "2"
    st = SimClient(sim2.url).state()
    assert query(db_dsn, new, "SELECT sum(events) FROM ingested_chunk") == [(st.last_event_id,)]
    alerts = query(db_dsn, new, "SELECT code, message FROM alert")
    assert alerts and alerts[0][0] == "NEW_WORLD" and schema in alerts[0][1]
    # the old world's data is kept for audit
    assert len(done_advances(db_dsn, schema)) == 10
