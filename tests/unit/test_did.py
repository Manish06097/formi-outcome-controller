"""Caller-ID health (DESIGN.md 5.9, 7.3): MH ratio, probe sizing, SPRT classification through the
production path, slow path, spread, no-healthy-DID rule, drain chains and the flag-day share."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.learning.did import (DidHealthModel, did_posterior, drain_chain, drain_step,
                                     flag_day_share, mh_ratio, probe_gate_share, probe_share,
                                     ratio_vs_others, trailing_window, tv)
from controller.settings import Thresholds
from controller.types import DidState, FrozenDict
from tests.support import synth

T = Thresholds()
TICK = 15


def test_mh_single_stratum_is_the_risk_ratio_with_the_textbook_variance():
    est = mh_ratio(np.array([30]), np.array([100]), np.array([60]), np.array([200]))
    assert est.r == pytest.approx(1.0)
    assert est.se_log ** 2 == pytest.approx(1 / 30 - 1 / 100 + 1 / 60 - 1 / 200)


def test_mh_stratification_removes_hour_confounding():
    """DID d dials evenly while its comparator dials mostly in a low-pickup hour: the crude
    ratio flatters d, MH says equal."""
    a = np.array([50, 10])
    n1 = np.array([100, 100])
    b = np.array([500, 400])
    n0 = np.array([1000, 4000])  # hour 1 p = 0.5, hour 2 p = 0.1
    est = mh_ratio(a, n1, b, n0)
    crude = (a.sum() / n1.sum()) / (b.sum() / n0.sum())
    assert est.r == pytest.approx(1.0)
    assert crude > 1.6


def test_mh_handles_a_dead_did():
    est = mh_ratio(np.array([0, 0]), np.array([100, 100]), np.array([25, 25]),
                   np.array([100, 100]))
    assert 0 < est.r < 0.05 and math.isfinite(est.se_log) and est.z < -4


def test_probe_sizing():
    assert probe_gate_share(T) == pytest.approx(0.0277, abs=1e-4)
    assert probe_share(3600, T) == pytest.approx(0.028)
    assert probe_share(54_000, T) == pytest.approx(400 / 54_000)   # 0.74%, volume binds
    assert probe_share(200_000, T) == pytest.approx(0.005)          # floor


@pytest.mark.parametrize("s,m,t_alarm,expect", [
    (0.333, 0.2, 1.75, 0.079), (0.386, 0.2, 1.75, 0.092), (0.486, 0.2, 1.5, 0.109),
    (0.333, 0.05, 1.75, 0.096), (0.486, 0.05, 1.5, 0.146)])
def test_flag_day_share_formula_matches_7_3(s, m, t_alarm, expect):
    assert flag_day_share(s, m, 1.0, t_alarm, 15, 12) == pytest.approx(expect, abs=0.0006)


@pytest.mark.parametrize("s,m", [(0.333, 0.2), (0.386, 0.2), (0.486, 0.05), (0.333, 0.05)])
def test_drain_chain_is_the_formula_chain(s, m):
    """Each step moves m off the flagged DID only (risk-reducing); the residuals the formula sums
    are exactly the chain's."""
    rest = (1 - s) / 2
    cur = {"D1": rest, "D2": s, "D3": rest}
    target = {"D1": 0.5, "D3": 0.5}
    steps = drain_chain(cur, target, ["D2"], m)
    assert len(steps) == math.ceil(s / m)
    prev = cur
    for j, st in enumerate(steps, 1):
        assert tv(prev, st) <= m + 1e-12
        assert st.get("D2", 0.0) == pytest.approx(max(s - j * m, 0.0), abs=1e-12)
        assert st["D1"] >= prev["D1"] - 1e-12 and st["D3"] >= prev["D3"] - 1e-12
        prev = st
    assert steps[-1] == pytest.approx(target)
    residual = sum(st.get("D2", 0.0) for st in steps[:-1])
    formula_tail = sum(max(s - j * m, 0) for j in range(1, math.ceil(s / m)))
    assert residual == pytest.approx(formula_tail)


