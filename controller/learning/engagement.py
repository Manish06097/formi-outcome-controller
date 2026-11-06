"""Engagement effect models: per-cohort Beta-binomial (ships first) and factorial ridge-logit.

Owner: track T3 (milestone M5). Implements DESIGN.md 5.3, 5.4 (priors), 17.2 and ADR 0003.

Both classes implement `types.EngagementModel` (`fit(ws, kind) -> Posterior`,
`draws(post, seed, n) -> Draws`) and are selected by `CTL_ENGAGEMENT_MODEL`
(`make_engagement_model`). Every consumer (harm gate, allocator, winners, decay) reads effects
only through `Draws` / `EffectCache`, so the two models are interchangeable.

What an "effect" is, in both: for dimension `dim` and option `arm`, the log ratio of expected
engagement with `arm` vs with the dimension's control, per cell. Effects combine
multiplicatively across welcome and voice, exactly as the simulator does (world.py:288-305).

1. `BetaBinomialModel` (17.2, the default). Per dimension, per cohort k and non-control arm a,
   the observation is the Mantel-Haenszel log rate ratio of a vs the control, stratified by
   (segment, prompt) so day-level shocks and supervisor prompt edits do not leak into arm effects
   under adaptive allocation, with its delta-method variance and the covariance arms get from
   the shared control (stats/ratios.py). With one stratum this is the log ratio of two
   Beta-binomial rates. The effect of a in cohort k is `theta_a + u_{a,k}`:
     theta_a ~ N(0, tau^2)                 the population effect (lead-weighted), tau = 0.06
     u_{a,.} ~ N(0, tau_u^2 P)             cohort deviations, tau_u^2 = 3 tau^2, P projects out
                                           the lead-weight direction, so sum_k w_k u_{a,k} = 0
   so the population prior sd is exactly tau = 0.06 and a cohort's prior sd is about
   sqrt(tau^2 + tau_u^2) = 0.12 (the design's "theta plus three deltas"), which is what the
   5.4 cost table and the 5.5 boundaries assume. The posterior is Gaussian in closed form
   (Kalman form, so the degenerate prior is fine). The EW fit raises each segment's likelihood
   to the power 2^(-age/2) by scaling counts (a power likelihood). The EW prior uses
   tau_H = max(tau_floor, tau_EB), the empirical-Bayes dispersion, so more dispersion makes the
   harm gate stricter, never looser; the STATIONARY fit keeps tau_floor, so the winner rule's
   boundaries (5.5) are the ones computed.

2. `RidgeLogitModel` (5.3). `logit P(engaged) = beta[cell] + gamma[segment] + pi[prompt] +
   theta_W[a] + sum_f delta_W[a, f, level_f] + theta_V[b] + sum_f delta_V[b, f, level_f]` with
   f over lang, age_band, city_tier; theta, delta ~ N(0, tau_E^2), tau_E = 0.13 (logit),
   intercepts ~ N(., 2^2); ridge IRLS on aggregated counts, Laplace posterior (stats/glm.py).

Arms. Data come only from calls whose recorded variant shows the option ran (r_engage w, v are
the variants actually dialled). The control of a dimension is the experiment's control, else
the incumbent: the option with the largest weight in the live default allocation.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from controller.settings import Settings, Thresholds
from controller.snapshot import (LearningSnapshot, active_population, cohort_of_cells,
                                 ew_weights, option_index, segment_of)
from controller.stats.draws import seeded_draws
from controller.stats.glm import SparseDesign, fit_ridge_logit
from controller.stats.ratios import mantel_haenszel, shared_control_cov
from controller.types import (DEFAULT_COHORT, Draws, FitKind, FrozenDict, ParamIndex, Posterior,
                              WorkflowSnapshot)

ENGAGEMENT_DIMS: tuple[str, ...] = ("welcome_message", "voice")
"""Dimensions the engagement model learns (5.3). Prompt is a nuisance intercept / stratum."""
FEATURES: tuple[tuple[str, str], ...] = (("lang", "lang"), ("age_band", "age"),
                                         ("city_tier", "tier"))
"""(feature name, Cell attribute) of the interaction terms: all three, not only the grid's."""
CLOSED_EXPERIMENT_STATES = frozenset({"finished", "interrupted", "cancelled", "closed"})


