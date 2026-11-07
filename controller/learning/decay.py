"""Decay detection (Page CUSUM per arm and scope) and rollback timed to the apply time.

Owner: track T3 (milestone M5). Implements DESIGN.md 5.7 and ADR 0006.

Monitored: every non-control arm with at least 10% share in some scope (population or a
cohort), and every promoted winner against its holdout. Per completed half-window segment j:

    z_j = (log RR_j - theta_ref) / SE_j     RR_j: arm vs control in segment j, Mantel-Haenszel
                                             across (cohort, prompt) strata of the scope
    S_j = max(0, S_{j-1} - z_j - k)          k = 0.5, alarm at h = 4

theta_ref is the stationary posterior mean of the arm's scope effect when monitoring starts.
`seg_last` makes folding idempotent: a restart or a repeated call never folds a segment twice.
An alarm only freezes increases (allocator) and starts the rollback evaluation; it never rolls
anything back by itself.

Rollback (every step once an alarm is on): the arm's effect is predicted to the expected apply
time t_a from the EW posterior plus a local linear trend over the last 4 segments, and the arm's
share in the scope is moved to the control when

    E[de_rollback(t_a)] > 0   and   P(de_rollback(t_a) < -1%) <= 10%

or, failing the tail condition, the largest share cut that passes it. Predicting to t_a, not
to now, is what keeps a rollback from being graded harmful at onset (UNDERSTANDING 5): we
propose when the variant is expected to be below the control by the time it applies. The trend
enters only when it is credibly falling (slope + z SE < 0, p = apply_time_trend_p), as in
`apply_time_shifts`: a noisy negative slope on a healthy arm must not read as a rollback case.

The cut is then limited by the whole gate of 0.1 at the rollback tail, not by the population
line alone (ADR 0031): per cohort the largest cut whose cohort line passes on the same predicted
draws, then one common factor for the population line. Without this, a population rollback that
passes the population line can fail every small cohort's line and never reach the simulator.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Sequence

import numpy as np

from controller.learning.allocator import _cohorts, _round_shares, build_config, same_dist
from controller.learning.engagement import (ENGAGEMENT_DIMS, CLOSED_EXPERIMENT_STATES,
                                            EffectCache, dim_arms)
from controller.learning.harm import GateContext, largest_passing
from controller.settings import Settings, Thresholds
from controller.snapshot import (active_population, option_index, segment_of, segment_position,
                                 segment_start_minute)
from controller.stats.draws import derive_seed, rng
from controller.stats.ratios import mantel_haenszel, norm_ppf
from controller.stats.sequential import PageCusum
from controller.types import (P1_ROLLBACK, Config, DecayMonitorState, Draws, Finding, FrozenDict,
                              PatchRequest, WorkflowSnapshot)

OWNER = "learning.decay"
POPULATION = "population"


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


def segment_ratio(ws: WorkflowSnapshot, dim: str, arm: str, control: str, cells: Sequence[int],
                  seg: int) -> tuple[float, float] | None:
    """(log RR, SE) of arm vs control in one segment over the scope's cells, or None.

    Stratified by (cohort, prompt). Shares are set per cohort, so that is the stratification that
    removes confounding; within a cohort every cell has the same shares. Stratifying by cell, as
    this did first, threw away calls: at dev volume a segment has a few calls per cell, so most
    cells lacked one of the two sides and were dropped, which doubled the segment's SE and the
    alarm's delay (found end to end in step I4, ADR 0031)."""
    e = ws.engage
    if len(e) == 0:
        return None
    col = e.w if dim == "welcome_message" else e.v
    opts = option_index(ws.spec, dim)
    m = (e.seg == seg) & np.isin(e.cell, np.asarray(list(cells), dtype=np.int64))
    if not m.any():
        return None
    cmap = _cohort_map(ws, e.cell[m])
    groups = {k: i for i, k in enumerate(sorted(set(cmap.values())))}
    coh_code = np.fromiter((groups[cmap[int(c)]] for c in e.cell[m]), dtype=np.int64,
                           count=int(m.sum()))
    keys = np.stack([coh_code, e.prompt[m]], axis=1)
    strata, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = inv.ravel()
    ns = len(strata)
    c = col[m]
    def side(code: int) -> tuple[np.ndarray, np.ndarray]:
        s = c == code
        return (np.bincount(inv[s], weights=e.engaged[m][s], minlength=ns),
                np.bincount(inv[s], weights=e.connected[m][s], minlength=ns))
    a_e, a_n = side(opts.get(arm, -1))
    c_e, c_n = side(opts.get(control, -1))
    r = mantel_haenszel(a_e, a_n, c_e, c_n)
    return None if r is None else (r.log_rr, r.se)


