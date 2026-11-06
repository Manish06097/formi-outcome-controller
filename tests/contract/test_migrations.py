"""The DDL of DESIGN.md section 3 applies cleanly into fresh run schemas, and the invariants it
encodes hold (one change in flight, the API role, partitions, the advisory lock)."""
from __future__ import annotations

import uuid

import psycopg
import pytest

from controller.store.db import (AdvisoryLock, Database, MigrationError, load_migrations,
                                 normalise_sim_url)

pytestmark = pytest.mark.pg

EXPECTED_RUN_TABLES = {
    "workflow", "directive", "cell", "cohort_def", "config_version", "config_cohort",
    "stage_raw", "ingested_chunk", "campaign", "lead_dim", "call_fact", "call_fact_default",
    "config_event", "approval_event", "event_other",
    "r_engage", "r_pickup", "r_did", "r_flags",
    "bootstrap", "experiment", "experiment_arm", "winner_look", "winner", "holdout",
    "posterior_snapshot", "decay_monitor", "did_state", "drift_monitor", "flag_monitor",
    "baseline", "incident", "validation_run", "prompt_verdict", "credit_ledger",
    "credit_envelope",
    "patch", "path_owner", "cooldown", "proposal", "proposal_transition", "approval_mirror",
    "intent", "hold",
    "decision", "audit_log", "allocation_provenance", "loop_step", "alert", "clock", "heartbeat",
    "schema_migrations",
}


@pytest.fixture()
def db(pg_dsn):
    d = Database(pg_dsn)
    d.ensure_ctl()
    created: list[str] = []
    d._created = created  # type: ignore[attr-defined]
    yield d
    d.close()
    for s in created:
        d.drop_run_schema(s)


def _fp() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _tables(conn, schema: str) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s",
                        (schema,)).fetchall()
    return {r[0] for r in rows}


def test_migrations_apply_into_two_fresh_run_schemas(db):
    fp = _fp()
    s1 = db.create_run_schema(fp, "http://127.0.0.1:8700", sim_days=8)
    s2 = db.create_run_schema(fp, "http://localhost:8700/", sim_days=8)
    db._created += [s1, s2]
    assert s1 == f"run_{fp[:12]}_1" and s2 == f"run_{fp[:12]}_2"
    run_migs = [m for m in load_migrations() if m.scope == "run"]
    with db.connect() as conn:
        for s in (s1, s2):
            tables = _tables(conn, s)
            assert EXPECTED_RUN_TABLES <= tables, EXPECTED_RUN_TABLES - tables
            parts = {t for t in tables if t.startswith("call_fact_d") and t != "call_fact_default"}
            assert parts == {f"call_fact_d{d:03d}" for d in range(8)}
            n = conn.execute(f"SELECT count(*) FROM {s}.schema_migrations").fetchone()[0]
            assert n == len(run_migs)
        reg = conn.execute("SELECT schema_name, sim_url, status FROM ctl.run_registry "
                           "WHERE fingerprint = %s ORDER BY schema_name", (fp,)).fetchall()
    assert reg == [(s1, "http://127.0.0.1:8700", "active"), (s2, "http://127.0.0.1:8700", "active")]
    assert [r.schema_name for r in db.run_candidates(fp)] == [s2, s1]
    # resuming applies nothing new; ctl is idempotent
    assert db.migrate_run_schema(s1) == []
    assert db.ensure_ctl() == []


def test_checksum_mismatch_is_refused(db):
    s = db.create_run_schema(_fp(), "http://127.0.0.1:8700")
    db._created.append(s)
    with db.connect() as conn:
        conn.execute(f"UPDATE {s}.schema_migrations SET checksum = 'x' WHERE version = '0002'")
    with pytest.raises(MigrationError):
        db.migrate_run_schema(s)


def _proposal(conn, pid: str, state: str, wf: int = 1) -> None:
    conn.execute("INSERT INTO proposal (proposal_id, wf, class, route, state, base_version) "
                 "VALUES (%s, %s, 3, 'approval', %s, 1)", (pid, wf, state))


def test_one_in_flight_is_a_database_invariant(db):
    s = db.create_run_schema(_fp(), "http://127.0.0.1:8700")
    db._created.append(s)
    db.open(s)
    with db.tx() as conn:
        _proposal(conn, "p1", "pending")
        _proposal(conn, "p0", "applied")          # terminal states do not count
        _proposal(conn, "q1", "pending", wf=2)    # another workflow is independent
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.tx() as conn:
            _proposal(conn, "p2", "auto_applying")
    # supersede: mark the holder and insert the new one in the same transaction
    with db.tx() as conn:
        conn.execute("UPDATE proposal SET state = 'superseded', superseded_by = 'p3' "
                     "WHERE proposal_id = 'p1'")
        _proposal(conn, "p3", "submitting")
    with db.tx() as conn:
        rows = conn.execute("SELECT proposal_id FROM proposal WHERE wf = 1 AND state IN "
                            "('submitting','pending','approved','applying','noticed',"
                            "'auto_applying')").fetchall()
    assert rows == [("p3",)]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.tx() as conn:
            _proposal(conn, "bad", "not_a_state", wf=3)


