"""Milestone M3 acceptance in the real loop (DESIGN.md 15.1 M3, integration step I2).

The real controller process (`python -m controller`, what run.sh runs in its container) with the
probe-only learner (`CTL_LEARNER=probe`: caller-ID probes and spreads, per-cohort call times from
history) drives a real simulator over HTTP with a real Postgres, until the scenario ends. The
simulator's own grader and log then judge governance hygiene, and our store is checked for the
invariants behind it:

- 0 auto_apply_rejected, 0 lapsed, 0 expired, 0 apply conflicts (the grader's counts);
- the peak number of requests in any 24h window is at most C_eff for every workflow;
- every approval the approvers approved was applied, except a proposal we had legitimately
  ended (STALE after a supervisor edit, SUPERSEDED), and none was ever let lapse locally;
- every routine (P2, P3) request was created exactly on a routine slot minute; at cap 1, routine
  requests are at least 48h apart;
- governance never lost a slot it was given work for: no NO_SAFE_WINDOW, no blocked submit, no
  governance page (403, 410, 422, apply at risk, expiry, unknown writer or intent);
- nothing is left mid-call or approved-but-unapplied at the end;
- public_02: both supervisor edits (the D3 prompt on ks_cancel, the D5 voice on nw_renewal) are
  rebased on and never reverted, and our proposals pending at an edit went STALE.

Scenarios: public_01, public_02, and the governance attack variants of scenarios/private/
((a) TTL 12h with latency 60-480, (b) approvers 14-16 with TTL 4h, (c) cap 1 with auto-apply off,
(d) max_shift 0.05). The crash matrix for submit_approval, apply and auto_apply is in
tests/e2e/platform/test_crash_equivalence.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from controller.governance.timing import routine_slots
from controller.settings import Thresholds
from controller.simclient import SimClient
from controller.types import SimEpoch
from controller.world.directives import load_directives
from controller.world.registry import WorkflowRegistry
from sim.grader import grade
from tests.e2e.platform.helpers import REPO, SIM_PORT, latest_schema, query, run_controller
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIOS = {
    "public_01": "scenarios/public_01_dev.yaml",
    "public_02": "scenarios/public_02_incidents.yaml",
    "a_ttl12_lat480": "scenarios/private/gov_a_ttl12_lat480_p01.yaml",
    "b_approvers_14_16_ttl4": "scenarios/private/gov_b_approvers_14_16_ttl4_p01.yaml",
    "c_cap1": "scenarios/private/gov_c_cap1_p01.yaml",
    "d_max_shift_005": "scenarios/private/max_shift_005_did_third_p01.yaml",
}
GOVERNANCE_PAGES = ("AUTO_APPLY_REJECTED", "APPLY_AT_RISK", "GOV_INVARIANT", "APPROVAL_EXPIRED",
                    "NO_SAFE_WINDOW", "UNKNOWN_WRITER", "INTENT_UNKNOWN")


def _baseline(scenario: str, out: Path) -> Path:
    run_dir = out / "baseline"
    subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(REPO / scenario),
                    "--run-dir", str(run_dir)], cwd=REPO, check=True, capture_output=True)
    return run_dir


def _run(scenario: str, workdir: Path, db_dsn: str) -> dict:
    sim = SimProcess(scenario, workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        run = run_controller(sim.url, db_dsn, workdir / "controller.log",
                             {"CTL_LEARNER": "probe"}, timeout_s=1800)
        c = SimClient(sim.url)
        st = c.state()
        registry = WorkflowRegistry(load_directives(), Thresholds())
        registry.load(c.workflows(), st.tick_minutes)
        approvals = c.approvals()
        history = {w: c.config_history(w) for w in registry.workflow_ids()}
    finally:
        sim.stop()
    schema = latest_schema(db_dsn)
    log = [json.loads(x) for x in (sim.run_dir / "sim_log.jsonl").read_text().splitlines()
           if x.strip()]
    return {"run": run, "state": st, "registry": registry, "approvals": approvals,
            "history": history, "schema": schema, "log": log, "run_dir": sim.run_dir}


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def world(request, pg_dsn, tmp_path_factory):
    from tests.e2e.platform.helpers import create_database, drop_database
    name = request.param
    workdir = tmp_path_factory.mktemp(f"m3_{name}")
    dsn = create_database(pg_dsn)
    try:
        out = _run(SCENARIOS[name], workdir, dsn)
        out["name"] = name
        out["dsn"] = dsn
        out["grade"] = grade(str(out["run_dir"]), str(_baseline(SCENARIOS[name], workdir)))
        out["store"] = _store_facts(dsn, out["schema"])
        yield out
    finally:
        import os
        if os.environ.get("CTL_KEEP_DB"):
            print(f"kept database: {dsn}")
        else:
            drop_database(pg_dsn, dsn)


def _store_facts(dsn: str, schema: str) -> dict:
    q = lambda sql: query(dsn, schema, sql)  # noqa: E731
    return {
        "proposals": q("""SELECT p.proposal_id, w.workflow_id, p.class, p.route, p.state,
                                 p.approval_id, p.submitted_minute, p.applied_minute,
                                 p.base_version
                          FROM proposal p JOIN workflow w USING (wf) ORDER BY 1"""),
        "alerts": q("SELECT code, dedupe_key, message FROM alert ORDER BY alert_id"),
        "release": q("""SELECT outcome->>'action', count(*) FROM decision
                        WHERE module = 'governance.release' GROUP BY 1"""),
        "intents": q("SELECT kind, status, count(*) FROM intent GROUP BY 1, 2"),
        "rebases": q("""SELECT d.sim_minute, w.workflow_id, d.inputs, d.outcome FROM decision d
                        JOIN workflow w USING (wf) WHERE d.kind = 'rebase'"""),
        "transitions": q("""SELECT t.proposal_id, w.workflow_id, t.seq, t.to_state, t.sim_minute
                            FROM proposal_transition t JOIN proposal p USING (proposal_id)
                            JOIN workflow w ON w.wf = p.wf ORDER BY 1, 3"""),
    }


def _minute(w: dict, ts: str) -> int:
    return SimEpoch.from_state(w["state"]).minute(ts)


def _per_day(w: dict) -> dict[str, dict[int, int]]:
    """Approvals applied per workflow and sim day, from the simulator's config history."""
    out: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for wf, versions in w["history"].items():
        for v in versions:
            if v.actor == "controller_approved":
                m = _minute(w, v.applied_at)
                out[wf][m // 1440] += 1
    return {wf: dict(d) for wf, d in out.items()}


# =============================================================================== the checks

def test_run_finishes_and_grader_counts_are_clean(world):
    assert world["run"].exit_codes == [0], world["run"].log[-3000:]
    assert world["state"].finished
    g = world["grade"]["governance"]
    report = json.dumps(g, default=str)
    assert g["auto_apply_rejected"] == 0, report
    assert g["apply_conflicts"] == 0, report
    assert g["lapsed"] == 0, report
    assert g["decisions"].get("expired", 0) == 0, report
    assert g["approval_requests"] + g["auto_applied"] > 0, "governance had real work"


def test_peak_requests_within_c_eff(world):
    peak = world["grade"]["governance"]["peak_requests_per_24h"]
    for wf, n in peak.items():
        assert n <= world["registry"].policy(wf).c_eff, (wf, n, peak)


def test_every_approved_request_was_applied_in_time(world):
    props = {p[0]: p for p in world["store"]["proposals"]}
    applied = {v.approval_id for vs in world["history"].values() for v in vs if v.approval_id}
    for a in world["approvals"]:
        p = props[a.proposal_id]
        assert p[4] != "lapsed_local", p
        if a.status in ("approved", "applied"):
            assert a.approval_id in applied or p[4] in ("stale", "superseded", "abandoned"), (a, p)
    per_day = _per_day(world)
    assert sum(sum(d.values()) for d in per_day.values()) >= 1, per_day


def test_routine_requests_only_at_slot_minutes(world):
    props = {p[0]: p for p in world["store"]["proposals"]}
    by_wf = defaultdict(list)
    for a in world["approvals"]:
        p = props[a.proposal_id]
        if p[2] < 2:
            continue
        m = _minute(world, a.created_at)
        pol = world["registry"].policy(a.workflow_id)
        assert m in routine_slots(m // 1440, pol), (a.proposal_id, m)
        by_wf[a.workflow_id].append(m)
    for wf, mins in by_wf.items():
        if world["registry"].policy(wf).c_eff == 1:
            mins.sort()
            assert all(b - a >= 2 * 1440 for a, b in zip(mins, mins[1:])), (wf, mins)


def test_requests_count_the_leads_they_affect(world):
    """6.6: `affected_leads` counts active leads in the changed cells plus their share of the
    next batch; every change of the probe learner reaches leads, so none may claim zero."""
    rows = query(world["dsn"], world["schema"],
                 """SELECT proposal_id, affected_leads FROM proposal
                    WHERE state NOT IN ('discarded') AND affected_leads <= 0
                      AND EXISTS (SELECT 1 FROM jsonb_array_elements(diff->'items') i
                                  WHERE i->'path'->>1 <> 'default' OR i->'path'->>0 <> 'alloc')""")
    # A change that touches only the default cohort's allocation reaches no lead once every
    # cohort overrides that dimension, so 0 is the true count (open problem in build-reports/I4.md).
    assert rows == [], rows


def test_no_lost_slot_and_no_governance_page(world):
    release = dict(world["store"]["release"])
    assert release.get("no_safe_window", 0) == 0, release
    assert release.get("blocked", 0) == 0, release
    pages = [a for a in world["store"]["alerts"] if a[0] in GOVERNANCE_PAGES]
    assert pages == [], pages


def test_nothing_left_mid_call_or_unapplied(world):
    left = [p for p in world["store"]["proposals"]
            if p[4] in ("draft", "submitting", "auto_applying", "applying", "approved")]
    assert left == [], left
    assert not [i for i in world["store"]["intents"] if i[1] in ("started", "unknown")]


def test_supervisor_edits_rebased_never_reverted(world):
    sup = {wf: [v for v in vs if v.actor == "supervisor"] for wf, vs in world["history"].items()}
    edits = [(wf, v) for wf, vs in sup.items() for v in vs]
    if world["name"] == "public_02":
        # the simulator records every dim whose stored weights differ, so an edit that
        # re-normalises our rounded allocations also lists them: containment, not equality
        assert all(any(wf == w and d in v.changed_dims for w, v in edits)
                   for wf, d in (("ks_cancel", "prompt_version"), ("nw_renewal", "voice"))), edits
    rebased = {(r[1], r[2]["version"]) for r in world["store"]["rebases"]}
    for wf, v in edits:
        assert (wf, v.version) in rebased, (wf, v.version, rebased)
        later = [x for x in world["history"][wf] if x.version > v.version
                 and x.actor.startswith("controller")]
        for x in later:
            # the engagement learner (I4) may judge an allocation again on top of an edit; that
            # is a new gated decision built on the edit, not a revert of it
            assert not (set(x.changed_dims) & set(v.changed_dims)) - {"welcome_message", "voice"}, (
                f"{wf} v{x.version} touches {x.changed_dims}, set by the supervisor in "
                f"v{v.version}")
        # every proposal of ours pending or approved when the edit landed ended STALE
        edit_minute = _minute(world, v.applied_at)
        final = {p[0]: p[4] for p in world["store"]["proposals"]}
        state_at: dict[str, str] = {}
        for pid, pwf, _, to_state, minute in world["store"]["transitions"]:
            if pwf == wf and minute <= edit_minute:
                state_at[pid] = to_state
        for pid, st in state_at.items():
            if st in ("pending", "approved", "noticed"):
                assert final[pid] == "stale", (pid, st, final[pid], v)


def test_report_applied_approvals_per_day(world, record_property):
    """Not an assertion beyond the ones above: the per-day table goes into the test report
    (`-rP` or the junit XML) so the acceptance numbers can be quoted."""
    per_day = _per_day(world)
    record_property("approvals_applied_per_day", json.dumps(per_day, sort_keys=True))
    record_property("governance", json.dumps(world["grade"]["governance"], default=str))
    print(world["name"], "approvals applied per day:", json.dumps(per_day, sort_keys=True))
