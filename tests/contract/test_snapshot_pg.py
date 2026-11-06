"""`load_snapshot` reads the M0 schema (DESIGN.md 3.2) into the same snapshot the in-memory
builder makes, and a model fitted on either gives the same posterior."""
from __future__ import annotations

import json
import uuid

import numpy as np
import pytest

from controller.learning.engagement import BetaBinomialModel
from controller.snapshot import build_snapshot, engage_from_columns, load_snapshot
from controller.store.db import Database
from tests.support.t3_world import (base_config, cohorts_by_age, make_policy, make_spec,
                                    uniform_world)

pytestmark = pytest.mark.pg


@pytest.fixture()
def schema(pg_dsn):
    db = Database(pg_dsn)
    db.ensure_ctl()
    s = db.create_run_schema(uuid.uuid4().hex, "http://127.0.0.1:8713", sim_days=4)
    try:
        yield db, s
    finally:
        db.drop_run_schema(s)
        db.close()


def test_load_snapshot_matches_the_in_memory_builder(schema):
    db, s = schema
    spec = make_spec()
    cfg = base_config(spec, cohorts_by_age(spec)).with_allocation(
        "default", "welcome_message", {"W1": 0.8, "W2": 0.1, "W3": 0.1})
    world = uniform_world(seed=3, connected_per_day=1200)
    for seg in range(4):
        world.simulate_segment(cfg, seg)
    rows = world.rows
    with db.connect(schema=s) as conn:
        conn.execute("INSERT INTO workflow (wf, workflow_id, tenant_id, spec, spec_hash) "
                     "VALUES (1, %s, 'tenant_test', %s, 'h')",
                     (spec.workflow_id, json.dumps(dict(spec.raw.thaw()))))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO cell VALUES (%s, %s, %s, %s, %s)",
                            [(c.cell_id, c.lang, c.tier, c.age, c.value)
                             for c in world.cells.values()])
            cur.executemany(
                "INSERT INTO r_engage VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (wf, seg, cell, config_version, prompt, w, v) DO UPDATE SET "
                "connected = r_engage.connected + excluded.connected, "
                "engaged = r_engage.engaged + excluded.engaged",
                list(zip(rows["seg"], rows["cell"], rows["config_version"], rows["prompt"],
                         rows["w"], rows["v"], rows["connected"], rows["engaged"],
                         rows["flags"])))
            cur.executemany(
                "INSERT INTO lead_dim (lead_id, person_id, wf, campaign_id, cell, created_minute,"
                " state) VALUES (%s, %s, 1, 'c1', %s, 0, %s)",
                [(f"l{i}", f"p{i}", i % len(world.cells), 0 if i % 5 else 1)
                 for i in range(500)])
        conn.execute("INSERT INTO experiment VALUES ('e1', 1, 'welcome_message', '{1,2}', 0, 1, "
                     "'live', 1, 0, NULL)")
        conn.execute("INSERT INTO experiment_arm VALUES ('e1', 1, 'population', 'live', 0.1, 0, "
                     "NULL, 7)")
        conn.execute("INSERT INTO winner_look VALUES ('e1', 1, 'population', 1, 210, 2000, 0.9, "
                     "0.5, 0.8, false, 8)")
        conn.execute("INSERT INTO decay_monitor VALUES (1, 'population', 'welcome_message', 1, "
                     "1.5, 3, 0.04, false)")
        conn.execute("INSERT INTO config_version (wf, version, actor, applied_minute, "
                     "changed_dims, body_source) VALUES (1, 1, 'business_default', NULL, '{}', "
                     "'fetched'), (1, 2, 'controller_approved', 1500, '{welcome_message}', "
                     "'ours')")
        conn.commit()
        ws = load_snapshot(conn, 1, spec=spec, policy=make_policy(), live=cfg, live_version=2,
                           now=4 * 720, run_id=s)
    mem = build_snapshot(spec=spec, policy=make_policy(), live=cfg, live_version=2,
                         now=4 * 720, engage=engage_from_columns(rows), cells=world.cells,
                         active=ws.active)
    assert len(ws.engage) == len(set(zip(rows["seg"], rows["cell"], rows["config_version"],
                                         rows["prompt"], rows["w"], rows["v"])))
    assert int(ws.engage.connected.sum()) == int(np.sum(rows["connected"]))
    assert set(ws.cells) == set(world.cells)
    assert ws.active is not None and float(ws.active.leads.sum()) == 400.0
    exp = ws.experiments[0]
    assert exp.arms == ("W2", "W3") and exp.control == "W1"
    assert exp.arm_rows[0].arm == "W2" and exp.arm_rows[0].state == "live"
    assert ws.looks[0].arm == "W2" and ws.looks[0].n_at_look == 210
    assert ws.decay[0].arm == "W2" and ws.decay[0].cusum == pytest.approx(1.5)
    assert [c.version for c in ws.changes] == [2] and ws.changes[0].dims == ("welcome_message",)
    m = BetaBinomialModel()
    a, b = m.fit(ws, "stationary"), m.fit(mem, "stationary")
    assert np.allclose(a.mean, b.mean) and np.allclose(a.cov, b.cov)
