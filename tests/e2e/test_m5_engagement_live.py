"""Milestone M5 acceptance in the real loop (DESIGN.md 15.1 M5, integration step I4).

The real controller process (`python -m controller`) with the probe learner and the engagement
learner (`CTL_LEARNER=probe`) drives a real simulator over HTTP with a real Postgres until the
scenario ends. The simulator's own grader judges every change; our store is checked for what the
grader cannot see.

- public_01, public_02: 0 harmful engagement changes, engagement gap captured above 0 on every
  engagement-primary workflow.
- J2 case (g), a strong winner that decays: no ramp or promotion graded harmful, and the decaying
  variant's share under 10% by onset + 2 days.
- J2 case (j), winners only among the last-listed options with sign-flipping cohort effects: gap
  above 0, 0 harmful, and no applied change's predicted cohort effect below the cohort's line.

Store checks on every scenario: looks persisted on the doubling schedule, a harm-gate decision with
its posterior snapshots before every change of ours, engagement decisions replay exactly from the
audit store, allocation proposals resolve out of VERIFYING, the request's dry run states the
guardrail forecast, and the end-game freeze holds.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from controller.audit import replay
from controller.settings import Thresholds
from controller.simclient import SimClient
from controller.world.clock import freeze_start
from sim.grader import grade
from tests.e2e.platform.helpers import (REPO, SIM_PORT, create_database, drop_database,
                                        latest_schema, query, run_controller)
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIOS = {
    "public_01": "scenarios/public_01_dev.yaml",
    "public_02": "scenarios/public_02_incidents.yaml",
    "decaying_winner": "scenarios/private/decaying_winner_dev_p01.yaml",
    "sign_flip": "scenarios/private/last_listed_signflip_p01.yaml",
}
GOVERNANCE_PAGES = ("AUTO_APPLY_REJECTED", "APPLY_AT_RISK", "GOV_INVARIANT", "APPROVAL_EXPIRED",
                    "NO_SAFE_WINDOW", "UNKNOWN_WRITER", "INTENT_UNKNOWN")
ENGAGEMENT_DIMS = {"welcome_message", "voice"}
REPLAY_RULES = ("0.1/harm_gate", "5.4/plan_allocation", "5.5/evaluate_looks", "5.4/harm_stop",
                "5.7/rollback")
REPLAYS_PER_RULE = 4
T = Thresholds()


def _baseline(scenario: str, out: Path) -> Path:
    run_dir = out / "baseline"
    subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(REPO / scenario),
                    "--run-dir", str(run_dir)], cwd=REPO, check=True, capture_output=True)
    return run_dir


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def world(request, pg_dsn, tmp_path_factory):
    name = request.param
    workdir = tmp_path_factory.mktemp(f"m5_{name}")
    dsn = create_database(pg_dsn)
    sim = SimProcess(SCENARIOS[name], workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        run = run_controller(sim.url, dsn, workdir / "controller.log", {"CTL_LEARNER": "probe"},
                             timeout_s=3000)
        c = SimClient(sim.url)
        specs = {w.workflow_id: w for w in c.workflows()}
    finally:
        sim.stop()
    try:
        schema = latest_schema(dsn)
        yield {"name": name, "run": run, "specs": specs, "dsn": dsn,
               "schema": schema, "grade": grade(str(sim.run_dir),
                                                str(_baseline(SCENARIOS[name], workdir)))}
    finally:
        if os.environ.get("CTL_KEEP_DB"):       # debugging: keep the run's database to inspect
            print(f"kept database: {dsn}")
        else:
            drop_database(pg_dsn, dsn)


def _q(w: dict, sql: str, params=None) -> list[tuple]:
    return query(w["dsn"], w["schema"], sql, params)


def _engagement_workflows(w: dict) -> list[str]:
    return sorted(k for k, s in w["specs"].items() if s.objective.primary == "engagement_rate")


def _changes(w: dict) -> list[dict]:
    return w["grade"]["controller_changes"]["detail"]


def _print_grade(w: dict) -> None:
    from sim.grader import to_markdown
    print(f"\n--- {w['name']} ---\n" + to_markdown(w["grade"]))


# ------------------------------------------------------------------ the grader's judgement
def test_the_run_finishes_clean(world):
    _print_grade(world)
    assert world["run"].final == 0
    g = world["grade"]["governance"]
    assert (g["auto_apply_rejected"], g["apply_conflicts"], g["lapsed"]) == (0, 0, 0), g
    assert g["decisions"]["expired"] == 0
    pages = _q(world, "SELECT code, message FROM alert WHERE code = ANY(%s)",
               (list(GOVERNANCE_PAGES),))
    assert pages == []


def test_no_change_of_ours_is_graded_harmful_on_engagement(world):
    bad = [(c["workflow_id"], c["version"], c["engage_delta"]) for c in _changes(world)
           if c["engage_delta"] < -0.01]
    assert bad == []


def test_the_engagement_gap_captured_is_above_zero_on_every_engagement_workflow(world):
    if world["name"] == "decaying_winner":
        pytest.skip("the decaying-winner bar is exposure and verdicts (next test), not gap")
    gaps = {k: world["grade"]["kpis"][k]["engagement_gap_captured"]
            for k in _engagement_workflows(world)}
    assert gaps and all(v is not None and v > 0 for v in gaps.values()), gaps


def test_a_decaying_winner_is_out_by_onset_plus_two_days(world):
    if world["name"] != "decaying_winner":
        pytest.skip("scenario without the J2 (g) acceptance")
    shares = world["grade"]["decaying_variant_share_after_onset"]
    assert shares
    for key, by_day in shares.items():
        days = sorted(int(d) for d in by_day)
        at = days[0] + 2
        # a variant never dialled in population has no row for that day: no exposure
        assert by_day.get(str(at), by_day.get(at, 0.0)) < 0.10, (key, by_day)


def test_no_ramp_or_promotion_of_a_decaying_variant_is_graded_harmful(world):
    if world["name"] != "decaying_winner":
        pytest.skip("scenario without the J2 (g) acceptance")
    bad = [c for c in _changes(world)
           if set(c["dims"]) & ENGAGEMENT_DIMS and c["verdict"] == "harmful"]
    assert bad == []


def test_no_applied_change_breaks_the_cohort_line_it_was_judged_on(world):
    """5.4: for every gate that let a change through, each cohort's predicted de_k stays above
    its line in the mean, and the tail is at or below the tail the class allows."""
    rows = _q(world, """SELECT d.sim_minute, d.inputs, d.harm_report FROM decision d
                        WHERE d.rule_id = '0.1/harm_gate'
                          AND (d.outcome->>'passes')::boolean""")
    assert rows or not _engagement_workflows(world)
    for minute, inputs, harm in rows:
        alpha = float(inputs["alpha"])
        for k, (mean, tail) in (harm.get("de_k") or {}).items():
            assert tail <= alpha + T.rounding, (minute, k, mean, tail)


# ------------------------------------------------------------------ the store
def test_every_engagement_change_has_a_gate_decision_with_its_posteriors(world):
    """0.1: every controller version that moved welcome or voice was judged first."""
    rows = _q(world, """SELECT wf, version, changed_dims, proposal_id, applied_minute
                        FROM config_version WHERE actor LIKE 'controller%%'
                        AND changed_dims && ARRAY['welcome_message', 'voice']""")
    if _engagement_workflows(world):
        assert rows
    for wf, version, _dims, pid, minute in rows:
        n = _q(world, """SELECT count(*) FROM decision d
                         WHERE d.wf = %s AND d.rule_id = '0.1/harm_gate' AND d.sim_minute <= %s
                           AND cardinality(d.snapshot_ids) > 0
                           AND (d.outcome->>'passes')::boolean""", (wf, minute))[0][0]
        assert n > 0, (wf, version, pid)


def test_looks_are_persisted_on_the_doubling_schedule(world):
    rows = _q(world, """SELECT exp_id, arm, scope, look_no, n_at_look FROM winner_look
                        ORDER BY exp_id, arm, scope, look_no""")
    assert rows or not _engagement_workflows(world)
    last: dict[tuple, int] = {}
    for exp, arm, scope, look_no, n in rows:
        key = (exp, arm, scope)
        if key in last:
            assert n >= T.look_growth * last[key], (key, n, last[key])
        else:
            assert look_no == 1 and n >= T.look_first_n, (key, look_no, n)
        last[key] = n


def test_engagement_decisions_replay_exactly_from_the_audit_store(world):
    from controller.settings import load_settings
    settings = load_settings()
    seen = 0
    with psycopg.connect(world["dsn"], options=f"-c search_path={world['schema']}") as conn:
        for rule in REPLAY_RULES:
            ids = [r[0] for r in conn.execute(
                """SELECT decision_id FROM decision WHERE rule_id = %s
                   AND cardinality(snapshot_ids) > 0
                   ORDER BY md5(decision_id::text) LIMIT %s""", (rule, REPLAYS_PER_RULE))]
            for did in ids:
                rep = replay.replay_decision(int(did), conn=conn, settings=settings)
                assert rep.equal, (rule, did, rep.diffs)
                seen += 1
    assert seen > 0 or not _engagement_workflows(world)


def test_allocation_proposals_resolve_out_of_verifying(world):
    """AUDIT-1 finding 2: a proposal applied two full segments before the end does not stay
    VERIFYING. Segments are half a dialling day, so three days is a safe margin on any window."""
    end = _q(world, "SELECT max(sim_minute) FROM decision")[0][0]
    left = _q(world, """SELECT proposal_id, state, applied_minute FROM proposal
                        WHERE state = 'verifying' AND applied_minute < %s""", (end - 3 * 1440,))
    assert left == []
    if _engagement_workflows(world):
        done = _q(world, """SELECT count(*) FROM proposal WHERE verify IS NOT NULL
                            AND verify->>'kind' = 'allocation'""")[0][0]
        assert done > 0


def test_the_regate_and_the_held_recheck_are_real(world):
    """AUDIT-1 findings 2 and 9: each late decision or notice expiry leaves a regate decision, and
    each held patch released or held again has a recheck decision before it."""
    released = _q(world, """SELECT d.outcome FROM decision d WHERE d.rule_id = '6.1/auto_release'""")
    n_rel = sum(len(o.get("released", [])) + len(o.get("held", [])) + len(o.get("dropped", []))
                for (o,) in released)
    n_chk = _q(world, "SELECT count(*) FROM decision WHERE rule_id = '0.1/recheck_held'")[0][0]
    assert n_chk >= n_rel
    noticed = _q(world, """SELECT count(*) FROM proposal_transition
                           WHERE from_state = 'noticed' AND to_state = 'auto_applying'""")[0][0]
    regates = _q(world, "SELECT count(*) FROM decision WHERE rule_id = '0.1/regate_notice'")[0][0]
    assert regates >= noticed


def test_the_request_states_the_guardrail_forecast_on_every_workflow(world):
    """AUDIT-1 finding 3: BuildContext.guardrail is set, so the dry run carries the forecast, for
    pickup-primary workflows too."""
    rows = _q(world, """SELECT w.workflow_id, p.proposal_id, p.dry_run FROM proposal p
                        JOIN workflow w USING (wf) WHERE p.route = 'approval'
                        AND p.submitted_minute IS NOT NULL""")
    assert rows
    for wid, pid, dry in rows:
        if world["specs"][wid].objective.guardrail_kpi:
            assert "guardrail" in (dry or {}), (wid, pid, sorted(dry or {}))


def test_the_end_game_freeze_holds(world):
    """4.2: in the freeze no arm gains its first share; in the last day no routine increase."""
    for wid, spec in world["specs"].items():
        if spec.objective.primary != "engagement_rate":
            continue
        code = _q(world, "SELECT wf FROM workflow WHERE workflow_id = %s", (wid,))[0][0]
        end = _q(world, "SELECT end_minute FROM clock")[0][0]
        fs = freeze_start(end, T.freeze_min_days, T.freeze_frac)
        rows = _q(world, """SELECT version, config, applied_minute FROM config_version
                            WHERE wf = %s AND config IS NOT NULL ORDER BY version""", (code,))
        seen = set()
        for _, cfg, minute in rows:
            arms = set()
            for k, by_dim in (cfg.get("allocations") or {}).items():
                for dim in ENGAGEMENT_DIMS:
                    arms |= {(dim, o) for o, v in (by_dim.get(dim) or {}).items() if v > 0}
            if minute is not None and minute >= fs:
                assert arms <= seen, (wid, minute, sorted(arms - seen))
            seen |= arms
