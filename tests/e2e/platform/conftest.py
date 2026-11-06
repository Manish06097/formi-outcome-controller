"""Fixtures of the platform end-to-end tests (track T1)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from tests.e2e.platform.helpers import SIM_PORT, create_database, drop_database
from tests.support.sim_process import SimProcess


@pytest.fixture()
def db_dsn(pg_dsn: str) -> Iterator[str]:
    """A fresh, empty database per test, so each test sees its own ctl.run_registry."""
    dsn = create_database(pg_dsn)
    try:
        yield dsn
    finally:
        drop_database(pg_dsn, dsn)


@pytest.fixture()
def sim_factory(tmp_path: Path) -> Iterator[Callable[..., SimProcess]]:
    """Start real simulators on the T1 port; each call stops the previous one first."""
    procs: list[SimProcess] = []

    def start(scenario: str = "scenarios/public_01_dev.yaml",
              overrides: dict[str, Any] | None = None, name: str = "sim") -> SimProcess:
        for p in procs:
            p.stop()
        proc = SimProcess(scenario, tmp_path / f"{name}{len(procs)}", port=SIM_PORT,
                          overrides=overrides, ready_timeout_s=600).start()
        procs.append(proc)
        return proc

    try:
        yield start
    finally:
        for p in procs:
            p.stop()
