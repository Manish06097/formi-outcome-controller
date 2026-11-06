"""build_grid: the D0 cohort grid of one workflow, from history only (regrid is cut).

Owner: track T4 (milestone M4). Implements DESIGN.md 5.2 ("Grid at D0", "Business cohorts
first", "Minimum sizes") and ADR 0022.

Steps:
1. Business cohorts from the default config are kept verbatim, in order, at the front, and are
   supervisor-owned. Only `max_cohorts - n_business` grid cohorts are built, over the cells the
   business cohorts do not already take (first match).
2. F1 = the feature whose levels have the most different hour-of-day pickup curves: binomial
   deviance of `connected ~ hour + f` against the saturated `hour * f` table, per degree of
   freedom, on first-attempt rows, required p < 0.001. It drives the schedule.
3. F2 = `lang` if the workflow has 2 to 5 languages each with >= 10% of leads (a stated prior:
   welcome and voice are language bound), else the remaining feature with the most balanced mass.
4. F1 x F2 cells under 5% of daily leads merge with their closest F1 neighbour inside the same F2
   level ("closest" = the most similar hour curve, which is what the merged cohort's schedule
   needs); an F2 level that is too small as a whole goes to an `other` bucket per F1 level.
   Buckets still under 5% fall to `default`. If more than the cap remain, the smallest F2 levels
   are folded into the buckets.
5. Cohorts are ordered most specific first, buckets last, so first match is unambiguous; each id
   is the versioned `cohort_key`.

Pure: inputs are the cell catalog, `r_pickup` arrays and optional lead counts per cell.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from controller.cohorts.cells import CellCatalog
from controller.cohorts.cohortset import (CohortEntry, GridCohortSet, business_entries,
                                          cohort_key)
from controller.settings import Thresholds
from controller.types import FEATURE_NAMES, CohortDef, PickupArrays

# =============================================================================== small statistics


def chi2_sf(x: float, df: int) -> float:
    """Upper tail of the chi-square distribution (regularised upper incomplete gamma Q(df/2, x/2)),
    series for x < a + 1 and Lentz's continued fraction otherwise (Numerical Recipes 6.2)."""
    if df <= 0:
        return 1.0
    if x <= 0:
        return 1.0
    a, z = df / 2.0, x / 2.0
    gln = math.lgamma(a)
    if z < a + 1.0:
        term = s = 1.0 / a
        ap = a
        for _ in range(10_000):
            ap += 1.0
            term *= z / ap
            s += term
            if abs(term) < abs(s) * 1e-15:
                break
        return max(0.0, 1.0 - s * math.exp(-z + a * math.log(z) - gln))
    b = z + 1.0 - a
    c, d = 1e300, 1.0 / b
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-15:
            break
    return min(1.0, math.exp(-z + a * math.log(z) - gln) * h)


def additive_logit_deviance(dials: np.ndarray, conn: np.ndarray) -> tuple[float, int]:
    """Deviance of the additive logistic model `row + column` on an (R x C) table of binomial
    counts against the saturated (interaction) model, and its degrees of freedom. Rows and columns
    without dials are dropped. Fitted by IRLS (a few dozen parameters at most)."""
    keep_r = dials.sum(1) > 0
    keep_c = dials.sum(0) > 0
    n, y = dials[keep_r][:, keep_c].astype(float), conn[keep_r][:, keep_c].astype(float)
    R, C = n.shape
    if R < 2 or C < 2:
        return 0.0, 0
    cells = n > 0
    ii, jj = np.nonzero(cells)
    nn, yy = n[cells], y[cells]
    X = np.zeros((len(nn), R + C - 1))
    X[:, 0] = 1.0
    X[np.arange(len(nn))[ii > 0], ii[ii > 0]] = 1.0
    X[np.arange(len(nn))[jj > 0], R - 1 + jj[jj > 0]] = 1.0
    p0 = np.clip((yy.sum() + 0.5) / (nn.sum() + 1.0), 1e-6, 1 - 1e-6)
    beta = np.zeros(X.shape[1])
    beta[0] = math.log(p0 / (1 - p0))
    for _ in range(50):
        eta = X @ beta
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = nn * mu * (1 - mu) + 1e-12
        zz = eta + (yy - nn * mu) / w
        A = X.T @ (w[:, None] * X) + 1e-9 * np.eye(X.shape[1])
        nb = np.linalg.solve(A, X.T @ (w * zz))
        if np.max(np.abs(nb - beta)) < 1e-10:
            beta = nb
            break
        beta = nb
    mu = nn / (1.0 + np.exp(-(X @ beta)))
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = np.where(yy > 0, yy * np.log(yy / mu), 0.0)
        t2 = np.where(nn - yy > 0, (nn - yy) * np.log((nn - yy) / (nn - mu)), 0.0)
    dev = float(2.0 * np.sum(t1 + t2))
    df = int(len(nn) - X.shape[1])
    return max(dev, 0.0), df


