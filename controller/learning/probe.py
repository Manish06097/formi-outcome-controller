"""The probe-only learner: caller-ID health and probes (5.9) and per-cohort call-time weights
from history (5.8), the learner milestone M3 runs governance with (DESIGN.md 15.1 M3).

Owner: integration step I2. Pure: it reads `WorkflowSnapshot`s and returns DID states and
`PatchRequest`s; the loop (`loop/governance.py`) loads the snapshots, persists the states and
writes a decision row for each request before it enters the overlay. ADR 0029.

What it proposes, per workflow, at its planning point:
- caller ID: T4's `DidHealthModel.plan_caller_ids` (probes of at most two unknown DIDs at the
  probe share, an equal split over healthy DIDs once the SPRT clears them, nothing onto a DID
  classified unhealthy or degraded);
- cohorts and schedule: T4's D0 grid (`cohorts/grid.build_grid`) built from history only, and
  `HourCurvesModel` weights fitted on history only (so the table does not chase noise; a world
  without history gets no schedule change). The grid's cohort list is proposed only together
  with at least one hour table, because a cohort list alone changes nothing a lead hears or when
  it is dialled. Business cohorts stay first and verbatim (5.2), and a cohort list someone else
  changed is never replaced: then only the tables of our cohorts still defined are proposed.

Every request is class P3 (routine): the release train decides route and timing.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from controller.cohorts.cells import CellCatalog
from controller.cohorts.cohortset import GridCohortSet
from controller.cohorts.grid import GridInputs, build_grid
from controller.learning.did import DidHealthModel
from controller.learning.schedule import (HourCurvesModel, schedule_change_check,
                                          window_hours)
from controller.settings import Thresholds
from controller.types import (P3_ROUTINE, CohortDef, DidState, Finding, HourCurveSet, PatchRequest,
                              PickupArrays, WorkflowSnapshot, freeze, thaw)

OWNER_COHORTS = "cohorts.grid"


def history_pickup(pk: PickupArrays) -> PickupArrays:
    """The history rows of `r_pickup` (sim_day < 0)."""
    m = pk.sim_day < 0
    return PickupArrays(pk.sim_day[m].copy(), pk.hour[m].copy(), pk.cell[m].copy(),
                        pk.attempt_bucket[m].copy(), pk.dials[m].copy(), pk.connected[m].copy())


def history_pickup_rate(pk: PickupArrays) -> float | None:
    m = pk.sim_day < 0
    d = int(pk.dials[m].sum())
    return float(pk.connected[m].sum()) / d if d > 0 else None


@dataclass
class ProbeLearner:
    """One per run. Grids and DID models are built lazily per workflow from history, which never
    changes, so a restarted process rebuilds exactly the same ones."""

    t: Thresholds = field(default_factory=Thresholds)
    _grids: dict[str, GridCohortSet | None] = field(default_factory=dict)
    _did: dict[str, DidHealthModel] = field(default_factory=dict)
    _curves: dict[str, HourCurveSet] = field(default_factory=dict)

    # ------------------------------------------------------------------ caller ID (5.9)
    def did_model(self, ws: WorkflowSnapshot) -> DidHealthModel:
        wf = ws.spec.workflow_id
        if wf not in self._did:
            self._did[wf] = DidHealthModel(t=self.t, end_minute=ws.end_minute,
                                           p_hist=history_pickup_rate(ws.pickup))
        return self._did[wf]

    def did_bootstrap(self, ws: WorkflowSnapshot) -> dict[str, DidState]:
        """D0 states from history (healthy, degraded or unknown)."""
        return self.did_model(ws).bootstrap(ws)

    def did_step(self, ws: WorkflowSnapshot) -> tuple[dict[str, DidState], list[Finding]]:
        """New states from the persisted ones in `ws.did_state` and the dials since."""
        return self.did_model(ws).step(ws)

    # ------------------------------------------------------------------ cohorts and hours (5.2, 5.8)
    def grid(self, ws: WorkflowSnapshot, business: Sequence[CohortDef]) -> GridCohortSet | None:
        """The D0 grid of one workflow from history only; None when history gives no grid
        cohort (then there is no per-cohort schedule to propose)."""
        wf = ws.spec.workflow_id
        if wf not in self._grids:
            hist = history_pickup(ws.pickup)
            if len(hist) == 0 or int(hist.dials.sum()) == 0:
                self._grids[wf] = None
            else:
                cat = CellCatalog.from_rows(ws.cells[c] for c in sorted(ws.cells))
                g = build_grid(GridInputs(workflow_id=wf, catalog=cat, pickup=hist,
                                          hours=window_hours(ws),
                                          business_cohorts=tuple(business),
                                          end_minute=ws.end_minute), self.t)
                self._grids[wf] = g if any(e.origin == "grid" for e in g.entries) else None
        return self._grids[wf]

    def curves(self, ws: WorkflowSnapshot, business: Sequence[CohortDef]
               ) -> tuple[GridCohortSet, HourCurveSet] | None:
        """The workflow's grid and its hour curves from history only. History never changes, so
        both are computed once per process and a restarted process gets the same ones."""
        wf = ws.spec.workflow_id
        g = self.grid(ws, business)
        if g is None:
            return None
        if wf not in self._curves:
            hist_ws = dataclasses.replace(ws, pickup=history_pickup(ws.pickup), cohorts=g)
            self._curves[wf] = HourCurvesModel(self.t).fit(hist_ws)
        return g, self._curves[wf]

    def verify_schedule_change(self, ws: WorkflowSnapshot, business: Sequence[CohortDef],
                               applied_minute: int) -> Finding | None:
        """5.8's credible band on the 2 segments after an applied table (None = not yet). With
        no history grid there is no curve to judge by: the table was not ours to verify."""
        fitted = self.curves(ws, business)
        if fitted is None:
            return Finding(ws.spec.workflow_id, "schedule", "verified", "workflow", 0.0, 0.0,
                           ws.now, freeze({"note": "no history curves"}))
        g, curves = fitted
        return schedule_change_check(dataclasses.replace(ws, cohorts=g), curves, applied_minute,
                                     self.t)

    def plan_schedule(self, ws: WorkflowSnapshot, business: Sequence[CohortDef]
                      ) -> list[PatchRequest]:
        fitted = self.curves(ws, business)
        if fitted is None:
            return []
        g, curves = fitted
        live_ids = ws.live.cohort_ids()
        ours = g.to_config()
        deployed = tuple(ws.live.cohorts) == tuple(ours)
        untouched = tuple(ws.live.cohorts) == tuple(business)
        hist_ws = dataclasses.replace(ws, pickup=history_pickup(ws.pickup), cohorts=g)
        reqs = HourCurvesModel(self.t).plan_hour_weights(hist_ws, curves)
        if not deployed and not untouched:
            # someone else changed the cohort list: never replace it; tables of our cohorts
            # that are still defined only
            return [r for r in reqs if r.path[1] in live_ids]
        if not reqs:
            return []
        if deployed:
            return reqs
        cohorts = PatchRequest(
            wf=ws.spec.workflow_id, owner=OWNER_COHORTS, path=("cohorts",),
            value=tuple(c.body() for c in ours), cls=P3_ROUTINE, p0_rank=None,
            evidence=freeze({"kind": "cohorts", "set_key": g.key(),
                             "grid": {k: v for k, v in g.evidence.items()
                                      if k in ("f1", "f2", "n_business", "cap", "shares")},
                             "reason": "per-cohort call times need the cohorts defined (5.2)"}))
        return [cohorts] + reqs

    # ------------------------------------------------------------------ the plan
    def plan(self, ws: WorkflowSnapshot, business: Sequence[CohortDef]) -> list[PatchRequest]:
        """Every request of this planning point, caller ID first."""
        return list(self.did_model(ws).plan_caller_ids(ws)) + self.plan_schedule(ws, business)


def did_rows(states: Mapping[str, DidState], options: Sequence[str]) -> list[tuple]:
    """`did_state` rows (did code = index in the workflow's caller_id options)."""
    idx = {d: i for i, d in enumerate(options)}
    return [(idx[n], s.status, s.r_hat, s.r_lcb, s.r_ucb, int(s.n_dials), float(s.sprt_llr),
             s.since_minute, thaw(s.evidence)) for n, s in sorted(states.items()) if n in idx]


__all__ = ["ProbeLearner", "history_pickup", "history_pickup_rate", "did_rows", "OWNER_COHORTS"]

