"""The integration migrations of step I1 (0009 onwards, ADR 0028) upgrade a run schema that was
created with the M0 set (0001 to 0008) without losing rows, exactly as a resumed controller
applies them (`Database.open` runs `migrate_run_schema`)."""
from __future__ import annotations

import uuid

import pytest

from controller.store.db import Database, apply_migrations, load_migrations

pytestmark = pytest.mark.pg

M0_LAST = "0008"


@pytest.fixture()
def db(pg_dsn):
    d = Database(pg_dsn)
    d.ensure_ctl()
    created: list[str] = []
    yield d, created
    d.close()
    for s in created:
        d.drop_run_schema(s)


def _m0_schema(d: Database, created: list[str]) -> str:
    """A run schema exactly as an M0-era controller left it: migrations up to 0008 only."""
    schema = f"run_{uuid.uuid4().hex[:12]}_1"
    m0 = [m for m in load_migrations() if m.version <= M0_LAST]
    with d.connect() as conn:
        with conn.transaction():
            apply_migrations(conn, "run", schema, m0)
            conn.execute(f"SET LOCAL search_path TO {schema}")
            conn.execute("INSERT INTO flag_monitor (wf, prompt, p0, p1, h, stat, flags_since_reset,"
                         " calls, last_event_id, alarm, reset_minute) "
                         "VALUES (1, 0, 0.01, 0.02, 5.0, 1.5, 3, 900, 41, false, 600)")
            conn.execute("INSERT INTO proposal (proposal_id, wf, class, route, state, "
                         "base_version) VALUES ('p3', 1, 3, 'approval', 'applied', 1)")
    created.append(schema)
    with d.connect() as conn:
        conn.execute("INSERT INTO ctl.run_registry (schema_name, fingerprint, sim_url, status) "
                     "VALUES (%s, %s, 'http://x', 'active')", (schema, uuid.uuid4().hex * 2))
    return schema


def test_resume_upgrades_an_m0_schema_in_place(db):
    d, created = db
    schema = _m0_schema(d, created)
    applied = d.migrate_run_schema(schema)
    assert [n[:4] for n in applied] == [m.version for m in load_migrations()
                                         if m.scope == "run" and m.version > M0_LAST]
    assert d.migrate_run_schema(schema) == []
    with d.connect() as conn:
        conn.execute(f"SET search_path TO {schema}")
        # 0010: the flag CUSUM's progress keeps its value under its real name and type
        assert conn.execute("SELECT last_sim_hour, calls FROM flag_monitor").fetchall() == [(41, 900)]
        typ = conn.execute("SELECT data_type FROM information_schema.columns WHERE "
                           "table_schema = %s AND table_name = 'flag_monitor' AND "
                           "column_name = 'last_sim_hour'", (schema,)).fetchone()
        assert typ == ("integer",)
        # 0009: existing proposals stay valid under the class / p0_rank CHECK
        assert conn.execute("SELECT proposal_id, p0_rank FROM proposal").fetchall() == [("p3", None)]
