"""A small, hand-written run schema for the audit, API and alert tests (track T5).

It is written in the shapes of DESIGN.md 3.2 and the writer conventions stated in
`controller/audit/explain.py`, so the explain queries, the supervisor view and the replay are
tested against a database before the tracks that write it in production have landed.

The world, at sim minute D3 15:00 (clock):
- northwind_life: nw_renewal (wf 1) and nw_kyc (wf 3); kestrel_finance: ks_cancel (wf 2).
- nw_renewal v1 business default; v2 (D2 11:40, approved apr_00012) adds cohort
  age60p~a1b2c3 with a V3 arm of exp_0007 at 10%; v3 (D3 09:30, auto-applied) spreads caller ID
  onto a D3 probe. A routine request apr_00015 is pending.
- ks_cancel v2 is a supervisor prompt edit to P4 at D3 11:30; incident INC-0001 follows, with a
  fix pending as apr_00016.
- nw_kyc has an announced auto-apply (noticed until D3 16:00).
- Lead L-100 (60+, hi) was called on D2 and D3; lead L-300 heard a voice with no provenance
  (a COHORT_MISMATCH to be found).
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import psycopg

from controller.audit.replay import encode_array, encode_inputs, hash_inputs
from controller.settings import load_settings
from controller.store.db import Database

FINGERPRINT = "ab12cd34ef56" + "0" * 52
API_PASSWORD = "ctl_api_test"

D = 1440


def m(day: int, hh: int, mm: int = 0) -> int:
    return day * D + hh * 60 + mm


NOW = m(3, 15)
PID_ROUTINE = "a1b2c3-nw_renewal-D2T1000-3-0004"
PID_AUTO = "a1b2c3-nw_renewal-D3T0915-3-0005"
PID_PENDING = "a1b2c3-nw_renewal-D3T1100-3-0006"
PID_FIX = "a1b2c3-ks_cancel-D3T1300-0-0001"
PID_NOTICED = "a1b2c3-nw_kyc-D3T1500-3-0001"
COHORT = "age60p~a1b2c3"

SPEC_OPTIONS = {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2", "V3", "V4"],
                "caller_id": ["D1", "D2", "D3", "D4"], "prompt_version": ["P1", "P3", "P4", "P5"]}


def _spec(workflow_id: str, tenant: str, primary: str, cap: int) -> dict[str, Any]:
    return {"workflow_id": workflow_id, "tenant_id": tenant, "use_case": f"{workflow_id} use case",
            "options": SPEC_OPTIONS, "objective": {"primary": primary},
            "governance": {"max_requests_per_24h": cap, "approver_hours": [10, 19],
                           "proposal_ttl_hours": 24}}


# seeded "gate" used by the replay tests: P(de < -1%) from draws of N(mean, cov) of a 1-param model
def toy_fit(inputs: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    eng, con = inputs["engaged"].astype(float), inputs["connected"].astype(float)
    rate = (eng + 1) / (con + 2)
    lr = np.log(rate[1] / rate[0])
    var = (1 / (eng + 1)).sum()
    return np.array([lr]), np.array([[var]])


def toy_gate(mean: np.ndarray, cov: np.ndarray, seed: int, n: int) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    draws = rng.normal(mean[0], np.sqrt(cov[0, 0]), n)
    de = np.exp(draws) - 1.0
    return {"de_mean": float(de.mean()), "p_de_below_1pct": float((de < -0.01).mean())}


def build(dsn: str) -> str:
    """Create a fresh run schema, fill it, give ctl_api a login. Returns the schema name."""
    db = Database(dsn)
    db.ensure_ctl()
    schema = db.create_run_schema(FINGERPRINT, "http://127.0.0.1:8715", sim_days=8)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"ALTER ROLE ctl_api LOGIN PASSWORD '{API_PASSWORD}'")
    with db.connect(schema=schema) as conn:
        with conn.transaction():
            _fill(conn)
    return schema


def api_dsn(dsn: str) -> str:
    parts = dict(p.split("=", 1) for p in dsn.split())
    parts["user"], parts["password"] = "ctl_api", API_PASSWORD
    # libpq defaults dbname to the user name; a DSN without dbname (CTL_TEST_PG_DSN) would then
    # name a database "ctl_api" that does not exist. Compose connects to dbname=postgres.
    parts.setdefault("dbname", "postgres")
    return " ".join(f"{k}={v}" for k, v in parts.items())


def _j(x: Any) -> str:
    return json.dumps(x)


def _fill(c: Any) -> None:
    settings_hash = load_settings().settings_hash()
    ex = c.execute
    ex("INSERT INTO clock VALUES (1, %s, %s, 15, 210)", (NOW, 8 * D))
    ex("INSERT INTO heartbeat VALUES (1, now(), 4242, 210)")
    for wf, wid, tenant, primary, cap in ((1, "nw_renewal", "northwind_life", "engagement_rate", 3),
                                          (2, "ks_cancel", "kestrel_finance", "engagement_rate", 4),
                                          (3, "nw_kyc", "northwind_life", "pickup_rate", 2)):
        ex("INSERT INTO workflow VALUES (%s, %s, %s, %s, 'h', 0)",
           (wf, wid, tenant, _j(_spec(wid, tenant, primary, cap))))
    ex("INSERT INTO cell VALUES (1, 'hi', '1', '60+', 'mid'), (2, 'en', '2', '18-30', 'low')")
    ex("INSERT INTO cohort_def VALUES (%s, 1, %s, 'dh', '{1}', 'grid', %s)",
       (COHORT, _j({"age_band": ["60+"]}), m(2, 10)))
    ex("INSERT INTO cohort_def VALUES ('vip~0f0f0f', 2, %s, 'dh2', '{}', 'business', %s)",
       (_j({"value_band": ["high"]}), -D))

    # ---------------------------------------------------------------- decisions and snapshots
    inputs = {"engaged": np.array([400, 52]), "connected": np.array([800, 95])}
    mean, cov = toy_fit(inputs)
    seed = 987654321
    gate = toy_gate(mean, cov, seed, 4000)
    ex("""INSERT INTO decision (decision_id, sim_minute, wf, module, kind, rule_id, inputs, outcome,
                                harm_report, snapshot_ids, seed, settings_hash, code_version)
          VALUES (1, %s, 1, 'test.toy', 'explore', 'toy_gate', %s, %s, %s, '{1}', %s, %s, 'dev')""",
       (m(2, 9, 45), _j({"arm": "V3", "cohort": COHORT, "n_draws": 4000}),
        _j({"share": 0.1, **gate}),
        _j({"de_mean": gate["de_mean"], "p_de_below_1pct": gate["p_de_below_1pct"],
            "dd_mean": 0.0, "p_dd_below_2pct": 0.0, "passes": True}), seed, settings_hash))
    ex("""INSERT INTO posterior_snapshot (snapshot_id, wf, decision_id, kind, inputs, inputs_hash,
                                          mean, cov, summary, seed, code_version, settings_hash)
          VALUES (1, 1, 1, 'ew', %s, %s, %s, %s, '{}', %s, 'dev', %s)""",
       (encode_inputs(inputs), hash_inputs(inputs), encode_array(mean), encode_array(cov), seed,
        settings_hash))
    decisions = [
        (2, m(3, 9, 15), 1, "learning.did", "probe", "did_probe", {"did": "D3"}, {"share": 0.2}),
        (3, m(3, 10), 1, "learning.did", "no_action", "did_spread", {}, {"reason": "spread ok"}),
        (4, m(3, 12, 45), 2, "incidents.manager", "open_incident", "flag_cusum",
         {"stat": 5.2}, {"incident": "INC-0001"}),
        (5, m(3, 11), 1, "governance.release", "routine", "routine_slot", {}, {"submit": True}),
        (6, m(3, 15), 3, "learning.did", "spread", "did_spread", {}, {"share": 0.33}),
        (7, m(1, 9), 1, "learning.winners", "declare", "w1_rule", {},
         {"effect": 0.08, "ci95": [0.02, 0.14]}),
    ]
    for did, sm, wf, mod, kind, rule, inp, out in decisions:
        ex("""INSERT INTO decision (decision_id, sim_minute, wf, module, kind, rule_id, inputs,
                                    outcome, seed, settings_hash, code_version)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, 'dev')""",
           (did, sm, wf, mod, kind, rule, _j(inp), _j(out), settings_hash))

    # ---------------------------------------------------------------- config versions
    ex("""INSERT INTO config_version VALUES
          (1, 1, 'business_default', NULL, NULL, NULL, NULL, NULL, '{}', 'fetched'),
          (1, 2, 'controller_approved', NULL, NULL, %s, %s, 'apr_00012', '{cohorts,voice}', 'ours'),
          (1, 3, 'controller_auto', NULL, NULL, %s, %s, NULL, '{caller_id}', 'ours'),
          (2, 1, 'business_default', NULL, NULL, NULL, NULL, NULL, '{}', 'fetched'),
          (2, 2, 'supervisor', NULL, NULL, %s, NULL, NULL, '{prompt_version}', 'fetched'),
          (3, 1, 'business_default', NULL, NULL, NULL, NULL, NULL, '{}', 'fetched')""",
       (m(2, 11, 40), PID_ROUTINE, m(3, 9, 30), PID_AUTO, m(3, 11, 30)))
    for wf, v, coh in ((1, 1, False), (1, 2, True), (1, 3, True), (2, 1, False), (2, 2, False),
                       (3, 1, False)):
        if coh:
            ex("INSERT INTO config_cohort VALUES (%s, %s, 0, %s, %s, 1, 'ours', '{1}')",
               (wf, v, COHORT, COHORT))
        ex("INSERT INTO config_cohort VALUES (%s, %s, %s, 'default', NULL, 0, 'ours', '{}')",
           (wf, v, 1 if coh else 0))
    prov = []

    def default_rows(wf: int, v: int, cohort: str, inherited: bool, caller: dict[int, tuple]):
        prov.append((wf, v, cohort, "welcome_message", 0, 1.0, "default", inherited))
        if cohort == "default":
            prov.append((wf, v, cohort, "voice", 0, 1.0, "default", inherited))
        for did, (share, src) in caller.items():
            prov.append((wf, v, cohort, "caller_id", did, share, src, inherited))

    half = {0: (0.5, "default"), 1: (0.5, "default")}
    spread = {0: (0.4, "default"), 1: (0.4, "default"), 2: (0.2, "probe")}
    for v, caller in ((1, half), (2, half), (3, spread)):
        default_rows(1, v, "default", False, caller)
        prov.append((1, v, "default", "prompt_version", 0, 1.0, "default", False))
        if v >= 2:
            default_rows(1, v, COHORT, True, caller)
    for v in (1, 2):
        default_rows(2, v, "default", False, half)
    prov.append((2, 1, "default", "prompt_version", 0, 1.0, "default", False))
    prov.append((2, 2, "default", "prompt_version", 2, 1.0, "supervisor", False))
    default_rows(3, 1, "default", False, {0: (1.0, "default")})
    prov.append((3, 1, "default", "prompt_version", 0, 1.0, "default", False))
    for r in prov:
        ex("INSERT INTO allocation_provenance (wf, version, cohort_key, dim, option, share, source,"
           " inherited) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", r)
    for v in (2, 3):  # the cohort's own voice allocation: an arm of exp_0007
        ex("""INSERT INTO allocation_provenance VALUES
              (1, %s, %s, 'voice', 0, 0.9, 'arm', false, 'exp_0007', %s, 'apr_00012', 1),
              (1, %s, %s, 'voice', 2, 0.1, 'arm', false, 'exp_0007', %s, 'apr_00012', 1)""",
           (v, COHORT, PID_ROUTINE, v, COHORT, PID_ROUTINE))

    # ---------------------------------------------------------------- proposals and approvals
    ex("""INSERT INTO proposal (proposal_id, wf, class, route, state, base_version, change_summary,
                                diff, affected_leads, dry_run, impact_score, rationale, evidence,
                                approval_id, submitted_minute, decided_minute, apply_by_minute,
                                applied_version, decision_id, verify)
          VALUES (%s, 1, 3, 'approval', 'verified', 1, 'Test V3 for over-60s at 10%%', %s, 812,
                  %s, 0.012, 'V3 may suit 60+; harm gate passes at 10%%', %s, 'apr_00012', %s,
                  %s, %s, 2, 1, %s)""",
       (PID_ROUTINE, _j({"voice": {COHORT: {"V3": 0.1}}}), _j({"headline": "engagement +0.1%"}),
        _j({"kpi": "engagement_rate", "notes": ["routine slot 10:00"]}), m(2, 10), m(2, 11, 30),
        m(2, 13, 30), _j({"result": "ok", "realised_share": 0.098})))
    ex("""INSERT INTO proposal (proposal_id, wf, class, route, state, base_version, change_summary,
                                rationale, evidence, submitted_minute, applied_version,
                                decision_id, impact_score, affected_leads)
          VALUES (%s, 1, 3, 'auto', 'applied', 2, 'Probe caller ID D3 at 20%%',
                  'D3 unknown; probe sized by the DID gate', %s, %s, 3, 2, 0.0, 0)""",
       (PID_AUTO, _j({"kpi": "pickup_rate"}), m(3, 9, 15)))
    ex("""INSERT INTO proposal (proposal_id, wf, class, route, state, base_version, change_summary,
                                impact_score, evidence, approval_id, submitted_minute, decision_id,
                                affected_leads, dry_run, p0_rank)
          VALUES (%s, 1, 3, 'approval', 'pending', 3, 'Ramp V3 for over-60s to 25%%', 0.02, %s,
                  'apr_00015', %s, 5, 640, %s, NULL),
                 (%s, 2, 0, 'approval', 'pending', 2, 'Restore prompt P1 (incident INC-0001)',
                  0.05, %s, 'apr_00016', %s, 4, 1500, %s, 0)""",
       (PID_PENDING, _j({"kpi": "engagement_rate", "expected_decision_window": ["D3 11:30",
                                                                                "D3 15:00"]}),
        m(3, 11), _j({"headline": "engagement +0.3%, worst case -0.2%"}),
        PID_FIX, _j({"kpi": "engagement_rate"}), m(3, 13), _j({"headline": "flag rate halves"})))
    ex("""INSERT INTO proposal (proposal_id, wf, class, route, state, base_version, change_summary,
                                notice_until_minute, decision_id, impact_score)
          VALUES (%s, 3, 3, 'auto', 'noticed', 1, 'Spread caller ID over D1, D2, D3', %s, 6, 0.1)""",
       (PID_NOTICED, m(3, 16)))
    ex("""INSERT INTO approval_mirror VALUES
          ('apr_00012', %s, 1, 'applied', %s, %s, %s, %s, NULL, %s, NULL),
          ('apr_00015', %s, 1, 'pending', %s, %s, NULL, NULL, NULL, %s, NULL),
          ('apr_00016', %s, 2, 'pending', %s, %s, NULL, NULL, NULL, %s, NULL)""",
       (PID_ROUTINE, m(2, 10), m(3, 10), m(2, 11, 30), m(2, 13, 30), m(2, 11, 45),
        PID_PENDING, m(3, 11), m(4, 11), NOW, PID_FIX, m(3, 13), m(4, 1), NOW))
    for seq, (fr, to, sm) in enumerate(((None, "draft", m(2, 9, 45)),
                                        ("draft", "submitting", m(2, 10)),
                                        ("submitting", "pending", m(2, 10)),
                                        ("pending", "approved", m(2, 11, 45)),
                                        ("approved", "applying", m(2, 11, 45)),
                                        ("applying", "applied", m(2, 11, 45)),
                                        ("applied", "verifying", m(2, 11, 45)),
                                        ("verifying", "verified", m(3, 9))), start=1):
        ex("INSERT INTO proposal_transition VALUES (%s, %s, %s, %s, %s, NULL, 1)",
           (PID_ROUTINE, seq, fr, to, sm))

    # ---------------------------------------------------------------- audit log (chains)
    rows = [
        (1, m(2, 10), 1, "controller", "submitted", "proposal", PID_ROUTINE,
         "requested approval apr_00012", None),
        (2, m(2, 11, 45), 1, "approver", "approved", "proposal", PID_ROUTINE,
         "apr_00012 approved (decided D2 11:30)", 1),
        (3, m(2, 11, 45), 1, "controller", "applied", "config_version", "nw_renewal@v2",
         "applied as version 2", 2),
        (4, m(3, 9), 1, "controller", "verified", "proposal", PID_ROUTINE,
         "realised share 9.8% within 3 SE; EW P(de < -1%) 1%", 3),
        (5, m(3, 11, 30), 2, "supervisor", "supervisor_edit", "config_version", "ks_cancel@v2",
         "supervisor changed prompt_version to P4", None),
        (6, m(3, 12, 45), 2, "controller", "incident_opened", "incident", "INC-0001",
         "flag CUSUM over h after the supervisor prompt", 5),
        (7, m(3, 13), 2, "controller", "submitted", "proposal", PID_FIX,
         "requested approval apr_00016 to restore P1", 6),
        (8, m(3, 9, 30), 1, "controller", "auto_applied", "proposal", PID_AUTO,
         "probe D3 at 20% auto-applied as version 3", None),
        (9, m(3, 10), 1, "controller", "unrelated", "proposal", "other", "not in any chain",
         None),
    ]
    for r in rows:
        ex("""INSERT INTO audit_log (audit_id, sim_minute, wf, actor, action, subject_type,
                                     subject_id, summary, caused_by)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", r)

    # ---------------------------------------------------------------- learning and health state
    ex("""INSERT INTO experiment VALUES
          ('exp_0007', 1, 'voice', '{0,2}', 0, 1, 'running', 2, %s, NULL),
          ('exp_0003', 1, 'welcome_message', '{0,1}', 0, 1, 'decided', 1, %s, NULL)""",
       (m(2, 11, 45), m(0, 10)))
    ex("""INSERT INTO experiment_arm VALUES
          ('exp_0007', 2, %s, 'live', 0.1, %s, NULL, 1),
          ('exp_0007', 0, %s, 'control', 0.9, %s, NULL, 1)""",
       (COHORT, m(2, 12), COHORT, m(2, 12)))
    ex("INSERT INTO winner_look VALUES ('exp_0007', 2, %s, 1, 120, %s, 0.86, 0.6, NULL, false, 1)",
       (COHORT, m(3, 9)))
    ex("INSERT INTO winner (winner_id, exp_id, wf, dim, arm, scope, look_no, declared_minute, "
       "decision_id, state) VALUES (1, 'exp_0003', 1, 'welcome_message', 1, 'population', 2, "
       "%s, 7, 'promoted')", (m(1, 9),))
    ex("INSERT INTO holdout VALUES (1, 'population', 'welcome_message', 1, 0, 0.1, 2, 'active')")
    ex("INSERT INTO decay_monitor VALUES (1, 'population', 'welcome_message', 1, 0.8, 6, 0.08, "
       "false)")
    ex("""INSERT INTO did_state (wf, did, status, r_hat, n_dials) VALUES
          (1, 0, 'healthy', 1.0, 4000), (1, 1, 'healthy', 0.98, 4000), (1, 2, 'probing', NULL, 60),
          (1, 3, 'unhealthy', 0.55, 160), (2, 0, 'healthy', 1.0, 5000), (2, 1, 'suspect', 0.8, 5000),
          (3, 0, 'degraded', 0.7, 3000)""")
    ex("INSERT INTO incident VALUES ('INC-0001', 2, 'hallucination', 'fixing', %s, %s, NULL, %s, "
       "%s, 16)", (m(3, 12, 45), m(3, 12, 45), _j({"stat": 5.2}), PID_FIX))
    ex("INSERT INTO incident VALUES ('INC-0000', 3, 'did_flagged', 'open', %s, NULL, NULL, NULL, "
       "NULL, 0)", (m(3, 10),))
    ex("INSERT INTO flag_monitor VALUES (2, 2, 0.012, 0.024, 5.0, 5.2, 14, 900, 99, true, %s)",
       (m(3, 11, 30),))
    ex("INSERT INTO drift_monitor VALUES (1, 'pickup_rate', 1.2, 0.3, 70, 'history', false)")
    ex("INSERT INTO credit_envelope VALUES ('R', '*', 80, 16, 'normal'), "
       "('S', 'northwind_life', 25, 20, 'normal'), ('S', 'kestrel_finance', 15, 6, 'normal')")
    ex("INSERT INTO credit_ledger (sim_minute, tenant, wf, pool, purpose, cost, remaining_after) "
       "VALUES (%s, 'kestrel_finance', 2, 'R', 'verdict', 13, 158)", (m(3, 12, 50),))

    # ---------------------------------------------------------------- facts and rollups
    ex("INSERT INTO campaign VALUES ('nw_renewal:c0003', 1, %s, 2, 1100, 2), "
       "('ks_cancel:c0004', 2, %s, 3, 1500, 1)", (m(2, 8), m(3, 8)))
    ex("""INSERT INTO lead_dim (lead_sk, lead_id, person_id, wf, campaign_id, cell, created_minute,
                                state, attempts) VALUES
          (1, 'L-100', 'P-100', 1, 'nw_renewal:c0003', 1, %s, 1, 2),
          (2, 'L-200', 'P-200', 2, 'ks_cancel:c0004', 2, %s, 0, 1),
          (3, 'L-300', 'P-300', 1, 'nw_renewal:c0003', 1, %s, 0, 1)""",
       (m(2, 8), m(3, 8), m(2, 8)))
    calls = [
        # event, wf, lead, minute, v(version), cohort_code, w, v, did, prompt, conn, eng, flag
        (1001, 1, 1, m(2, 12, 10), 2, 1, 0, 2, 0, 0, False, False, False),
        (2001, 1, 1, m(3, 10, 5), 3, 1, 0, 2, 2, 0, True, True, False),
        (2002, 2, 2, m(3, 12), 2, 0, 0, 0, 1, 2, True, False, True),
        (2003, 1, 3, m(3, 14), 3, 1, 0, 3, 0, 0, True, False, False),   # V4: no provenance
    ]
    for ev, wf, lead, minute, ver, coh, w, v, did, prompt, conn, eng, flag in calls:
        day = minute // D
        ex("""INSERT INTO call_fact VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 0, %s, %s, 1, %s, %s,
                                            %s, %s, %s, %s, false, %s, 0.93, 60, 8)""",
           (ev, wf, lead, minute, day, (minute % D) // 60, 2 * day + (1 if minute % D >= 900
                                                                    else 0),
            ver, coh, w, v, did, prompt, conn, eng, flag))
    ex("INSERT INTO r_pickup VALUES (1, 3, 10, 1, 1, 300, 90), (1, 3, 11, 1, 2, 200, 50), "
       "(2, 3, 12, 2, 1, 400, 100)")
    ex("INSERT INTO r_engage VALUES (1, 6, 1, 3, 0, 0, 2, 140, 77, 1), "
       "(2, 7, 2, 2, 2, 0, 0, 100, 47, 6)")
    ex("INSERT INTO ingested_chunk (chunk_no, kind, events, until_minute) VALUES "
       "(0, 'history', 200000, 0), (1, 'events', 3000, %s)", (NOW,))
    ex("INSERT INTO loop_step (step_no, from_minute, to_minute, phase_ms, chunks, events, "
       "orphan_calls, decisions) VALUES (210, %s, %s, %s, 1, 3000, 0, 4)",
       (NOW - 60, NOW, _j({"advance": 120, "ingest": 300})))
    for table, col in (("decision", "decision_id"), ("posterior_snapshot", "snapshot_id"),
                       ("audit_log", "audit_id"), ("winner", "winner_id"),
                       ("lead_dim", "lead_sk")):
        ex(f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
           f"(SELECT max({col}) FROM {table}))")
