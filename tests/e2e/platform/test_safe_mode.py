"""Safe mode and the layers around it, end to end (AUDIT-1 finding 10, ADR 0019): a real simulator
over HTTP, the real controller, and the restart loop run.sh uses (`scripts/run_local.sh`, whose loop,
progress rule and limp mode mirror run.sh's; tests/e2e/platform/test_run_sh_docker.py runs a case
through Docker too).

The reproduction of the finding: a deterministic exception in one hook made every restart crash at
the same step, and the simulator stayed part-finished. Each test here breaks something on purpose and
asserts the scenario still finishes and the store says what happened.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import REPO, SHORT, query, registry

pytestmark = [pytest.mark.e2e, pytest.mark.pg]

FROM_D1 = 1440


def run_loop(sim_url: str, dsn: str, log: Path, env_extra: dict[str, str],
             timeout_s: float = 600) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    env.update({"PG_DSN": dsn, "CTL_PYTHON": sys.executable, "CTL_RESTART_SLEEP_S": "0",
                "CTL_SIM_WAIT": "60", "CTL_HEARTBEAT_S": "2", "CTL_LOCK_POLL_S": "1",
                "CTL_LEARNER": "none", "CTL_CODE_VERSION": "test", "PYTHONUNBUFFERED": "1",
                **env_extra})
    with open(log, "ab") as f:
        return subprocess.run([str(REPO / "scripts" / "run_local.sh"), sim_url], cwd=REPO, env=env,
                              stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s)


def only_schema(dsn: str) -> str:
    rows = registry(dsn)
    assert len(rows) == 1, rows
    assert rows[0][2] == "done", rows
    return rows[0][0]


def audit_actions(dsn: str, schema: str, *actions: str) -> list[tuple]:
    return query(dsn, schema, """SELECT action, subject_id, summary FROM audit_log
                                 WHERE action = ANY(%s) ORDER BY audit_id""", (list(actions),))


def assert_finished(sim_url: str) -> None:
    st = SimClient(sim_url).state()
    assert st.finished and st.sim_minute == st.end_minute


def page(dsn: str, schema: str, code: str) -> list[tuple]:
    return query(dsn, schema, """SELECT severity, dedupe_key, message FROM alert
                                 WHERE code = %s AND closed_wall IS NULL""", (code,))


@pytest.mark.parametrize("point", ["detect", "lifecycle", "govern", "verify", "plan", "decide"])
def test_a_hook_that_always_raises_does_not_stop_the_run(sim_factory, db_dsn, tmp_path, point):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": f"{point}:{FROM_D1}:raise"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert_finished(sim.url)
    schema = only_schema(db_dsn)

    ((state, since, skips),) = query(db_dsn, schema, """SELECT state, since_minute, skips
                                                        FROM hook_guard WHERE point = %s""", (point,))
    assert state == "guarded" and since >= FROM_D1 and skips >= 1
    # the restart budget cured nothing, so it took exactly two restarts to reach the guard
    assert text.count("restart 1") == 1 and text.count("restart 2") == 1
    assert "restart 3" not in text and "limp mode" not in text
    entered = audit_actions(db_dsn, schema, "safe_mode_entered")
    assert [(a, s) for a, s, _ in entered] == [("safe_mode_entered", point)]
    assert "deterministic failure" in entered[0][2]
    assert len(audit_actions(db_dsn, schema, "hook_skipped")) == skips
    assert [r[:2] for r in page(db_dsn, schema, "SAFE_MODE")] == [("page", point)]
    decisions = query(db_dsn, schema, """SELECT rule_id, outcome->>'state' FROM decision
                                         WHERE kind = 'safe_mode'""")
    assert decisions == [("hook_crashes_at_one_step", "guarded")]
    # every other stage kept running to the end and the attempts table is empty again
    assert query(db_dsn, schema, "SELECT count(*) FROM hook_attempt WHERE started > raised "
                                 "AND point = %s", (point,)) == [(0,)]
    last_step = query(db_dsn, schema, "SELECT max(step_no) FROM loop_step")[0][0]
    stages = {r[0] for r in query(db_dsn, schema, "SELECT stage FROM loop_stage WHERE step_no = %s",
                                  (last_step,))}
    assert stages == {"lifecycle", "detect", "decide", "govern", "verify"}


def test_governance_lifecycle_keeps_running_when_the_learner_breaks(sim_factory, db_dsn, tmp_path):
    """Whole public_01 with the probe learner. DECIDE breaks on D1: what was submitted before still
    gets decided and applied by LIFECYCLE, and nothing is left in flight."""
    sim = sim_factory()
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_LEARNER": "probe",
                                        "CTL_HOOK_FAULT": f"decide:{FROM_D1}:raise"})
    assert p.returncode == 0, log.read_text(errors="replace")[-3000:]
    assert_finished(sim.url)
    schema = only_schema(db_dsn)
    assert query(db_dsn, schema, "SELECT state FROM hook_guard WHERE point = 'decide'") == [
        ("guarded",)]
    assert query(db_dsn, schema, "SELECT count(*) FROM intent WHERE status = 'started'") == [(0,)]
    created = query(db_dsn, schema, "SELECT count(*) FROM proposal")[0][0]
    applied = query(db_dsn, schema, "SELECT count(*) FROM proposal WHERE state IN "
                                    "('APPLIED', 'VERIFYING', 'VERIFIED')")[0][0]
    assert created > 0 and applied > 0, (created, applied)
    client = SimClient(sim.url)
    assert any(len(client.config_history(s.workflow_id)) > 1 for s in client.workflows())
    # no double applies, no rejected auto-applies: governance stayed correct in safe mode
    assert query(db_dsn, schema, """SELECT count(*) FROM alert WHERE code = 'GOV_INVARIANT'""") == [(0,)]


def test_attach_failure_runs_the_bare_loop(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": "attach:-100000:raise"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert_finished(sim.url)
    schema = only_schema(db_dsn)
    assert "the loop continues without governance" in text
    assert query(db_dsn, schema, "SELECT state FROM hook_guard WHERE point = 'attach'") == [
        ("guarded",)]


def test_process_death_inside_a_hook_disables_it(sim_factory, db_dsn, tmp_path):
    """No exception to catch (out of memory, a native crash): after the hard limit the hook is not
    called again. The healthy stages after it still run at that step."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": f"detect:{FROM_D1}:kill",
                                        "CTL_SAFE_MODE_HARD_CRASHES": "3"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert_finished(sim.url)
    schema = only_schema(db_dsn)
    ((state, skips, reason),) = query(db_dsn, schema, """SELECT state, skips, reason FROM hook_guard
                                                         WHERE point = 'detect'""")
    assert state == "disabled" and skips >= 1 and "died 3 times" in reason
    assert text.count("exited 137") == 3


