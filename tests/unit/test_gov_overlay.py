"""Overlay, path ownership, cooldown and the supervisor-edit rebase (DESIGN.md 6.7)."""
from __future__ import annotations

import pytest

from controller.governance.lifecycle import (InMemoryProposalStore, LifecycleDeps,
                                             ProposalLifecycle)
from controller.planner.overlay import (CompositionInvalid, Overlay, OverlayContext, diff_paths)
from controller.types import (IN_FLIGHT_STATES, P0_INCIDENT, P1_ROLLBACK, P3_ROUTINE, CohortDef,
                              Config, FrozenDict, PatchRequest, Proposal, SimEpoch)
from tests.unit.gov_fixtures import base_config, hm, policy, spec

SPEC = spec(tenant_id="northwind_life")


class Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


def req(path, value, owner="allocator", cls=P3_ROUTINE, decision_id=1, **ev) -> PatchRequest:
    return PatchRequest(wf="wf_test", owner=owner, path=path, value=value, cls=cls,
                        p0_rank=0 if cls == P0_INCIDENT else None, evidence=FrozenDict(ev),
                        decision_id=decision_id)


def make_overlay(clock, **kw) -> Overlay:
    ctx = OverlayContext(spec=SPEC, did_state={"D1": "healthy", "D2": "healthy",
                                               "D3": "unknown"},
                         controls={("default", "voice"): "V1"})
    return Overlay(lambda wf: ctx, clock, **kw)


# ---------------------------------------------------------------- compose
def test_compose_applies_paths_in_fixed_order_and_labels_the_result():
    ov = make_overlay(Clock(hm(1, 11)))
    live = base_config()
    ov.add(req(("alloc", "senior", "voice"), {"V1": 0.9, "V3": 0.1}))
    ov.add(req(("cohorts",), [{"id": "senior", "where": {"age_band": ["60+"]}}], owner="grid"))
    ov.add(req(("hour_weights", "default"), {h: 1 for h in range(9, 21)}, owner="schedule"))
    c = ov.compose("wf_test", live, {3}, base_version=4)
    assert c.changed_dims == ("cohorts", "voice", "schedule")
    assert c.base_version == 4 and c.cls == 3 and c.p0_rank is None
    assert c.proposed.dist("senior", "voice") == {"V1": 0.9, "V3": pytest.approx(0.1)}
    assert c.tv_by_cohort_dim == {("senior", "voice"): pytest.approx(0.1)}
    assert c.risk[("senior", "voice")] == "increasing"         # a new arm
    assert c.risk[("*", "cohorts")] == "increasing"
    assert c.proposed.problems(SPEC) == []


def test_alloc_on_undefined_cohort_is_skipped_not_invalid():
    ov = make_overlay(Clock(hm(1, 11)))
    ov.add(req(("alloc", "ghost", "voice"), {"V1": 1}))
    c = ov.compose("wf_test", base_config(), {3})
    assert c.changed_dims == () and c.patches == ()
    assert ov.last_compose["wf_test"].skipped[0][1] == "cohort ghost is not defined"


def test_removing_a_cohort_drops_its_overrides():
    live = base_config().with_cohorts([CohortDef.make("old", {"lang": ["hi"]})]) \
        .with_allocation("old", "voice", {"V2": 1}).with_hour_weights("old", {10: 1.0})
    ov = make_overlay(Clock(hm(1, 11)))
    ov.add(req(("cohorts",), [], owner="grid"))
    c = ov.compose("wf_test", live, {3})
    assert "old" not in c.proposed.allocations and "old" not in c.proposed.hour_weights
    assert c.proposed.problems(SPEC) == []


def test_invalid_composition_raises():
    ov = make_overlay(Clock(hm(1, 11)))
    ov.add(req(("hour_weights", "default"), {3: 1.0}, owner="schedule"))   # outside 9-21
    with pytest.raises(CompositionInvalid) as e:
        ov.compose("wf_test", base_config(), {3})
    assert "outside" in e.value.problems[0]


def test_classes_filter_and_p0_rank():
    ov = make_overlay(Clock(hm(1, 11)))
    ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.8, "D2": 0.2}, owner="did",
               cls=P0_INCIDENT))
    ov.add(req(("alloc", "default", "voice"), {"V1": 0.9, "V2": 0.1}))
    c = ov.compose("wf_test", base_config(), {0})
    assert c.changed_dims == ("caller_id",) and c.cls == 0 and c.p0_rank == 0


# ---------------------------------------------------------------- ownership
def test_same_owner_replaces_other_owner_refused_unless_more_urgent():
    ov = make_overlay(Clock(hm(1, 11)))
    a = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.6, "D2": 0.4}, owner="did"))
    b = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.7, "D2": 0.3}, owner="did"))
    assert ov.patches[a.patch_id].state == "replaced" and b.state == "open"
    c = ov.add(req(("alloc", "default", "caller_id"), {"D1": 1}, owner="schedule"))
    assert c.state == "refused"                                 # same class, other module
    d = ov.add(req(("alloc", "default", "caller_id"), {"D1": 1}, owner="incidents",
                   cls=P1_ROLLBACK))
    assert d.state == "open" and ov.patches[b.patch_id].state == "replaced"