def test_drain_step_never_takes_share_from_a_healthy_did():
    cur = {"D1": 0.7, "D2": 0.3}
    st = drain_step(cur, {"D1": 0.5, "D3": 0.5}, ["D2"], 0.2)
    assert st["D1"] == pytest.approx(0.7)
    assert st["D3"] == pytest.approx(0.2) and st["D2"] == pytest.approx(0.1)


# ------------------------------------------------------------------------------- production path

def probe_world(r_probe: float, days: int, seed: int, p: float = 0.26,
                dials_per_hour: int = 300):
    """Incumbent D1 at 0.972, probe D2 at 0.028 (renewal-like volume): r_did rows per tick."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21),
                                       dials_per_hour=dials_per_hour,
                                       shares={0: 0.972, 1: 0.028}, p=p, r={1: r_probe})
    return synth.did_arrays(rows)


def run_probe(r_probe: float, seed: int, days: int = 6):
    spec = synth.make_spec(dids=("D1", "D2"), dials_per_hour=300)
    live = synth.config({"D1": 0.972, "D2": 0.028})
    da = probe_world(r_probe, days, seed)
    model = DidHealthModel(p_hist=0.26)
    states = {"D1": DidState("wf_a", "D1", "healthy"), "D2": DidState("wf_a", "D2", "unknown")}
    for now in range(9 * 60, days * 1440, 60):           # hourly stops
        ws = synth.snapshot(spec, live=live, did=da, did_state=states, now=now)
        new, _ = model.step(ws)
        states = dict(new)
        if states["D2"].status in ("healthy", "unhealthy"):
            return states["D2"], now
    return states["D2"], None


def test_sprt_through_the_model_classifies_a_healthy_probe():
    outcomes = [run_probe(1.0, seed) for seed in range(30)]
    healthy = [s for s, _ in outcomes if s.status == "healthy"]
    assert len(healthy) >= 26                               # beta = 5% plus sampling slack
    dials = np.mean([s.n_dials for s in healthy])
    assert 150 < dials < 330                                # about 245 at p = 0.26


def test_sprt_through_the_model_rejects_a_flagged_probe_fast():
    outcomes = [run_probe(0.36, seed) for seed in range(20)]
    assert all(s.status == "unhealthy" for s, _ in outcomes)
    assert np.mean([s.n_dials for s, _ in outcomes]) == pytest.approx(52, rel=0.35)


def test_bootstrap_from_history():
    rng = np.random.default_rng(1)
    rows = []
    for d in range(-14, 0):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                       shares={0: 0.5, 1: 0.5}, p=0.25, r={1: 0.6})
    spec = synth.make_spec(dids=("D1", "D2", "D3"))
    ws = synth.snapshot(spec, did=synth.did_arrays(rows), now=0)
    st = DidHealthModel().bootstrap(ws)
    assert st["D1"].status == "healthy" and st["D2"].status == "degraded"
    assert st["D3"].status == "unknown"
    sole = synth.snapshot(spec, did=synth.did_arrays([r for r in rows if r[3] == 0]), now=0)
    assert DidHealthModel().bootstrap(sole)["D1"].status == "healthy"


def test_slow_path_degrades_a_mildly_bad_did():
    """r = 0.75 at a one-third share, cancel-like volume: degraded within about 12-24 dialling
    hours by the UCB < 0.85 rule (5.9)."""
    rng = np.random.default_rng(4)
    rows = []
    for d in range(3):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                       shares={0: 1 / 3, 1: 1 / 3, 2: 1 / 3}, p=0.25,
                                       r={2: 0.75})
    spec = synth.make_spec()
    live = synth.config({"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3})
    da = synth.did_arrays(rows)
    states = {n: DidState("wf_a", n, "healthy") for n in ("D1", "D2", "D3")}
    model = DidHealthModel()
    hit = None
    for now in range(10 * 60, 3 * 1440, 60):
        ws = synth.snapshot(spec, live=live, did=da, did_state=states, now=now)
        new, f = model.step(ws)
        states = dict(new)
        if states["D3"].status == "degraded":
            hit = now
            assert any(x.kind == "slow_degraded" and x.scope == "D3" for x in f)
            break
    assert hit is not None and hit <= 2 * 1440 + 21 * 60
    assert states["D1"].status == states["D2"].status == "healthy"


def test_trailing_window_counts_dialling_hours_only():
    rng = np.random.default_rng(0)
    rows = []
    for d in range(2):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                       shares={0: 0.5, 1: 0.5}, p=0.25)
    da = synth.did_arrays(rows)
    now = 1440 + 10 * 60          # D1 10:00: 1 dialling hour today, 11 more yesterday evening
    m = trailing_window(da, now, TICK, 2)
    ticks = np.unique(da.tick_no[m])
    assert len(ticks) == 8
    assert ticks.min() == (20 * 60) // TICK            # D0 20:00, the previous evening
    assert ticks.max() == (1440 + 9 * 60 + 45) // TICK  # D1 09:45
    est = ratio_vs_others(da, m, 0)
    assert est.dials > 0 and est.comp_dials > 0


# ------------------------------------------------------------------------------- planning

def plan(states: dict[str, str], live: dict[str, float], *, dids=("D1", "D2", "D3", "D4"),
         now: int = 0, end: int | None = None, auto: bool = True, max_shift: float = 0.2,
         fast: tuple[str, ...] = (), dials_per_hour: int = 300):
    spec = synth.make_spec(dids=dids, auto=auto, max_shift=max_shift,
                           dials_per_hour=dials_per_hour)
    st = {n: DidState("wf_a", n, s, evidence=FrozenDict({"path": "fast"} if n in fast else {}))
          for n, s in states.items()}
    ws = synth.snapshot(spec, live=synth.config(live), did_state=st, now=now)
    return DidHealthModel(end_minute=end).plan_caller_ids(ws)


def test_probes_two_unknown_dids_at_the_gate_share():
    reqs = plan({"D1": "healthy", "D2": "healthy", "D3": "unknown", "D4": "unknown"},
                {"D1": 0.5, "D2": 0.5})
    assert len(reqs) == 1
    v = dict(reqs[0].value)
    assert v["D3"] == pytest.approx(0.028) and v["D4"] == pytest.approx(0.028)
    assert v["D1"] == pytest.approx(v["D2"]) == pytest.approx((1 - 0.056) / 2)
    assert reqs[0].cls == 3 and reqs[0].path == ("alloc", "default", "caller_id")


def test_at_most_two_probes_at_a_time():
    reqs = plan({"D1": "healthy", "D2": "probing", "D3": "probing", "D4": "unknown"},
                {"D1": 0.944, "D2": 0.028, "D3": 0.028})
    assert reqs == [] or "D4" not in reqs[0].value


def test_spread_equal_over_healthy_and_unhealthy_removed():
    reqs = plan({"D1": "healthy", "D2": "healthy", "D3": "healthy", "D4": "unhealthy"},
                {"D1": 0.944, "D3": 0.028, "D4": 0.028})
    v = dict(reqs[0].value)
    assert set(v) == {"D1", "D2", "D3"}
    assert all(x == pytest.approx(1 / 3) for x in v.values())


def test_no_healthy_did_keeps_the_allocation_and_probes():
    reqs = plan({"D1": "degraded", "D2": "unknown", "D3": "unknown", "D4": "unknown"},
                {"D1": 1.0})
    v = dict(reqs[0].value)
    assert v["D2"] == pytest.approx(0.028) and v["D3"] == pytest.approx(0.028)
    assert "D4" not in v
    assert v["D1"] == pytest.approx(1 - 0.056)
    assert reqs[0].evidence["alert"] == "NO_HEALTHY_DID"


def test_freeze_blocks_new_probes_but_not_spreads():
    reqs = plan({"D1": "healthy", "D2": "healthy", "D3": "unknown", "D4": "unknown"},
                {"D1": 1.0}, now=9 * 1440, end=10 * 1440)
    v = dict(reqs[0].value)
    assert set(v) == {"D1", "D2"}


def test_freeze_reads_the_snapshot_end_minute():
    """The snapshot's end_minute (from /sim/state) drives the freeze; the model needs none."""
    spec = synth.make_spec(dids=("D1", "D2", "D3", "D4"))
    st = {n: DidState("wf_a", n, s) for n, s in
          {"D1": "healthy", "D2": "healthy", "D3": "unknown", "D4": "unknown"}.items()}
    ws = synth.snapshot(spec, live=synth.config({"D1": 1.0}), did_state=st, now=9 * 1440,
                        end_minute=10 * 1440)
    v = dict(DidHealthModel().plan_caller_ids(ws)[0].value)
    assert set(v) == {"D1", "D2"}
    start = 10 * 1440 - int(0.15 * 10 * 1440)                   # max(1 day, 15%) = 2160 min
    assert DidHealthModel().in_freeze(start, 10 * 1440)
    assert not DidHealthModel().in_freeze(start - 1, 10 * 1440)
    assert not DidHealthModel().in_freeze(9 * 1440)


