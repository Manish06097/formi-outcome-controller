"""Deciding a winner: information-doubling looks, per-scope thresholds, holdout sizing.

Owner: track T3 (milestone M5). Implements DESIGN.md 5.5, 5.6 and ADR 0005.

A hypothesis is (arm, scope); a scope is the population or a cohort that meets the winner-scope
floor (5.2). It is looked at only on an information-doubling schedule: the first look once the
arm has >= 200 connected calls in scope over >= 2 calendar days, later looks once that count has
doubled since the previous look. Every look is returned as a `LookRecord` for the caller to
persist in `winner_look` (the schedule survives a crash; the persisted looks are an input).

At a look, arm a wins scope k when all of W1 to W6 hold:
  W1  STATIONARY P(theta_{a,k} > 0) >= c_k  (0.995 population, 0.998 cohort)
  W2  STATIONARY P(a is best in k) >= 0.95  (control and every live arm of the dimension)
  W3  EW P(theta_{a,k} > 0) >= 0.90 and E_EW[theta] >= 0.5 E_stat[theta]   (recency, F1)
  W4  no decay alarm and the arm's CUSUM statistic below h / 2
  W5  the promotion passes the population and cohort harm gates on EW draws at alpha_H
  W6  the promotion's guardrail forecast predicts no drop
A look can also declare the arm futile: P(theta > ln 1.02) <= 5% on the stationary posterior.

Holdout (5.6): h_k is the smallest of {5, 10, 15, 20}% whose one-day SE of log(winner / holdout)
is <= 0.075, capped at 20%.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import numpy as np

from controller.learning.allocator import (GuardFn, apply_time_cache, build_config,
                                           merge_results)
from controller.learning.engagement import (ENGAGEMENT_DIMS, CLOSED_EXPERIMENT_STATES, DimArms,
                                            EffectCache, dim_arms, p_best)
from controller.learning.harm import GateContext
from controller.settings import Settings, Thresholds
from controller.stats.ratios import norm_sf
from controller.snapshot import active_population, cohort_of_cells, option_index, segment_day
from controller.types import (DEFAULT_COHORT, Config, DidPosterior, Draws, HarmReport,
                              LookRecord, WorkflowSnapshot, Winner)

POPULATION = "population"


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


# ============================================================================== holdout

def holdout_se(n_k: float, e: float, h: float) -> float:
    """One-day SE of log(winner / holdout) with N_k connected a day and a holdout share h."""
    if n_k <= 0 or not 0 < h < 1 or not 0 < e < 1:
        return math.inf
    q = (1.0 - e) / e
    return math.sqrt(q / (n_k * h) + q / (n_k * (1.0 - h)))


def holdout_size(n_k: float, e: float, settings: Settings | Thresholds | None = None) -> float:
    """Smallest h on the grid with SE <= target, else the largest (the 20% cap) (5.6)."""
    t = _thresholds(settings)
    grid = sorted(t.holdout_grid)
    for h in grid:
        if holdout_se(n_k, e, h) <= t.holdout_se_target:
            return h
    return grid[-1]


# ============================================================================== counts in scope

def scope_cell_ids(ws: WorkflowSnapshot, cfg: Config, scope: str) -> set[int]:
    cells = sorted(set(int(c) for c in np.unique(ws.engage.cell)) |
                   set(int(c) for c in ws.cells))
    if scope == POPULATION:
        return set(cells)
    coh = cohort_of_cells(cfg, ws.cells, cells)
    return {c for c, k in zip(cells, coh) if k == scope}


def arm_calls(ws: WorkflowSnapshot, dim: str, arm: str, cells: set[int]) -> tuple[int, int]:
    """(connected calls with `arm` in the scope's cells, distinct calendar days with any)."""
    e = ws.engage
    if len(e) == 0:
        return 0, 0
    code = option_index(ws.spec, dim).get(arm, -1)
    col = e.w if dim == "welcome_message" else e.v
    m = (col == code) & np.isin(e.cell, np.fromiter(cells, dtype=np.int64, count=len(cells)))
    m &= e.connected > 0
    if not m.any():
        return 0, 0
    days = np.unique(segment_day(e.seg[m]))
    return int(e.connected[m].sum()), int(len(days))


def connected_per_day(ws: WorkflowSnapshot, cells: set[int], days: int = 2) -> float:
    """Average connected calls per day in the scope over the last `days` calendar days."""
    e = ws.engage
    if len(e) == 0:
        return 0.0
    day_now = int(ws.now // 1440)
    d = segment_day(e.seg)
    m = (d >= day_now - days) & (d < day_now) & np.isin(
        e.cell, np.fromiter(cells, dtype=np.int64, count=len(cells)))
    if not m.any():
        m = np.isin(e.cell, np.fromiter(cells, dtype=np.int64, count=len(cells)))
        n_days = max(1, len(np.unique(d[m])))
        return float(e.connected[m].sum()) / n_days
    return float(e.connected[m].sum()) / days


def control_rate(ws: WorkflowSnapshot, dim: str, control: str, cells: set[int]) -> float:
    e = ws.engage
    code = option_index(ws.spec, dim).get(control, -1)
    col = e.w if dim == "welcome_message" else e.v
    m = (col == code) & np.isin(e.cell, np.fromiter(cells, dtype=np.int64, count=len(cells)))
    n = float(e.connected[m].sum())
    return float(e.engaged[m].sum()) / n if n > 0 else 0.5


def winner_scope_eligible(ws: WorkflowSnapshot, cohort: str,
                          settings: Settings | Thresholds | None = None) -> bool:
    """5.2 winner-scope floor: at least 15% of daily connected calls (recent 2 days). The T4
    CohortSet may add the projected n = 800 look before the freeze; when the snapshot carries a
    CohortSet that answers, its answer is ANDed in."""
    t = _thresholds(settings)
    tot = connected_per_day(ws, scope_cell_ids(ws, ws.live, POPULATION))
    mine = connected_per_day(ws, scope_cell_ids(ws, ws.live, cohort))
    ok = tot > 0 and mine / tot >= t.winner_scope_min_share
    cs = ws.cohorts
    if ok and cs is not None and hasattr(cs, "winner_scope_eligible"):
        try:
            ok = bool(cs.winner_scope_eligible(cohort, ws))
        except NotImplementedError:
            pass
    return ok


# ============================================================================== tails

def gaussian_tail_above(draws: np.ndarray, x: float) -> float:
    """P(effect > x) from the draws' mean and sd under a normal approximation.

    W1 thresholds a 0.5% (0.2% for cohorts) tail. Counted from 4,000 draws that tail has a
    Monte Carlo sd of about 0.11 points (22% relative), and a posterior whose true tail is 1%
    shows <= 0.5% with non-trivial probability, which inflates false winners. The scope effect
    is Gaussian for the Beta-binomial model (a sum of Gaussian parameters) and very nearly so
    for the ridge-logit (a smooth function of a Laplace posterior), so its moments give the
    tail with about 1% relative error instead (ADR 0005)."""
    sd = float(np.std(draws))
    m = float(np.mean(draws))
    if sd <= 0:
        return 1.0 if m > x else 0.0
    return norm_sf((x - m) / sd)


# ============================================================================== looks

def look_due(n: int, days: int, prior: Sequence[LookRecord],
             settings: Settings | Thresholds | None = None) -> bool:
    """Is a look at this hypothesis due: the first at >= look_first_n calls over
    look_first_days days, later ones once the count has grown look_growth-fold, and none after
    `look_max` looks when that cap is set (5.5)."""
    t = _thresholds(settings)
    if not prior:
        return n >= t.look_first_n and days >= t.look_first_days
    if t.look_max > 0 and len(prior) >= t.look_max:
        return False
    last = max(prior, key=lambda r: r.look_no)
    return n >= t.look_growth * last.n_at_look


@dataclass(frozen=True)
class LookOutcome:
    looks: tuple[LookRecord, ...]
    winners: tuple[Winner, ...]
    futile: tuple[tuple[str, str, str], ...]
    """(exp_id, arm, scope)"""
    detail: tuple[Mapping[str, object], ...]
    """per look: every W condition's value and verdict (the decision row's outcome)"""
    promotions: Mapping[tuple[str, str], Config]
    """(dim, scope) -> the promotion config W5 judged"""


def promotion_config(live: Config, dim: str, winner: str, control: str, scope: str,
                     h: float) -> Config:
    """Scope allocation 1 - h on the winner and h on the control, other arms dropped (5.6).
    A population winner sets `default` and every cohort override of the dimension."""
    w = {winner: 1.0 - h, control: h}
    if scope == POPULATION:
        props = {k: {dim: dict(w)} for k in [*live.cohort_ids(), DEFAULT_COHORT]}
    else:
        props = {scope: {dim: dict(w)}}
    return build_config(live, props)


def evaluate_looks(ws: WorkflowSnapshot, stat: Draws | EffectCache, ew: Draws | EffectCache, *,
                   settings: Settings | Thresholds | None = None,
                   did_post: DidPosterior | None = None, guard_fn: GuardFn | None = None,
                   eligible: Callable[[str], bool] | None = None,
                   t_apply: int | None = None) -> LookOutcome:
    """Looks due now. A promotion is judged at its expected apply time `t_apply` too (default
    now + mean approver latency): when the arm's recent trend is credibly falling, W3 and W5
    must also hold on the EW draws predicted to t_apply, as the grader will judge the promotion
    then (0.1, decay.apply_time_shifts)."""
    t = _thresholds(settings)
    live = ws.live
    pop = active_population(ws)
    cells_cat = ws.cells
    ctx_t = None
    if not isinstance(ew, EffectCache):
        shifted = apply_time_cache(ws, ew, t_apply, t)
        if shifted.shifts:
            ctx_t = GateContext(shifted, pop, cells=cells_cat, settings=t)
    ctx_s = GateContext(stat, pop, cells=cells_cat, settings=t)
    ctx_e = GateContext(ew, pop, cells=cells_cat, settings=t)
    looks: list[LookRecord] = []
    winners: list[Winner] = []
    futile: list[tuple[str, str, str]] = []
    detail: list[dict[str, object]] = []
    promos: dict[tuple[str, str], Config] = {}
    prior_looks = tuple(ws.looks)
    elig = eligible or (lambda k: winner_scope_eligible(ws, k, t))
    scopes = [POPULATION] + [k for k in [*live.cohort_ids(), DEFAULT_COHORT]
                             if k != DEFAULT_COHORT or live.cohorts]
    scopes = [s for s in scopes if s == POPULATION or elig(s)]
    for exp in ws.experiments:
        if exp.dim not in ENGAGEMENT_DIMS or exp.state in CLOSED_EXPERIMENT_STATES:
            continue
        arms = dim_arms(ws, exp.dim)
        for scope in scopes:
            cells = scope_cell_ids(ws, live, scope)
            if not cells:
                continue
            cl, w = _cells_weights(ctx_s, live, scope)
            fx_s = {a: ctx_s.cache.scope_effect(exp.dim, a, cl, w) for a in arms.arms}
            fx_e = {a: ctx_e.cache.scope_effect(exp.dim, a, cl, w) for a in arms.arms}
            fx_t = {a: ctx_t.cache.scope_effect(exp.dim, a, cl, w) for a in arms.arms} \
                if ctx_t is not None else fx_e
            best = p_best({a: fx_s[a] for a in arms.experiment_arms}, arms.control)
            c_k = t.w1_population if scope == POPULATION else t.w1_cohort
            for a in arms.experiment_arms:
                if _arm_closed(exp, a, scope):
                    continue
                n, days = arm_calls(ws, exp.dim, a, cells)
                prior = [r for r in prior_looks
                         if r.exp_id == exp.exp_id and r.arm == a and r.scope == scope]
                if not look_due(n, days, prior, t):
                    continue
                look_no = 1 + max((r.look_no for r in prior), default=0)
                s_eff, e_eff = fx_s[a], fx_e[a]
                w1 = gaussian_tail_above(s_eff, 0.0)
                w2 = float(best.get(a, 0.0))
                w3p = float(np.mean(e_eff > 0))
                m_s, m_e = float(s_eff.mean()), float(e_eff.mean())
                w3 = w3p >= t.w3_ew_p_pos and m_e >= t.w3_ew_ratio * m_s
                e_ta = fx_t[a]
                w3 = w3 and float(np.mean(e_ta > 0)) >= t.w3_ew_p_pos and \
                    float(e_ta.mean()) >= t.w3_ew_ratio * m_s
                w4 = _decay_ok(ws, exp.dim, a, scope, t)
                ok123 = w1 >= c_k and w2 >= t.w2_p_best and w3 and w4
                harm: HarmReport | None = None
                g_ok = False
                if ok123:
                    h = holdout_size(connected_per_day(ws, cells), control_rate(
                        ws, exp.dim, arms.control, cells), t)
                    promo = promotion_config(live, exp.dim, a, arms.control, scope, h)
                    promos[(exp.dim, scope)] = promo
                    guard = guard_fn(live, promo) if guard_fn is not None else None
                    now_r = ctx_e.evaluate(live, promo, ws.policy, t.alpha_h, t.alpha_d,
                                           did_post, guard)
                    at_r = ctx_t.evaluate(live, promo, ws.policy, t.alpha_h, t.alpha_d,
                                          did_post, guard) if ctx_t is not None else None
                    harm = merge_results(now_r, at_r).report
                    g_ok = harm.passes
                passed = bool(ok123 and g_ok)
                looks.append(LookRecord(exp_id=exp.exp_id, arm=a, scope=scope, look_no=look_no,
                                        n_at_look=n, sim_minute=ws.now, w1=w1, w2=w2, w3=w3p,
                                        passed=passed))
                is_futile = gaussian_tail_above(s_eff, t.futility_margin) <= t.futility_p
                if is_futile and not passed:
                    futile.append((exp.exp_id, a, scope))
                detail.append({"exp_id": exp.exp_id, "arm": a, "scope": scope,
                               "look_no": look_no, "n": n, "days": days, "w1": w1, "c_k": c_k,
                               "w2": w2, "w3_p": w3p, "w3_mean_ew": m_e, "w3_mean_stat": m_s,
                               "w3": w3, "w4": w4, "w5_w6": g_ok, "passed": passed,
                               "futile": is_futile})
                if passed and harm is not None:
                    winners.append(Winner(exp_id=exp.exp_id, wf=ws.spec.workflow_id,
                                          dim=exp.dim, arm=a, scope=scope, look_no=look_no,
                                          n=n, p_pos=w1, p_best=w2, ew_p_pos=w3p, harm=harm))
    return LookOutcome(looks=tuple(looks), winners=_one_per_scope(winners), futile=tuple(futile),
                       detail=tuple(detail), promotions=promos)


def declare_winners(ws: WorkflowSnapshot, stat: Draws, ew: Draws, **kw: object) -> list[Winner]:
    """The 11.2 entry point: winners of the looks due now (persist `evaluate_looks().looks`)."""
    return list(evaluate_looks(ws, stat, ew, **kw).winners)  # type: ignore[arg-type]


def _one_per_scope(ws: Sequence[Winner]) -> tuple[Winner, ...]:
    """W2 already makes two winners per (dim, scope) impossible above 0.5; keep the higher
    P(best) deterministically if rounding ever allowed it."""
    best: dict[tuple[str, str], Winner] = {}
    for w in ws:
        k = (w.dim, w.scope)
        if k not in best or (w.p_best, w.arm) > (best[k].p_best, best[k].arm):
            best[k] = w
    return tuple(best[k] for k in sorted(best))


def _cells_weights(ctx: GateContext, cfg: Config, scope: str) -> tuple[list[int], np.ndarray]:
    if scope == POPULATION:
        return ctx.cell_ids, ctx.lead
    coh = ctx.cohorts(cfg)
    sel = np.array([c == scope for c in coh])
    return [c for c, s in zip(ctx.cell_ids, sel) if s], ctx.lead[sel]


def _arm_closed(exp, arm: str, scope: str) -> bool:
    for r in exp.arm_rows:
        if r.arm == arm and r.scope in (scope, POPULATION) and r.state in ("removed", "futile",
                                                                          "winner"):
            return True
    return False


def _decay_ok(ws: WorkflowSnapshot, dim: str, arm: str, scope: str, t: Thresholds) -> bool:
    for s in ws.decay:
        if s.dim == dim and s.arm == arm and s.scope in (scope, POPULATION):
            if s.alarm or s.cusum >= t.w4_cusum_frac * t.decay_cusum_h:
                return False
    return True