def _cohort_map(ws: WorkflowSnapshot, cells: np.ndarray) -> dict[int, str]:
    """cell -> first-match cohort under the live config, for the cells in `cells`."""
    from controller.snapshot import cohort_of_cells
    uniq = [int(c) for c in np.unique(cells)]
    return dict(zip(uniq, cohort_of_cells(ws.live, ws.cells, uniq)))


class PageDecayMonitor:
    """`types.DecayMonitor` over persisted `DecayMonitorState` rows (decay_monitor table)."""

    def __init__(self, settings: Settings | Thresholds | None = None,
                 states: Iterable[DecayMonitorState] = ()):
        self.t = _thresholds(settings)
        self._s: dict[tuple[str, str, str], DecayMonitorState] = {
            (s.scope, s.dim, s.arm): s for s in states}

    def states(self) -> tuple[DecayMonitorState, ...]:
        return tuple(self._s[k] for k in sorted(self._s))

    # ---------------------------------------------------------------- what is monitored
    def monitored(self, ws: WorkflowSnapshot) -> list[tuple[str, str, str, str]]:
        """(scope, dim, arm, control) with share >= 10% in the scope, plus promoted winners."""
        t = self.t
        out: list[tuple[str, str, str, str]] = []
        live = ws.live
        pop = active_population(ws)
        coh = _cohorts_of(ws, pop)
        lead = np.asarray(pop.leads, dtype=float)
        for exp in ws.experiments:
            if exp.dim not in ENGAGEMENT_DIMS or exp.state in CLOSED_EXPERIMENT_STATES:
                continue
            arms = dim_arms(ws, exp.dim)
            for a in arms.experiment_arms:
                for scope in [POPULATION, *_cohorts(live)]:
                    members = _cohorts(live) if scope == POPULATION else [scope]
                    share = _share(live, coh, lead, members, exp.dim, a)
                    if share >= t.decay_arm_share:
                        out.append((scope, exp.dim, a, arms.control))
        for h in ws.holdouts:
            if h.state == "active" and h.winner_arm:
                out.append((h.scope, h.dim, h.winner_arm, h.control))
        return sorted(set(out))

    # ---------------------------------------------------------------- update
    def update(self, ws: WorkflowSnapshot, segment: int,
               stat: Draws | EffectCache | None = None) -> list[Finding]:
        """Fold every completed segment up to `segment` into each monitored arm's CUSUM."""
        t = self.t
        findings: list[Finding] = []
        cache = stat if isinstance(stat, EffectCache) else (EffectCache(stat) if stat else None)
        pop = active_population(ws)
        coh = _cohorts_of(ws, pop)
        for scope, dim, arm, control in self.monitored(ws):
            cells = _scope_cells(pop, coh, ws.live, scope)
            key = (scope, dim, arm)
            st = self._s.get(key)
            if st is None:
                # the reference must be an estimate, not the prior: start once the arm has the
                # evidence floor of 5.5 in scope (200 connected calls)
                if _arm_n(ws, dim, arm, cells) < t.look_first_n:
                    continue
                ref = self._theta_ref(ws, cache, dim, arm, control, cells, pop, coh, scope,
                                      segment)
                st = DecayMonitorState(wf=ws.spec.workflow_id, scope=scope, dim=dim, arm=arm,
                                       cusum=0.0, seg_last=segment - 1, theta_ref=ref,
                                       alarm=False)
            cus = PageCusum(k=t.decay_cusum_k, h=t.decay_cusum_h, stat=st.cusum,
                            last=st.seg_last, alarm=st.alarm)
            zs = []
            for j in range(st.seg_last + 1, segment + 1):
                r = segment_ratio(ws, dim, arm, control, cells, j)
                if r is None or r[1] <= 0:
                    cus = replace(cus, last=j)
                    continue
                z = (r[0] - st.theta_ref) / r[1]
                zs.append((j, z))
                cus = cus.update(z, j)
            new = replace(st, cusum=float(cus.stat), seg_last=int(cus.last if cus.last is not None
                                                                     else segment),
                          alarm=bool(cus.alarm))
            self._s[key] = new
            if new.alarm and not st.alarm:
                first = zs[0][0] if zs else segment
                findings.append(Finding(
                    wf=ws.spec.workflow_id, detector="decay", kind="alarm", scope=scope,
                    stat=new.cusum, threshold=t.decay_cusum_h,
                    first_evidence_minute=segment_start_minute(first, ws.spec.dialling_window),
                    evidence=FrozenDict({"dim": dim, "arm": arm, "control": control,
                                         "theta_ref": new.theta_ref,
                                         "z": tuple((int(j), float(z)) for j, z in zs)})))
        return findings

    def restart(self, ws: WorkflowSnapshot, scope: str, dim: str, arm: str,
                stat: Draws | EffectCache | None = None) -> DecayMonitorState:
        """Re-take the reference at promotion (5.7: "theta_ref ... or at promotion"): a winner
        is then monitored against its holdout from its effect at promotion, with a fresh CUSUM.
        Call it when the promotion is APPLIED; the caller persists the returned row."""
        cache = stat if isinstance(stat, EffectCache) else (EffectCache(stat) if stat else None)
        pop = active_population(ws)
        coh = _cohorts_of(ws, pop)
        cells = _scope_cells(pop, coh, ws.live, scope)
        control = next((c for s_, d, a, c in self.monitored(ws)
                        if (s_, d, a) == (scope, dim, arm)), dim_arms(ws, dim).control)
        seg = segment_of(ws.now, ws.spec.dialling_window)
        ref = self._theta_ref(ws, cache, dim, arm, control, cells, pop, coh, scope, seg - 1)
        st = DecayMonitorState(wf=ws.spec.workflow_id, scope=scope, dim=dim, arm=arm, cusum=0.0,
                               seg_last=seg - 1, theta_ref=ref, alarm=False)
        self._s[(scope, dim, arm)] = st
        return st

    def _theta_ref(self, ws, cache, dim, arm, control, cells, pop, coh, scope, segment) -> float:
        if cache is not None:
            w = _scope_weights(pop, coh, ws.live, scope)
            return float(cache.scope_effect(dim, arm, cells, w).mean())
        ys, ws_ = [], []
        for j in range(segment - 40, segment + 1):
            r = segment_ratio(ws, dim, arm, control, cells, j)
            if r is not None and r[1] > 0:
                ys.append(r[0])
                ws_.append(1.0 / r[1] ** 2)
        return float(np.average(ys, weights=ws_)) if ys else 0.0

    # ---------------------------------------------------------------- rollback
    def rollback_plan(self, ws: WorkflowSnapshot, ew: Draws | EffectCache, t_apply: int,
                      decision_id: int | None = None) -> list[PatchRequest]:
        t = self.t
        cache = ew if isinstance(ew, EffectCache) else EffectCache(ew)
        pop = active_population(ws)
        coh = _cohorts_of(ws, pop)
        lead = np.asarray(pop.leads, dtype=float)
        live = ws.live
        props: dict[str, dict[str, dict[str, float]]] = {}
        evidence: list[dict[str, object]] = []
        controls = {(s, d, a): c for s, d, a, c in self.monitored(ws)}
        for (scope, dim, arm), st in sorted(self._s.items()):
            if not st.alarm or (scope, dim, arm) not in controls:
                continue
            if st.cusum < t.w4_cusum_frac * t.decay_cusum_h:
                continue    # the evidence faded (W4's h/2): an alarm freezes increases for good,
                            # but a rollback needs the arm to be below its reference still, or a
                            # latched false alarm gets a rollback test at every step from then on
            control = controls[(scope, dim, arm)]
            plan = self._rollback_one(ws, cache, pop, coh, lead, scope, dim, arm, control,
                                      t_apply, getattr(ew, "seed", 0))
            if plan is None:
                continue
            x, info = plan
            evidence.append(info)
            members = _cohorts(live) if scope == POPULATION else [scope]
            for k in members:
                cur = props.setdefault(k, {}).setdefault(dim, dict(live.dist(k, dim)))
                moved = cur.get(arm, 0.0) * x
                cur[arm] = cur.get(arm, 0.0) - moved
                cur[control] = cur.get(control, 0.0) + moved
        if not evidence:
            return []
        new = self._gate_limited(ws, ew, t_apply, props, pop)
        out = []
        for k in _cohorts(live):
            for dim in ENGAGEMENT_DIMS:
                a = (live.allocations.get(k) or {}).get(dim)
                b = (new.allocations.get(k) or {}).get(dim)
                if not same_dist(a, b):
                    out.append(PatchRequest(
                        wf=ws.spec.workflow_id, owner=OWNER, path=("alloc", k, dim),
                        value=FrozenDict(b) if b is not None else None, cls=P1_ROLLBACK,
                        p0_rank=None, evidence=FrozenDict({"kind": "decay_rollback",
                                                           "t_apply": t_apply,
                                                           "rollbacks": tuple(
                                                               FrozenDict(e) for e in evidence)}),
                        decision_id=decision_id))
        return out

    def _gate_limited(self, ws: WorkflowSnapshot, ew: Draws | EffectCache, t_apply: int,
                      props: dict[str, dict[str, dict[str, float]]], pop) -> Config:
        """The rollback config with each cohort's cut limited by its own cohort line and every
        cut by one common factor for the population line, at the rollback tail, on the draws
        predicted to the apply time. This is the gate the composed change will meet (0.1)."""
        t = self.t
        live = ws.live
        cache = ew if isinstance(ew, EffectCache) else EffectCache(
            ew, apply_time_shifts(ws, int(ew.params.shape[0]), int(ew.seed), t_apply, t))
        ctx = GateContext(cache, pop, cells=ws.cells, settings=t)

        def at(lams: dict[str, float]) -> Config:
            out: dict[str, dict[str, dict[str, float]]] = {}
            for k, m in props.items():
                lam = lams.get(k, 0.0)
                out[k] = {}
                for d, tgt in m.items():
                    cur = dict(live.dist(k, d))
                    keys = sorted(set(cur) | set(tgt))
                    out[k][d] = _round_shares({o: cur.get(o, 0.0) + lam * (tgt.get(o, 0.0)
                                                                           - cur.get(o, 0.0))
                                               for o in keys})
            return build_config(live, out)

        def fails(cfg: Config) -> tuple[str, ...]:
            return ctx.evaluate(live, cfg, ws.policy, t.alpha_h_rollback, t.alpha_d, None,
                                None, planning=True).report.failed

        lams = {}
        for k in props:
            line = f"cohort:{k}"
            lams[k] = largest_passing(lambda x, k=k, line=line: line not in fails(at({k: x})),
                                      t.bisection_iters)
        common = largest_passing(lambda c: not fails(at({k: c * v for k, v in lams.items()})),
                                 t.bisection_iters)
        return at({k: common * v for k, v in lams.items()})

    def _rollback_one(self, ws, cache, pop, coh, lead, scope, dim, arm, control, t_apply,
                      seed) -> tuple[float, dict[str, object]] | None:
        t = self.t
        live = ws.live
        cells = _scope_cells(pop, coh, live, scope)
        w = _scope_weights(pop, coh, live, scope)
        theta_now = cache.scope_effect(dim, arm, cells, w)
        b, se_b, pts = self._trend(ws, dim, arm, control, cells)
        window = ws.spec.dialling_window
        delta = segment_position(t_apply, window) - segment_position(ws.now, window)
        g = rng(derive_seed(seed, "decay_trend", scope, dim, arm))
        z = norm_ppf(t.apply_time_trend_p)
        falling = pts >= 3 and b + z * se_b < 0
        slope = np.minimum(0.0, b + se_b * g.standard_normal(theta_now.shape[0])) if falling \
            else np.zeros(theta_now.shape[0])
        theta_ta = theta_now + slope * max(delta, 0.0)
        in_scope = np.array([(scope == POPULATION) or (k == scope) for k in coh])
        share = np.array([live.dist(k, dim).get(arm, 0.0) for k in coh])
        # E_cur = sum lead (1 + share (e^theta - 1)) over the population, relative to control;
        # cutting a fraction x of the arm's share gives de(x) = -x lift / (E_cur)
        denom = float(lead.sum())
        lift = (np.expm1(theta_ta)[:, None] * (lead * share * in_scope)[None, :]).sum(axis=1)
        de1 = -lift / (denom + lift)                     # full rollback of the arm in scope
        mean_ok = float(de1.mean()) > 0
        def passes(x: float) -> bool:
            de = -x * lift / (denom + lift)
            return float(np.mean(de < -t.grader_engagement_line)) <= t.rollback_tolerance
        if not mean_ok:
            return None
        x = largest_passing(passes, t.bisection_iters)
        if x <= 0:
            return None
        info = {"scope": scope, "dim": dim, "arm": arm, "control": control, "cut": x,
                "theta_now_mean": float(theta_now.mean()), "slope": b, "slope_se": se_b,
                "trend_points": pts, "delta_segments": delta,
                "theta_ta_mean": float(theta_ta.mean()), "de_mean": float((x * de1).mean()),
                "p_de_below_1pct": float(np.mean(x * de1 < -t.grader_engagement_line))}
        return x, info

    def _trend(self, ws, dim, arm, control, cells) -> tuple[float, float, int]:
        return arm_trend(ws, dim, arm, control, cells, self.t.rollback_trend_segments)


