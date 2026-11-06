"""Local policy replica, risk labels and the auto-apply rule (DESIGN.md 6.5): hand fixtures."""
from __future__ import annotations

import pytest

from controller.governance.policy import (auto_apply_permitted, change_kinds, classify_risk,
                                          is_risk_reducing, max_shift, needs_notice,
                                          sim_verdict, tv, tv_by_cohort_dim)
from controller.types import (P0_INCIDENT, P1_ROLLBACK, P3_ROUTINE, AutoApplyPolicy, CohortDef,
                              Composition, Config, FrozenDict, Hold, Proposal, VelocityEntry,
                              VelocityLedger)
from tests.unit.gov_fixtures import base_config, hm, policy

ALL_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]
OPS = ["schedule", "caller_id"]
CF = ["schedule", "caller_id", "welcome_message", "voice"]
DIDS = {"D1": "healthy", "D2": "suspect", "D3": "unknown"}
CONTROLS = {("default", "voice"): "V1", ("default", "welcome_message"): "W1"}


def comp(live: Config, new: Config, cls: int = P3_ROUTINE, wf: str = "wf_test",
         did_state=DIDS, controls=CONTROLS, history=()) -> Composition:
    stored = new.as_stored()
    return Composition(wf=wf, base_version=1, live=live, proposed=new, patches=(),
                       changed_dims=tuple(live.changed_dims(stored)),
                       tv_by_cohort_dim=tv_by_cohort_dim(live, stored),
                       risk=classify_risk(live, stored, did_state, controls, history),
                       cls=cls, p0_rank=0 if cls == P0_INCIDENT else None)


# ---------------------------------------------------------------- TV and max_shift
def test_tv_hand_values():
    assert tv({"W1": 1.0}, {"W1": 0.8, "W2": 0.2}) == pytest.approx(0.2)
    assert tv({"D1": 0.5, "D2": 0.5}, {"D1": 0.5, "D3": 0.5}) == pytest.approx(0.5)
    assert tv({"V1": 1.0}, {"V1": 1.0}) == 0.0


def test_max_shift_is_unweighted_over_every_cohort():
    live = base_config().with_cohorts([CohortDef.make("tiny", {"age_band": ["60+"]})]) \
        .with_allocation("tiny", "voice", {"V1": 1, "V3": 1})
    # removing the tiny cohort's override sends it back to V1: TV 0.5, whatever its size
    new = live.with_allocation("tiny", "voice", {})
    assert max_shift(live, new) == pytest.approx(0.5)
    # a cohort only in the new config counts too (old side falls back to default)
    new2 = live.with_cohorts(list(live.cohorts) + [CohortDef.make("c2", {"lang": ["ta"]})]) \
        .with_allocation("c2", "caller_id", {"D1": 0.2, "D2": 0.8})
    assert max_shift(live, new2) == pytest.approx(0.3)
    assert set(tv_by_cohort_dim(live, new2)) == {("c2", "caller_id")}


# ---------------------------------------------------------------- the simulator replica
def test_sim_verdict_boundary_and_margin():
    aa = AutoApplyPolicy(True, frozenset(OPS), 0.2)
    live = base_config()
    new = live.with_allocation("default", "caller_id", {"D1": 0.3, "D2": 0.7})   # TV 0.2
    exact = sim_verdict(aa, live, new.as_stored())
    assert exact.permitted                                  # simulator: <= 0.2 + 1e-9
    ours = sim_verdict(aa, live, new.as_stored(), margin=1e-6)
    assert not ours.permitted and ours.shift_exceeds        # we stay 1e-6 under: approval
    voice = live.with_allocation("default", "voice", {"V1": 0.9, "V2": 0.1})
    v = sim_verdict(aa, live, voice.as_stored())
    assert v.bad_dims == ("voice",) and not v.permitted
    assert v.problems(0.2) == ["dims not permitted for auto-apply: ['voice']"]
    same = sim_verdict(aa, live, live)
    assert same.no_change and same.problems(0.2) == ["no change"]


def test_schedule_only_change_passes_with_max_shift_zero():
    aa = AutoApplyPolicy(True, frozenset(["schedule"]), 0.0)
    live = base_config()
    new = live.with_hour_weights("default", {h: 1.0 for h in range(9, 21)})
    assert sim_verdict(aa, live, new.as_stored(), margin=1e-6).permitted   # 9.7 row


def test_disabled_policy_lists_every_changed_dim():
    aa = AutoApplyPolicy(False, frozenset(), 0.0)
    live = base_config()
    new = live.with_prompt("P3").with_allocation("default", "voice", {"V2": 1})
    v = sim_verdict(aa, live, new.as_stored())
    assert v.problems(0) == ["auto-apply is not enabled for this workflow",
                             "dims not permitted for auto-apply: ['voice', 'prompt_version']",
                             "allocation shift 1.000 exceeds max_shift 0"]