def test_tx_rolls_back_on_error_and_partitions_route_rows(db):
    s = db.create_run_schema(_fp(), "http://127.0.0.1:8700")
    db._created.append(s)
    db.open(s)
    with pytest.raises(RuntimeError):
        with db.tx() as conn:
            conn.execute("INSERT INTO clock VALUES (1, 0, 11520, 15, 0)")
            raise RuntimeError("boom")
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) FROM clock").fetchone()[0] == 0
        assert conn.execute("SELECT ensure_call_fact_partitions(0, 2)").fetchone()[0] == 3
        assert conn.execute("SELECT ensure_call_fact_partitions(0, 2)").fetchone()[0] == 0
        conn.execute("""INSERT INTO call_fact (event_id, wf, lead_sk, minute, sim_day, hour, seg,
                        attempt_no, day_index, config_version, cohort_code, cell, w, v, did,
                        prompt, connected, engaged, interested, flag, adherence, duration_s,
                        turns) VALUES (10, 1, 1, 1500, 1, 1, 2, 1, 0, 1, 0, 3, 0, 0, 0, 0,
                        true, false, false, false, 0.9, 12, 2),
                        (11, 1, 1, -60, -1, 23, -1, 1, 0, 1, 0, 3, 0, 0, 0, 0,
                        false, false, false, false, NULL, 0, 0)""")
        where = dict(conn.execute("SELECT event_id, tableoid::regclass::text FROM call_fact")
                     .fetchall())
    assert where[10].endswith("call_fact_d001") and where[11].endswith("call_fact_default")


def test_api_role_reads_everything_and_writes_only_hold(db):
    s = db.create_run_schema(_fp(), "http://127.0.0.1:8700")
    db._created.append(s)
    with db.connect(schema=s) as conn:
        conn.execute("SET ROLE ctl_api")
        conn.execute("SELECT count(*) FROM proposal").fetchone()
        conn.execute("SELECT count(*) FROM ctl.run_registry").fetchone()
        conn.execute("INSERT INTO hold (wf, set_by, reason, from_minute) VALUES (1, 'sup', 'x', 0)")
        conn.execute("UPDATE hold SET released_minute = 5")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO decision (sim_minute, module, kind, rule_id, settings_hash, "
                         "code_version) VALUES (0, 'm', 'k', 'r', 'h', 'c')")
        conn.rollback()
        conn.execute("SET ROLE ctl_api")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM hold")
        conn.rollback()


def test_advisory_lock_is_single_owner_per_sim_url(pg_dsn):
    a = AdvisoryLock(pg_dsn, "http://127.0.0.1:8700")
    b = AdvisoryLock(pg_dsn, "http://localhost:8700/")     # same world, normalised
    c = AdvisoryLock(pg_dsn, "http://127.0.0.1:8711")      # a different world
    try:
        assert a.try_acquire() and a.held()
        assert not b.try_acquire()
        with pytest.raises(TimeoutError):
            b.acquire(poll_s=0.05, timeout_s=0.2)
        assert c.try_acquire()
        a.release()
        assert b.try_acquire() and b.held()
    finally:
        for lk in (a, b, c):
            lk.release()


def test_normalise_sim_url():
    assert normalise_sim_url("HTTP://LocalHost:8700/") == "http://127.0.0.1:8700"
    assert normalise_sim_url("http://sim:80") == "http://sim"
    assert normalise_sim_url("http://host.docker.internal:8700") == \
        "http://host.docker.internal:8700"


def test_proposal_state_check_matches_types(db):
    """The proposal.state CHECK and types.PROPOSAL_STATES are the same set."""
    from controller.types import IN_FLIGHT_STATES, PROPOSAL_STATES
    s = db.create_run_schema(_fp(), "http://127.0.0.1:8700")
    db._created.append(s)
    db.open(s)
    with db.tx() as conn:
        for i, state in enumerate(sorted(PROPOSAL_STATES)):
            _proposal(conn, f"s{i}", state, wf=100 + i)
        idx = conn.execute("SELECT pg_get_indexdef('one_in_flight'::regclass)").fetchone()[0]
    for state in IN_FLIGHT_STATES:
        assert f"'{state}'" in idx