# ============================================================================== apply time

def default_apply_time(ws: WorkflowSnapshot) -> int:
    """Expected apply time of an approval submitted now: now + the mean approver latency (the
    planner passes the timing calculator's answer when it has one, 6.3)."""
    lo, hi = ws.policy.latency_minutes
    return int(ws.now + (lo + hi) / 2)


def arm_trend(ws: WorkflowSnapshot, dim: str, arm: str, control: str, cells: Sequence[int],
              n_segments: int) -> tuple[float, float, int]:
    """WLS slope (log ratio per segment), its SE and the number of points, over the last
    `n_segments` completed segments with data (searching back at most 4 x n_segments)."""
    seg_now = segment_of(ws.now, ws.spec.dialling_window)
    pts = []
    j = seg_now - 1
    while j >= seg_now - 4 * n_segments and len(pts) < n_segments:
        r = segment_ratio(ws, dim, arm, control, cells, j)
        if r is not None and r[1] > 0:
            pts.append((j, r[0], r[1]))
        j -= 1
    if len(pts) < 2:
        return 0.0, 0.0, len(pts)
    x = np.array([p[0] for p in pts], dtype=float)
    y = np.array([p[1] for p in pts])
    wts = 1.0 / np.array([p[2] for p in pts]) ** 2
    xm = np.average(x, weights=wts)
    sxx = float((wts * (x - xm) ** 2).sum())
    if sxx <= 0:
        return 0.0, 0.0, len(pts)
    b = float((wts * (x - xm) * (y - np.average(y, weights=wts))).sum() / sxx)
    return b, float(np.sqrt(1.0 / sxx)), len(pts)


