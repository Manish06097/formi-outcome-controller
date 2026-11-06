"""`./run.sh <SIM_URL>` end to end through Docker (DESIGN.md 11.3, 15.1 M1 (1)): public_01 with the
no-op loop finishes with exit 0 and zero config changes.

Uses the compose project and Postgres port of track T1 (`formi_t1`, 55441) unless
CTL_T1_COMPOSE_PROJECT / CTL_T1_PG_PORT say otherwise; skipped without a Docker daemon.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import REPO

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="no Docker daemon")
def test_run_sh_public01_exit_0_zero_changes(sim_factory, tmp_path):
    sim = sim_factory()
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    env.update({"COMPOSE_PROJECT_NAME": os.environ.get("CTL_T1_COMPOSE_PROJECT", "formi_t1"),
                "CTL_PG_PORT": os.environ.get("CTL_T1_PG_PORT", "55441"),
                "CTL_MAX_RESTARTS": "2", "CTL_START_API": "0"})
    env.pop("TMPDIR", None)        # the docker CLI's plugin socket needs a short path
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        p = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, env=env, stdout=f,
                           stderr=subprocess.STDOUT, timeout=1800)
    assert p.returncode == 0, log.read_text()[-3000:]
    c = SimClient(sim.url)
    assert c.state().finished
    for spec in c.workflows():
        assert [v.version for v in c.config_history(spec.workflow_id)] == [1]
    assert "scenario finished" in log.read_text()