def test_a_hang_is_cut_short_and_the_hook_disabled(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": f"govern:{FROM_D1}:hang",
                                        "CTL_STAGE_WATCHDOG_S": "2"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert_finished(sim.url)
    schema = only_schema(db_dsn)
    ((state, reason),) = query(db_dsn, schema, "SELECT state, reason FROM hook_guard "
                                               "WHERE point = 'govern'")
    assert state == "disabled" and "hung 2 times" in reason


def test_a_core_crash_loop_ends_in_limp_mode(sim_factory, db_dsn, tmp_path):
    """A failure outside every hook cannot be skipped: when the restart budget runs out with no
    progress, limp mode still finishes the scenario, says DEGRADED and leaves a record."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": f"core:{FROM_D1}:raise",
                                        "CTL_MAX_RESTARTS": "3"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert "RESULT scenario finished DEGRADED" in text
    assert_finished(sim.url)
    rows = registry(db_dsn)
    assert len(rows) == 1 and rows[0][2] == "active"
    schema = rows[0][0]
    assert [a for a, _, _ in audit_actions(db_dsn, schema, "limp_mode")] == ["limp_mode"]
    assert len(page(db_dsn, schema, "LIMP_MODE")) == 1


def test_limp_can_be_turned_off(sim_factory, db_dsn, tmp_path):
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_HOOK_FAULT": f"core:{FROM_D1}:raise",
                                        "CTL_MAX_RESTARTS": "2", "CTL_LIMP": "0"})
    assert p.returncode == 1
    assert not SimClient(sim.url).state().finished


def test_restart_budget_resets_on_progress(sim_factory, db_dsn, tmp_path):
    """Four crashes spread over the run with a budget of two: every one came after progress, so the
    run is not a crash loop (before the fix `restarts` was never reset and this ended unfinished)."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    faults = ",".join(f"advance:3:{phase}" for phase in
                      ("before_t1", "after_t1", "after_http", "in_t2"))
    p = run_loop(sim.url, db_dsn, log, {"CTL_FAULT": faults, "CTL_MAX_RESTARTS": "2"})
    text = log.read_text(errors="replace")
    assert p.returncode == 0, text[-3000:]
    assert text.count("exited 137") == 4 and "limp mode" not in text
    assert_finished(sim.url)
    only_schema(db_dsn)


