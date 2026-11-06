"""Crash-equivalence matrix for the M2 kinds (DESIGN.md 10.2 step 2, 15.1 M2; 15.1 M1 (4)).

One clean run of shortened public_01, then one run per (kind, n, phase) with CTL_FAULT making
the controller `os._exit(137)` (the effect of SIGKILL) the n-th time it reaches that point,
restarted as run.sh would. Every crashed run must finish and leave the same world and the same
store as the clean run:
- the same advances, in the same order (the simulator's clock went through the same stops, so
  it issued the same dials: identical simulator state),
- every event ingested exactly once (ingested events equal /sim/state last_event_id),
- identical facts, rollups, catalog and config tables.
The kill mid-COPY (`chunk_copy:1:mid`) crashes inside the history chunk's COPY; the bootstrap
crash checks that an interrupted bootstrap reruns.
"""
from __future__ import annotations

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import (DOMAIN_TABLES, SHORT, create_database, diff_dumps,
                                        done_advances, drop_database, dump_schema,
                                        latest_schema, query, run_controller)
from tests.support.sim_process import SimProcess
from tests.e2e.platform.helpers import SIM_PORT

pytestmark = [pytest.mark.e2e, pytest.mark.pg, pytest.mark.slow]

CELLS = ([("advance", n, ph) for ph in ("before_t1", "after_t1", "after_http", "in_t2")
          for n in (1, 3)]
         + [("chunk_transform", n, ph) for ph in ("before_t1", "in_t2", "after_t2")
            for n in (1, 3)]
         + [("chunk_copy", 1, "mid"), ("audit_write", 2, "in_t2"), ("bootstrap", 1, "mid")])
COMPARED = DOMAIN_TABLES + ("ingested_chunk",)


def _one_run(base_dsn: str, workdir, fault: str | None):
    dsn = create_database(base_dsn)
    sim = SimProcess("scenarios/public_01_dev.yaml", workdir, port=SIM_PORT, overrides=SHORT,
                     ready_timeout_s=300).start()
    try:
        extra = {"CTL_FAULT": fault} if fault else {}
        run = run_controller(sim.url, dsn, workdir / "controller.log", extra, max_restarts=5)
        st = SimClient(sim.url).state()
        schema = latest_schema(dsn)
        out = {"codes": run.exit_codes, "log": run.log, "state": st, "schema": schema,
               "dump": dump_schema(dsn, schema, COMPARED),
               "advances": done_advances(dsn, schema),
               "events": query(dsn, schema, "SELECT sum(events) FROM ingested_chunk")[0][0],
               "faults": query(dsn, schema, """SELECT subject_id FROM audit_log
                                               WHERE action = 'fault_injected'"""),
               "bootstrap": query(dsn, schema, """SELECT details FROM audit_log
                                                  WHERE action = 'bootstrap_completed'"""),
               "intents": query(dsn, schema, """SELECT status, count(*) FROM intent
                                                GROUP BY 1 ORDER BY 1"""),
               "history": {w.workflow_id: len(SimClient(sim.url).config_history(w.workflow_id))
                           for w in SimClient(sim.url).workflows()}}
        return out
    finally:
        sim.stop()
        drop_database(base_dsn, dsn)


@pytest.fixture(scope="module")
def clean(pg_dsn, tmp_path_factory):
    out = _one_run(pg_dsn, tmp_path_factory.mktemp("clean"), None)
    assert out["codes"] == [0]
    return out


@pytest.mark.parametrize("kind,n,phase", CELLS, ids=[f"{k}:{n}:{p}" for k, n, p in CELLS])
def test_crash_equivalence(clean, pg_dsn, tmp_path, kind, n, phase):
    spec = f"{kind}:{n}:{phase}"
    out = _one_run(pg_dsn, tmp_path, spec)
    assert out["codes"][0] == 137 and out["codes"][-1] == 0, out["log"][-3000:]
    assert len(out["codes"]) == 2, "the fault fires once per run"
    assert out["faults"] == [(spec,)]
    st = out["state"]
    assert st.finished and out["events"] == st.last_event_id
    assert out["state"].last_event_id == clean["state"].last_event_id
    assert out["state"].chunks == clean["state"].chunks
    assert out["history"] == clean["history"], "no config change in either run"
    assert out["advances"] == clean["advances"], "the same stops, in the same order"
    assert diff_dumps(clean["dump"], out["dump"]) == []
    assert ("started", 1) not in out["intents"]
    if kind == "bootstrap":
        assert [d[0]["rerun"] for d in out["bootstrap"]] == [True]
