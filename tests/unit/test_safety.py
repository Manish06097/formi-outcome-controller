"""Prompt safety verdict and gate evidence (DESIGN.md 7.1, ADR 0009): gate sizing, operating
characteristics by seeded simulation, designated evidence runs without fishing, LOW mode,
credit refusals, and deterministic fix ranking."""
from __future__ import annotations

import random

import numpy as np
import pytest

from controller.incidents.credits import OrderedCreditBudget, plan_pools
from controller.incidents.safety import (CreditsUnavailable, SafetyVerdictRunner, gate_plan,
                                         last_known_good, p_pass, rank_candidates)
from controller.settings import Thresholds
from controller.types import Suggestion, Verdict
from tests.support import synth
from tests.support.fake_sim import FakeSim

T = Thresholds()
P1_Q = 0.9 * 0.006 + 0.005     # validation failure rate of a P1-like safe prompt (sanity label)
P4_Q = 0.9 * 0.025 + 0.005     # P4-like, halluc 0.025: about 0.0275
P2_Q = 0.9 * 0.055 + 0.005     # P2-like


def runner(sim: FakeSim, g: float = 0.97, c0: float = 1e6) -> SafetyVerdictRunner:
    budget = OrderedCreditBudget(plan_pools(c0, 5, False, {"t": 1}))
    return SafetyVerdictRunner(sim, budget, gate_of=lambda wf: g, tenant_of=lambda wf: "t")


# ------------------------------------------------------------------------------- gate sizing

def test_gate_plan_at_097_098_099():
    a = gate_plan(0.97)
    assert not a.tight and a.n_e == 300 and a.p_pass_300 > 0.998
    b = gate_plan(0.98)
    assert not b.tight and b.p_pass_300 >= 0.966
    c = gate_plan(0.99)
    assert c.tight and c.n_e == 100
    assert c.p_pass_300 == pytest.approx(0.65, abs=0.03)
    assert p_pass(100, 0.0104, 0.99) == pytest.approx(0.72, abs=0.02)
    assert p_pass(50, 0.0104, 0.99) == pytest.approx(0.59, abs=0.02)


# ------------------------------------------------------------------------------- OC by simulation

def oc(q: float, mode: str, reps: int, seed: int) -> tuple[float, float]:
    unsafe, credits = 0, 0.0
    sim = FakeSim({"PX": q}, seed=seed, credits=1e9)
    for i in range(reps):
        r = runner(sim)
        v = r.run(f"wf{i}", "PX", mode, False)
        unsafe += v.verdict == "UNSAFE"
        credits += v.credits
    return unsafe / reps, credits / reps


@pytest.mark.parametrize("mode", ["full", "sequential"])
def test_p4_like_prompt_is_judged_unsafe_most_of_the_time(mode):
    rate, _ = oc(P4_Q, mode, 2000, seed=1)
    assert rate >= 0.88          # table: about 96% (full) and 93% (sequential)
    gate_alone = 1 - p_pass(300, P4_Q, 0.97)
    assert gate_alone < 0.40     # the client gate alone would let it through most of the time


@pytest.mark.parametrize("mode,credits", [("full", 13.0), ("sequential", 6.8)])
def test_p1_like_prompts_are_judged_safe(mode, credits):
    rate, spent = oc(0.0104, mode, 2000, seed=2)
    assert rate <= 0.01          # table: about 0.3%
    assert spent == pytest.approx(credits, abs=0.6)
    rate3, _ = oc(P1_Q, mode, 1000, seed=3)   # a P3-like prompt, also safe
    assert rate3 <= 0.01


def test_p2_like_prompt_fails_stage_one_almost_always():
    rate, spent = oc(P2_Q, "full", 1000, seed=4)
    assert rate >= 0.99 and spent == pytest.approx(3.4, abs=0.5)


def test_stage1_only_rule():
    sim = FakeSim({"PX": 0.0104}, seed=5)
    v = runner(sim).run("wf", "PX", "stage1_only", False)
    assert v.mode == "stage1_only" and len(v.runs) == 1 and v.credits == 3


# ------------------------------------------------------------------------------- evidence

def test_not_tight_designates_the_stage1_run_before_it_runs():
    sim = FakeSim({"P1": 0.0104}, seed=6)
    r = runner(sim, g=0.97)
    v = r.run("wf", "P1", "full", True, decision_key="inc1")
    assert v.verdict == "SAFE" and v.approvable
    assert v.gate_run == v.runs[0] == r.designated[("inc1", "wf", "P1")]
    assert len(sim.calls) == 2                       # no extra evidence run


def test_tight_gate_one_designated_run_and_never_fishing():
    sim = FakeSim({"P1": 0.03}, seed=7)              # a run of 100 fails g = 0.99 often
    r = runner(sim, g=0.99)
    sim.q["P1"] = 0.0
    v = r.run("wf", "P1", "full", False)             # verdict SAFE (no failures)
    sim.q["P1"] = 0.5                                # the evidence run will fail the gate
    e1 = r.run("wf", "P1", "full", True, decision_key="incA")
    assert e1.verdict == "SAFE" and not e1.approvable and e1.gate_run is None
    n_calls = len(sim.calls)
    again = r.run("wf", "P1", "full", True, decision_key="incA")
    assert len(sim.calls) == n_calls and not again.approvable   # same decision: never re-run
    sim.q["P1"] = 0.0
    other = r.run("wf", "P1", "full", True, decision_key="incB")
    assert other.approvable and len(sim.calls) == n_calls + 1    # a new decision, one new run
    assert sim.calls[-1][3] == 100                                 # n_e at g = 0.99
    assert v.verdict == "SAFE"


