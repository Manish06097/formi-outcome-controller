"""Incident manager (DESIGN.md 7.1, 7.3, 17.1): prompt incident from trigger to P0a fix, one
suggest-fixes call per incident, LOW mode skip, ranked fallback, blocked states, determinism,
post-apply verification, the DID drain chain and its verification, MTTR."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from controller.incidents.credits import OrderedCreditBudget, plan_pools
from controller.incidents.manager import IncidentManagerImpl, predicted_mttr
from controller.incidents.safety import SafetyVerdictRunner
from controller.learning.did import DidHealthModel
from controller.settings import Thresholds
from controller.types import DidState, Finding, FlagMonitorState, FrozenDict, WorkflowSpec
from tests.support import synth
from tests.support.fake_sim import FakeSim

T = Thresholds()
PROMPTS = ("P1", "P2", "P3", "P4", "P5")
NOW = 3 * 1440 + 12 * 60       # D3 12:00, the stop after an 11:30 supervisor edit


def spec(g: float = 0.97) -> WorkflowSpec:
    d = synth.spec_dict(prompts=PROMPTS, g=g)
    d["prompt_catalog"] = {"P1": "standard", "P2": "short", "P3": "warm", "P4": "persuasive",
                           "P5": "reads the compliance script word for word"}
    return WorkflowSpec.from_api(d)


def history_flags(seed: int = 1, prompt: int = 0, until_hour: int = 3 * 24 + 11):
    rng = np.random.default_rng(seed)
    rows = []
    for h in range(-14 * 24, until_hour):
        if 9 <= h % 24 < 21:
            rows.append((h, prompt, 100, int(rng.binomial(100, 0.0147)),
                         int(rng.binomial(100, 0.47))))
    return rows


def manager(q: dict[str, float], *, c0: float = 200, g: float = 0.97, llm: bool = True,
            seed: int = 0):
    sim = FakeSim(q, credits=c0, seed=seed, catalog={"wf_a": [p for p in PROMPTS if p != "P2"]})
    budget = OrderedCreditBudget(plan_pools(c0, 5, False, {"tenant_a": 1}))
    runner = SafetyVerdictRunner(sim, budget, gate_of=lambda wf: g, tenant_of=lambda wf: "tenant_a")
    m = IncidentManagerImpl(runner, budget, DidHealthModel(), llm=sim, llm_enabled=llm)
    return m, sim


def ws_at(flags, prompt="P2", now=NOW, g=0.97, avoid=("verbatim", "word for word")):
    sp = spec(g)
    pol = synth.make_policy(sp, avoid_prompt_terms=avoid)
    return synth.snapshot(sp, flags=synth.flag_series(flags), now=now, policy=pol,
                          live=synth.config({"D1": 1.0}, prompt=prompt))


def changed(prompt="P2"):
    return Finding("wf_a", "prompt", "changed", prompt, 0.0, 0.0, NOW,
                   FrozenDict({"version": 5, "actor": "supervisor", "mode": "full"}))


SAFE_Q = {"P1": 0.0104, "P2": 0.0545, "P3": 0.0104, "P4": 0.0275, "P5": 0.0104}


def test_supervisor_prompt_edit_to_p0a_fix_with_lkg():
    m, sim = manager(SAFE_Q)
    ws = ws_at(history_flags())
    reqs = m.on_findings(ws, [changed()])
    assert len(reqs) == 1
    r = reqs[0]
    assert (r.path, r.value, r.cls, r.p0_rank) == (("prompt",), "P1", 0, 0)
    assert r.evidence["tier"] == "lkg"
    gate = r.evidence["validation_run_id"]
    assert gate in r.evidence["all_validation_runs"]
    assert "P1" in r.evidence["summary"] and "P2" in r.evidence["summary"]
    inc = m.open_incidents("wf_a", "prompt_safety")[0]
    assert inc.state == "fixing" and inc.evidence["bad_prompt"] == "P2"
    # exactly one suggest-fixes call for the incident, and it was recorded
    assert [c[0] for c in sim.calls].count("suggest") == 1
    assert any(d["kind"] == "suggest_fixes" and d["outcome"].get("suggestions") is not None
               for d in m.decisions)
    # the next stop: no new incident, no second call, no new patch while the fix is pending
    again = m.on_findings(ws, [changed()])
    assert again == [] and [c[0] for c in sim.calls].count("suggest") == 1
    assert len(m.incidents) == 1


def test_low_mode_skips_suggest_fixes_and_records_the_skip():
    m, sim = manager(SAFE_Q, c0=50)
    m.on_findings(ws_at(history_flags()), [changed()])
    assert "suggest" not in [c[0] for c in sim.calls]
    assert any(d["kind"] == "suggest_fixes" and d["outcome"]["called"] is False
               and d["outcome"]["reason"] == "credits LOW" for d in m.decisions)
    # every verdict was stage-1 only
    assert all(c[3] == 300 for c in sim.calls if c[0] == "validate")


def test_a_safe_supervisor_prompt_opens_nothing():
    m, sim = manager({**SAFE_Q, "P2": 0.0104})
    assert m.on_findings(ws_at(history_flags()), [changed()]) == []
    assert m.incidents == {}
    assert any(d["kind"] == "prompt_verdict" and d["outcome"]["verdict"] == "SAFE"
               for d in m.decisions)


def test_unsafe_default_uses_the_ranked_catalog_and_skips_avoid_terms():
    """No LKG (the bad prompt is the only one ever live): live prompts first, then catalog order;
    P5 matches the Kestrel avoid term and goes last; P4 is unsafe."""
    m, sim = manager({**SAFE_Q, "P1": 0.0545, "P3": 0.0104})
    flags = history_flags(prompt=0)
    ws = ws_at(flags, prompt="P1")
    f = Finding("wf_a", "prompt", "changed", "P1", 0, 0, NOW, FrozenDict({}))
    reqs = m.on_findings(ws, [f])
    assert reqs[0].value == "P2" or reqs[0].value == "P3"
    order = [c["prompt"] for c in reqs[0].evidence["checked"]]
    # P2 (never live, catalog order first) is UNSAFE at q 0.0545, so P3 wins
    assert order[:2] == ["P2", "P3"] and reqs[0].value == "P3"
    assert reqs[0].evidence["tier"] == "catalog"


def test_no_safe_prompt_blocks_and_pages():
    m, sim = manager({p: 0.08 for p in PROMPTS})
    reqs = m.on_findings(ws_at(history_flags(prompt=1), prompt="P2"), [changed()])
    assert reqs == []
    inc = m.open_incidents("wf_a")[0]
    assert inc.state == "blocked" and inc.evidence["blocked"] == "NO_SAFE_PROMPT"
    assert any(a["code"] == "NO_SAFE_PROMPT" for a in m.alerts)
    n = len(sim.calls)
    m.on_findings(ws_at(history_flags(prompt=1), prompt="P2"), [])   # re-tried, cached verdicts
    assert len([c for c in sim.calls[n:] if c[0] == "validate"]) == 0


def test_client_gate_unreachable_at_a_strict_gate():
    q = {**SAFE_Q, "P1": 0.0, "P3": 0.0, "P5": 0.0}
    m, sim = manager(q, g=0.995)
    sim.q.update({"P1": 0.0, "P3": 0.0, "P5": 0.0})
    # make every designated evidence run fail: verdicts first see clean runs, then the
    # evidence runs see a failure rate the gate cannot pass
    orig = sim.validate

    def validate(wf, prompt, n):
        if n not in (300, 1000):
            sim.q[prompt] = 0.5
        return orig(wf, prompt, n)
    sim.validate = validate
    reqs = m.on_findings(ws_at(history_flags()), [changed()])
    assert reqs == []
    assert m.open_incidents("wf_a")[0].evidence["blocked"] == "CLIENT_GATE_UNREACHABLE"


def test_fix_selection_is_deterministic():
    outs = []
    for _ in range(3):
        m, _ = manager(SAFE_Q, seed=5)
        r = m.on_findings(ws_at(history_flags()), [changed()])[0]
        outs.append((r.value, r.evidence["validation_run_id"],
                     tuple(c["prompt"] for c in r.evidence["checked"])))
    assert outs[0] == outs[1] == outs[2]


def after_fix_flags(rate: float, engage: float, start_hour: int, hours: int, code: int = 0):
    rng = np.random.default_rng(42)
    return [(h, code, 100, int(rng.binomial(100, rate)), int(rng.binomial(100, engage)))
            for h in range(start_hour, start_hour + hours) if 9 <= h % 24 < 21]


def test_post_apply_verification_resolves_or_reopens():
    m, sim = manager(SAFE_Q)
    base = history_flags()
    m.on_findings(ws_at(base), [changed()])
    fix_at = NOW + 60
    ws_live = ws_at(base, prompt="P1", now=fix_at)
    assert m.verify(ws_live) == []                       # live, but no calls yet
    inc = m.open_incidents("wf_a")[0]
    assert inc.state == "verifying"
    good = base + after_fix_flags(0.0147, 0.47, fix_at // 60, 30)
    for hours in range(4, 30):
        f = m.verify(ws_at(good, prompt="P1", now=fix_at + hours * 60))
        if f:
            break
    assert [x.kind for x in f] == ["verified"]
    assert hours <= 26                               # decided inside the 24h window (+ a stop)
    assert m.incidents[inc.incident_id].state == "resolved"
    # a second world where the fix still flags at 4x: reopened, next candidate next stop
    m2, _ = manager(SAFE_Q)
    m2.on_findings(ws_at(base), [changed()])
    m2.verify(ws_at(base, prompt="P1", now=fix_at))
    bad = base + after_fix_flags(0.06, 0.47, fix_at // 60, 6)
    f2 = m2.verify(ws_at(bad, prompt="P1", now=fix_at + 6 * 60))
    assert [x.kind for x in f2] == ["verify_failed"]
    inc2 = m2.open_incidents("wf_a")[0]
    assert inc2.state == "open" and list(inc2.evidence["tried"]) == ["P1"]
    nxt = m2.on_findings(ws_at(bad, prompt="P1", now=fix_at + 6 * 60), [])
    assert nxt and nxt[0].value not in ("P1", "P2")


def test_engagement_collapse_fails_verification():
    m, _ = manager(SAFE_Q)
    base = history_flags()
    m.on_findings(ws_at(base), [changed()])
    m.verify(ws_at(base, prompt="P1", now=NOW + 60))
    poor = base + after_fix_flags(0.0147, 0.30, (NOW + 60) // 60, 6)
    f = m.verify(ws_at(poor, prompt="P1", now=NOW + 7 * 60))
    assert f[0].kind == "verify_failed" and f[0].evidence["engagement_ok"] is False


def test_mttr_formula_public_02_shape():
    assert predicted_mttr(690, 720, 20, 15) == 75
    assert predicted_mttr(690, 720, 120, 15) == 165
    assert predicted_mttr(690, 720, 106, 15) == 165


# ------------------------------------------------------------------------------- DID incidents

def did_ws(live_dist, now, rows):
    sp = synth.make_spec(dids=("D1", "D2", "D3"), dials_per_hour=400, max_shift=0.2)
    st = {n: DidState("wf_a", n, "healthy") for n in ("D1", "D2", "D3")}
    return synth.snapshot(sp, live=synth.config(live_dist), now=now, did=synth.did_arrays(rows),
                          did_state=st)


def test_did_alarm_drains_in_chain_steps_then_verifies():
    m, _ = manager(SAFE_Q)
    rng = np.random.default_rng(3)
    rows = []
    for d in range(2):
        rows += synth.simulate_did_day(rng, day=d, tick=15, window=(9, 21), dials_per_hour=400,
                                       shares={0: 0.307, 1: 0.386, 2: 0.307}, p=0.25)
    live = {"D1": 0.307, "D2": 0.386, "D3": 0.307}
    t0 = 1440 + 11 * 60 + 45
    rows = [r for r in rows if r[0] < (t0 + 30) // 15]          # D2 dialled until the drain
    alarm = Finding("wf_a", "did", "alarm", "D2", -4.6, -4.0, 1440 + 10 * 60,
                    FrozenDict({"window_h": 2, "r": 0.36}))
    reqs = m.on_findings(did_ws(live, t0, rows), [alarm])
    assert len(reqs) == 1 and reqs[0].cls == 0 and reqs[0].p0_rank == 1
    step1 = dict(reqs[0].value)
    assert step1["D2"] == pytest.approx(0.186, abs=1e-5)
    assert m.did_overrides[("wf_a", "D2")].status == "degraded"
    reqs2 = m.on_findings(did_ws(step1, t0 + 15, rows), [])
    step2 = dict(reqs2[0].value)
    assert "D2" not in step2 and step2 == pytest.approx({"D1": 0.5, "D3": 0.5})
    assert m.on_findings(did_ws(step2, t0 + 30, rows), []) == []
    inc = m.open_incidents("wf_a", "did_degraded")[0]
    assert inc.state == "verifying" and len(inc.evidence["steps"]) == 2
    # two dialling hours after the last step, on healthy DIDs only: verified
    after = synth.simulate_did_day(np.random.default_rng(4), day=1, tick=15, window=(9, 21),
                                   dials_per_hour=400, shares={0: 0.5, 2: 0.5}, p=0.25)
    after = [r for r in after if r[0] >= (t0 + 30) // 15]
    assert m.verify(did_ws(step2, t0 + 90, rows + after)) == []       # 1 hour: not yet
    f = m.verify(did_ws(step2, t0 + 30 + 120, rows + after))
    assert [x.kind for x in f] == ["verified"]
    assert m.incidents[inc.incident_id].state == "resolved"


def test_suspect_and_clear_toggle_the_did_state():
    m, _ = manager(SAFE_Q)
    ws = did_ws({"D1": 0.5, "D2": 0.5}, 600, [])
    m.on_findings(ws, [Finding("wf_a", "did", "suspect", "D2", -2.7, -2.5, 540, FrozenDict({}))])
    assert m.did_overrides[("wf_a", "D2")].status == "suspect"
    ws2 = replace(ws, did_state=FrozenDict({**ws.did_state,
                                            "D2": m.did_overrides[("wf_a", "D2")]}))
    m.on_findings(ws2, [Finding("wf_a", "did", "clear", "D2", -1.0, -2.5, 600, FrozenDict({}))])
    assert m.did_overrides[("wf_a", "D2")].status == "healthy"


# ------------------------------------------------------------------------------- KPI drift (7.5)

def test_pickup_drift_on_a_sole_did_marks_it_suspect_and_starts_probes():
    m, sim = manager(SAFE_Q)
    ws = did_ws({"D1": 1.0}, 2 * 1440, [])
    ws = replace(ws, did_state=FrozenDict({"D1": DidState("wf_a", "D1", "healthy"),
                                           "D2": DidState("wf_a", "D2", "unknown"),
                                           "D3": DidState("wf_a", "D3", "unknown")}))
    f = Finding("wf_a", "drift", "pickup_down", "workflow", 6.3, 6.0, 2 * 1440 - 600,
                FrozenDict({"z": -2.0}))
    assert m.on_findings(ws, [f]) == []                       # never a config change by itself
    assert m.did_overrides[("wf_a", "D1")].status == "suspect"
    assert any(a["code"] == "KPI_DRIFT" for a in m.alerts)
    plan = m.did_model.plan_caller_ids(m.states_for(ws))
    v = dict(plan[0].value)
    assert v["D2"] == pytest.approx(0.028) and v["D3"] == pytest.approx(0.028)
    assert plan[0].evidence["alert"] == "NO_HEALTHY_DID"
    assert sim.calls == []                                    # the DID path needs no credits


def test_engagement_drift_pays_for_an_incumbent_verdict_only_when_flags_are_up():
    m, sim = manager({**SAFE_Q, "P1": 0.0545})
    ws = ws_at(history_flags(), prompt="P1")
    f = Finding("wf_a", "drift", "engagement_down", "workflow", 6.5, 6.0, NOW, FrozenDict({}))
    low = FlagMonitorState("wf_a", "P1", 0.0147, 0.0294, 10.5, 1.0, 3, 900, 0, False, 0)
    m.flag_states["wf_a"] = low
    m.on_findings(ws, [f])
    assert sim.calls == []
    m.flag_states["wf_a"] = replace(low, stat=6.0)
    m.on_findings(ws, [f])
    assert sim.calls and sim.calls[0][2] == "P1"
    assert m.open_incidents("wf_a", "prompt_safety")
