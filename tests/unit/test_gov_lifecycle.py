"""Proposal state machine and its pure rules (DESIGN.md 6.1, 6.2): hand fixtures."""
from __future__ import annotations

import pytest

from controller.governance.lifecycle import (TERMINAL_STATES, TRANSITIONS, ArmObservation,
                                             IllegalTransition, InMemoryProposalStore,
                                             OneInFlightViolation, ShareObservation,
                                             can_supersede, next_state, pre_apply_event,
                                             rejection_plan, verify_allocation, verify_kind)
from controller.settings import Thresholds
from controller.types import (IN_FLIGHT_STATES, PROPOSAL_STATES, REQUEUE_STATES, FrozenDict,
                              Proposal)
from tests.unit.gov_fixtures import hm

T = Thresholds()


def prop(state="pending", cls=3, **kw) -> Proposal:
    base = dict(proposal_id=f"r-wf-D1T1100-{cls}-0001", wf="wf", cls=cls, route="approval",
                state=state, base_version=4)
    base.update(kw)
    return Proposal(**base)


# ---------------------------------------------------------------- the table
def test_table_covers_exactly_the_contract_states():
    states = {s for s, _ in TRANSITIONS} | set(TRANSITIONS.values())
    assert states == PROPOSAL_STATES
    for s in TERMINAL_STATES:
        assert not [e for (f, e) in TRANSITIONS if f == s], f"{s} must be terminal"
    assert REQUEUE_STATES <= TERMINAL_STATES
    assert not IN_FLIGHT_STATES & TERMINAL_STATES


def test_key_paths_of_6_1():
    path = ["draft", "submitting", "pending", "approved", "applying", "applied", "verifying",
            "verified"]
    events = ["route_approval", "created", "approved", "apply_start", "applied", "verify_start",
              "verified"]
    s = "draft"
    for e, want in zip(events, path[1:]):
        s = next_state(s, e)
        assert s == want
    assert next_state("approved", "stale") == "stale"            # never an apply_conflict
    assert next_state("approved", "lapse_risk") == "lapsed_local"   # never a 410
    assert next_state("pending", "superseded") == "superseded"
    assert next_state("auto_applying", "forbidden") == "failed_policy"
    assert next_state("verifying", "verify_failed") == "rollback"
    with pytest.raises(IllegalTransition):
        next_state("superseded", "approved")                     # never applied if approved later
    with pytest.raises(IllegalTransition):
        next_state("stale", "apply_start")


# ---------------------------------------------------------------- pre-apply checks
def test_pre_apply_order_base_then_deadline_then_late_gate():
    p = prop("approved", submitted_minute=hm(1, 11), decided_minute=hm(1, 12),
             apply_by_minute=hm(1, 14))
    assert pre_apply_event(p, hm(1, 12, 15), 4, 15, T) == "apply_start"
    assert pre_apply_event(p, hm(1, 12, 15), 5, 15, T) == "stale"
    assert pre_apply_event(p, hm(1, 13, 45), 4, 15, T) == "apply_start"   # apply_by - tick
    assert pre_apply_event(p, hm(1, 14), 4, 15, T) == "lapse_risk"        # inside the last tick
    late = prop("approved", submitted_minute=hm(1, 18), decided_minute=hm(2, 14, 1),
                apply_by_minute=hm(2, 16, 1))
    assert pre_apply_event(late, hm(2, 14, 30), 4, 15, T, lambda q: False) == "late_gate_failed"
    assert pre_apply_event(late, hm(2, 14, 30), 4, 15, T, lambda q: True) == "apply_start"
    early = prop("approved", submitted_minute=hm(1, 18), decided_minute=hm(2, 13, 59),
                 apply_by_minute=hm(2, 15, 59))          # 19h59m after submit: no re-gate
    assert pre_apply_event(early, hm(2, 14), 4, 15, T, lambda q: False) == "apply_start"


# ---------------------------------------------------------------- supersede (ADR 0013)
def test_supersede_strictly_higher_class_only():
    assert can_supersede(prop("pending", 3), 1, None)
    assert can_supersede(prop("approved", 2), 1, None)
    assert can_supersede(prop("noticed", 3), 0, 1)
    assert not can_supersede(prop("pending", 1), 1, None)        # same class
    assert not can_supersede(prop("pending", 1), 2, None)
    assert not can_supersede(prop("submitting", 3), 0, 0)        # mid-call: never
    assert not can_supersede(prop("applying", 3), 0, 0)


def test_p0a_bundle_is_the_only_same_class_supersede():
    p0b = prop("pending", 0, p0_rank=1)
    p0a = prop("pending", 0, p0_rank=0)
    assert can_supersede(p0b, 0, 0, bundle_keeps_drain=True)
    assert not can_supersede(p0b, 0, 0, bundle_keeps_drain=False)  # would drop the drain
    assert not can_supersede(p0a, 0, 1)                            # a P0a is never superseded
    assert not can_supersede(p0a, 0, 0, bundle_keeps_drain=True)
    assert not can_supersede(prop("approved", 0, p0_rank=1), 0, 0,
                             bundle_keeps_drain=True)              # about to apply: let it


