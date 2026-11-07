"""Milestone M6 acceptance in the real loop (DESIGN.md 15.1 M6, integration step I5).

The real controller process (`python -m controller`, what run.sh runs in its container) with prompt
safety wired (`CTL_LEARNER=probe`: D0 prompt screens, the prompt trigger on supervisor edits, the
live flag CUSUM, two-stage verdicts with the designated gate-evidence run, one suggest-fixes call
per incident, the ranked P0a prompt fix through the release train, post-apply verification, credit
pools with LOW mode) drives a real simulator over HTTP with a real Postgres until the scenario
ends. The simulator's grader and event log then judge it:

- every scenario: the run finishes; governance hygiene stays clean (0 auto_apply_rejected,
  0 lapsed, 0 expired, 0 apply conflicts); the books balance (the credit ledger is exactly what the
  simulator charged); no request failed with a 402;
- public_02 (the P2-like supervisor prompt at D3 11:30): hallucination MTTR under 4 hours (the
  grader's `mttr_minutes`), credits used at most 40, 0 harmful changes;
- custom (e) `two_incidents_p02` (a caller ID flagged while the prompt-fix approval is pending):
  the prompt fix is never stale or superseded and its MTTR is under 4 hours;
- custom (f) P4-like supervisor prompt (one seed here; `scripts/i5_matrix.py` runs all 20): a
  finite MTTR;
- custom (i) unsafe business defaults: every unsafe default gets a fix proposed from a FULL verdict
  with a designated gate-evidence run;
- credits-50 world: LOW mode from the start, the fix still lands, no 402;
- a g = 0.99 world: the safe fix passes its tight-gate evidence run (n = 100) and is applied.

Scenarios are test fixtures (tests may read them); the controller never does. Prompt fixes are the
one change exempt from the engagement harm gate (7.1): the grader may call a fix away from a prompt
that engages better "harmful", so "0 harmful" is asserted on every change that is not a prompt fix
and, for public_02 and (e), on all of them.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sim.grader import grade
from tests.e2e.platform.helpers import REPO, SIM_PORT, latest_schema, query, run_controller
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIOS = {
    "public_02": "scenarios/public_02_incidents.yaml",
    "e_two_incidents": "scenarios/private/two_incidents_p02.yaml",
    "f_p4_like": "scenarios/private/p4_like_supervisor_p01_s6001.yaml",
    "i_unsafe_default": "scenarios/private/unsafe_default_prompt_p01.yaml",
    "credits50": "scenarios/private/credits50_p02.yaml",
    "gate099": "scenarios/private/gate099_p02.yaml",
}
MTTR_LIMIT_MIN = 240
FIX_BAD_STATES = ("stale", "superseded", "expired", "lapsed_local", "failed", "failed_policy")


def _baseline(scenario: str, out: Path) -> Path:
    run_dir = out / "baseline"
    subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(REPO / scenario),
                    "--run-dir", str(run_dir)], cwd=REPO, check=True, capture_output=True)
    return run_dir


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def world(request, pg_dsn, tmp_path_factory):
    from tests.e2e.platform.helpers import create_database, drop_database
    name = request.param
    workdir = tmp_path_factory.mktemp(f"m6_{name}")
    dsn = create_database(pg_dsn)
    sim = SimProcess(SCENARIOS[name], workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        run = run_controller(sim.url, dsn, workdir / "controller.log", {"CTL_LEARNER": "probe"},
                             timeout_s=1800)
        from controller.simclient import SimClient
        st = SimClient(sim.url).state()
    finally:
        sim.stop()
    try:
        schema = latest_schema(dsn)
        g = grade(str(sim.run_dir), str(_baseline(SCENARIOS[name], workdir)))
        yield {"name": name, "run": run, "state": st, "dsn": dsn, "schema": schema, "grade": g,
               "workdir": workdir}
    finally:
        drop_database(pg_dsn, dsn)


def _q(w: dict, sql: str) -> list[tuple]:
    return query(w["dsn"], w["schema"], sql)


def _only(*names: str):
    def deco(fn):
        def inner(world, *a, **k):
            if world["name"] not in names:
                pytest.skip(f"{', '.join(names)} only")
            return fn(world, *a, **k)
        inner.__name__ = fn.__name__
        return inner
    return deco


def _edits(w: dict) -> list[dict]:
    return [i for i in w["grade"]["incidents"] if i["type"] == "supervisor_edit"
            and i["workflow"] == "ks_cancel" or (i["type"] == "supervisor_edit"
                                                 and "mttr_minutes" in i)]


def _prompt_proposals(w: dict) -> list[tuple]:
    return _q(w, """SELECT p.proposal_id, x.workflow_id, p.state, p.evidence->>'validation_run_id',
                           p.submitted_minute, p.applied_version
                    FROM proposal p JOIN workflow x USING (wf)
                    WHERE p.class = 0 AND p.diff->'changed_dims' ? 'prompt_version'
                    ORDER BY p.submitted_minute""")


# =============================================================================== every scenario

def test_run_finishes_with_clean_governance(world):
    assert world["run"].exit_codes == [0], world["run"].log[-3000:]
    assert world["state"].finished
    g = world["grade"]["governance"]
    report = json.dumps(g, default=str)
    assert g["auto_apply_rejected"] == 0, report
    assert g["apply_conflicts"] == 0, report
    assert g["lapsed"] == 0, report
    assert g["decisions"].get("expired", 0) == 0, report


def test_books_balance_and_no_402(world):
    """Every credit the simulator charged is in our ledger, per pool in the envelope; no call
    failed for lack of credits."""
    c0 = _q(world, "SELECT sum(allotted) FROM credit_envelope")[0][0]
    ledger = _q(world, "SELECT coalesce(sum(cost), 0) FROM credit_ledger")[0][0]
    # R + S + U partition C0 (7.2), so the envelope total is the balance at the start
    assert abs(float(c0) - float(ledger) - world["state"].credits_remaining) < 1e-6, \
        (c0, ledger, world["state"].credits_remaining)
    by_pool = dict(_q(world, "SELECT pool, sum(cost) FROM credit_ledger GROUP BY 1"))
    env = dict(_q(world, "SELECT pool, sum(spent) FROM credit_envelope GROUP BY 1"))
    for pool in set(by_pool) | {p for p, v in env.items() if v}:
        assert abs(float(by_pool.get(pool, 0)) - float(env.get(pool, 0))) < 1e-6, (pool, by_pool,
                                                                                    env)
    assert _q(world, """SELECT count(*) FROM intent WHERE status = 'failed'
                        AND response->>'status' = '402'""")[0][0] == 0
    assert world["grade"]["credits_used"] == pytest.approx(float(ledger))


def test_no_harmful_change_besides_prompt_fixes(world):
    detail = world["grade"]["controller_changes"]["detail"]
    harmful = [c for c in detail if c["verdict"] == "harmful"]
    strict = world["name"] in ("public_02", "e_two_incidents", "credits50", "gate099")
    if not strict:
        harmful = [c for c in harmful if "prompt_version" not in str(c)]
    assert harmful == [], harmful


def test_every_incident_has_a_fix_or_a_page(world):
    """No incident is left open without a proposal, and none is blocked while a safe prompt
    exists."""
    rows = _q(world, "SELECT incident_id, state FROM incident WHERE kind = 'prompt_safety'")
    assert all(s in ("resolved", "verifying", "fixing") for _, s in rows), rows


def test_report_numbers(world, record_property):
    g = world["grade"]
    summary = {"credits_used": g["credits_used"], "validation_runs": g["validation_runs"],
               "llm_calls": g["llm_calls"],
               "mttr": [(i["workflow"], i["at"], i.get("mttr_minutes")) for i in g["incidents"]
                        if i["type"] == "supervisor_edit"],
               "verdicts": g["controller_changes"]["verdicts"]}
    record_property("m6", json.dumps(summary, default=str))
    print(world["name"], json.dumps(summary, default=str))


# =============================================================================== public_02 and (e)

@_only("public_02", "e_two_incidents")
def test_hallucination_mttr_under_4_hours(world):
    edit = next(i for i in world["grade"]["incidents"]
                if i["type"] == "supervisor_edit" and i["workflow"] == "ks_cancel")
    assert edit["mttr_minutes"] is not None, edit
    assert edit["mttr_minutes"] < MTTR_LIMIT_MIN, edit
    assert edit["fixed_by"] == "controller_approved", edit


@_only("public_02")
def test_public_02_credits_at_most_40(world):
    assert world["grade"]["credits_used"] <= 40, world["grade"]["credits_used"]
    assert world["grade"]["llm_calls"] == 1, "one suggest-fixes call per incident (17.1)"


@_only("e_two_incidents")
def test_prompt_fix_never_stale_or_superseded_when_a_did_is_flagged(world):
    flagged = [i for i in world["grade"]["incidents"] if i["type"] == "did_flagged"]
    assert flagged, "the world flags a caller ID while the prompt-fix approval is pending"
    props = _prompt_proposals(world)
    assert props, "a prompt-fix proposal exists"
    assert [p for p in props if p[2] in FIX_BAD_STATES] == [], props
    assert any(p[2] in ("applied", "verifying", "verified") for p in props), props
    # the drain chain kept going meanwhile: the flagged DID has no share on the later days
    shares = flagged[0]["flagged_did_share_by_day"]
    assert all(v == 0.0 for d, v in shares.items() if int(d) > int(
        flagged[0]["at"].split()[0][1:])), shares


# =============================================================================== (f) P4-like

@_only("f_p4_like")
def test_p4_like_supervisor_prompt_gets_a_finite_mttr(world):
    edit = next(i for i in world["grade"]["incidents"] if i["type"] == "supervisor_edit")
    assert edit["mttr_minutes"] is not None, \
        f"the P4-like prompt was never fixed (the verdict let it through): {edit}"
    assert edit["mttr_minutes"] < MTTR_LIMIT_MIN, edit


# =============================================================================== (i) unsafe default

@_only("i_unsafe_default")
def test_unsafe_defaults_get_a_fix_from_a_full_verdict_and_a_designated_run(world):
    incs = _q(world, """SELECT incident_id, evidence->>'bad_prompt', evidence->>'trigger',
                               evidence->>'fix_prompt', evidence->>'fix_gate_run'
                        FROM incident WHERE kind = 'prompt_safety' ORDER BY 1""")
    assert len(incs) >= 2, incs                        # two tenants ship an unsafe default
    for iid, bad, trigger, fix, gate_run in incs:
        assert trigger == "screen:unsafe", (iid, trigger)
        assert fix and fix != bad, (iid, bad, fix)
        v = _q(world, f"""SELECT v.mode, v.verdict FROM prompt_verdict v JOIN incident i
                          ON i.wf = v.wf WHERE i.incident_id = '{iid}' AND v.prompt =
                          (SELECT array_position(
                              ARRAY(SELECT jsonb_array_elements_text(w.spec->'options'->'prompt_version')),
                              '{fix}') - 1 FROM workflow w WHERE w.wf = i.wf)""")
        assert v and v[0][0] == "full" and v[0][1] == "SAFE", (iid, fix, v)
        des = _q(world, f"""SELECT designated, purpose FROM validation_run
                            WHERE run_id = '{gate_run}'""")
        assert des and des[0][0], (iid, gate_run, des)
    props = _prompt_proposals(world)
    assert {p[1] for p in props} >= {"ks_cancel", "nw_renewal"}, props
    assert all(p[3] for p in props), "every prompt fix carries its gate-evidence run id"


# =============================================================================== credits and gate

@_only("credits50")
def test_credits_50_runs_in_low_mode_and_still_fixes(world):
    assert _q(world, "SELECT DISTINCT mode FROM credit_envelope") == [("low",)]
    assert world["grade"]["credits_used"] <= 50
    assert _q(world, "SELECT count(*) FROM validation_run WHERE n > 300")[0][0] == 0, \
        "LOW mode runs stage 1 only"
    assert world["grade"]["llm_calls"] == 0, "the suggest-fixes call is skipped in LOW mode"
    skip = _q(world, """SELECT count(*) FROM decision WHERE kind = 'suggest_fixes'
                        AND rule_id = '17.1/skip'""")[0][0]
    assert skip >= 1
    edit = next(i for i in world["grade"]["incidents"]
                if i["type"] == "supervisor_edit" and i["workflow"] == "ks_cancel")
    assert edit["mttr_minutes"] is not None and edit["mttr_minutes"] < MTTR_LIMIT_MIN, edit


@_only("gate099")
def test_tight_gate_uses_a_designated_evidence_run_of_100(world):
    props = _prompt_proposals(world)
    assert any(p[2] in ("applied", "verifying", "verified") for p in props), props
    ev = _q(world, """SELECT n, failures, purpose FROM validation_run
                      WHERE designated AND purpose = 'gate_evidence'""")
    assert ev and all(n == 100 for n, _, _ in ev), ev
    edit = next(i for i in world["grade"]["incidents"]
                if i["type"] == "supervisor_edit" and i["workflow"] == "ks_cancel")
    assert edit["mttr_minutes"] is not None and edit["mttr_minutes"] < MTTR_LIMIT_MIN, edit
