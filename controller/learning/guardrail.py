"""Guardrail: mix-adjusted forecast for every proposal and a breach check with attribution.

Owner: track T3 (milestone M5). Implements DESIGN.md 5.10 and ADR 0007.

Definition (assumption A-3): `objective.guardrail.<kpi>_max_drop: x` is an ABSOLUTE drop of x in
the guardrail rate (0.02 = 2 points) over a rolling 2-day window, against the trailing 3-day
baseline. A proposal must predict no drop (point estimate <= 0) and P(drop > x) <= 2.5%.

Forecast (`predict`). The guardrail is the raw rate, so the forecast models who connects:
    E(cfg) = sum_c units_c(cfg) * rate_c(cfg) / sum_c units_c(cfg)
units are connected calls (engagement guardrail) or dials (pickup guardrail) per cell.
- Welcome and voice move engagement only: rate_c(new) = rate_c(cur) * M_new(c) / M_cur(c), with
  M the effect multipliers of the engagement draws, so the forecast is a distribution.
- Schedule and caller-ID changes move who connects: the caller passes `connects_fn(cfg)` (the
  schedule expectation, 5.8, and DID factors, 5.9, owned by track T4) and, for a pickup
  guardrail, `pickup_fn(cfg)`. Without them such a change cannot be forecast and the forecast
  fails closed (p_breach = 1): never a silent pass.
The forecast also returns the mix term `sum_k (c'_k - c_k) e_k` and the within term
`sum_k c'_k (e'_k - e_k)`, so a pickup gain bought with a low-engagement mix is visible. The
decomposition unit k is the CELL, the finest unit we have: 5.10 states it by cohort, but a
cohort decomposition is a coarsening that hides a mix shift inside a cohort (and on a workflow
with no grid cohorts it would hide every mix shift).

Breach check (`check`). Observed window vs baseline, decomposed into mix and within terms by
cell. Breach when the lower 97.5% bound of the drop exceeds x AND the drop is attributable to
a controller change applied inside the window: for welcome / voice changes by the concurrent
arm-vs-control comparison (the arms' share-weighted deficit explains at least half the drop);
for schedule / caller-ID changes when the mix term explains at least half the drop and the mix
shift started within 2 segments of the change. Unattributable drops are a `kpi_drift` finding
(digest item; it changes nothing by itself).
"""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

import numpy as np

from controller.learning.engagement import ENGAGEMENT_DIMS, EffectCache, dim_arms
from controller.learning.harm import GateContext
from controller.settings import Settings, Thresholds
from controller.snapshot import active_population, option_index, segment_of
from controller.stats.ratios import norm_ppf
from controller.types import (Config, Draws, Finding, FrozenDict, GuardrailForecast,
                              WorkflowSnapshot)

UnitsFn = Callable[[Config], Mapping[int, float]]
"""cfg -> expected units (connected calls or dials) per cell over the next day."""
PickupFn = Callable[[Config], Mapping[int, float]]
"""cfg -> expected pickup rate per cell."""
POPULATION_DIMS = frozenset({"schedule", "caller_id", "cohorts"})


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


def _kpi(ws: WorkflowSnapshot) -> tuple[str | None, float]:
    obj = ws.spec.objective
    return obj.guardrail_kpi, float(obj.guardrail_max_drop or 0.0)


