"""Release train rules (DESIGN.md 6.2, 6.5): precedence table, chains, notice, batching."""
from __future__ import annotations

import dataclasses

import pytest

from controller.governance.policy import classify_risk, tv_by_cohort_dim
from controller.governance.release import (chain_lambda, chain_steps_left, composition_classes,
                                           drain_chain_steps, interpolate, notice_until, p0_order,
                                           precedence, worst_remaining_minutes)
from controller.settings import Thresholds
from controller.types import (P0_INCIDENT, P0A_PROMPT_SAFETY, P0B_DID_DRAIN, P1_ROLLBACK,
                              P2_PROMOTION, P3_ROUTINE, Composition, FrozenDict, Proposal)
from tests.unit.gov_fixtures import base_config, hm, policy

T = Thresholds()
POL = policy(allowed_dims=["caller_id", "schedule"])           # 10-19, latency 30-240, tick 15


def holder(cls, state="pending", rank=None, submitted=hm(1, 11)) -> Proposal:
    return Proposal(proposal_id=f"h-{cls}", wf="wf_test", cls=cls, route="approval", state=state,
                    base_version=3, submitted_minute=submitted, p0_rank=rank)


def act(h, cls, rank=None, now=hm(1, 12), **kw):
    return precedence(h, cls, rank, now, POL, T, **kw).action


# ---------------------------------------------------------------- the 6.2 table, row by row
def test_free_workflow_submits():
    assert act(None, P3_ROUTINE) == "submit"


def test_p1_supersedes_routine_holder_and_routine_waits():
    assert act(holder(P3_ROUTINE), P1_ROLLBACK) == "supersede"
    assert act(holder(P2_PROMOTION, "approved"), P1_ROLLBACK) == "supersede"
    assert act(holder(P3_ROUTINE), P2_PROMOTION) == "wait"
    assert act(holder(P1_ROLLBACK), P1_ROLLBACK) == "wait"
    assert act(holder(P3_ROUTINE, "submitting"), P1_ROLLBACK) == "wait"   # mid-call


def test_suspect_did_defers_routine_up_to_2h():
    assert act(None, P3_ROUTINE, suspect_since=hm(1, 11)) == "defer_suspect"
    assert act(None, P3_ROUTINE, now=hm(1, 13), suspect_since=hm(1, 11)) == "submit"
    assert act(None, P1_ROLLBACK, suspect_since=hm(1, 11)) == "submit"


def test_p0b_drain_waits_for_a_holder_finishing_within_60_min():
    # pending P3 submitted 11:00, worst decide 15:00 -> visible 15:15: 195 min left at 12:00
    h = holder(P3_ROUTINE, submitted=hm(1, 11))
    assert worst_remaining_minutes(h, hm(1, 12), POL) == 195
    assert act(h, P0_INCIDENT, P0B_DID_DRAIN, caller_id_auto=True) == "supersede"
    # at 14:15 only 60 min are left: wait and drain right after its apply
    assert act(h, P0_INCIDENT, P0B_DID_DRAIN, now=hm(1, 14, 15), caller_id_auto=True) == "wait"
    # an approved holder is applied at the next stop: wait
    assert act(holder(P3_ROUTINE, "approved"), P0_INCIDENT, P0B_DID_DRAIN,
               caller_id_auto=True) == "wait"
    # approval-only workflow: the drain approval supersedes (strictly higher class)
    assert act(holder(P3_ROUTINE, "approved"), P0_INCIDENT, P0B_DID_DRAIN) == "supersede"


def test_pending_p0a_is_never_staled_by_a_drain():
    p0a = holder(P0_INCIDENT, rank=P0A_PROMPT_SAFETY)
    assert act(p0a, P0_INCIDENT, P0B_DID_DRAIN, caller_id_auto=True) == "wait"
    assert act(p0a, P0_INCIDENT, P0A_PROMPT_SAFETY) == "wait"


def test_p0a_bundles_over_a_pending_p0b_approval():
    p0b = holder(P0_INCIDENT, rank=P0B_DID_DRAIN)
    assert act(p0b, P0_INCIDENT, P0A_PROMPT_SAFETY) == "bundle_supersede"
    assert act(holder(P0_INCIDENT, "approved", P0B_DID_DRAIN), P0_INCIDENT,
               P0A_PROMPT_SAFETY) == "wait"
    assert act(holder(P1_ROLLBACK), P0_INCIDENT, P0A_PROMPT_SAFETY) == "supersede"


def test_p0a_vs_running_drain_chain():
    # (k_rem - 1) x tick <= 30: k_rem 3 at tick 15 finishes first (MTTR + 30 min)
    assert act(None, P0_INCIDENT, P0A_PROMPT_SAFETY, chain_k_rem=3) == "finish_chain_first"
    assert act(None, P0_INCIDENT, P0A_PROMPT_SAFETY, chain_k_rem=4) == "bundle_stop_chain"
    # both detected in one step: k = ceil(share / max_shift); 6.2 worked cases
    assert drain_chain_steps(0.4, 0.2) == 2
    assert drain_chain_steps(0.35, 0.05) == 7
    assert act(None, P0_INCIDENT, P0A_PROMPT_SAFETY,
               chain_k_rem=drain_chain_steps(0.4, 0.2)) == "finish_chain_first"
    assert act(None, P0_INCIDENT, P0A_PROMPT_SAFETY,
               chain_k_rem=drain_chain_steps(0.35, 0.05)) == "bundle_stop_chain"


