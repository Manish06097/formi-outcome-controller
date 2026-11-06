"""Unit tests of the step rule (DESIGN.md 4.2) and the time helpers it uses (6.3).

Contexts are built by hand: three public_01-like workflows read from fixture specs, so every case
states the policy it assumes. Pure functions only; no simulator, no database.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from controller.loop.stepper import (basic_policy, boundaries, next_stop, overnight_allowed,
                                     plan_state, plan_stop)
from controller.types import P0_INCIDENT, P3_ROUTINE, PlannerContext, WorkflowSpec
from controller.world.clock import (ceil_to, decision_visible_at, floor_to, may_decide_between,
                                    next_daily, next_working, p_expire_exact, s_watch_max,
                                    submit_candidates)

TICK = 15


def spec(wid: str, window=(9, 21), approvers=(10, 19), latency=(30, 240), ttl=24, apply_h=2,
         batch=8) -> WorkflowSpec:
    return WorkflowSpec.from_api({
        "workflow_id": wid, "tenant_id": "t", "use_case": "x", "languages": ["hi"],
        "dialling_window": list(window), "dials_per_hour": 300, "cadence": [2, 3, 3],
        "min_gap_minutes": 120, "daily_batch": {"size": 100, "at_hour": batch},
        "mid_flight": "allowed", "objective": {"primary": "engagement_rate"},
        "options": {"welcome_message": ["W1"], "voice": ["V1"], "caller_id": ["D1"],
                    "prompt_version": ["P1"]},
        "prompt_catalog": {"P1": "x"},
        "governance": {"auto_apply": {"enabled": False}, "approver_hours": list(approvers),
                       "approver_latency_minutes": list(latency), "proposal_ttl_hours": ttl,
                       "apply_window_hours": apply_h, "max_requests_per_24h": 3,
                       "base_reject_rate": 0.1, "min_validation_pass_rate": 0.97},
        "current_version": 1})


def ctx(*states, end=8 * 1440, base=60) -> PlannerContext:
    return PlannerContext(tick=TICK, base_step=base, end_minute=end, workflows=tuple(states))


def st(s: WorkflowSpec, **facts):
    return plan_state(s, basic_policy(s, TICK), **facts)


D = 1440


def at(day: int, hh: int, mm: int = 0) -> int:
    return day * D + hh * 60 + mm


# ---------------------------------------------------------------- clock helpers

def test_tick_arithmetic_handles_history_minutes():
    assert floor_to(-1, 15) == -15 and ceil_to(-1, 15) == 0
    assert floor_to(30, 15) == 30 and ceil_to(31, 15) == 45
    assert next_daily(at(0, 8), 8 * 60) == at(1, 8)      # strictly after
    assert next_daily(at(0, 7, 59), 8 * 60) == at(0, 8)
    assert next_daily(-60, 0) == 0


def test_next_working_matches_contract():
    hours = (10, 19)
    assert next_working(at(0, 9, 59), hours) == at(0, 10)
    assert next_working(at(0, 10), hours) == at(0, 10)
    assert next_working(at(0, 18, 59), hours) == at(0, 18, 59)
    assert next_working(at(0, 19), hours) == at(1, 10)       # close is exclusive
    assert next_working(at(4, 18, 45) + 60, hours) == at(5, 10)   # no weekends


def test_s_watch_max_derivation():
    # W = 120, tick 15: 15 * floor(106 / 15) = 105 (DESIGN.md 4.2)
    assert s_watch_max(15, 120) == 105
    assert s_watch_max(30, 60) == 30
    assert s_watch_max(15, 60) == 45


def test_decision_visibility():
    assert decision_visible_at(at(0, 10, 1), 15) == at(0, 10, 30)
    assert decision_visible_at(at(0, 10), 15) == at(0, 10, 15)


@pytest.mark.parametrize("hours,latency,ttl,t,expected", [
    # the worked cases of DESIGN.md 6.3 (sanity checks of the formula)
    ((10, 18), (60, 480), 12, at(0, 10), Fraction(1, 421)),
    ((10, 18), (60, 480), 12, at(0, 10, 15), Fraction(16, 421)),
    ((10, 18), (60, 480), 12, at(0, 6), Fraction(0)),
    ((10, 19), (20, 120), 12, at(0, 16, 45), Fraction(0)),
    ((10, 19), (20, 120), 12, at(0, 17), Fraction(1, 101)),
    ((14, 16), (30, 240), 4, at(0, 14), Fraction(121, 211)),
    ((14, 16), (30, 240), 4, at(0, 11, 45), Fraction(0)),
    ((14, 16), (30, 240), 4, at(0, 12), Fraction(1, 211)),
    ((10, 19), (30, 240), 4, at(0, 15), Fraction(1, 211)),
    ((10, 19), (30, 240), 24, at(0, 20), Fraction(0)),
])
def test_p_expire_worked_cases(hours, latency, ttl, t, expected):
    assert p_expire_exact(t, latency, ttl, hours) == expected


def test_submit_candidates_only_safe_tick_minutes():
    c = submit_candidates(at(0, 0), at(1, 0), 15, (60, 480), 12, (10, 18), 0.01)
    assert at(0, 10) in c and at(0, 10, 15) not in c
    assert all(t % 15 == 0 for t in c)


def test_may_decide_between_spans_days():
    assert may_decide_between(at(0, 18, 50), at(0, 19, 10), (10, 19))
    assert not may_decide_between(at(0, 19), at(1, 10), (10, 19))
    assert may_decide_between(at(0, 19), at(1, 10, 1), (10, 19))


# ---------------------------------------------------------------- step rule

A = spec("a")


def test_base_step_in_hours():
    assert next_stop(at(1, 11), ctx(st(A))) == at(1, 12)


def test_boundaries_cut_the_step():
    # 18:30 -> 19:00 is not a boundary of A, but approver close + tick (19:15) is
    assert next_stop(at(1, 18, 30), ctx(st(A))) == at(1, 19, 15)
    names = {n.split(":")[0] for _, n in boundaries(at(1, 11), ctx(st(A)))}
    assert {"dialling_open", "dialling_close", "approver_open", "approver_close_plus_tick",
            "daily_batch", "scenario_end"} <= names


def test_overnight_jump_from_dialling_close_to_batch():
    p = plan_stop(at(1, 21), ctx(st(A)))
    assert p.to_minute == at(2, 8) and p.overnight_jump


def test_pending_in_approver_hours_obeys_apply_bound():
    c = ctx(st(A, has_pending=True, pending_class=P3_ROUTINE, pending_created=at(1, 9)),
            base=180)
    p = plan_stop(at(1, 11), c)
    assert p.step == s_watch_max(15, 120) - 15 == 90
    assert p.watch_bound == 90


def test_pending_outside_hours_does_not_bound_but_blocks_jump_until_close_plus_tick():
    c = ctx(st(A, has_pending=True, pending_class=P3_ROUTINE, pending_created=at(1, 18)))
    # 19:00: a decision at 18:59 is processed at 19:00 and visible at 19:15, so no jump yet
    p = plan_stop(at(1, 19), c)
    assert not p.overnight_jump and p.to_minute <= at(1, 19, 15)
    # from 21:00 no decision is possible before tomorrow's 10:00: the jump is allowed
    p = plan_stop(at(1, 21), c)
    assert p.overnight_jump and p.to_minute == at(2, 8)


def test_jump_never_crosses_approver_open_with_pending():
    c = ctx(st(A, has_pending=True, pending_class=P3_ROUTINE, pending_created=at(1, 20)))
    assert next_stop(at(2, 8), c) == at(2, 9)        # dialling open first
    assert next_stop(at(2, 9), c) == at(2, 10)       # then approver open


def test_approved_blocks_overnight_jump_and_bounds_by_apply_by():
    c = ctx(st(A, approved_apply_by=(at(1, 21, 30),)))
    assert not overnight_allowed(at(1, 21), at(2, 8), c)
    assert next_stop(at(1, 21), c) == at(1, 21, 15)   # apply_by - tick


def test_incident_pending_past_min_latency_steps_one_tick():
    c = ctx(st(A, has_pending=True, pending_class=P0_INCIDENT, pending_created=at(1, 10)))
    assert next_stop(at(1, 10, 15), c) == at(1, 11, 15)     # before lat_lo (30): base step
    assert next_stop(at(1, 10, 30), c) == at(1, 10, 45)


def test_open_incident_steps_one_tick_and_never_jumps():
    c = ctx(st(A, open_incident=True))
    assert next_stop(at(1, 21), c) == at(1, 21, 15)


def test_scenario_end_is_the_last_stop():
    assert next_stop(at(7, 21), ctx(st(A))) == 8 * D


def test_several_workflows_take_the_earliest_boundary():
    b = spec("b", window=(10, 19), approvers=(10, 18), batch=8)
    c = ctx(st(A), st(b))
    assert next_stop(at(1, 17, 30), c) == at(1, 18, 15)     # b's approver close + tick
    assert next_stop(at(1, 19), c) == at(1, 19, 15)         # a's approver close + tick


def test_planned_slot_and_notice_are_boundaries():
    c = ctx(st(A, planned_slots=(at(1, 11, 15),), notice_expiries=(at(1, 11, 45),)))
    assert next_stop(at(1, 11), c) == at(1, 11, 15)
    assert next_stop(at(1, 11, 15), c) == at(1, 11, 45)


def test_step_is_always_a_positive_multiple_of_tick():
    c = ctx(st(A), st(spec("c", window=(8, 20), approvers=(9, 17), batch=7)))
    now = 0
    while now < 3 * D:
        nxt = next_stop(now, c)
        assert nxt > now and (nxt - now) % TICK == 0
        now = nxt
