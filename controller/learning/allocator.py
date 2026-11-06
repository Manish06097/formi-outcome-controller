"""Harm-gated probability matching with seeded draws (DESIGN.md 5.4, ADR 0004).

Owner: track T3 (milestone M5).

At a planning point, per cohort k and engagement dimension:

    target_k  = probability matching on the EW draws: share_a proportional to P(a is best in k),
                floors (live undecided arm >= 2%, control >= max(holdout, 2%)),
                ceiling (undeclared arm <= 50%), decay-alarmed arms may not increase,
                removed / futile arms 0, options outside the experiment unchanged
    proposal  = current + lambda_k (target_k - current), one lambda_k per cohort (both dims)
    lambda_k* = the largest lambda_k passing the population gate, this cohort's gate, the
                guardrail forecast and (only when routed to auto-apply) TV <= max_shift,
                by deterministic bisection (12 halvings), cohorts in config order, default
                last; then every lambda_k is scaled by the largest common factor that passes
                the population gate with all cohorts moved together.

Arms whose EW `P(theta < 0) >= 0.8` in a scope are removed in that scope (a risk-reducing part
of the same plan). The minimum-gain rule then drops an exploitation move whose expected gain on
engagement is below 0.25% of the affected cohorts' engagement; exploration (an arm gaining its
first share) and risk-reducing moves (no arm gains share) are exempt.

`harm_stops` runs every step: an arm with EW `P(theta < 0) >= 0.95` whose expected loss
`share x |E theta|` is at least 0.5% of the scope's engagement is moved to the control as a P1.

Everything is a pure function of (snapshot, draws, settings): the same data and seed give the
same plan, which is how a decision replays from the audit store.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from controller.learning.engagement import (CLOSED_EXPERIMENT_STATES, ENGAGEMENT_DIMS, DimArms,
                                            EffectCache, dim_arms, p_best)
from controller.learning.harm import GateContext, GateResult, largest_passing
from controller.settings import Settings, Thresholds
from controller.snapshot import active_population
from controller.types import (DEFAULT_COHORT, P1_ROLLBACK, P3_ROUTINE, Config, DidPosterior,
                              Draws, Experiment, FrozenDict, GuardrailForecast, HarmReport,
                              PatchRequest, WorkflowSnapshot)

OWNER = "learning.allocator"
SHARE_DECIMALS = 4
"""Shares are rounded to 0.01% before the gate sees them: the gate judges exactly what is sent."""

GuardFn = Callable[[Config, Config], GuardrailForecast | None]


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


# ============================================================================== shares

def project_shares(raw: Mapping[str, float], lo: Mapping[str, float],
                   hi: Mapping[str, float]) -> dict[str, float]:
    """Shares `x_i = clip(c * raw_i, lo_i, hi_i)` summing to 1 (water-filling).

    `sum_i clip(c raw_i, lo_i, hi_i)` is continuous and non-decreasing in c, so c is found by
    bisection on log c (deterministic, 200 halvings). Options with zero raw mass get a 1e-12
    floor so they can absorb what the others' ceilings leave. Infeasible bounds are repaired
    first: lower bounds scaled down if they exceed 1; if the ceilings sum below 1, the option
    with the largest ceiling (the control, whose ceiling is 1) takes the remainder."""
    names = list(raw)
    lo_s = {n: max(0.0, lo.get(n, 0.0)) for n in names}
    hi_s = {n: max(lo_s[n], min(1.0, hi.get(n, 1.0))) for n in names}
    tot_lo = sum(lo_s.values())
    if tot_lo > 1.0:
        lo_s = {n: v / tot_lo for n, v in lo_s.items()}
        return lo_s
    if sum(hi_s.values()) < 1.0:
        top = max(names, key=lambda n: (hi_s[n], -names.index(n)))
        hi_s[top] = 1.0 - sum(v for n, v in hi_s.items() if n != top)
    r = {n: max(float(raw[n]), 0.0) + 1e-12 for n in names}

    def total(c: float) -> float:
        return sum(min(max(c * r[n], lo_s[n]), hi_s[n]) for n in names)

    lo_c, hi_c = -60.0, 60.0          # log c
    for _ in range(200):
        mid = 0.5 * (lo_c + hi_c)
        if total(float(np.exp(mid))) < 1.0:
            lo_c = mid
        else:
            hi_c = mid
    c = float(np.exp(hi_c))
    x = {n: min(max(c * r[n], lo_s[n]), hi_s[n]) for n in names}
    s = sum(x.values())
    return {n: v / s for n, v in x.items()}


def _round_shares(w: Mapping[str, float]) -> dict[str, float]:
    r = {o: round(float(v), SHARE_DECIMALS) for o, v in w.items()}
    r = {o: v for o, v in r.items() if v > 0}
    return r or {max(w, key=lambda o: w[o]): 1.0}


def tv(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


# ============================================================================== scope state

@dataclass(frozen=True)
class ArmBounds:
    lo: Mapping[str, float]
    hi: Mapping[str, float]
    frozen: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def _arm_state(ws: WorkflowSnapshot, exp_id: str | None, arm: str, scope: str) -> str:
    for e in ws.experiments:
        if e.exp_id != exp_id:
            continue
        rows = {r.scope: r.state for r in e.arm_rows if r.arm == arm}
        return rows.get(scope, rows.get("population", "live"))
    return "live"


def _decay_alarm(ws: WorkflowSnapshot, dim: str, arm: str, scope: str) -> bool:
    for s in ws.decay:
        if s.dim == dim and s.arm == arm and s.alarm and s.scope in (scope, "population"):
            return True
    return False


def _holdout_share(ws: WorkflowSnapshot, dim: str, scope: str) -> float:
    for h in ws.holdouts:
        if h.dim == dim and h.scope in (scope, "population") and h.state == "active":
            return float(h.share)
    return 0.0


def scope_cells(ws: WorkflowSnapshot, ctx: GateContext, cfg: Config, scope: str
                ) -> tuple[list[int], np.ndarray]:
    """Cells (and lead weights) of a scope: "population" or a cohort of `cfg`."""
    if scope == "population":
        return ctx.cell_ids, ctx.lead
    coh = ctx.cohorts(cfg)
    sel = np.array([c == scope for c in coh])
    return [c for c, s in zip(ctx.cell_ids, sel) if s], ctx.lead[sel]


def scope_effects(ctx: GateContext, ws: WorkflowSnapshot, cfg: Config, scope: str,
                  arms: DimArms) -> dict[str, np.ndarray]:
    cells, w = scope_cells(ws, ctx, cfg, scope)
    return {a: ctx.cache.scope_effect(arms.dim, a, cells, w) for a in arms.arms}


# ============================================================================== the plan

@dataclass(frozen=True, eq=False)
class AllocationPlan:
    proposed: Config
    patches: tuple[PatchRequest, ...]
    kind: str
    """exploration | exploitation | risk_reducing | none | skipped_min_gain | blocked"""
    lambdas: Mapping[str, float]
    common_factor: float
    targets: Mapping[str, Mapping[str, Mapping[str, float]]]
    removed: tuple[tuple[str, str, str], ...]
    """(scope, dim, arm) removals by the P(theta < 0) >= 0.8 rule"""
    harm: HarmReport | None
    expected_gain: float
    evidence: Mapping[str, Any] = field(default_factory=dict)
    new_experiments: tuple[Experiment, ...] = ()
    """Experiments this plan opens (the caller persists them with the decision): the row pins
    each dimension's control, so the incumbent never drifts as allocations move."""