# ---------------------------------------------------------------- risk labels (ADR 0025)
def test_voice_reducing_only_toward_control():
    live = base_config(voice={"V1": 0.9, "V3": 0.1})
    back = live.with_allocation("default", "voice", {"V1": 0.96, "V3": 0.04})
    up = live.with_allocation("default", "voice", {"V1": 0.8, "V3": 0.2})
    enter = live.with_allocation("default", "voice", {"V1": 0.9, "V2": 0.05, "V3": 0.05})
    r = lambda new: classify_risk(live, new, DIDS, CONTROLS, ())[("default", "voice")]  # noqa
    assert r(back) == "reducing"
    assert r(up) == "increasing"
    assert r(enter) == "increasing"                          # an option enters
    assert classify_risk(live, back, DIDS, {}, ())[("default", "voice")] == "increasing"


def test_caller_id_labels():
    live = base_config()                                     # D1 0.5 (healthy), D2 0.5 (suspect)
    drain = live.with_allocation("default", "caller_id", {"D1": 0.7, "D2": 0.3})
    probe = live.with_allocation("default", "caller_id", {"D1": 0.5, "D2": 0.3, "D3": 0.2})
    wrong = live.with_allocation("default", "caller_id", {"D1": 0.3, "D2": 0.7})
    key = ("default", "caller_id")
    assert classify_risk(live, drain, DIDS, CONTROLS, ())[key] == "reducing"
    assert classify_risk(live, probe, DIDS, CONTROLS, ())[key] == "increasing"   # D3 unknown
    assert classify_risk(live, wrong, DIDS, CONTROLS, ())[key] == "increasing"


def test_schedule_revert_and_structural_dims():
    old_table = {h: 1.0 for h in range(9, 21)}
    prev = base_config().with_hour_weights("default", old_table)
    live = prev.with_hour_weights("default", {h: 2.0 if h < 12 else 1.0 for h in range(9, 21)})
    revert = live.with_hour_weights("default", old_table)
    fresh = live.with_hour_weights("default", {h: 3.0 for h in range(9, 21)})
    assert classify_risk(live, revert, DIDS, CONTROLS, [prev])[("default", "schedule")] == "reducing"
    assert classify_risk(live, fresh, DIDS, CONTROLS, [prev])[("default", "schedule")] == "increasing"
    c = classify_risk(live, live.with_prompt("P3"), DIDS, CONTROLS, ())
    assert c == {("default", "prompt_version"): "increasing"}
    c2 = classify_risk(live, live.with_cohorts([CohortDef.make("x", {"lang": ["hi"]})]),
                       DIDS, CONTROLS, ())
    assert c2 == {("*", "cohorts"): "increasing"}


def test_change_kinds_ramp_vs_new_arm():
    live = base_config(voice={"V1": 0.9, "V3": 0.1})
    ramp = live.with_allocation("default", "voice", {"V1": 0.8, "V3": 0.2})
    arm = live.with_allocation("default", "voice", {"V1": 0.8, "V2": 0.1, "V3": 0.1})
    risk_r = classify_risk(live, ramp, DIDS, CONTROLS, ())
    risk_a = classify_risk(live, arm, DIDS, CONTROLS, ())
    assert change_kinds(live, ramp, risk_r)[("default", "voice")] == "ramp"
    assert change_kinds(live, arm, risk_a)[("default", "voice")] == "new_arm"
    assert not is_risk_reducing(risk_r) and not is_risk_reducing({})


# ---------------------------------------------------------------- auto_apply_permitted
EMPTY = VelocityLedger("wf_test")


def test_star_default_customer_facing_reducing_after_notice():
    pol = policy(allowed_dims=CF)                            # tenant "acme": `*` default
    live = base_config(voice={"V1": 0.9, "V3": 0.1})
    back = comp(live, live.with_allocation("default", "voice", {"V1": 1.0}))
    assert needs_notice(pol, back)                           # customer-facing, 60 min
    ok, reasons = auto_apply_permitted(pol, back, EMPTY, [], hm(1, 12))
    assert not ok and reasons == ["notice required and not yet posted"]
    ok, reasons = auto_apply_permitted(pol, back, EMPTY, [], hm(1, 13), notice_until=hm(1, 13))
    assert ok, reasons
    ramp = comp(live, live.with_allocation("default", "voice", {"V1": 0.85, "V3": 0.15}))
    ok, reasons = auto_apply_permitted(pol, ramp, EMPTY, [], hm(1, 13), notice_until=hm(1, 13))
    assert not ok and any("risk_reducing_only" in r for r in reasons)


def test_northwind_none_routes_customer_facing_to_approval():
    pol = policy(allowed_dims=CF, tenant_id="northwind_life")
    assert pol.allowed_dims == frozenset(OPS)                # directive narrows the API policy
    live = base_config(voice={"V1": 0.9, "V3": 0.1})
    back = comp(live, live.with_allocation("default", "voice", {"V1": 1.0}))
    ok, reasons = auto_apply_permitted(pol, back, EMPTY, [], hm(1, 12), notice_until=hm(1, 12))
    assert not ok
    assert "policy: dims not permitted for auto-apply: ['voice']" in reasons
    assert any("no customer-facing auto-apply" in r for r in reasons)


