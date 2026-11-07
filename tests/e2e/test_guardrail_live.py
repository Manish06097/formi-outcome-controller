"""AUDIT-2 F3: the live loop refuses a change whose forecast breaches the guardrail and writes the
`5.10/forecast` decision.

No public world breaches the guardrail in a way the loop must refuse, but the private variant
`seed_p01_s1101` does it naturally (the forecast for a caller-ID spread on nw_renewal is a 0.18
point drop of the pickup guardrail against a 0.1 point tolerance), so this test runs the real
controller against it and reads the store; nothing is faked. The P0 exemption (a drain is never
refused) has its unit test in `tests/unit/test_connects.py`.
"""
from __future__ import annotations

import pytest

from controller.settings import Thresholds
from tests.e2e.platform.helpers import (SIM_PORT, create_database, drop_database, latest_schema,
                                        query, run_controller)
from tests.support.sim_process import SimProcess

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

SCENARIO = "scenarios/private/seed_p01_s1101.yaml"
T = Thresholds()


@pytest.fixture(scope="module")
def world(pg_dsn, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("guardrail_live")
    dsn = create_database(pg_dsn)
    sim = SimProcess(SCENARIO, workdir, port=SIM_PORT, ready_timeout_s=600).start()
    try:
        run = run_controller(sim.url, dsn, workdir / "controller.log", {"CTL_LEARNER": "probe"},
                             timeout_s=3000)
    finally:
        sim.stop()
    schema = latest_schema(dsn)
    try:
        yield {"run": run, "dsn": dsn, "schema": schema}
    finally:
        drop_database(pg_dsn, dsn)


def _q(w, sql, params=None):
    return query(w["dsn"], w["schema"], sql, params)


def test_the_run_finishes_clean(world):
    assert world["run"].final == 0


def test_a_breaching_change_is_refused_with_a_forecast_decision_and_an_alert(world):
    rows = _q(world, """SELECT decision_id, sim_minute, inputs, outcome FROM decision
                        WHERE rule_id = '5.10/forecast' AND outcome->'dropped' <> '[]'::jsonb""")
    assert rows, "no breaching change met the live guardrail filter in this world"
    for did, minute, inputs, outcome in rows:
        assert inputs["tolerance"] == T.guardrail_drop_tol
        for d in outcome["dropped"]:
            assert d["drop"] > T.guardrail_drop_tol or d["p_breach"] > T.guardrail_p_breach
            kept = [tuple(p) for p in outcome["kept"]]
            assert tuple(d["path"]) not in kept
        alerts = _q(world, """SELECT count(*) FROM alert WHERE code = 'HARM_GATE_BLOCK'
                              AND message LIKE %s""", (f"%decision #{did})",))
        assert alerts[0][0] >= 1, (did, minute)
