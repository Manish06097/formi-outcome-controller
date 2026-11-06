"""Ingest on a hand-written NDJSON fixture (DESIGN.md 10.1 "Ingest"), against a real Postgres.

- The SQL path and the Python fallback produce identical facts and rollups.
- Rollups equal a from-scratch recomputation over call_fact, and hand-computed counts.
- A chunk is ingested exactly once; a chunk that does not match its metadata leaves nothing.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Iterator

import pytest

from controller.ingest.pipeline import IngestCountMismatch, IngestPipeline, rollup_mismatches
from controller.store import repos
from controller.store.db import Database
from controller.types import ChunkMeta, Config, ConfigEvent, SimEpoch, WorkflowSpec
from controller.world.configs import ConfigMirror
from tests.e2e.platform.helpers import DOMAIN_TABLES, diff_dumps, dump_schema

pytestmark = pytest.mark.pg

EPOCH = dt.datetime.fromisoformat("2027-01-04T00:00:00+05:30")
TICK = 15
SPEC = {
    "workflow_id": "wf_a", "tenant_id": "ten", "use_case": "renewal", "languages": ["hi", "en"],
    "dialling_window": [9, 21], "dials_per_hour": 100, "cadence": [2, 3], "min_gap_minutes": 60,
    "daily_batch": {"size": 3, "at_hour": 8, "returning_share": 0.0}, "mid_flight": "allowed",
    "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
    "options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"],
                "caller_id": ["D1", "D2"], "prompt_version": ["P1", "P2"]},
    "prompt_catalog": {"P1": "a", "P2": "b"},
    "governance": {"auto_apply": {"enabled": False}, "approver_hours": [10, 19],
                   "approver_latency_minutes": [30, 240], "proposal_ttl_hours": 24,
                   "apply_window_hours": 2, "max_requests_per_24h": 3, "base_reject_rate": 0.0,
                   "min_validation_pass_rate": 0.97},
    "current_version": 1}
V1 = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0},
                                                 "voice": {"V1": 1.0},
                                                 "caller_id": {"D1": 0.5, "D2": 0.5}}},
      "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


def ts(minute: int, offset: int = 0) -> str:
    return (EPOCH + dt.timedelta(minutes=minute + offset)).strftime("%Y-%m-%dT%H:%M:00+05:30")


def lead(eid, n, minute, lang="hi", age="18-30"):
    return {"type": "lead", "ts": ts(minute), "workflow_id": "wf_a", "campaign_id": "wf_a:c1",
            "lead_id": f"wf_a:l{n}", "person_id": f"wf_a:p{n}", "event_id": eid,
            "features": {"lang": lang, "city_tier": "1", "age_band": age, "value_band": "low"}}


def call(eid, n, minute, offset, attempt, connected, engaged=False, flag=False, w="W1", v="V1",
         did="D1", prompt="P1", version=1, cohort="default", lead_id=None):
    disp = ("concluded_interested" if engaged else "short_hangup") if connected else "no_answer"
    return {"type": "call", "ts": ts(minute, offset), "tenant_id": "ten", "workflow_id": "wf_a",
            "campaign_id": "wf_a:c1", "lead_id": lead_id or f"wf_a:l{n}",
            "person_id": f"wf_a:p{n}", "attempt_no": attempt, "campaign_day_index": 0,
            "config_version": version, "cohort_id": cohort,
            "variant": {"welcome_message": w, "voice": v, "caller_id": did,
                        "prompt_version": prompt},
            "connected": connected, "engaged": engaged, "disposition": disp,
            "duration_s": 100 if engaged else 10, "turns": 8 if engaged else 2,
            "qa": {"hallucination_flag": flag, "flow_adherence": 0.931} if connected else None,
            "event_id": eid}


# History (day -1) and run (day 0) events, ids strictly increasing.
H = -1440
HISTORY = [
    {"type": "campaign_created", "ts": ts(H + 480), "tenant_id": "ten", "workflow_id": "wf_a",
     "campaign_id": "wf_a:c1", "leads": 3, "config_version_at_creation": 1, "event_id": 1},
    lead(2, 1, H + 480), lead(3, 2, H + 480, lang="en"), lead(4, 3, H + 480, age="60+"),
    call(5, 1, H + 540, 7, 1, True, engaged=False, flag=True),
    call(6, 2, H + 540, 3, 1, False),
    call(7, 3, H + 555, 14, 1, True, engaged=True),
]
EVENTS = [
    call(8, 1, 600, 2, 2, True, engaged=True, w="W2", v="V2", did="D2"),      # 10:02, seg 0
    call(9, 2, 600, 9, 2, False, did="D2"),
    {"type": "config_changed", "ts": ts(615), "workflow_id": "wf_a", "version": 2,
     "actor": "supervisor", "changed_dims": ["cohorts", "voice"], "proposal_id": None,
     "approval_id": None, "event_id": 10},
    call(11, 2, 900, 1, 3, True, version=2, cohort="seniors", v="V2"),     # 15:01, seg 1
    call(12, 9, 900, 5, 1, True, engaged=True, lead_id="wf_a:l404"),         # orphan call
    {"type": "approval_decided", "ts": ts(905), "approval_id": "apr_00001",
     "workflow_id": "wf_a", "status": "rejected", "reasons": ["judgement"], "event_id": 13},
    {"type": "brand_new_event", "ts": ts(910), "workflow_id": "wf_a", "x": 1, "event_id": 14},
]


def ndjson(events) -> bytes:
    return b"".join(json.dumps(e, separators=(",", ":")).encode() + b"\n" for e in events)


CHUNKS = {0: ndjson(HISTORY), 1: ndjson(EVENTS)}
META = {0: ChunkMeta(0, "history", len(HISTORY), 1, 7, 0, ts(0)),
        1: ChunkMeta(1, "events", len(EVENTS), 8, 14, 1440, ts(1440))}


class FixtureSource:
    """Serves the fixture chunks in small pieces, like an HTTP stream."""

    def __init__(self, chunks: dict[int, bytes], piece: int = 97):
        self.chunks, self.piece = chunks, piece

    def stream_chunk(self, n: int) -> Iterator[bytes]:
        data = self.chunks[n]
        for i in range(0, len(data), self.piece):
            yield data[i:i + self.piece]

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[ChunkMeta]:
        return [m for m in META.values() if m.kind == kind and m.chunk > after]


class FixtureClient:
    """The two calls ConfigMirror makes, answered from the fixture."""

    def __init__(self) -> None:
        self.version = 1

    def config(self, wf: str):
        from controller.types import ConfigSnapshot
        body = dict(V1)
        if self.version == 2:
            body = {**V1, "cohorts": [{"id": "seniors", "where": {"age_band": ["60+"]}}],
                    "allocations": {**V1["allocations"], "seniors": {"voice": {"V2": 1.0}}}}
        return ConfigSnapshot(wf, self.version, "supervisor" if self.version == 2
                              else "business_default", None, Config.from_stored(body))

    def config_history(self, wf: str):
        from controller.types import VersionMeta
        return [VersionMeta(1, "business_default", (), None, None, None)]


def open_fixture_schema(dsn: str, fp: str, spec: dict, chunks: dict[int, bytes],
                        parser: str = "sql") -> tuple[str, IngestPipeline, Database,
                                                      ConfigMirror, FixtureClient]:
    db = Database(dsn)
    db.ensure_ctl()
    schema = db.create_run_schema(fp, "http://fixture", sim_days=2)
    db.open(schema)
    epoch = SimEpoch(EPOCH)
    client = FixtureClient()
    with db.tx() as tx:
        cat = repos.register_workflows(tx, [WorkflowSpec.from_api(spec)], -1440)
        tx.execute("INSERT INTO clock VALUES (1, 0, 2880, 15, 0)")
        mirror = ConfigMirror(client, cat, epoch)  # type: ignore[arg-type]
        mirror.bootstrap(tx)
    src = FixtureSource(chunks)
    p = IngestPipeline(db, src, int(EPOCH.timestamp()), TICK, parser=parser)
    return schema, p, db, mirror, client


def ingest_fixture(dsn: str, parser: str, fp: str) -> tuple[str, IngestPipeline, Database]:
    schema, p, db, mirror, client = open_fixture_schema(dsn, fp, SPEC, CHUNKS, parser)
    rep0 = p.ingest([META[0]])
    rep1 = p.ingest([META[1]])
    assert rep0.chunks == (0,) and rep1.chunks == (1,)
    assert rep1.orphan_calls == 1
    assert [e.version for e in rep1.config_events] == [2]
    assert [e.status for e in rep1.approval_events] == ["rejected"]
    client.version = 2
    with db.tx() as tx:
        mirror.refresh(tx, [ConfigEvent(10, "wf_a", 2, "supervisor", ("cohorts", "voice"), None,
                                        None, 615)])
        mirror.refresh_effective_cells(tx)
    return schema, p, db


@pytest.fixture()
def fixture_db(db_dsn):
    yield db_dsn


def test_sql_and_python_paths_identical(fixture_db):
    a, pa, da = ingest_fixture(fixture_db, "sql", "a" * 64)
    b, pb, db_ = ingest_fixture(fixture_db, "python", "b" * 64)
    try:
        dump_a = dump_schema(fixture_db, a, DOMAIN_TABLES + ("ingested_chunk",))
        dump_b = dump_schema(fixture_db, b, DOMAIN_TABLES + ("ingested_chunk",))
        assert diff_dumps(dump_a, dump_b) == []
        for d in (da, db_):
            with d.tx() as tx:
                assert rollup_mismatches(tx, TICK) == {"r_engage": 0, "r_pickup": 0, "r_did": 0,
                                                       "r_flags": 0}
    finally:
        da.close()
        db_.close()


def test_hand_computed_rollups_and_facts(fixture_db):
    schema, p, db = ingest_fixture(fixture_db, "sql", "c" * 64)
    try:
        with db.tx() as tx:
            q = lambda s: tx.execute(s).fetchall()  # noqa: E731
            # history calls: rollups only; post-D0 calls: call_fact too (orphan included)
            assert q("SELECT count(*) FROM call_fact") == [(4,)]
            assert q("SELECT count(*) FROM call_fact WHERE lead_sk IS NULL") == [(1,)]
            # lead_sk is the lead event id; cells in (lang, tier, age, value) order
            assert q("SELECT lead_sk, lead_id, cell FROM lead_dim ORDER BY 1") == [
                (2, "wf_a:l1", 2), (3, "wf_a:l2", 1), (4, "wf_a:l3", 3)]
            # lead 1 engaged at 10:02 D0, lead 3 engaged in history; lead 2 has attempts left
            # (3 of 5) but its campaign (day -1, cadence of 2 days) ages out at D1 00:00, the
            # until_minute of chunk 1, so it is exhausted
            assert q("SELECT lead_id, state, attempts, concluded_min FROM lead_dim ORDER BY 1") == [
                ("wf_a:l1", 1, 2, 602), ("wf_a:l2", 2, 3, None), ("wf_a:l3", 1, 1, -1440 + 569)]
            # r_engage: history seg -2 (09:07 is before the 15:00 midpoint); D0 seg 0 and seg 1
            assert q("""SELECT seg, cell, config_version, w, v, connected, engaged, flags
                        FROM r_engage ORDER BY 1, 2, 4, 5""") == [
                (-2, 2, 1, 0, 0, 1, 0, 1), (-2, 3, 1, 0, 0, 1, 1, 0),
                (0, 2, 1, 1, 1, 1, 1, 0), (1, 1, 2, 0, 1, 1, 0, 0)]
            # r_pickup: attempt buckets 1 | 2-3 | 4+
            assert q("""SELECT sim_day, hour, cell, attempt_bucket, dials, connected
                        FROM r_pickup WHERE sim_day = 0 ORDER BY 2, 3""") == [
                (0, 10, 1, 2, 1, 0), (0, 10, 2, 2, 1, 1), (0, 15, 1, 2, 1, 1)]
            # r_did: tick of the dial (10:00 -> tick 40), seniors cohort coded 1, orphan included
            assert q("""SELECT tick_no, hour, cohort_code, did, dials, connected FROM r_did
                        WHERE tick_no >= 0 ORDER BY 1, 3, 4""") == [
                (40, 10, 0, 1, 2, 1), (60, 15, 0, 0, 1, 1), (60, 15, 1, 0, 1, 1)]
            assert q("""SELECT version, position, cohort_id, cohort_code, owner, effective_cells
                        FROM config_cohort ORDER BY 1, 4""") == [
                (1, 0, "default", 0, "supervisor", [1, 2, 3]),
                (2, 1, "default", 0, "supervisor", [1, 2]),
                (2, 0, "seniors", 1, "supervisor", [3])]
            assert q("SELECT version, actor, body_source FROM config_version ORDER BY 1") == [
                (1, "business_default", "fetched"), (2, "supervisor", "fetched")]
            assert q("SELECT type FROM event_other") == [("brand_new_event",)]
            assert q("SELECT approval_id, status, reasons FROM approval_event") == [
                ("apr_00001", "rejected", ["judgement"])]
    finally:
        db.close()


def test_exactly_once_and_count_mismatch(fixture_db):
    schema, p, db = ingest_fixture(fixture_db, "sql", "d" * 64)
    try:
        again = p.ingest([META[0], META[1]])
        assert again.chunks == () and set(again.skipped_chunks) == {0, 1}
        with db.tx() as tx:
            before = tx.execute("SELECT sum(dials) FROM r_pickup").fetchone()
        bad = ChunkMeta(2, "events", 3, 15, 17, 1500, ts(1500))
        p.source.chunks[2] = ndjson(EVENTS[:2])        # 2 lines, metadata says 3
        p.loader.source = p.source
        with pytest.raises(IngestCountMismatch):
            p.ingest([bad])
        with db.tx() as tx:
            assert tx.execute("SELECT sum(dials) FROM r_pickup").fetchone() == before
            assert tx.execute("SELECT count(*) FROM ingested_chunk").fetchone() == (2,)
    finally:
        db.close()


@pytest.mark.parametrize("parser", ["sql", "python"])
@pytest.mark.parametrize("window", [(9, 21), (9, 20), (7, 20)])
def test_ingest_segments_follow_the_learning_rule(fixture_db, window, parser):
    """Ingest's `seg` is `snapshot.segment_of` of the dial's tick start (DESIGN.md 5.3, ADR
    0028): the learning code and the rollups agree on every segment, including the tick that
    starts exactly at the half-window and calls randomised late inside the tick before it."""
    from controller.snapshot import segment_of
    from controller.world.clock import floor_to
    lo, hi = window
    half = lo * 60 + (hi - lo) * 30
    spec = {**SPEC, "dialling_window": list(window)}
    hist = [{"type": "campaign_created", "ts": ts(H + 480), "tenant_id": "ten",
             "workflow_id": "wf_a", "campaign_id": "wf_a:c1", "leads": 1,
             "config_version_at_creation": 1, "event_id": 1},
            lead(2, 1, H + 480),
            call(3, 1, H + half - TICK, TICK - 1, 1, False),
            call(4, 1, H + half, 3, 2, True)]
    ev = [call(5, 1, half - TICK, TICK - 1, 3, False), call(6, 1, half, 0, 4, False),
          call(7, 1, half, TICK - 1, 5, True, engaged=True), call(8, 1, lo * 60, 0, 6, False)]
    chunks = {0: ndjson(hist), 1: ndjson(ev)}
    meta = [ChunkMeta(0, "history", len(hist), 1, 4, 0, ts(0)),
            ChunkMeta(1, "events", len(ev), 5, 8, 1440, ts(1440))]
    fp = (f"{lo:02d}{hi:02d}{'a' if parser == 'sql' else 'b'}" * 13)[:64]
    schema, p, db, _, _ = open_fixture_schema(fixture_db, fp, spec, chunks, parser)
    try:
        p.source.list_chunks = lambda kind="events", after=-1: [   # type: ignore[assignment]
            m for m in meta if m.kind == kind and m.chunk > after]
        p.ingest(meta[:1])
        p.ingest(meta[1:])
        with db.tx() as tx:
            facts = tx.execute("SELECT minute, seg FROM call_fact ORDER BY event_id").fetchall()
            pickup = tx.execute("SELECT sim_day, hour, sum(dials) FROM r_pickup "
                                "GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
            engage = tx.execute("SELECT seg FROM r_engage ORDER BY seg").fetchall()
        assert [seg for _, seg in facts] == [segment_of(floor_to(m, TICK), window)
                                              for m, _ in facts]
        assert [seg for _, seg in facts] == [0, 1, 1, 0]
        assert sum(d for _, _, d in pickup) == 6
        assert engage == [(segment_of(H + half, window),), (1,)] == [(-1,), (1,)]
    finally:
        db.close()
