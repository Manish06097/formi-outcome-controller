"""Milestone M4 acceptance in the real loop (DESIGN.md 15.1 M4, integration step I3).

The real controller process (`python -m controller`, what run.sh runs in its container) with the
pickup levers (`CTL_LEARNER=probe`: cohort grid at bootstrap, per-cohort hour weights, caller-ID
SPRT probes behind the caller-ID harm gate, spreads, the flagged-DID detector with its P0b drain
chain, the slow path and its load floor, the KPI drift CUSUM) drives a real simulator over HTTP
with a real Postgres until the scenario ends. The simulator's grader and event log then judge it:

- every scenario: 0 harmful changes; governance hygiene stays clean (0 auto_apply_rejected,
  0 lapsed, 0 expired, 0 apply conflicts); every hour table we set gives every in-window hour a
  positive weight; the D0 cohort grid was recorded at bootstrap and reached the config through
  governance;
- public_01: nw_kyc pickup above the no-op baseline, renewal and cancel at or above it;
- flagged-DID worlds (public_02, custom (d) max_shift 0.05 with a one-third share): the flagged
  DID's share of the flag day is at most the 7.3 formula bound for the measured pre-flag share
  plus 0.01, and 0 on every later day;
- custom (h) spare caller IDs pre-flagged at health 0.3: no change with dd below -2%, and the bad
  DID never carries more than a probe share;
- a sole-DID workflow whose only caller ID is flagged: `KPI_DRIFT` is raised after the flag,
  probes run on the spare caller IDs, and the flagged DID is degraded once a comparator exists.

Scenarios are test fixtures (tests may read them); the controller never does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from controller.learning.did import flag_day_share
from controller.settings import Thresholds
from controller.simclient import SimClient
from controller.types import SimEpoch
from sim.grader import grade
from tests.e2e.platform.helpers import REPO, SIM_PORT, latest_schema, query, run_controller
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIOS = {
    "public_01": "scenarios/public_01_dev.yaml",
    "public_02": "scenarios/public_02_incidents.yaml",
    "d_max_shift_005": "scenarios/private/max_shift_005_did_third_p01.yaml",
    "h_preflagged": "scenarios/private/preflagged_unknown_did_p01.yaml",
    "sole_did_flag": "scenarios/private/did_flag_d1_sole_p01.yaml",
}
FLAG_SLACK = 0.01
"""7.3: sampling slack of the flag-day share (binomial SE of the share is about 0.004)."""
T_ALARM_H = 2.0
"""7.3: the formula bound is evaluated at a 2-hour alarm, the slowest the detector should be at a
one-third share or more."""
T_ALARM_MAX_H = 3.0
"""The alarm tick is random (75 to 150 min over 7 seeds, M4-LATENCY report): 2 h is typical, not a
promise. The incident must open within 3 h; the share bound above still uses 2 h and passes."""


def _baseline(scenario: str, out: Path) -> Path:
    run_dir = out / "baseline"
    subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(REPO / scenario),
                    "--run-dir", str(run_dir)], cwd=REPO, check=True, capture_output=True)
    return run_dir


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def world(request, pg_dsn, tmp_path_factory):
    from tests.e2e.platform.helpers import create_database, drop_database
    name = request.param
    workdir = tmp_path_factory.mktemp(f"m4_{name}")
    dsn = create_database(pg_dsn)
    sim = SimProcess(SCENARIOS[name], workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        run = run_controller(sim.url, dsn, workdir / "controller.log", {"CTL_LEARNER": "probe"},
                             timeout_s=1800)
        st = SimClient(sim.url).state()
    finally:
        sim.stop()
    try:
        schema = latest_schema(dsn)
        scen = yaml.safe_load((REPO / SCENARIOS[name]).read_text())
        yield {"name": name, "run": run, "state": st, "scenario": scen, "dsn": dsn,
               "schema": schema, "run_dir": sim.run_dir,
               "grade": grade(str(sim.run_dir), str(_baseline(SCENARIOS[name], workdir)))}
    finally:
        drop_database(pg_dsn, dsn)


def _q(w: dict, sql: str) -> list[tuple]:
    return query(w["dsn"], w["schema"], sql)


def _wf_spec(w: dict, wf: str) -> dict:
    return next(x for t in w["scenario"]["tenants"] for x in t["workflows"] if x["id"] == wf)


def _calls(w: dict, wf: str):
    """(minute, caller_id) of every call of `wf` in the simulator's event log."""
    epoch = SimEpoch.from_state(w["state"])
    for f in sorted((w["run_dir"] / "events").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            if '"type":"call"' not in line or f'"workflow_id":"{wf}"' not in line:
                continue
            e = json.loads(line)
            yield epoch.minute(e["ts"]), e["variant"]["caller_id"]


def _configured_share(w: dict, wf: str, did: str, at: int) -> float:
    """The flagged DID's share of the default allocation in force at the flag. The sampled share
    of a one-hour pre-flag sample is noisy (binomial SE about 0.03), so the formula's pre-flag
    share is the larger of the sample and the configured weight."""
    rows = _q(w, f"""SELECT c.config->'allocations'->'default'->'caller_id'
                     FROM config_version c JOIN workflow x USING (wf)
                     WHERE x.workflow_id = '{wf}' AND c.config IS NOT NULL
                     AND coalesce(c.applied_minute, -1) <= {at}
                     ORDER BY c.version DESC LIMIT 1""")
    dist = (rows[0][0] if rows else None) or {}
    tot = sum(float(v) for v in dist.values())
    return float(dist.get(did, 0.0)) / tot if tot > 0 else 0.0


def _flag_minute(at: str) -> int:
    day, hm = at.split()
    h, m = hm.split(":")
    return int(day[1:]) * 1440 + int(h) * 60 + int(m)


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


def test_no_harmful_change(world):
    harmful = [c for c in world["grade"]["controller_changes"]["detail"]
               if c["verdict"] == "harmful"]
    assert harmful == [], harmful


def test_every_in_window_hour_has_positive_weight(world):
    """5.8 and constraint 2: a missing or zero hour wastes dialling capacity the business owns."""
    rows = _q(world, """SELECT w.workflow_id, c.version, c.config->'schedule'->'hour_weights'
                        FROM config_version c JOIN workflow w USING (wf)
                        WHERE c.actor LIKE 'controller%' AND c.config ? 'schedule'""")
    seen = 0
    for wf, version, tables in rows:
        lo, hi = _wf_spec(world, wf)["dialling_window"]
        for cohort, table in (tables or {}).items():
            seen += 1
            weights = {int(h): float(x) for h, x in table.items()}
            for h in range(lo, hi):
                assert weights.get(h, 0.0) > 0, (wf, version, cohort, h, weights)
    if world["name"] == "public_01":
        assert seen > 0, "public_01 has history, so hour tables must have been set"


def test_cohort_grid_recorded_at_bootstrap_and_sent_through_governance(world):
    grid = _q(world, "SELECT count(*) FROM cohort_def WHERE origin = 'grid'")[0][0]
    boots = _q(world, """SELECT count(DISTINCT wf) FROM decision
                         WHERE rule_id = '5.2/build_grid'""")[0][0]
    assert boots == 3, boots                       # one bootstrap decision per workflow
    if grid:
        cohort_versions = _q(world, """SELECT count(*) FROM config_version
                                       WHERE actor = 'controller_approved'
                                       AND 'cohorts' = ANY (changed_dims)""")[0][0]
        assert cohort_versions >= 1


def test_report_numbers(world, record_property):
    """The acceptance numbers, for the I3 report (`-rP` or the junit XML)."""
    g = world["grade"]
    kpis = {wf: {k: round(v, 4) if isinstance(v, float) else v for k, v in d.items()}
            for wf, d in g["delta_vs_baseline"].items()}
    record_property("delta", json.dumps(kpis, sort_keys=True))
    record_property("incidents", json.dumps(g.get("incidents", []), default=str))
    print(world["name"], json.dumps(g["controller_changes"]["verdicts"]),
          json.dumps(kpis, sort_keys=True), json.dumps(g.get("incidents", []), default=str))


# =============================================================================== public_01

def test_public_01_pickup_at_or_above_baseline(world):
    if world["name"] != "public_01":
        pytest.skip("public_01 only")
    d = world["grade"]["delta_vs_baseline"]
    assert d["nw_kyc"]["pickup_rate"] > 0, d
    assert d["nw_renewal"]["pickup_rate"] >= 0, d
    assert d["ks_cancel"]["pickup_rate"] >= 0, d


# =============================================================================== flagged DID

def test_flag_day_share_within_the_7_3_bound_and_zero_after(world):
    flags = [i for i in world["scenario"].get("incidents", []) if i["type"] == "did_flagged"]
    if world["name"] not in ("public_02", "d_max_shift_005") or not flags:
        pytest.skip("flagged-DID worlds with a comparator only")
    tick = world["scenario"]["tick_minutes"]
    for inc in flags:
        wf, did, at = inc["workflow"], inc["did"], _flag_minute(inc["at"])
        spec = _wf_spec(world, wf)
        lo, hi = spec["dialling_window"]
        m = float(spec["governance"]["auto_apply"]["max_shift"])
        open_m = (at // 1440) * 1440 + lo * 60
        pre = [c for t, c in _calls(world, wf) if open_m <= t < at]
        assert pre, "no dials before the flag"
        s = max(sum(c == did for c in pre) / len(pre), _configured_share(world, wf, did, at))
        bound = flag_day_share(s, m, (at - open_m) / 60, T_ALARM_H, tick, hi - lo) + FLAG_SLACK
        rec = next(r for r in world["grade"]["incidents"]
                   if r["type"] == "did_flagged" and r["workflow"] == wf)
        by_day = {int(k): v for k, v in rec["flagged_did_share_by_day"].items()}
        flag_day = at // 1440
        assert by_day[flag_day] <= bound, (wf, did, s, by_day, bound)
        later = {d: v for d, v in by_day.items() if d > flag_day}
        assert later and all(v == 0.0 for v in later.values()), later
        incident = _q(world, f"""SELECT state, opened_minute FROM incident
                                 WHERE kind = 'did_degraded' AND evidence->>'did' = '{did}'""")
        assert incident and incident[0][1] - at <= T_ALARM_MAX_H * 60, incident
        print(world["name"], wf, did, f"s={s:.3f} m={m} bound={bound:.3f}",
              f"flag_day={by_day[flag_day]:.3f}", "alarm after", incident[0][1] - at, "min")


# =============================================================================== custom (h)

def test_preflagged_did_never_costs_more_than_the_harm_line(world):
    if world["name"] != "h_preflagged":
        pytest.skip("custom (h) only")
    changes = world["grade"]["controller_changes"]["detail"]
    worst = min(c["did_health_delta"] for c in changes)
    assert worst >= -0.02, worst
    cap = Thresholds().did_probe_max
    for wf in ("nw_renewal", "ks_cancel"):
        bad = [d for d, h in _wf_spec(world, wf)["did_base_health"].items() if h <= 0.3]
        assert bad
        for did in bad:
            rows = _q(world, f"""SELECT c.version, (c.config->'allocations'->'default'
                                                     ->'caller_id'->>'{did}')::float
                                 FROM config_version c JOIN workflow w USING (wf)
                                 WHERE w.workflow_id = '{wf}' AND c.actor LIKE 'controller%'""")
            top = max((x or 0.0) for _, x in rows) if rows else 0.0
            assert top <= cap + 1e-6, (wf, did, rows)        # never spread onto, only probed


# =============================================================================== sole DID

def test_sole_did_flag_raises_kpi_drift_and_starts_probes(world):
    if world["name"] != "sole_did_flag":
        pytest.skip("sole-DID world only")
    inc = next(i for i in world["scenario"]["incidents"] if i["type"] == "did_flagged")
    wf, at = inc["workflow"], _flag_minute(inc["at"])
    drift = _q(world, f"""SELECT a.sim_minute FROM alert a JOIN workflow w USING (wf)
                          WHERE a.code = 'KPI_DRIFT' AND w.workflow_id = '{wf}'
                          ORDER BY 1""")
    assert drift and drift[0][0] > at, drift
    first = drift[0][0]
    assert first - at <= 2 * 1440, f"KPI_DRIFT {first - at} minutes after the flag"
    probes = _q(world, f"""SELECT count(*) FROM decision d JOIN workflow w USING (wf)
                           WHERE w.workflow_id = '{wf}'
                           AND d.rule_id = '5.9/probing_started'""")[0][0]
    assert probes >= 1
    degraded = _q(world, f"""SELECT d.sim_minute FROM decision d JOIN workflow w USING (wf)
                             WHERE w.workflow_id = '{wf}' AND d.inputs->>'did' = '{inc["did"]}'
                             AND d.rule_id IN ('5.9/floor_degraded', '7.3/alarm')""")
    assert degraded, "the flagged sole DID was never degraded once a comparator existed"
    moved = _q(world, f"""SELECT count(*) FROM proposal p JOIN workflow w USING (wf)
                          WHERE w.workflow_id = '{wf}' AND p.submitted_minute > {degraded[0][0]}
                          AND p.change_summary LIKE '%from {inc["did"]}%'""")[0][0]
    assert moved >= 1, "no request moved traffic off the flagged DID"
    print(wf, "flag at", at, "KPI_DRIFT at", [r[0] for r in drift], "degraded at",
          degraded[0][0])