# ============================================================================== arms

@dataclass(frozen=True)
class DimArms:
    """The control and the modelled non-control options of one dimension."""

    dim: str
    control: str
    arms: tuple[str, ...]
    """Every non-control option of the workflow, in option order (all are modelled)."""
    experiment_arms: tuple[str, ...]
    """The arms the live experiment allocates to (equal to `arms` without an experiment)."""
    exp_id: str | None = None


def dim_arms(ws: WorkflowSnapshot, dim: str) -> DimArms:
    options = tuple(ws.spec.options.get(dim, ()))
    exp = next((e for e in ws.experiments if e.dim == dim
                and e.state not in CLOSED_EXPERIMENT_STATES), None)
    if exp is not None and exp.control in options:
        control = exp.control
        exp_arms = tuple(a for a in exp.arms if a != control and a in options)
    else:
        dist = ws.live.dist(DEFAULT_COHORT, dim) if dim in ws.live.allocations.get(
            DEFAULT_COHORT, {}) else {}
        control = max(options, key=lambda o: (dist.get(o, 0.0), -options.index(o))) \
            if options else ""
        exp_arms = tuple(o for o in options if o != control)
    arms = tuple(o for o in options if o != control)
    return DimArms(dim=dim, control=control, arms=arms, experiment_arms=exp_arms,
                   exp_id=exp.exp_id if exp is not None else None)


def model_cells(ws: WorkflowSnapshot) -> list[int]:
    """Every cell the model gives an effect to: catalogue, rollups and active leads."""
    cells: set[int] = set(int(c) for c in getattr(ws, "cells", {}) or {})
    cells.update(int(c) for c in np.unique(ws.engage.cell))
    pop = active_population(ws)
    cells.update(int(c) for c in pop.cells)
    return sorted(cells)


def _cohort_order(ws: WorkflowSnapshot, cohorts_of_cells: Sequence[str]) -> list[str]:
    """Cohorts that own at least one model cell, in config order, default last."""
    present = set(cohorts_of_cells)
    order = [c for c in ws.live.cohort_ids() if c in present]
    if DEFAULT_COHORT in present:
        order.append(DEFAULT_COHORT)
    return order


def _hash_inputs(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, np.ndarray):
            h.update(np.ascontiguousarray(p).tobytes())
            h.update(str(p.dtype).encode())
        else:
            h.update(repr(p).encode())
        h.update(b"\x1f")
    return h.hexdigest()


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


def _segment_weights(ws: WorkflowSnapshot, kind: FitKind, t: Thresholds) -> np.ndarray:
    if kind == "stationary":
        return np.ones(len(ws.engage))
    seg_now = segment_of(ws.now, ws.spec.dialling_window)
    return ew_weights(ws.engage.seg, seg_now, t.ew_half_life_segments)


# ============================================================================== Beta-binomial

@dataclass(frozen=True)
class ArmObservation:
    """One cohort-level observation of the Beta-binomial model."""

    dim: str
    arm: str
    cohort: str
    log_rr: float
    var: float
    n_arm: float
    n_ctrl: float


