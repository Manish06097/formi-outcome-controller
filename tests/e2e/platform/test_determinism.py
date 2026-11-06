"""test_determinism (DESIGN.md 8.6, 10.1, 15.1 M2): two clean runs of public_01 with the no-op loop
give identical store contents.

Both runs go into one database (the second gets schema `_2`, since the first is done), and every
table of the two run schemas is compared row for row, except the wall-clock columns listed in
`helpers.WALL_COLUMNS` (ingest and step timings, intent and audit wall times, RSS, DB size) and the
heartbeat row. Decision rows, intents, audit rows, facts, rollups and the catalog must match.
"""
from __future__ import annotations

import pytest

from tests.e2e.platform.helpers import diff_dumps, dump_schema, registry, run_controller

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]


def test_two_clean_runs_identical_store(sim_factory, db_dsn, tmp_path):
    for i in range(2):
        sim = sim_factory(name=f"run{i}")
        run = run_controller(sim.url, db_dsn, tmp_path / f"controller{i}.log")
        assert run.exit_codes == [0], run.log[-2000:]
    rows = registry(db_dsn)
    assert [r[2] for r in rows] == ["done", "done"] and rows[0][1] == rows[1][1]
    a, b = rows[0][0], rows[1][0]
    dump_a, dump_b = dump_schema(db_dsn, a), dump_schema(db_dsn, b)
    assert set(dump_a) == set(dump_b) and len(dump_a) > 40
    assert sum(len(v) for v in dump_a.values()) > 100_000
    assert diff_dumps(dump_a, dump_b) == []