def _engagement_dims(ws: WorkflowSnapshot, t: Thresholds) -> list[DimArms]:
    if ws.spec.objective.primary != t.engagement_model_primary_kpi:
        return []   # 5.3: welcome / voice only where engagement is the primary KPI
    out = []
    for dim in ENGAGEMENT_DIMS:
        a = dim_arms(ws, dim)
        if a.arms and dim in ws.live.allocations.get(DEFAULT_COHORT, {}):
            out.append(a)
    return out


def _bounds(ws: WorkflowSnapshot, arms: DimArms, scope: str, cur: Mapping[str, float],
            removed: set[tuple[str, str, str]], t: Thresholds) -> ArmBounds:
    lo: dict[str, float] = {}
    hi: dict[str, float] = {}
    frozen, gone = [], []
    hold = _holdout_share(ws, arms.dim, scope)
    lo[arms.control] = max(hold, t.arm_floor)
    hi[arms.control] = 1.0
    for a in arms.arms:
        c = float(cur.get(a, 0.0))
        if a not in arms.experiment_arms:
            lo[a] = hi[a] = c     # outside the experiment: untouched
            continue
        state = _arm_state(ws, arms.exp_id, a, scope)
        if state in ("removed", "futile") or (scope, arms.dim, a) in removed \
                or ("population", arms.dim, a) in removed:
            lo[a] = hi[a] = 0.0
            gone.append(a)
            continue
        if state == "winner":
            lo[a], hi[a] = t.arm_floor, 1.0 - lo[arms.control]
        else:
            lo[a], hi[a] = t.arm_floor, t.arm_ceiling
        if _decay_alarm(ws, arms.dim, a, scope):
            hi[a] = min(hi[a], c)
            lo[a] = min(lo[a], c)
            frozen.append(a)
    return ArmBounds(lo=lo, hi=hi, frozen=tuple(frozen), removed=tuple(gone))


