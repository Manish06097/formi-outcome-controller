"""Alert rules (DESIGN.md 9.2) and their runbook anchors (9.5)."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import replace
from pathlib import Path

import pytest

from controller.obs import alerts
from controller.obs.alerts import AlertInputs, ApplyRisk, OpenIncident
from controller.settings import Thresholds

ROOT = Path(__file__).resolve().parents[2]
T = Thresholds()
NOW = dt.datetime(2027, 1, 7, 12, 0, tzinfo=dt.timezone.utc)


def base(**kw) -> AlertInputs:
    return replace(AlertInputs(wall_now=NOW, sim_minute=3 * 1440 + 600, tick=15), **kw)


def codes(inputs: AlertInputs) -> list[str]:
    return [f.code for f in alerts.evaluate(inputs, T)]


# ------------------------------------------------------------------ runbook

def test_every_code_has_a_runbook_anchor_and_every_anchor_a_code():
    text = (ROOT / "docs" / "RUNBOOK.md").read_text()
    anchors = re.findall(r'<a id="([A-Z0-9_]+)"></a>', text)
    assert len(anchors) == len(set(anchors))
    assert set(anchors) == set(alerts.CODES)
    for code, c in alerts.CODES.items():
        assert c.runbook == f"docs/RUNBOOK.md#{code}"
        assert f"### {code}" in text


def test_design_codes_are_all_present():
    for code in ("INGEST_STALLED", "APPLY_AT_RISK", "GOV_INVARIANT", "CREDITS_BELOW_RESERVE",
                 "INCIDENT_UNRESOLVED", "NO_SAFE_PROMPT", "CLIENT_GATE_UNREACHABLE",
                 "NO_HEALTHY_DID", "NO_SAFE_WINDOW", "NEW_WORLD", "UNKNOWN_WRITER",
                 "COHORT_MISMATCH", "CRASH_LOOP", "KPI_DRIFT"):
        assert code in alerts.CODES
    assert alerts.CODES["KPI_DRIFT"].severity == "digest"
    assert alerts.CODES["GOV_INVARIANT"].severity == "page"


# ------------------------------------------------------------------ pure rules

def test_quiet_world_raises_nothing():
    assert codes(base()) == []


def test_ingest_stalled_after_5_wall_minutes_only_when_behind():
    late = NOW - dt.timedelta(seconds=T.alert_ingest_stalled_s)
    assert codes(base(listed_chunk_max=12, ingested_chunk_max=11, last_ingested_wall=late)) == \
        ["INGEST_STALLED"]
    almost = NOW - dt.timedelta(seconds=T.alert_ingest_stalled_s - 1)
    assert codes(base(listed_chunk_max=12, ingested_chunk_max=11, last_ingested_wall=almost)) == []
    assert codes(base(listed_chunk_max=11, ingested_chunk_max=11, last_ingested_wall=late)) == []


def test_apply_at_risk_within_one_step_or_before_next_stop():
    now = 3 * 1440 + 600
    risk = ApplyRisk(wf=1, proposal_id="p1", state="approved", apply_by_minute=now + 15)
    assert codes(base(apply_risks=(risk,))) == ["APPLY_AT_RISK"]
    far = replace(risk, apply_by_minute=now + 16)
    assert codes(base(apply_risks=(far,))) == []
    # an overnight jump past apply_by is a risk even if apply_by is hours away
    assert codes(base(apply_risks=(far,), next_stop=now + 600)) == ["APPLY_AT_RISK"]
    assert codes(base(apply_risks=(replace(risk, state="applied"),))) == []


def test_credits_below_reserve_and_low_mode():
    assert codes(base(reserve_left=64.0, worst_incident_cost=65.0)) == ["CREDITS_BELOW_RESERVE"]
    assert codes(base(reserve_left=65.0, worst_incident_cost=65.0)) == []
    assert codes(base(low_mode_tenants=("kestrel_finance",))) == ["CREDITS_BELOW_RESERVE"]
    assert alerts.worst_incident(T, 5) == 65.0  # W = 13 K at K = 5 (DESIGN.md 7.2)


def test_incident_unresolved_thresholds():
    now = 3 * 1440 + 600
    inc = OpenIncident(wf=2, incident_id="INC-1", kind="hallucination",
                       opened_minute=now - T.alert_incident_no_fix_min, fix_submitted_minute=None)
    assert codes(base(incidents=(inc,))) == ["INCIDENT_UNRESOLVED"]
    young = replace(inc, opened_minute=now - T.alert_incident_no_fix_min + 1)
    assert codes(base(incidents=(young,))) == []
    fixing = replace(inc, fix_submitted_minute=now - 30)
    assert codes(base(incidents=(fixing,))) == []
    old = replace(fixing, opened_minute=now - T.alert_incident_unresolved_min)
    assert codes(base(incidents=(old,))) == ["INCIDENT_UNRESOLVED"]
    drain = OpenIncident(wf=2, incident_id="INC-2", kind="did_flagged", opened_minute=now - 10,
                         fix_submitted_minute=None,
                         p0a_in_flight_since=now - T.alert_drain_wait_min - 1)
    f = alerts.evaluate(base(incidents=(drain,)), T)
    assert [(x.code, x.subject) for x in f] == [("INCIDENT_UNRESOLVED", "INC-2:drain")]


def test_no_healthy_did():
    assert codes(base(did_status={1: ("unhealthy", "degraded")})) == ["NO_HEALTHY_DID"]
    assert codes(base(did_status={1: ("unhealthy", "suspect")})) == []
    assert codes(base(did_status={1: ()})) == []


def test_crash_loop_more_than_3_in_10_minutes():
    w = tuple(NOW - dt.timedelta(minutes=m) for m in (1, 2, 3, 4))
    assert codes(base(restart_walls=w)) == ["CRASH_LOOP"]
    assert codes(base(restart_walls=w[:3])) == []
    old = w[:3] + (NOW - dt.timedelta(seconds=T.alert_crash_loop_window_s + 1),)
    assert codes(base(restart_walls=old)) == []


def test_sim_unreachable_after_5_minutes_of_retries():
    assert codes(base(get_retry_since=NOW - dt.timedelta(seconds=300))) == ["SIM_UNREACHABLE"]
    assert codes(base(get_retry_since=NOW - dt.timedelta(seconds=299))) == []


# ------------------------------------------------------------------ store (Postgres)

@pytest.mark.pg
def test_gather_and_sync_open_and_close_condition_alerts(conn):
    i = alerts.gather_inputs(conn, T, wall_now=NOW)
    assert i.sim_minute == 3 * 1440 + 900 and i.reserve_left == 64.0
    assert i.worst_incident_cost == 52.0          # 13 x 4 prompts in the fixture catalogs
    fired = alerts.evaluate(i, T)
    assert sorted((f.code, f.subject) for f in fired) == [
        ("INCIDENT_UNRESOLVED", "INC-0000"), ("NO_HEALTHY_DID", "caller_id")]
    r = alerts.sync(conn, fired, sim_minute=i.sim_minute)
    assert len(r["opened"]) == 2 and r["closed"] == []
    open_ = alerts.open_alerts(conn)
    assert {a["runbook"] for a in open_} == {"docs/RUNBOOK.md#INCIDENT_UNRESOLVED",
                                             "docs/RUNBOOK.md#NO_HEALTHY_DID"}
    assert alerts.sync(conn, fired) == {"opened": [], "closed": []}     # idempotent
    r = alerts.sync(conn, [f for f in fired if f.code == "NO_HEALTHY_DID"])
    assert len(r["closed"]) == 1
    assert [a["code"] for a in alerts.open_alerts(conn)] == ["NO_HEALTHY_DID"]


@pytest.mark.pg
def test_event_alerts_dedupe_and_digests_are_recorded_closed(conn):
    a = alerts.raise_alert(conn, "GOV_INVARIANT", 1, "p-17", "403 on auto-apply", 100)
    assert a is not None
    assert alerts.raise_alert(conn, "GOV_INVARIANT", 1, "p-17", "403 again", 105) is None
    assert alerts.raise_alert(conn, "GOV_INVARIANT", 1, "p-18", "another proposal", 105)
    d = alerts.raise_alert(conn, "SUPERVISOR_EDIT", 2, "ks_cancel@v2", "prompt to P4", 110)
    assert d is not None
    open_codes = [x["code"] for x in alerts.open_alerts(conn)]
    assert open_codes.count("GOV_INVARIANT") == 2 and "SUPERVISOR_EDIT" not in open_codes
    # condition sync never closes event alerts
    alerts.sync(conn, [])
    assert [x["code"] for x in alerts.open_alerts(conn)].count("GOV_INVARIANT") == 2
    assert alerts.close_alert(conn, a) and not alerts.close_alert(conn, a)


@pytest.mark.pg
def test_event_dedupe_treats_like_wildcards_literally(conn):
    assert alerts.raise_alert(conn, "UNKNOWN_WRITER", 1, "p_17", "x") is not None
    assert alerts.raise_alert(conn, "UNKNOWN_WRITER", 1, "pX17", "y") is not None