class BetaBinomialModel:
    """Per-cohort Beta-binomial log-ratio model with partial pooling (see module docstring)."""

    name = "beta_binomial"

    def __init__(self, settings: Settings | Thresholds | None = None, n_features: int = 3):
        self.t = _thresholds(settings)
        self.n_features = n_features
        """Cohort deviation variance = n_features x tau^2 (one delta per feature, 5.4)."""

    # ---------------------------------------------------------------- observations
    def _observations(self, ws: WorkflowSnapshot, w_rows: np.ndarray, cells: list[int],
                      coh_of: dict[int, str], arms: DimArms
                      ) -> tuple[list[ArmObservation], dict[tuple[str, str, str], float]]:
        """Per cohort: MH log ratios of each arm vs control, and the shared-control covariances."""
        e = ws.engage
        opts = option_index(ws.spec, arms.dim)
        code = e.w if arms.dim == "welcome_message" else e.v
        ctrl = opts.get(arms.control, -1)
        obs: list[ArmObservation] = []
        cov: dict[tuple[str, str, str], float] = {}
        if len(e) == 0:
            return obs, cov
        coh = np.array([coh_of.get(int(c), DEFAULT_COHORT) for c in e.cell], dtype=object)
        for k in sorted(set(coh.tolist()), key=str):
            in_k = coh == k
            strata, inv = np.unique(np.stack([e.seg[in_k], e.prompt[in_k]], axis=1), axis=0,
                                    return_inverse=True)
            inv = inv.ravel()
            ns = len(strata)
            wk = w_rows[in_k]
            conn = e.connected[in_k] * wk
            eng = e.engaged[in_k] * wk
            ck = code[in_k]
            def per_stratum(c: int) -> tuple[np.ndarray, np.ndarray]:
                m = ck == c
                return (np.bincount(inv[m], weights=eng[m], minlength=ns),
                        np.bincount(inv[m], weights=conn[m], minlength=ns))
            c_a, c_n = per_stratum(ctrl)
            est: dict[str, Any] = {}
            n1s: dict[str, np.ndarray] = {}
            for a in arms.arms:
                a_a, a_n = per_stratum(opts[a])
                r = mantel_haenszel(a_a, a_n, c_a, c_n)
                if r is None:
                    continue
                est[a], n1s[a] = r, a_n
                obs.append(ArmObservation(arms.dim, a, k, r.log_rr, r.var, r.n_arm, r.n_ctrl))
            names = list(est)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    cov[(k, a, b)] = shared_control_cov(n1s[a], n1s[b], c_a, c_n,
                                                       est[a].c_std, est[b].c_std)
        return obs, cov

    def _tau_eb(self, obs: Sequence[ArmObservation]) -> float:
        """Empirical-Bayes dispersion of population-level arm effects (method of moments over
        informative arms: those whose pooled variance is below tau_floor^2). 0 when fewer
        than two arms are informative."""
        tau0 = self.t.tau_h_floor
        pooled: dict[tuple[str, str], tuple[float, float]] = {}
        for o in obs:
            s, p = pooled.get((o.dim, o.arm), (0.0, 0.0))
            pooled[(o.dim, o.arm)] = (s + o.log_rr / o.var, p + 1.0 / o.var)
        vals = [(s / p, 1.0 / p) for s, p in pooled.values() if 1.0 / p < tau0 * tau0]
        if len(vals) < 2:
            return 0.0
        m2 = float(np.mean([th * th - v for th, v in vals]))
        return math.sqrt(m2) if m2 > 0 else 0.0

    # ---------------------------------------------------------------- posterior
    def _dim_posterior(self, arms: DimArms, cohorts: list[str], w: np.ndarray, tau: float,
                       obs: list[ArmObservation], cov: Mapping[tuple[str, str, str], float]
                       ) -> tuple[list[str], np.ndarray, np.ndarray]:
        A, K = len(arms.arms), len(cohorts)
        names = [f"{arms.dim}|{a}|population" for a in arms.arms]
        names += [f"{arms.dim}|{a}|{k}" for a in arms.arms for k in cohorts]
        n_par = A + A * K
        prior = np.zeros((n_par, n_par))
        prior[:A, :A] = np.eye(A) * tau * tau
        if K > 1:
            ww = w / float(w @ w) if float(w @ w) > 0 else np.full(K, 1.0 / K)
            P = np.eye(K) - np.outer(w, ww)
            P = 0.5 * (P + P.T)
            tau_u2 = self.n_features * tau * tau
            for i in range(A):
                s = A + i * K
                prior[s:s + K, s:s + K] = tau_u2 * P
        if not obs:
            return names, np.zeros(n_par), prior
        arm_pos = {a: i for i, a in enumerate(arms.arms)}
        coh_pos = {k: i for i, k in enumerate(cohorts)}
        X = np.zeros((len(obs), n_par))
        y = np.zeros(len(obs))
        R = np.zeros((len(obs), len(obs)))
        for r, o in enumerate(obs):
            i = arm_pos[o.arm]
            X[r, i] = 1.0
            if K > 1:
                X[r, A + i * K + coh_pos[o.cohort]] = 1.0
            y[r] = o.log_rr
            R[r, r] = o.var
        for r1, o1 in enumerate(obs):
            for r2 in range(r1 + 1, len(obs)):
                o2 = obs[r2]
                if o1.cohort != o2.cohort:
                    continue
                c = cov.get((o1.cohort, o1.arm, o2.arm), cov.get((o1.cohort, o2.arm, o1.arm), 0.0))
                R[r1, r2] = R[r2, r1] = c
        S = X @ prior @ X.T + R
        G = np.linalg.solve(S, X @ prior).T          # prior X' S^-1
        mean = G @ y
        post = prior - G @ X @ prior
        return names, mean, 0.5 * (post + post.T)

    def fit(self, ws: WorkflowSnapshot, kind: FitKind) -> Posterior:
        t = self.t
        cells = model_cells(ws)
        cmap_list = cohort_of_cells(ws.live, getattr(ws, "cells", {}) or {}, cells)
        coh_of = dict(zip(cells, cmap_list))
        cohorts = _cohort_order(ws, cmap_list)
        pop = active_population(ws)
        lead_w = {k: 0.0 for k in cohorts}
        for c, n in zip(pop.cells, pop.leads):
            k = coh_of.get(int(c), DEFAULT_COHORT)
            if k in lead_w:
                lead_w[k] += float(n)
        w = np.array([lead_w[k] for k in cohorts], dtype=float)
        if w.sum() <= 0:
            w = np.ones(len(cohorts))
        w = w / w.sum()

        rows_w = _segment_weights(ws, kind, t)
        stat_obs: list[ArmObservation] = []
        per_dim: list[tuple[DimArms, list[ArmObservation], dict]] = []
        for dim in ENGAGEMENT_DIMS:
            arms = dim_arms(ws, dim)
            if not arms.arms:
                continue
            obs, cov = self._observations(ws, rows_w, cells, coh_of, arms)
            if kind == "stationary":
                stat_obs += obs
            else:
                stat_obs += self._observations(ws, np.ones(len(ws.engage)), cells, coh_of,
                                               arms)[0]
            per_dim.append((arms, obs, cov))
        tau_eb = self._tau_eb(stat_obs)
        tau = t.tau_h_floor if kind == "stationary" else max(t.tau_h_floor, tau_eb)

        names: list[str] = []
        means: list[np.ndarray] = []
        covs: list[np.ndarray] = []
        arm_info: dict[str, Any] = {}
        for arms, obs, cov in per_dim:
            n, m, c = self._dim_posterior(arms, cohorts, w, tau, obs, cov)
            names += n
            means.append(m)
            covs.append(c)
            arm_info[arms.dim] = {"control": arms.control, "arms": list(arms.arms)}
        n_par = len(names)
        mean = np.concatenate(means) if means else np.zeros(0)
        cov = np.zeros((n_par, n_par))
        o = 0
        for c in covs:
            cov[o:o + c.shape[0], o:o + c.shape[0]] = c
            o += c.shape[0]

        col = {nm: i for i, nm in enumerate(names)}
        emaps: dict[tuple[str, str], np.ndarray] = {}
        for arms, _, _ in per_dim:
            for a in arms.arms:
                E = np.zeros((len(cells), n_par))
                E[:, col[f"{arms.dim}|{a}|population"]] = 1.0
                if len(cohorts) > 1:
                    for r, c in enumerate(cells):
                        E[r, col[f"{arms.dim}|{a}|{coh_of[c]}"]] = 1.0
                emaps[(arms.dim, a)] = E
        index = ParamIndex(names=tuple(names), cell_ids=tuple(cells), effect_maps=FrozenDict(emaps),
                           scale="log")
        beta = self._beta_params(ws, rows_w, coh_of)
        summary = FrozenDict({
            "model": self.name, "kind": kind, "tau": tau, "tau_eb": tau_eb,
            "tau_u": math.sqrt(self.n_features) * tau, "cohorts": tuple(cohorts),
            "cohort_weights": tuple(float(x) for x in w), "arms": FrozenDict(arm_info),
            "observations": tuple(FrozenDict({"dim": o.dim, "arm": o.arm, "cohort": o.cohort,
                                              "log_rr": o.log_rr, "var": o.var, "n": o.n_arm})
                                  for _, obs, _ in per_dim for o in obs)})
        e = ws.engage
        return Posterior(wf=ws.spec.workflow_id, kind=kind, mean=mean, cov=cov, index=index,
                         inputs_hash=_hash_inputs(self.name, kind, e.seg, e.cell, e.prompt, e.w,
                                                  e.v, e.connected, e.engaged, rows_w,
                                                  tuple(cells), tuple(cmap_list), tau,
                                                  tuple(names)),
                         summary=summary, extra=FrozenDict({"beta": beta}))

    def _beta_params(self, ws: WorkflowSnapshot, rows_w: np.ndarray,
                     coh_of: Mapping[int, str]) -> FrozenDict:
        """Pooled Beta(0.5 + engaged, 0.5 + not engaged) per (dim, option, cohort), weighted
        like the fit: the exact Beta-binomial parameters, for the supervisor view and audit."""
        e = ws.engage
        out: dict[str, Any] = {}
        if len(e) == 0:
            return FrozenDict()
        coh = np.array([coh_of.get(int(c), DEFAULT_COHORT) for c in e.cell], dtype=object)
        for dim in ENGAGEMENT_DIMS:
            code = e.w if dim == "welcome_message" else e.v
            for o, i in option_index(ws.spec, dim).items():
                for k in sorted(set(coh.tolist()), key=str):
                    m = (code == i) & (coh == k)
                    if not m.any():
                        continue
                    n = float((e.connected[m] * rows_w[m]).sum())
                    g = float((e.engaged[m] * rows_w[m]).sum())
                    out[f"{dim}|{o}|{k}"] = (0.5 + g, 0.5 + n - g)
        return FrozenDict(out)

    def draws(self, post: Posterior, seed: int, n: int | None = None) -> Draws:
        return _draws(post, seed, n if n is not None else self.t.n_draws)