def _removals(ws: WorkflowSnapshot, ctx: GateContext, dims: Sequence[DimArms],
              scopes: Sequence[str], t: Thresholds) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for arms in dims:
        for scope in ["population", *scopes]:
            fx = scope_effects(ctx, ws, ws.live, scope, arms)
            for a, eff in fx.items():
                if a in arms.experiment_arms and float(np.mean(eff < 0)) >= t.arm_removal_p:
                    out.add((scope, arms.dim, a))
    return out


def _cohorts(cfg: Config) -> list[str]:
    return list(cfg.cohort_ids()) + [DEFAULT_COHORT]


def build_config(live: Config, props: Mapping[str, Mapping[str, Mapping[str, float]]]) -> Config:
    """Write per-cohort effective distributions with the 5.2 layering: `default` carries its
    own; a cohort gets (or keeps) an override only when it diverges from the default's."""
    cfg = live
    for dim in {d for p in props.values() for d in p}:
        dflt = props.get(DEFAULT_COHORT, {}).get(dim)
        if dflt is not None:
            cfg = cfg.with_allocation(DEFAULT_COHORT, dim, dflt)
        new_default = cfg.dist(DEFAULT_COHORT, dim)
        for k in live.cohort_ids():
            p = props.get(k, {}).get(dim)
            if p is None:
                p = dict(live.dist(k, dim))
            if not live.has_override(k, dim) and _norm(p) == dict(new_default):
                continue
            cfg = cfg.with_allocation(k, dim, p)
    return cfg


def _norm(w: Mapping[str, float]) -> dict[str, float]:
    """The simulator's normalisation (float(w) / float(sum), zeros dropped)."""
    tot = float(sum(w.values()))
    return {o: float(v) / tot for o, v in w.items() if v > 0}


def _proposal(cur: Mapping[str, float], target: Mapping[str, float], lam: float) -> dict[str, float]:
    keys = set(cur) | set(target)
    return _round_shares({o: cur.get(o, 0.0) + lam * (target.get(o, 0.0) - cur.get(o, 0.0))
                          for o in keys})


