"""WorkflowRegistry and EffectivePolicy: the stricter of API policy and directives (6.4, 6.5, 9.7)."""
from __future__ import annotations

from controller.world.directives import load_directives
from controller.world.registry import WorkflowRegistry, ext
from tests.unit.gov_fixtures import policy, spec

CF = ["schedule", "caller_id", "welcome_message", "voice"]


def test_api_fields_and_derived_budget():
    pol = policy(approver_hours=[10, 18], approver_latency_minutes=[60, 480],
                 proposal_ttl_hours=12, apply_window_hours=1.5, max_requests_per_24h=2)
    assert pol.approver_open_min == 600 and ext(pol, "approver_close_min") == 1080
    assert pol.ttl_hours == 12 and ext(pol, "ttl_min") == 720
    assert pol.apply_window_min == 90
    assert (pol.c_policy, pol.c_eff, pol.reserve, pol.routine_budget) == (2, 2, 1, 1)


def test_directives_only_tighten():
    nw = policy(allowed_dims=CF, tenant_id="northwind_life")
    assert nw.allowed_dims == frozenset({"schedule", "caller_id"})
    assert nw.cohort_harm_line == 0.01 and nw.customer_facing_auto == "none"
    disabled = policy(enabled=False, allowed_dims=CF, tenant_id="kestrel_finance",
                      use_case="cancel")
    assert not disabled.auto_enabled and disabled.allowed_dims == frozenset()
    star = policy(allowed_dims=CF)
    assert star.allowed_dims == frozenset(CF) and star.cohort_harm_line == 0.02
    assert star.customer_facing_auto == "risk_reducing_only" and star.notice_minutes == 60


def test_registry_flags_policy_vs_memo_conflict_and_reports_constraint_changes():
    reg = WorkflowRegistry(load_directives())
    s = spec(workflow_id="nort_cancel_2", tenant_id="northwind_life", allowed_dims=CF)
    assert reg.load([s], tick=15) == []
    conflict = reg.auto_apply_conflict("nort_cancel_2")
    assert conflict.dims == frozenset({"welcome_message", "voice"})
    assert "memo 02" in conflict.directive_source
    changed = reg.load([spec(workflow_id="nort_cancel_2", tenant_id="northwind_life",
                             allowed_dims=CF, max_requests_per_24h=1)], tick=15)
    fields = {c.field: (c.old, c.new) for c in changed}
    assert fields["c_policy"] == (3, 1) and fields["c_eff"] == (3, 1)
    assert fields["reserve"] == (1, 0) and fields["routine_budget"] == (2, 1)
    assert reg.policy("nort_cancel_2").c_eff == 1