# ---------------------------------------------------------------- rejections
def test_rejection_plans():
    jc = ["approver declined (judgement call)"]
    assert rejection_plan(0, jc, 0).action == "resubmit_now"
    assert rejection_plan(1, jc, 3).action == "resubmit_now"
    assert rejection_plan(3, jc, 0).action == "requeue_next_slot"
    assert rejection_plan(2, jc, 1).action == "drop_needs_discussion"
    fat = ["approver fatigue: more than 3 requests in 24h for this workflow, batch your changes"]
    plan = rejection_plan(3, fat, 0)
    assert (plan.kind, plan.action, plan.page) == ("local_check", "hold", "GOV_INVARIANT")


# ---------------------------------------------------------------- verification
def test_verify_allocation_three_checks():
    shares = [ShareObservation("default", "voice", "V3", 0.10, 95, 1000)]   # SE 0.0095
    arms = [ArmObservation("default", "voice", "V3", 0.02, -0.05)]
    ok = verify_allocation(shares, arms, 0.04, True, T)
    assert ok.status == "verified" and ok.page is None
    off = verify_allocation([ShareObservation("default", "voice", "V3", 0.10, 140, 1000)],
                            arms, 0.04, True, T)                   # 4.2 SE away: page only
    assert off.status == "verified" and off.page == "COHORT_MISMATCH"
    bad_b = verify_allocation(shares, [ArmObservation("default", "voice", "V3", -0.08, -0.05)],
                              0.04, True, T)
    assert bad_b.status == "rollback"
    assert verify_allocation(shares, arms, 0.11, True, T).status == "rollback"
    assert verify_allocation(shares, arms, 0.04, False, T).status == "rollback"


def test_verify_kind():
    assert verify_kind(prop(diff=FrozenDict({"changed_dims": ("voice", "prompt_version")}))) \
        == "prompt"
    assert verify_kind(prop(diff=FrozenDict({"changed_dims": ("caller_id", "voice")}))) \
        == "allocation"
    assert verify_kind(prop(diff=FrozenDict({"changed_dims": ("caller_id",)}))) == "caller_id"
    assert verify_kind(prop(diff=FrozenDict({"changed_dims": ("schedule",)}))) == "schedule"


# ---------------------------------------------------------------- the store invariant
def test_store_enforces_one_in_flight_and_atomic_supersede():
    st = InMemoryProposalStore()
    a = st.insert(prop("pending", 3, proposal_id="a"), hm(1, 11), "t")
    with pytest.raises(OneInFlightViolation):
        st.insert(prop("pending", 1, proposal_id="b"), hm(1, 12), "t")
    st.insert(prop("draft", 1, proposal_id="b"), hm(1, 12), "drafts are not in flight")
    st.rows.pop("b")
    c = st.supersede(a.proposal_id, prop("draft", 1, proposal_id="c"), hm(1, 12), "P1")
    assert st.get("a").state == "superseded" and st.get("a").superseded_by == "c"
    st.apply(c.proposal_id, "route_approval", hm(1, 12), "go")
    assert st.holder("wf").proposal_id == "c"
    rows = [(t.from_state, t.to_state) for t in st.transitions_of("a")]
    assert rows == [(None, "pending"), ("pending", "superseded")]


# ---------------------------------------------------------------- fatigue source of truth
class _Sim:
    def __init__(self, approvals):
        self._a = approvals

    def approvals(self, wf=None, status=None):
        return list(self._a)


def test_used_counts_every_listed_record_even_in_one_minute():
    from controller.governance.lifecycle import LifecycleDeps, ProposalLifecycle
    from controller.types import ApprovalPublic, SimEpoch

    epoch = SimEpoch.from_state(type("S", (), {"ts": "2027-01-04T00:00:00+05:30",
                                               "sim_minute": 0})())
    listed = [ApprovalPublic(f"apr_{i}", f"p{i}", "wf", "pending", 3, ("voice",),
                             epoch.ts(hm(1, 11)), epoch.ts(hm(2, 11))) for i in (1, 2)]
    st = InMemoryProposalStore()
    st.insert(prop("pending", 1, proposal_id="p3", approval_id="apr_3",
                   submitted_minute=hm(1, 12)), hm(1, 12), "not listed yet")
    lc = ProposalLifecycle(LifecycleDeps(store=st, overlay=None, sim=_Sim(listed),
                                         executor=None, policy_of=None,
                                         clock=lambda: hm(1, 12), epoch=epoch, run6="r"))
    assert lc.used("wf") == [hm(1, 11), hm(1, 11), hm(1, 12)]