def open_experiments(ws: WorkflowSnapshot, settings: Settings | Thresholds | None = None
                     ) -> tuple[Experiment, ...]:
    """One experiment per engagement dimension without an open one: every option of the
    workflow is an arm (all explored at once, 5.3) and the control is the incumbent, the
    largest weight in the live default allocation at the moment the experiment opens."""
    t = _thresholds(settings)
    if ws.spec.objective.primary != t.engagement_model_primary_kpi:
        return ()
    out = []
    for dim in ENGAGEMENT_DIMS:
        if any(e.dim == dim and e.state not in CLOSED_EXPERIMENT_STATES for e in ws.experiments):
            continue
        a = dim_arms(ws, dim)
        if not a.arms or dim not in ws.live.allocations.get(DEFAULT_COHORT, {}):
            continue
        out.append(Experiment(
            exp_id=f"{ws.spec.workflow_id}:{dim}:v{ws.live_version}", wf=ws.spec.workflow_id,
            dim=dim, arms=a.arms, control=a.control,
            cohort_set_version=int(getattr(ws.cohorts, "version", 0) or 0), state="live",
            started_version=ws.live_version, started_minute=ws.now))
    return tuple(out)


def plan_allocation_detail(ws: WorkflowSnapshot, draws_ew: Draws | EffectCache, *,
                           settings: Settings | Thresholds | None = None,
                           guard_fn: GuardFn | None = None, did_post: DidPosterior | None = None,
                           max_tv: float | None = None, decision_id: int | None = None,
                           ctx: GateContext | None = None,
                           t_apply: int | None = None) -> AllocationPlan:
    """The full planning computation, with every intermediate result for the decision row.

    Dimensions without an open experiment get one first (`open_experiments`), returned in
    `new_experiments`: the caller must persist them, or the next plan opens them again with
    whatever the incumbent is then.

    Robust to decay between now and the expected apply time `t_apply` (default: now + the mean
    approver latency): when an arm's recent trend is credibly falling, the move must pass the
    gate both on the EW draws and on the draws predicted to t_apply (decay.apply_time_shifts).
    A pessimistic prediction alone is not conservative: it would make cutting a still-good arm
    look harmless. Requiring both makes the move safe whether or not the decline continues."""
    opened = open_experiments(ws, settings)
    if opened:
        ws = replace(ws, experiments=tuple(ws.experiments) + opened)
    t = _thresholds(settings)
    trend_ctx = None
    if ctx is None and not isinstance(draws_ew, EffectCache):
        shifted = apply_time_cache(ws, draws_ew, t_apply, t)
        if shifted.shifts:
            trend_ctx = GateContext(shifted, active_population(ws),
                                    cells=ws.cells, settings=t)
    plan = _plan(ws, draws_ew, settings=settings, guard_fn=guard_fn, did_post=did_post,
                 max_tv=max_tv, decision_id=decision_id, ctx=ctx, trend_ctx=trend_ctx)
    return replace(plan, new_experiments=opened) if opened else plan


def merge_results(now: GateResult, at_apply: GateResult | None) -> GateResult:
    """The 'now' result, failing also where the apply-time scenario fails ("apply_time:...")."""
    if at_apply is None:
        return now
    extra = tuple(f"apply_time:{f}" for f in at_apply.report.failed)
    if not extra:
        return now
    rep = replace(now.report, passes=False, failed=now.report.failed + extra)
    return replace(now, report=rep)


