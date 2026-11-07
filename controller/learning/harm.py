"""The harm gate: one computation that mirrors the grader, plus our cohort line (DESIGN.md 0.1).

Owner: track T3 (milestone M5). Implements DESIGN.md 0.1, 5.4 and ADR 0002.

The grader judges a change at apply time over the active leads: harmful if expected engagement
falls by more than 1% or expected caller-ID health by more than 2% (grader.py:67-73,
world.py:634-655). From our posterior draws:

    population:  P( de   < -1%  ) <= alpha_H      de   = E_new / E_cur - 1 over the active leads
    per cohort:  P( de_k < -x_k ) <= alpha_H      for every cohort k (of the NEW config)
    caller ID:   P( dd   < -2%  ) <= alpha_D      dd   = H_new / H_cur - 1
    guardrail:   predicted drop <= tol and P(drop > x) <= 2.5%   (5.10, from a GuardrailForecast)

`E_X = sum_c leads_c * base_c * prod_dim sum_o p_X(cohort_X(c), dim)[o] * exp(L_{dim,o}(c))`,
the simulator's own expected-engagement formula (world.py:288-305) with our effect draws in
place of the hidden multipliers. Welcome and voice are the modelled dimensions; an option with
no effect map (the control, or an unmodelled option) has multiplier 1. A caller-ID change with
no DID posterior fails closed ("did_unmodeled").

`GateContext` holds everything that does not depend on the proposed config (draws, effect
cache, population, the current config's multipliers), so the allocator's bisection evaluates
many candidate configs cheaply: only cells whose distribution changes are recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import numpy as np

from controller.learning.engagement import ENGAGEMENT_DIMS, EffectCache
from controller.settings import Settings, Thresholds
from controller.snapshot import cohort_of_cells
from controller.types import (DEFAULT_COHORT, ActivePopulation, Cell, Config, DidPosterior,
                              Draws, EffectivePolicy, FrozenDict, GuardrailForecast, HarmReport)


def _thresholds(settings: Settings | Thresholds | None) -> Thresholds:
    if settings is None:
        return Thresholds()
    return settings.t if isinstance(settings, Settings) else settings


@dataclass(frozen=True, eq=False)
class GateResult:
    """A HarmReport plus the per-draw arrays callers reuse (minimum gain, rollback)."""

    report: HarmReport
    de: np.ndarray
    """(n_draws,) population de"""
    de_k: Mapping[str, np.ndarray]
    changed_cells: np.ndarray
    """positions (into the population) of cells whose expected engagement changed"""
    e_cur: np.ndarray
    """(n_draws, n_cells) expected engagement per cell under cur (lead weighted)"""
    e_new: np.ndarray


class GateContext:
    """Draws, population and current config, prepared once for many gate evaluations."""

    def __init__(self, draws: Draws | EffectCache, pop: ActivePopulation,
                 cells: Mapping[int, Cell] | None = None,
                 settings: Settings | Thresholds | None = None,
                 dims: Sequence[str] = ENGAGEMENT_DIMS):
        self.t = _thresholds(settings)
        self.cache = draws if isinstance(draws, EffectCache) else EffectCache(draws)
        self.pop = pop
        self.cells = dict(cells or {})
        self.cell_ids = [int(c) for c in pop.cells]
        self.pos = self.cache.positions(self.cell_ids)
        self.lead = np.asarray(pop.leads, dtype=float)
        base = np.ones((self.cache.n_draws, len(self.cell_ids)))
        known = self.pos >= 0
        if known.any():
            base[:, known] = self.cache.base()[:, self.pos[known]]
        self.wbase = base * self.lead[None, :]
        self.dims = tuple(dims)
        self._mult_memo: dict[str, np.ndarray] = {}
        self._coh_memo: dict[tuple[str, ...], list[str]] = {}

    # ---------------------------------------------------------------- membership
    def cohorts(self, cfg: Config) -> list[str]:
        """First-match cohort of every population cell under cfg."""
        key = tuple(c.id + repr(c.where) for c in cfg.cohorts)
        if key not in self._coh_memo:
            if self.cells:
                self._coh_memo[key] = cohort_of_cells(cfg, self.cells, self.cell_ids)
            else:
                m = self.pop.cohort_of_cell
                self._coh_memo[key] = [m.get(c, DEFAULT_COHORT) for c in self.cell_ids]
        return self._coh_memo[key]

    # ---------------------------------------------------------------- multipliers
    def _dim_mult(self, cfg: Config, dim: str, coh: Sequence[str],
                  cols: np.ndarray | None = None) -> np.ndarray:
        """(n_draws, len(cols)) multiplier sum_o p_o exp(L_o) for the given cell positions."""
        cols = np.arange(len(self.cell_ids)) if cols is None else cols
        out = np.zeros((self.cache.n_draws, len(cols)))
        groups: dict[str, list[int]] = {}
        for j, c in enumerate(cols):
            groups.setdefault(coh[int(c)], []).append(j)
        for k, js in groups.items():
            jj = np.asarray(js)
            cells_pos = self.pos[cols[jj]]
            for o, p in cfg.dist(k, dim).items():
                if p <= 0:
                    continue
                if not self.cache.has(dim, o):
                    out[:, jj] += p
                    continue
                r = np.ones((self.cache.n_draws, len(jj)))
                known = cells_pos >= 0
                if known.any():
                    r[:, known] = self.cache.ratio(dim, o)[:, cells_pos[known]]
                out[:, jj] += p * r
        return out

    def multiplier(self, cfg: Config) -> np.ndarray:
        h = cfg.hash()
        if h not in self._mult_memo:
            coh = self.cohorts(cfg)
            m = np.ones((self.cache.n_draws, len(self.cell_ids)))
            for dim in self.dims:
                m *= self._dim_mult(cfg, dim, coh)
            if len(self._mult_memo) > 64:
                self._mult_memo.clear()
            self._mult_memo[h] = m
        return self._mult_memo[h]

    def _changed_cells(self, cur: Config, new: Config) -> np.ndarray:
        cc, cn = self.cohorts(cur), self.cohorts(new)
        changed = []
        memo: dict[tuple[str, str], bool] = {}
        for j in range(len(self.cell_ids)):
            key = (cc[j], cn[j])
            if key not in memo:
                memo[key] = any(cur.dist(cc[j], d) != new.dist(cn[j], d) for d in self.dims)
            if memo[key]:
                changed.append(j)
        return np.asarray(changed, dtype=np.int64)

    def multiplier_from(self, cur: Config, new: Config) -> tuple[np.ndarray, np.ndarray]:
        """Multiplier under `new`, recomputing only the cells whose distribution differs."""
        base = self.multiplier(cur)
        changed = self._changed_cells(cur, new)
        if len(changed) == 0:
            return base, changed
        m = base.copy()
        coh = self.cohorts(new)
        part = np.ones((self.cache.n_draws, len(changed)))
        for dim in self.dims:
            part *= self._dim_mult(new, dim, coh, changed)
        m[:, changed] = part
        return m, changed

    # ---------------------------------------------------------------- the gate
    def evaluate(self, cur: Config, new: Config, pol: EffectivePolicy | None = None,
                 alpha_h: float | None = None, alpha_d: float | None = None,
                 did_post: DidPosterior | None = None, guard: GuardrailForecast | None = None,
                 cohort_line: float | None = None) -> GateResult:
        t = self.t
        alpha_h = t.alpha_h if alpha_h is None else alpha_h
        alpha_d = t.alpha_d if alpha_d is None else alpha_d
        x_k = cohort_line if cohort_line is not None else (
            pol.cohort_harm_line if pol is not None else t.cohort_harm_line_default)
        tol = t.rounding
        m_cur = self.multiplier(cur)
        m_new, changed = self.multiplier_from(cur, new)
        e_cur = self.wbase * m_cur
        e_new = self.wbase * m_new
        de = e_new.sum(axis=1) / e_cur.sum(axis=1) - 1.0
        failed: list[str] = []
        p_pop = float(np.mean(de < -t.grader_engagement_line))
        if p_pop > alpha_h + tol:
            failed.append("population")
        coh_new = self.cohorts(new)
        de_k: dict[str, np.ndarray] = {}
        report_k: dict[str, tuple[float, float]] = {}
        order = list(new.cohort_ids()) + [DEFAULT_COHORT]
        for k in order:
            sel = np.array([c == k for c in coh_new])
            if not sel.any() or self.lead[sel].sum() <= 0:
                continue
            dk = e_new[:, sel].sum(axis=1) / e_cur[:, sel].sum(axis=1) - 1.0
            de_k[k] = dk
            pk = float(np.mean(dk < -x_k))
            report_k[k] = (float(dk.mean()), pk)
            if pk > alpha_h + tol:
                failed.append(f"cohort:{k}")
        dd_mean, p_dd = self._did(cur, new, did_post, failed, alpha_d)
        g_kpi, g_mean, g_p = "", 0.0, 0.0
        if guard is not None:
            g_kpi, g_mean, g_p = guard.kpi, float(guard.drop_mean), float(guard.p_breach)
            if g_mean > t.guardrail_drop_tol or g_p > t.guardrail_p_breach + tol:
                failed.append("guardrail")
        report = HarmReport(de_mean=float(de.mean()), p_de_below_1pct=p_pop,
                            de_k=FrozenDict(report_k), dd_mean=dd_mean, p_dd_below_2pct=p_dd,
                            guardrail_kpi=g_kpi, guardrail_drop_mean=g_mean,
                            p_guardrail_breach=g_p, passes=not failed, failed=tuple(failed),
                            alpha_used=float(alpha_h))
        return GateResult(report=report, de=de, de_k=de_k, changed_cells=changed, e_cur=e_cur,
                          e_new=e_new)

    def _did(self, cur: Config, new: Config, did_post: DidPosterior | None,
             failed: list[str], alpha_d: float) -> tuple[float, float]:
        cc, cn = self.cohorts(cur), self.cohorts(new)
        same = all(cur.dist(a, "caller_id") == new.dist(b, "caller_id") for a, b in zip(cc, cn))
        if same:
            return 0.0, 0.0
        if did_post is None:
            failed.append("did_unmodeled")
            return 0.0, 1.0
        r = np.asarray(did_post.draws, dtype=float)
        col = {d: i for i, d in enumerate(did_post.dids)}
        def health(cfg: Config, coh: Sequence[str]) -> np.ndarray:
            tot = np.zeros(r.shape[0])
            for k in sorted(set(coh)):
                lead = float(self.lead[np.array([c == k for c in coh])].sum())
                for d, p in cfg.dist(k, "caller_id").items():
                    rd = r[:, col[d]] if d in col else np.ones(r.shape[0])
                    tot += lead * p * rd
            return tot
        dd = health(new, cn) / health(cur, cc) - 1.0
        p = float(np.mean(dd < -self.t.grader_did_line))
        if p > alpha_d + self.t.rounding:
            failed.append("did")
        return float(dd.mean()), p


def harm_gate(cur: Config, new: Config, draws_ew: Draws, did_post: DidPosterior | None,
              guard: GuardrailForecast | None, pop: ActivePopulation, pol: EffectivePolicy,
              alpha_h: float, alpha_d: float, *, cells: Mapping[int, Cell] | None = None,
              settings: Settings | Thresholds | None = None) -> HarmReport:
    """The 11.2 signature: population, cohort, DID and guardrail lines in one report."""
    ctx = GateContext(draws_ew, pop, cells=cells, settings=settings)
    return ctx.evaluate(cur, new, pol, alpha_h, alpha_d, did_post, guard).report


def largest_passing(passes: Callable[[float], bool], iters: int, hi: float = 1.0) -> float:
    """Largest x in [0, hi] with passes(x), by bisection, assuming passes(0) (no change is
    always safe) and monotonicity. Deterministic: `iters` halvings, resolution hi / 2^iters."""
    if passes(hi):
        return hi
    lo, up = 0.0, hi
    for _ in range(iters):
        mid = 0.5 * (lo + up)
        if passes(mid):
            lo = mid
        else:
            up = mid
    return lo


def uniform_exploration(cfg: Config, dim_arms: Mapping[str, tuple[str, Sequence[str]]],
                        share: float, cohort: str = DEFAULT_COHORT) -> Config:
    """`cfg` with every listed arm at `share` in `cohort` and the control taking the rest
    (dim -> (control, arms)). Used to size the first exploration from the gate (5.4)."""
    out = cfg
    for dim, (control, arms) in dim_arms.items():
        w = {a: share for a in arms}
        w[control] = 1.0 - share * len(arms)
        out = out.with_allocation(cohort, dim, w)
    return out
