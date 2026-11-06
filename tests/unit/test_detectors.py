"""Detectors (DESIGN.md 7.1, 7.3, 7.5): DID detector delay and false-alarm rate by simulation,
flag CUSUM on the hourly series, prompt triggers and D0 screens, KPI drift CUSUM."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.incidents.detectors import (DidDetector, DriftDetector, FlagCusum, PromptTrigger,
                                            ScreenInput, detect, poisson_lower, poisson_sf)
from controller.settings import Thresholds
from controller.types import (Config, ConfigChange, DetectorState, DidState, Verdict)
from tests.support import synth

T = Thresholds()
TICK = 15


def cancel_like_did_rows(seed: int, days: int, flag_minute: int | None, s: float,
                         r_flag: float = 0.36):
    rng = np.random.default_rng(seed)
    rest = (1 - s) / 2
    rows = []
    for d in range(days):
        rows += synth.simulate_did_day(
            rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
            shares={0: rest, 1: s, 2: rest}, p=0.25,
            r_after={1: (flag_minute, r_flag)} if flag_minute is not None else None)
    return rows


def did_ws(rows, now, did_state=None):
    spec = synth.make_spec(dials_per_hour=400)
    return synth.snapshot(spec, did=synth.did_arrays(rows), now=now, did_state=did_state or {})


@pytest.mark.parametrize("s", [0.333, 0.486])
def test_did_detector_alarm_delay_matches_7_3(s):
    """Flag to r = 0.36 at D1 10:00, cancel-like volume: alarm about 1.5 to 1.75 h later, a
    suspect earlier; one tick-sized stop at a time from the flag."""
    flag = 1440 + 10 * 60
    delays, suspect_first = [], 0
    for seed in range(30):
        rows = cancel_like_did_rows(seed, 2, flag, s)
        first_suspect = None
        for now in range(flag + TICK, 1440 + 21 * 60, TICK):
            f = DidDetector().detect(did_ws(rows, now))
            kinds = {x.kind for x in f if x.scope == "D2"}
            if "suspect" in kinds and first_suspect is None:
                first_suspect = now
            if "alarm" in kinds:
                delays.append((now - flag) / 60)
                suspect_first += first_suspect is not None and first_suspect < now
                break
    assert len(delays) == 30
    assert 1.0 <= np.median(delays) <= 2.25
    assert max(delays) <= 3.0
    assert suspect_first >= 20


def test_did_detector_false_alarm_rate_by_simulation():
    """No flag, three equal DIDs, 150 dialling days of hourly stops: alarms are at most a couple
    (bound 3.2e-5 per test x 3 windows x 3 DIDs x 12 stops x 150 days = 0.52 expected) and
    suspects near P(z <= -2.5) = 0.6% per DID-stop."""
    rng_rows = cancel_like_did_rows(seed=123, days=150, flag_minute=None, s=1 / 3)
    rows = np.array(rng_rows)
    alarms = suspects = tests = 0
    for d in range(1, 150):
        day_rows = rows[(rows[:, 0] >= (d - 1) * 1440 // TICK) & (rows[:, 0] < (d + 1) * 1440 //
                                                                  TICK)]
        for hour in range(10, 22):
            now = d * 1440 + hour * 60
            f = DidDetector().detect(did_ws([tuple(r) for r in day_rows], now))
            alarms += sum(x.kind == "alarm" for x in f)
            suspects += sum(x.kind == "suspect" for x in f)
            tests += 3
    assert alarms <= 2
    rate = suspects / tests
    assert 0.002 <= rate <= 0.012, rate


def test_did_detector_needs_a_comparator_and_skips_drained_dids():
    rows = [r for r in cancel_like_did_rows(1, 1, 9 * 60, 1.0, r_flag=0.3) if r[3] == 1]
    assert DidDetector().detect(did_ws(rows, 15 * 60)) == []
    rows = cancel_like_did_rows(2, 1, 9 * 60, 0.333)
    st = {"D2": DidState("wf_a", "D2", "degraded")}
    assert not any(x.scope == "D2" for x in DidDetector().detect(did_ws(rows, 15 * 60, st)))
    st = {"D1": DidState("wf_a", "D1", "suspect")}
    clean = cancel_like_did_rows(3, 1, None, 0.333)
    f = DidDetector().detect(did_ws(clean, 15 * 60, st))
    assert any(x.kind == "clear" and x.scope == "D1" for x in f)


# ------------------------------------------------------------------------------- flag CUSUM

def flag_rows(seed, hours, p_by_hour, prompt=0, n=100, start=0, engage=0.5):
    rng = np.random.default_rng(seed)
    out = []
    for h in range(start, start + hours):
        if not 9 <= h % 24 < 21:
            continue
        p = p_by_hour(h)
        out.append((h, prompt, n, int(rng.binomial(n, p)), int(rng.binomial(n, engage))))
    return out


def flag_ws(rows, now, prompt="P1"):
    spec = synth.make_spec()
    return synth.snapshot(spec, flags=synth.flag_series(rows), now=now,
                          live=synth.config({"D1": 1.0}, prompt=prompt))


def test_flag_cusum_alarms_on_a_jump_and_not_in_control():
    hist = flag_rows(1, 14 * 24, lambda h: 0.0147, start=-14 * 24)
    live = flag_rows(2, 10 * 24, lambda h: 0.0147, start=0)
    fc = FlagCusum()
    st, _ = fc.update(flag_ws(hist, 0), None)
    assert st.p0 == pytest.approx(0.0147, rel=0.25)
    assert st.h == pytest.approx(np.log(30 * 1200), rel=0.01)
    st, f = fc.update(flag_ws(hist + live, 10 * 1440), st)
    assert not st.alarm and not any(x.kind == "alarm" for x in f)
    jump = flag_rows(3, 3 * 24, lambda h: 0.0535, start=10 * 24)
    alarm_at = None
    for now in range(10 * 1440 + 60, 13 * 1440, 60):
        st, f = fc.update(flag_ws(hist + live + jump, now), st)
        if any(x.kind == "alarm" for x in f):
            alarm_at = now
            break
    assert alarm_at is not None
    connected_since = (alarm_at - 10 * 1440) // 60 * 100 * 12 // 24
    assert connected_since <= 2000


def test_flag_cusum_resets_on_a_prompt_change_and_opens_on_live_evidence():
    hist = flag_rows(1, 14 * 24, lambda h: 0.0147, start=-14 * 24)
    fc = FlagCusum()
    st, _ = fc.update(flag_ws(hist, 0), None)
    bad = flag_rows(5, 24, lambda h: 0.08, prompt=1, start=0)
    st2, f = fc.update(flag_ws(hist + bad, 22 * 60, prompt="P2"), st)
    assert st2.prompt == "P2" and any(x.kind == "reset" for x in f)
    assert st2.calls == 12 * 100          # the P2 hours before the stop that saw the change
    assert any(x.kind == "live_open" for x in f)


def test_poisson_helpers():
    assert poisson_sf(0, 3.0) == 1.0
    assert poisson_sf(10, 3.0) == pytest.approx(0.001102, rel=1e-3)
    lam = poisson_lower(10, 0.99)
    assert poisson_sf(10, lam) == pytest.approx(0.01, rel=1e-6)


# ------------------------------------------------------------------------------- prompt trigger

def change(actor, version=2, dims=("prompt_version",), prompt="P2"):
    cfg = synth.config(prompt=prompt)
    return ConfigChange("wf_a", version, actor, "fetched", cfg, tuple(dims), ())


def verdict(prompt, v):
    return Verdict("wf_a", prompt, v, "full", ("val_1",), 1300, 5, None, False, 13.0)


def test_prompt_trigger():
    pt = PromptTrigger()
    ws = flag_ws([], 0, prompt="P2")
    f = pt.on_config_changes(ws, [change("supervisor")], {})
    assert [(x.kind, x.scope) for x in f] == [("changed", "P2")]
    assert pt.on_config_changes(ws, [change("controller_approved")], {}) == []
    assert pt.on_config_changes(ws, [change("supervisor", dims=("voice",))], {}) == []
    assert pt.on_config_changes(ws, [change("supervisor")], {"P2": verdict("P2", "SAFE")}) == []
    f = pt.on_config_changes(ws, [change("supervisor")], {"P2": verdict("P2", "UNSAFE")})
    assert f[0].kind == "unsafe_cached"


def test_d0_screens_order_escalation_and_low_mode():
    inputs = [ScreenInput("a", "t1", "P1", 12_000, 170), ScreenInput("b", "t1", "P1", 12_000, 180),
              ScreenInput("c", "t2", "P1", 12_000, 420), ScreenInput("d", "t2", "P1", 500, 9)]
    pt = PromptTrigger()
    plan = pt.plan_d0_screens(inputs, {}, low_mode=False)
    assert [p.wf for p in plan] == ["c", "d", "b", "a"]
    assert plan[0].mode == "full" and plan[0].escalated     # 420 / 170 = 2.5x, z >> 4
    assert all(p.mode == "sequential" for p in plan[1:])
    low = pt.plan_d0_screens(inputs, {}, low_mode=True)
    assert [p.wf for p in low] == ["c"]
    cached = pt.plan_d0_screens(inputs, {("b", "P1"): verdict("P1", "SAFE")}, low_mode=False)
    assert "b" not in [p.wf for p in cached]


# ------------------------------------------------------------------------------- drift (7.5)

def pickup_rows(seed, days, start_day, p_mult=lambda day, h: 1.0, cells=(0, 1, 2)):
    rng = np.random.default_rng(seed)
    out = []
    for d in range(start_day, start_day + days):
        for h in range(9, 21):
            for c in cells:
                for b, share in ((1, 0.5), (2, 0.3), (3, 0.2)):
                    n = int(rng.poisson(400 / len(cells) * share))
                    p = (0.30 if b == 1 else 0.22 if b == 2 else 0.15) * (1.0 + 0.1 * c)
                    out.append((d, h, c, b, n, int(rng.binomial(n, min(p * p_mult(d, h),
                                                                         0.95)))))
    return out


def drift_ws(rows, now):
    spec = synth.make_spec(dials_per_hour=400)
    return synth.snapshot(spec, pickup=synth.pickup_arrays(rows), now=now)


def test_drift_quiet_in_control_and_alarms_on_a_20_percent_pickup_drop():
    hist = pickup_rows(1, 14, -14)
    ok = pickup_rows(2, 5, 0)
    dd = DriftDetector()
    st, f = dd.update(drift_ws(hist + ok, 5 * 1440), {})
    assert not any(x.kind == "pickup_down" for x in f)
    assert st["pickup_rate"].baseline_kind == "own"     # D4's blocks: the trailing D1 to D3
    drop = pickup_rows(3, 2, 5, p_mult=lambda d, h: 0.8 if (d > 5 or h >= 12) else 1.0)
    alarm = None
    for now in range(5 * 1440 + 10 * 60, 7 * 1440, 60):
        st, f = dd.update(drift_ws(hist + ok + drop, now), st)
        if any(x.kind == "pickup_down" for x in f):
            alarm = now
            break
    assert alarm is not None
    assert (alarm - (5 * 1440 + 12 * 60)) / 60 <= 8      # table: about 3.8 dialling hours


def test_drift_ignores_the_cold_start_of_history():
    """History starts with no caller-ID load, so its first days pick up more than the steady
    state (ADR 0030: found end to end, the CUSUM alarmed on D0 before any change). Judged
    against the trailing days only, an unchanged world raises no drift."""
    cold = lambda d, h: 1.0 + 0.25 * math.exp((d + 14) / -2.0)     # +25% at D-14, gone by D-7
    hist = pickup_rows(11, 14, -14, p_mult=cold)
    ok = pickup_rows(12, 3, 0)
    dd = DriftDetector()
    st, f = dd.update(drift_ws(hist + ok, 3 * 1440), {})
    assert not any(x.kind == "pickup_down" for x in f)
    assert st["pickup_rate"].last_block >= 2 * 24 + 20


def test_drift_upward_arm_is_view_only_and_no_history_uses_own_baseline():
    own = pickup_rows(4, 2, 0)
    dd = DriftDetector()
    st, f = dd.update(drift_ws(own, 2 * 1440), {})
    assert st["pickup_rate"].baseline_kind == "own" and f == []
    up = pickup_rows(5, 2, 2, p_mult=lambda d, h: 1.3)
    st2, f2 = dd.update(drift_ws(own + up, 4 * 1440), st)
    ups = [x for x in f2 if x.kind == "pickup_up"]
    assert ups and ups[0].evidence["view_only"] is True
    assert not any(x.kind == "pickup_down" for x in f2)


def test_engagement_drift_from_the_hourly_flag_series():
    hist = flag_rows(1, 14 * 24, lambda h: 0.01, start=-14 * 24, n=100, engage=0.5)
    rng = np.random.default_rng(9)
    live = [(h, 0, 100, 1, int(rng.binomial(100, 0.4))) for h in range(0, 48) if 9 <= h % 24 < 21]
    dd = DriftDetector()
    spec = synth.make_spec()
    ws = synth.snapshot(spec, flags=synth.flag_series(hist + live), now=2 * 1440)
    st, f = dd.update(ws, {})
    assert any(x.kind == "engagement_down" for x in f)


def test_detect_entry_point_runs_every_detector():
    rows = cancel_like_did_rows(1, 2, 1440 + 10 * 60, 0.333)
    spec = synth.make_spec(dials_per_hour=400)
    ws = synth.snapshot(spec, did=synth.did_arrays(rows), now=1440 + 13 * 60,
                        flags=synth.flag_series(flag_rows(1, 48, lambda h: 0.01, start=-48)))
    f = detect(ws, DetectorState("wf_a"))
    assert any(x.detector == "did" and x.kind == "alarm" for x in f)
    assert any(x.detector == "flag" and x.kind == "reset" for x in f)
    assert isinstance(Config, type)


# ------------------------------------------------------------------------------- ADR 0030 rules

def _did_rows(seed, shares, r=None, r_after=None, days=2):
    rng = np.random.default_rng(seed)
    out = []
    for d in range(days):
        out += synth.simulate_did_day(rng, day=d, tick=TICK, window=(9, 21), dials_per_hour=400,
                                      shares=shares, p=0.25, r=r, r_after=r_after)
    return out


def test_did_detector_leaves_a_persistent_shortfall_to_the_slow_path():
    """A DID that has run at r = 0.6 all along (an incumbent still carrying the load of its old
    share after a spread) is not a sudden flag: no alarm, no suspect. The same DID falling from
    1.0 to 0.36 at D1 10:00 alarms."""
    shares = {0: 1 / 3, 1: 1 / 3, 2: 1 / 3}
    steady = _did_rows(5, shares, r={1: 0.6})
    for now in range(1440 + 11 * 60, 1440 + 21 * 60, 60):
        assert not [f for f in DidDetector().detect(did_ws(steady, now)) if f.scope == "D2"]
    flagged = _did_rows(5, shares, r_after={1: (1440 + 10 * 60, 0.36)})
    kinds = {f.kind for now in range(1440 + 11 * 60, 1440 + 14 * 60, TICK)
             for f in DidDetector().detect(did_ws(flagged, now)) if f.scope == "D2"}
    assert "alarm" in kinds


def test_did_detector_needs_a_comparator_with_enough_dials():
    """A flagged DID carrying 98% of the dials against two 1% DIDs: the comparator has about 16
    dials in 2 hours, too few to call anything (a 5-dial comparator once gave z = -4.6)."""
    rows = _did_rows(6, {0: 0.01, 1: 0.98, 2: 0.01}, r_after={1: (1440 + 10 * 60, 0.36)})
    for now in range(1440 + 10 * 60 + TICK, 1440 + 13 * 60, TICK):
        assert not [f for f in DidDetector().detect(did_ws(rows, now)) if f.kind == "alarm"]


def test_probes_are_never_comparators():
    """Against probes an incumbent always looks degraded (they carry no load), so a workflow
    whose other DIDs are all probing has no comparator: the drift detector covers it."""
    rows = _did_rows(7, {0: 0.5, 1: 0.25, 2: 0.25}, r_after={0: (1440 + 10 * 60, 0.36)})
    probing = {n: DidState("wf_a", n, "probing") for n in ("D2", "D3")}
    probing["D1"] = DidState("wf_a", "D1", "healthy")
    for now in range(1440 + 11 * 60, 1440 + 14 * 60, TICK):
        assert DidDetector().detect(did_ws(rows, now, probing)) == []
