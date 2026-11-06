"""Helpers for the platform end-to-end tests (track T1): a throwaway database per test, the real
controller as a subprocess (`python -m controller`, exactly what run_local.sh runs), and store
dumps for crash-equivalence and determinism comparisons.

Tests may read the simulator's run directory; the controller never does.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parents[3]
SIM_PORT = int(os.environ.get("CTL_T1_SIM_PORT", "8711"))
SHORT = {"days": 2, "history_days": 2}
"""public_01 shortened: two history days and two run days (about 30 steps)."""

# Wall-clock columns: everything else in a run schema is a function of the world and the code.
WALL_COLUMNS = {
    "ingested_chunk": {"ingested_wall", "txn_ms"},
    "intent": {"started_wall", "finished_wall"},
    "audit_log": {"wall"},
    "loop_step": {"phase_ms", "rss_mb", "db_bytes", "wall"},
    "alert": {"opened_wall", "closed_wall"},
}
SKIP_TABLES = {"heartbeat", "schema_migrations", "stage_raw"}

DOMAIN_TABLES = ("cell", "campaign", "lead_dim", "call_fact", "config_event", "approval_event",
                 "event_other", "config_version", "config_cohort", "r_engage", "r_pickup",
                 "r_did", "r_flags", "workflow", "allocation_provenance")
"""The tables a crash must leave exactly as a clean run does (facts, rollups, catalog)."""


def with_dbname(dsn: str, dbname: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    return " ".join(parts + [f"dbname={dbname}"])


def create_database(base_dsn: str) -> str:
    name = f"t1_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return with_dbname(base_dsn, name)


def drop_database(base_dsn: str, dsn: str) -> None:
    name = next(p.split("=", 1)[1] for p in dsn.split() if p.startswith("dbname="))
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))


@dataclass
class Run:
    exit_codes: list[int]
    log: str

    @property
    def final(self) -> int:
        return self.exit_codes[-1]


def controller_env(sim_url: str, dsn: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    env.update({"CTL_SIM_URL": sim_url, "PG_DSN": dsn, "CTL_SIM_WAIT": "60",
                "CTL_HEARTBEAT_S": "2", "CTL_LOCK_POLL_S": "1", "PYTHONUNBUFFERED": "1",
                "CTL_CODE_VERSION": "test"})
    env.update(extra or {})
    return env


def run_controller(sim_url: str, dsn: str, log_path: Path, extra: dict[str, str] | None = None,
                   timeout_s: float = 900, max_restarts: int = 0) -> Run:
    """Run `python -m controller`, restarting it like run.sh after any exit other than 0 or 3,
    at most `max_restarts` times."""
    codes: list[int] = []
    env = controller_env(sim_url, dsn, extra)
    with open(log_path, "ab") as log:
        while True:
            p = subprocess.run([sys.executable, "-m", "controller"], cwd=REPO, env=env,
                               stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
            codes.append(p.returncode)
            if p.returncode in (0, 3) or len(codes) > max_restarts:
                break
    return Run(codes, log_path.read_text(errors="replace"))


def registry(dsn: str) -> list[tuple[str, str, str, Any]]:
    with psycopg.connect(dsn) as conn:
        return [tuple(r) for r in conn.execute(
            """SELECT schema_name, fingerprint, status, last_sim_minute FROM ctl.run_registry
               ORDER BY created_wall, schema_name""")]


def latest_schema(dsn: str) -> str:
    return registry(dsn)[-1][0]


def query(dsn: str, schema: str, q: str, params: Iterable[Any] | None = None) -> list[tuple]:
    with psycopg.connect(dsn, options=f"-c search_path={schema}") as conn:
        return [tuple(r) for r in conn.execute(q, params)]  # type: ignore[arg-type]


def table_names(dsn: str, schema: str) -> list[str]:
    rows = query(dsn, schema, """SELECT tablename FROM pg_tables WHERE schemaname = %s
                                 AND tablename NOT LIKE 'call_fact_%%' ORDER BY 1""", (schema,))
    return [r[0] for r in rows if r[0] not in SKIP_TABLES]


def dump_table(dsn: str, schema: str, table: str, exclude: set[str] | None = None) -> list[str]:
    """Every row as text, without the excluded columns, sorted."""
    cols = [r[0] for r in query(dsn, schema, """SELECT column_name FROM information_schema.columns
                                                WHERE table_schema = %s AND table_name = %s
                                                ORDER BY ordinal_position""", (schema, table))]
    keep = [c for c in cols if c not in (exclude or set())]
    q = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in keep), sql.Identifier(schema),
        sql.Identifier(table))
    with psycopg.connect(dsn) as conn:
        rows = [repr(tuple(r)) for r in conn.execute(q)]
    return sorted(rows)


def dump_schema(dsn: str, schema: str, tables: Iterable[str] | None = None) -> dict[str, list[str]]:
    names = list(tables) if tables is not None else table_names(dsn, schema)
    return {t: dump_table(dsn, schema, t, WALL_COLUMNS.get(t)) for t in names}


def diff_dumps(a: dict[str, list[str]], b: dict[str, list[str]], limit: int = 3) -> list[str]:
    out = []
    for t in sorted(set(a) | set(b)):
        ra, rb = set(a.get(t, [])), set(b.get(t, []))
        if ra != rb:
            out.append(f"{t}: {len(ra)} vs {len(rb)} rows; only first: {sorted(ra - rb)[:limit]}; "
                       f"only second: {sorted(rb - ra)[:limit]}")
    return out


def done_advances(dsn: str, schema: str) -> list[str]:
    return [r[0] for r in query(dsn, schema, """SELECT idem_key FROM intent
                                                WHERE kind = 'advance' AND status = 'done'
                                                ORDER BY intent_id""")]
