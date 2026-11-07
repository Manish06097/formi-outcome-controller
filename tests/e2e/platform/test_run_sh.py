"""`./run.sh <SIM_URL>` end to end through Docker (DESIGN.md 11.3, 15.1 M1 (1), ADR 0019), on whatever
daemon this machine has. Each backend finishes public_01 (shortened) with exit 0 and zero config
changes under the no-op learner, then stops what it started. tests/e2e/platform/test_linux_box.py
runs the same script on a real Linux network stack.

Uses the compose project and Postgres port of the environment (CTL_RUNSH_PROJECT, CTL_RUNSH_PG_PORT;
defaults formi_t1, 55441); skipped without a Docker daemon.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import REPO, SHORT

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

PROJECT = os.environ.get("CTL_RUNSH_PROJECT", os.environ.get("CTL_T1_COMPOSE_PROJECT", "formi_t1"))
PG_PORT = os.environ.get("CTL_RUNSH_PG_PORT", os.environ.get("CTL_T1_PG_PORT", "55441"))


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


skip_no_docker = pytest.mark.skipif(not _docker_ok(), reason="no Docker daemon")


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    env.update({"COMPOSE_PROJECT_NAME": PROJECT, "CTL_PG_PORT": PG_PORT, "CTL_MAX_RESTARTS": "2",
                "CTL_LEARNER": "none", **extra})
    env.pop("TMPDIR", None)        # the docker CLI's plugin socket needs a short path
    return env


def _containers(*filters: str) -> list[str]:
    args = ["docker", "ps", "-q"]
    for f in filters:
        args += ["--filter", f]
    return subprocess.run(args, capture_output=True, text=True).stdout.split()


def _project_containers() -> list[str]:
    plain = _containers(f"label=formi.project={PROJECT}")
    composed = _containers(f"label=com.docker.compose.project={PROJECT}")
    return plain + composed


def _assert_no_changes(url: str) -> None:
    c = SimClient(url)
    assert c.state().finished
    for spec in c.workflows():
        assert [v.version for v in c.config_history(spec.workflow_id)] == [1]


@skip_no_docker
@pytest.mark.parametrize("backend", ["compose2", "plain"])
def test_run_sh_finishes_and_stops_what_it_started(sim_factory, tmp_path, backend):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        p = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                           stderr=subprocess.STDOUT, timeout=1200,
                           env=_env(CTL_DOCKER_BACKEND=backend))
    text = log.read_text()
    assert p.returncode == 0, text[-3000:]
    assert f"backend {backend}" in text and "RESULT scenario finished" in text
    assert "DEGRADED" not in text
    _assert_no_changes(sim.url)
    # finding 15: nothing is left holding memory (the data stays in the volume)
    assert _project_containers() == [], text[-1000:]


@skip_no_docker
def test_run_sh_rerun_is_a_fast_no_op(sim_factory, tmp_path):
    """Finding 11: a second run.sh after the first finished exits 0 without re-ingesting."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        assert subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                              stderr=subprocess.STDOUT, timeout=1200, env=_env()).returncode == 0
    t0 = time.monotonic()
    with open(log, "ab") as f:
        assert subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                              stderr=subprocess.STDOUT, timeout=300, env=_env()).returncode == 0
    assert time.monotonic() - t0 < 120
    assert "has the whole run" in log.read_text()


@skip_no_docker
def test_sigterm_stops_the_controller_container(sim_factory, tmp_path):
    """Finding 11: run.sh killed left its container running and still driving the simulator. Now a
    SIGTERM stops it, promptly, and a later run.sh resumes the same run to the end."""
    sim = sim_factory()
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        proc = subprocess.Popen([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                                stderr=subprocess.STDOUT, env=_env(), start_new_session=True)
        try:
            client = SimClient(sim.url)
            deadline = time.monotonic() + 600
            while client.state().sim_minute == 0:
                assert time.monotonic() < deadline, log.read_text()[-2000:]
                assert proc.poll() is None, log.read_text()[-2000:]
                time.sleep(0.5)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=120) == 143, log.read_text()[-2000:]
            assert time.monotonic() - t0 < 90, "docker stop waited out its grace period"
        finally:
            if proc.poll() is None:
                proc.kill()
    assert _project_containers() == [], "the controller and Postgres are stopped"
    minute = SimClient(sim.url).state().sim_minute
    time.sleep(3)
    state = SimClient(sim.url).state()
    assert state.sim_minute == minute and not state.finished, "nothing drives the simulator now"
    with open(log, "ab") as f:
        again = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                               stderr=subprocess.STDOUT, timeout=1200, env=_env())
    assert again.returncode == 0, log.read_text()[-3000:]
    assert SimClient(sim.url).state().finished