def _plan(ws: WorkflowSnapshot, draws_ew: Draws | EffectCache, *,
          settings: Settings | Thresholds | None, guard_fn: GuardFn | None,
          did_post: DidPosterior | None, max_tv: float | None, decision_id: int | None,
          ctx: GateContext | None, trend_ctx: GateContext | None = None) -> AllocationPlan:
    t = _thresholds(settings)
    live = ws.live
    dims = _engagement_dims(ws, t)
    pop = active_population(ws)
    ctx = ctx or GateContext(draws_ew, pop, cells=ws.cells, settings=t)
    cohorts = _cohorts(live)
    empty = AllocationPlan(proposed=live, patches=(), kind="none", lambdas={}, common_factor=0.0,
                           targets={}, removed=(), harm=None, expected_gain=0.0)
    if not dims:
        return empty
    removed = _removals(ws, ctx, dims, live.cohort_ids(), t)

    # ---------------------------------------------------------------- targets per cohort
    targets: dict[str, dict[str, dict[str, float]]] = {}
    for k in cohorts:
        cells, _ = scope_cells(ws, ctx, live, k)
        if not cells:
            continue
        targets[k] = {}
        for arms in dims:
            cur = dict(live.dist(k, arms.dim))
            b = _bounds(ws, arms, k, cur, removed, t)
            fx = scope_effects(ctx, ws, live, k, arms)
            pb = p_best(fx, arms.control)
            raw = {o: pb.get(o, 0.0) for o in [arms.control, *arms.arms]}
            for o in cur:
                raw.setdefault(o, 0.0)
            targets[k][arms.dim] = project_shares(raw, b.lo, b.hi)

    def props_at(lams: Mapping[str, float]) -> dict[str, dict[str, dict[str, float]]]:
        return {k: {d: _proposal(dict(live.dist(k, d)), tgt, lams.get(k, 0.0))
                    for d, tgt in targets[k].items()} for k in targets}

    def gate(cfg: Config, scopes: Sequence[str] | None) -> GateResult:
        guard = guard_fn(live, cfg) if guard_fn is not None else None
        r = ctx.evaluate(live, cfg, ws.policy, t.alpha_h, t.alpha_d, did_post, guard)
        r2 = trend_ctx.evaluate(live, cfg, ws.policy, t.alpha_h, t.alpha_d, did_post, guard) \
            if trend_ctx is not None else None
        return merge_results(r, r2)

    def ok(r: GateResult, scopes: Sequence[str] | None) -> bool:
        fails = [f.removeprefix("apply_time:") for f in r.report.failed]
        if scopes is None:
            return not fails
        bad = {"population", "guardrail", "did", "did_unmodeled"}
        return not any(f in bad or f.split(":", 1)[-1] in scopes for f in fails)

    # ---------------------------------------------------------------- lambda per cohort
    lams: dict[str, float] = {}
    for k in targets:
        cap = 1.0
        if max_tv is not None:
            for d, tgt in targets[k].items():
                dist = tv(dict(live.dist(k, d)), tgt)
                if dist > 0:
                    cap = min(cap, max(0.0, max_tv - t.max_shift_margin) / dist)
        def passes(lam: float, k: str = k) -> bool:
            cfg = build_config(live, props_at({k: lam}))
            return ok(gate(cfg, [k]), [k])
        lams[k] = largest_passing(passes, t.bisection_iters, hi=cap) if cap > 0 else 0.0

    def all_pass(c: float) -> bool:
        cfg = build_config(live, props_at({k: c * v for k, v in lams.items()}))
        return ok(gate(cfg, None), None)
    common = largest_passing(all_pass, t.bisection_iters) if any(lams.values()) else 0.0
    final = {k: common * v for k, v in lams.items()}
    proposed = build_config(live, props_at(final))
    if live.equivalent(proposed):
        return AllocationPlan(proposed=live, patches=(), kind="blocked" if targets else "none",
                              lambdas=lams, common_factor=common, targets=targets,
                              removed=tuple(sorted(removed)), harm=None, expected_gain=0.0)
    result = gate(proposed, None)

    # ---------------------------------------------------------------- classify, minimum gain
    kind = _classify(live, proposed, dims)
    gain = _expected_gain(result)
    if kind == "exploitation" and gain < t.min_exploit_gain:
        if removed:   # the removals alone are risk-reducing and exempt from the gain rule
            alone = _removal_only(ws, ctx, live, dims, removed, gate, ok, t, decision_id)
            if alone is not None:
                return replace(alone, lambdas=lams, targets=targets, expected_gain=gain)
        return AllocationPlan(proposed=live, patches=(), kind="skipped_min_gain", lambdas=lams,
                              common_factor=common, targets=targets,
                              removed=tuple(sorted(removed)), harm=result.report,
                              expected_gain=gain)
    evidence = {"kind": kind, "lambdas": dict(final), "common_factor": common,
                "expected_gain": gain, "harm": result.report.to_json(),
                "removed": [list(r) for r in sorted(removed)],
                "targets": {k: {d: dict(v) for d, v in m.items()} for k, m in targets.items()}}
    patches = _patches(ws, live, proposed, P3_ROUTINE, evidence, decision_id)
    return AllocationPlan(proposed=proposed, patches=patches, kind=kind, lambdas=lams,
                          common_factor=common, targets=targets, removed=tuple(sorted(removed)),
                          harm=result.report, expected_gain=gain, evidence=evidence)