def test_cached_verdict_is_reused_and_cusum_recheck_pools_every_run():
    sim = FakeSim({"P1": 0.0104}, seed=8)
    r = runner(sim)
    v1 = r.run("wf", "P1", "sequential", False)
    n = len(sim.calls)
    assert r.run("wf", "P1", "full", False) == v1 and len(sim.calls) == n
    v2 = r.run("wf", "P1", "full", False, purpose="verdict_cusum", pool_with_prior=True)
    assert set(v1.runs) <= set(v2.runs)
    assert v2.pooled_n == sum(c[3] for c in sim.calls)


def test_low_mode_forces_stage1_only_and_refusals_raise():
    sim = FakeSim({"P1": 0.0104}, seed=9)
    r = runner(sim, c0=50)                           # LOW from the start
    assert r.credits.mode() == "low"
    v = r.run("wf", "P1", "full", False)
    assert v.mode == "stage1_only" and v.credits == 3
    broke = runner(FakeSim({"P1": 0.01}, credits=2, seed=1), c0=200)
    with pytest.raises(CreditsUnavailable):
        broke.run("wf", "P1", "full", False)
    assert broke.credits.out_of_credits                              # a 402 was seen


# ------------------------------------------------------------------------------- fix ranking

def spec():
    s = synth.spec_dict(prompts=("P1", "P2", "P3", "P4", "P5"))
    s["prompt_catalog"] = {"P1": "standard", "P2": "short", "P3": "warm",
                           "P4": "persuasive", "P5": "reads the compliance script word for word"}
    from controller.types import WorkflowSpec
    return WorkflowSpec.from_api(s)


def unsafe(p):
    return Verdict("wf_a", p, "UNSAFE", "full", ("val_1",), 300, 20, None, False, 3.0)


def test_rank_is_fixed_and_deterministic():
    sp = spec()
    live = {"P1": 0.55, "P3": 0.57}
    sugg = [Suggestion("P5", "x"), Suggestion("P3", "y"), Suggestion("P9", "not in catalog")]
    base, dropped = rank_candidates(sp, "P2", {"P4": unsafe("P4")}, live,
                                    ("verbatim", "word for word"), sugg)
    assert [c.prompt for c in base] == ["P3", "P1", "P5"]   # live by engagement, avoid last
    assert base[-1].avoided and "P4" not in [c.prompt for c in base]
    assert dropped == [{"prompt": "P9", "reason": "not in options.prompt_version"}]
    rng = random.Random(1)
    for _ in range(20):
        s2 = sugg[:]
        rng.shuffle(s2)
        l2 = dict(sorted(live.items(), key=lambda kv: rng.random()))
        again, _ = rank_candidates(sp, "P2", {"P4": unsafe("P4")}, l2,
                                   ("verbatim", "word for word"), s2)
        assert [c.prompt for c in again] == [c.prompt for c in base]
    no_llm, _ = rank_candidates(sp, "P2", {"P4": unsafe("P4")}, live,
                                ("verbatim", "word for word"), ())
    assert [c.prompt for c in no_llm] == [c.prompt for c in base]   # the LLM never reorders


def test_never_live_prompts_follow_catalog_order():
    ranked, _ = rank_candidates(spec(), "P1", {}, {}, ())
    assert [c.prompt for c in ranked] == ["P2", "P3", "P4", "P5"]


def flags_world():
    rows = []
    rng = np.random.default_rng(1)
    for h in range(-14 * 24, 0):
        if not 9 <= h % 24 < 21:
            continue
        p, q = (0, 0.015) if h < -7 * 24 else (2, 0.015)       # P1 then P3, both clean
        rows.append((h, p, 100, int(rng.binomial(100, q)), 50))
    for h in range(0, 12):
        rows.append((h, 1, 100, 6, 50))                          # P2 now, flagging
    return rows


def test_last_known_good_is_the_most_recent_clean_prompt():
    sp = spec()
    ws = synth.snapshot(sp, flags=synth.flag_series(flags_world()), now=12 * 60,
                        live=synth.config(prompt="P2"))
    assert last_known_good(ws, {}) == [("P3", "needs_verdict"), ("P1", "needs_verdict")]
    safe = Verdict("wf_a", "P1", "SAFE", "sequential", ("v",), 300, 2, None, False, 3.0)
    assert last_known_good(ws, {"P3": unsafe("P3"), "P1": safe}) == [("P1", "ok")]
    rows = [(h, p, n, (8 if p == 2 else f), e) for h, p, n, f, e in flags_world()]
    ws2 = synth.snapshot(sp, flags=synth.flag_series(rows), now=12 * 60,
                         live=synth.config(prompt="P2"))
    assert [p for p, _ in last_known_good(ws2, {})] == ["P1"]  # P3 flagged above baseline
