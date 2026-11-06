"""Per-cohort call-time weights: HourCurves.fit(ws), plan_hour_weights(ws, curves).

Owner: track T4 (milestone M4). Implements DESIGN.md 5.8 (call time per cohort).

Curves. Pickup by (cohort, hour, attempt bucket {1, 2-3, 4+}) from `r_pickup`, each bucket's rate
shrunk toward the workflow's pooled rate at that (hour, bucket) with a prior of 200 dials, then
averaged over ONE fixed attempt mix (the workflow's overall bucket shares), because later hours
over-represent hard-to-reach leads on a flat history schedule. The remaining eligibility bias is
stated in ADR 0022, not corrected.

Weights. The dialler ranks eligible leads inside an hour by weight times Gumbel noise and capacity
binds, so weights express comparative advantage:

    w_k(h) = clip( (p_k(h) / pbar_k)^kappa / mean_h((p_k / pbar_k)^kappa), 0.25, 4.0 ),  kappa = 2

Every in-window hour gets a positive weight: a missing hour gets weight 0 in the simulator and
wastes capacity the business owns (constraint 2).

Governance. Schedule changes are graded neutral, ship as P3 patches, and are proposed only when
the L1 distance between normalised tables moves by more than 0.1 (slot economy). Business and
supervisor-owned tables are never touched; `default` gets no table from us (it is the fallback of
business cohorts too). Without history no table is proposed before 3 dialling days of own dials.

Pure: reads a WorkflowSnapshot, returns HourCurveSet and PatchRequests.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from controller.settings import Thresholds
from controller.types import (DEFAULT_COHORT, P3_ROUTINE, Cohort, Finding, FrozenDict,
                              HourCurveSet, PatchRequest, WorkflowSnapshot, freeze)

OWNER = "learning.schedule"
BUCKETS = (1, 2, 3)


def window_hours(ws: WorkflowSnapshot) -> tuple[int, ...]:
    lo, hi = ws.spec.dialling_window
    return tuple(range(lo, hi))


def hour_weights_from_curve(curve: Mapping[int, float], hours: Sequence[int],
                            t: Thresholds) -> dict[int, float]:
    """The 5.8 formula for one cohort. Every hour in `hours` gets a weight in [min, max]."""
    p = np.array([max(float(curve.get(h, 0.0)), 0.0) for h in hours])
    pbar = p.mean()
    if pbar <= 0:
        return {h: 1.0 for h in hours}
    r = (p / pbar) ** t.hour_weight_kappa
    w = np.clip(r / r.mean(), t.hour_weight_min, t.hour_weight_max)
    return {h: round(float(x), 4) for h, x in zip(hours, w)}


def normalised_l1(a: Mapping[int, float] | None, b: Mapping[int, float] | None,
                  hours: Sequence[int]) -> float:
    """L1 distance between two weight tables normalised to sum 1 over `hours`. A missing table
    is flat (weight 1 everywhere); a missing hour in a table is 0, as the simulator reads it."""
    def norm(t: Mapping[int, float] | None) -> np.ndarray:
        v = np.ones(len(hours)) if t is None else np.array([float(t.get(h, 0.0)) for h in hours])
        s = v.sum()
        return v / s if s > 0 else v
    return float(np.abs(norm(a) - norm(b)).sum())


@dataclass
class HourCurvesModel:
    """The concrete `HourCurves` (types.HourCurves protocol)."""

    t: Thresholds = Thresholds()

    # ------------------------------------------------------------------ fit
    def fit(self, ws: WorkflowSnapshot) -> HourCurveSet:
        hours = window_hours(ws)
        pk = ws.pickup
        hidx = {h: i for i, h in enumerate(hours)}
        cohorts = (*ws.cohorts.to_config(),)
        keys = [c.id for c in cohorts] + [DEFAULT_COHORT]
        kidx = {k: i for i, k in enumerate(keys)}
        n = np.zeros((len(keys), len(hours), len(BUCKETS)))
        y = np.zeros_like(n)
        inwin = np.isin(pk.hour, hours)
        for cell, h, b, d, c in zip(pk.cell[inwin].tolist(), pk.hour[inwin].tolist(),
                                    pk.attempt_bucket[inwin].tolist(), pk.dials[inwin].tolist(),
                                    pk.connected[inwin].tolist()):
            k = kidx[ws.cohorts.cohort_of(int(cell))]
            n[k, hidx[int(h)], int(b) - 1] += d
            y[k, hidx[int(h)], int(b) - 1] += c
        tot_n, tot_y = n.sum(0), y.sum(0)                      # (hour, bucket)
        overall = tot_y.sum() / tot_n.sum() if tot_n.sum() > 0 else 0.0
        by_bucket = np.where(tot_n.sum(0) > 0, tot_y.sum(0) / np.maximum(tot_n.sum(0), 1), overall)
        pooled_hb = np.where(tot_n > 0, tot_y / np.maximum(tot_n, 1), by_bucket[None, :])
        mix = tot_n.sum(0) / tot_n.sum() if tot_n.sum() > 0 else np.array([1.0, 0.0, 0.0])
        m = self.t.hour_curve_prior
        shrunk = (y + m * pooled_hb[None]) / (n + m)           # (cohort, hour, bucket)
        curves = shrunk @ mix                                    # fixed attempt mix
        pooled = pooled_hb @ mix
        return HourCurveSet(
            wf=ws.spec.workflow_id,
            curves=freeze({k: {h: float(curves[i, j]) for j, h in enumerate(hours)}
                           for i, k in enumerate(keys)}),
            pooled=freeze({h: float(pooled[j]) for j, h in enumerate(hours)}),
            dials=freeze({k: {h: int(n[i, j].sum()) for j, h in enumerate(hours)}
                          for i, k in enumerate(keys)}),
            prior_strength=m)

    # ------------------------------------------------------------------ plan
    def enough_data(self, ws: WorkflowSnapshot) -> bool:
        """History exists, or at least `schedule_no_history_days` dialling days of our own."""
        pk = ws.pickup
        has = pk.dials > 0
        if np.any(pk.sim_day[has] < 0):
            return True
        return len(np.unique(pk.sim_day[has])) >= self.t.schedule_no_history_days

    def plan_hour_weights(self, ws: WorkflowSnapshot, curves: HourCurveSet) -> list[PatchRequest]:
        """One P3 patch per grid cohort whose table should change by more than the refresh
        threshold. Supervisor-owned cohorts and `default` are left alone."""
        if not self.enough_data(ws):
            return []
        hours = window_hours(ws)
        out: list[PatchRequest] = []
        for c in ws.cohorts.to_config():
            k = c.id
            if ws.cohorts.owner(k) != "ours" or k not in curves.curves:
                continue
            new = hour_weights_from_curve(curves.curves[k], hours, self.t)
            assert all(new[h] > 0 for h in hours)
            live_table = ws.live.hour_table(k)
            dist = normalised_l1(live_table, new, hours)
            if dist <= self.t.schedule_refresh_l1:
                continue
            out.append(PatchRequest(
                wf=ws.spec.workflow_id, owner=OWNER, path=("hour_weights", k),
                value=FrozenDict(new), cls=P3_ROUTINE, p0_rank=None,
                evidence=freeze({
                    "kind": "schedule", "cohort": k, "l1_vs_live": round(dist, 4),
                    "curve": {str(h): round(curves.curves[k][h], 4) for h in hours},
                    "dials": {str(h): curves.dials[k][h] for h in hours},
                    "kappa": self.t.hour_weight_kappa,
                    "clip": [self.t.hour_weight_min, self.t.hour_weight_max],
                    "needs_cohort_defined": k not in ws.live.cohort_ids()}),
                decision_id=0))
        return out


# =============================================================================== verification (5.8)

def _betabinom_cdf(k: int, n: int, a: float, b: float) -> float:
    """P(X <= k) for X ~ BetaBinomial(n, a, b)."""
    if k < 0:
        return 0.0
    k = min(k, n)
    base = math.lgamma(n + 1) - math.lgamma(a + b + n) + math.lgamma(a + b) - math.lgamma(a) \
        - math.lgamma(b)
    tot = 0.0
    for x in range(k + 1):
        tot += math.exp(base - math.lgamma(x + 1) - math.lgamma(n - x + 1)
                        + math.lgamma(x + a) + math.lgamma(n - x + b))
    return min(tot, 1.0)


def predictive_lower(n: int, p: float, strength: float, conf: float = 0.95) -> int:
    """Largest k with P(X < k) <= (1 - conf) / 2 under the Beta(p s, (1 - p) s) predictive:
    connects below it are outside the 95% posterior predictive band."""
    a, b = max(p * strength, 1e-6), max((1 - p) * strength, 1e-6)
    tail = (1.0 - conf) / 2.0
    k = 0
    while k <= n and _betabinom_cdf(k, n, a, b) <= tail:
        k += 1
    return k


def verify_schedule(ws: WorkflowSnapshot, curves: HourCurveSet, since_day: int,
                    did_factor: float = 1.0, t: Thresholds | None = None) -> list[Finding]:
    """The credible-band check of 5.8 on (cohort, hour) cells with >= 30 dials since the change
    (`since_day` onward, the 2 segments after it): fails if more than 10% fall below their band.
    `did_factor` is the current DID factor the band is multiplied by. Returns one Finding
    ("schedule", "verified" | "verify_failed")."""
    t = t or Thresholds()
    pk = ws.pickup
    sel = pk.sim_day >= since_day
    cells: dict[tuple[Cohort, int], list[int]] = {}
    for cell, h, d, c in zip(pk.cell[sel].tolist(), pk.hour[sel].tolist(), pk.dials[sel].tolist(),
                             pk.connected[sel].tolist()):
        key = (ws.cohorts.cohort_of(int(cell)), int(h))
        acc = cells.setdefault(key, [0, 0])
        acc[0] += d
        acc[1] += c
    tested = below = 0
    detail = []
    for (k, h), (d, c) in sorted(cells.items()):
        if d < t.schedule_verify_min_dials or k not in curves.curves or h not in curves.curves[k]:
            continue
        p = min(curves.curves[k][h] * did_factor, 0.95)
        strength = curves.dials[k][h] + curves.prior_strength
        lo = predictive_lower(d, p, strength)
        tested += 1
        if c < lo:
            below += 1
            detail.append({"cohort": k, "hour": h, "dials": d, "connected": c, "lower": lo})
    frac = below / tested if tested else 0.0
    kind = "verify_failed" if tested and frac > t.schedule_verify_fail_frac else "verified"
    return [Finding(wf=ws.spec.workflow_id, detector="schedule", kind=kind, scope="workflow",
                    stat=frac, threshold=t.schedule_verify_fail_frac,
                    first_evidence_minute=ws.now,
                    evidence=freeze({"cells_tested": tested, "below": detail}))]


__all__ = ["HourCurvesModel", "hour_weights_from_curve", "normalised_l1", "verify_schedule",
           "predictive_lower", "window_hours"]