def test_random_kills_do_not_put_a_healthy_hook_in_safe_mode(sim_factory, db_dsn, tmp_path):
    """Two process deaths at one point, below the hard limit: the crash semantics of the kill test
    stay as they were, and no hook is switched off for someone's kill -9."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    p = run_loop(sim.url, db_dsn, log, {"CTL_FAULT": "bootstrap:1:mid,advance:2:after_http"})
    assert p.returncode == 0, log.read_text(errors="replace")[-3000:]
    schema = only_schema(db_dsn)
    assert query(db_dsn, schema, "SELECT count(*) FROM hook_guard") == [(0,)]
    assert page(db_dsn, schema, "SAFE_MODE") == []


# ------------------------------------------------------------------ the simulator goes away

def test_a_dead_simulator_ends_the_run_in_bounded_time(sim_factory, db_dsn, tmp_path):
    """AUDIT-1 finding 15: GETs retried forever, so run.sh never exited. Now the controller exits 1
    after its budget, the restart finds no simulator, and the loop exits 3."""
    sim = sim_factory()
    log = tmp_path / "loop.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CTL_")}
    env.update({"PG_DSN": db_dsn, "CTL_PYTHON": sys.executable, "CTL_RESTART_SLEEP_S": "0",
                "CTL_LEARNER": "none", "CTL_GET_RETRY_BUDGET_S": "6", "CTL_SIM_WAIT": "4",
                "CTL_LIMP_SIM_WAIT": "2", "CTL_HEALTH_POLL_S": "1", "PYTHONUNBUFFERED": "1"})
    with open(log, "ab") as f:
        proc = subprocess.Popen([str(REPO / "scripts" / "run_local.sh"), sim.url], cwd=REPO, env=env,
                                stdout=f, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            client = SimClient(sim.url)
            while client.state().sim_minute == 0:
                assert time.monotonic() < deadline, "the controller never advanced"
                time.sleep(0.2)
            sim.stop()
            t0 = time.monotonic()
            assert proc.wait(timeout=120) == 3, log.read_text(errors="replace")[-3000:]
            assert time.monotonic() - t0 < 60
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
    text = log.read_text(errors="replace")
    assert "controller exited 1" in text
    assert "never answered /health" in text


# ------------------------------------------------------------------ a second run after the first

def test_rerun_after_finish_exits_at_once_without_a_new_schema(sim_factory, db_dsn, tmp_path):
    """AUDIT-1 finding 11: the second controller used to create a fresh schema and re-ingest the whole
    run before exiting 0. A finished simulator with a done schema is now a no-op."""
    sim = sim_factory(overrides=SHORT)
    log = tmp_path / "loop.log"
    assert run_loop(sim.url, db_dsn, log, {}).returncode == 0
    schema = only_schema(db_dsn)
    t0 = time.monotonic()
    assert run_loop(sim.url, db_dsn, log, {}).returncode == 0
    assert time.monotonic() - t0 < 15
    assert only_schema(db_dsn) == schema
    assert len(audit_actions(db_dsn, schema, "run_already_done")) == 1
