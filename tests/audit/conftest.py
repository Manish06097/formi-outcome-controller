"""Fixtures for the T5 audit, API and alert tests: one hand-filled run schema per session."""
from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from controller.store.db import Database
from tests.audit import fixture_world


@pytest.fixture(scope="session")
def world(pg_dsn):
    schema = fixture_world.build(pg_dsn)
    try:
        yield {"dsn": pg_dsn, "schema": schema, "api_dsn": fixture_world.api_dsn(pg_dsn)}
    finally:
        Database(pg_dsn).drop_run_schema(schema)


@pytest.fixture()
def conn(world):
    """A connection on the fixture run schema, inside a transaction that is rolled back."""
    with psycopg.connect(world["dsn"]) as c:
        c.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(world["schema"])))
        yield c
        c.rollback()
