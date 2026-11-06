"""FastAPI JSON views for supervisors and operators, plus supervisor holds.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.3 (supervisor view, holds), 9.4
(`/explain/*`) and 9.1 (`/ops`). No HTML page (cut, ADR 0023).

Least privilege, enforced twice:
1. The API connects as role `ctl_api`, which migrations grant SELECT on the run schema and
   INSERT and UPDATE on `hold` only (0008_api_role.sql). It cannot write a decision, a proposal
   or an approval, so a bug or an attacker here cannot change what the controller does except
   through a hold, which the controller reads at every step.
2. Before serving a run schema, the app checks the privileges it actually has and refuses
   (HTTP 503) if it could write anything other than `hold`, or if it connected as a superuser
   or the schema owner (set CTL_API_ALLOW_OWNER=1 only for local debugging). Every read runs in
   a READ ONLY transaction.

Environment:
    CTL_API_PG_DSN       DSN for role ctl_api (default: CTL_PG_* host/port/db with user
                         ctl_api and password $CTL_API_PG_PASSWORD)
    CTL_API_SCHEMA       pin a run schema (default: the newest active run in ctl.run_registry,
                         looked up per request, so a new run is picked up without a restart)
    CTL_API_ALLOW_OWNER  1 to skip the least-privilege refusal (local debugging only)

Run: `uvicorn --factory controller.api.app:create_app --port $CTL_API_PORT`.
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

import psycopg
from fastapi import FastAPI, HTTPException, Query
from psycopg import sql
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from controller.api import views
from controller.audit import explain

_SCHEMA_RE = re.compile(r"^run_[0-9a-f]{12}_[0-9]+$")
WRITABLE_TABLES = {"hold": {"INSERT", "UPDATE"}}
"""The only writes the API role may hold (DESIGN.md 2, 9.3)."""
WRITE_PRIVS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


class LeastPrivilegeError(RuntimeError):
    """The API's database role can write more than `hold`."""


def default_dsn() -> str:
    explicit = os.environ.get("CTL_API_PG_DSN")
    if explicit:
        return explicit
    from controller.settings import load_settings
    env = load_settings().env
    pw = os.environ.get("CTL_API_PG_PASSWORD", "")
    return (f"host={env.pg_host} port={env.pg_port} dbname={env.pg_db} user=ctl_api "
            f"password={pw}")


def check_privileges(conn: Any, schema: str) -> list[str]:
    """Problems with the connected role's privileges on `schema` (empty = least privilege)."""
    problems: list[str] = []
    who = conn.execute("SELECT current_user, rolsuper FROM pg_roles "
                       "WHERE rolname = current_user").fetchone()
    if who and who[1]:
        problems.append(f"connected as superuser {who[0]}")
    owner = conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s",
                         (schema,)).fetchone()
    if owner and who and owner[0] == who[0]:
        problems.append(f"connected as the owner of {schema}")
    rows = conn.execute(
        """SELECT c.relname, p.priv
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           CROSS JOIN unnest(%s::text[]) AS p(priv)
           WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
             AND has_table_privilege(c.oid, p.priv)""", (list(WRITE_PRIVS), schema)).fetchall()
    for rel, priv in rows:
        if priv not in WRITABLE_TABLES.get(rel, set()):
            problems.append(f"can {priv} {schema}.{rel}")
    return problems


class HoldIn(BaseModel):
    tenant_id: str
    workflow_id: str
    set_by: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)
    until: str | int | None = Field(default=None, description='"D3 18:00", a sim minute, or none')


class HoldRelease(BaseModel):
    released_by: str = Field(min_length=1, max_length=200)


class Api:
    """Connection handling and schema resolution, shared by the routes."""

    def __init__(self, dsn: str, schema: str | None = None, allow_owner: bool = False,
                 pool_size: int = 2):
        self.pinned = schema
        self.allow_owner = allow_owner
        self.pool = ConnectionPool(dsn, min_size=1, max_size=pool_size, open=False,
                                   kwargs={"application_name": "formi-api", "autocommit": False})
        self._checked: dict[str, list[str]] = {}

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    def _schema(self, conn: Any) -> str:
        schema = self.pinned
        if schema is None:
            row = conn.execute("SELECT schema_name FROM ctl.run_registry WHERE status = 'active' "
                               "ORDER BY created_wall DESC, schema_name DESC LIMIT 1").fetchone()
            schema = row[0] if row else None
        if not schema or not _SCHEMA_RE.match(schema):
            raise HTTPException(503, "no active run schema yet")
        if schema not in self._checked:
            self._checked[schema] = check_privileges(conn, schema)
        problems = self._checked[schema]
        if problems and not self.allow_owner:
            raise HTTPException(503, "refusing to serve: the API role is not least-privilege: "
                                + "; ".join(problems))
        return schema

    @contextmanager
    def tx(self, write: bool = False) -> Iterator[Any]:
        """A transaction on the current run schema; READ ONLY unless `write`."""
        with self.pool.connection() as conn:
            with conn.transaction():
                if not write:
                    conn.execute("SET TRANSACTION READ ONLY")
                schema = self._schema(conn)
                conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(
                    sql.Identifier(schema)))
                yield conn


