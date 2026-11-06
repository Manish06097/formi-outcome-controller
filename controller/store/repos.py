"""Typed read and write helpers per table group of the run schema.

Owner: track T1 (milestones M1, M2). Implements DESIGN.md 3.2 for the tables the platform owns:
the workflow catalog, the clock, the ingest cursor, config versions and their cohort codes,
intents, the bootstrap marker, loop steps and the heartbeat (alerts are written by
`obs/alerts.py`). Other tracks own the SQL of their own tables; the read helpers here that touch
them (`plan_facts`) are read-only.

Every function takes an open connection (usually the one of a `Database.tx()` block) and never
commits: the caller's transaction decides atomicity. Minutes are simulated minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from controller.types import (IN_FLIGHT_STATES, Config, FrozenDict, Intent, SimState, StepReport,
                              Tx, VersionMeta, WorkflowSpec, freeze, thaw)

UNKNOWN_POSITION = -1
"""`config_cohort.position` of a cohort seen only on call events (the version body is unknown)."""


def _jsonb(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(thaw(v))


# =============================================================================== workflow catalog

@dataclass(frozen=True)
class WorkflowCatalog:
    """The `workflow` table: dense smallint codes for workflow ids, assigned in sorted id order."""

    specs: tuple[WorkflowSpec, ...]
    codes: FrozenDict
    """workflow_id -> wf code"""

    def code(self, workflow_id: str) -> int:
        return int(self.codes[workflow_id])

    def spec(self, workflow_id: str) -> WorkflowSpec:
        return next(s for s in self.specs if s.workflow_id == workflow_id)

    def by_code(self, wf: int) -> WorkflowSpec:
        wid = next(k for k, v in self.codes.items() if v == wf)
        return self.spec(wid)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(s.workflow_id for s in self.specs)


def register_workflows(tx: Tx, specs: Sequence[WorkflowSpec], minute: int) -> WorkflowCatalog:
    """Insert or refresh `workflow` rows. Existing codes are kept; new workflow ids get the next
    codes in sorted id order, so two runs of one scenario get the same codes."""
    known = {r[0]: r[1] for r in tx.execute("SELECT workflow_id, wf FROM workflow")}
    nxt = max(known.values(), default=0) + 1
    for spec in sorted(specs, key=lambda s: s.workflow_id):
        if spec.workflow_id not in known:
            known[spec.workflow_id] = nxt
            nxt += 1
        tx.execute(
            """INSERT INTO workflow (wf, workflow_id, tenant_id, spec, spec_hash, fetched_minute)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (wf) DO UPDATE SET spec = excluded.spec, spec_hash = excluded.spec_hash,
                   fetched_minute = excluded.fetched_minute
               WHERE workflow.spec_hash IS DISTINCT FROM excluded.spec_hash""",
            (known[spec.workflow_id], spec.workflow_id, spec.tenant_id,
             Jsonb(_spec_body(spec)), spec.spec_hash(), minute))
    return load_workflows(tx)


def _spec_body(spec: WorkflowSpec) -> dict[str, Any]:
    """The stored spec: everything but `current_version`, so the row changes only when the
    business-owned spec does."""
    return {k: thaw(v) for k, v in spec.raw.items() if k != "current_version"}


def load_workflows(conn: Tx) -> WorkflowCatalog:
    rows = conn.execute("SELECT wf, workflow_id, spec FROM workflow ORDER BY workflow_id").fetchall()
    specs = tuple(WorkflowSpec.from_api({**r[2], "current_version": 0}) for r in rows)
    return WorkflowCatalog(specs=specs, codes=FrozenDict({r[1]: r[0] for r in rows}))


# =============================================================================== clock

@dataclass(frozen=True)
class ClockRow:
    sim_minute: int
    end_minute: int
    tick: int
    last_step_no: int


def init_clock(tx: Tx, state: SimState) -> None:
    tx.execute("""INSERT INTO clock (id, sim_minute, end_minute, tick, last_step_no)
                  VALUES (1, %s, %s, %s, 0) ON CONFLICT (id) DO NOTHING""",
               (state.sim_minute, state.end_minute, state.tick_minutes))


def get_clock(conn: Tx) -> ClockRow:
    r = conn.execute("SELECT sim_minute, end_minute, tick, coalesce(last_step_no, 0) "
                     "FROM clock WHERE id = 1").fetchone()
    if r is None:
        raise LookupError("clock row missing: run schema not initialised")
    return ClockRow(*r)


def set_clock(tx: Tx, sim_minute: int, step_no: int | None = None) -> None:
    tx.execute("""UPDATE clock SET sim_minute = %s, last_step_no = coalesce(%s, last_step_no)
                  WHERE id = 1""", (sim_minute, step_no))


# =============================================================================== ingest cursor

@dataclass(frozen=True)
class IngestCursor:
    chunks: int
    max_chunk: int
    """-1 when nothing is ingested"""
    events: int
    max_event_id: int
    until_minute: int | None


def ingest_cursor(conn: Tx) -> IngestCursor:
    r = conn.execute("""SELECT count(*), coalesce(max(chunk_no), -1), coalesce(sum(events), 0),
                               coalesce(max(last_event_id), 0), max(until_minute)
                        FROM ingested_chunk""").fetchone()
    return IngestCursor(int(r[0]), int(r[1]), int(r[2]), int(r[3]), r[4])


def ingested_chunk_numbers(conn: Tx) -> set[int]:
    return {r[0] for r in conn.execute("SELECT chunk_no FROM ingested_chunk")}


def last_ingested_meta(conn: Tx) -> tuple[int, int, int] | None:
    """(chunk_no, events, last_event_id) of the newest ingested chunk."""
    r = conn.execute("""SELECT chunk_no, events, last_event_id FROM ingested_chunk
                        ORDER BY chunk_no DESC LIMIT 1""").fetchone()
    return None if r is None else (int(r[0]), int(r[1]), int(r[2]))


# =============================================================================== config versions

COHORT_CODES_SQL = """
-- Assign stable per-workflow cohort codes to the (wf, version, cohort_id) rows of the temp
-- table cohort_need(wf, version, position, cohort_id) and upsert config_cohort.
-- A cohort id keeps its code across versions; new ids get max(code) + 1.. in id order;
-- 'default' is always 0. A known position replaces an unknown one (-1); nothing else changes.
-- Owner: our cohorts carry the versioned key form <slug>~<hash> (DESIGN.md 5.2); every other
-- cohort, and the business default, belongs to the client's supervisor.
WITH known AS (
    SELECT wf, cohort_id, min(cohort_code) AS code FROM config_cohort GROUP BY wf, cohort_id
), fresh AS (
    SELECT DISTINCT n.wf, n.cohort_id FROM cohort_need n
    LEFT JOIN known k ON k.wf = n.wf AND k.cohort_id = n.cohort_id
    WHERE k.code IS NULL AND n.cohort_id <> 'default'
), base AS (
    SELECT wf, max(cohort_code) AS mx FROM config_cohort GROUP BY wf
), assigned AS (
    SELECT f.wf, f.cohort_id,
           coalesce(b.mx, 0) + row_number() OVER (PARTITION BY f.wf ORDER BY f.cohort_id) AS code
    FROM fresh f LEFT JOIN base b ON b.wf = f.wf
), need AS (
    SELECT wf, version, cohort_id, max(position) AS position,
           CASE WHEN strpos(cohort_id, '~') > 0 THEN 'ours' ELSE 'supervisor' END AS owner
    FROM cohort_need GROUP BY wf, version, cohort_id
)
INSERT INTO config_cohort (wf, version, position, cohort_id, cohort_key, cohort_code, owner,
                           effective_cells)