def test_requeued_patch_yields_to_a_newer_one_on_its_path():
    ov = make_overlay(Clock(hm(1, 11)))
    a = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.6, "D2": 0.4}, owner="did"))
    ov.mark_proposed([a.patch_id], "prop-1")
    b = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.7, "D2": 0.3}, owner="did"))
    assert b.state == "open"                                   # a is in flight: both live
    assert ov.return_to_open("prop-1") == []                   # stale: a yields to b
    assert ov.patches[a.patch_id].state == "replaced"
    assert [p.patch_id for p in ov.open_patches("wf_test")] == [b.patch_id]


def test_business_cohorts_are_supervisor_owned():
    live = base_config().with_cohorts([CohortDef.make("vip", {"value_band": ["high"]})])
    ov = make_overlay(Clock(hm(1, 11)))
    ov.mark_supervisor_cohorts("wf_test", ["vip"])
    ov.add(req(("cohorts",), [{"id": "x", "where": {"lang": ["hi"]}}], owner="grid"))
    assert ov.compose("wf_test", live, {3}).changed_dims == ()
    ov.add(req(("cohorts",), [{"id": "vip", "where": {"value_band": ["high"]}},
                              {"id": "x", "where": {"lang": ["hi"]}}], owner="grid"))
    assert ov.compose("wf_test", live, {3}).changed_dims == ("cohorts",)


# ---------------------------------------------------------------- held patches: auto-release
def test_auto_release_of_held_patches():
    clock = Clock(hm(1, 11))
    ov = make_overlay(clock)
    p = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.6, "D2": 0.4}, owner="did"))
    ov.hold([p.patch_id], hm(1, 17))
    assert ov.release_held(hm(1, 16), lambda x: True) == {"released": [], "held": [],
                                                          "dropped": []}
    out = ov.release_held(hm(1, 17), lambda x: False)            # checks still fail: hold 2
    assert out["held"] == [p.patch_id] and ov.patches[p.patch_id].hold_count == 2
    out = ov.release_held(hm(1, 23), lambda x: False)            # after 2 holds: dropped
    assert out["dropped"] == [p.patch_id]
    q = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.7, "D2": 0.3}, owner="did"))
    ov.hold([q.patch_id], hm(2, 5))
    assert ov.release_held(hm(2, 5), lambda x: True)["released"] == [q.patch_id]
    assert ov.patches[q.patch_id].state == "open"


def test_rows_round_trip():
    clock = Clock(hm(1, 11))
    ov = make_overlay(clock)
    ov.add(req(("alloc", "default", "voice"), {"V1": 0.9, "V2": 0.1}, exp_id="e1"))
    ov.cooldowns[("wf_test", ("prompt",))] = __import__(
        "controller.planner.overlay", fromlist=["Cooldown"]).Cooldown(hm(2, 0), 5, "x")
    ov2 = Overlay.restore(ov.rows(), ov.context, clock)
    assert ov2.patches == ov.patches and ov2.cooldowns == ov.cooldowns
    assert ov2.owners == ov.owners
    assert ov2.add(req(("prompt",), "P3")).patch_id == 2