# =============================================================================== inputs


@dataclass(frozen=True)
class GridInputs:
    """What the grid needs, already restricted to the rows the caller chose (history at D0)."""

    workflow_id: str
    catalog: CellCatalog
    pickup: PickupArrays
    hours: tuple[int, ...]
    """In-window hours (dialling window from the API)."""
    business_cohorts: tuple[CohortDef, ...] = ()
    leads_by_cell: Mapping[int, float] | None = None
    """Daily leads per cell; default: first-attempt dials per cell (one per lead dialled)."""
    end_minute: int | None = None


@dataclass
class _Group:
    f1_levels: list[str]
    f2_level: str | None
    cells: set[int]
    mass: float


class _Curves:
    """Shrunk first-attempt hour curves of arbitrary cell sets (5.8 shrinkage)."""

    def __init__(self, pk: PickupArrays, hours: Sequence[int], prior: float):
        self.hours = list(hours)
        self.prior = prior
        hidx = {h: i for i, h in enumerate(self.hours)}
        first = (pk.attempt_bucket == 1) & np.isin(pk.hour, self.hours)
        self.by_cell: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for c, h, n, y in zip(pk.cell[first].tolist(), pk.hour[first].tolist(),
                              pk.dials[first].tolist(), pk.connected[first].tolist()):
            d, k = self.by_cell.setdefault(int(c), (np.zeros(len(self.hours)),
                                                    np.zeros(len(self.hours))))
            d[hidx[int(h)]] += n
            k[hidx[int(h)]] += y
        tot_d = sum((v[0] for v in self.by_cell.values()), np.zeros(len(self.hours)))
        tot_k = sum((v[1] for v in self.by_cell.values()), np.zeros(len(self.hours)))
        overall = tot_k.sum() / tot_d.sum() if tot_d.sum() > 0 else 0.0
        self.pooled = np.where(tot_d > 0, tot_k / np.maximum(tot_d, 1), overall)

    def counts(self, cells: set[int]) -> tuple[np.ndarray, np.ndarray]:
        d = np.zeros(len(self.hours))
        k = np.zeros(len(self.hours))
        for c in cells:
            if c in self.by_cell:
                d += self.by_cell[c][0]
                k += self.by_cell[c][1]
        return d, k

    def shape(self, cells: set[int]) -> np.ndarray:
        d, k = self.counts(cells)
        p = (k + self.prior * self.pooled) / (d + self.prior)
        m = p.mean()
        return p / m if m > 0 else np.ones_like(p)


# =============================================================================== feature choice


def f1_scores(inp: GridInputs, cells: set[int], t: Thresholds) -> dict[str, dict]:
    """Per feature: deviance of hour + f vs hour * f per df, df and p (first-attempt rows)."""
    pk = inp.pickup
    first = (pk.attempt_bucket == 1) & np.isin(pk.hour, inp.hours) & np.isin(pk.cell, list(cells))
    out: dict[str, dict] = {}
    hidx = {h: i for i, h in enumerate(inp.hours)}
    for f in FEATURE_NAMES:
        levels = inp.catalog.levels(f, cells)
        if len(levels) < 2:
            continue
        lidx = {lv: j for j, lv in enumerate(levels)}
        n = np.zeros((len(inp.hours), len(levels)))
        y = np.zeros_like(n)
        for c, h, d, k in zip(pk.cell[first].tolist(), pk.hour[first].tolist(),
                              pk.dials[first].tolist(), pk.connected[first].tolist()):
            j = lidx[inp.catalog.feature(int(c), f)]
            n[hidx[int(h)], j] += d
            y[hidx[int(h)], j] += k
        dev, df = additive_logit_deviance(n, y)
        if df <= 0:
            continue
        out[f] = {"deviance": dev, "df": df, "per_df": dev / df, "p": chi2_sf(dev, df)}
    return out