def test_kestrel_cancel_ramps_allowed_new_arms_not():
    pol = policy(allowed_dims=CF, tenant_id="kestrel_finance", use_case="loan cancellation")
    live = base_config(voice={"V1": 0.9, "V3": 0.1})
    ramp = comp(live, live.with_allocation("default", "voice", {"V1": 0.85, "V3": 0.15}))
    assert needs_notice(pol, ramp)                           # non_incident scope
    ok, reasons = auto_apply_permitted(pol, ramp, EMPTY, [], hm(1, 13), notice_until=hm(1, 13))
    assert ok, reasons
    arm = comp(live, live.with_allocation("default", "voice", {"V1": 0.85, "V2": 0.05,
                                                                "V3": 0.1}))
    ok, reasons = auto_apply_permitted(pol, arm, EMPTY, [], hm(1, 13), notice_until=hm(1, 13))
    assert not ok and any("ramps_and_risk_reducing" in r for r in reasons)
    # an incident fix is never delayed by a notice
    drain = comp(live, live.with_allocation("default", "caller_id", {"D1": 0.6, "D2": 0.4}),
                 cls=P0_INCIDENT)
    assert not needs_notice(pol, drain)


def test_velocity_budget_and_chaining():
    pol = policy(allowed_dims=OPS)                           # max_shift 0.2
    live = base_config(caller_id={"D1": 0.9, "D2": 0.1})
    probe = comp(live, live.with_allocation("default", "caller_id",
                                            {"D1": 0.75, "D2": 0.1, "D3": 0.15}))   # TV 0.15
    now = hm(2, 12)
    recent = VelocityLedger("wf_test", (VelocityEntry(now - 600, "default", "caller_id", 0.1,
                                                      True, "p-old"),))
    ok, reasons = auto_apply_permitted(pol, probe, recent, [], now)
    assert not ok and reasons[0].startswith("velocity: default/caller_id would move 0.2500")
    old = VelocityLedger("wf_test", (VelocityEntry(now - 1440, "default", "caller_id", 0.1,
                                                   True, "p-old"),))
    assert auto_apply_permitted(pol, probe, old, [], now)[0]   # exactly 24h ago: rolled off
    drain_live = base_config(caller_id={"D1": 0.5, "D2": 0.5})
    step = drain_live.with_allocation("default", "caller_id", {"D1": 0.6, "D2": 0.4})
    chained = VelocityLedger("wf_test", (VelocityEntry(now - 15, "default", "caller_id", 0.1,
                                                       False, "p-step1"),))
    ok, reasons = auto_apply_permitted(pol, comp(drain_live, step, cls=P3_ROUTINE), chained,
                                       [], now)
    assert not ok and reasons[0].startswith("chaining")
    assert auto_apply_permitted(pol, comp(drain_live, step, cls=P1_ROLLBACK), chained, [],
                                now)[0]


def test_in_flight_hold_and_approval_only_dims():
    pol = policy(allowed_dims=ALL_DIMS)
    live = base_config()
    sched = comp(live, live.with_hour_weights("default", {h: 1.0 for h in range(9, 21)}))
    holder = Proposal(proposal_id="r-wf-D1T1100-3-0001", wf="wf_test", cls=3,
                      route="approval", state="pending", base_version=1)
    ok, reasons = auto_apply_permitted(pol, sched, EMPTY, [], hm(1, 12), in_flight=holder)
    assert not ok and reasons[0].startswith("one_in_flight")
    hold = Hold(1, "wf_test", "supervisor", "pause", hm(1, 9), None)
    ok, reasons = auto_apply_permitted(pol, sched, EMPTY, [hold], hm(1, 12))
    assert not ok and reasons[0].startswith("hold 1")
    p0 = comp(live, live.with_allocation("default", "caller_id", {"D1": 0.6, "D2": 0.4}),
              cls=P0_INCIDENT)
    assert auto_apply_permitted(pol, p0, EMPTY, [hold], hm(1, 12))[0]   # holds pause non-incident
    prompt = comp(live, live.with_prompt("P3"), cls=P0_INCIDENT)
    ok, reasons = auto_apply_permitted(pol, prompt, EMPTY, [], hm(1, 12))
    assert not ok and "design: ['prompt_version'] are approval-only" in reasons


def test_stale_composition_is_refused():
    pol = policy(allowed_dims=OPS)
    live = base_config()
    c = comp(live, live.with_allocation("default", "caller_id", {"D1": 0.6, "D2": 0.4}))
    bad = Composition(**{**c.__dict__, "changed_dims": ("voice",)})
    ok, reasons = auto_apply_permitted(pol, bad, EMPTY, [], hm(1, 12))
    assert not ok and reasons[0].startswith("composition stale")
    assert FrozenDict(c.risk) == {("default", "caller_id"): "reducing"}
