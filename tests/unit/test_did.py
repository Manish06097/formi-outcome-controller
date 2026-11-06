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


def _mildly_bad_rows():
    rng = np.random.default_rng(4)
    rows = []
    for d in range(3):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                       shares={0: 1 / 3, 1: 1 / 3, 2: 1 / 3}, p=0.25,
                                       r={2: 0.75})
    return rows


def test_slow_path_shortfall_gets_a_proportional_share_by_default():
    """ADR 0030: with `did_slow_path_degrades` off (the default) a DID at r = 0.75 stays
    healthy, its estimate is updated, and the spread gives it a share proportional to r_d,
    so its load falls instead of everything moving onto the others."""
    spec = synth.make_spec()
    live = synth.config({"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3})
    da = synth.did_arrays(_mildly_bad_rows())
    states = {n: DidState("wf_a", n, "healthy") for n in ("D1", "D2", "D3")}
    model = DidHealthModel()
    for now in range(10 * 60, 2 * 1440 + 20 * 60, 60):
        states = dict(model.step(synth.snapshot(spec, live=live, did=da, did_state=states,
                                                now=now))[0])
    assert all(st.status == "healthy" for st in states.values())
    assert states["D3"].r_hat == pytest.approx(0.75, abs=0.08)
    ws = synth.snapshot(spec, live=live, did=da, did_state=states, now=2 * 1440 + 20 * 60)
    v = dict(DidHealthModel().plan_caller_ids(ws)[0].value)
    assert v["D3"] < 1 / 3 - 0.02 and v["D1"] > 1 / 3 and v["D2"] > 1 / 3
    assert v["D3"] / v["D1"] == pytest.approx(states["D3"].r_hat / states["D1"].r_hat, rel=0.02)


def test_slow_path_degrades_a_mildly_bad_did():
    """With `did_slow_path_degrades` on (the 5.9 rule as designed): r = 0.75 at a one-third
    share, cancel-like volume, degraded within about 12-24 dialling hours by the UCB < 0.85
    rule."""
    import dataclasses
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
    model = DidHealthModel(t=dataclasses.replace(Thresholds(), did_slow_path_degrades=True))
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


TWO_PROBE_GATE_SHARE = 0.02 / 1.2766
"""Hand-computed gate share of two untested DIDs probed together (DESIGN.md 0.1, 5.9): with
x_i = 1 - r_i, x_i = 0.65 with P(flagged) = 0.2, else uniform on [-0.1, 0.65], the change is
dd = -s (x_1 + x_2). P(x_1 + x_2 > q) = 0.05 needs both flagged (0.04, sum 1.3) plus the
one-flagged case 0.32 x (1.3 - q) / 0.75 = 0.01, so q = 1.2766 and s = 0.02 / q = 1.567%."""


def test_one_unknown_did_is_probed_at_the_full_probe_share():
    """One untested DID: s (1 - r_flag) = 0.028 x 0.65 = 0.0182 is inside the 2% line even if
    it is flagged, so the gate passes the whole volume-rule share."""
    reqs = plan({"D1": "healthy", "D2": "healthy", "D3": "unknown"}, {"D1": 0.5, "D2": 0.5},
                dids=("D1", "D2", "D3"))
    v = dict(reqs[0].value)
    assert v["D3"] == pytest.approx(0.028)
    assert reqs[0].evidence["harm_gate"]["passes_full"] is True


def test_two_unknown_dids_start_one_per_change_at_the_full_share():
    """Two untested DIDs together would have to share one change's harm budget (the gate
    gives each about 1.6%); one per change gets the full 2.8% and classifies sooner, so one
    starts now and the other at the next planning point (ADR 0030)."""
    reqs = plan({"D1": "healthy", "D2": "healthy", "D3": "unknown", "D4": "unknown"},
                {"D1": 0.5, "D2": 0.5})
    assert len(reqs) == 1
    v = dict(reqs[0].value)
    assert v["D3"] == pytest.approx(0.028) and "D4" not in v
    assert v["D1"] == pytest.approx(v["D2"]) == pytest.approx((1 - 0.028) / 2)
    count = reqs[0].evidence["parts"]["probe_count"]
    assert count["chosen"] == 1
    assert count["share_each_if_all"] == pytest.approx(TWO_PROBE_GATE_SHARE, abs=0.0008)
    assert reqs[0].cls == 3 and reqs[0].path == ("alloc", "default", "caller_id")
    # next planning point: D3 probing at its share, D4 starts at the full share too
    nxt = plan({"D1": "healthy", "D2": "healthy", "D3": "probing", "D4": "unknown"}, v)
    w = dict(nxt[0].value)
    assert w["D4"] == pytest.approx(0.028) and w["D3"] == pytest.approx(0.028)


def test_two_unknown_dids_together_get_the_gate_share():
    """The gate itself, on a change that starts both: about 1.57% each (hand derivation
    above), with P(dd < -2%) at most alpha_D."""
    spec = synth.make_spec(dids=("D1", "D2", "D3", "D4"))
    st = {"D1": DidState("wf_a", "D1", "healthy"), "D2": DidState("wf_a", "D2", "healthy"),
          "D3": DidState("wf_a", "D3", "unknown"), "D4": DidState("wf_a", "D4", "unknown")}
    ws = synth.snapshot(spec, live=synth.config({"D1": 0.5, "D2": 0.5}), did_state=st)
    m = DidHealthModel()
    target, _ = m.target(ws, {"D1": 0.5, "D2": 0.5}, st, True)
    gated, ev = m.gate(ws, {"D1": 0.5, "D2": 0.5}, target, st)
    assert gated["D3"] == pytest.approx(gated["D4"])
    assert gated["D3"] == pytest.approx(TWO_PROBE_GATE_SHARE, abs=0.0008)
    assert ev["passes_full"] is False and ev["p_dd_below_line"] <= 0.05


def test_a_readmitted_did_is_gated_on_its_last_estimate():
    """A DID dropped at r = 0.30 and re-admitted is not an untested DID: its probe is sized
    from that estimate, so the change stays inside the 2% line."""
    import math
    from controller.types import freeze
    prior = {"prior_r_hat": 0.30, "prior_r_ucb": 0.36, "prior_r_lcb": 0.25}
    st = {"D1": DidState("wf_a", "D1", "healthy"), "D2": DidState("wf_a", "D2", "healthy"),
          "D3": DidState("wf_a", "D3", "unknown", evidence=freeze(
              {"readmitted_from": "degraded", **prior}))}
    spec = synth.make_spec(dids=("D1", "D2", "D3"))
    ws = synth.snapshot(spec, live=synth.config({"D1": 0.5, "D2": 0.5}), did_state=st)
    reqs = DidHealthModel().plan_caller_ids(ws)
    s = dict(reqs[0].value)["D3"]
    se = math.log(0.36 / 0.30) / 1.959964
    r05 = 0.30 * math.exp(-1.645 * se)            # 5% quantile of the lognormal estimate
    assert s == pytest.approx(0.02 / (1 - r05), abs=0.001)
    assert s * (1 - 0.30) < 0.02


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
    assert v["D2"] == pytest.approx(0.028)          # one new probe per change (ADR 0030)
    assert "D3" not in v and "D4" not in v
    assert v["D1"] == pytest.approx(1 - 0.028)
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
    assert v["D2"] == pytest.approx(0.186, abs=3e-6)  # one max_shift step, 2e-6 under (ADR 0030)
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


def test_only_load_explained_degradation_is_readmitted():
    """After 3 days at share 0 a slow-path degraded DID is probed again (load decays); a
    fast-path one (a sudden drop below 0.75, 7.3) and a probe the SPRT judged unhealthy (a
    probe carries no load to decay; custom (h) re-probed a DID at r = 0.3 every 3 days) stay
    out (ADR 0030)."""
    from controller.types import freeze
    spec = synth.make_spec(dids=("D1", "D2", "D3", "D4", "D5"))
    st = {"D1": DidState("wf_a", "D1", "healthy"), "D2": DidState("wf_a", "D2", "healthy"),
          "D3": DidState("wf_a", "D3", "degraded", r_hat=0.4, r_ucb=0.5, since_minute=0,
                         evidence=freeze({"path": "fast"})),
          "D4": DidState("wf_a", "D4", "degraded", r_hat=0.8, r_ucb=0.84, since_minute=0,
                         evidence=freeze({"path": "slow"})),
          "D5": DidState("wf_a", "D5", "unhealthy", r_hat=0.3, r_ucb=0.6, since_minute=0,
                         evidence=freeze({"sprt_decision": "accept_h0"}))}
    ws = synth.snapshot(spec, live=synth.config({"D1": 0.5, "D2": 0.5}), did_state=st,
                        now=4 * 1440)
    states, _ = DidHealthModel().step(ws)
    assert states["D3"].status == "degraded"
    assert states["D4"].status == "unknown"
    assert states["D4"].evidence["prior_r_hat"] == pytest.approx(0.8)
    assert states["D5"].status == "unhealthy"


def test_every_drain_step_stays_under_the_cap_after_wire_rounding():
    """Regression (ADR 0030, custom (d)): a step of exactly max_shift - margin, rounded to 9
    decimals and normalised like the simulator, landed a hair over our cap, so one step of
    the max_shift 0.05 chain went to an approval and the drain stalled 75 minutes. Every step
    of the chain must now pass our own check on the stored form."""
    from controller.types import _sim_normalise
    live = {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}
    cap = 0.05 - T.max_shift_margin
    for _ in range(8):
        reqs = plan({"D1": "healthy", "D2": "degraded", "D3": "healthy"}, live,
                    dids=("D1", "D2", "D3"), fast=("D2",), max_shift=0.05)
        if not reqs:
            break
        new = _sim_normalise(dict(reqs[0].value))
        assert 0 < tv(live, new) <= cap
        live = new
    assert "D2" not in live


def test_floor_degrades_a_did_load_cannot_explain_and_never_readmits_it():
    """A healthy DID reading r = 0.3 against established peers (a flag on an incumbent before
    any comparator existed, so the fast detector never saw it fine) is below the load floor:
    degraded by the slow path even with the 0.85 rule off, and not re-admitted after 3 days."""
    rng = np.random.default_rng(7)
    rows = []
    for d in range(2):
        rows += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                       shares={0: 1 / 3, 1: 1 / 3, 2: 1 / 3}, p=0.25,
                                       r={2: 0.3})
    spec = synth.make_spec()
    live = synth.config({"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3})
    da = synth.did_arrays(rows)
    states = {n: DidState("wf_a", n, "healthy") for n in ("D1", "D2", "D3")}
    model = DidHealthModel()
    hit = None
    for now in range(10 * 60, 2 * 1440, 60):
        new, f = model.step(synth.snapshot(spec, live=live, did=da, did_state=states, now=now))
        states = dict(new)
        if states["D3"].status == "degraded":
            hit = now
            assert any(x.kind == "floor_degraded" and x.scope == "D3" for x in f)
            break
    assert hit is not None and hit <= 21 * 60
    assert states["D3"].evidence["path"] == "floor"
    assert states["D1"].status == states["D2"].status == "healthy"
    later = synth.snapshot(spec, live=synth.config({"D1": 0.5, "D2": 0.5}), did=da,
                           did_state=states, now=hit + 4 * 1440)
    assert DidHealthModel().step(later)[0]["D3"].status == "degraded"


def test_a_reweighting_that_lowers_expected_health_is_not_proposed():
    """Proportional shares from noisy estimates once moved 8% onto a DID at r = 0.34 (graded
    didD -0.009): a change that only re-weights DIDs already dialling must raise the posterior
    mean of DID health (what the grader judges at apply time), or it is churn. Here the
    proportional target moves 8.6% onto D1 at r = 0.8: inside the 2% harm line, so the gate
    alone would pass it."""
    spec = synth.make_spec(dids=("D1", "D2", "D3"))

    def st(r):
        return {n: DidState("wf_a", n, "healthy", r_hat=v, r_lcb=v * 0.97, r_ucb=v * 1.03)
                for n, v in r.items()}
    live = synth.config({"D1": 0.2, "D2": 0.4, "D3": 0.4})
    down = synth.snapshot(spec, live=live, did_state=st({"D1": 0.8, "D2": 1.0, "D3": 1.0}))
    assert DidHealthModel().plan_caller_ids(down) == []
    up = synth.snapshot(spec, live=live, did_state=st({"D1": 1.4, "D2": 1.0, "D3": 1.0}))
    reqs = DidHealthModel().plan_caller_ids(up)
    assert len(reqs) == 1 and dict(reqs[0].value)["D1"] > 0.2
    assert reqs[0].evidence["harm_gate"]["dd_mean"] > 0
