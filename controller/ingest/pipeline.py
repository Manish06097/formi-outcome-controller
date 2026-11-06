"""Ingestor: every chunk exactly once, in chunk order, with gap filling and per-step timings.

Owner: track T1 (milestone M1). Implements DESIGN.md 3.1 (the per-chunk transaction), 8.1 (the
chunk idempotency key) and the `Ingestor` interface of 11.2.

One transaction per chunk runs the statements of `transform.sql` and inserts the chunk's
`ingested_chunk` row; that row is the exactly-once guard, so a chunk whose transaction did not
commit (crash, count mismatch, stream error) is simply ingested again later, and a committed one
is never ingested twice. Chunks are applied in chunk-number order, which is event-id order.

Two parsers sit behind the same pipeline (`CTL_INGEST`): `sql` stages raw lines with COPY on a
loader thread (`loader.py`) and parses them in Postgres; `python` parses with `json.loads` inside
the chunk transaction (`python_parse.py`). Everything after parsing is shared SQL.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from controller.ingest.loader import ChunkSource, PrefetchingStager, StageLoader
from controller.ingest.python_parse import parse_into_ev
from controller.reliability.faults import maybe_crash
from controller.store import repos
from controller.store.db import Database
from controller.types import ApprovalEvent, ChunkMeta, ConfigEvent, IngestReport, Tx

log = logging.getLogger(__name__)

TRANSFORM_PATH = Path(__file__).with_name("transform.sql")
NO_PREVIOUS_UNTIL = -10 ** 9
"""prev_until_minute for the first chunk: every campaign older than its cadence expires."""


class IngestError(RuntimeError):
    """A chunk could not be ingested; its transaction rolled back (nothing partial remains)."""


class IngestCountMismatch(IngestError):
    """The parsed chunk does not match its metadata (events, first and last event id)."""


@dataclass(frozen=True)
class Step:
    name: str
    sql: str
    paths: frozenset[str]
    """parsers that run this step; empty = both"""

    @property
    def has_params(self) -> bool:
        return "%(" in self.sql


def load_transform(path: Path = TRANSFORM_PATH) -> list[Step]:
    """Split transform.sql into named statements. Comment lines are dropped (they may contain
    characters the parameter syntax would misread); `@COHORT_CODES_SQL` is the shared statement
    from `store.repos`."""
    steps: list[Step] = []
    name: str | None = None
    paths: frozenset[str] = frozenset()
    body: list[str] = []

    def flush() -> None:
        if name is not None:
            sql = "\n".join(body).strip().rstrip(";").strip()
            if sql == "@COHORT_CODES_SQL":
                sql = _strip_comments(repos.COHORT_CODES_SQL)
            steps.append(Step(name, sql, paths))

    for line in path.read_text().splitlines():
        m = re.match(r"^--\s*name:\s*(\w+)\s*$", line)
        if m:
            flush()
            name, paths, body = m.group(1), frozenset(), []
            continue
        p = re.match(r"^--\s*paths:\s*([\w ,]+)$", line)
        if p and name is not None:
            paths = frozenset(x.strip() for x in p.group(1).split(",") if x.strip())
            continue
        if name is not None and not line.lstrip().startswith("--"):
            body.append(line)
    flush()
    return steps


def _strip_comments(sql: str) -> str:
    return "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--")).strip()


@dataclass
class ChunkResult:
    chunk: int
    events: int
    calls: int
    leads: int
    orphan_calls: int
    unknown_options: int
    bytes: int
    txn_ms: int
    step_ms: dict[str, int] = field(default_factory=dict)
    config_events: list[ConfigEvent] = field(default_factory=list)
    approval_events: list[ApprovalEvent] = field(default_factory=list)
    skipped: bool = False


class IngestPipeline:
    """The `Ingestor` (DESIGN.md 11.2) over a Database bound to the run schema."""

    def __init__(self, db: Database, source: ChunkSource, epoch0: int, tick: int,
                 parser: str = "sql", loader_source: ChunkSource | None = None,
                 prefetch: int = 1):
        if parser not in ("sql", "python"):
            raise ValueError(f"parser must be sql or python, not {parser}")
        self.db = db
        self.source = source
        self.epoch0 = int(epoch0)
        self.tick = int(tick)
        self.parser = parser
        self.steps = load_transform()
        self.loader = StageLoader(db, loader_source or source)
        self.prefetch = prefetch
        self.last_results: list[ChunkResult] = []

    # ------------------------------------------------------------------ Ingestor
    def ingest(self, chunks: Iterable[ChunkMeta]) -> IngestReport:
        """Ingest the given chunks in chunk order; already-ingested ones are skipped."""
        started = time.monotonic()
        todo = sorted({c.chunk: c for c in chunks}.values(), key=lambda c: c.chunk)
        with self.db.tx() as conn:
            done = repos.ingested_chunk_numbers(conn)
        skipped = tuple(c.chunk for c in todo if c.chunk in done)
        todo = [c for c in todo if c.chunk not in done]
        results: list[ChunkResult] = []
        if self.parser == "sql":
            stager = PrefetchingStager(self.loader, ahead=self.prefetch)
            try:
                for meta, nbytes in stager.run(todo):
                    results.append(self._with_retry(meta, nbytes))
            finally:
                stager.close()
        else:
            for meta in todo:
                results.append(self._with_retry(meta, 0))
        self.last_results = results
        return _report(results, skipped, int((time.monotonic() - started) * 1000))

    def pending_chunks(self) -> list[ChunkMeta]:
        """Every listed chunk (history and events) above what is ingested, plus any gap below."""
        with self.db.tx() as conn:
            done = repos.ingested_chunk_numbers(conn)
        listed = (self.source.list_chunks("history", -1) + self.source.list_chunks("events", -1))
        return sorted((c for c in listed if c.chunk not in done), key=lambda c: c.chunk)

    def catch_up(self) -> IngestReport:
        """Ingest everything the simulator lists that we have not ingested (gap filling)."""
        return self.ingest(self.pending_chunks())

    # ------------------------------------------------------------------ one chunk
    def _with_retry(self, meta: ChunkMeta, nbytes: int) -> ChunkResult:
        try:
            return self._transform(meta, nbytes)
        except IngestCountMismatch as e:
            log.error("chunk %d: %s; staging it again", meta.chunk, e)
            if self.parser == "sql":
                with self.db.tx() as conn:
                    conn.execute("DELETE FROM stage_raw WHERE chunk_no = %s", (meta.chunk,))
                nbytes = self.loader.stage(meta)
            return self._transform(meta, nbytes)

    def _transform(self, meta: ChunkMeta, nbytes: int) -> ChunkResult:
        maybe_crash("chunk_transform", "before_t1")
        t0 = time.monotonic()
        res = ChunkResult(meta.chunk, meta.events, 0, 0, 0, 0, nbytes, 0)
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM ingested_chunk WHERE chunk_no = %s",
                            (meta.chunk,)).fetchone():
                conn.execute("DELETE FROM stage_raw WHERE chunk_no = %s", (meta.chunk,))
                res.skipped = True
                return res
            prev = conn.execute("SELECT max(until_minute) FROM ingested_chunk").fetchone()[0]
            params: dict[str, Any] = {
                "chunk_no": meta.chunk, "kind": meta.kind, "epoch0": self.epoch0,
                "tick": self.tick, "until_minute": meta.until_minute,
                "prev_until_minute": NO_PREVIOUS_UNTIL if prev is None else prev,
                "events": meta.events, "first_event_id": meta.first_event_id,
                "last_event_id": meta.last_event_id, "bytes": nbytes, "txn_ms": 0}
            for step in self.steps:
                if step.paths and self.parser not in step.paths:
                    continue
                s0 = time.monotonic()
                if step.name == "chunk_done":
                    self._collect_events(conn, res)
                    res.bytes = params["bytes"]
                    params["txn_ms"] = res.txn_ms = int((time.monotonic() - t0) * 1000)
                cur = conn.execute(step.sql, params if step.has_params else None)  # type: ignore[arg-type]
                if step.name == "create_ev" and self.parser == "python":
                    params["bytes"] = parse_into_ev(conn, self.source.stream_chunk(meta.chunk),
                                                    self.epoch0)
                elif step.name == "check":
                    self._check(meta, cur.fetchone())
                elif step.name == "summary":
                    calls, leads, orphans, unknown = cur.fetchone()
                    res.calls, res.leads, res.orphan_calls = int(calls), int(leads), int(orphans)
                    res.unknown_options = int(unknown)
                res.step_ms[step.name] = int((time.monotonic() - s0) * 1000)
            maybe_crash("chunk_transform", "in_t2")
        maybe_crash("chunk_transform", "after_t2")
        if res.orphan_calls or res.unknown_options:
            log.warning("chunk %d: %d orphan calls, %d calls with an unknown option",
                        meta.chunk, res.orphan_calls, res.unknown_options)
        log.info("ingested chunk %d (%s): %d events in %d ms (%.0f events/s)", meta.chunk,
                 meta.kind, meta.events, res.txn_ms, meta.events / max(res.txn_ms, 1) * 1000)
        return res

    @staticmethod
    def _check(meta: ChunkMeta, row: tuple[Any, ...]) -> None:
        n, first, last, bad = (int(x) for x in row)
        if (n, first, last) != (meta.events, meta.first_event_id, meta.last_event_id) or bad:
            raise IngestCountMismatch(
                f"chunk {meta.chunk}: parsed {n} events ids {first}..{last} ({bad} without id or "
                f"ts), metadata says {meta.events} ids {meta.first_event_id}..{meta.last_event_id}")

    @staticmethod
    def _collect_events(conn: Tx, res: ChunkResult) -> None:
        for r in conn.execute("""SELECT event_id, workflow_id, version, actor, changed_dims,
                                        proposal_id, approval_id, minute
                                 FROM ev WHERE type = 'config_changed' ORDER BY event_id"""):
            res.config_events.append(ConfigEvent(
                event_id=r[0], workflow_id=r[1], version=r[2], actor=r[3] or "unknown",
                changed_dims=tuple(r[4] or ()), proposal_id=r[5], approval_id=r[6], minute=r[7]))
        for r in conn.execute("""SELECT event_id, approval_id, workflow_id, status, reasons, minute
                                 FROM ev WHERE type = 'approval_decided' ORDER BY event_id"""):
            res.approval_events.append(ApprovalEvent(
                event_id=r[0], approval_id=r[1], workflow_id=r[2], status=r[3] or "unknown",
                reasons=tuple(r[4] or ()), minute=r[5]))


def _report(results: list[ChunkResult], skipped: tuple[int, ...], wall_ms: int) -> IngestReport:
    done = [r for r in results if not r.skipped]
    return IngestReport(
        chunks=tuple(r.chunk for r in done), events=sum(r.events for r in done),
        calls=sum(r.calls for r in done), leads=sum(r.leads for r in done),
        orphan_calls=sum(r.orphan_calls for r in done),
        last_event_id=0, until_minute=0, wall_ms=wall_ms,
        config_events=tuple(e for r in done for e in r.config_events),
        approval_events=tuple(e for r in done for e in r.approval_events),
        skipped_chunks=skipped + tuple(r.chunk for r in results if r.skipped))


# =============================================================================== verification

ROLLUP_RECOMPUTE: dict[str, tuple[str, str]] = {
    # table: (rollup rows for post-D0 data, the same rows recomputed from call_fact)
    "r_engage": (
        """SELECT wf, seg, cell, config_version, prompt, w, v, connected, engaged, flags
           FROM r_engage WHERE seg >= 0""",
        """SELECT wf, seg, cell, config_version, prompt, w, v, count(*)::int,
                  (count(*) FILTER (WHERE engaged))::int, (count(*) FILTER (WHERE flag))::int
           FROM call_fact WHERE connected AND cell IS NOT NULL
           GROUP BY wf, seg, cell, config_version, prompt, w, v"""),
    "r_pickup": (
        """SELECT wf, sim_day, hour, cell, attempt_bucket, dials, connected
           FROM r_pickup WHERE sim_day >= 0""",
        """SELECT wf, sim_day, hour, cell,
                  CASE WHEN attempt_no <= 1 THEN 1 WHEN attempt_no <= 3 THEN 2 ELSE 3 END,
                  count(*)::int, (count(*) FILTER (WHERE connected))::int
           FROM call_fact WHERE cell IS NOT NULL GROUP BY 1, 2, 3, 4, 5"""),
    "r_did": (
        """SELECT wf, tick_no, hour, cohort_code, did, dials, connected
           FROM r_did WHERE tick_no >= 0""",
        """SELECT wf, minute / %(tick)s, hour, cohort_code, did, count(*)::int,
                  (count(*) FILTER (WHERE connected))::int
           FROM call_fact GROUP BY 1, 2, 3, 4, 5"""),
    "r_flags": (
        """SELECT wf, sim_hour, prompt, connected, flags, engaged FROM r_flags WHERE sim_hour >= 0""",
        """SELECT wf, (minute / %(tick)s) * %(tick)s / 60, prompt, count(*)::int,
                  (count(*) FILTER (WHERE flag))::int, (count(*) FILTER (WHERE engaged))::int
           FROM call_fact WHERE connected GROUP BY 1, 2, 3"""),
}


def rollup_mismatches(conn: Tx, tick: int) -> dict[str, int]:
    """Rows that differ between each post-D0 rollup and its recomputation from call_fact
    (DESIGN.md 10.1 `test_ingest_counts`): 0 everywhere means the incremental upserts are exact."""
    out: dict[str, int] = {}
    for table, (stored, fresh) in ROLLUP_RECOMPUTE.items():
        q = (f"SELECT count(*) FROM ((({stored}) EXCEPT ({fresh})) UNION ALL "
             f"(({fresh}) EXCEPT ({stored}))) d")
        out[table] = int(conn.execute(q, {"tick": tick}).fetchone()[0])  # type: ignore[arg-type]
    return out


def event_totals(conn: Tx) -> dict[str, int]:
    """Counts the store can reconcile against `GET /sim/state` (DESIGN.md 10.1)."""
    r = conn.execute("""SELECT coalesce(sum(events), 0), coalesce(max(last_event_id), 0),
                               count(*) FROM ingested_chunk""").fetchone()
    return {"events": int(r[0]), "max_event_id": int(r[1]), "chunks": int(r[2])}
