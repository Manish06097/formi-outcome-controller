"""The pickup levers in the real loop: the flagged-DID detector, KPI drift and DID incidents with
their drain chains, over the run schema (milestone M4).

Owner: integration step I3. Implements, inside the orchestrator's stages, DESIGN.md 7.3 (the
multi-window DID detector, the P0b drain one `max_shift` step per stop, drain verification), 7.5
(the comparator-free KPI drift CUSUM, `KPI_DRIFT` digest items, a sole DID marked suspect so
probes create a comparator) and the persistence of 5.9's fast-path states. The statistics are
T4's (`incidents/detectors.py`, `incidents/manager.py`, `learning/did.py`); this module only
feeds them snapshots and writes what they decide. ADR 0030.

State: nothing lives only here. Incidents are rows of `incident`, fast-path DID states rows of
`did_state`, drift CUSUMs rows of `drift_monitor`; `restore` rebuilds the manager from them on
every start, and every stage writes in the same transaction as the decisions and alerts that
explain it, so a crash anywhere resumes into exactly the state a clean run holds.

Per step (called by `loop/governance.py: GovernanceHooks`):
  DETECT  `detect`: after the SPRT and slow path (the probe learner), the DID detector (alarm,
          suspect, clear) and the drift CUSUMs, then the incident manager's reading of both
  DECIDE  `drain_requests`: the next drain step of every draining incident, every stop (the
          release train auto-applies it, or submits a P0 approval where caller ID cannot
          auto-apply)
  VERIFY  `verify`: drain verification two dialling hours after the DID reached 0;
          `verify_change`: the kind verifier of an applied caller-ID or schedule proposal (6.1)
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any, Callable, Mapping

import numpy as np
from psycopg.types.json import Jsonb

from controller.audit.writer import write_decision
from controller.governance.lifecycle import VerifyResult
from controller.incidents.detectors import DidDetector, DriftDetector
from controller.incidents.manager import IncidentManagerImpl
from controller.learning.did import DidHealthModel, caller_change_check
from controller.obs.alerts import raise_alert
from controller.settings import Thresholds
from controller.store.repos import WorkflowCatalog
from controller.types import (Decision, DidState, DriftMonitorState, Finding, FrozenDict,
                              Incident, PatchRequest, Proposal, Tx, WorkflowSnapshot, freeze,
                              thaw)

DecisionFactory = Callable[..., Decision]
"""`(wf, module, kind, rule, inputs, outcome) -> Decision` (the hooks' `_decision`)."""


def json_safe(v: Any) -> Any:
    """A value Postgres `jsonb` accepts: numpy scalars as Python numbers, non-finite floats as
    None (jsonb has no Infinity or NaN), containers recursively."""
    v = thaw(v)
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


class PickupLevers:
    """DID incidents and KPI drift for every workflow of one run."""

    def __init__(self, t: Thresholds, catalog: WorkflowCatalog, decision: DecisionFactory):
        self.t = t
        self.catalog = catalog
        self.decision = decision
        self.did_detector = DidDetector(t)
        self.drift = DriftDetector(t)
        self.mgr = IncidentManagerImpl(runner=None, credits=None, did_model=DidHealthModel(t),
                                       llm=None, llm_enabled=False, t=t)

    # ------------------------------------------------------------------ restore
    def restore(self, tx: Tx) -> None:
        """Every incident of the run, from `incident` (on every start, before any stage)."""
        rows = tx.execute("""SELECT i.incident_id, w.workflow_id, i.kind, i.state,
                                    i.opened_minute, i.confirmed_minute, i.resolved_minute,
                                    i.evidence, i.fix_proposal_id, i.credits_spent
                             FROM incident i JOIN workflow w USING (wf)
                             ORDER BY i.incident_id""").fetchall()
        self.mgr.restore({r[0]: Incident(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                                         freeze(r[7] or {}), r[8], float(r[9] or 0.0))
                          for r in rows})

    # ------------------------------------------------------------------ DETECT
    def detect(self, tx: Tx, ws: WorkflowSnapshot, did_model: DidHealthModel
               ) -> dict[str, DidState]:
        """The DID detector and the drift CUSUMs on `ws` (whose `did_state` already holds this
        step's SPRT and slow-path states), read by the incident manager. Returns the DID states
        the manager changed (suspect, clear, fast-path degraded) for the caller to persist."""
        wf = ws.spec.workflow_id
        self.mgr.did_model = did_model
        self.mgr.did_overrides = {}
        findings = self.did_detector.detect(ws)
        drift_states = self._drift_states(tx, wf)
        new_drift, drift_findings = self.drift.update(ws, drift_states)
        self._write_drift(tx, wf, new_drift)
        for f in findings + drift_findings:
            if f.kind in ("alarm", "suspect", "clear") or f.detector == "drift":
                write_decision(tx, self.decision(
                    wf, f"incidents.detectors.{f.detector}", "detector",
                    f"{'7.3' if f.detector == 'did' else '7.5'}/{f.kind}",
                    {"scope": f.scope, "stat": f.stat, "threshold": f.threshold,
                     "evidence": json_safe(f.evidence)}, {}))
        before = {i.incident_id: i for i in self.mgr.incidents.values() if i.wf == wf}
        self.mgr.on_detections(ws, findings + drift_findings)
        changed = {did: st for (w, did), st in self.mgr.did_overrides.items() if w == wf}
        changed.update(self._expire_drift_suspects(tx, ws, changed))
        self._flush(tx, wf, before)
        return changed

    def _expire_drift_suspects(self, tx: Tx, ws: WorkflowSnapshot,
                               changed: Mapping[str, DidState]) -> dict[str, DidState]:
        """A sole DID marked suspect by a drift alarm (7.5) has no comparator to clear it, so
        the mark lapses after `suspect_defer_min`: by then the routine submits it deferred may
        go ahead, and the probes it asked for are planned. A drop that persists alarms the
        restarted CUSUM again."""
        wf = ws.spec.workflow_id
        out: dict[str, DidState] = {}
        for did, st in ws.did_state.items():
            st = changed.get(did, st)
            if st.status == "suspect" and st.evidence.get("suspect_by") == "drift" \
                    and st.since_minute is not None \
                    and ws.now - st.since_minute >= self.t.suspect_defer_min:
                out[did] = replace(st, status="healthy", since_minute=ws.now)
                write_decision(tx, self.decision(
                    wf, "incidents.manager", "did_state", "7.5/drift_suspect_lapsed",
                    {"did": did, "since": st.since_minute}, {"status": "healthy"}))
        return out

    def _drift_states(self, tx: Tx, wf: str) -> dict[str, DriftMonitorState]:
        rows = tx.execute("""SELECT kpi, cusum, last_block, baseline_kind, alarm, up_cusum
                             FROM drift_monitor WHERE wf = %s""",
                          (self.catalog.code(wf),)).fetchall()
        return {r[0]: DriftMonitorState(wf, r[0], float(r[1]),
                                        int(r[2]) if r[2] is not None else -1 << 30,
                                        r[3] or "history", bool(r[4]), float(r[5] or 0.0))
                for r in rows}

    def _write_drift(self, tx: Tx, wf: str, states: Mapping[str, DriftMonitorState]) -> None:
        for kpi, s in sorted(states.items()):
            tx.execute("""INSERT INTO drift_monitor (wf, kpi, cusum, up_cusum, last_block,
                                                     baseline_kind, alarm)
                          VALUES (%s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (wf, kpi) DO UPDATE SET cusum = excluded.cusum,
                              up_cusum = excluded.up_cusum, last_block = excluded.last_block,
                              baseline_kind = excluded.baseline_kind, alarm = excluded.alarm""",
                       (self.catalog.code(wf), kpi, float(s.cusum), float(s.up_cusum),
                        s.last_block if s.last_block > -(1 << 29) else None, s.baseline_kind,
                        bool(s.alarm)))

    # ------------------------------------------------------------------ DECIDE
    def drain_requests(self, tx: Tx, ws: WorkflowSnapshot, did_model: DidHealthModel
                       ) -> list[PatchRequest]:
        """The next P0b step of every draining incident of this workflow (none if nothing is
        draining). The caller writes a decision row per request before it enters the overlay."""
        wf = ws.spec.workflow_id
        self.mgr.did_model = did_model
        self.mgr.did_overrides = {}
        before = {i.incident_id: i for i in self.mgr.incidents.values() if i.wf == wf}
        reqs = self.mgr.drain_patches(ws)
        self._flush(tx, wf, before)
        return reqs

    def draining(self, wf: str) -> bool:
        return any(i.state == "draining" for i in self.mgr.open_incidents(wf, "did_degraded"))

    # ------------------------------------------------------------------ VERIFY
    def verify(self, tx: Tx, ws: WorkflowSnapshot) -> list[Finding]:
        wf = ws.spec.workflow_id
        if not any(i.state == "verifying" for i in self.mgr.open_incidents(wf, "did_degraded")):
            return []
        before = {i.incident_id: i for i in self.mgr.incidents.values() if i.wf == wf}
        out = [f for f in self.mgr.verify(ws) if f.detector == "incident"]
        self._flush(tx, wf, before)
        return out

    def verify_change(self, ws: WorkflowSnapshot, p: Proposal,
                      schedule: Callable[[int], Finding | None]) -> VerifyResult:
        """The kind check of one applied proposal (6.1): caller ID by 7.3, an hour table by 5.8's
        band, both when one proposal carried both. Pending until every part it carries is due.
        `schedule` runs the band check for this workflow from an apply minute."""
        dims = set((p.diff or {}).get("changed_dims") or ())
        at = p.applied_minute if p.applied_minute is not None else ws.now
        reasons: list[str] = []
        page = None
        failed = False
        if "caller_id" in dims:
            res = caller_change_check(ws, at, p.proposed_config, self.t)
            if res is None:
                return VerifyResult("pending")
            ok, ev = res
            reasons.append(f"caller_id {'ok' if ok else 'failed'}: {json.dumps(json_safe(ev))}")
            if not ok:
                failed, page = True, "DID_CHANGE_UNVERIFIED"
        if "schedule" in dims:
            f = schedule(at)
            if f is None:
                return VerifyResult("pending")
            ev = {"below_frac": round(float(f.stat), 4), **thaw(f.evidence)}
            reasons.append(f"schedule {f.kind}: {json.dumps(json_safe(ev))}")
            if f.kind == "verify_failed":
                failed, page = True, "SCHEDULE_UNVERIFIED"
        return VerifyResult("rollback" if failed else "verified", tuple(reasons), page)

    # ------------------------------------------------------------------ persistence
    def _flush(self, tx: Tx, wf: str, before: Mapping[str, Incident]) -> None:
        """Write the manager's decisions, alerts and changed incidents of `wf` in `tx`."""
        for d in self.mgr.decisions:
            write_decision(tx, self.decision(d["wf"], d["module"], d["kind"], d["rule_id"],
                                             json_safe(d["inputs"]), json_safe(d["outcome"])))
        for a in self.mgr.alerts:
            raise_alert(tx, a["code"], self.catalog.code(a["wf"]), a["wf"], a["message"],
                        sim_minute=a["sim_minute"])
        self.mgr.decisions, self.mgr.alerts = [], []
        for inc in self.mgr.incidents.values():
            if inc.wf == wf and before.get(inc.incident_id) != inc:
                self._write_incident(tx, inc)

    def _write_incident(self, tx: Tx, inc: Incident) -> None:
        tx.execute("""INSERT INTO incident (incident_id, wf, kind, state, opened_minute,
                                            confirmed_minute, resolved_minute, evidence,
                                            fix_proposal_id, credits_spent)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (incident_id) DO UPDATE SET state = excluded.state,
                          confirmed_minute = excluded.confirmed_minute,
                          resolved_minute = excluded.resolved_minute,
                          evidence = excluded.evidence,
                          fix_proposal_id = excluded.fix_proposal_id,
                          credits_spent = excluded.credits_spent""",
                   (inc.incident_id, self.catalog.code(inc.wf), inc.kind, inc.state,
                    inc.opened_minute, inc.confirmed_minute, inc.resolved_minute,
                    Jsonb(json_safe(inc.evidence)), inc.fix_proposal_id,
                    float(inc.credits_spent)))


def merged_states(ws: WorkflowSnapshot, changed: Mapping[str, DidState]) -> WorkflowSnapshot:
    """`ws` with the manager's DID state changes applied."""
    if not changed:
        return ws
    return replace(ws, did_state=FrozenDict({**dict(ws.did_state), **changed}))


__all__ = ["PickupLevers", "merged_states", "json_safe"]
