"""public_01 end to end with the no-op loop (DESIGN.md 15.1 M1 (1), M2): the real controller
process against a real simulator over HTTP and a real Postgres.

Asserts: exit 0 at the scenario end, zero config changes, every event ingested exactly once,
rollups equal their recomputation, every advance went through a done intent with a decision
row, and every step is a positive multiple of the tick that obeys the step rule's boundaries.
"""
from __future__ import annotations

import pytest

from controller.ingest.pipeline import rollup_mismatches
from controller.simclient import SimClient
from tests.e2e.platform.helpers import latest_schema, query, registry, run_controller

pytestmark = [pytest.mark.e2e, pytest.mark.pg]


def test_public01_no_op_loop_finishes_with_zero_changes(sim_factory, db_dsn, tmp_path):
    sim = sim_factory()
    run = run_controller(sim.url, db_dsn, tmp_path / "controller.log")
    assert run.exit_codes == [0], run.log[-3000:]

    c = SimClient(sim.url)
    st = c.state()
    assert st.finished
    for spec in c.workflows():
        assert [v.version for v in c.config_history(spec.workflow_id)] == [1]
    changes = sim.run_dir / "_truth" / "changes.jsonl"
    assert not changes.exists() or changes.read_text().strip() == ""

    (schema, _, status, last_min), = registry(db_dsn)
    assert status == "done" and last_min == st.end_minute
    events, max_id, chunks = query(db_dsn, schema, """SELECT sum(events), max(last_event_id),
                                                      count(*) FROM ingested_chunk""")[0]
    assert (events, max_id, chunks) == (st.last_event_id, st.last_event_id, st.chunks)

    import psycopg
    with psycopg.connect(db_dsn, options=f"-c search_path={schema}") as conn:
        assert set(rollup_mismatches(conn, st.tick_minutes).values()) == {0}

    steps = query(db_dsn, schema, "SELECT from_minute, to_minute FROM loop_step ORDER BY step_no")
    assert steps[0][0] == 0 and steps[-1][1] == st.end_minute
    for (a, b), (c2, _) in zip(steps, steps[1:]):
        assert b == c2, "steps are contiguous"
    assert all((b - a) > 0 and (b - a) % st.tick_minutes == 0 for a, b in steps)
    per_day = len(steps) / (st.end_minute / 1440)
    assert 10 <= per_day <= 20, f"{per_day:.1f} advances per day (about 14 expected, 4.2)"

    intents = query(db_dsn, schema, "SELECT kind, status, count(*) FROM intent GROUP BY 1, 2")
    assert intents == [("advance", "done", len(steps))]
    decisions = query(db_dsn, schema, """SELECT count(*) FROM decision
                                         WHERE module = 'loop' AND kind = 'advance'""")
    assert decisions == [(len(steps),)]
    assert query(db_dsn, schema, "SELECT code FROM alert") == []
    assert query(db_dsn, schema, "SELECT completed_minute IS NOT NULL FROM bootstrap") == [(True,)]
    assert latest_schema(db_dsn) == schema