class MixGuardrailMonitor:
    """`types.GuardrailMonitor`: `predict(ws, cur, new)` and `check(ws)`."""

    def __init__(self, settings: Settings | Thresholds | None = None,
                 draws: Draws | EffectCache | None = None, connects_fn: UnitsFn | None = None,
                 pickup_fn: PickupFn | None = None):
        self.t = _thresholds(settings)
        self.draws = draws
        self.connects_fn = connects_fn
        self.pickup_fn = pickup_fn

    # ---------------------------------------------------------------- forecast
    def predict(self, ws: WorkflowSnapshot, cur: Config, new: Config) -> GuardrailForecast:
        kpi, x = _kpi(ws)
        if not kpi:
            return GuardrailForecast(kpi="", current=0.0, proposed=0.0, max_drop_abs=0.0,
                                     drop_mean=0.0, p_breach=0.0)
        dims = set(cur.changed_dims(new))
        mix_changes = bool(dims & POPULATION_DIMS)
        if kpi == "pickup_rate":
            return self._predict_pickup(ws, cur, new, x, mix_changes)
        return self._predict_engagement(ws, cur, new, x, dims, mix_changes)

    def _fail_closed(self, kpi: str, x: float, why: str) -> GuardrailForecast:
        return GuardrailForecast(kpi=f"{kpi}:{why}", current=0.0, proposed=0.0, max_drop_abs=x,
                                 drop_mean=0.0, p_breach=1.0)

    def _predict_pickup(self, ws, cur, new, x, mix_changes) -> GuardrailForecast:
        rate = _observed_pickup(ws)
        if not mix_changes:   # welcome, voice, prompt never move pickup
            return GuardrailForecast(kpi="pickup_rate", current=rate, proposed=rate,
                                     max_drop_abs=x, drop_mean=0.0, p_breach=0.0)
        if self.connects_fn is None or self.pickup_fn is None:
            return self._fail_closed("pickup_rate", x, "no_pickup_model")
        u0, u1 = self.connects_fn(cur), self.connects_fn(new)
        p0, p1 = self.pickup_fn(cur), self.pickup_fn(new)
        e0 = _weighted(u0, p0)
        e1 = _weighted(u1, p1)
        drop = e0 - e1
        return GuardrailForecast(kpi="pickup_rate", current=e0, proposed=e1, max_drop_abs=x,
                                 drop_mean=drop, p_breach=1.0 if drop > x else 0.0)

    def _predict_engagement(self, ws, cur, new, x, dims, mix_changes) -> GuardrailForecast:
        cells, rate, units = _cell_rates(ws)
        if not cells:
            return GuardrailForecast(kpi="engagement_rate", current=0.0, proposed=0.0,
                                     max_drop_abs=x, drop_mean=0.0, p_breach=0.0)
        if mix_changes and self.connects_fn is None:
            return self._fail_closed("engagement_rate", x, "no_connect_model")
        u0 = np.array([self.connects_fn(cur).get(c, 0.0) for c in cells]) if mix_changes \
            else units
        u1 = np.array([self.connects_fn(new).get(c, 0.0) for c in cells]) if mix_changes \
            else units
        if dims & set(ENGAGEMENT_DIMS):
            if self.draws is None:
                return self._fail_closed("engagement_rate", x, "no_engagement_draws")
            pop = active_population(ws)
            ctx = GateContext(self.draws, pop, cells=ws.cells,
                              settings=self.t)
            pos = {c: i for i, c in enumerate(ctx.cell_ids)}
            idx = np.array([pos.get(c, -1) for c in cells])
            m0, m1 = ctx.multiplier(cur), ctx.multiplier_from(cur, new)[0]
            ratio = np.ones((m0.shape[0], len(cells)))
            known = idx >= 0
            ratio[:, known] = m1[:, idx[known]] / m0[:, idx[known]]
        else:
            ratio = np.ones((1, len(cells)))
        r1 = np.minimum(rate[None, :] * ratio, 0.95)
        e0 = float((u0 * rate).sum() / max(u0.sum(), 1e-12))
        e1 = (r1 * u1[None, :]).sum(axis=1) / max(u1.sum(), 1e-12)
        drop = e0 - e1
        mix, within = _decompose([str(c) for c in cells], u0, rate, u1, r1.mean(axis=0))
        return GuardrailForecast(kpi="engagement_rate", current=e0, proposed=float(e1.mean()),
                                 max_drop_abs=x, drop_mean=float(drop.mean()),
                                 p_breach=float(np.mean(drop > x)), mix_term=mix,
                                 within_term=within)

    # ---------------------------------------------------------------- breach check
    def check(self, ws: WorkflowSnapshot) -> list[Finding]:
        t = self.t
        kpi, x = _kpi(ws)
        if not kpi:
            return []
        day_now = ws.now // 1440
        win = (day_now - t.guardrail_window_days + 1, day_now)
        base = (win[0] - t.guardrail_baseline_days, win[0] - 1)
        series = _units_rates(ws, kpi)
        if series is None:
            return []
        seg, cell, units, hits = series
        day = seg // 2
        coh = np.array([int(c) for c in cell], dtype=object)   # decomposition unit: the cell
        in_w = (day >= win[0]) & (day <= win[1])
        in_b = (day >= base[0]) & (day <= base[1])
        n_w, n_b = float(units[in_w].sum()), float(units[in_b].sum())
        if n_w <= 0 or n_b <= 0:
            return []
        p_w, p_b = float(hits[in_w].sum()) / n_w, float(hits[in_b].sum()) / n_b
        drop = p_b - p_w
        se = float(np.sqrt(p_w * (1 - p_w) / n_w + p_b * (1 - p_b) / n_b))
        lcb = drop - norm_ppf(t.guardrail_breach_conf) * se
        if lcb <= x:
            return []
        keys = sorted(set(coh.tolist()))
        def shares(mask):
            u = np.array([units[mask & (coh == k)].sum() for k in keys], dtype=float)
            h = np.array([hits[mask & (coh == k)].sum() for k in keys], dtype=float)
            r = np.divide(h, u, out=np.zeros_like(h), where=u > 0)
            return u / max(u.sum(), 1e-12), r
        c0, e0 = shares(in_b)
        c1, e1 = shares(in_w)
        mix = float(((c1 - c0) * e0).sum())
        within = float((c1 * (e1 - e0)).sum())
        win_start = win[0] * 1440
        attributed = None
        for ch in sorted(ws.changes, key=lambda c: -c.minute):
            if ch.minute < win_start or not ch.actor.startswith("controller"):
                continue
            dims = set(ch.dims)
            if dims & set(ENGAGEMENT_DIMS) and kpi == "engagement_rate":
                deficit = _arm_deficit(ws, ch.minute)
                if deficit >= t.guardrail_mix_attrib_frac * drop:
                    attributed = (ch, "concurrent", deficit)
                    break
            if dims & {"schedule", "caller_id"} and -mix >= t.guardrail_mix_attrib_frac * drop:
                start = _mix_shift_start(seg, coh, units, hits, keys, in_b, c0, e0, drop, t,
                                         win_start, ws)
                ch_seg = segment_of(ch.minute, ws.spec.dialling_window)
                if start is not None and abs(start - ch_seg) <= t.guardrail_mix_attrib_segments:
                    attributed = (ch, "mix", -mix)
                    break
        ev = {"kpi": kpi, "window_days": list(win), "baseline_days": list(base),
              "rate_window": p_w, "rate_baseline": p_b, "drop": drop, "lcb": lcb,
              "mix_term": mix, "within_term": within}
        if attributed is not None:
            ch, how, part = attributed
            ev.update({"version": ch.version, "change_minute": ch.minute, "dims": list(ch.dims),
                       "attribution": how, "explained": part})
            kind = "breach"
        else:
            kind = "kpi_drift"
        return [Finding(wf=ws.spec.workflow_id, detector="guardrail", kind=kind, scope="population",
                        stat=lcb, threshold=x, first_evidence_minute=win_start,
                        evidence=FrozenDict(ev))]


