"""Engagement learning in the real loop: fits, allocation, winners, promotion, decay, guardrail,
and the harm gate on every composed change.

Owner: integration step I4 (milestone M5). Implements DESIGN.md 5.2 to 5.7 and 5.10 in the loop
(section 17 override 2: the Beta-binomial model by default, `CTL_ENGAGEMENT_MODEL` selects),
the 4.2 end-game freeze, 0.1 on every composed change before routing, the kind verifiers of 6.1
and the held-patch re-check. ADR 0031.

`GovernanceHooks` (loop/governance.py) owns one `EngagementLoop` and calls it from its stages;
this module owns everything engagement-specific so the integration edit there stays small.

Per step (every engagement-primary workflow that has welcome or voice options):
  DETECT  bookkeeping of learned state: an experiment whose dimension a supervisor edited is
          INTERRUPTED (5.2: data kept, re-planned with the new incumbent as control); a
          promotion whose patches applied makes its winner PROMOTED, its holdout ACTIVE and
          restarts the arm's decay monitor at its effect then (5.7); a holdout whose winner was
          rolled back ends. Then every completed half-window segment is folded into each
          monitored arm's Page CUSUM (idempotent by `seg_last`) and the rows are persisted.
  DECIDE  every step: harm stops (5.4, P1) and decay rollbacks timed to the apply time (5.7,
          P1), each from a fresh EW fit, each with its decision row and posterior snapshots.
          At the workflow's planning points (its routine slot minutes): the guardrail check
          (5.10; a breach attributed to our change reverts it as a P1, unattributable drift is a
          KPI_DRIFT digest), then winner looks (5.5, persisted, with futility and promotion with
          a holdout, 5.6), else the harm-gated allocation plan (5.4). Routine planning waits
          while one of our engagement patches is in flight.
  DECIDE (every workflow) `guardrail_filter`: the guardrail forecast runs before a pickup-lever
          patch of the probe learner enters the overlay, and the guardrail breach check runs at
          every planning point (5.10), pickup-primary workflows included.
  GOVERN  `gate(comp, now)`, called by the release train on every composed change before it is
          routed: population, cohort, caller-ID and guardrail lines (0.1). Patches on the
          failing lever are held and the rest is recomposed.
  LIFECYCLE `regate(p, why)`: the same gate on fresh data after a late decision or at a
          notice expiry (6.1, 6.3). `release_ok(patch)`: the same gate when a held patch is due
          for its re-check (6.1).
  VERIFY  `verifiers()`: allocation changes over the next 2 segments, schedule tables by the
          credible band, caller-ID changes recorded (6.1). A failed allocation or schedule
          verification reverts that change's increases as a P1 (`on_verify_failed`).

End-game freeze (4.2): from `freeze_start` no arm gains its first share (exploration stops);
in the last `freeze_min_days` no ramp, look or promotion is made; harm stops, rollbacks and
promotions already declared continue to the end.

Crash safety: everything one workflow decides at one minute is written in one transaction
(learned-state rows, decision rows, posterior snapshots, overlay patches). Each stage is
idempotent at a given minute (ADR 0029): looks are keyed by look number, decay folding by
`seg_last`, and a patch equal to a live one is never added twice.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from controller.audit.replay import encode_array, encode_inputs, hash_inputs
from controller.audit.writer import write_decision
from controller.governance.lifecycle import ArmObservation, ShareObservation, VerifyResult
from controller.governance.lifecycle import verify_allocation as check_allocation
from controller.learning.allocator import same_dist
from controller.learning import engagement_plan as ep
from controller.learning.connects import ConnectModel
from controller.learning.decay import PageDecayMonitor, segment_ratio
from controller.learning.engagement import (ENGAGEMENT_DIMS, CLOSED_EXPERIMENT_STATES, EffectCache,
                                            dim_arms)
from controller.learning.guardrail import MixGuardrailMonitor
from controller.learning.harm import GateContext
from controller.learning.probe import history_pickup
from controller.learning.schedule import HourCurvesModel, verify_schedule
from controller.learning.winners import POPULATION, promotion_config
from controller.learning.ws_codec import encode_snapshot
from controller.obs.alerts import raise_alert
from controller.planner.overlay import (APPLIED, LIVE_STATES, OPEN, PROPOSED, apply_patch,
                                         diff_paths)
from controller.snapshot import active_population, cohort_of_cells, segment_of
from controller.types import (DEFAULT_COHORT, P0_INCIDENT, P1_ROLLBACK, P2_PROMOTION, Composition,
                              Config,
                              Decision, EngageArrays, Experiment, FrozenDict, Patch, PatchRequest,
                              Proposal, Tx, WorkflowSnapshot, canonical_json, freeze, thaw)
from controller.world.clock import MINUTES_PER_DAY, expected_visible_minute, freeze_start

log = logging.getLogger(__name__)

OWN_OWNERS = frozenset({"learning.allocator", "learning.winners", "learning.decay",
                        "learning.guardrail"})
"""Patch owners of this module (allocator, promotions, rollbacks, guardrail reverts)."""
OWNER_ALLOCATOR = "learning.allocator"
OWNER_WINNERS = "learning.winners"
OWNER_DECAY = "learning.decay"
OWNER_GUARDRAIL = "learning.guardrail"
OWNER_SCHEDULE = "learning.schedule"
ENGAGEMENT_FAIL = ("population", "cohort")
DID_FAIL = ("did", "did_unmodeled")
UNPROVEN_DID = frozenset({"unknown", "probing", "degraded", "unhealthy", "suspect"})
"""A DID status with no evidence that it is at least as good as its peers (5.9)."""
VERIFY_MIN_CALLS = 100
"""Fewest calls in a (cohort, dim) before check (a) of 6.1 compares shares: 3 SE on a handful of
calls across many cohorts and arms pages falsely by multiplicity alone."""
Z90 = 1.645
"""One-sided 95% point: an arm fails (b) of 6.1 only when its whole 90% observation interval lies
below the dry run's, because two segments of a small arm are mostly sampling noise."""


