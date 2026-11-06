"""The integration migrations of step I1 (0009 to 0012, ADR 0028) upgrade a run schema that was
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
            # two config versions: v2 refreshed by the M0-era loop (audit marker), v3 not yet
            conn.execute("INSERT INTO workflow VALUES (1, 'wf_a', 't', '{}', 'h', 0)")
            conn.execute("INSERT INTO config_event (event_id, wf, version, actor, minute) "
                         "VALUES (10, 1, 2, 'supervisor', 615), (20, 1, 3, 'supervisor', 700)")
            conn.execute("INSERT INTO audit_log (sim_minute, actor, action, subject_type, "
                         "subject_id, summary) VALUES (630, 'controller', 'config_refreshed', "
                         "'config_version', 'wf_a:2', 'wf_a v2')")
            # T5's M0-era encodings: "[subject] " message prefix, release suffix on the reason
            conn.execute("INSERT INTO alert (severity, code, wf, message, runbook) VALUES "
                         "('page', 'GOV_INVARIANT', 1, '[p-17] 403 on auto-apply', 'r'), "
                         "('page', 'NEW_WORLD', NULL, 'fresh schema', 'r')")
            conn.execute("INSERT INTO hold (wf, set_by, reason, from_minute, released_minute) "
                         "VALUES (1, 'Asha', 'board review [released by Ravi at D3 16:00]', "
                         "900, 960), (1, 'Asha', 'audit', 900, NULL)")
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
        # 0011: the old audit marker is carried over; v3 is still due for REFRESH
        assert conn.execute("SELECT event_id, refreshed_minute FROM config_event "
                            "ORDER BY 1").fetchall() == [(10, 630), (20, None)]
        # 0012: subjects and releasers move into their columns, the text is kept as written
        assert conn.execute("SELECT code, dedupe_key, message FROM alert ORDER BY alert_id"
                            ).fetchall() == [("GOV_INVARIANT", "p-17", "403 on auto-apply"),
                                             ("NEW_WORLD", "", "fresh schema")]
        assert conn.execute("SELECT reason, released_by FROM hold ORDER BY hold_id"
                            ).fetchall() == [("board review", "Ravi"), ("audit", None)]
        # 0009: existing proposals stay valid under the class / p0_rank CHECK
        assert conn.execute("SELECT proposal_id, p0_rank FROM proposal").fetchall() == [("p3", None)]