SELECT n.wf, n.version, n.position, n.cohort_id, NULL,
       CASE WHEN n.cohort_id = 'default' THEN 0 ELSE coalesce(k.code, a.code) END,
       n.owner, '{}'
FROM need n
LEFT JOIN known k ON k.wf = n.wf AND k.cohort_id = n.cohort_id
LEFT JOIN assigned a ON a.wf = n.wf AND a.cohort_id = n.cohort_id
ORDER BY n.wf, n.version, n.cohort_id
ON CONFLICT (wf, version, cohort_id) DO UPDATE SET position = excluded.position
WHERE config_cohort.position = -1 AND excluded.position <> -1
"""


COHORT_NEED_DDL = """CREATE TEMP TABLE IF NOT EXISTS cohort_need (wf smallint, version int,
                      position smallint, cohort_id text) ON COMMIT DELETE ROWS"""


def ensure_cohort_codes(tx: Tx, rows: Iterable[tuple[int, int, int, str]]) -> None:
    """rows: (wf, version, position, cohort_id). Runs COHORT_CODES_SQL over them."""
    tx.execute(COHORT_NEED_DDL)  # type: ignore[arg-type]
    tx.execute("DELETE FROM cohort_need")
    with tx.cursor().copy("COPY cohort_need (wf, version, position, cohort_id) FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
    tx.execute(COHORT_CODES_SQL)  # type: ignore[arg-type]


@dataclass(frozen=True)
class VersionRow:
    wf: int
    version: int
    actor: str
    config: Config | None
    body_source: str
    applied_minute: int | None
    proposal_id: str | None
    approval_id: str | None
    changed_dims: tuple[str, ...]


def upsert_config_version(tx: Tx, wf: int, version: int, actor: str, config: Config | None,
                          body_source: str, applied_minute: int | None = None,
                          proposal_id: str | None = None, approval_id: str | None = None,
                          changed_dims: Sequence[str] = ()) -> None:
    """Insert a version, or fill in what was unknown about it. A known body is never replaced by
    an unknown one, and 'ours' (the body we sent) wins over 'fetched'."""
    tx.execute(
        """INSERT INTO config_version (wf, version, actor, config, config_hash, applied_minute,
                                       proposal_id, approval_id, changed_dims, body_source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (wf, version) DO UPDATE SET
             actor = CASE WHEN config_version.actor = 'unknown' THEN excluded.actor
                          ELSE config_version.actor END,
             config = CASE WHEN config_version.body_source = 'unknown'
                                OR (config_version.body_source = 'fetched'
                                    AND excluded.body_source = 'ours')
                           THEN coalesce(excluded.config, config_version.config)
                           ELSE config_version.config END,
             config_hash = CASE WHEN config_version.body_source = 'unknown'
                                     OR (config_version.body_source = 'fetched'
                                         AND excluded.body_source = 'ours')
                                THEN coalesce(excluded.config_hash, config_version.config_hash)
                                ELSE config_version.config_hash END,
             body_source = CASE WHEN excluded.body_source = 'unknown' THEN config_version.body_source
                                WHEN config_version.body_source = 'ours' THEN 'ours'
                                ELSE excluded.body_source END,
             applied_minute = coalesce(config_version.applied_minute, excluded.applied_minute),
             proposal_id = coalesce(config_version.proposal_id, excluded.proposal_id),
             approval_id = coalesce(config_version.approval_id, excluded.approval_id),
             changed_dims = CASE WHEN cardinality(config_version.changed_dims) = 0
                                 THEN excluded.changed_dims ELSE config_version.changed_dims END""",
        (wf, version, actor, _jsonb(config.stored()) if config else None,
         config.hash() if config else None, applied_minute, proposal_id, approval_id,
         list(changed_dims), body_source))


def config_version(conn: Tx, wf: int, version: int) -> VersionRow | None:
    r = conn.execute("""SELECT wf, version, actor, config, body_source, applied_minute,
                               proposal_id, approval_id, changed_dims
                        FROM config_version WHERE wf = %s AND version = %s""",
                     (wf, version)).fetchone()
    return None if r is None else _version_row(r)


def config_versions(conn: Tx, wf: int) -> list[VersionRow]:
    rows = conn.execute("""SELECT wf, version, actor, config, body_source, applied_minute,
                                  proposal_id, approval_id, changed_dims
                           FROM config_version WHERE wf = %s ORDER BY version""", (wf,)).fetchall()
    return [_version_row(r) for r in rows]


def _version_row(r: Sequence[Any]) -> VersionRow:
    return VersionRow(wf=r[0], version=r[1], actor=r[2],
                      config=Config.from_stored(r[3]) if r[3] is not None else None,
                      body_source=r[4], applied_minute=r[5], proposal_id=r[6], approval_id=r[7],
                      changed_dims=tuple(r[8] or ()))


def record_history(tx: Tx, wf: int, history: Sequence[VersionMeta],
                   minute_of: Any) -> None:
    """Record every version of `GET config/history` (bodies unknown here)."""
    for v in history:
        upsert_config_version(tx, wf, v.version, v.actor, None, "unknown",
                              applied_minute=minute_of(v.applied_at) if v.applied_at else None,
                              proposal_id=v.proposal_id, approval_id=v.approval_id,
                              changed_dims=v.changed_dims)


def controller_versions(conn: Tx) -> list[tuple[str, int, str | None]]:
    """(workflow_id, version, proposal_id) of every controller-actor version we recorded."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        """SELECT w.workflow_id, c.version, c.proposal_id FROM config_version c
           JOIN workflow w USING (wf) WHERE c.actor LIKE 'controller%%'
           ORDER BY 1, 2""")]


def max_versions(conn: Tx) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        """SELECT w.workflow_id, max(c.version) FROM config_version c JOIN workflow w USING (wf)
           GROUP BY 1""")}


def held_approvals(conn: Tx) -> list[tuple[str, str]]:
    """(approval_id, proposal_id) of every approval we hold (proposal and mirror tables)."""
    rows = conn.execute(
        """SELECT approval_id, proposal_id FROM proposal WHERE approval_id IS NOT NULL
           UNION SELECT approval_id, proposal_id FROM approval_mirror
           WHERE proposal_id IS NOT NULL ORDER BY 1""").fetchall()
    return [(r[0], r[1]) for r in rows]


# =============================================================================== intents

def insert_intent(tx: Tx, kind: str, key: str, request: Mapping[str, Any], decision_id: int | None,
                  sim_minute: int) -> int:
    r = tx.execute("""INSERT INTO intent (kind, idem_key, request, status, decision_id, sim_minute)
                      VALUES (%s, %s, %s, 'started', %s, %s) RETURNING intent_id""",
                   (kind, key, _jsonb(request), decision_id, sim_minute)).fetchone()
    return int(r[0])


def finish_intent(tx: Tx, intent_id: int, status: str, response: Mapping[str, Any] | None) -> None:
    tx.execute("""UPDATE intent SET status = %s, response = %s, finished_wall = now()
                  WHERE intent_id = %s""", (status, _jsonb(response), intent_id))


def _intent(r: Sequence[Any]) -> Intent:
    return Intent(intent_id=r[0], kind=r[1], idem_key=r[2], request=freeze(r[3] or {}),
                  status=r[4], decision_id=r[5], sim_minute=r[6],
                  response=freeze(r[7]) if r[7] is not None else None)


_INTENT_COLS = "intent_id, kind, idem_key, request, status, decision_id, sim_minute, response"


def open_intents(conn: Tx) -> list[Intent]:
    rows = conn.execute(f"SELECT {_INTENT_COLS} FROM intent WHERE status = 'started' "
                        "ORDER BY intent_id").fetchall()
    return [_intent(r) for r in rows]


def intent_by_key(conn: Tx, key: str) -> Intent | None:
    r = conn.execute(f"SELECT {_INTENT_COLS} FROM intent WHERE idem_key = %s", (key,)).fetchone()
    return None if r is None else _intent(r)


# =============================================================================== bootstrap

@dataclass(frozen=True)
class BootstrapRow:
    started_minute: int | None
    completed_minute: int | None
    history_chunks: int | None
    decision_id: int | None


def bootstrap_row(conn: Tx) -> BootstrapRow | None:
    r = conn.execute("""SELECT started_minute, completed_minute, history_chunks, decision_id
                        FROM bootstrap WHERE id = 1""").fetchone()
    return None if r is None else BootstrapRow(*r)


def start_bootstrap(tx: Tx, minute: int, history_chunks: int, decision_id: int | None) -> None:
    tx.execute("""INSERT INTO bootstrap (id, started_minute, completed_minute, history_chunks,
                                         decision_id)
                  VALUES (1, %s, NULL, %s, %s)
                  ON CONFLICT (id) DO UPDATE SET started_minute = excluded.started_minute,
                      history_chunks = excluded.history_chunks,
                      decision_id = excluded.decision_id
                  WHERE bootstrap.completed_minute IS NULL""",
               (minute, history_chunks, decision_id))


def complete_bootstrap(tx: Tx, minute: int) -> None:
    tx.execute("UPDATE bootstrap SET completed_minute = %s WHERE id = 1", (minute,))


# =============================================================================== loop and ops

def insert_loop_step(tx: Tx, r: StepReport, orphan_calls: int, rss_mb: int | None,
                     db_bytes: int | None) -> None:
    tx.execute(
        """INSERT INTO loop_step (step_no, from_minute, to_minute, phase_ms, chunks, events,
                                  orphan_calls, decisions, proposals_submitted, applies,
                                  credits_remaining, rss_mb, db_bytes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (step_no) DO NOTHING""",
        (r.step_no, r.from_minute, r.to_minute, Jsonb(thaw(r.phase_ms)), r.chunks, r.events,
         orphan_calls, r.decisions, r.proposals_submitted, r.applies, r.credits_remaining,
         rss_mb, db_bytes))


def beat(conn: Tx, pid: int, step_no: int | None) -> None:
    conn.execute("""INSERT INTO heartbeat (id, wall, pid, step_no) VALUES (1, now(), %s, %s)
                    ON CONFLICT (id) DO UPDATE SET wall = now(), pid = excluded.pid,
                        step_no = excluded.step_no""", (pid, step_no))


def db_bytes(conn: Tx) -> int:
    return int(conn.execute("SELECT pg_database_size(current_database())").fetchone()[0])


# =============================================================================== plan facts

@dataclass(frozen=True)
class PlanFacts:
    """Store facts per workflow code for `WorkflowPlanState` (DESIGN.md 4.2)."""

    pending: Mapping[int, tuple[int, int | None]]
    """wf -> (class, submitted_minute) of the in-flight pending proposal"""
    approved_apply_by: Mapping[int, tuple[int, ...]]
    open_incident: frozenset[int]
    suspect_did: frozenset[int]


def plan_facts(conn: Tx) -> PlanFacts:
    """Read the proposal, incident and DID tables (owned by other tracks; read-only here)."""
    pending: dict[int, tuple[int, int | None]] = {}
    approved: dict[int, list[int]] = {}
    for wf, cls, state, sub, apply_by in conn.execute(
            """SELECT wf, class, state, submitted_minute, apply_by_minute FROM proposal
               WHERE state = ANY(%s) ORDER BY wf, proposal_id""", (sorted(IN_FLIGHT_STATES),)):
        if state in ("pending", "submitting"):
            pending[wf] = (cls, sub)
        elif state in ("approved", "applying") and apply_by is not None:
            approved.setdefault(wf, []).append(apply_by)
        elif state in ("approved", "applying"):
            pending[wf] = (cls, sub)    # approved without a known apply_by: watch as pending
    incident = frozenset(r[0] for r in conn.execute(
        "SELECT DISTINCT wf FROM incident WHERE resolved_minute IS NULL"))
    suspect = frozenset(r[0] for r in conn.execute(
        "SELECT DISTINCT wf FROM did_state WHERE status = 'suspect'"))
    return PlanFacts(pending=pending,
                     approved_apply_by={k: tuple(sorted(v)) for k, v in approved.items()},
                     open_incident=incident, suspect_did=suspect)


def local_approved_count(conn: Tx) -> int:
    """Proposals in local APPROVED or APPLYING (the overnight-jump assertion, 4.2)."""
    return int(conn.execute("SELECT count(*) FROM proposal WHERE state IN ('approved', 'applying')"
                            ).fetchone()[0])
