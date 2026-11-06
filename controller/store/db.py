"""Postgres access: connection pool, transactions, the single-owner advisory lock, schema per
run and the migrations runner.

Owner: M0 (track T1 extends behaviour only through new modules). Implements DESIGN.md 2
("Single owner", "Where state lives"), 3 (the DDL in `migrations/`) and 8.5 (schema per run).

Usage, in the order the restart sequence needs it (DESIGN.md 2):

    db = Database.from_settings(settings)
    lock = AdvisoryLock(settings.env.dsn, settings.env.sim_url)
    lock.acquire(poll_s=settings.env.lock_poll_s)     # before anything reads or creates schemas
    db.ensure_ctl()
    schema = <resume a candidate from db.run_candidates(fp) or db.create_run_schema(fp, url)>
    db.open(schema)                                    # pool bound to the run schema
    with db.tx() as conn:                              # one transaction; commits on success
        conn.execute("INSERT INTO decision ...")

Migrations: `migrations/NNNN_name.sql`, applied in order. The first line of each file names its
scope: `-- scope: ctl` (the shared control schema) or `-- scope: run` (applied into every run
schema with search_path set to it, so names are unqualified). Each schema records what it has
applied in its own `schema_migrations`; an applied file whose checksum changed is refused,
because a run schema must never silently diverge from the DDL that built it.
"""
from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
CTL_SCHEMA = "ctl"
_SCHEMA_RE = re.compile(r"^run_[0-9a-f]{12}_[0-9]+$")
_SCOPE_RE = re.compile(r"^--\s*scope:\s*(ctl|run)\s*$")


class MigrationError(RuntimeError):
    """A migration file is malformed, or an applied migration's checksum changed."""


# =============================================================================== migrations

@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    scope: str
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migration files, sorted by version, with their scope and checksum."""
    out: list[Migration] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        text = path.read_text()
        first = text.splitlines()[0] if text else ""
        m = _SCOPE_RE.match(first)
        if not m:
            raise MigrationError(f"{path.name}: first line must be '-- scope: ctl' or '-- scope: run'")
        out.append(Migration(version=path.name[:4], name=path.stem, scope=m.group(1), sql=text,
                             checksum=hashlib.sha256(text.encode()).hexdigest()))
    versions = [m.version for m in out]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions in {directory}")
    return out


def apply_migrations(conn: psycopg.Connection[Any], scope: str, schema: str,
                     migrations: list[Migration] | None = None) -> list[str]:
    """Apply pending `scope` migrations into `schema`, inside the caller's transaction.

    Creates the schema and its `schema_migrations` table if needed. Returns the names applied.
    """
    migs = [m for m in (migrations or load_migrations()) if m.scope == scope]
    ident = sql.Identifier(schema)
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident))
    conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(ident))
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                        version text PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,
                        applied_wall timestamptz NOT NULL DEFAULT now())""")
    applied = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    done: list[str] = []
    for m in migs:
        if m.version in applied:
            if applied[m.version] != m.checksum:
                raise MigrationError(
                    f"{schema}: migration {m.name} was applied with a different checksum; "
                    "applied migrations are never edited, add a new file instead")
            continue
        conn.execute(m.sql)  # type: ignore[arg-type]  # multi-statement, no parameters
        conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                     (m.version, m.name, m.checksum))
        done.append(m.name)
    return done


# =============================================================================== run identity