def test_fast_drain_is_a_p0b_chain_step_where_auto_applies():
    reqs = plan({"D1": "healthy", "D2": "degraded", "D3": "healthy", "D4": "unknown"},
                {"D1": 0.307, "D2": 0.386, "D3": 0.307}, fast=("D2",), max_shift=0.2)
    r = reqs[0]
    assert r.cls == 0 and r.p0_rank == 1
    v = dict(r.value)
    assert v["D2"] == pytest.approx(0.186, abs=2e-6)  # one max_shift step (less the 1e-6 margin)
    assert "D4" not in v                                         # a drain carries no new probe
    assert r.evidence["chain"]["k"] == 2
    assert r.evidence["chain"]["final"] == pytest.approx({"D1": 0.5, "D3": 0.5})


def test_fast_drain_is_one_full_step_without_auto_apply():
    reqs = plan({"D1": "healthy", "D2": "degraded", "D3": "healthy"},
                {"D1": 0.307, "D2": 0.386, "D3": 0.307}, dids=("D1", "D2", "D3"),
                fast=("D2",), auto=False)
    assert reqs[0].cls == 0 and dict(reqs[0].value) == pytest.approx({"D1": 0.5, "D3": 0.5})
    assert "chain" not in reqs[0].evidence


def test_slow_path_spread_is_routine():
    reqs = plan({"D1": "healthy", "D2": "degraded", "D3": "healthy"},
                {"D1": 0.307, "D2": 0.386, "D3": 0.307}, dids=("D1", "D2", "D3"))
    assert reqs[0].cls == 3


def test_did_posterior_prior_for_unknown_dids():
    spec = synth.make_spec(dids=("D1", "D2"))
    ws = synth.snapshot(spec)
    st = {"D1": DidState("wf_a", "D1", "healthy", r_hat=1.0, r_lcb=0.9, r_ucb=1.1),
          "D2": DidState("wf_a", "D2", "unknown")}
    post = did_posterior(ws, st, seed=1, n=20_000)
    assert post.draws.shape == (20_000, 2)
    flagged = np.mean(post.draws[:, 1] == T.did_r_flag)
    assert flagged == pytest.approx(T.did_prior_p_flagged, abs=0.01)
    assert post.draws[:, 1].max() <= T.did_r_unknown_max
    assert np.median(post.draws[:, 0]) == pytest.approx(1.0, abs=0.01)
