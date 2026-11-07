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

Governance cells (DESIGN.md 15.1 M3, step I2): the same matrix for `submit_approval`, `apply`
and `auto_apply` with the probe-only learner on, so the run submits, applies and auto-applies.
A crashed run must leave the simulator with the same approvals and config versions (same ids,
minutes, statuses, bodies' dims) and the store with the same proposals, patches, DID states and
approval mirror as the clean run. Decision and intent rows are not compared: reconciliation
adds its own rows and resolutions, which is the audit trail of the crash.
"""
from __future__ import annotations

import re

import pytest

from controller.simclient import SimClient
from tests.e2e.platform.helpers import (DOMAIN_TABLES, SHORT, WALL_COLUMNS, create_database,
                                        diff_dumps, done_advances, drop_database, dump_table,
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
GOV_CELLS = ([(k, 1, ph) for k in ("submit_approval", "apply", "auto_apply")
              for ph in ("before_t1", "after_t1", "after_http", "in_t2")]
             + [(k, 3, ph) for k in ("submit_approval", "apply", "auto_apply")
                for ph in ("after_t1", "after_http")])
GOV_COMPARED = COMPARED + ("proposal", "patch", "path_owner", "cooldown", "approval_mirror",
                           "did_state", "loop_stage")
GOV_EXCLUDE = {"proposal": {"decision_id"}, "patch": {"decision_id"}}
"""Decision ids are identity values: a crashed run writes extra decision rows (reconciliation),
so the ids a proposal or patch points at differ while everything they say is the same."""


DECISION_REFS = (re.compile(r"decision #\d+"), re.compile(r"'decision_ids': \[[\d, ]*\]"))


def _without_decision_refs(rows: list[str]) -> list[str]:
    """Rationale and evidence quote decision ids; only the ids may differ."""
    out = []
    for r in rows:
        for pat in DECISION_REFS:
            r = pat.sub("<decision>", r)
        out.append(r)
    return sorted(out)


def _sim_view(url: str) -> dict:
    c = SimClient(url)
    return {"approvals": sorted((a.proposal_id, a.workflow_id, a.status, a.base_version,
                                 a.changed_dims, a.created_at, a.decided_at, a.apply_by)
                                for a in c.approvals()),
            "versions": {w.workflow_id: [(v.version, v.actor, v.changed_dims, v.proposal_id,
                                          v.approval_id, v.applied_at)
                                         for v in c.config_history(w.workflow_id)]
                         for w in c.workflows()}}


def _one_run(base_dsn: str, workdir, fault: str | None, learner: str = "none",
             tables: tuple[str, ...] = COMPARED, scenario: str = "scenarios/public_01_dev.yaml"):
    dsn = create_database(base_dsn)
    sim = SimProcess(scenario, workdir, port=SIM_PORT, overrides=SHORT,
                     ready_timeout_s=300).start()
    try:
        extra = {"CTL_FAULT": fault} if fault else {}
        extra["CTL_LEARNER"] = learner
        run = run_controller(sim.url, dsn, workdir / "controller.log", extra, max_restarts=5)
        st = SimClient(sim.url).state()
        schema = latest_schema(dsn)
        dump = {t: _without_decision_refs(dump_table(
                    dsn, schema, t, WALL_COLUMNS.get(t, set()) | GOV_EXCLUDE.get(t, set())))
                for t in tables}
        out = {"codes": run.exit_codes, "log": run.log, "state": st, "schema": schema,
               "dump": dump, "sim": _sim_view(sim.url),
               "advances": done_advances(dsn, schema),
               "events": query(dsn, schema, "SELECT sum(events) FROM ingested_chunk")[0][0],
               "faults": query(dsn, schema, """SELECT subject_id FROM audit_log
                                               WHERE action = 'fault_injected'"""),
               "bootstrap": query(dsn, schema, """SELECT details FROM audit_log
                                                  WHERE action = 'bootstrap_completed'"""),
               "intents": query(dsn, schema, """SELECT status, count(*) FROM intent
                                                GROUP BY 1 ORDER BY 1"""),
               "gov_intents": query(dsn, schema, """SELECT kind, count(*) FROM intent
                                                    WHERE status = 'done' GROUP BY 1"""),
               "ledger": query(dsn, schema, """SELECT purpose, pool, cost FROM credit_ledger
                                               ORDER BY seq"""),
               "envelope": query(dsn, schema, """SELECT pool, tenant, allotted, spent
                                                 FROM credit_envelope ORDER BY 1, 2"""),
               "incidents": query(dsn, schema, """SELECT incident_id, state FROM incident
                                                  ORDER BY 1"""),
               "runs": query(dsn, schema, """SELECT count(*), count(*) FILTER (WHERE designated)
                                             FROM validation_run"""),
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


@pytest.fixture(scope="module")
def clean_gov(pg_dsn, tmp_path_factory):
    out = _one_run(pg_dsn, tmp_path_factory.mktemp("clean_gov"), None, "probe", GOV_COMPARED)
    assert out["codes"] == [0]
    done = dict(out["gov_intents"])
    for kind in ("submit_approval", "apply", "auto_apply"):
        assert done.get(kind, 0) >= 3, f"the clean run must exercise {kind} 3 times: {done}"
    return out


@pytest.mark.parametrize("kind,n,phase", GOV_CELLS, ids=[f"{k}:{n}:{p}" for k, n, p in GOV_CELLS])
def test_crash_equivalence_governance(clean_gov, pg_dsn, tmp_path, kind, n, phase):
    spec = f"{kind}:{n}:{phase}"
    out = _one_run(pg_dsn, tmp_path, spec, "probe", GOV_COMPARED)
    assert out["codes"][0] == 137 and out["codes"][-1] == 0, out["log"][-3000:]
    assert len(out["codes"]) == 2, "the fault fires once per run"
    assert out["faults"] == [(spec,)]
    assert out["state"].finished and out["events"] == out["state"].last_event_id
    assert out["advances"] == clean_gov["advances"], "the same stops, in the same order"
    assert out["sim"] == clean_gov["sim"], "the same approvals and config versions"
    assert diff_dumps(clean_gov["dump"], out["dump"]) == []
    assert ("started", 1) not in out["intents"]


# ---------------------------------------------------------------------------- money cells (I5)
# The credit-spending kinds (DESIGN.md 8.3, AUDIT-1 finding 4): a world whose business defaults
# are unsafe, so the run makes D0 screens, opens incidents, calls suggest-fixes once per incident,
# validates fix candidates and proposes P0 prompt fixes.

MONEY_SCENARIO = "scenarios/private/unsafe_default_prompt_p01.yaml"
MONEY_CELLS = ([(k, 1, ph) for k in ("validation", "suggest_fixes")
                for ph in ("before_t1", "after_t1", "after_http", "in_t2")]
               + [("validation", 3, "after_http"), ("validation", 5, "in_t2")])
MONEY_COMPARED = COMPARED + ("proposal", "patch", "did_state", "loop_stage")


def _ledger_total(rows) -> float:
    return sum(float(r[2]) for r in rows)


def _check_money_invariants(out, c0: float) -> None:
    """The ledger is the credit balance: what the simulator charged is exactly what we recorded,
    and each pool's envelope spend is its ledger sum."""
    assert abs(c0 - out["state"].credits_remaining - _ledger_total(out["ledger"])) < 1e-6, \
        (c0, out["state"].credits_remaining, out["ledger"])
    by_pool: dict[str, float] = {}
    for _, pool, cost in out["ledger"]:
        by_pool[pool] = by_pool.get(pool, 0.0) + float(cost)
    env: dict[str, float] = {}
    for pool, _, _, spent in out["envelope"]:
        env[pool] = env.get(pool, 0.0) + float(spent)
    for pool in set(by_pool) | {p for p, v in env.items() if v}:
        assert abs(by_pool.get(pool, 0.0) - env.get(pool, 0.0)) < 1e-6, (pool, by_pool, env)


@pytest.fixture(scope="module")
def clean_money(pg_dsn, tmp_path_factory):
    out = _one_run(pg_dsn, tmp_path_factory.mktemp("clean_money"), None, "probe", MONEY_COMPARED,
                   MONEY_SCENARIO)
    assert out["codes"] == [0]
    done = dict(out["gov_intents"])
    assert done.get("validation", 0) >= 5 and done.get("suggest_fixes", 0) >= 1, done
    out["c0"] = out["state"].credits_remaining + _ledger_total(out["ledger"])
    _check_money_invariants(out, out["c0"])
    return out


@pytest.mark.parametrize("kind,n,phase", MONEY_CELLS,
                         ids=[f"{k}:{n}:{p}" for k, n, p in MONEY_CELLS])
def test_crash_equivalence_money(clean_money, pg_dsn, tmp_path, kind, n, phase):
    """A crash around a validation or suggest-fixes call never double-spends and never loses the
    spend. Before the call (before_t1, after_t1) the world and store equal the clean run's. After
    the call (after_http, in_t2) the call's result is lost: its credits are in the ledger, a
    validation is paid once more (at most one extra run), a suggest-fixes call is not repeated,
    and the run still resolves every incident."""
    spec = f"{kind}:{n}:{phase}"
    out = _one_run(pg_dsn, tmp_path, spec, "probe", MONEY_COMPARED, MONEY_SCENARIO)
    assert out["codes"][0] == 137 and out["codes"][-1] == 0, out["log"][-3000:]
    assert len(out["codes"]) == 2, "the fault fires once per run"
    assert out["faults"] == [(spec,)]
    assert out["state"].finished and out["events"] == out["state"].last_event_id
    assert ("started", 1) not in out["intents"]
    _check_money_invariants(out, clean_money["c0"])
    if phase in ("before_t1", "after_t1"):
        assert out["advances"] == clean_money["advances"]
        assert out["sim"] == clean_money["sim"], "the same approvals and config versions"
        assert out["ledger"] == clean_money["ledger"]
        assert diff_dumps(clean_money["dump"], out["dump"]) == []
        return
    extra = _ledger_total(out["ledger"]) - _ledger_total(clean_money["ledger"])
    assert 0 <= extra <= 10, f"a lost call costs at most one extra run (10 credits): {extra}"
    lost = [r for r in out["ledger"] if r not in clean_money["ledger"]]
    n_runs_clean, n_runs = clean_money["runs"][0][0], out["runs"][0][0]
    if kind == "validation":
        assert n_runs <= n_runs_clean + 1
    else:
        assert extra == 0 and n_runs == n_runs_clean, (extra, lost)
    assert all(state == "resolved" for _, state in out["incidents"]), out["incidents"]
    assert [i for i, _ in out["incidents"]] == [i for i, _ in clean_money["incidents"]]
