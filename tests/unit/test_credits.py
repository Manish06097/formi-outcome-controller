"""Credit budget (DESIGN.md 7.2, ADR 0021): pool arithmetic at C0 = 50, 100, 200, 400, LOW mode,
pool rules, top-up and screen release, 402, and the ledger rows (against Postgres when one is
available)."""
from __future__ import annotations

import pytest

from controller.incidents.credits import (OrderedCreditBudget, plan_pools, worst_incident,
                                          worst_incident_low)
from controller.settings import Thresholds

T = Thresholds()


@pytest.mark.parametrize("c0,r,s,u,mode", [(50, 40, 10, 0, "low"), (100, 80, 20, 0, "normal"),
                                           (200, 130, 50, 20, "normal"),
                                           (400, 130, 100, 170, "normal")])
def test_pool_table_of_7_2(c0, r, s, u, mode):
    plan = plan_pools(c0, k=5, tight=False, workflows_by_tenant={"a": 2, "b": 1},
                      llm_suggest=False)
    assert plan.w == 65
    assert (plan.r, plan.s, plan.u) == pytest.approx((r, s, u))
    assert plan.start_mode == mode
    assert sum(plan.s_by_tenant.values()) == pytest.approx(s)
    assert plan.s_by_tenant["a"] == pytest.approx(2 * s / 3)


def test_section_17_suggest_call_is_part_of_the_worst_incident():
    plan = plan_pools(200, 5, False, {"a": 1})          # CTL_LLM_SUGGEST on (the default)
    assert plan.w == 68
    assert (plan.r, plan.s, plan.u) == pytest.approx((136, 50, 14))
    assert plan_pools(84, 5, False, {"a": 1}).start_mode == "low"     # 0.8 x 84 < 68
    assert plan_pools(85, 5, False, {"a": 1}).start_mode == "normal"


def test_worst_incident_with_a_tight_gate_and_low_mode_cost():
    assert worst_incident(5, tight=True, t=T) == 70
    assert worst_incident_low(5, tight=False, t=T) == 15
    assert worst_incident_low(5, tight=True, t=T) == 20
    # LOW from the start whenever 0.8 C0 < W: C0 < 82 at K = 5
    assert plan_pools(81, 5, False, {"a": 1}, llm_suggest=False).start_mode == "low"
    assert plan_pools(82, 5, False, {"a": 1}, llm_suggest=False).start_mode == "normal"


def test_pool_rules():
    b = OrderedCreditBudget(plan_pools(200, 5, False, {"t1": 2, "t2": 1}, llm_suggest=False))
    assert not b.authorize("t1", "R", "d0_screen", 3)          # R pays incidents only
    assert b.authorize("t1", "S", "d0_screen", 3)
    assert b.left("S", "t1") == pytest.approx(50 * 2 / 3 - 3)
    assert not b.authorize("t2", "S", "d0_screen", 20)          # t2's envelope is 16.7
    assert b.authorize("t1", "R", "verdict_full", 13)
    assert b.left("R") == 117
    assert b.mode() == "normal"


def test_low_mode_when_the_reserve_cannot_cover_one_worst_incident():
    b = OrderedCreditBudget(plan_pools(100, 5, False, {"t": 1}, llm_suggest=False))
    assert b.mode() == "normal"                                  # R + U = 80 >= 65
    assert b.authorize_incident("t", "verdict_full", 13) == "R"
    assert b.left("R") == 67 and b.mode() == "normal"
    assert b.authorize_incident("t", "fix_candidate", 3) == "R"
    assert b.mode() == "low"                                     # 64 < 65
    b2 = OrderedCreditBudget(plan_pools(200, 5, False, {"t": 1}, llm_suggest=False))
    b2.sync(60)                                                  # the API's balance governs too
    assert b2.mode() == "low"


def test_top_up_release_and_402():
    b = OrderedCreditBudget(plan_pools(400, 5, False, {"t": 1}, llm_suggest=False))
    for _ in range(6):
        assert b.authorize_incident("t", "fix_candidate", 13) == "R"
    assert b.left("R") == 52
    moved = b.top_up_reserve()
    assert moved == 13 and b.left("R") == 65 and b.left("U") == 157
    assert b.authorize("t", "S", "d0_screen", 7)
    assert b.release_screens() == pytest.approx(93)
    assert b.left("U") == pytest.approx(250) and b.left("S", "t") == 0
    b.on_402()
    assert b.mode() == "low"
    assert not b.authorize("t", "U", "d0_screen", 1)


def test_envelopes_round_trip():
    plan = plan_pools(200, 5, False, {"t": 1}, llm_suggest=False)
    b = OrderedCreditBudget(plan)
    b.authorize_incident("t", "verdict_full", 3)
    again = OrderedCreditBudget.from_envelopes(plan, b.envelopes())
    assert again.left("R") == b.left("R") and again.left("S", "t") == b.left("S", "t")


@pytest.mark.pg
def test_record_writes_ledger_and_envelope(pg_dsn):
    import psycopg

    from controller.store.db import apply_migrations
    plan = plan_pools(200, 5, False, {"t": 1}, llm_suggest=False)
    b = OrderedCreditBudget(plan)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS t4_credits CASCADE")
        with conn.transaction():
            apply_migrations(conn, "run", "t4_credits")
        conn.execute("SET search_path TO t4_credits")
        assert b.authorize_incident("t", "verdict_full", 3) == "R"
        with conn.transaction():
            b.record(conn, intent_id=7, pool="R", cost=3, remaining_after=197, wf=1,
                     sim_minute=720)
        row = conn.execute("SELECT tenant, pool, purpose, cost, remaining_after, intent_id"
                           " FROM credit_ledger").fetchone()
        assert row == ("t", "R", "verdict_full", 3.0, 197.0, 7)
        env = conn.execute("SELECT allotted, spent, mode FROM credit_envelope WHERE pool = 'R'"
                           ).fetchone()
        assert env == (130.0, 3.0, "normal")
        conn.execute("DROP SCHEMA t4_credits CASCADE")


def test_refund_undoes_an_authorize_whose_spend_is_already_stored():
    """A replayed intent after a crash: the result transaction committed before the process died,
    so the in-memory debit made for the repeat must be given back (ADR 0032)."""
    b = OrderedCreditBudget(plan_pools(200, 5, False, {"a": 1}, llm_suggest=False))
    b.sync(200)
    before = (b.left("R"), b.api_remaining)
    assert b.authorize("a", "R", "verdict_full", 3)
    assert (b.left("R"), b.api_remaining) == (before[0] - 3, before[1] - 3)
    b.refund("a", "R", 3)
    assert (b.left("R"), b.api_remaining) == before