# ============================================================================== helpers

def _weighted(units: Mapping[int, float], rate: Mapping[int, float]) -> float:
    num = sum(units[c] * rate.get(c, 0.0) for c in units)
    den = sum(units.values())
    return num / den if den > 0 else 0.0


def _observed_pickup(ws: WorkflowSnapshot) -> float:
    p = ws.pickup
    d = float(p.dials.sum())
    return float(p.connected.sum()) / d if d > 0 else 0.0


def _cell_rates(ws: WorkflowSnapshot, recent_segments: int = 4
                ) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Per cell: engagement rate (shrunk to the workflow rate with 50 calls of prior) and
    recent connected calls, from r_engage."""
    e = ws.engage
    if len(e) == 0:
        return [], np.zeros(0), np.zeros(0)
    seg_now = segment_of(ws.now, ws.spec.dialling_window)
    cells = sorted(int(c) for c in np.unique(e.cell))
    pos = {c: i for i, c in enumerate(cells)}
    idx = np.array([pos[int(c)] for c in e.cell])
    n = np.bincount(idx, weights=e.connected.astype(float), minlength=len(cells))
    g = np.bincount(idx, weights=e.engaged.astype(float), minlength=len(cells))
    p0 = g.sum() / max(n.sum(), 1.0)
    rate = (g + 50.0 * p0) / (n + 50.0)
    recent = e.seg >= seg_now - recent_segments
    u = np.bincount(idx[recent], weights=e.connected[recent].astype(float), minlength=len(cells))
    if u.sum() <= 0:
        u = n
    return cells, rate, u


def _decompose(coh: Sequence[str], u0: np.ndarray, r0: np.ndarray, u1: np.ndarray,
               r1: np.ndarray) -> tuple[float, float]:
    keys = sorted(set(coh), key=str)
    ca, cb, ea, eb = [], [], [], []
    for k in keys:
        m = np.array([c == k for c in coh])
        a, b = float(u0[m].sum()), float(u1[m].sum())
        ca.append(a)
        cb.append(b)
        ea.append(float((u0[m] * r0[m]).sum()) / a if a > 0 else 0.0)
        eb.append(float((u1[m] * r1[m]).sum()) / b if b > 0 else 0.0)
    c0 = np.array(ca) / max(sum(ca), 1e-12)
    c1 = np.array(cb) / max(sum(cb), 1e-12)
    e0, e1 = np.array(ea), np.array(eb)
    return float(((c1 - c0) * e0).sum()), float((c1 * (e1 - e0)).sum())


def _units_rates(ws: WorkflowSnapshot, kpi: str
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """(segment, cell, units, hits) rows of the guardrail KPI."""
    if kpi == "engagement_rate":
        e = ws.engage
        if len(e) == 0:
            return None
        return (np.asarray(e.seg), np.asarray(e.cell), e.connected.astype(float),
                e.engaged.astype(float))
    p = ws.pickup
    if len(p) == 0:
        return None
    lo, hi = ws.spec.dialling_window
    half = lo + (hi - lo) / 2.0
    seg = 2 * np.asarray(p.sim_day) + (np.asarray(p.hour) >= half).astype(np.int64)
    return seg, np.asarray(p.cell), p.dials.astype(float), p.connected.astype(float)


def _arm_deficit(ws: WorkflowSnapshot, since_minute: int) -> float:
    """Share-weighted engagement deficit of non-control arms vs control since a change:
    sum_a share_a (e_control - e_a) over concurrent calls (the concurrent comparator)."""
    e = ws.engage
    seg0 = segment_of(since_minute, ws.spec.dialling_window)
    m = e.seg >= seg0
    if not m.any():
        return 0.0
    tot = float(e.connected[m].sum())
    out = 0.0
    for dim in ENGAGEMENT_DIMS:
        arms = dim_arms(ws, dim)
        col = e.w if dim == "welcome_message" else e.v
        opts = option_index(ws.spec, dim)
        cm = m & (col == opts.get(arms.control, -1))
        n_c = float(e.connected[cm].sum())
        if n_c <= 0:
            continue
        r_c = float(e.engaged[cm].sum()) / n_c
        for a in arms.arms:
            am = m & (col == opts[a])
            n_a = float(e.connected[am].sum())
            if n_a > 0:
                out += (n_a / tot) * (r_c - float(e.engaged[am].sum()) / n_a)
    return out


def _mix_shift_start(seg, coh, units, hits, keys, in_b, c0, e0, drop, t, win_start, ws):
    """First segment in the window whose own mix term explains half the drop."""
    seg_w0 = segment_of(win_start, ws.spec.dialling_window)
    for j in sorted(set(int(s) for s in seg if s >= seg_w0)):
        m = seg == j
        u = np.array([units[m & (coh == k)].sum() for k in keys], dtype=float)
        if u.sum() <= 0:
            continue
        mix_j = float(((u / u.sum() - c0) * e0).sum())
        if -mix_j >= t.guardrail_mix_attrib_frac * drop:
            return j
    return None
