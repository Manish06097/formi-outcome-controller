"""Submit timing, fatigue and slots (DESIGN.md 6.3, 6.4): hand-computed fixtures.

Each expected value is worked out in the comment next to it from the simulator's rules:
decide = next_working(t + L), L uniform on [lo, hi]; expired iff decide > t + 60 x TTL;
fatigue counts requests with created > t - 1440.
"""
from __future__ import annotations

import pytest

from controller.governance.timing import (choose_submit, expected_apply_minute, fatigue_ok,
                                          is_slot_minute, p_expire, routine_slots, used_at)
from controller.types import P0_INCIDENT, P1_ROLLBACK, P2_PROMOTION, P3_ROUTINE
from controller.world.clock import (next_open, next_working, s_watch_max, visible_minute)
from tests.unit.gov_fixtures import hm, policy

RENEWAL_LIKE = dict(approver_hours=[10, 19], approver_latency_minutes=[30, 240],
                    proposal_ttl_hours=24)
CASE_A = dict(approver_hours=[10, 18], approver_latency_minutes=[60, 480], proposal_ttl_hours=12)
CASE_B = dict(approver_hours=[14, 16], approver_latency_minutes=[30, 240], proposal_ttl_hours=4)
CANCEL_LIKE = dict(approver_hours=[10, 19], approver_latency_minutes=[20, 120],
                   proposal_ttl_hours=12)
TTL4 = dict(approver_hours=[10, 19], approver_latency_minutes=[30, 240], proposal_ttl_hours=4)


# ---------------------------------------------------------------- clock
def test_next_working_and_open():
    h = (10, 19)
    assert next_working(hm(0, 9, 59), h) == hm(0, 10)
    assert next_working(hm(0, 10), h) == hm(0, 10)
    assert next_working(hm(0, 18, 59), h) == hm(0, 18, 59)
    assert next_working(hm(0, 19), h) == hm(1, 10)          # close is exclusive
    assert next_working(hm(4, 23), h) == hm(5, 10)          # no weekends (D5 is a Saturday)
    assert next_open(hm(0, 10), h) == hm(0, 10) and next_open(hm(0, 10, 1), h) == hm(1, 10)


def test_s_watch_max_and_visibility():
    assert s_watch_max(15, 120) == 105        # 15 x floor(106 / 15)
    assert s_watch_max(15, 60) == 45          # 9.7: apply window 1h
    assert s_watch_max(30, 120) == 90         # 9.7: tick 30
    assert visible_minute(hm(0, 10, 1), 15) == hm(0, 10, 30)   # processed 10:15, seen at 10:30
    assert visible_minute(hm(0, 10), 15) == hm(0, 10, 15)


# ---------------------------------------------------------------- p_expire, worked cases of 6.3
def test_p_expire_renewal_like_is_zero_in_hours():
    pol = policy(**RENEWAL_LIKE)
    assert all(p_expire(m, pol) == 0 for m in range(hm(0, 10), hm(0, 19), 15))


def test_p_expire_case_a_ttl12_latency_60_480():
    pol = policy(**CASE_A)
    # 10:00: only L = 480 reaches 18:00 and rolls to 10:00 next day, past 22:00
    assert p_expire(hm(0, 10), pol) == pytest.approx(1 / 421)
    # 10:15: L in [465, 480] gives t + L >= 18:00: 16 of 421
    assert p_expire(hm(0, 10, 15), pol) == pytest.approx(16 / 421)
    # pre-open 06:00 to 09:45: every decision lands the same day
    assert all(p_expire(m, pol) == 0 for m in range(hm(0, 6), hm(0, 10), 15))


def test_p_expire_cancel_like():
    pol = policy(**CANCEL_LIKE)
    assert all(p_expire(m, pol) == 0 for m in range(hm(0, 10), hm(0, 16, 46), 15))
    assert p_expire(hm(0, 17), pol) == pytest.approx(1 / 101)       # L = 120 hits 19:00
    assert p_expire(hm(0, 17, 15), pol) == pytest.approx(16 / 101)  # L >= 105: 15.8%
    assert p_expire(hm(0, 20), pol) == 1.0                          # UNDERSTANDING 4: always


def test_p_expire_case_b_approvers_14_16_ttl4():
    pol = policy(**CASE_B)
    assert p_expire(hm(0, 14), pol) == pytest.approx(121 / 211)     # L >= 120 reaches 16:00
    assert p_expire(hm(0, 11, 45), pol) == 0
    assert p_expire(hm(0, 12), pol) == pytest.approx(1 / 211)       # L = 240 lands at 16:00
    assert p_expire(hm(0, 12, 15), pol) == pytest.approx(16 / 211)  # 7.6%