def normalise_sim_url(url: str) -> str:
    """Canonical SIM_URL for the lock key and the registry: lower-case scheme and host,
    localhost -> 127.0.0.1, default ports and trailing slashes dropped."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if host == "localhost":
        host = "127.0.0.1"
    port = parts.port
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class RunRecord:
    """One `ctl.run_registry` row."""

    schema_name: str
    fingerprint: str
    sim_url: str
    status: str
    last_sim_minute: int | None
    created_wall: Any


# =============================================================================== advisory lock

class AdvisoryLock:
    """`pg_try_advisory_lock(hashtext('formi_ctl:' || normalised SIM_URL))` on a dedicated
    connection held for the life of the process (DESIGN.md 2). Session-level: the lock is
    released if the process dies, which is exactly what crash safety needs."""

    def __init__(self, dsn: str, sim_url: str):
        self.dsn = dsn
        self.key_text = f"formi_ctl:{normalise_sim_url(sim_url)}"
        self._conn: psycopg.Connection[Any] | None = None

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True,
                                         application_name="formi-controller-lock")
        return self._conn

    def try_acquire(self) -> bool:
        row = self._connection().execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                                          (self.key_text,)).fetchone()
        return bool(row and row[0])

    def acquire(self, poll_s: float = 5.0, timeout_s: float | None = None,
                on_wait: Callable[[float], None] | None = None) -> None:
        """Block until the lock is ours, polling every `poll_s` (a second controller waits
        instead of acting). Raises TimeoutError after `timeout_s` if given."""
        start = time.monotonic()
        while not self.try_acquire():
            waited = time.monotonic() - start
            if timeout_s is not None and waited >= timeout_s:
                raise TimeoutError(f"advisory lock {self.key_text!r} held by another controller")
            if on_wait:
                on_wait(waited)
            time.sleep(poll_s)

    def held(self) -> bool:
        """True if this session still holds the lock (the heartbeat checks it)."""
        if self._conn is None or self._conn.closed:
            return False
        try:
            row = self._conn.execute(
                """SELECT count(*) FROM pg_locks
                   WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
                     AND objsubid = 1
                     AND classid = ((hashtext(%s)::bigint >> 32) & 4294967295)::oid
                     AND objid = (hashtext(%s)::bigint & 4294967295)::oid""",
                (self.key_text, self.key_text)).fetchone()
        except psycopg.Error:
            return False
        return bool(row and row[0])

    def release(self) -> None:
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.key_text,))
            finally:
                self._conn.close()
        self._conn = None

    def __enter__(self) -> "AdvisoryLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# =============================================================================== database

class Database:
    """Pool, transactions and the schema-per-run lifecycle.

    Control-plane methods (`ensure_ctl`, `run_candidates`, `create_run_schema`, `update_run`,
    `drop_run_schema`) use short-lived connections. After `open(schema)`, `tx()` and
    `connection()` hand out pooled connections whose search_path is the run schema.
    """

    def __init__(self, dsn: str, pool_size: int = 3, application_name: str = "formi-controller"):
        self.dsn = dsn
        self.pool_size = pool_size
        self.application_name = application_name
        self._pool: ConnectionPool | None = None
        self.schema: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> "Database":
        return cls(settings.env.dsn, pool_size=settings.env.pg_pool_size)

    # ------------------------------------------------------------------ plain connections
    @contextmanager
    def connect(self, autocommit: bool = False,
                schema: str | None = None) -> Iterator[psycopg.Connection[Any]]:
        """A one-off connection (closed on exit; committed on success if not autocommit)."""
        kwargs: dict[str, Any] = {"autocommit": autocommit,
                                  "application_name": self.application_name}
        if schema:
            kwargs["options"] = f"-c search_path={_checked(schema)},public"
        with psycopg.connect(self.dsn, **kwargs) as conn:
            yield conn

    # ------------------------------------------------------------------ control plane
    def ensure_ctl(self) -> list[str]:
        """Create or migrate the `ctl` schema. Safe to call from two processes at once."""
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('formi_ctl:migrations'))")
                return apply_migrations(conn, "ctl", CTL_SCHEMA)

    def run_candidates(self, fingerprint: str) -> list[RunRecord]:
        """Registry rows for this fingerprint that are not done, newest first (8.5)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT schema_name, fingerprint, sim_url, status, last_sim_minute, created_wall
                   FROM ctl.run_registry WHERE fingerprint = %s AND status <> 'done'
                   ORDER BY created_wall DESC, schema_name DESC""", (fingerprint,)).fetchall()
        return [RunRecord(*r) for r in rows]

    def done_runs(self, fingerprint: str) -> list[RunRecord]:
        """Registry rows for this fingerprint that finished, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT schema_name, fingerprint, sim_url, status, last_sim_minute, created_wall
                   FROM ctl.run_registry WHERE fingerprint = %s AND status = 'done'
                   ORDER BY created_wall DESC, schema_name DESC""", (fingerprint,)).fetchall()
        return [RunRecord(*r) for r in rows]

    def create_run_schema(self, fingerprint: str, sim_url: str,
                          sim_days: int | None = None) -> str:
        """Create `run_<fp12>_<n+1>`, apply every run migration and register it, atomically.

        `sim_days` (end_minute // 1440 from the API) pre-creates the call_fact day partitions
        0..sim_days-1. Returns the schema name.
        """
        fp12 = fingerprint[:12].lower()
        if not re.fullmatch(r"[0-9a-f]{12}", fp12):
            raise ValueError("fingerprint must start with 12 hex characters")
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('formi_ctl:registry'))")
                apply_migrations(conn, "ctl", CTL_SCHEMA)
                rows = conn.execute(
                    """SELECT schema_name FROM ctl.run_registry WHERE schema_name LIKE %s
                       UNION SELECT nspname FROM pg_namespace WHERE nspname LIKE %s""",
                    (f"run_{fp12}_%", f"run_{fp12}_%")).fetchall()
                n = max((int(r[0].rsplit("_", 1)[1]) for r in rows), default=0) + 1
                schema = f"run_{fp12}_{n}"
                apply_migrations(conn, "run", schema)
                if sim_days:
                    conn.execute("SELECT ensure_call_fact_partitions(0, %s)", (sim_days - 1,))
                conn.execute(
                    """INSERT INTO ctl.run_registry (schema_name, fingerprint, sim_url, status)
                       VALUES (%s, %s, %s, 'active')""",
                    (schema, fingerprint, normalise_sim_url(sim_url)))
        return schema

    def migrate_run_schema(self, schema: str) -> list[str]:
        """Apply run migrations added since the schema was created (resume after an upgrade)."""
        with self.connect() as conn:
            with conn.transaction():
                return apply_migrations(conn, "run", _checked(schema))

    def update_run(self, schema: str, status: str | None = None,
                   last_sim_minute: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE ctl.run_registry
                   SET status = coalesce(%s, status),
                       last_sim_minute = coalesce(%s, last_sim_minute)
                   WHERE schema_name = %s""", (status, last_sim_minute, schema))

    def drop_run_schema(self, schema: str) -> None:
        """Drop a run schema and its registry row (old runs drop with one statement, 3)."""
        with self.connect() as conn:
            with conn.transaction():
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(_checked(schema))))
                conn.execute("DELETE FROM ctl.run_registry WHERE schema_name = %s", (schema,))

    # ------------------------------------------------------------------ data plane
    def open(self, schema: str, timeout_s: float = 30.0) -> None:
        """Bind the pool to a run schema (applying any pending run migrations first)."""
        self.migrate_run_schema(schema)
        self.close()
        self._pool = ConnectionPool(
            self.dsn, min_size=self.pool_size, max_size=self.pool_size, open=False,
            name=f"ctl-{schema}",
            kwargs={"options": f"-c search_path={_checked(schema)},public",
                    "application_name": self.application_name})
        self._pool.open(wait=True, timeout=timeout_s)
        self.schema = schema

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection[Any]]:
        """One transaction on a pooled connection: committed on normal exit, rolled back on
        an exception. Every state change and its audit row go through one of these."""
        with self.connection() as conn:
            with conn.transaction():
                yield conn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """A pooled connection without an explicit transaction (e.g. the loader's COPY)."""
        if self._pool is None:
            raise RuntimeError("Database.open(schema) has not been called")
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        self.schema = None


def _checked(schema: str) -> str:
    """Only run schemas and ctl may be bound; refuses anything that is not a plain identifier."""
    if schema != CTL_SCHEMA and not _SCHEMA_RE.match(schema):
        raise ValueError(f"not a run schema name: {schema!r}")
    return schema