def _removal_only(ws: WorkflowSnapshot, ctx: GateContext, live: Config,
                  dims: Sequence[DimArms], removed: set[tuple[str, str, str]],
                  gate: Callable[[Config, Sequence[str] | None], GateResult],
                  ok: Callable[[GateResult, Sequence[str] | None], bool], t: Thresholds,
                  decision_id: int | None) -> AllocationPlan | None:
    """Move only the removed arms' share to the control, as far as the gate allows."""
    full: dict[str, dict[str, dict[str, float]]] = {}
    for k in _cohorts(live):
        for arms in dims:
            cur = dict(live.dist(k, arms.dim))
            gone = [a for a in arms.arms if cur.get(a, 0.0) > 0 and (
                (k, arms.dim, a) in removed or ("population", arms.dim, a) in removed)]
            if not gone:
                continue
            tgt = dict(cur)
            for a in gone:
                tgt[arms.control] = tgt.get(arms.control, 0.0) + tgt.pop(a)
            full.setdefault(k, {})[arms.dim] = tgt
    if not full:
        return None
    def cfg_at(f: float) -> Config:
        return build_config(live, {k: {d: _proposal(dict(live.dist(k, d)), tgt, f)
                                       for d, tgt in m.items()} for k, m in full.items()})
    f = largest_passing(lambda x: ok(gate(cfg_at(x), None), None), t.bisection_iters)
    proposed = cfg_at(f)
    if f <= 0 or live.equivalent(proposed):
        return None
    result = gate(proposed, None)
    evidence = {"kind": "risk_reducing", "removal_factor": f, "harm": result.report.to_json(),
                "removed": [list(r) for r in sorted(removed)]}
    return AllocationPlan(proposed=proposed,
                          patches=_patches(ws, live, proposed, P3_ROUTINE, evidence, decision_id),
                          kind="risk_reducing", lambdas={}, common_factor=f, targets={},
                          removed=tuple(sorted(removed)), harm=result.report,
                          expected_gain=_expected_gain(result), evidence=evidence)


def apply_time_cache(ws: WorkflowSnapshot, draws: Draws, t_apply: int | None,
                     settings: Settings | Thresholds | None = None) -> EffectCache:
    """An EffectCache whose arm effects are predicted to the apply time (see decay.py)."""
    from controller.learning.decay import apply_time_shifts, default_apply_time  # no cycle
    ta = default_apply_time(ws) if t_apply is None else t_apply
    shifts = apply_time_shifts(ws, int(draws.params.shape[0]), int(draws.seed), ta, settings)
    return EffectCache(draws, shifts)


def plan_allocations(ws: WorkflowSnapshot, draws_ew: Draws, **kw: Any) -> list[PatchRequest]:
    """The 11.2 entry point: the patches of `plan_allocation_detail` (empty when no move)."""
    return list(plan_allocation_detail(ws, draws_ew, **kw).patches)


def _classify(live: Config, new: Config, dims: Sequence[DimArms]) -> str:
    gained_first = False
    increased = False
    for k in _cohorts(live):
        for arms in dims:
            cur, nxt = live.dist(k, arms.dim), new.dist(k, arms.dim)
            for a in arms.arms:
                c, n = cur.get(a, 0.0), nxt.get(a, 0.0)
                if c <= 1e-12 and n > 0:
                    gained_first = True
                if n > c + 1e-12:
                    increased = True
    if gained_first:
        return "exploration"
    return "exploitation" if increased else "risk_reducing"