def create_app(dsn: str | None = None, schema: str | None = None,
               allow_owner: bool | None = None) -> FastAPI:
    """The app (uvicorn --factory). Arguments override the environment (tests use them)."""
    api = Api(dsn or default_dsn(), schema or os.environ.get("CTL_API_SCHEMA"),
              allow_owner if allow_owner is not None
              else os.environ.get("CTL_API_ALLOW_OWNER") == "1")
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        api.open()
        try:
            yield
        finally:
            api.close()

    app = FastAPI(title="Formi controller: supervisor and ops views", version="1",
                  lifespan=lifespan)
    app.state.api = api

    def _not_found(e: Exception) -> HTTPException:
        return HTTPException(404, str(e))

    @app.get("/health")
    def health() -> dict[str, Any]:
        with api.tx() as conn:
            schema = conn.execute("SELECT current_schema()").fetchone()[0]
            return {"ok": True, "schema": schema, "role": conn.execute(
                "SELECT current_user").fetchone()[0], "privilege_problems": api._checked[schema]}

    @app.get("/supervisor/{tenant_id}")
    def supervisor(tenant_id: str) -> dict[str, Any]:
        with api.tx() as conn:
            try:
                return views.supervisor_view(conn, tenant_id)
            except views.TenantNotFound as e:
                raise HTTPException(404, f"unknown tenant {e}") from e

    @app.get("/supervisor/{tenant_id}/digest")
    def digest(tenant_id: str, until: str | None = None, days: int = Query(7, ge=1, le=60)
               ) -> dict[str, Any]:
        with api.tx() as conn:
            try:
                return explain.weekly_digest(conn, tenant_id, until, days).to_json()
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

    @app.post("/supervisor/holds", status_code=201)
    def create_hold(body: HoldIn) -> dict[str, Any]:
        with api.tx(write=True) as conn:
            row = conn.execute("SELECT wf, tenant_id FROM workflow WHERE workflow_id = %s",
                               (body.workflow_id,)).fetchone()
            if row is None or row[1] != body.tenant_id:
                raise HTTPException(404, f"no workflow {body.workflow_id} for tenant "
                                         f"{body.tenant_id}")
            now = explain.now_minute(conn) or 0
            try:
                until = None if body.until is None else explain.parse_when(body.until)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if until is not None and until <= now:
                raise HTTPException(422, "until must be after the current sim time "
                                         f"({explain.label(now)})")
            r = conn.execute(
                "INSERT INTO hold (wf, set_by, reason, from_minute, until_minute) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING hold_id",
                (row[0], body.set_by, body.reason, now, until)).fetchone()
            return {"hold_id": r[0], "workflow_id": body.workflow_id, "set_by": body.set_by,
                    "reason": body.reason, "from": explain.label(now),
                    "until": explain.when(until),
                    "effect": "pauses every non-incident change on this workflow; incident "
                              "fixes still go through approval"}

    @app.post("/supervisor/holds/{hold_id}/release")
    def release_hold(hold_id: int, body: HoldRelease) -> dict[str, Any]:
        with api.tx(write=True) as conn:
            now = explain.now_minute(conn) or 0
            r = conn.execute(
                "UPDATE hold SET released_minute = %s, "
                "reason = coalesce(reason, '') || %s "
                "WHERE hold_id = %s AND released_minute IS NULL RETURNING hold_id",
                (now, f" [released by {body.released_by} at {explain.label(now)}]",
                 hold_id)).fetchone()
            if r is None:
                raise HTTPException(404, f"no open hold {hold_id}")
            return {"hold_id": hold_id, "released": explain.label(now)}

    @app.get("/explain/change")
    def explain_change(workflow: str, at: str) -> dict[str, Any]:
        with api.tx() as conn:
            try:
                x = explain.why_changed(conn, workflow, at)
            except explain.NotFound as e:
                raise _not_found(e) from e
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return {**x.to_json(), "text": x.render()}

    @app.get("/explain/call")
    def explain_call(lead: str, day: int) -> dict[str, Any]:
        with api.tx() as conn:
            try:
                x = explain.why_heard(conn, lead, day)
            except explain.NotFound as e:
                raise _not_found(e) from e
            return {**x.to_json(), "text": x.render()}

    @app.get("/explain/decision/{decision_id}")
    def explain_decision(decision_id: int) -> dict[str, Any]:
        with api.tx() as conn:
            d = explain.fetch_one(conn.execute(
                "SELECT d.*, w.workflow_id FROM decision d LEFT JOIN workflow w USING (wf) "
                "WHERE d.decision_id = %s", (decision_id,)))
            if d is None:
                raise HTTPException(404, f"no decision {decision_id}")
            d["at"] = explain.label(d["sim_minute"])
            d["snapshot_ids"] = list(d["snapshot_ids"] or [])
            d["proposals"] = [r[0] for r in conn.execute(
                "SELECT proposal_id FROM proposal WHERE decision_id = %s ORDER BY proposal_id",
                (decision_id,)).fetchall()]
            d["replay"] = (f"python -m controller.audit replay-decision {decision_id} "
                           "(recomputes it from the stored inputs)")
            return d

    @app.get("/ops")
    def ops() -> dict[str, Any]:
        with api.tx() as conn:
            return views.ops_view(conn)

    return app