# ============================================================================== ridge-logit

class RidgeLogitModel:
    """Factorial ridge-logit with arm x feature interactions and a Laplace posterior (5.3)."""

    name = "ridge_logit"

    def __init__(self, settings: Settings | Thresholds | None = None):
        self.t = _thresholds(settings)

    def fit(self, ws: WorkflowSnapshot, kind: FitKind) -> Posterior:
        t = self.t
        e = ws.engage
        cells = model_cells(ws)
        cell_pos = {c: i for i, c in enumerate(cells)}
        catalogue = getattr(ws, "cells", {}) or {}
        levels: dict[str, list[str]] = {}
        for fname, attr in FEATURES:
            levels[fname] = sorted({getattr(catalogue[c], attr) for c in cells if c in catalogue})

        rows_w_raw = _segment_weights(ws, kind, t)
        if len(e):
            keys = np.stack([e.cell, e.seg, e.prompt, e.w, e.v], axis=1)
            uk, inv = np.unique(keys, axis=0, return_inverse=True)
            inv = inv.ravel()
            trials = np.bincount(inv, weights=e.connected.astype(float), minlength=len(uk))
            succ = np.bincount(inv, weights=e.engaged.astype(float), minlength=len(uk))
            wsum = np.bincount(inv, weights=rows_w_raw * e.connected, minlength=len(uk))
            roww = np.where(trials > 0, wsum / np.maximum(trials, 1e-300), 0.0)
            keep = trials > 0
            uk, trials, succ, roww = uk[keep], trials[keep], succ[keep], roww[keep]
        else:
            uk = np.zeros((0, 5), dtype=np.int64)
            trials = succ = roww = np.zeros(0)
        segs = sorted(int(s) for s in np.unique(uk[:, 1])) if len(uk) else []
        prompts = sorted(int(p) for p in np.unique(uk[:, 2])) if len(uk) else []

        names: list[str] = [f"beta|{c}" for c in cells]
        names += [f"gamma|{s}" for s in segs]
        names += [f"pi|{p}" for p in prompts]
        dims = [dim_arms(ws, d) for d in ENGAGEMENT_DIMS]
        dims = [d for d in dims if d.arms]
        for d in dims:
            for a in d.arms:
                names.append(f"theta|{d.dim}|{a}")
                for fname, _ in FEATURES:
                    names += [f"delta|{d.dim}|{a}|{fname}|{lv}" for lv in levels[fname]]
        col = {nm: i for i, nm in enumerate(names)}
        n_par = len(names)

        # design rows: cell, segment, prompt, then per dim the arm main effect and interactions
        width = 3 + sum(1 + len(FEATURES) for _ in dims)
        cols = np.full((len(uk), width), -1, dtype=np.int64)
        if len(uk):
            cols[:, 0] = [col[f"beta|{int(c)}"] for c in uk[:, 0]]
            cols[:, 1] = [col[f"gamma|{int(s)}"] for s in uk[:, 1]]
            cols[:, 2] = [col[f"pi|{int(p)}"] for p in uk[:, 2]]
            off = 3
            for d in dims:
                opts = list(ws.spec.options.get(d.dim, ()))
                code = uk[:, 3] if d.dim == "welcome_message" else uk[:, 4]
                for r in range(len(uk)):
                    o = opts[int(code[r])] if 0 <= int(code[r]) < len(opts) else None
                    if o is None or o == d.control or f"theta|{d.dim}|{o}" not in col:
                        continue
                    cols[r, off] = col[f"theta|{d.dim}|{o}"]
                    cell = catalogue.get(int(uk[r, 0]))
                    if cell is not None:
                        for j, (fname, attr) in enumerate(FEATURES):
                            cols[r, off + 1 + j] = col[f"delta|{d.dim}|{o}|{fname}|"
                                                       f"{getattr(cell, attr)}"]
                off += 1 + len(FEATURES)
        prior_sd = np.full(n_par, t.nuisance_prior_sd)
        prior_mean = np.zeros(n_par)
        rate = (succ.sum() + 0.5) / (trials.sum() + 1.0) if len(trials) else 0.5
        prior_mean[:len(cells)] = math.log(rate / (1.0 - rate))
        for nm, i in col.items():
            if nm.startswith(("theta|", "delta|")):
                prior_sd[i] = t.tau_e
        fit = fit_ridge_logit(SparseDesign(cols=cols, n_params=n_par), succ, trials, roww,
                              prior_sd, prior_mean)

        # effect maps (logit scale) and the control's linear predictor per cell
        emaps: dict[tuple[str, str], np.ndarray] = {}
        for d in dims:
            for a in d.arms:
                E = np.zeros((len(cells), n_par))
                E[:, col[f"theta|{d.dim}|{a}"]] = 1.0
                for r, c in enumerate(cells):
                    cell = catalogue.get(c)
                    if cell is None:
                        continue
                    for fname, attr in FEATURES:
                        E[r, col[f"delta|{d.dim}|{a}|{fname}|{getattr(cell, attr)}"]] = 1.0
                emaps[(d.dim, a)] = E
        C = np.zeros((len(cells), n_par))
        for c, r in cell_pos.items():
            C[r, col[f"beta|{c}"]] = 1.0
        if segs:
            C[:, col[f"gamma|{segs[-1]}"]] = 1.0
        if prompts:
            live_p = option_index(ws.spec, "prompt_version").get(ws.live.prompt_version)
            p_ref = live_p if live_p in prompts else prompts[-1]
            C[:, col[f"pi|{p_ref}"]] = 1.0
        index = ParamIndex(names=tuple(names), cell_ids=tuple(cells),
                           effect_maps=FrozenDict(emaps), scale="logit", control_map=C)
        summary = FrozenDict({"model": self.name, "kind": kind, "tau_e": t.tau_e,
                              "rows": int(len(uk)), "params": n_par,
                              "iterations": fit.iterations, "converged": fit.converged,
                              "loglik": fit.loglik,
                              "arms": FrozenDict({d.dim: FrozenDict({"control": d.control,
                                                                     "arms": d.arms})
                                                  for d in dims})})
        return Posterior(wf=ws.spec.workflow_id, kind=kind, mean=fit.mean, cov=fit.cov,
                         index=index,
                         inputs_hash=_hash_inputs(self.name, kind, uk, trials, succ, roww,
                                                  tuple(names), t.tau_e, t.nuisance_prior_sd),
                         summary=summary)

    def draws(self, post: Posterior, seed: int, n: int | None = None) -> Draws:
        return _draws(post, seed, n if n is not None else self.t.n_draws)