def test_p0_order_prompt_safety_before_drain():
    assert p0_order({5: P0B_DID_DRAIN, 6: P0A_PROMPT_SAFETY, 7: None}) == [6, 5, 7]


# ---------------------------------------------------------------- batching and notice
def test_composition_classes():
    assert composition_classes(P0_INCIDENT) == {0}
    assert composition_classes(P1_ROLLBACK) == {0, 1}
    assert composition_classes(P3_ROUTINE) == {0, 1, 2, 3}


def test_notice_until_inside_approver_hours():
    kestrel = policy(tenant_id="kestrel_finance", use_case="loan cancellation")
    assert notice_until(hm(1, 12), kestrel) == hm(1, 13)
    assert notice_until(hm(1, 20), kestrel) == hm(2, 11)       # posted at next open, 10:00
    star = policy()
    assert notice_until(hm(1, 20), star) == hm(1, 21)          # `*`: no in-hours requirement


# ---------------------------------------------------------------- chain steps
def _comp(live, new, cls=P0_INCIDENT):
    stored = new.as_stored()
    return Composition(wf="wf_test", base_version=1, live=live, proposed=new, patches=(),
                       changed_dims=tuple(live.changed_dims(stored)),
                       tv_by_cohort_dim=tv_by_cohort_dim(live, stored),
                       risk=classify_risk(live, stored, {"D1": "healthy", "D2": "unhealthy"},
                                          {}, ()), cls=cls, p0_rank=1)


def test_chain_step_moves_max_shift_on_the_straight_line():
    pol = policy(allowed_dims=["caller_id"], max_shift=0.05)
    live = base_config(caller_id={"D1": 0.65, "D2": 0.35})
    target = live.with_allocation("default", "caller_id", {"D1": 1.0})
    c = _comp(live, target)
    assert chain_steps_left(c, pol, T) == 8            # 0.35 / (0.05 - 1e-6) -> 8 steps
    lam = chain_lambda(c, pol, T)
    step = interpolate(live, target.as_stored(), lam)
    d = step.dist("default", "caller_id")
    assert d["D2"] == pytest.approx(0.35 - (0.05 - 1e-6))
    assert tv_by_cohort_dim(live, step.as_stored())[("default", "caller_id")] <= 0.05 - 1e-6 + 1e-12
    small = live.with_allocation("default", "caller_id", {"D1": 0.68, "D2": 0.32})
    assert chain_lambda(_comp(live, small), pol, T) == 1.0


def test_every_chain_step_passes_our_own_check_on_the_stored_form():
    """Regression (ADR 0030): at lam = cap / worst the normalised step landed on the cap with
    float noise and failed `max_shift - margin` about one time in six, which sent a drain step
    of custom (d) to an approval. A step must pass the check exactly, for any start."""
    from controller.governance.policy import sim_verdict
    from controller.types import AutoApplyPolicy
    pol = policy(allowed_dims=["caller_id"], max_shift=0.05)
    aa = AutoApplyPolicy(enabled=True, allowed_dims=("caller_id",), max_shift=0.05)
    for i in range(200):
        d2 = 0.06 + i * 0.0013337
        live = base_config(caller_id={"D1": 1 - d2, "D2": d2})
        target = live.with_allocation("default", "caller_id", {"D1": 1.0})
        c = _comp(live, target)
        step = interpolate(live, target.as_stored(), chain_lambda(c, pol, T)).as_stored()
        v = sim_verdict(aa, live, step, margin=T.max_shift_margin)
        assert not v.shift_exceeds, (d2, v.shift)


def test_no_submit_reason_tells_fatigue_from_a_missing_window():
    """At cap 1 a rejected routine request leaves no slot for 48h: that is the budget working,
    not NO_SAFE_WINDOW (found in the gov_c_cap1 variant, step I2)."""
    from controller.governance.release import no_submit_reason
    from controller.governance.timing import choose_submit
    pol = policy(max_requests_per_24h=1, enabled=False, allowed_dims=[], max_shift=0)
    used = [hm(3, 11)]
    assert choose_submit(hm(3, 15), pol, used, 3, True, t=T) is None
    assert no_submit_reason(hm(3, 15), pol, 3, T) == "fatigue_wait"
    # approvers 14-16 with TTL 4h and pre-open submits forbidden: no expiry-safe slot exists
    closed = policy(approver_hours=[14, 16], proposal_ttl_hours=4)
    closed = dataclasses.replace(closed, pre_open_submits="never")
    assert choose_submit(hm(3, 9), closed, [], 3, True, t=T) is None
    assert no_submit_reason(hm(3, 9), closed, 3, T) == "no_safe_window"