def _expected_gain(r: GateResult) -> float:
    """E[relative engagement change over the cells the move touches] (minimum-gain rule)."""
    if len(r.changed_cells) == 0:
        return 0.0
    c = r.changed_cells
    return float(np.mean(r.e_new[:, c].sum(axis=1) / r.e_cur[:, c].sum(axis=1) - 1.0))


def _patches(ws: WorkflowSnapshot, live: Config, new: Config, cls: int,
             evidence: Mapping[str, Any], decision_id: int | None) -> tuple[PatchRequest, ...]:
    out = []
    for k in _cohorts(live):
        for dim in ENGAGEMENT_DIMS:
            a = (live.allocations.get(k) or {}).get(dim)
            b = (new.allocations.get(k) or {}).get(dim)
            if a == b:
                continue
            out.append(PatchRequest(wf=ws.spec.workflow_id, owner=OWNER, path=("alloc", k, dim),
                                    value=FrozenDict(b) if b is not None else None, cls=cls,
                                    p0_rank=None, evidence=FrozenDict(_freeze(evidence)),
                                    decision_id=decision_id))
    return tuple(out)


def _freeze(x: Any) -> Any:
    if isinstance(x, Mapping):
        return FrozenDict({k: _freeze(v) for k, v in x.items()})
    if isinstance(x, (list, tuple)):
        return tuple(_freeze(v) for v in x)
    return x


# ============================================================================== harm stop

def harm_stops(ws: WorkflowSnapshot, draws_ew: Draws | EffectCache, *,
               settings: Settings | Thresholds | None = None, decision_id: int | None = None,
               ctx: GateContext | None = None) -> list[PatchRequest]:
    """P1 risk-reducing moves of clearly bad arms to the control (runs every step, 5.4)."""
    t = _thresholds(settings)
    live = ws.live
    pop = active_population(ws)
    ctx = ctx or GateContext(draws_ew, pop, cells=ws.cells, settings=t)
    props: dict[str, dict[str, dict[str, float]]] = {}
    stopped: list[dict[str, Any]] = []
    for arms in _engagement_dims(ws, t):
        for scope in ["population", *live.cohort_ids(), DEFAULT_COHORT]:
            cells, w = scope_cells(ws, ctx, live, scope)
            if not cells:
                continue
            fx = scope_effects(ctx, ws, live, scope, arms)
            coh = ctx.cohorts(live)
            members = [k for k in _cohorts(live)
                       if scope == "population" or k == scope]
            for a, eff in fx.items():
                share = _scope_share(live, ctx, coh, members, arms.dim, a)
                if share <= t.arm_floor + 1e-12:
                    continue
                p_neg = float(np.mean(eff < 0))
                loss = share * abs(float(eff.mean()))
                if p_neg >= t.harm_stop_p and loss >= t.harm_stop_loss:
                    stopped.append({"scope": scope, "dim": arms.dim, "arm": a, "p_neg": p_neg,
                                    "loss": loss, "share": share})
                    for k in members:
                        cur = props.setdefault(k, {}).setdefault(
                            arms.dim, dict(live.dist(k, arms.dim)))
                        moved = cur.pop(a, 0.0)
                        cur[arms.control] = cur.get(arms.control, 0.0) + moved
    if not stopped:
        return []
    new = build_config(live, {k: {d: _round_shares(v) for d, v in m.items()}
                              for k, m in props.items()})
    return list(_patches(ws, live, new, P1_ROLLBACK, {"kind": "harm_stop", "stopped": stopped},
                         decision_id))


def _scope_share(cfg: Config, ctx: GateContext, coh: Sequence[str], members: Sequence[str],
                 dim: str, arm: str) -> float:
    """Lead-weighted share of `arm` over the scope's cohorts."""
    num = den = 0.0
    for k in members:
        lead = float(ctx.lead[np.array([c == k for c in coh])].sum())
        num += lead * cfg.dist(k, dim).get(arm, 0.0)
        den += lead
    return num / den if den > 0 else 0.0
