"""Postgres for tests.

Resolution order:
1. `CTL_TEST_PG_DSN` (explicit; for example the per-track container of the build brief).
2. Otherwise start a throwaway `postgres:16` container on an OS-assigned loopback port and stop
   it at the end of the session (`docker run --rm`), so tests never touch a database they did
   not create.
If neither works, tests that need Postgres are skipped with the reason.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import psycopg


def _wait_ready(dsn: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except psycopg.Error as e:  # starting up
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Postgres at {dsn} not ready: {last}")


class PostgresForTests:
    """A Postgres DSN for the session; `stop()` removes a container this object started."""

    def __init__(self) -> None:
        self.container: str | None = None
        self.dsn: str | None = None
        self.skip_reason: str | None = None

    def start(self) -> "PostgresForTests":
        explicit = os.environ.get("CTL_TEST_PG_DSN")
        if explicit:
            _wait_ready(explicit)
            self.dsn = explicit
            return self
        if not shutil.which("docker"):
            self.skip_reason = "no CTL_TEST_PG_DSN and no docker"
            return self
        name = f"formi_test_pg_{uuid.uuid4().hex[:8]}"
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                            "-e", "POSTGRES_PASSWORD=ctl", "-p", "127.0.0.1::5432", "postgres:16"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skip_reason = f"docker run failed: {r.stderr.strip()}"
            return self
        self.container = name
        port = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True,
                              text=True, check=True).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        self.dsn = f"host=127.0.0.1 port={port} user=postgres password=ctl dbname=postgres"
        _wait_ready(self.dsn)
        return self

    def stop(self) -> None:
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.container = None