def test_p_expire_ttl4_normal_hours():
    pol = policy(**TTL4)
    assert all(p_expire(m, pol) == 0 for m in range(hm(0, 10), hm(0, 14, 46), 15))
    assert p_expire(hm(0, 15), pol) == pytest.approx(1 / 211)
    assert p_expire(hm(0, 15, 15), pol) == pytest.approx(16 / 211)


# ---------------------------------------------------------------- routine slots
def test_preferred_slot_default_and_slot2():
    pol = policy(**RENEWAL_LIKE)            # cap 3: reserve 1, routine 2
    assert (pol.c_eff, pol.reserve, pol.routine_budget) == (3, 1, 2)
    assert pol.preferred_slot_min == 11 * 60     # 10:00 + min(60, 25% of 540)
    assert routine_slots(2, pol) == [hm(2, 11), hm(2, 15)]
    assert is_slot_minute(hm(2, 15), pol) and not is_slot_minute(hm(2, 11, 15), pol)


def test_slots_case_a_uses_in_hours_tier_before_pre_open():
    pol = policy(**CASE_A)
    # preferred 11:00 has p = 61/421; 10:00 (p = 1/421) is the latest safe in-hours tick before
    # it; slot 2 at 14:00 has p = 241/421, so no second slot
    assert routine_slots(0, pol) == [hm(0, 10)]


def test_slots_case_b_pre_open():
    pol = policy(**CASE_B)
    assert pol.preferred_slot_min == 14 * 60 + 30    # 14:00 + min(60, 25% of 120 = 30)
    assert routine_slots(3, pol) == [hm(3, 12)]      # latest pre-open tick with p <= 1%


def test_slots_ttl4_normal_hours():
    # cap 3 (routine 2): slot 2 = 11:00 + 240 = 15:00 has p = 1/211 <= 1%, so it is a slot
    assert routine_slots(0, policy(**TTL4)) == [hm(0, 11), hm(0, 15)]
    # cap 2 (routine 1): one slot
    assert routine_slots(0, policy(**TTL4, max_requests_per_24h=2)) == [hm(0, 11)]


def test_kestrel_cancel_directive_slot_and_caps():
    pol = policy(tenant_id="kestrel_finance", use_case="consumer-loan cancellation",
                 max_requests_per_24h=4, **CANCEL_LIKE)
    assert (pol.c_policy, pol.c_eff, pol.reserve, pol.routine_budget) == (4, 2, 1, 1)
    assert pol.not_before_min == 660 and pol.preferred_slot_min == 660
    assert routine_slots(1, pol) == [hm(1, 11)]
    # Kestrel's other lines get the `*` default (scope by use case, A-11)
    other = policy(tenant_id="kestrel_finance", use_case="kyc", max_requests_per_24h=4)
    assert other.not_before_min is None and other.c_eff == 4


def test_not_before_binds_fallback_tiers():
    # a hard 11:00 with approvers 14-16 and TTL 4: preferred = max(14:00, 11:00) has p = 57%;
    # tier 3 searches pre-open ticks at or after 11:00 only: 12:00 is the latest safe one
    pol = policy(tenant_id="kestrel_finance", use_case="cancel", **CASE_B)
    assert routine_slots(0, pol) == [hm(0, 12)]
    # TTL-12 case (a) with a not_before of 11:00: 11:00 has p = 61/421 and every safe tick
    # (10:00, pre-open) is before 11:00, so the day has no routine slot (NO_SAFE_WINDOW)
    pol_a = policy(tenant_id="kestrel_finance", use_case="cancel", **CASE_A)
    assert routine_slots(0, pol_a) == []


# ---------------------------------------------------------------- fatigue
def test_used_at_is_strict_like_the_simulator():
    used = [hm(0, 10)]
    assert used_at(hm(1, 9, 59), used) == 1
    assert used_at(hm(1, 10), used) == 0          # created > t - 1440 is false at exactly 24h
    assert used_at(hm(0, 9), used) == 0           # not yet created


def test_fatigue_reserve_and_classes():
    pol = policy(**RENEWAL_LIKE)                  # C 3, routine 2
    used = [hm(0, 11), hm(0, 15)]
    assert not fatigue_ok(hm(0, 18), pol, used, P3_ROUTINE)
    assert fatigue_ok(hm(0, 18), pol, used, P1_ROLLBACK)        # the reserve slot
    assert not fatigue_ok(hm(0, 18), pol, used + [hm(0, 17)], P0_INCIDENT)


