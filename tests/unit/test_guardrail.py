"""Guardrail forecast and breach check (DESIGN.md 5.10)."""
from __future__ import annotations

import pytest

from controller.learning.engagement import BetaBinomialModel
from controller.learning.guardrail import MixGuardrailMonitor
from controller.snapshot import build_snapshot, engage_from_columns
from controller.types import Cell, ChangeRecord
from tests.support.t3_world import base_config, make_policy, make_spec, uniform_world

ENG = make_spec()                                   # engagement primary, pickup guardrail
KYC = make_spec(primary="pickup_rate")              # pickup primary, engagement guardrail
CELLS = {0: Cell(0, "en", "t1", "60+", "low"), 1: Cell(1, "en", "t1", "18-30", "low")}


def _rows(days, conn_by_cell, rate_by_cell):
    rows = {k: [] for k in ("seg", "cell", "w", "v", "connected", "engaged")}
    for d in days:
        for half in (0, 1):
            for c, n in conn_by_cell.items():
                for key, val in (("seg", 2 * d + half), ("cell", c), ("w", 0), ("v", 0),
                                 ("connected", n), ("engaged", round(rate_by_cell[c] * n))):
                    rows[key].append(val)
    return rows


def _merge(*parts):
    return {k: sum((p[k] for p in parts), []) for k in parts[0]}


def test_welcome_change_cannot_move_a_pickup_guardrail():
    w = uniform_world()
    cfg = base_config(ENG)
    ws = w.snapshot(cfg, 0)
    new = cfg.with_allocation("default", "welcome_message", {"W1": 0.9, "W2": 0.1})
    f = MixGuardrailMonitor().predict(ws, cfg, new)
    assert f.kpi == "pickup_rate" and f.drop_mean == 0.0 and f.p_breach == 0.0


def test_engagement_guardrail_forecast_uses_the_effect_draws():
    def mult(dim, o, cell, d):
        return 0.80 if (dim, o) == ("welcome_message", "W2") else 1.0
    w = uniform_world(seed=2, connected_per_day=6000, mult=mult, spec=KYC)
    cfg = base_config(KYC).with_allocation("default", "welcome_message",
                                           {"W1": 0.7, "W2": 0.3})
    for s in range(6):
        w.simulate_segment(cfg, s)
    ws = w.snapshot(cfg, 6)
    m = BetaBinomialModel()
    mon = MixGuardrailMonitor(draws=m.draws(m.fit(ws, "ew"), 3))
    worse = cfg.with_allocation("default", "welcome_message", {"W2": 1.0})
    f = mon.predict(ws, cfg, worse)
    # truth: 0.55 x 0.7 x (1 - 0.8) = 7.7 points; the EW posterior shrinks log 0.8 = -0.22
    # toward 0 (about -0.16 here), so the forecast is a little milder, still a clear breach
    assert f.kpi == "engagement_rate" and 0.045 < f.drop_mean < 0.085
    assert f.p_breach > 0.9
    assert mon.predict(ws, cfg, cfg).drop_mean == pytest.approx(0.0, abs=1e-12)


def test_a_schedule_change_without_a_connect_model_fails_closed():
    ws = build_snapshot(spec=KYC, policy=make_policy(), live=base_config(KYC), live_version=1,
                        now=6 * 1440, engage=engage_from_columns(
                            _rows(range(3), {0: 500, 1: 500}, {0: 0.6, 1: 0.4})), cells=CELLS)
    new = ws.live.with_hour_weights("default", {h: 1.0 for h in range(9, 21)})
    f = MixGuardrailMonitor().predict(ws, ws.live, new)
    assert f.p_breach == 1.0 and "no_connect_model" in f.kpi


def test_a_pickup_gain_through_a_low_engagement_mix_is_caught_before_submit():
    """KYC-like: the schedule change raises pickup by connecting more young leads, whose
    engagement is lower. Nobody's engagement changes, the raw rate falls: the mix term."""
    ws = build_snapshot(spec=KYC, policy=make_policy(), live=base_config(KYC), live_version=1,
                        now=6 * 1440, engage=engage_from_columns(
                            _rows(range(3), {0: 500, 1: 500}, {0: 0.70, 1: 0.40})), cells=CELLS)
    new = ws.live.with_hour_weights("default", {h: 1.0 for h in range(9, 21)})
    def connects(cfg):
        return {0: 500.0, 1: 500.0} if cfg is ws.live else {0: 300.0, 1: 900.0}
    f = MixGuardrailMonitor(connects_fn=connects).predict(ws, ws.live, new)
    assert f.drop_mean > 0.05 and f.mix_term < -0.05 and f.within_term == pytest.approx(0, abs=1e-3)


def test_breach_is_attributed_to_a_schedule_change_through_the_mix_term():
    base = _rows(range(0, 3), {0: 600, 1: 600}, {0: 0.70, 1: 0.40})
    window = _rows(range(3, 5), {0: 300, 1: 900}, {0: 0.70, 1: 0.40})
    ws = build_snapshot(spec=KYC, policy=make_policy(), live=base_config(KYC), live_version=2,
                        now=4 * 1440 + 20 * 60, engage=engage_from_columns(_merge(base, window)),
                        cells=CELLS, changes=[ChangeRecord(minute=3 * 1440 + 9 * 60, version=2,
                                                           dims=("schedule",),
                                                           actor="controller_auto")])
    found = MixGuardrailMonitor().check(ws)
    assert len(found) == 1 and found[0].kind == "breach"
    ev = found[0].evidence
    assert ev["attribution"] == "mix" and ev["version"] == 2 and ev["drop"] > 0.05


def test_an_unattributable_drop_is_kpi_drift_and_changes_nothing():
    base = _rows(range(0, 3), {0: 600, 1: 600}, {0: 0.60, 1: 0.60})
    window = _rows(range(3, 5), {0: 600, 1: 600}, {0: 0.50, 1: 0.50})
    ws = build_snapshot(spec=KYC, policy=make_policy(), live=base_config(KYC), live_version=1,
                        now=4 * 1440 + 20 * 60, engage=engage_from_columns(_merge(base, window)),
                        cells=CELLS)
    found = MixGuardrailMonitor().check(ws)
    assert [f.kind for f in found] == ["kpi_drift"]


def test_no_finding_when_the_drop_is_within_the_limit():
    base = _rows(range(0, 3), {0: 600, 1: 600}, {0: 0.60, 1: 0.60})
    window = _rows(range(3, 5), {0: 600, 1: 600}, {0: 0.59, 1: 0.59})
    ws = build_snapshot(spec=KYC, policy=make_policy(), live=base_config(KYC), live_version=1,
                        now=4 * 1440 + 20 * 60, engage=engage_from_columns(_merge(base, window)),
                        cells=CELLS)
    assert MixGuardrailMonitor().check(ws) == []