class EngagementLoop:
    """The engagement learner of one controller process (state lives in Postgres)."""

    def __init__(self, hooks: Any):
        self.g = hooks
        self._gate_seen: dict[tuple[str, str, str], bool] = {}
        self._last_gate: dict[str, tuple[str, str, ep.GateVerdict, Any]] = {}

    def attach(self) -> None:
        rt = self.g.rt
        self.t = rt.settings.t
        self.model = rt.settings.env.engagement_model
        self._gate_seen.clear()

    # ------------------------------------------------------------------ helpers
    @property
    def now(self) -> int:
        return self.g.now

    def workflows(self) -> list[str]:
        """Engagement-primary workflows with welcome or voice options (5.3)."""
        out = []
        for wf in self.g.registry.workflow_ids():
            spec = self.g.rt.catalog.spec(wf)
            if spec.objective.primary == self.t.engagement_model_primary_kpi and any(
                    spec.options.get(d) for d in ENGAGEMENT_DIMS):
                out.append(wf)
        return out

    def _code(self, wf: str) -> int:
        return self.g.rt.catalog.code(wf)

    def _opt(self, ws: WorkflowSnapshot, dim: str, name: str) -> int:
        return list(ws.spec.options.get(dim, ())).index(name)

    def _decision(self, wf: str, module: str, kind: str, rule: str, inputs: Mapping[str, Any],
                  outcome: Mapping[str, Any] | None = None, *, harm: Any = None,
                  snapshot_ids: Sequence[int] = (), seed: int = 0) -> Decision:
        d = self.g._decision(wf, module, kind, rule, _plain(inputs), _plain(outcome or {}))
        return dataclasses.replace(d, harm_report=harm, snapshot_ids=tuple(snapshot_ids),
                                   seed=int(seed))

    def _store_fits(self, tx: Tx, ws: WorkflowSnapshot, fits: ep.Fits | None,
                    extra: Mapping[str, Any], seed: int = 0, with_pickup: bool = False
                    ) -> list[int]:
        """One posterior_snapshot row per fit, with the encoded snapshot as its inputs. Without
        fits (a gate that judged no engagement lever) one 'ew' row with an empty posterior
        keeps the snapshot and the seed, so the decision still replays."""
        model = fits.model if fits is not None else "none"
        arrays = encode_snapshot(ws, {**extra, "model": model}, with_pickup=with_pickup)
        blob, h = encode_inputs(arrays), hash_inputs(arrays)
        if fits is None:
            row = tx.execute(
                """INSERT INTO posterior_snapshot (wf, kind, inputs, inputs_hash, mean, summary,
                                                   seed, code_version, settings_hash)
                   VALUES (%s, 'ew', %s, %s, %s, %s, %s, %s, %s) RETURNING snapshot_id""",
                (self._code(ws.spec.workflow_id), blob, h, encode_array(np.zeros(0)),
                 _jsonb({"model": "none", **_plain(extra)}), int(seed),
                 self.g.rt.settings.env.code_version,
                 self.g.rt.settings.settings_hash())).fetchone()
            return [int(row[0])]
        ids = []
        for kind in sorted(fits.post):
            p = fits.post[kind]
            row = tx.execute(
                """INSERT INTO posterior_snapshot (wf, kind, inputs, inputs_hash, mean, cov,
                                                   summary, seed, code_version, settings_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING snapshot_id""",
                (self._code(ws.spec.workflow_id), kind, blob, h, encode_array(p.mean),
                 encode_array(p.cov) if p.cov is not None else None,
                 _jsonb({"model": fits.model, "fit": _plain(dict(p.summary)), **_plain(extra)}),
                 int(fits.seeds[kind]), self.g.rt.settings.env.code_version,
                 self.g.rt.settings.settings_hash())).fetchone()
            ids.append(int(row[0]))
        return ids

    def _wanted(self, req: PatchRequest) -> bool:
        """A live patch on the same path already carries this value (or, for routine work,
        the path is in flight): adding it again would change nothing."""
        want = canonical_json(thaw(req.value))
        for p in self.g.overlay.patches.values():
            if p.wf == req.wf and p.path == tuple(req.path) and p.state in LIVE_STATES and \
                    canonical_json(thaw(p.value)) == want:
                return True
        return False

    def _add(self, tx: Tx, reqs: Iterable[PatchRequest], decision_id: int) -> int:
        n = 0
        for r in reqs:
            if self._wanted(r):
                continue
            self.g.overlay.add(dataclasses.replace(r, decision_id=decision_id))
            n += 1
        return n

    def _pending(self, wf: str, owner: str) -> bool:
        """A P1 patch of this owner waits to ride, or rides, a proposal: judging another now
        would only replace it with one that differs in the third decimal."""
        return any(p.wf == wf and p.owner == owner and p.cls == P1_ROLLBACK
                   and p.state in (OPEN, PROPOSED) for p in self.g.overlay.patches.values())

    def _in_flight(self, wf: str) -> bool:
        """One of our engagement patches rides a proposal that is not terminal yet."""
        return any(p.wf == wf and p.owner in OWN_OWNERS and p.state == PROPOSED
                   for p in self.g.overlay.patches.values())

    def _t_apply(self, ws: WorkflowSnapshot, urgent: bool = False) -> int:
        """Expected apply minute of a change submitted now: the next stop for an auto-apply,
        else the expected visible decide minute of an approval (6.3)."""
        pol = ws.policy
        if urgent and pol.auto_enabled and pol.max_shift > 0 and \
                set(ENGAGEMENT_DIMS) <= set(pol.allowed_dims):
            return int(ws.now + pol.tick)
        return int(round(expected_visible_minute(ws.now, pol.latency_minutes,
                                                 pol.approver_hours, pol.tick)))

    def _freeze(self, ws: WorkflowSnapshot) -> tuple[bool, bool]:
        """(in the freeze: no new arms, in the last day: no ramps or promotions) (4.2)."""
        fs = freeze_start(ws.end_minute, self.t.freeze_min_days, self.t.freeze_frac)
        if fs is None or ws.end_minute is None:
            return False, False
        ramp_stop = ws.end_minute - int(self.t.freeze_min_days * MINUTES_PER_DAY)
        return ws.now >= fs, ws.now >= ramp_stop

    # ================================================================== DETECT
    def detect(self, now: int) -> None:
        for wf in self.workflows():
            with self.g.scope.tx() as tx:
                ws = self.g._snapshot(tx, wf)
                if self._bookkeeping(tx, ws):
                    ws = self.g._snapshot(tx, wf)
                self._fold_decay(tx, ws)

    def _bookkeeping(self, tx: Tx, ws: WorkflowSnapshot) -> bool:
        changed = self._interrupt_on_supervisor_edit(tx, ws)
        changed |= self._promotions_applied(tx, ws)
        changed |= self._holdouts_rolled_back(tx, ws)
        return changed

    def _interrupt_on_supervisor_edit(self, tx: Tx, ws: WorkflowSnapshot) -> bool:
        wf = ws.spec.workflow_id
        code = self._code(wf)
        out = False
        for e in ws.experiments:
            if e.state in CLOSED_EXPERIMENT_STATES:
                continue
            r = tx.execute("""SELECT min(version) FROM config_version
                              WHERE wf = %s AND version > %s AND actor NOT LIKE 'controller%%'
                                AND applied_minute IS NOT NULL AND %s = ANY(changed_dims)""",
                           (code, e.started_version, e.dim)).fetchone()
            if r is None or r[0] is None:
                continue
            v = int(r[0])
            tx.execute("""UPDATE experiment SET state = 'interrupted', interrupted_by = %s
                          WHERE exp_id = %s""", (v, e.exp_id))
            tx.execute("""UPDATE holdout SET state = 'ended' WHERE wf = %s AND dim = %s
                          AND state <> 'ended'""", (code, e.dim))
            did = write_decision(tx, self._decision(
                wf, "learning.allocator", "experiment", "5.2/interrupted",
                {"exp_id": e.exp_id, "dim": e.dim, "supervisor_version": v},
                {"state": "interrupted", "replanned_with": "the new incumbent as control"}))
            raise_alert(tx, "EXPERIMENT_INTERRUPTED", code, e.exp_id,
                        f"supervisor v{v} edited {e.dim} of {wf}: experiment {e.exp_id} "
                        f"interrupted (decision #{did}); the next plan starts a new one with "
                        "their choice as the control", sim_minute=self.now)
            out = True
        return out

    def _promotions_applied(self, tx: Tx, ws: WorkflowSnapshot) -> bool:
        wf = ws.spec.workflow_id
        code = self._code(wf)
        rows = tx.execute("""SELECT w.winner_id, w.scope, w.dim, w.arm FROM winner w
                             WHERE w.wf = %s AND w.state = 'declared'""", (code,)).fetchall()
        out = False
        for wid, scope, dim, arm_code in rows:
            ps = [p for p in self.g.overlay.patches.values()
                  if p.wf == wf and p.owner == OWNER_WINNERS
                  and (p.evidence or {}).get("winner_id") == wid]
            if not ps or not any(p.state == APPLIED for p in ps):
                continue
            arm = ws.spec.options[dim][int(arm_code)]
            tx.execute("UPDATE winner SET state = 'promoted' WHERE winner_id = %s", (wid,))
            tx.execute("""UPDATE holdout SET state = 'active', since_version = %s
                          WHERE wf = %s AND scope = %s AND dim = %s AND winner_id = %s""",
                       (ws.live_version, code, scope, dim, wid))
            fits = ep.fit(ws, self.model, self.t, "promotion_restart", kinds=("stationary",))
            st = PageDecayMonitor(self.t, ws.decay).restart(ws, scope, dim, arm, fits.stat)
            self._upsert_decay(tx, ws, [st])
            sids = self._store_fits(tx, ws, fits, {"purpose": "promotion_restart"})
            write_decision(tx, self._decision(
                wf, "learning.decay", "monitor_restart", "5.7/restart_at_promotion",
                {"winner_id": wid, "scope": scope, "dim": dim, "arm": arm},
                {"theta_ref": st.theta_ref, "seg_last": st.seg_last}, snapshot_ids=sids,
                seed=fits.seeds["stationary"]))
            out = True
        return out

    def _holdouts_rolled_back(self, tx: Tx, ws: WorkflowSnapshot) -> bool:
        """A promoted winner whose share in its scope fell below half (a rollback applied) is
        rolled back: its holdout ends, so it is no longer monitored as a winner."""
        out = False
        code = self._code(ws.spec.workflow_id)
        for h in ws.holdouts:
            if h.state != "active" or not h.winner_arm:
                continue
            key = DEFAULT_COHORT if h.scope == POPULATION else h.scope
            if ws.live.dist(key, h.dim).get(h.winner_arm, 0.0) >= 0.5:
                continue
            tx.execute("""UPDATE holdout SET state = 'ended' WHERE wf = %s AND scope = %s
                          AND dim = %s""", (code, h.scope, h.dim))
            tx.execute("""UPDATE winner SET state = 'rolled_back' WHERE wf = %s AND scope = %s
                          AND dim = %s AND state = 'promoted'""", (code, h.scope, h.dim))
            write_decision(tx, self._decision(
                ws.spec.workflow_id, "learning.winners", "holdout", "5.6/winner_rolled_back",
                {"scope": h.scope, "dim": h.dim, "arm": h.winner_arm},
                {"share_now": ws.live.dist(key, h.dim).get(h.winner_arm, 0.0)}))
            out = True
        return out

    def _fold_decay(self, tx: Tx, ws: WorkflowSnapshot) -> None:
        if not any(e.state not in CLOSED_EXPERIMENT_STATES for e in ws.experiments) and \
                not any(h.state == "active" for h in ws.holdouts):
            return
        mon = PageDecayMonitor(self.t, ws.decay)
        seg = last_complete_segment(ws.now, ws.spec.dialling_window)
        known = {(s.scope, s.dim, s.arm) for s in ws.decay}
        need = [m for m in mon.monitored(ws) if (m[0], m[1], m[2]) not in known]
        fits = ep.fit(ws, self.model, self.t, "decay_ref", kinds=("stationary",)) \
            if need else None
        findings = mon.update(ws, seg, fits.stat if fits else None)
        before = {(s.scope, s.dim, s.arm): s for s in ws.decay}
        changed = [s for s in mon.states() if before.get((s.scope, s.dim, s.arm)) != s]
        if changed:
            self._upsert_decay(tx, ws, changed)
        if findings:
            sids = self._store_fits(tx, ws, fits, {"purpose": "decay_ref"}) if fits else []
            for f in findings:
                write_decision(tx, self._decision(
                    ws.spec.workflow_id, "learning.decay", "decay_alarm", "5.7/cusum",
                    {"scope": f.scope, "segment": seg, "threshold": f.threshold},
                    {"cusum": f.stat, **thaw(f.evidence)}, snapshot_ids=sids))

    def _upsert_decay(self, tx: Tx, ws: WorkflowSnapshot, states: Iterable[Any]) -> None:
        code = self._code(ws.spec.workflow_id)
        for s in states:
            tx.execute("""INSERT INTO decay_monitor (wf, scope, dim, arm, cusum, seg_last,
                                                      theta_ref, alarm, seg_first)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (wf, scope, dim, arm) DO UPDATE SET cusum = excluded.cusum,
                              seg_last = excluded.seg_last, theta_ref = excluded.theta_ref,
                              alarm = excluded.alarm, seg_first = excluded.seg_first""",
                       (code, s.scope, s.dim, self._opt(ws, s.dim, s.arm), float(s.cusum),
                        int(s.seg_last), float(s.theta_ref), bool(s.alarm),
                        None if s.seg_first is None else int(s.seg_first)))

    # ================================================================== DECIDE
    def decide(self, now: int) -> int:
        n = 0
        ours = self.workflows()
        for wf in self.g.registry.workflow_ids():
            planning = self.g.is_planning_point(wf, now)
            if wf not in ours:      # pickup-primary: no experiments, the guardrail still applies
                if planning:
                    with self.g.scope.tx() as tx:
                        n += self._guardrail(tx, self.g._snapshot(tx, wf))
                continue
            with self.g.scope.tx() as tx:
                ws = self.g._snapshot(tx, wf)
                if not ws.experiments and not planning:
                    continue
                n += self._urgent(tx, ws)
                if planning:
                    n += self._plan(tx, ws)
        return n

    def guardrail_filter(self, tx: Tx, ws: WorkflowSnapshot, reqs: Sequence[PatchRequest]
                         ) -> list[PatchRequest]:
        """AUDIT-1 finding 3: the forecast before a pickup-lever patch enters the overlay, on
        every workflow. A refused request is a recorded decision not to act."""
        f = ep.filter_by_guardrail(ws, reqs, self.t)
        if f.dropped or f.exempt:
            wf = ws.spec.workflow_id
            kpi = ws.spec.objective.guardrail_kpi
            did = write_decision(tx, self._decision(
                wf, "learning.guardrail", "no_action", "5.10/forecast",
                {"kpi": kpi, "max_drop": ws.spec.objective.guardrail_max_drop,
                 "tolerance": self.t.guardrail_drop_tol},
                {"dropped": [{"path": list(pa), "drop": d, "p_breach": b}
                             for pa, d, b in f.dropped],
                 "exempt_p0": [{"path": list(pa), "drop": d, "p_breach": b}
                               for pa, d, b in f.exempt],
                 "kept": [list(r.path) for r in f.keep]}))
            if f.dropped:
                raise_alert(tx, "HARM_GATE_BLOCK", self._code(wf), f"{wf}:guardrail",
                            f"{wf}: guardrail forecast ({kpi}) refused {len(f.dropped)} "
                            f"pickup-lever change(s) (decision #{did})", sim_minute=self.now)
        return list(f.keep)

    def _exposed(self, ws: WorkflowSnapshot) -> bool:
        """Some non-control option of an open experiment has share above the arm floor."""
        for e in ws.experiments:
            if e.state in CLOSED_EXPERIMENT_STATES:
                continue
            for k in [*ws.live.cohort_ids(), DEFAULT_COHORT]:
                d = ws.live.dist(k, e.dim)
                if any(v > self.t.arm_floor + 1e-9 for o, v in d.items() if o != e.control):
                    return True
        return any(s.alarm for s in ws.decay)

    def _urgent(self, tx: Tx, ws: WorkflowSnapshot) -> int:
        """Harm stops and decay rollbacks (every step, 5.4 and 5.7)."""
        if not self._exposed(ws):
            return 0
        fits = ep.fit(ws, self.model, self.t, "urgent", kinds=("ew",))
        n = 0
        wf = ws.spec.workflow_id
        all_stops = [] if self._pending(wf, OWNER_ALLOCATOR) else ep.run_harm_stops(
            ws, fits.ew, self.t)
        # the stored outcome is the rule's whole output (what a replay recomputes); only the
        # requests nobody wants yet are added
        stops = [r for r in all_stops if not self._wanted(r)]
        if stops:
            sids = self._store_fits(tx, ws, fits, {"purpose": "urgent"})
            did = write_decision(tx, self._decision(
                wf, "learning.allocator", "harm_stop", "5.4/harm_stop", {"now": ws.now},
                {"patches": ep.patches_json(all_stops),
                 "stopped": thaw(all_stops[0].evidence).get("stopped", [])},
                snapshot_ids=sids, seed=fits.seeds["ew"]))
            n += self._add(tx, stops, did)
        if any(s.alarm for s in ws.decay) and not self._pending(wf, OWNER_DECAY):
            ta = self._t_apply(ws, urgent=True)
            all_rbs = ep.run_rollbacks(ws, fits.ew, self.t, ta)
            rbs = [r for r in all_rbs if not self._wanted(r)]
            if rbs:
                sids = self._store_fits(tx, ws, fits, {"purpose": "urgent", "t_apply": ta})
                did = write_decision(tx, self._decision(
                    wf, "learning.decay", "rollback", "5.7/rollback",
                    {"now": ws.now, "t_apply": ta},
                    {"patches": ep.patches_json(all_rbs),
                     "rollbacks": thaw(all_rbs[0].evidence).get("rollbacks", [])},
                    snapshot_ids=sids, seed=fits.seeds["ew"]))
                n += self._add(tx, rbs, did)
        return n

    # ------------------------------------------------------------------ planning point
    def _plan(self, tx: Tx, ws: WorkflowSnapshot) -> int:
        wf = ws.spec.workflow_id
        n = self._guardrail(tx, ws)
        in_freeze, ramp_stop = self._freeze(ws)
        if self._in_flight(wf):
            write_decision(tx, self._decision(
                wf, "learning.allocator", "allocation", "5.4/wait_in_flight", {"now": ws.now},
                {"action": "wait", "reason": "an engagement change of ours is in flight"}))
            return n
        ta = self._t_apply(ws)
        fits = ep.fit(ws, self.model, self.t, "plan")
        sids: list[int] | None = None

        def snapshots() -> list[int]:
            nonlocal sids
            if sids is None:
                sids = self._store_fits(tx, ws, fits, {"purpose": "plan", "t_apply": ta})
            return sids

        promoted = False
        if not ramp_stop:
            promoted, k = self._looks(tx, ws, fits, ta, snapshots)
            n += k
            if not promoted:
                n += self._redo_promotions(tx, ws, snapshots, fits)
        if promoted:
            return n
        pws = ep.frozen_view(ws) if in_freeze else ws
        plan = ep.run_allocation(pws, fits, self.t, ta)
        if plan.new_experiments:
            self._persist_experiments(tx, ws, plan.new_experiments)
        kind = plan.kind
        reqs = list(plan.patches)
        if ramp_stop and kind != "risk_reducing":
            reqs, kind = [], f"{kind}_blocked_by_freeze"
        did = write_decision(tx, self._decision(
            wf, "learning.allocator", "allocation", "5.4/plan_allocation",
            {"now": ws.now, "t_apply": ta, "in_freeze": in_freeze, "ramp_stop": ramp_stop},
            {**ep.allocation_outcome(plan), "action": kind},
            harm=plan.harm, snapshot_ids=snapshots(), seed=fits.seeds["ew"]))
        if reqs:
            n += self._add(tx, reqs, did)
            for a in plan.removed:
                raise_alert(tx, "ARM_REMOVED", self._code(wf), f"{a[1]}:{a[2]}@{a[0]}",
                            f"{wf}: {a[1]} option {a[2]} removed in {a[0]} "
                            "(P(theta < 0) >= 0.8)", sim_minute=self.now)
        return n

    def _persist_experiments(self, tx: Tx, ws: WorkflowSnapshot,
                             exps: Sequence[Experiment]) -> None:
        code = self._code(ws.spec.workflow_id)
        for e in exps:
            tx.execute("""INSERT INTO experiment (exp_id, wf, dim, arms, control,
                                                  cohort_set_version, state, started_version,
                                                  started_minute)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (exp_id) DO NOTHING""",
                       (e.exp_id, code, e.dim, [self._opt(ws, e.dim, a) for a in e.arms],
                        self._opt(ws, e.dim, e.control), e.cohort_set_version, e.state,
                        e.started_version, e.started_minute))

    # ------------------------------------------------------------------ looks and promotion
    def _looks(self, tx: Tx, ws: WorkflowSnapshot, fits: ep.Fits, ta: int,
               snapshots: Any) -> tuple[bool, int]:
        if not any(e.state not in CLOSED_EXPERIMENT_STATES for e in ws.experiments):
            return False, 0
        out = ep.run_looks(ws, fits, self.t, ta)
        if not out.looks:
            return False, 0
        wf = ws.spec.workflow_id
        code = self._code(wf)
        did = write_decision(tx, self._decision(
            wf, "learning.winners", "looks", "5.5/evaluate_looks", {"now": ws.now, "t_apply": ta},
            {**ep.looks_outcome(out), "detail": [dict(d) for d in out.detail]},
            snapshot_ids=snapshots(), seed=fits.seeds["ew"]))
        exps = {e.exp_id: e for e in ws.experiments}
        for x in out.looks:
            dim = exps[x.exp_id].dim
            tx.execute("""INSERT INTO winner_look (exp_id, arm, scope, look_no, n_at_look,
                                                   sim_minute, w1, w2, w3, passed, decision_id)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT DO NOTHING""",
                       (x.exp_id, self._opt(ws, dim, x.arm), x.scope, x.look_no, x.n_at_look,
                        x.sim_minute, x.w1, x.w2, x.w3, x.passed, did))
        for exp_id, arm, scope in out.futile:
            self._arm_state(tx, ws, exps[exp_id], arm, scope, "futile", did)
            raise_alert(tx, "FUTILITY_DROP", code, f"{exp_id}:{arm}@{scope}",
                        f"{wf}: {arm} dropped for futility in {scope} (P(theta > ln 1.02) <= "
                        f"{self.t.futility_p})", sim_minute=self.now)
        n = 0
        promoted = False
        dims_done: set[str] = set()
        for w in sorted(out.winners, key=lambda w: (w.scope != POPULATION, w.dim, w.scope)):
            if w.dim in dims_done:
                continue
            dims_done.add(w.dim)
            n += self._declare(tx, ws, exps[w.exp_id], w, out.promotions[(w.dim, w.scope)], did)
            promoted = True
        return promoted, n

    def _arm_state(self, tx: Tx, ws: WorkflowSnapshot, e: Experiment, arm: str, scope: str,
                   state: str, decision_id: int) -> None:
        tx.execute("""INSERT INTO experiment_arm (exp_id, arm, scope, state, share,
                                                  removed_minute, decision_id)
                      VALUES (%s, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (exp_id, arm, scope) DO UPDATE SET state = excluded.state,
                          removed_minute = excluded.removed_minute,
                          decision_id = excluded.decision_id""",
                   (e.exp_id, self._opt(ws, e.dim, arm), scope, state, 0.0,
                    ws.now if state in ("removed", "futile") else None, decision_id))

    def _declare(self, tx: Tx, ws: WorkflowSnapshot, e: Experiment, w: Any, promo: Config,
                 look_decision: int) -> int:
        """5.5 winner declared: winner row, arm states, a pending holdout, promotion patches."""
        wf = ws.spec.workflow_id
        code = self._code(wf)
        key = DEFAULT_COHORT if w.scope == POPULATION else w.scope
        h = float(dict(promo.dist(key, w.dim)).get(e.control, 0.0))
        row = tx.execute("""INSERT INTO winner (exp_id, wf, dim, arm, scope, look_no,
                                                declared_minute, decision_id, state)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'declared')
                            RETURNING winner_id""",
                         (e.exp_id, code, w.dim, self._opt(ws, w.dim, w.arm), w.scope,
                          w.look_no, ws.now, look_decision)).fetchone()
        wid = int(row[0])
        self._arm_state(tx, ws, e, w.arm, w.scope, "winner", look_decision)
        for a in e.arms:
            if a not in (w.arm, e.control):
                self._arm_state(tx, ws, e, a, w.scope, "removed", look_decision)
        tx.execute("""INSERT INTO holdout (wf, scope, dim, winner_id, control, share, state)
                      VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                      ON CONFLICT (wf, scope, dim) DO UPDATE SET winner_id = excluded.winner_id,
                          control = excluded.control, share = excluded.share,
                          state = 'pending', since_version = NULL""",
                   (code, w.scope, w.dim, wid, self._opt(ws, w.dim, e.control), h))
        reqs = self._promotion_patches(ws, promo, wid, w.dim, w.scope, w.arm, h)
        did = write_decision(tx, self._decision(
            wf, "learning.winners", "promotion", "5.6/promote_with_holdout",
            {"winner_id": wid, "look_decision": look_decision, "scope": w.scope, "dim": w.dim,
             "arm": w.arm}, {"holdout": h, "patches": ep.patches_json(reqs)},
            harm=w.harm))
        return self._add(tx, reqs, did)

    def _promotion_patches(self, ws: WorkflowSnapshot, promo: Config, wid: int, dim: str,
                           scope: str, arm: str, h: float) -> list[PatchRequest]:
        out = []
        for k in [*ws.live.cohort_ids(), DEFAULT_COHORT]:
            a = (ws.live.allocations.get(k) or {}).get(dim)
            b = (promo.allocations.get(k) or {}).get(dim)
            if same_dist(a, b):
                continue
            out.append(PatchRequest(
                wf=ws.spec.workflow_id, owner=OWNER_WINNERS, path=("alloc", k, dim),
                value=FrozenDict(b) if b is not None else None, cls=P2_PROMOTION, p0_rank=None,
                evidence=freeze({"kind": "promotion", "winner_id": wid, "arm": arm,
                                 "scope": scope, "holdout": h})))
        return out

    def _redo_promotions(self, tx: Tx, ws: WorkflowSnapshot, snapshots: Any,
                         fits: ep.Fits) -> int:
        """A declared winner whose promotion patches were lost (rejected, stale, dropped) is
        proposed again at the next planning point; the composed-change gate re-judges it."""
        wf = ws.spec.workflow_id
        code = self._code(wf)
        n = 0
        rows = tx.execute("""SELECT w.winner_id, w.scope, w.dim, w.arm, h.share, h.control
                             FROM winner w JOIN holdout h USING (winner_id)
                             WHERE w.wf = %s AND w.state = 'declared' AND h.state = 'pending'""",
                          (code,)).fetchall()
        for wid, scope, dim, arm_code, share, ctrl in rows:
            live_or_applied = [p for p in self.g.overlay.patches.values()
                               if p.wf == wf and p.owner == OWNER_WINNERS
                               and (p.evidence or {}).get("winner_id") == wid
                               and (p.state in LIVE_STATES or p.state == APPLIED)]
            if live_or_applied:
                continue
            arm = ws.spec.options[dim][int(arm_code)]
            control = ws.spec.options[dim][int(ctrl)]
            promo = promotion_config(ws.live, dim, arm, control, scope, float(share))
            reqs = self._promotion_patches(ws, promo, int(wid), dim, scope, arm, float(share))
            if not reqs:
                continue
            did = write_decision(tx, self._decision(
                wf, "learning.winners", "promotion", "5.6/promote_again",
                {"winner_id": int(wid), "scope": scope, "dim": dim, "arm": arm},
                {"holdout": float(share), "patches": ep.patches_json(reqs)}))
            n += self._add(tx, reqs, did)
        return n

    # ------------------------------------------------------------------ guardrail (5.10)
    def _guardrail(self, tx: Tx, ws: WorkflowSnapshot) -> int:
        findings = MixGuardrailMonitor(self.t).check(ws)
        n = 0
        wf = ws.spec.workflow_id
        code = self._code(wf)
        for f in findings:
            ev = thaw(f.evidence)
            if f.kind == "kpi_drift":
                did = write_decision(tx, self._decision(
                    wf, "learning.guardrail", "guardrail", "5.10/kpi_drift", {"now": ws.now},
                    {"kind": f.kind, **ev}))
                raise_alert(tx, "KPI_DRIFT", code, f"guardrail:{ev.get('kpi')}",
                            f"{wf}: {ev.get('kpi')} fell {ev.get('drop', 0):.3f} (lower bound "
                            f"{ev.get('lcb', 0):.3f}) with no change of ours to blame "
                            f"(decision #{did}); nothing changed", sim_minute=self.now)
                continue
            if self._reverted_recently(tx, code, ws.now):
                write_decision(tx, self._decision(
                    wf, "learning.guardrail", "guardrail", "5.10/breach_wait", {"now": ws.now},
                    {"kind": f.kind, **ev, "reason": "a breach revert is inside the window: the "
                     "window still holds the data that caused it"}))
                continue
            reqs = self._revert(tx, ws, int(ev["version"]))
            did = write_decision(tx, self._decision(
                wf, "learning.guardrail", "guardrail", "5.10/breach_rollback", {"now": ws.now},
                {"kind": f.kind, **ev, "patches": ep.patches_json(reqs)}))
            n += self._add(tx, reqs, did)
        return n

    def _reverted_recently(self, tx: Tx, code: int, now: int) -> bool:
        """A breach revert inside the rolling window: the revert is itself our latest change, so
        without this the next planning point would attribute the same drop to it and undo it."""
        r = tx.execute("""SELECT 1 FROM decision WHERE wf = %s AND sim_minute > %s
                          AND rule_id = '5.10/breach_rollback' LIMIT 1""",
                       (code, now - self.t.guardrail_window_days * MINUTES_PER_DAY)).fetchone()
        return r is not None

    def _revert(self, tx: Tx, ws: WorkflowSnapshot, version: int) -> list[PatchRequest]:
        """P1 patches that put every allocation and hour-table path our version changed back to
        the body before it (the change a guardrail breach is attributed to)."""
        wf = ws.spec.workflow_id
        before = self.g.rt.mirror.body(wf, version - 1, tx)
        after = self.g.rt.mirror.body(wf, version, tx)
        if before is None or after is None:
            return []
        out = []
        for path in diff_paths(before, after):
            if path[0] == "hour_weights" and path[1] in ws.live.cohort_ids():
                t = before.hour_weights.get(path[1])
                out.append(PatchRequest(
                    wf=wf, owner=OWNER_GUARDRAIL, path=path,
                    value=FrozenDict(t) if t is not None else None, cls=P1_ROLLBACK, p0_rank=None,
                    evidence=freeze({"kind": "guardrail_rollback", "version": version})))
                continue
            if path[0] != "alloc" or path[1] not in (*ws.live.cohort_ids(), DEFAULT_COHORT):
                continue
            v = (before.allocations.get(path[1]) or {}).get(path[2])
            if v is None and path[1] == DEFAULT_COHORT:
                continue
            out.append(PatchRequest(
                wf=wf, owner=OWNER_GUARDRAIL, path=path,
                value=FrozenDict(v) if v is not None else None, cls=P1_ROLLBACK, p0_rank=None,
                evidence=freeze({"kind": "guardrail_rollback", "version": version})))
        return out

    # ================================================================== the composed gate
    def gate(self, comp: Composition, now: int) -> tuple[list[int], str] | None:
        """0.1 on one composed change before routing. None when it passes; else the patch ids
        to hold (the lever that fails) and the reason. P0 incident work is judged and
        recorded but never held here (prompt safety and DID drains come first)."""
        if not comp.changed_dims:
            return None
        alpha = ep.alpha_for(comp.cls, self.t)
        v, ws, fits, ta = self._judge(comp.wf, comp.live, comp.proposed, alpha, comp.cls)
        self._last_gate[comp.wf] = (comp.proposed.hash(), comp.live.hash(), v, fits)
        hold = self._to_hold(comp.patches, comp.cls, v, ws, comp.live, comp.proposed)
        key = (comp.wf, comp.proposed.hash(), comp.live.hash())
        if self._gate_seen.get(key) != v.passes:
            self._gate_seen[key] = v.passes
            did_seed = ep.seed_for(ws, "gate", "did")
            with self.g.scope.tx() as tx:
                sids = self._store_fits(
                    tx, ws, fits, {"purpose": "gate", "cur": comp.live.stored(),
                                   "new": comp.proposed.as_stored().stored(), "alpha": alpha,
                                   "t_apply": ta},
                    seed=did_seed, with_pickup=True)
                did = write_decision(tx, self._decision(
                    comp.wf, "learning.harm", "harm_gate", "0.1/harm_gate",
                    {"class": comp.cls, "patch_ids": [p.patch_id for p in comp.patches],
                     "changed_dims": list(comp.changed_dims), "alpha": alpha, "t_apply": ta},
                    {**v.outcome(), "held": hold}, harm=v.report, snapshot_ids=sids,
                    seed=fits.seeds["ew"] if fits else did_seed))
                if hold:
                    raise_alert(tx, "HARM_GATE_BLOCK", self._code(comp.wf),
                                f"{comp.wf}:{comp.proposed.hash()[:8]}",
                                f"{comp.wf}: the composed change failed {list(v.report.failed)} "
                                f"(decision #{did}); patches {hold} are held", sim_minute=now)
        if not hold:
            return None
        return hold, f"harm gate failed {list(v.report.failed)}"

    def _to_hold(self, patches: Sequence[Patch], cls: int, v: ep.GateVerdict,
                 ws: WorkflowSnapshot, live: Config, new: Config) -> list[int]:
        """The patches on the failing lever. An engagement line holds welcome and voice; the
        guardrail line holds the pickup levers when the change has any (only they move who
        connects), else welcome and voice; the caller-ID line holds caller ID when the change
        adds share to a DID nothing has proved yet. Nothing for P0 incident work."""
        if v.passes or cls == P0_INCIDENT:
            return []
        failed = [f.removeprefix("apply_time:") for f in v.report.failed]

        def of(dims: tuple[str, ...]) -> set[int]:
            return {p.patch_id for p in patches if p.path[0] == "alloc" and p.path[2] in dims}

        pickup = of(("caller_id",)) | {p.patch_id for p in patches
                                       if p.path[0] in ("hour_weights", "cohorts")}
        hold: set[int] = set()
        if any(f.split(":")[0] in ENGAGEMENT_FAIL for f in failed):
            hold |= of(ENGAGEMENT_DIMS)
        if "guardrail" in failed:
            hold |= pickup or of(ENGAGEMENT_DIMS)
        if any(f in DID_FAIL for f in failed) and self._adds_unproven_did(ws, live, new):
            hold |= of(("caller_id",))
        return sorted(hold)

    @staticmethod
    def _adds_unproven_did(ws: WorkflowSnapshot, live: Config, new: Config) -> bool:
        """The change raises some DID's share where its state gives no evidence for it. The
        caller-ID line is recorded on every change, but it holds only these: the posterior of a
        DID the SPRT cleared is wide (P(dd < -2%) about 0.48 for an ordinary spread), and
        enforcing it there held almost every spread and cost about 2% pickup on public_01 (run
        runs/i4/smoke1, ADR 0031). Step I3's own DID gate owns that calibration."""
        for k in [*new.cohort_ids(), DEFAULT_COHORT]:
            before = live.dist(k, "caller_id") if k in live.cohort_ids() or k == DEFAULT_COHORT \
                else live.dist(DEFAULT_COHORT, "caller_id")
            for d, x in new.dist(k, "caller_id").items():
                st = ws.did_state.get(d)
                status = st.status if st is not None else "unknown"
                if x > before.get(d, 0.0) + 1e-9 and status in UNPROVEN_DID:
                    return True
        return False

    def _judge(self, wf: str, live: Config, new: Config, alpha: float, cls: int
               ) -> tuple[ep.GateVerdict, WorkflowSnapshot, ep.Fits | None, int]:
        with self.g.scope.tx() as tx:
            ws = self.g._snapshot(tx, wf)
        fits = None
        if wf in self.workflows() and set(live.changed_dims(new)) & set(ENGAGEMENT_DIMS):
            # the draws the planner of this class used: rollbacks and harm stops are planned in
            # the urgent stage, everything else at the planning point. Other draws would make
            # a plan that sits at the edge of its own gate fail it by Monte Carlo noise.
            purpose = "urgent" if cls <= P1_ROLLBACK else "plan"
            fits = ep.fit(ws, self.model, self.t, purpose, kinds=("ew",))
        ta = self._t_apply(ws, urgent=cls <= P1_ROLLBACK)
        v = ep.run_gate(ws, fits.ew if fits else None, live, new, self.t, alpha,
                        ep.seed_for(ws, "gate", "did"), ta)
        return v, ws, fits, ta

    def regate(self, p: Proposal, why: str) -> bool:
        """6.1 and 6.3: the gate again on fresh data, after a late decision or at a notice
        expiry, against the config live now."""
        if p.cls == P0_INCIDENT:
            return True
        _, live = self.g._live(p.wf)
        if not live.changed_dims(p.proposed_config):
            return True
        alpha = ep.alpha_for(p.cls, self.t)
        v, ws, _, ta = self._judge(p.wf, live, p.proposed_config, alpha, p.cls)
        ok = self._lever_lines_pass(v, ws, live, p.proposed_config)
        with self.g.scope.tx() as tx:
            write_decision(tx, self._decision(
                p.wf, "learning.harm", "harm_gate", f"0.1/regate_{why}",
                {"proposal_id": p.proposal_id, "class": p.cls, "alpha": alpha, "t_apply": ta},
                {**v.outcome(), "ok": ok}, harm=v.report))
        return ok

    def _lever_lines_pass(self, v: ep.GateVerdict, ws: WorkflowSnapshot, live: Config,
                          new: Config) -> bool:
        """The gate passes, a caller-ID line on proven DIDs aside (see `_adds_unproven_did`)."""
        failed = [f.removeprefix("apply_time:") for f in v.report.failed]
        return v.passes or (all(f in DID_FAIL for f in failed)
                            and not self._adds_unproven_did(ws, live, new))

    def release_ok(self, p: Patch) -> bool:
        """AUDIT-1 finding 9: the re-check of a patch whose hold has run out (6.1): the same
        gate and guardrail forecast on today's data, against the config live now. Failing it
        holds the patch again, and after `held_patch_max_holds` holds it is dropped."""
        if p.cls == P0_INCIDENT:
            return True
        _, live = self.g._live(p.wf)
        try:
            new = apply_patch(live, tuple(p.path), thaw(p.value))
        except ValueError:
            return True         # its cohort left the config: compose skips it with a reason
        if not live.changed_dims(new):
            return True
        alpha = ep.alpha_for(p.cls, self.t)
        v, ws, _, ta = self._judge(p.wf, live, new, alpha, p.cls)
        ok = self._lever_lines_pass(v, ws, live, new)
        with self.g.scope.tx() as tx:
            write_decision(tx, self._decision(
                p.wf, "learning.harm", "harm_gate", "0.1/recheck_held",
                {"patch_id": p.patch_id, "path": list(p.path), "class": p.cls, "alpha": alpha,
                 "t_apply": ta, "hold_count": p.hold_count},
                {**v.outcome(), "ok": ok}, harm=v.report))
        return ok

    # ================================================================== VERIFY (6.1)
    def verifiers(self) -> dict[str, Any]:
        return {"allocation": self._verify_allocation, "schedule": self._verify_schedule,
                "caller_id": self._verify_caller_id,
                "none": lambda p, now: VerifyResult("verified", ("cohort list only",))}

    def _verification_segments(self, ws: WorkflowSnapshot, p: Proposal) -> list[int] | None:
        """The next `alloc_verify_segments` whole segments after the apply, once they are over;
        None while they are not."""
        window = ws.spec.dialling_window
        first = segment_of(int(p.applied_minute), window) + 1
        last = first + self.t.alloc_verify_segments - 1
        return None if last_complete_segment(ws.now, window) < last else list(range(first, last + 1))

    def _verify_allocation(self, p: Proposal, now: int) -> VerifyResult:
        """6.1 allocation check. (a) realised shares against configured ones, on calls made
        under this version (on `next_campaign_only` that is new campaigns only); (b) an arm whose
        share rose, on those segments, against the 90% interval the data before the change gave
        it; (c) the EW posterior of this change, updated with the new data."""
        with self.g.scope.tx() as tx:
            ws = self.g._snapshot(tx, p.wf)
            base = self.g.rt.mirror.body(p.wf, p.base_version, tx)
        segs = self._verification_segments(ws, p)
        if segs is None:
            return VerifyResult("pending")
        new = p.proposed_config
        if base is None or new is None or ws.spec.objective.primary != \
                self.t.engagement_model_primary_kpi:
            return VerifyResult("verified", ("no comparison possible",))
        shares = self._share_observations(ws, p, new, segs)
        arms = self._arm_observations(ws, p, base, new, segs)
        fits = ep.fit(ws, self.model, self.t, f"verify:{p.proposal_id}", kinds=("ew",))
        ctx = GateContext(fits.ew, active_population(ws), cells=ws.cells, settings=self.t)
        rep = ctx.evaluate(base, new, ws.policy, self.t.alloc_verify_p_harm, self.t.alpha_d, None,
                           None).report
        cohort_ok = not any(f.startswith("cohort:") for f in rep.failed)
        return check_allocation(shares, arms, rep.p_de_below_1pct, cohort_ok, self.t)

    def _share_observations(self, ws: WorkflowSnapshot, p: Proposal, new: Config,
                            segs: Sequence[int]) -> list[ShareObservation]:
        e = ws.engage
        mask = np.isin(e.seg, np.asarray(segs)) & (e.config_version == int(p.applied_version or 0))
        if not mask.any():
            return []
        cells = sorted(int(c) for c in np.unique(e.cell[mask]))
        coh = dict(zip(cells, cohort_of_cells(new, ws.cells, cells)))
        keys = np.array([coh[int(c)] for c in e.cell[mask]], dtype=object)
        out = []
        for k in sorted(set(keys.tolist())):
            in_k = keys == k
            total = int(e.connected[mask][in_k].sum())
            if total < VERIFY_MIN_CALLS:
                continue
            for dim in ENGAGEMENT_DIMS:
                col = (e.w if dim == "welcome_message" else e.v)[mask][in_k]
                n = e.connected[mask][in_k]
                opts = list(ws.spec.options.get(dim, ()))
                for o, share in new.dist(k, dim).items():
                    out.append(ShareObservation(
                        cohort=k, dim=dim, option=o, configured=float(share),
                        dials_with_option=int(n[col == opts.index(o)].sum()), dials_total=total))
        return out

    def _arm_observations(self, ws: WorkflowSnapshot, p: Proposal, base: Config, new: Config,
                          segs: Sequence[int]) -> list[ArmObservation]:
        risen = _risen_arms(ws, base, new)
        if not risen:
            return []
        e = ws.engage
        before = dataclasses.replace(
            ws, engage=_rows(e, e.seg < segs[0]), now=int(p.applied_minute))
        fits = ep.fit(before, self.model, self.t, f"verify_before:{p.proposal_id}", kinds=("ew",))
        cache = EffectCache(fits.ew)
        pop = active_population(ws)
        cells_of = dict(zip([int(c) for c in pop.cells],
                            cohort_of_cells(new, ws.cells, [int(c) for c in pop.cells])))
        out = []
        for k, dim, arm, control in risen:
            cells = [c for c, x in cells_of.items() if x == k]
            weights = [float(n) for c, n in zip(pop.cells, pop.leads) if cells_of[int(c)] == k]
            if not cells:
                continue
            obs = [r for r in (segment_ratio(ws, dim, arm, control, cells, j) for j in segs)
                   if r is not None and r[1] > 0]
            if not obs:
                continue
            w = np.array([1.0 / r[1] ** 2 for r in obs])
            log_rr = float(np.average([r[0] for r in obs], weights=w))
            se = float(1.0 / np.sqrt(w.sum()))
            low = float(np.quantile(cache.scope_effect(dim, arm, cells, weights), 0.05))
            out.append(ArmObservation(cohort=k, dim=dim, arm=arm,
                                      observed_rel=log_rr + Z90 * se, dry_run_ci90_low=low))
        return out

    def _verify_schedule(self, p: Proposal, now: int) -> VerifyResult:
        """5.8: connects per (cohort, hour) against the credible band of the history curves,
        times the DID factor of the live caller-ID mix against the business default's."""
        with self.g.scope.tx() as tx:
            ws = self.g._snapshot(tx, p.wf)
            default_cfg = self.g.rt.mirror.body(p.wf, 1, tx)
        segs = self._verification_segments(ws, p)
        if segs is None:
            return VerifyResult("pending")
        learner = self.g.learner
        grid = learner.grid(ws, self.g.business[p.wf]) if learner is not None else None
        if grid is None:
            return VerifyResult("verified", ("no history grid to compare with",))
        hist = dataclasses.replace(ws, pickup=history_pickup(ws.pickup), cohorts=grid)
        curves = HourCurvesModel(self.t).fit(hist)
        ranged = dataclasses.replace(ws, cohorts=grid)
        model = ConnectModel(ws, self.t)
        factor = model.factor_against(default_cfg) if default_cfg is not None else 1.0
        finding = verify_schedule(ranged, curves, segs[0] // 2, factor, self.t)[0]
        if finding.kind == "verified":
            return VerifyResult("verified", (f"{finding.stat:.3f} of cells below the band",))
        return VerifyResult("rollback", (f"{finding.stat:.3f} of cells below the band",))

    def _verify_caller_id(self, p: Proposal, now: int) -> VerifyResult:
        """A caller-ID change is verified by the DID detectors of 5.9 and 7.3, which see every
        DID at every step; this resolves the proposal and records the states it left."""
        with self.g.scope.tx() as tx:
            ws = self.g._snapshot(tx, p.wf)
        if self._verification_segments(ws, p) is None:
            return VerifyResult("pending")
        states = {d: s.status for d, s in sorted(ws.did_state.items())}
        return VerifyResult("verified", (f"DID states {states}",))

    def on_verify_failed(self, p: Proposal, r: VerifyResult) -> None:
        """A failed verification (6.1): a P1 risk-reducing patch reverting that change's
        increases, with its decision row."""
        with self.g.scope.tx() as tx:
            ws = self.g._snapshot(tx, p.wf)
            base = self.g.rt.mirror.body(p.wf, p.base_version, tx)
            if base is None or p.proposed_config is None:
                return
            reqs = self._reverse_increases(ws, p, base)
            did = write_decision(tx, self._decision(
                p.wf, "learning.allocator", "verify_rollback", "6.1/verify_failed",
                {"proposal_id": p.proposal_id, "reasons": list(r.reasons)},
                {"patches": ep.patches_json(reqs)}))
            self._add(tx, reqs, did)

    def _reverse_increases(self, ws: WorkflowSnapshot, p: Proposal, base: Config
                           ) -> list[PatchRequest]:
        new = p.proposed_config
        live = ws.live
        out: list[PatchRequest] = []
        ev = freeze({"kind": "verify_rollback", "proposal_id": p.proposal_id})
        for k, dim, arm, control in _risen_arms(ws, base, new):
            if k not in (*live.cohort_ids(), DEFAULT_COHORT):
                continue
            cur = dict(live.dist(k, dim))
            keep = float(base.dist(k if k in base.cohort_ids() else DEFAULT_COHORT, dim)
                         .get(arm, 0.0))
            excess = cur.get(arm, 0.0) - keep
            if excess <= 1e-9:
                continue
            cur[arm] = keep
            cur[control] = cur.get(control, 0.0) + excess
            cur = {o: v for o, v in cur.items() if v > 0}
            out.append(PatchRequest(wf=p.wf, owner=OWNER_ALLOCATOR, path=("alloc", k, dim),
                                    value=FrozenDict(cur), cls=P1_ROLLBACK, p0_rank=None,
                                    evidence=ev))
        for k, t in (new.hour_weights or {}).items():
            before = base.hour_weights.get(k)
            if t != before and k in live.cohort_ids():
                out.append(PatchRequest(wf=p.wf, owner=OWNER_SCHEDULE, path=("hour_weights", k),
                                        value=FrozenDict(before) if before is not None else None,
                                        cls=P1_ROLLBACK, p0_rank=None, evidence=ev))
        return out

    def enrich(self, wf: str, ctx: Any, comp: Composition | None = None) -> Any:
        """The request builder's dry run (6.6): the draws and the harm report of the gate on
        this composition. The in-memory verdict of the last gate is used only when it judged
        this very change against this very live config; otherwise (the change was gated before
        another proposal of the same tick was applied, or the process restarted in between) the
        gate runs again, so the package is a function of the store and the API alone and a
        restarted run builds the same proposal as a clean one."""
        if comp is None:
            return ctx
        last = self._last_gate.get(wf)
        if last is None or last[:2] != (comp.proposed.hash(), comp.live.hash()):
            if not comp.changed_dims:
                return ctx
            alpha = ep.alpha_for(comp.cls, self.t)
            v, _, fits, _ = self._judge(wf, comp.live, comp.proposed, alpha, comp.cls)
        else:
            v, fits = last[2], last[3]
        upd: dict[str, Any] = {"harm": v.report, "guardrail": v.guard}
        if fits is not None and ctx.cells:
            act = {int(c): float(n) for c, n in dict(ctx.active_by_cell).items()}
            tot = sum(act.values())
            if tot > 0:
                upd.update(draws=fits.ew, mix=FrozenDict({c: n / tot for c, n in act.items()}))
        return dataclasses.replace(ctx, **upd)


# ============================================================================== helpers

def last_complete_segment(now: int, window: tuple[int, int]) -> int:
    """The last half-window segment whose dialling time has fully passed at `now`."""
    seg = segment_of(now, window)
    if now % MINUTES_PER_DAY >= window[1] * 60:
        return seg
    return seg - 1


def _risen_arms(ws: WorkflowSnapshot, base: Config, new: Config
                ) -> list[tuple[str, str, str, str]]:
    """(cohort, dim, arm, control) for every non-control arm whose share `new` raised over `base`."""
    out = []
    known = set(base.cohort_ids())
    for k in [*new.cohort_ids(), DEFAULT_COHORT]:
        for dim in ENGAGEMENT_DIMS:
            arms = dim_arms(ws, dim)
            before = base.dist(k if k in known or k == DEFAULT_COHORT else DEFAULT_COHORT, dim)
            for a, x in new.dist(k, dim).items():
                if a != arms.control and x > before.get(a, 0.0) + 1e-9:
                    out.append((k, dim, a, arms.control))
    return out


def _rows(e: EngageArrays, mask: np.ndarray) -> EngageArrays:
    return EngageArrays(*[np.asarray(getattr(e, f.name))[mask]
                          for f in dataclasses.fields(EngageArrays)])


def _plain(x: Any) -> Any:
    if isinstance(x, Mapping):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def _jsonb(v: Any) -> Any:
    from psycopg.types.json import Jsonb
    return Jsonb(_plain(v))


__all__ = ["EngagementLoop", "last_complete_segment", "OWN_OWNERS"]
