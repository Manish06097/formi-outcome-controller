"""The supervisor/ops API over HTTP (FastAPI TestClient) as role ctl_api, and the role's limits.

DESIGN.md 9.3: "The api container connects as role ctl_api, which can INSERT and UPDATE hold and
nothing else". These tests prove that against a real Postgres, both through the API and with a
direct ctl_api connection.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors, sql

from controller.api.app import check_privileges, create_app
from tests.audit import fixture_world as fw

pytestmark = pytest.mark.pg


@pytest.fixture()
def client(world):
    app = create_app(dsn=world["api_dsn"], schema=world["schema"], allow_owner=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def api_conn(world):
    with psycopg.connect(world["api_dsn"]) as c:
        c.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(world["schema"])))
        yield c
        c.rollback()


# ------------------------------------------------------------------ the role itself

@pytest.mark.parametrize("stmt", [
    "INSERT INTO decision (sim_minute, module, kind, rule_id, settings_hash, code_version) "
    "VALUES (0, 'api', 'x', 'x', 'h', 'c')",
    "UPDATE proposal SET state = 'approved' WHERE proposal_id = '" + fw.PID_PENDING + "'",
    "DELETE FROM audit_log",
    "INSERT INTO approval_mirror (approval_id, wf, status) VALUES ('apr_x', 1, 'approved')",
    "UPDATE config_version SET actor = 'supervisor'",
    "INSERT INTO intent (kind, idem_key, status, sim_minute) VALUES ('apply', 'k', 'started', 0)",
    "DELETE FROM hold",
    "TRUNCATE hold",
    "UPDATE clock SET sim_minute = 0",
    "CREATE TABLE sneaky (x int)",
    "INSERT INTO ctl.run_registry (schema_name, fingerprint, sim_url) VALUES ('a', 'b', 'c')",
])
def test_ctl_api_cannot_write_outside_hold(api_conn, stmt):
    with pytest.raises(errors.InsufficientPrivilege):
        api_conn.execute(stmt)


def test_ctl_api_can_insert_and_update_hold_and_read_everything(api_conn):
    hid = api_conn.execute("INSERT INTO hold (wf, set_by, reason, from_minute) "
                           "VALUES (1, 'ops', 't', 10) RETURNING hold_id").fetchone()[0]
    api_conn.execute("UPDATE hold SET released_minute = 20 WHERE hold_id = %s", (hid,))
    assert api_conn.execute("SELECT count(*) FROM decision").fetchone()[0] >= 1
    assert api_conn.execute("SELECT count(*) FROM call_fact").fetchone()[0] >= 1
    assert check_privileges(api_conn, fw_schema(api_conn)) == []


def fw_schema(conn) -> str:
    return conn.execute("SELECT current_schema()").fetchone()[0]


def test_owner_connection_is_refused_unless_allowed(world):
    app = create_app(dsn=world["dsn"], schema=world["schema"], allow_owner=False)
    with TestClient(app) as c:
        r = c.get("/supervisor/northwind_life")
        assert r.status_code == 503 and "superuser" in r.json()["detail"]
    app = create_app(dsn=world["dsn"], schema=world["schema"], allow_owner=True)
    with TestClient(app) as c:
        assert c.get("/supervisor/northwind_life").status_code == 200


# ------------------------------------------------------------------ views

def test_health_reports_role_and_no_privilege_problems(client, world):
    r = client.get("/health").json()
    assert r == {"ok": True, "schema": world["schema"], "role": "ctl_api",
                 "privilege_problems": []}


def test_supervisor_view_sections(client):
    r = client.get("/supervisor/northwind_life")
    assert r.status_code == 200
    v = r.json()
    assert v["now"] == "D3 15:00"
    wfs = {w["workflow_id"]: w for w in v["workflows"]}
    assert set(wfs) == {"nw_renewal", "nw_kyc"}
    nw = wfs["nw_renewal"]
    k = nw["kpis_today"]
    assert k["pickup_rate"] == pytest.approx(140 / 500) and k["engagement_rate"] == \
        pytest.approx(77 / 140)
    assert "never combined" in k["note"]
    (pending,) = nw["needs_your_decision"]
    assert pending["proposal_id"] == fw.PID_PENDING and pending["minutes_to_expiry"] == 1200
    assert pending["impact_kpi"] == "engagement_rate"
    assert [c["version"] for c in nw["changed_today"]] == [3]
    assert nw["changed_today"][0]["decision"] == "/explain/decision/2"
    words = {t["option"]: t["words"] for t in nw["being_tested"]}
    assert words["V3"] == "V3 for age60p~a1b2c3: 10% of calls, probably better, not yet proven"
    (win,) = nw["winning"]
    assert win["option"] == "W2" and win["ci95"] == [0.02, 0.14] and \
        win["holdout"]["control"] == "W1"
    assert [d["caller_id"] for d in nw["health"]["caller_ids"]] == ["D1", "D2", "D3", "D4"]
    (reach,) = nw["where_your_default_does_not_reach"]
    assert reach["cohort"] == fw.COHORT and reach["overrides"] == ["voice"]
    kyc = wfs["nw_kyc"]
    assert kyc["health"]["no_healthy_caller_id"] is True
    assert kyc["upcoming"]["announced_auto_applies"][0]["minutes_until"] == 60
    assert v["weekly_digest"]["items"][0]["workflow_id"] == "nw_renewal"
    assert {c["pool"] for c in v["credits"]} == {"R", "S"}


def test_supervisor_view_incident_timeline(client):
    ks = client.get("/supervisor/kestrel_finance").json()["workflows"][0]
    (inc,) = ks["health"]["incidents"]
    assert inc["incident_id"] == "INC-0001" and inc["open"]
    assert [t["action"] for t in inc["timeline"]] == ["incident_opened"]
    assert ks["health"]["flag_rate_monitor"]["prompt"] == "P4"
    assert ks["needs_your_decision"][0]["class_name"] == "incident"


def test_unknown_tenant_404(client):
    assert client.get("/supervisor/nobody").status_code == 404


def test_explain_endpoints(client):
    r = client.get("/explain/change", params={"workflow": "nw_renewal", "at": "D3 08:00"})
    assert r.status_code == 200 and r.json()["approval"]["approval_id"] == "apr_00012"
    assert "What happened after" in r.json()["text"]
    r = client.get("/explain/call", params={"lead": "L-100", "day": 3})
    assert r.status_code == 200
    assert r.json()["calls"][0]["dims"][1]["experiment_id"] == "exp_0007"
    assert client.get("/explain/call", params={"lead": "nope", "day": 3}).status_code == 404
    assert client.get("/explain/change", params={"workflow": "nw_renewal",
                                                 "at": "soon"}).status_code == 422
    d = client.get("/explain/decision/1").json()
    assert d["rule_id"] == "toy_gate" and d["proposals"] == [fw.PID_ROUTINE]
    assert client.get("/explain/decision/999").status_code == 404


def test_ops_view(client):
    o = client.get("/ops").json()
    assert o["now"] == "D3 15:00" and o["ingest"]["lag_sim_min"] == 0
    assert o["heartbeat"]["pid"] == 4242
    assert o["recent_steps"][0]["step_no"] == 210
    assert {w["workflow_id"]: w["in_flight"] for w in o["workflows"]} == \
        {"nw_renewal": 1, "ks_cancel": 1, "nw_kyc": 1}
    assert o["credits_remaining"] == 158


def test_post_hold_then_release(client, world):
    body = {"tenant_id": "northwind_life", "workflow_id": "nw_renewal", "set_by": "Asha (ops)",
            "reason": "board review", "until": "D4 12:00"}
    r = client.post("/supervisor/holds", json=body)
    assert r.status_code == 201, r.text
    hold = r.json()
    assert hold["from"] == "D3 15:00" and hold["until"] == "D4 12:00"
    holds = client.get("/supervisor/northwind_life").json()["workflows"][1]["holds"]
    assert [h["hold_id"] for h in holds] == [hold["hold_id"]]
    r = client.post(f"/supervisor/holds/{hold['hold_id']}/release", json={"released_by": "Asha"})
    assert r.status_code == 200 and r.json()["released_by"] == "Asha"
    with psycopg.connect(world["dsn"]) as c:     # ADR 0028: a column, the reason untouched
        row = c.execute(sql.SQL("SELECT reason, released_by FROM {}.hold WHERE hold_id = %s")
                        .format(sql.Identifier(world["schema"])), (hold["hold_id"],)).fetchone()
    assert row == ("board review", "Asha")
    assert client.post(f"/supervisor/holds/{hold['hold_id']}/release",
                       json={"released_by": "Asha"}).status_code == 404
    assert client.get("/supervisor/northwind_life").json()["workflows"][1]["holds"] == []
    with psycopg.connect(world["dsn"]) as c:
        c.execute(sql.SQL("DELETE FROM {}.hold").format(sql.Identifier(world["schema"])))


def test_post_hold_validation(client):
    base = {"tenant_id": "northwind_life", "workflow_id": "nw_renewal", "set_by": "ops"}
    assert client.post("/supervisor/holds", json={**base, "workflow_id": "ks_cancel"}
                       ).status_code == 404          # another tenant's workflow
    assert client.post("/supervisor/holds", json={**base, "until": "D1 10:00"}
                       ).status_code == 422          # in the past
    assert client.post("/supervisor/holds", json={**base, "until": "later"}).status_code == 422
    assert client.post("/supervisor/holds", json={**base, "set_by": ""}).status_code == 422


def test_uvicorn_factory_serves_with_env_configuration(world):
    """The deployment path: uvicorn --factory with the API's environment variables."""
    import os
    import socket
    import subprocess
    import sys
    import time

    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "CTL_API_PG_DSN": world["api_dsn"], "CTL_API_SCHEMA": world["schema"]}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory",
                          "controller.api.app:create_app", "--port", str(port),
                          "--log-level", "warning"], env=env)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline or p.poll() is not None:
                    raise
                time.sleep(0.2)
        assert r.status_code == 200 and r.json()["role"] == "ctl_api"
        assert httpx.get(f"http://127.0.0.1:{port}/ops").json()["now"] == "D3 15:00"
    finally:
        p.terminate()
        p.wait(10)