# ============================================================================== shared

def _draws(post: Posterior, seed: int, n: int) -> Draws:
    cov = post.cov if post.cov is not None else np.zeros((len(post.mean), len(post.mean)))
    params = seeded_draws(post.mean, cov, seed, n)
    return Draws(kind=post.kind, seed=int(seed), snapshot_id=int(post.snapshot_id or 0),
                 params=params, index=post.index)


def make_engagement_model(settings: Settings | None = None,
                          which: Literal["beta_binomial", "ridge_logit"] | None = None
                          ) -> BetaBinomialModel | RidgeLogitModel:
    """The model selected by `CTL_ENGAGEMENT_MODEL` (17.2): beta_binomial unless the
    ridge-logit beat it on the private variants (ADR 0003)."""
    s = settings or Settings()
    kind = which or s.env.engagement_model
    if kind == "ridge_logit":
        return RidgeLogitModel(s)
    if kind == "beta_binomial":
        return BetaBinomialModel(s)
    raise ValueError(f"unknown engagement model {kind!r}")


class EffectCache:
    """Per-cell effects of one set of draws, computed once and reused by every gate evaluation.

    `effects(dim, arm)` equals `Draws.cell_effects(dim, arm)` (the M0 contract) but multiplies
    only by the columns an effect map touches, which is 10x to 100x cheaper for these sparse
    maps. `base()` is the control's engagement per cell (1 on the log scale, where only ratios
    are modelled; sigmoid of the control's linear predictor on the logit scale).
    """

    def __init__(self, draws: Draws,
                 shifts: Mapping[tuple[str, str], np.ndarray] | None = None):
        self.draws = draws
        self.index = draws.index
        self.shifts = dict(shifts or {})
        """(dim, arm) -> (n_draws,) log-ratio shift added in every cell: the predicted change of
        the arm's effect between now and the apply time (decay.apply_time_shifts)."""
        self._fx: dict[tuple[str, str], np.ndarray] = {}
        self._exp: dict[tuple[str, str], np.ndarray] = {}
        self._base_lin: np.ndarray | None = None
        self._pos = {c: i for i, c in enumerate(self.index.cell_ids)}

    @property
    def n_draws(self) -> int:
        return int(self.draws.params.shape[0])

    @property
    def n_cells(self) -> int:
        return len(self.index.cell_ids)

    def positions(self, cell_ids: Sequence[int]) -> np.ndarray:
        return np.array([self._pos.get(int(c), -1) for c in cell_ids], dtype=np.int64)

    def _lin(self, emap: np.ndarray) -> np.ndarray:
        emap = np.asarray(emap)
        nz = np.flatnonzero(np.any(emap != 0, axis=0))
        return self.draws.params[:, nz] @ emap[:, nz].T

    def base_linear(self) -> np.ndarray | None:
        if self.index.scale == "log" or self.index.control_map is None:
            return None
        if self._base_lin is None:
            b = self._lin(self.index.control_map)
            if self.index.offset is not None:
                b = b + self.index.offset
            self._base_lin = b
        return self._base_lin

    def effects(self, dim: str, arm: str) -> np.ndarray:
        key = (dim, arm)
        if key not in self._fx:
            emap = self.index.effect_maps.get(key)
            if emap is None:
                self._fx[key] = np.zeros((self.n_draws, self.n_cells))
            else:
                delta = self._lin(emap)
                base = self.base_linear()
                if base is None:
                    fx = delta
                else:
                    fx = -np.logaddexp(0.0, -(base + delta)) + np.logaddexp(0.0, -base)
                shift = self.shifts.get(key)
                self._fx[key] = fx if shift is None else fx + np.asarray(shift)[:, None]
        return self._fx[key]

    def ratio(self, dim: str, arm: str) -> np.ndarray:
        """exp(effects): the engagement multiplier of `arm` vs control per draw and cell."""
        key = (dim, arm)
        if key not in self._exp:
            self._exp[key] = np.exp(self.effects(dim, arm))
        return self._exp[key]

    def has(self, dim: str, arm: str) -> bool:
        return (dim, arm) in self.index.effect_maps

    def base(self) -> np.ndarray:
        lin = self.base_linear()
        if lin is None:
            return np.ones((self.n_draws, self.n_cells))
        return 1.0 / (1.0 + np.exp(-lin))

    def scope_effect(self, dim: str, arm: str, cell_ids: Sequence[int],
                     weights: Sequence[float]) -> np.ndarray:
        """(n_draws,) log ratio of scope-level expected engagement, arm vs control, over the
        given cells weighted by leads (and by the control's base rate on the logit scale)."""
        pos = self.positions(cell_ids)
        keep = pos >= 0
        if not keep.any():
            return np.zeros(self.n_draws)
        pos = pos[keep]
        w = np.asarray(weights, dtype=float)[keep]
        b = self.base()[:, pos] * w[None, :]
        num = (b * self.ratio(dim, arm)[:, pos]).sum(axis=1)
        return np.log(num / b.sum(axis=1))


def p_best(effects: Mapping[str, np.ndarray], control: str) -> dict[str, float]:
    """P(option is best) from per-draw scope effects (the control's effect is 0)."""
    names = [control] + [a for a in effects if a != control]
    n = next(iter(effects.values())).shape[0] if effects else 1
    mat = np.stack([np.zeros(n)] + [effects[a] for a in names[1:]], axis=1)
    best = np.argmax(mat, axis=1)
    counts = np.bincount(best, minlength=len(names)) / float(n)
    return {nm: float(counts[i]) for i, nm in enumerate(names)}


__all__ = ["BetaBinomialModel", "RidgeLogitModel", "DimArms", "dim_arms", "EffectCache",
           "make_engagement_model", "p_best", "model_cells", "ENGAGEMENT_DIMS", "LearningSnapshot"]