def apply_time_shifts(ws: WorkflowSnapshot, n_draws: int, seed: int, t_apply: int,
                      settings: Settings | Thresholds | None = None
                      ) -> dict[tuple[str, str], np.ndarray]:
    """Per (dim, arm): draws of the predicted change of the arm's effect from now to t_apply.

    The grader judges a change at its apply time (0.1), so ramps and promotions are gated on
    the arm's effect THEN. With a credibly falling population trend over the last 4 segments
    (slope b, SE s, b + z_p s < 0 with p = `apply_time_trend_p`) the shift is min(0, b + s Z) x (segments to apply);
    otherwise 0. Rising trends are never extrapolated: the prediction can only make the gate
    stricter. Same seed, same shifts."""
    t = _thresholds(settings)
    window = ws.spec.dialling_window
    delta = max(0.0, segment_position(t_apply, window) - segment_position(ws.now, window))
    out: dict[tuple[str, str], np.ndarray] = {}
    if delta <= 0:
        return out
    pop = active_population(ws)
    cells = [int(c) for c in pop.cells]
    z = norm_ppf(t.apply_time_trend_p)
    for exp in ws.experiments:
        if exp.dim not in ENGAGEMENT_DIMS or exp.state in CLOSED_EXPERIMENT_STATES:
            continue
        arms = dim_arms(ws, exp.dim)
        for a in arms.arms:
            b, se, n = arm_trend(ws, exp.dim, a, arms.control, cells, t.rollback_trend_segments)
            if n < 3 or b + z * se >= 0:
                continue
            g = rng(derive_seed(seed, "apply_time_trend", exp.dim, a))
            out[(exp.dim, a)] = np.minimum(0.0, b + se * g.standard_normal(n_draws)) * delta
    return out


