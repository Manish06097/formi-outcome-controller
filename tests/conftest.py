"""Session fixtures shared by every suite (M0). Tracks add fixtures in their own conftest.py
files under their test directories rather than editing this one."""
from __future__ import annotations

import pytest

from tests.support.pg import PostgresForTests


@pytest.fixture(scope="session")
def pg_dsn():
    """A DSN of a Postgres the tests may freely create schemas in (see tests/support/pg.py)."""
    pg = PostgresForTests().start()
    if pg.dsn is None:
        pytest.skip(pg.skip_reason or "no Postgres")
    try:
        yield pg.dsn
    finally:
        pg.stop()
