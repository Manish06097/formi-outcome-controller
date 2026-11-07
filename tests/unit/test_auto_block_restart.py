"""AUDIT-2 F5: the post-403 auto-apply block lived only in memory, so a restart retried the
auto-apply the simulator had just refused (a second 403, a second AUTO_APPLY_REJECTED).

Reproduced before the fix: a fresh lifecycle over the same store had an empty `auto_blocked`."""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from controller.governance.lifecycle import ProposalLifecycle, blocks_after_403


class _Bare(ProposalLifecycle):
    def __init__(self):        # the block table needs no dependencies
        self.auto_blocked = {}


def test_a_403_newer_than_the_last_policy_change_blocks_after_a_restart():
    restarted = _Bare()
    assert restarted.auto_blocked == {}                       # what a restart used to do
    restarted.restore_auto_blocks({"wf_a": (900, "p-1", "dim not allowed")},
                                  {"wf_a": 500, "wf_b": 950})
    assert list(restarted.auto_blocked) == ["wf_a"]
    assert "p-1" in restarted.auto_blocked["wf_a"]


def test_a_later_policy_change_or_no_403_leaves_no_block():
    assert blocks_after_403({"wf_a": (900, "p-1", "x")}, {"wf_a": 900}) == {}
    assert blocks_after_403({"wf_a": (900, "p-1", "x")}, {"wf_a": 1000}) == {}
    assert blocks_after_403({}, {"wf_a": 1}) == {}


@pytest.mark.pg
def test_the_store_reads_the_latest_403_and_policy_change_per_workflow(pg_dsn):
    from controller.governance.store_pg import PgProposalStore
    from controller.store.db import Database
    from controller.store.repos import WorkflowCatalog
    from controller.types import FrozenDict
    db = Database(pg_dsn)
    db.ensure_ctl()
    s = db.create_run_schema(uuid.uuid4().hex, "http://127.0.0.1:8713", sim_days=4)
    try:
        with db.connect(schema=s) as conn:
            for wf, wid in ((1, "wf_a"), (2, "wf_b")):
                conn.execute("INSERT INTO workflow (wf, workflow_id, tenant_id, spec, spec_hash) "
                             "VALUES (%s, %s, 't', '{}', 'h')", (wf, wid))
            for pid, wf in (("p-1", 1), ("p-2", 1), ("p-3", 2)):
                conn.execute("INSERT INTO proposal (proposal_id, wf, class, route, state, "
                             "base_version) VALUES (%s, %s, 3, 'auto', 'failed_policy', 1)", (pid, wf))
            for pid, m in (("p-1", 600), ("p-2", 900), ("p-3", 700)):
                conn.execute("INSERT INTO proposal_transition (proposal_id, seq, from_state, "
                             "to_state, sim_minute, reason) VALUES (%s, 0, 'auto_applying', "
                             "'failed_policy', %s, %s)", (pid, m, f"r-{pid}"))
            conn.execute("INSERT INTO audit_log (sim_minute, wf, actor, action, subject_type, "
                         "subject_id, summary) VALUES (800, 2, 'controller', 'policy_changed', "
                         "'workflow', 'wf_b', 'x')")

            class Scope:
                @contextmanager
                def tx(self):
                    yield conn
            cat = WorkflowCatalog(specs=(), codes=FrozenDict({"wf_a": 1, "wf_b": 2}))
            forbidden, changed = PgProposalStore(Scope(), cat).auto_block_history()
    finally:
        db.drop_run_schema(s)
        db.close()
    assert forbidden == {"wf_a": (900, "p-2", "r-p-2"), "wf_b": (700, "p-3", "r-p-3")}
    assert changed == {"wf_b": 800}
    assert blocks_after_403(forbidden, changed).keys() == {"wf_a"}