@skip_no_docker
def test_sigkill_orphan_is_replaced_by_the_next_run(sim_factory, tmp_path):
    """SIGKILL cannot be trapped, so the container outlives run.sh. The next run.sh removes that
    orphan (the controller is crash-safe) and finishes the scenario."""
    sim = sim_factory()
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        proc = subprocess.Popen([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                                stderr=subprocess.STDOUT, env=_env(), start_new_session=True)
        client = SimClient(sim.url)
        deadline = time.monotonic() + 600
        while client.state().sim_minute == 0:
            assert time.monotonic() < deadline, log.read_text()[-2000:]
            time.sleep(0.5)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    with open(log, "ab") as f:
        again = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                               stderr=subprocess.STDOUT, timeout=1200, env=_env())
    assert again.returncode == 0, log.read_text()[-3000:]
    assert SimClient(sim.url).state().finished
    assert _project_containers() == []


@skip_no_docker
def test_keep_up_leaves_postgres_and_a_working_api(sim_factory, tmp_path):
    """CTL_KEEP_UP=1 is the documented way to look at a run afterwards. The API serves as its
    least-privilege role."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "run_sh.log"
    port = os.environ.get("CTL_RUNSH_API_PORT", "8791")
    with open(log, "wb") as f:
        p = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                           stderr=subprocess.STDOUT, timeout=1200,
                           env=_env(CTL_KEEP_UP="1", CTL_API_PORT=port))
    assert p.returncode == 0, log.read_text()[-3000:]
    try:
        import httpx
        deadline = time.monotonic() + 60
        while True:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/ops", timeout=5)
                if r.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "the API never answered"
            time.sleep(1)
        assert _project_containers(), "Postgres and the API stay up"
    finally:
        subprocess.run(["docker", "compose", "-f", "compose.yml", "--profile", "api", "stop"], cwd=REPO,
                       env=_env(CTL_SIM_URL="x", CTL_API_PORT=port), capture_output=True)


def _bin_without(tmp_path: Path, *hidden: str) -> str:
    """A PATH holding only symlinks to what run.sh needs, leaving out the named commands."""
    out = tmp_path / "bin"
    out.mkdir(exist_ok=True)
    for name in ("bash", "sh", "env", "sed", "grep", "cut", "cksum", "seq", "uname", "git", "tail",
                 "mktemp", "rm", "head", "sleep", "cat", "dirname", "python3", "docker"):
        found = shutil.which(name)
        if found and name not in hidden:
            (out / name).symlink_to(found)
    return str(out)


@skip_no_docker
def test_no_docker_on_the_path_still_finishes_the_scenario_degraded(sim_factory, tmp_path):
    """Docker missing is the worst setup: limp mode from the box's own Python finishes the scenario
    and says so."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        p = subprocess.run([str(REPO / "run.sh"), sim.url], cwd=REPO, stdout=f,
                           stderr=subprocess.STDOUT, timeout=600,
                           env=_env(PATH=_bin_without(tmp_path, "docker")))
    text = log.read_text()
    assert p.returncode == 0, text[-3000:]
    assert "docker not found" in text and "RESULT scenario finished DEGRADED" in text
    _assert_no_changes(sim.url)


@skip_no_docker
def test_fatal_when_the_simulator_never_answers(tmp_path):
    """Nothing can finish a scenario that is not there: exit 3, in bounded time, nothing left up."""
    log = tmp_path / "run_sh.log"
    with open(log, "wb") as f:
        p = subprocess.run([str(REPO / "run.sh"), "http://127.0.0.1:9"], cwd=REPO, stdout=f,
                           stderr=subprocess.STDOUT, timeout=600,
                           env=_env(CTL_SIM_WAIT="3", CTL_LIMP_SIM_WAIT="2", CTL_HEALTH_POLL_S="1"))
    assert p.returncode == 3, log.read_text()[-3000:]
    assert _project_containers() == []