def test_fatigue_cap_1_case_c():
    pol = policy(max_requests_per_24h=1, **RENEWAL_LIKE)
    assert (pol.c_eff, pol.reserve, pol.routine_budget) == (1, 0, 1)
    used = [hm(0, 11)]
    assert not fatigue_ok(hm(1, 11, 15), pol, used, P3_ROUTINE)  # outside 24h, inside 48h
    assert fatigue_ok(hm(2, 11), pol, used, P3_ROUTINE)          # exactly 48h: strict window
    assert fatigue_ok(hm(1, 11), pol, used, P0_INCIDENT)         # incident may use the slot
    assert choose_submit(hm(1, 0), pol, used, P3_ROUTINE, True) == hm(2, 11)


def test_directive_cap_does_not_bind_incidents():
    pol = policy(tenant_id="kestrel_finance", use_case="cancel", max_requests_per_24h=4,
                 **CANCEL_LIKE)
    used = [hm(0, 11), hm(0, 12)]                 # C_eff = 2 reached
    assert not fatigue_ok(hm(0, 13), pol, used, P2_PROMOTION)
    assert fatigue_ok(hm(0, 13), pol, used, P0_INCIDENT)        # policy cap 4 still free


# ---------------------------------------------------------------- choose_submit
def test_choose_submit_routine_only_at_slot_minutes():
    pol = policy(tenant_id="kestrel_finance", use_case="cancel", max_requests_per_24h=4,
                 **CANCEL_LIKE)
    assert choose_submit(hm(0, 9), pol, [], P3_ROUTINE, True) == hm(0, 11)
    assert choose_submit(hm(0, 11), pol, [], P3_ROUTINE, True) == hm(0, 11)
    assert choose_submit(hm(0, 11, 15), pol, [], P3_ROUTINE, True) == hm(1, 11)   # never late
    # yesterday's 11:00 request stops counting at exactly 11:00 today
    assert choose_submit(hm(1, 10), pol, [hm(0, 11)], P2_PROMOTION, True) == hm(1, 11)


def test_choose_submit_slot2_needs_slot1_terminal():
    pol = policy(**RENEWAL_LIKE)
    used = [hm(0, 11)]
    assert choose_submit(hm(0, 11, 30), pol, used, P3_ROUTINE, False) == hm(1, 11)
    assert choose_submit(hm(0, 11, 30), pol, used, P3_ROUTINE, True) == hm(0, 15)


def test_choose_submit_urgent_in_hours_now():
    pol = policy(**RENEWAL_LIKE)
    assert choose_submit(hm(0, 12, 7), pol, [], P0_INCIDENT, False) == hm(0, 12, 15)


def test_choose_submit_urgent_case_a_after_10_15():
    pol = policy(**CASE_A)
    # at 13:00 a P1: p(13:00) = L >= 300 -> 181/421, unsafe; every in-hours tick to 18:00 is
    # unsafe; the next in-hours safe tick is 10:00 tomorrow, which beats any pre-open tick
    assert choose_submit(hm(0, 13), pol, [], P1_ROLLBACK, False) == hm(1, 10)


def test_choose_submit_urgent_case_b_pre_open():
    pol = policy(**CASE_B)
    # 16:30: no in-hours tick in the next 24h is safe; outside hours the safe ticks are up to
    # 12:00 tomorrow, all decided around 14:00 or later; the earliest expected apply wins,
    # ties go to the latest submit (less queue time)
    m = choose_submit(hm(0, 16, 30), pol, [], P0_INCIDENT, False)
    assert m == hm(1, 10)          # every L decides at 14:00 sharp: the earliest apply
    cands = [t for t in range(hm(0, 16, 30), hm(1, 16, 30), 15) if p_expire(t, pol) <= 0.01]
    best = min(expected_apply_minute(t, pol) for t in cands)
    assert expected_apply_minute(m, pol) == best


def test_choose_submit_waits_for_policy_cap_to_roll_off():
    pol = policy(max_requests_per_24h=1, **CANCEL_LIKE)
    # the only slot is used until exactly D1 11:30 (strict window): first safe tick then
    assert choose_submit(hm(0, 12), pol, [hm(0, 11, 30)], P0_INCIDENT, False) == hm(1, 11, 30)
    assert choose_submit(hm(0, 12), pol, [hm(0, 11, 30), hm(0, 11, 45)], P0_INCIDENT,
                         False) == hm(1, 11, 45)
    # nothing frees inside the next 24h
    assert choose_submit(hm(0, 12), pol, [hm(0, 12)], P0_INCIDENT, False) is None
