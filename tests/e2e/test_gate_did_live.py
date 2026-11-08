"""A caller-ID change whose DID line fails is never applied (DESIGN 0.1, ADR 0002, GATE-DID).

Found on public_02: nw_renewal v8 (a P3 spread raising D4 from 2.8% to 21.6%) was auto-applied
although its approval package read "Harm gate FAILED ... failed ['did']". The release train
recorded the DID line of the composed gate and enforced it only when a change raised an unproven
DID, so a spread onto an SPRT-cleared DID went out on the pickup learner's own cut alone, which
had been optimised on different draws than the ones that judge the change at release.

Here the real controller (`CTL_LEARNER=probe`) drives a real simulator over HTTP with the
learner's own cut made too generous (tests/support/controller_generous_did_cut.py), so a spread
among healthy DIDs reaches the release train with a failing DID line. Then:
- at least one composed change failed its DID line and was held (HARM_GATE_BLOCK names 'did'),
  proving the scenario reaches the case;
- no applied non-P0 caller-ID change carries a package that says FAILED, and no applied
  caller-ID change has a stored composed-gate row whose failed lines include a DID line;
- the grader finds no harmful change.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from sim.grader import grade
from tests.e2e.platform.helpers import (REPO, SIM_PORT, controller_env, create_database,
                                        drop_database, latest_schema, query)
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIO = "scenarios/public_02_incidents.yaml"
P0 = 0


@pytest.fixture(scope="module")
def world(pg_dsn, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("gate_did")
    dsn = create_database(pg_dsn)
    base = workdir / "baseline"
    subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(REPO / SCENARIO),
                    "--run-dir", str(base)], cwd=REPO, check=True, capture_output=True)
    sim = SimProcess(SCENARIO, workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        log = workdir / "controller.log"
        with open(log, "ab") as fh:
            p = subprocess.run([sys.executable, "-m", "tests.support.controller_generous_did_cut"],
                               cwd=REPO, env=controller_env(sim.url, dsn, {"CTL_LEARNER": "probe"}),
                               stdout=fh, stderr=subprocess.STDOUT, timeout=1800)
        yield {"dsn": dsn, "schema": latest_schema(dsn), "code": p.returncode,
               "log": log.read_text(errors="replace"),
               "grade": grade(str(sim.run_dir), str(base))}
    finally:
        sim.stop()
        drop_database(pg_dsn, dsn)


def _q(w, sql):
    return query(w["dsn"], w["schema"], sql)


def test_run_finishes_and_nothing_is_harmful(world):
    assert world["code"] == 0, world["log"][-3000:]
    harmful = [c for c in world["grade"]["controller_changes"]["detail"]
               if c["verdict"] == "harmful"]
    assert harmful == [], harmful


def test_a_failing_did_line_was_held(world):
    rows = _q(world, """SELECT message FROM alert WHERE code = 'HARM_GATE_BLOCK'
                        AND message LIKE '%''did''%'""")
    assert rows, "the scenario never produced a caller-ID change failing its DID line"


def test_no_applied_caller_id_change_failed_its_did_line(world):
    applied = _q(world, """SELECT p.proposal_id, p."class", p.rationale, p.applied_version
                           FROM proposal p JOIN config_version c
                             ON c.wf = p.wf AND c.version = p.applied_version
                           WHERE 'caller_id' = ANY (c.changed_dims) AND c.actor LIKE 'controller%'""")
    assert applied
    bad = []
    for pid, cls, rationale, version in applied:
        if cls == P0:
            continue                      # incident work is judged and recorded, never held
        if "FAILED" in (rationale or "") and "'did'" in (rationale or ""):
            bad.append((pid, version, rationale[-200:]))
        rows = _q(world, f"""SELECT d.decision_id, d.harm_report->'failed'
                             FROM decision d JOIN patch pt ON d.inputs->'patch_ids' @> to_jsonb(pt.patch_id)
                             WHERE d.rule_id = '0.1/harm_gate' AND pt.proposal_id = '{pid}'
                             ORDER BY d.decision_id DESC LIMIT 1""")
        for did, failed in rows:
            if any(f in ("did", "did_unmodeled", "apply_time:did") for f in (failed or [])):
                bad.append((pid, version, did, failed))
    assert not bad, bad