def choose_f1(scores: Mapping[str, dict], t: Thresholds) -> str | None:
    sig = {f: s for f, s in scores.items() if s["p"] < t.grid_f1_p}
    if not sig:
        return None
    return max(sorted(sig), key=lambda f: sig[f]["per_df"])


def level_shares(inp: GridInputs, mass: Mapping[int, float], f: str,
                 cells: set[int]) -> dict[str, float]:
    tot = sum(mass.get(c, 0.0) for c in cells)
    out: dict[str, float] = {}
    for c in cells:
        lv = inp.catalog.feature(c, f)
        out[lv] = out.get(lv, 0.0) + mass.get(c, 0.0)
    return {k: (v / tot if tot > 0 else 0.0) for k, v in sorted(out.items())}


def choose_f2(inp: GridInputs, mass: Mapping[int, float], cells: set[int], f1: str | None,
              t: Thresholds) -> str | None:
    lang = level_shares(inp, mass, "lang", cells)
    if (f1 != "lang" and t.grid_f2_lang_min_langs <= len(lang) <= t.grid_f2_lang_max_langs
            and all(s >= t.grid_f2_lang_min_share for s in lang.values())):
        return "lang"
    best, best_h = None, -1.0
    for f in FEATURE_NAMES:
        if f == f1:
            continue
        sh = [s for s in level_shares(inp, mass, f, cells).values() if s > 0]
        if len(sh) < 2:
            continue
        ent = -sum(s * math.log(s) for s in sh) / math.log(len(sh))
        if ent > best_h + 1e-12:
            best, best_h = f, ent
    return best


# =============================================================================== merging


def _merge_small(groups: list[_Group], curves: _Curves, min_mass: float,
                 log: list[dict]) -> list[_Group]:
    """Merge the smallest group under `min_mass` into the group with the closest hour-curve
    shape, until every group reaches it or one group is left."""
    groups = list(groups)
    while len(groups) > 1:
        small = [g for g in groups if g.mass < min_mass]
        if not small:
            break
        g = min(small, key=lambda x: (x.mass, x.f1_levels))
        shape = curves.shape(g.cells)
        others = [o for o in groups if o is not g]
        o = min(others, key=lambda x: (float(np.abs(curves.shape(x.cells) - shape).sum()),
                                       x.f1_levels))
        log.append({"merge": sorted(g.f1_levels), "into": sorted(o.f1_levels),
                    "f2": g.f2_level, "mass": g.mass})
        o.f1_levels = sorted(set(o.f1_levels) | set(g.f1_levels))
        o.cells |= g.cells
        o.mass += g.mass
        groups.remove(g)
    return groups


def _where(f1: str | None, f1_levels: Sequence[str], f2: str | None,
           f2_level: str | None) -> dict[str, list[str]]:
    w: dict[str, list[str]] = {}
    if f1 is not None:
        w[f1] = sorted(f1_levels)
    if f2 is not None and f2_level is not None:
        w[f2] = [f2_level]
    return w