# ============================================================================== membership

def _cohorts_of(ws: WorkflowSnapshot, pop) -> list[str]:
    from controller.snapshot import cohort_of_cells
    return cohort_of_cells(ws.live, ws.cells, [int(c) for c in pop.cells]) \
        if ws.cells else [pop.cohort_of_cell.get(int(c), "default")
                                            for c in pop.cells]


def _arm_n(ws: WorkflowSnapshot, dim: str, arm: str, cells: Sequence[int]) -> int:
    e = ws.engage
    if len(e) == 0:
        return 0
    col = e.w if dim == "welcome_message" else e.v
    m = (col == option_index(ws.spec, dim).get(arm, -1)) & np.isin(
        e.cell, np.asarray(list(cells), dtype=np.int64))
    return int(e.connected[m].sum())


def _scope_cells(pop, coh: Sequence[str], cfg: Config, scope: str) -> list[int]:
    return [int(c) for c, k in zip(pop.cells, coh) if scope == POPULATION or k == scope]


def _scope_weights(pop, coh: Sequence[str], cfg: Config, scope: str) -> np.ndarray:
    return np.array([float(n) for n, k in zip(pop.leads, coh) if scope == POPULATION or k == scope])


def _share(cfg: Config, coh: Sequence[str], lead: np.ndarray, members: Sequence[str], dim: str,
           arm: str) -> float:
    num = den = 0.0
    for k in members:
        sel = np.array([c == k for c in coh])
        n = float(lead[sel].sum())
        num += n * cfg.dist(k, dim).get(arm, 0.0)
        den += n
    return num / den if den > 0 else 0.0