def test_diff_paths_tolerates_renormalisation_drift():
    third = {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}
    a = Config.from_stored({"cohorts": [], "allocations": {"default": {
        "welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}, "caller_id": third}},
        "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}})
    drift = {k: v * (1 + 2e-16) for k, v in third.items()}
    b = Config.from_stored({**a.stored(), "allocations": {"default": {
        "welcome_message": {"W1": 1.0}, "voice": {"V2": 1.0}, "caller_id": drift}}})
    assert diff_paths(a, b) == (("alloc", "default", "voice"),)


# ---------------------------------------------------------------- the D5 supervisor voice edit
def test_d5_supervisor_voice_edit_rebase_never_reverts():
    """public_02-like: the Northwind supervisor sets the default voice to V2 at D5 15:00 while
    our voice test and a caller-ID spread are pending in one approval."""
    clock = Clock(hm(5, 11))
    store = InMemoryProposalStore()
    worse = {("young", "voice", "V2")}
    ov = make_overlay(clock, in_flight=lambda wf: store.in_states(IN_FLIGHT_STATES, wf),
                      credibly_worse=lambda wf, c, d, o: (c, d, o) in worse)
    cohorts = [CohortDef.make("senior", {"age_band": ["60+"]}),
               CohortDef.make("young", {"age_band": ["18-30"]})]
    live_v7 = base_config(caller_id={"D1": 0.5, "D2": 0.5}).with_cohorts(cohorts)
    p_default = ov.add(req(("alloc", "default", "voice"), {"V1": 0.96, "V3": 0.04},
                           exp_id="exp-voice-1"))
    p_senior = ov.add(req(("alloc", "senior", "voice"), {"V1": 0.8, "V3": 0.2},
                          exp_id="exp-voice-1"))
    p_young = ov.add(req(("alloc", "young", "voice"), {"V1": 0.9, "V3": 0.1},
                         exp_id="exp-voice-1"))
    p_did = ov.add(req(("alloc", "default", "caller_id"), {"D1": 0.45, "D2": 0.45, "D3": 0.1},
                       owner="did"))
    comp = ov.compose("wf_test", live_v7, {3}, base_version=7)
    lc = ProposalLifecycle(LifecycleDeps(
        store=store, overlay=ov, sim=None, executor=None, policy_of=lambda wf: policy(),
        clock=clock, epoch=SimEpoch.from_state(type("S", (), {"ts": "2027-01-04T00:00:00+05:30",
                                                              "sim_minute": 0})()),
        run6="run001"))
    pending = store.insert(Proposal(proposal_id="run001-wf_test-D5T1100-3-0001", wf="wf_test",
                                    cls=3, route="approval", state="pending", base_version=7,
                                    proposed_config=comp.proposed.as_stored(),
                                    approval_id="apr_00009", submitted_minute=hm(5, 11)),
                           hm(5, 11), "test")
    ov.mark_proposed([p.patch_id for p in comp.patches], pending.proposal_id)

    # D5 15:00: the supervisor edit lands as version 8
    clock.now = hm(5, 15)
    live_v8 = live_v7.with_allocation("default", "voice", {"V2": 1})
    rep = ov.rebase_on_supervisor_edit("wf_test", live_v7, live_v8, 8)
    assert rep.sup_paths == (("alloc", "default", "voice"),)
    assert rep.invalidated_patches == (p_default.patch_id,)
    assert rep.interrupted_experiments == ("exp-voice-1",)
    assert rep.stale_proposals == (pending.proposal_id,)
    assert rep.cooldown_until == hm(6, 15)
    subs = {s.cohort: s for s in rep.substitutions}
    assert subs["senior"].applied and (subs["senior"].old_control, subs["senior"].new_control) \
        == ("V1", "V2")
    assert not subs["young"].applied                            # V2 credibly worse there: listed
    assert ov.patches[p_senior.patch_id].value == {"V2": 0.8, "V3": 0.2}
    assert ov.patches[p_young.patch_id].value == {"V1": 0.9, "V3": 0.1}

    # step 3: the lifecycle marks the pending approval STALE (never applied)
    stale = lc.mark_stale("wf_test", 8, hm(5, 15))
    assert [p.state for p in stale] == ["stale"]
    assert {p.patch_id for p in ov.open_patches("wf_test")} == {
        p_senior.patch_id, p_young.patch_id, p_did.patch_id}

    # next routine slot (D6 11:00, inside the cooldown): rebased on v8, nothing reverted
    clock.now = hm(6, 11)
    c2 = ov.compose("wf_test", live_v8, {3}, base_version=8)
    assert c2.proposed.dist("default", "voice") == {"V2": 1.0}
    assert c2.proposed.dist("senior", "voice") == {"V2": 0.8, "V3": pytest.approx(0.2)}
    assert c2.proposed.dist("default", "caller_id")["D3"] == pytest.approx(0.1)
    assert ("alloc", "default", "voice") not in {p.path for p in c2.patches}

    # a new P3 voice patch on the supervisor's path waits for the cooldown, then says so
    p_new = ov.add(req(("alloc", "default", "voice"), {"V2": 0.96, "V3": 0.04},
                       exp_id="exp-voice-2"))
    c3 = ov.compose("wf_test", live_v8, {3}, base_version=8)
    assert p_new.patch_id not in {p.patch_id for p in c3.patches}
    assert (p_new.patch_id, "supervisor cooldown") in ov.last_compose["wf_test"].skipped
    clock.now = hm(6, 15)
    c4 = ov.compose("wf_test", live_v8, {3}, base_version=8)
    assert p_new.patch_id in {p.patch_id for p in c4.patches}
    assert ov.supervisor_notes("wf_test", [p.path for p in c4.patches]) == [
        "changes the voice your supervisor set on D5 15:00"]
    # an incident (P1 here) is never blocked by the cooldown
    clock.now = hm(5, 16)
    ov.add(req(("alloc", "default", "voice"), {"V2": 1, "V1": 0}, owner="incidents",
               cls=P1_ROLLBACK))
    assert ov.compose("wf_test", live_v8, {1}).patches


def test_add_refuses_a_request_without_its_decision():
    """ADR 0028: pure modules emit decision_id=None; the caller stamps it before Overlay.add."""
    import dataclasses
    ov = make_overlay(Clock(hm(1, 10)))
    bare = dataclasses.replace(req(("alloc", "default", "voice"), {"V1": 0.9, "V2": 0.1}),
                               decision_id=None)
    with pytest.raises(ValueError, match="decision_id"):
        ov.add(bare)
    assert ov.add(dataclasses.replace(bare, decision_id=7)).decision_id == 7
