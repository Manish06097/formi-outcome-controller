"""The 9.4 audit queries against the hand-filled fixture run schema (Postgres)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from controller.audit import explain
from tests.audit import fixture_world as fw

pytestmark = pytest.mark.pg
ROOT = Path(__file__).resolve().parents[2]


def test_parse_when_round_trips_labels():
    assert explain.parse_when("D3 14:00") == 3 * 1440 + 14 * 60
    assert explain.parse_when("d0 09:05") == 545
    assert explain.parse_when("D-2 23:59") == -2 * 1440 + 23 * 60 + 59
    assert explain.parse_when("4440") == 4440
    assert explain.label(explain.parse_when("D7 00:15")) == "D7 00:15"
    for bad in ("D3", "3 14:00", "D3 25:00", "D3 14:61", ""):
        with pytest.raises(ValueError):
            explain.parse_when(bad)


def test_why_changed_ours_names_proposal_approval_decision_and_aftermath(conn):
    x = explain.why_changed(conn, "nw_renewal", "D3 08:00")
    assert (x.version, x.actor, x.applied_at) == (2, "controller_approved", "D2 11:40")
    assert x.changed_dims == ["cohorts", "voice"]
    assert x.proposal["proposal_id"] == fw.PID_ROUTINE
    assert x.proposal["class_name"] == "routine"
    assert x.approval["approval_id"] == "apr_00012"
    assert x.approval["decided_at"] == "D2 11:30"
    assert "never the approver" in x.approval["approver_note"]
    assert x.decision["decision_id"] == 1 and x.decision["rule_id"] == "toy_gate"
    assert x.decision["harm_report"]["passes"] is True
    assert [t["to_state"] for t in x.transitions][-1] == "verified"
    # the chain follows caused_by across subjects (proposal -> config_version -> proposal)
    assert [e.action for e in x.after] == ["submitted", "approved", "applied", "verified"]
    assert "unrelated" not in [e.action for e in x.after]
    assert x.findings == []
    text = x.render()
    assert "apr_00012" in text and fw.PID_ROUTINE in text and "What happened after" in text


def test_why_changed_picks_the_version_live_at_t(conn):
    assert explain.why_changed(conn, "nw_renewal", "D2 11:39").version == 1
    assert explain.why_changed(conn, "nw_renewal", "D2 11:40").version == 2
    assert explain.why_changed(conn, "nw_renewal", "D3 09:30").version == 3
    first = explain.why_changed(conn, "nw_renewal", "D-3 10:00")
    assert first.version == 1 and first.actor == "business_default" and first.proposal is None


def test_why_changed_auto_apply_has_no_approval(conn):
    x = explain.why_changed(conn, "nw_renewal", "D3 14:00")
    assert x.version == 3 and x.proposal["route"] == "auto" and x.approval is None
    assert "Auto-applied" in x.render()


def test_why_changed_supervisor_edit_shows_our_reaction(conn):
    x = explain.why_changed(conn, "ks_cancel", "D3 14:00")
    assert x.actor == "supervisor" and x.proposal is None
    assert [e.action for e in x.after] == ["supervisor_edit", "incident_opened", "submitted"]
    assert x.after[-1].subject == f"proposal:{fw.PID_FIX}"


def test_why_changed_flags_a_version_without_our_proposal(conn):
    conn.execute("INSERT INTO config_version VALUES (1, 4, 'controller_approved', NULL, NULL, "
                 "%s, 'ghost-proposal', NULL, '{voice}', 'fetched')", (fw.m(3, 14),))
    x = explain.why_changed(conn, "nw_renewal", "D3 14:30")
    assert any("UNKNOWN_WRITER" in f for f in x.findings)


def test_why_changed_unknown_workflow(conn):
    with pytest.raises(explain.NotFound):
        explain.why_changed(conn, "nope", "D1 10:00")


def test_why_heard_per_dim_provenance_with_inherited_rows_and_live_prompt(conn):
    x = explain.why_heard(conn, "L-100", 3)
    assert x.features == {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "mid"}
    assert len(x.calls) == 1
    call = x.calls[0]
    assert (call.at, call.config_version, call.live_version, call.cohort_key) == \
        ("D3 10:05", 3, 3, fw.COHORT)
    dims = {d.dim: d for d in call.dims}
    assert list(dims) == ["welcome_message", "voice", "caller_id", "prompt_version"]
    w = dims["welcome_message"]
    assert (w.option, w.source, w.inherited, w.scope) == ("W1", "default", True, fw.COHORT)
    v = dims["voice"]
    assert (v.option, v.share, v.source, v.inherited) == ("V3", pytest.approx(0.1), "arm", False)
    assert (v.experiment_id, v.proposal_id, v.approval_id, v.approval_decided_at) == \
        ("exp_0007", fw.PID_ROUTINE, "apr_00012", "D2 11:30")
    assert "arm of exp_0007" in v.sentence and "approved apr_00012 at D2 11:30" in v.sentence
    c = dims["caller_id"]
    assert (c.option, c.source, c.inherited) == ("D3", "probe", True)
    p = dims["prompt_version"]
    assert (p.option, p.scope, p.version) == ("P1", "default", 3)
    assert x.findings == []
    assert explain.why_heard(conn, "L-100", 2).calls[0].config_version == 2


def test_why_heard_prompt_comes_from_the_version_live_at_the_call(conn):
    # a call dialled with version 1 but placed after the supervisor's prompt edit (version 2):
    # welcome/voice/caller from version 1, the prompt from version 2 (KYC-style locked campaign)
    conn.execute("""INSERT INTO call_fact VALUES (3001, 2, 2, %s, 3, 13, 7, 2, 0, 1, 0, 2, 0, 0,
                                                  0, 2, true, true, false, false, 0.9, 90, 9)""",
                 (fw.m(3, 13, 10),))
    x = explain.why_heard(conn, "L-200", 3)
    late = next(c for c in x.calls if c.event_id == 3001)
    dims = {d.dim: d for d in late.dims}
    assert dims["voice"].version == 1 and dims["prompt_version"].version == 2
    assert dims["prompt_version"].option == "P4" and dims["prompt_version"].source == "supervisor"


def test_why_heard_reports_cohort_mismatch_instead_of_dropping_the_row(conn):
    x = explain.why_heard(conn, "L-300", 3)
    voice = next(d for d in x.calls[0].dims if d.dim == "voice")
    assert voice.missing and voice.option == "V4"
    assert any("COHORT_MISMATCH" in f for f in x.findings)
    # a call whose cohort code is not in config_cohort is kept and reported too
    conn.execute("UPDATE call_fact SET cohort_code = 9 WHERE event_id = 2003")
    x = explain.why_heard(conn, "L-300", 3)
    assert len(x.calls) == 1 and any("config_cohort" in f for f in x.findings)


def test_why_heard_unknown_lead(conn):
    with pytest.raises(explain.NotFound):
        explain.why_heard(conn, "L-999", 3)


def test_weekly_digest_lists_operational_changes_with_reasons(conn):
    d = explain.weekly_digest(conn, "northwind_life")
    assert d.period == "D-4 15:00 to D3 15:00"
    assert [(i.workflow_id, i.version, i.changed_dims) for i in d.items] == \
        [("nw_renewal", 3, ["caller_id"])]
    assert d.items[0].reason.startswith("D3 unknown")
    assert [i["incident_id"] for i in d.incidents] == ["INC-0000"]
    assert d.not_acted == 1
    # customer-facing changes are not in the operational digest
    assert all("voice" not in i.changed_dims for i in d.items)
    assert explain.weekly_digest(conn, "kestrel_finance").items == []


def _cli(world, *args):
    return subprocess.run([sys.executable, "-m", "controller.audit", "--dsn", world["dsn"],
                           "--schema", world["schema"], *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)


def test_cli_why_changed_and_why_heard(world):
    r = _cli(world, "why-changed", "--workflow", "nw_renewal", "--at", "D3 08:00")
    assert r.returncode == 0, r.stderr
    assert "apr_00012" in r.stdout and "What happened after" in r.stdout
    r = _cli(world, "why-heard", "--lead", "L-100", "--day", "3", "--json")
    assert r.returncode == 0, r.stderr
    assert '"experiment_id": "exp_0007"' in r.stdout
    r = _cli(world, "weekly-digest", "--tenant", "northwind_life")
    assert r.returncode == 0 and "Probe caller ID D3" in r.stdout


def test_cli_not_found_and_bad_time_exit_2(world):
    assert _cli(world, "why-heard", "--lead", "L-999", "--day", "3").returncode == 2
    r = _cli(world, "why-changed", "--workflow", "nw_renewal", "--at", "tomorrow")
    assert r.returncode == 2 and "D<day> HH:MM" in r.stderr


def test_cli_is_read_only(world):
    import psycopg

    from controller.audit.__main__ import open_connection
    with open_connection(world["schema"], world["dsn"]) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO hold (wf, set_by, from_minute) VALUES (1, 'x', 0)")