def build_grid(inp: GridInputs, t: Thresholds | None = None) -> GridCohortSet:
    """The D0 cohort set: business cohorts verbatim first, then the F1 x F2 grid (DESIGN.md 5.2)."""
    t = t or Thresholds()
    cat = inp.catalog
    biz = business_entries(inp.business_cohorts, cat)
    cap = t.max_cohorts - len(biz)
    taken: set[int] = set()
    for e in biz:
        taken |= set(e.matched_cells)
    mass = _lead_mass(inp)
    total = sum(mass.values())
    residual = {c for c in cat.ids() if c not in taken and mass.get(c, 0.0) > 0}
    evidence: dict = {"n_business": len(biz), "cap": cap, "total_daily_leads": total}
    if cap <= 0 or total <= 0 or not residual:
        evidence["reason"] = "no grid: no capacity or no history"
        return GridCohortSet(inp.workflow_id, biz, cat, 1, inp.end_minute, t, evidence)
    min_mass = t.targeting_cohort_min_share * total
    scores = f1_scores(inp, residual, t)
    f1 = choose_f1(scores, t)
    f2 = choose_f2(inp, mass, residual, f1, t)
    evidence.update({"f1": f1, "f2": f2, "f1_scores": scores, "min_mass": min_mass})
    curves = _Curves(inp.pickup, inp.hours, t.hour_curve_prior)
    log: list[dict] = []
    f2_levels = list(cat.levels(f2, residual)) if f2 else [None]
    f1_levels = list(cat.levels(f1, residual)) if f1 else [None]

    def cells_of(l1: str | None, l2: str | None) -> set[int]:
        return {c for c in residual
                if (l1 is None or cat.feature(c, f1) == l1)  # type: ignore[arg-type]
                and (l2 is None or cat.feature(c, f2) == l2)}  # type: ignore[arg-type]

    folded: set[str | None] = set()
    while True:
        specific: list[_Group] = []
        to_other: list[_Group] = []
        for l2 in f2_levels:
            gs = [_Group([l1] if l1 is not None else [], l2, cells_of(l1, l2), 0.0)
                  for l1 in f1_levels]
            gs = [g for g in gs if g.cells]
            for g in gs:
                g.mass = sum(mass[c] for c in g.cells)
            if not gs:
                continue
            if l2 in folded:
                to_other.extend(gs)
                continue
            if f1 is None or f2 is None:
                specific.extend(gs)  # a one-feature grid: the levels themselves
                continue
            gs = _merge_small(gs, curves, min_mass, log)
            if len(gs) == 1 and gs[0].mass < min_mass:
                to_other.extend(_Group([l1], l2, gs[0].cells & cells_of(l1, l2), 0.0)
                                for l1 in gs[0].f1_levels)
            else:
                specific.extend(gs)
        if f1 is None or f2 is None:
            specific = _merge_small(specific, curves, min_mass, log)
            specific = [g for g in specific if g.mass >= min_mass]
            others: list[_Group] = []
        else:
            by_l1: dict[str, _Group] = {}
            for g in to_other:
                for l1 in g.f1_levels:
                    cs = {c for c in g.cells if cat.feature(c, f1) == l1}
                    b = by_l1.setdefault(l1, _Group([l1], None, set(), 0.0))
                    b.cells |= cs
                    b.mass += sum(mass[c] for c in cs)
            others = _merge_small([by_l1[k] for k in sorted(by_l1)], curves, min_mass, log)
            dropped = [g for g in others if g.mass < min_mass]
            others = [g for g in others if g.mass >= min_mass]
            if dropped:
                log.append({"to_default": [sorted(g.f1_levels) for g in dropped],
                            "mass": sum(g.mass for g in dropped)})
        if len(specific) + len(others) <= cap:
            break
        live = [l2 for l2 in f2_levels if l2 not in folded]
        if f1 is None or f2 is None or not live:
            specific = sorted(specific, key=lambda g: -g.mass)[:cap]
            others = []
            break
        smallest = min(live, key=lambda l2: (sum(mass[c] for c in cells_of(None, l2)), l2))
        folded.add(smallest)
        log.append({"fold_f2_level": smallest, "reason": "cap"})

    specific.sort(key=lambda g: (str(g.f2_level), g.f1_levels))
    others.sort(key=lambda g: g.f1_levels)
    entries: list[CohortEntry] = list(biz)
    for i, g in enumerate(specific + others):
        where = _where(f1, g.f1_levels, f2, g.f2_level)
        d = CohortDef.make(cohort_key(inp.workflow_id, tuple((k, tuple(v)) for k, v in
                                                             sorted(where.items())),
                                      len(entries)), where)
        entries.append(CohortEntry(d.id, d, "grid", "ours", cat.matching(d)))
    evidence["merges"] = log
    evidence["shares"] = {e.key: sum(mass.get(c, 0.0) for c in e.matched_cells - taken) / total
                          for e in entries if e.origin == "grid"}
    return GridCohortSet(inp.workflow_id, entries, cat, 1, inp.end_minute, t, evidence)


def _lead_mass(inp: GridInputs) -> dict[int, float]:
    """Daily leads per cell: the given counts, else first-attempt dials per history day."""
    if inp.leads_by_cell is not None:
        return {int(c): float(v) for c, v in inp.leads_by_cell.items() if v > 0}
    pk = inp.pickup
    first = pk.attempt_bucket == 1
    days = max(len(np.unique(pk.sim_day[first])), 1)
    out: dict[int, float] = {}
    for c, d in zip(pk.cell[first].tolist(), pk.dials[first].tolist()):
        out[int(c)] = out.get(int(c), 0.0) + d / days
    return out


__all__ = ["build_grid", "GridInputs", "chi2_sf", "additive_logit_deviance", "f1_scores",
           "choose_f1", "choose_f2"]
