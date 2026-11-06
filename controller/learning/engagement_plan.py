"""The engagement rules as the loop runs them, and as replay re-runs them: one function each.

Owner: integration step I4 (milestone M5). Implements the glue of DESIGN.md 5.3 to 5.7 and 5.10
between a `WorkflowSnapshot` and T3's pure modules: which fits, which seeds, which gate
arguments, and the JSON outcome stored on the decision row. The loop (`loop/engagement.py`) and
the replay recomputers (`learning/replay_engagement.py`) both call these, so a replayed decision
runs exactly the code that made it. ADR 0031.

Seeds: every fit's draws use `derive_seed(run_id, workflow, minute, purpose, kind)`, stored on
its `posterior_snapshot` row; the decision row carries the EW seed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from controller.learning.allocator import (AllocationPlan, GuardFn, apply_time_cache, harm_stops,
                                           merge_results, plan_allocation_detail)
from controller.learning.connects import ConnectModel
from controller.learning.decay import PageDecayMonitor
from controller.learning.did import did_posterior
from controller.learning.engagement import (ENGAGEMENT_DIMS, BetaBinomialModel, EffectCache,
                                            RidgeLogitModel, make_engagement_model)
from controller.learning.guardrail import MixGuardrailMonitor
from controller.learning.harm import GateContext
from controller.learning.winners import LookOutcome, evaluate_looks
from controller.settings import Thresholds
from controller.snapshot import active_population
from controller.stats.draws import derive_seed
from controller.planner.overlay import apply_patch
from controller.types import (P2_PROMOTION, Config, DidPosterior, Draws, FrozenDict,
                              GuardrailForecast, HarmReport, ParamIndex, PatchRequest, Posterior,
                              WorkflowSnapshot, thaw)

FIT_KINDS = ("stationary", "ew")


def model_named(name: str, t: Thresholds) -> BetaBinomialModel | RidgeLogitModel:
    return RidgeLogitModel(t) if name == "ridge_logit" else BetaBinomialModel(t)


def seed_for(ws: WorkflowSnapshot, purpose: str, kind: str) -> int:
    return derive_seed(ws.run_id, ws.spec.workflow_id, int(ws.now), purpose, kind)


@dataclass(frozen=True, eq=False)
class Fits:
    model: str
    post: Mapping[str, Posterior]
    draws: Mapping[str, Draws]
    seeds: Mapping[str, int]

    @property
    def stat(self) -> Draws:
        return self.draws["stationary"]

    @property
    def ew(self) -> Draws:
        return self.draws["ew"]


def fit(ws: WorkflowSnapshot, model_name: str, t: Thresholds, purpose: str,
        kinds: Sequence[str] = FIT_KINDS, seeds: Mapping[str, int] | None = None) -> Fits:
    m = model_named(model_name, t)
    post, draws, used = {}, {}, {}
    for k in kinds:
        p = m.fit(ws, k)  # type: ignore[arg-type]
        s = int(seeds[k]) if seeds and k in seeds else seed_for(ws, purpose, k)
        post[k], draws[k], used[k] = p, m.draws(p, s, t.n_draws), s
    return Fits(model=model_name, post=post, draws=draws, seeds=used)


def draws_from(post_mean: np.ndarray, post_cov: np.ndarray | None, ref: Posterior, seed: int,
               t: Thresholds) -> Draws:
    """Draws of a stored (or refitted) mean and covariance with the index of `ref`."""
    from controller.stats.draws import seeded_draws
    cov = post_cov if post_cov is not None else np.zeros((len(post_mean), len(post_mean)))
    return Draws(kind=ref.kind, seed=int(seed), snapshot_id=0,
                 params=seeded_draws(post_mean, cov, int(seed), t.n_draws), index=ref.index)


# ============================================================================== shared pieces

def guard_fn(ws: WorkflowSnapshot, ew: Draws, t: Thresholds) -> GuardFn:
    """The guardrail forecast (5.10) of an engagement-lever move. Welcome and voice never move
    who connects, so for these moves the forecast is complete."""
    mon = MixGuardrailMonitor(t, draws=ew)
    return lambda cur, new: mon.predict(ws, cur, new)


def is_engagement_workflow(ws: WorkflowSnapshot, t: Thresholds) -> bool:
    return ws.spec.objective.primary == t.engagement_model_primary_kpi and any(
        ws.spec.options.get(d) for d in ENGAGEMENT_DIMS)


def patches_json(reqs: Sequence[PatchRequest]) -> list[dict[str, Any]]:
    return [{"path": list(r.path), "value": thaw(r.value), "class": int(r.cls)}
            for r in reqs]


def report_json(r: HarmReport | None) -> dict[str, Any] | None:
    return None if r is None else r.to_json()


# ============================================================================== allocation (5.4)

def run_allocation(ws: WorkflowSnapshot, fits: Fits, t: Thresholds, t_apply: int) -> AllocationPlan:
    return plan_allocation_detail(ws, fits.ew, settings=t, guard_fn=guard_fn(ws, fits.ew, t),
                                  t_apply=t_apply)


def allocation_outcome(plan: AllocationPlan) -> dict[str, Any]:
    return {"kind": plan.kind, "lambdas": dict(plan.lambdas),
            "common_factor": plan.common_factor, "expected_gain": plan.expected_gain,
            "removed": [list(r) for r in plan.removed],
            "patches": patches_json(plan.patches),
            "new_experiments": [e.exp_id for e in plan.new_experiments]}


def frozen_view(ws: WorkflowSnapshot) -> WorkflowSnapshot:
    """The snapshot the allocator sees inside the end-game freeze (4.2): an arm with no share in
    any cohort is closed (in memory only), so no arm gains its first share."""
    import dataclasses
    from controller.types import DEFAULT_COHORT, ExperimentArm
    from controller.learning.engagement import CLOSED_EXPERIMENT_STATES
    keys = [*ws.live.cohort_ids(), DEFAULT_COHORT]
    exps = []
    for e in ws.experiments:
        if e.state in CLOSED_EXPERIMENT_STATES:
            exps.append(e)
            continue
        rows = list(e.arm_rows)
        for a in e.arms:
            if a != e.control and all(ws.live.dist(k, e.dim).get(a, 0.0) <= 0 for k in keys):
                rows.append(ExperimentArm(exp_id=e.exp_id, arm=a, scope="population",
                                          state="removed", share=0.0))
        exps.append(dataclasses.replace(e, arm_rows=tuple(rows)))
    return dataclasses.replace(ws, experiments=tuple(exps))


# ============================================================================== looks (5.5, 5.6)

def run_looks(ws: WorkflowSnapshot, fits: Fits, t: Thresholds, t_apply: int) -> LookOutcome:
    return evaluate_looks(ws, fits.stat, fits.ew, settings=t,
                          guard_fn=guard_fn(ws, fits.ew, t), t_apply=t_apply)


def looks_outcome(out: LookOutcome) -> dict[str, Any]:
    return {"looks": [{"exp_id": x.exp_id, "arm": x.arm, "scope": x.scope, "look_no": x.look_no,
                       "n": x.n_at_look, "w1": x.w1, "w2": x.w2, "w3": x.w3,
                       "passed": x.passed} for x in out.looks],
            "winners": [{"exp_id": w.exp_id, "dim": w.dim, "arm": w.arm, "scope": w.scope,
                         "look_no": w.look_no, "p_pos": w.p_pos, "p_best": w.p_best}
                        for w in out.winners],
            "futile": [list(f) for f in out.futile]}


# ============================================================================== harm stop, rollback

def run_harm_stops(ws: WorkflowSnapshot, ew: Draws, t: Thresholds) -> list[PatchRequest]:
    return harm_stops(ws, ew, settings=t)


def run_rollbacks(ws: WorkflowSnapshot, ew: Draws, t: Thresholds, t_apply: int
                  ) -> list[PatchRequest]:
    return PageDecayMonitor(t, ws.decay).rollback_plan(ws, ew, t_apply)


# ============================================================================== guardrail (5.10)

def forecast(ws: WorkflowSnapshot, ew: Draws | None, cur: Config, new: Config, t: Thresholds,
             model: ConnectModel | None = None) -> GuardrailForecast:
    """The guardrail forecast of any change, on any workflow: welcome and voice from the effect
    draws, schedule and caller ID from who connects under each config (`ConnectModel`)."""
    model = model or ConnectModel(ws, t)
    units = model.dials if ws.spec.objective.guardrail_kpi == "pickup_rate" else model.connects
    return MixGuardrailMonitor(t, draws=ew, connects_fn=units, pickup_fn=model.pickup).predict(
        ws, cur, new)


@dataclass(frozen=True)
class ForecastFilter:
    """Which pickup-lever requests of one planning point survive the guardrail forecast."""

    keep: tuple[PatchRequest, ...]
    dropped: tuple[tuple[tuple[str, ...], float, float], ...]
    """(path, predicted drop, P(breach)) of each request the forecast refused"""
    forecast: GuardrailForecast | None
    """the forecast of everything kept"""


def filter_by_guardrail(ws: WorkflowSnapshot, reqs: Sequence[PatchRequest], t: Thresholds
                        ) -> ForecastFilter:
    """AUDIT-1 finding 3: the forecast runs before a pickup-lever patch enters the overlay, in
    request order, so a table is kept only if the set kept so far plus it predicts no breach. A
    cohort list changes nothing by itself: it rides with the tables that need it, so it is
    forecast with them and dropped when none of them survives."""
    if not ws.spec.objective.guardrail_kpi or not reqs:
        return ForecastFilter(tuple(reqs), (), None)
    cohorts = [r for r in reqs if r.path[0] == "cohorts"]
    base = ws.live
    for r in cohorts:
        base = apply_patch(base, tuple(r.path), thaw(r.value))
    cfg = base
    model = ConnectModel(ws, t)
    keep: list[PatchRequest] = []
    dropped: list[tuple[tuple[str, ...], float, float]] = []
    last: GuardrailForecast | None = None
    for r in reqs:
        if r.path[0] == "cohorts":
            continue
        trial = apply_patch(cfg, tuple(r.path), thaw(r.value))
        g = forecast(ws, None, ws.live, trial, t, model)
        if forecast_breaches(g, t):
            dropped.append((tuple(r.path), float(g.drop_mean), float(g.p_breach)))
            continue
        cfg, last = trial, g
        keep.append(r)
    if any(r.path[0] == "hour_weights" for r in keep):
        keep = cohorts + keep
    return ForecastFilter(tuple(keep), tuple(dropped), last)


def forecast_breaches(g: GuardrailForecast, t: Thresholds) -> bool:
    """5.10 with the forecast's own resolution: predicted drop above `guardrail_drop_tol`, or
    P(drop > x) above `guardrail_p_breach`."""
    return g.drop_mean > t.guardrail_drop_tol or g.p_breach > t.guardrail_p_breach + t.rounding


# ============================================================================== composed gate (0.1)

@dataclass(frozen=True)
class GateVerdict:
    report: HarmReport
    alpha: float
    notes: tuple[str, ...]
    guard: GuardrailForecast | None = None

    @property
    def passes(self) -> bool:
        return self.report.passes

    def outcome(self) -> dict[str, Any]:
        return {"passes": self.report.passes, "failed": list(self.report.failed),
                "alpha": self.alpha, "notes": list(self.notes)}


def did_post_for(ws: WorkflowSnapshot, seed: int, t: Thresholds) -> DidPosterior:
    return did_posterior(ws, ws.did_state, seed, n=t.n_draws, t=t)


def run_gate(ws: WorkflowSnapshot, ew: Draws | None, live: Config, new: Config, t: Thresholds,
             alpha: float, did_seed: int, t_apply: int | None = None) -> GateVerdict:
    """The harm gate on one composed change (0.1): population and cohort engagement lines on the
    EW draws, the caller-ID line on the DID posterior of the persisted DID states, and the
    guardrail forecast of the whole change.

    Ramps and promotions (the 2.5% tail) must pass both now and on the draws predicted to the
    apply time; rollbacks (the 10% tail) are judged at the apply time only, because that is when
    the grader judges them and a falling arm is the reason for them. Pickup levers do not move
    engagement in the effect model, so the engagement lines judge only welcome and voice."""
    dims = set(live.changed_dims(new))
    notes: list[str] = []
    did_post = did_post_for(ws, did_seed, t) if "caller_id" in dims else None
    guard = forecast(ws, ew, live, new, t)
    if ew is None:
        notes.append("no engagement model for this workflow: engagement lines at multiplier 1")
        report = _gate_without_draws(ws, live, new, t, alpha, did_post, guard)
        return GateVerdict(report=report, alpha=alpha, notes=tuple(notes), guard=guard)
    pop = active_population(ws)
    ctx = GateContext(ew, pop, cells=ws.cells, settings=t)
    now_r = ctx.evaluate(live, new, ws.policy, alpha, t.alpha_d, did_post, guard)
    shifted = apply_time_cache(ws, ew, t_apply, t)
    if not shifted.shifts:
        return GateVerdict(report=now_r.report, alpha=alpha, notes=tuple(notes), guard=guard)
    at_r = GateContext(shifted, pop, cells=ws.cells, settings=t).evaluate(
        live, new, ws.policy, alpha, t.alpha_d, did_post, guard)
    if alpha <= t.alpha_h:
        return GateVerdict(report=merge_results(now_r, at_r).report, alpha=alpha,
                           notes=tuple(notes), guard=guard)
    notes.append("rollback: judged on the draws predicted to the apply time")
    return GateVerdict(report=at_r.report, alpha=alpha, notes=tuple(notes), guard=guard)


def alpha_for(cls: int, t: Thresholds) -> float:
    """The harm tail of a composition by its class (0.1)."""
    return t.alpha_h if cls >= P2_PROMOTION else t.alpha_h_rollback


def _gate_without_draws(ws: WorkflowSnapshot, live: Config, new: Config, t: Thresholds,
                        alpha: float, did_post: DidPosterior | None,
                        guard: GuardrailForecast | None) -> HarmReport:
    """Workflows without engagement experiments: an all-zero effect model (every option has
    multiplier 1), so only the caller-ID and guardrail lines can fail."""
    pop = active_population(ws)
    idx = ParamIndex(names=(), cell_ids=tuple(int(c) for c in pop.cells),
                     effect_maps=FrozenDict(), scale="log")
    zero = Draws(kind="ew", seed=0, snapshot_id=0, params=np.zeros((t.n_draws, 0)), index=idx)
    ctx = GateContext(EffectCache(zero), pop, cells=ws.cells, settings=t)
    return ctx.evaluate(live, new, ws.policy, alpha, t.alpha_d, did_post, guard).report


__all__ = ["Fits", "fit", "seed_for", "model_named", "draws_from", "guard_fn", "alpha_for",
           "is_engagement_workflow", "run_allocation", "allocation_outcome", "run_looks",
           "looks_outcome", "run_harm_stops", "run_rollbacks", "run_gate", "GateVerdict",
           "patches_json", "report_json", "make_engagement_model", "forecast",
           "filter_by_guardrail", "forecast_breaches", "ForecastFilter"]
