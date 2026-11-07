"""Governance in the real loop: the hooks that run the overlay, the proposal lifecycle and the
release train inside the orchestrator's stages, over Postgres and the write-ahead intents.

Owner: integration step I2 (milestone M3). Implements DESIGN.md 4.1 steps 4, 5, 8 and 9 (REFRESH's
supervisor-edit handler, LIFECYCLE, GOVERN, VERIFY), 2 restart step 5 (the proposal sweep), 6.1 to
6.7 through `governance/*` and `planner/overlay.py`, and 8.2 and 8.3 through
`governance/effects.py`. It also runs the pickup levers (`learning/probe.py`, `loop/pickup.py`) in
DETECT, DECIDE and VERIFY when `CTL_LEARNER=probe`, and the engagement learner
(`loop/engagement.py`, step I4, ADR 0031) in DETECT, DECIDE, the release train's composed-change
gate, the lifecycle's re-gate and verifiers, and the held-patch re-check. ADR 0029, 0030, 0031.

State: nothing lives only here. Proposals, transitions, the approval mirror, the overlay
(patches, owners, cooldowns), DID states and every decision are in the run schema; `attach`
rebuilds the in-memory working set from it on every start, before the reconciler runs, so a
crash anywhere resumes into exactly the state a clean run holds at that point.

Per step, in the orchestrator's order:
  REFRESH    `on_config_changes` (in REFRESH's transaction): a supervisor version rebases the
             overlay (6.7: invalidate, cooldown, substitute the new control) and marks every older
             pending or approved proposal STALE, so it is never applied
  LIFECYCLE  policy re-read (9.7); `recover` (crash leftovers); `tick` (apply visible approvals
             after the pre-apply checks, resolve rejected and expired, release noticed
             auto-applies); auto-release of held patches
  DETECT     DID states (probe learner): SPRT on probe dials, slow path, the flagged-DID detector
             and the KPI drift CUSUMs (`loop/pickup.py`), persisted every step
  DECIDE     every stop: the next drain step of a draining DID incident, and the caller-ID plan
             of a workflow where a DID carrying traffic just turned bad; at each workflow's
             planning point (its routine slot minutes): learner patches, each with its decision
             row and behind the caller-ID harm gate, into the overlay
  GOVERN     `ReleaseTrain.run`: compose, route, timing, act; one decision row per workflow that
             had something to do
  VERIFY     `ProposalLifecycle.verify` with the caller-ID and schedule verifiers, then DID drain
             verification
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from controller.audit.writer import record, write_decision
from controller.governance.builder import BuildContext
from controller.governance.effects import IntentEffects, attach_effect_hooks
from controller.governance.lifecycle import LifecycleDeps, ProposalLifecycle, VerifyResult
from controller.governance.release import ReleaseDeps, ReleaseTrain
from controller.governance.store_pg import OverlayStore, PgProposalStore, TxScope, holds_of
from controller.governance.timing import choose_submit, routine_slots
from controller.learning.probe import OWNER_COHORTS, ProbeLearner, did_rows
from controller.learning.schedule import OWNER as OWNER_SCHEDULE
from controller.loop.engagement import EngagementLoop
from controller.loop.incidents_hooks import PromptSafety
from controller.loop.orchestrator import LoopHooks, Runtime, StageCounts
from controller.loop.pickup import PickupLevers, json_safe
from controller.obs.alerts import raise_alert
from controller.planner.overlay import LIVE_STATES, OverlayContext, dominant
from controller.snapshot import load_snapshot
from controller.store import repos
from controller.types import (DEFAULT_COHORT, IN_FLIGHT_STATES, P0_INCIDENT, P1_ROLLBACK, CohortDef,
                              Config, ConfigChange, Decision, DidState, EffectivePolicy,
                              FrozenDict, Hold, PatchRequest, Proposal, Tx, WorkflowSnapshot,
                              canonical_json, freeze, thaw)
from controller.world.configs import load_cells
from controller.world.directives import load_directives
from controller.world.registry import WorkflowRegistry

log = logging.getLogger(__name__)

RULE_SCHEDULE_RESTORE = "5.8/schedule_restore"


class GovernanceHooks(LoopHooks):
    """The `LoopHooks` that make governance (and the probe-only learner) part of the loop."""

    def __init__(self, learner: str = "probe"):
        self.learner_kind = learner
        self.learner = ProbeLearner() if learner in ("probe", "probe_only") else None
        self.engagement = EngagementLoop(self) if learner == "probe" else None
        self.now = 0

    # ------------------------------------------------------------------ wiring (on every start)
    def attach(self, rt: Runtime) -> None:
        """Build the working set from the store and register the intent hooks. Runs before the
        reconciler, which may need the hooks to finish a governance intent."""
        self.rt = rt
        self.t = rt.settings.t
        if self.learner is not None:
            self.learner = ProbeLearner(self.t)
        if self.engagement is not None:
            self.engagement.attach()
        self.scope = TxScope(rt.db)
        self.store = PgProposalStore(self.scope, rt.catalog)
        self.registry = WorkflowRegistry(load_directives(), self.t)
        self.registry.load(rt.ident.specs, rt.tick)
        with rt.db.tx() as tx:
            self.now = repos.get_clock(tx).sim_minute
            self.business = {s.workflow_id: self._business_cohorts(tx, s.workflow_id)
                             for s in rt.catalog.specs}
        self.overlay_store = OverlayStore(self.scope, rt.catalog)
        self.overlay = self.overlay_store.load(
            self._overlay_context, self._clock, thresholds=self.t,
            in_flight=lambda wf: self.store.in_states(IN_FLIGHT_STATES, wf))
        for wid, cohorts in self.business.items():
            self.overlay.mark_supervisor_cohorts(wid, [c.id for c in cohorts])
        self.pickup = PickupLevers(self.t, rt.catalog, self._decision)
        self.lc = ProposalLifecycle(LifecycleDeps(
            store=self.store, overlay=self.overlay, sim=rt.client,
            executor=IntentEffects(rt.intents, self.scope), policy_of=self.registry.policy,
            clock=self._clock, epoch=rt.epoch, run6=rt.run6,
            settings_hash=rt.settings.settings_hash(), code_version=rt.settings.env.code_version,
            t=self.t, holds=self._holds, on_alert=self._alert, on_applied=self._on_applied,
            regate=self.engagement.regate if self.engagement else None,
            verifiers=self._all_verifiers(), on_rollback=self._on_rollback_all))
        self.release = ReleaseTrain(ReleaseDeps(
            overlay=self.overlay, lifecycle=self.lc, policy_of=self.registry.policy,
            workflows=self.registry.workflow_ids, live=self._live,
            build_context=self._build_context, holds=self._holds,
            suspect_since=self._suspect_since, t=self.t,
            gate=self.engagement.gate if self.engagement else None))
        attach_effect_hooks(rt.registry, lambda: self.lc, self.scope)
        with self.scope.tx() as tx:
            self.pickup.restore(tx)
        self.prompts = PromptSafety(self) if self.learner is not None else None
        if self.prompts is not None:
            self.prompts.attach()

    def _business_cohorts(self, tx: Tx, wid: str) -> tuple[CohortDef, ...]:
        """The business default's cohorts (version 1): supervisor-owned from the start (5.2)."""
        row = repos.config_version(tx, self.rt.catalog.code(wid), 1)
        if row is not None and row.config is not None:
            return tuple(row.config.cohorts)
        return tuple(self.rt.mirror.live(wid)[1].cohorts)

    # ------------------------------------------------------------------ small dependencies
    def _clock(self) -> int:
        return self.now

    def _live(self, wf: str) -> tuple[int, Config]:
        snap = self.rt.client.config(wf)
        return snap.version, snap.config

    def _holds(self, wf: str) -> Sequence[Hold]:
        return holds_of(self.scope, self.rt.catalog, wf)

    def _suspect_since(self, wf: str) -> int | None:
        with self.scope.tx() as tx:
            r = tx.execute("""SELECT min(since_minute) FROM did_state
                              WHERE wf = %s AND status = 'suspect'""",
                           (self.rt.catalog.code(wf),)).fetchone()
        return None if r is None or r[0] is None else int(r[0])

    def _alert(self, code: str, wf: str | None, msg: str) -> None:
        subject = msg.split(":")[0].split(" ")[0] or code
        with self.scope.tx() as tx:
            raise_alert(tx, code, self.rt.catalog.code(wf) if wf else None, subject, msg,
                        sim_minute=self.now)

    def _on_applied(self, p: Proposal, version: int) -> None:
        """The config mirror records the body we sent as `ours`, in the transaction that marks
        the proposal APPLIED (4.1 step 4)."""
        actor = "controller_auto" if p.route == "auto" else "controller_approved"
        with self.scope.tx() as tx:
            base = self.rt.mirror.body(p.wf, p.base_version, tx)
            dims = base.changed_dims(p.proposed_config) if base is not None else ()
            self.rt.mirror.record_ours(tx, p.wf, version, p.proposed_config, actor,
                                       p.applied_minute if p.applied_minute is not None
                                       else self.now, p.proposal_id, p.approval_id, dims)
            self.rt.mirror.record_provenance(tx, p.wf, version, p.proposed_config,
                                             _provenance_overrides(p))

    def did_states(self, wf: str, tx: Tx | None = None) -> dict[str, DidState]:
        opts = self.rt.catalog.spec(wf).options["caller_id"]

        def read(t: Tx) -> dict[str, DidState]:
            rows = t.execute("""SELECT did, status, r_hat, r_lcb, r_ucb, n_dials, sprt_llr,
                                       since_minute, evidence FROM did_state WHERE wf = %s
                                ORDER BY did""", (self.rt.catalog.code(wf),)).fetchall()
            return {opts[r[0]]: DidState(wf, opts[r[0]], r[1], r[2], r[3], r[4], int(r[5]),
                                         float(r[6]), r[7], freeze(r[8] or {}))
                    for r in rows if r[0] < len(opts)}
        if tx is not None:
            return read(tx)
        with self.scope.tx() as t:
            return read(t)

    def _overlay_context(self, wf: str) -> OverlayContext:
        spec = self.rt.catalog.spec(wf)
        _, live = self.rt.mirror.live(wf)
        controls = {(DEFAULT_COHORT, d): dominant(live.dist(DEFAULT_COHORT, d))
                    for d in ("welcome_message", "voice")}
        return OverlayContext(spec=spec, did_state=FrozenDict(self.did_states(wf)),
                              controls=FrozenDict({k: v for k, v in controls.items() if v}),
                              history=tuple(self._recent_bodies(wf)))

    def _recent_bodies(self, wf: str) -> list[Config]:
        with self.scope.tx() as tx:
            rows = repos.config_versions(tx, self.rt.catalog.code(wf))
        return [r.config for r in rows if r.config is not None
                and (r.applied_minute is None
                     or r.applied_minute > self.now - self.t.schedule_revert_lookback_min)]

    def _build_context(self, wf: str, comp: Any = None) -> BuildContext:
        """What the request builder needs for the affected-leads count (6.6): the cells, the
        active leads in each, and each cell's share of new leads (first attempts per cell over
        the history days, else over our own days). Posterior replays are wired with the
        learners that own them."""
        spec = self.rt.catalog.spec(wf)
        code = self.rt.catalog.code(wf)
        with self.scope.tx() as tx:
            cells = tuple(load_cells(tx))
            active = {int(c): int(n) for c, n in tx.execute(
                """SELECT cell, count(*) FROM lead_dim WHERE wf = %s AND state = 0
                   AND cell IS NOT NULL GROUP BY cell""", (code,))}
            first = tx.execute("""SELECT cell, sum(dials) FILTER (WHERE sim_day < 0),
                                         sum(dials) FROM r_pickup
                                  WHERE wf = %s AND attempt_bucket = 1 GROUP BY cell""",
                               (code,)).fetchall()
        hist = {int(c): float(h or 0) for c, h, _ in first}
        if sum(hist.values()) <= 0:
            hist = {int(c): float(a or 0) for c, _, a in first}
        total = sum(hist.values())
        share = {c: v / total for c, v in hist.items()} if total > 0 else {}
        ctx = BuildContext(primary_kpi=spec.objective.primary, batch_size=spec.daily_batch.size,
                           mid_flight=spec.mid_flight, cells=cells,
                           active_by_cell=FrozenDict(active),
                           hist_share_by_cell=FrozenDict(share))
        return self.engagement.enrich(wf, ctx, comp) if self.engagement else ctx

    def _decision(self, wf: str | None, module: str, kind: str, rule: str,
                  inputs: Mapping[str, Any], outcome: Mapping[str, Any] | None = None) -> Decision:
        return self.rt.decision(wf=wf, module=module, kind=kind, rule_id=rule, inputs=inputs,
                                outcome=outcome, sim_minute=self.now)

    # ------------------------------------------------------------------ LoopHooks
    def policy(self, rt: Runtime, workflow_id: str) -> EffectivePolicy | None:
        return self.registry.policy(workflow_id)

    def plan_facts(self, rt: Runtime, now: int, workflow_id: str) -> Mapping[str, Any]:
        """Stops the step rule must land on (4.2): today's and tomorrow's routine slots (the
        planning points), the submit minute of any urgent open work, and notice expiries. A pure
        function of the store and the API, so a restarted run plans the same stops."""
        pol = self.registry.policy(workflow_id)
        d = now // 1440
        slots = set(routine_slots(d, pol, t=self.t)) | set(routine_slots(d + 1, pol, t=self.t))
        urgent = [p.cls for p in self.overlay.open_patches(workflow_id) if p.cls <= 1]
        if urgent:
            when = choose_submit(now, pol, self.lc.used(workflow_id), min(urgent),
                                 self.store.holder(workflow_id) is None, t=self.t)
            if when is not None:
                slots.add(when)
        notices = [p.notice_until_minute for p in self.store.in_states(("noticed",), workflow_id)
                   if p.notice_until_minute is not None]
        # an incident needs tick-sized stops while it acts (a drain chain moves one step per
        # stop); a drained DID waiting for its 2 dialling hours of verification does not
        acting = any(i.kind != "did_degraded" or i.state == "draining"
                     for i in self.pickup.mgr.open_incidents(workflow_id))
        return {"planned_slots": tuple(sorted(s for s in slots if s > now)),
                "notice_expiries": tuple(sorted(notices)),
                "open_incident": acting}

    def full_sweep(self, rt: Runtime) -> None:
        """Restart step 5: every proposal a crash left mid-call against the simulator, unsent
        drafts resent or aborted, and any controller version no proposal of ours explains."""
        self.now = self._db_clock()
        self.lc.recover(self.now)
        self._unknown_writers()

    def _db_clock(self) -> int:
        with self.rt.db.tx() as tx:
            return repos.get_clock(tx).sim_minute

    def _unknown_writers(self) -> None:
        with self.scope.tx() as tx:
            ours = {r[0] for r in tx.execute("SELECT proposal_id FROM proposal")}
            for wid, version, pid in repos.controller_versions(tx):
                if pid is not None and pid not in ours:
                    raise_alert(tx, "UNKNOWN_WRITER", self.rt.catalog.code(wid),
                                f"{wid}@v{version}", f"{wid} v{version} was written by a "
                                f"controller with proposal {pid}, which is not ours",
                                sim_minute=self.now)

    def on_config_changes(self, rt: Runtime, tx: Tx, changes: list[ConfigChange]) -> None:
        """6.7 for every supervisor version, inside REFRESH's transaction."""
        with self.scope.bind(tx):
            self.now = repos.get_clock(tx).sim_minute
            if self.prompts is not None:
                self.prompts.on_config_changes(tx, changes)
            for ch in changes:
                if not ch.is_supervisor or ch.config is None:
                    continue
                old = rt.mirror.body(ch.workflow_id, ch.version - 1, tx)
                rep = self.overlay.rebase_on_supervisor_edit(ch.workflow_id, old, ch.config,
                                                             ch.version)
                stale = self.lc.mark_stale(ch.workflow_id, ch.version, self.now)
                d = self._decision(ch.workflow_id, "planner.overlay", "rebase",
                                   "6.7/rebase_on_supervisor_edit",
                                   {"version": ch.version, "old_known": old is not None,
                                    "sup_paths": [list(p) for p in rep.sup_paths]},
                                   {"invalidated_patches": list(rep.invalidated_patches),
                                    "stale_proposals": [p.proposal_id for p in stale],
                                    "interrupted_experiments": list(rep.interrupted_experiments),
                                    "cooldown_until": rep.cooldown_until,
                                    "substitutions": [list(dataclass_row(s))
                                                      for s in rep.substitutions]})
                did = write_decision(tx, d)
                raise_alert(tx, "SUPERVISOR_EDIT", rt.catalog.code(ch.workflow_id),
                            f"{ch.workflow_id}@v{ch.version}",
                            f"supervisor edited {', '.join(ch.changed_dims)} of "
                            f"{ch.workflow_id} (v{ch.version}); our work was rebased on it, "
                            f"{len(stale)} proposal(s) marked stale", sim_minute=self.now)
                record(tx, wf=ch.workflow_id, actor="controller", action="rebased",
                       subject=("config_version", f"{ch.workflow_id}@v{ch.version}"),
                       summary=f"rebased on supervisor v{ch.version}",
                       details={"stale": [p.proposal_id for p in stale]}, caused_by=did)

    def lifecycle(self, rt: Runtime, now: int) -> StageCounts:
        self.now = now
        self._reread_policies(rt)
        before = self._applied_count()
        self.lc.recover(now)
        self.lc.tick(now)
        released = self.overlay.release_held(
            now, self.engagement.release_ok if self.engagement else (lambda p: True))
        self.scope.flush()
        if released["released"] or released["dropped"]:
            with self.scope.tx() as tx:
                write_decision(tx, self._decision(None, "planner.overlay", "release_held",
                                                  "6.1/auto_release", {"now": now},
                                                  released))
        return StageCounts(applies=self._applied_count() - before)

    def _reread_policies(self, rt: Runtime) -> None:
        """9.7: the policy is read from the API every step; a changed field is audited, and a
        fresh read clears the post-403 auto-apply block. A workflow that was not there at the
        start has no catalog entry or history, so it is reported and left alone."""
        specs = rt.client.workflows()
        known = [s for s in specs if s.workflow_id in rt.catalog.codes]
        self._report_unknown_workflows([s.workflow_id for s in specs
                                        if s.workflow_id not in rt.catalog.codes])
        changes = self.registry.load(known, rt.tick)
        if not changes:
            return
        with self.scope.tx() as tx:
            for c in changes:
                record(tx, wf=c.workflow_id, actor="controller", action="policy_changed",
                       subject=("workflow", c.workflow_id), summary=f"{c.field} changed",
                       details={"old": str(c.old), "new": str(c.new)})
        for wf in {c.workflow_id for c in changes}:
            self.lc.policy_reread(wf)

    def _report_unknown_workflows(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        with self.scope.tx() as tx:
            for wid in sorted(ids):
                alert = raise_alert(tx, "UNKNOWN_WORKFLOW", None, wid,
                                    f"{wid} appeared mid-run; ignored until a new run",
                                    sim_minute=self.now)
                if alert is not None:
                    record(tx, wf=None, actor="controller", action="workflow_ignored",
                           subject=("workflow", wid),
                           summary=f"{wid} is not in this run's catalog", details={})

    def _applied_count(self) -> int:
        with self.scope.tx() as tx:
            return int(tx.execute("SELECT count(*) FROM proposal WHERE applied_minute IS NOT NULL"
                                  ).fetchone()[0])

    # ------------------------------------------------------------------ the probe learner
    def _snapshot(self, tx: Tx, wf: str) -> WorkflowSnapshot:
        spec = self.rt.catalog.spec(wf)
        version, live = self._live(wf)
        clock = repos.get_clock(tx)
        return load_snapshot(tx, self.rt.catalog.code(wf), spec=spec,
                             policy=self.registry.policy(wf), live=live, live_version=version,
                             now=self.now, did_state=self.did_states(wf, tx),
                             in_flight=self.store.holder(wf), end_minute=clock.end_minute,
                             run_id=self.rt.schema)

    def bootstrap(self, rt: Runtime) -> StageCounts | None:
        """Restart step 6, once per run (DESIGN.md 2, 5.2, 5.9): each workflow's D0 cohort grid
        from history, recorded in `cohort_def` with its decision row (the grid reaches the
        config through governance, at the workflow's first routine slot), and the D0 DID states
        from history. Idempotent: rows are keyed and upserted, so an interrupted bootstrap
        reruns to the same result."""
        if self.learner is None:
            return None
        self.now = self._db_clock()
        n = 0
        for wf in self.registry.workflow_ids():
            with self.scope.tx() as tx:
                ws = self._snapshot(tx, wf)
                g = self.learner.grid(ws, self.business[wf])
                if g is not None:
                    for row in g.rows(created_minute=self.now):
                        tx.execute("""INSERT INTO cohort_def (cohort_key, wf, where_canon,
                                                              def_hash, cells, origin,
                                                              created_minute)
                                      VALUES (%s, %s, %s, %s, %s, %s, %s)
                                      ON CONFLICT (cohort_key) DO NOTHING""",
                                   (row["cohort_key"], self.rt.catalog.code(wf),
                                    _jsonb(row["where_canon"]), row["def_hash"], row["cells"],
                                    row["origin"], row["created_minute"]))
                write_decision(tx, self._decision(
                    wf, OWNER_COHORTS, "cohort_grid", "5.2/build_grid",
                    {"business_cohorts": [c.id for c in self.business[wf]]},
                    {"cohorts": list(g.keys()) if g is not None else [],
                     "set_key": g.key() if g is not None else None,
                     "evidence": json_safe(g.evidence) if g is not None else
                     {"reason": "no history grid cohort"}}))
                if not ws.did_state:
                    states = self.learner.did_bootstrap(ws)
                    self._write_did_states(tx, wf, states)
                    write_decision(tx, self._decision(
                        wf, "learning.did", "did_state", "5.9/bootstrap", {},
                        {"states": {d: st.status for d, st in sorted(states.items())}}))
                n += 1
        if self.prompts is not None:
            n += self.prompts.bootstrap()
        return StageCounts(decisions=n)

    def detect(self, rt: Runtime, now: int) -> StageCounts | None:
        """DID states every step (the SPRT reads each tick's probe dials once), then the
        flagged-DID detector and the KPI drift CUSUMs (`loop/pickup.py`)."""
        self.now = now
        if self.learner is None:
            return None
        for wf in self.registry.workflow_ids():
            with self.scope.tx() as tx:
                ws = self._snapshot(tx, wf)
                if not ws.did_state:
                    states = self.learner.did_bootstrap(ws)
                    findings = []
                else:
                    states, findings = self.learner.did_step(ws)
                had_healthy = not ws.did_state or any(
                    s.status == "healthy" for s in ws.did_state.values())
                for f in findings:
                    if f.kind == "no_healthy" and not had_healthy:
                        continue        # recorded when it starts, not at every step it lasts
                    write_decision(tx, self._decision(
                        wf, "learning.did", "did_state", f"5.9/{f.kind}",
                        {"did": f.scope, "stat": f.stat, "threshold": f.threshold},
                        {"status": states[f.scope].status} if f.scope in states else {}))
                ws1 = dataclasses.replace(ws, did_state=FrozenDict(states))
                states = {**states, **self.pickup.detect(tx, ws1,
                                                         self.learner.did_model(ws1))}
                changed = {n: s for n, s in states.items() if ws.did_state.get(n) != s}
                if changed:
                    self._write_did_states(tx, wf, changed)
        if self.prompts is not None:
            self.prompts.detect()
        if self.engagement is not None:
            self.engagement.detect(now)
        return None

    def _write_did_states(self, tx: Tx, wf: str, states: Mapping[str, DidState]) -> None:
        opts = self.rt.catalog.spec(wf).options["caller_id"]
        code = self.rt.catalog.code(wf)
        for row in did_rows(states, opts):
            tx.execute("""INSERT INTO did_state (wf, did, status, r_hat, r_lcb, r_ucb, n_dials,
                                                 sprt_llr, since_minute, evidence)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (wf, did) DO UPDATE SET status = excluded.status,
                              r_hat = excluded.r_hat, r_lcb = excluded.r_lcb,
                              r_ucb = excluded.r_ucb, n_dials = excluded.n_dials,
                              sprt_llr = excluded.sprt_llr, since_minute = excluded.since_minute,
                              evidence = excluded.evidence""",
                       (code, *row[:-1], _jsonb(row[-1])))

    def is_planning_point(self, wf: str, now: int) -> bool:
        """The workflow's routine slot minutes (6.3): the stop rule lands on them exactly."""
        return now in routine_slots(now // 1440, self.registry.policy(wf), t=self.t)

    def decide(self, rt: Runtime, now: int) -> StageCounts | None:
        """Every stop: the next drain step of a draining DID incident (P0b, 7.3). At the
        workflow's planning points: the probe learner's requests (probes, spread, cohorts and
        hour weights), each cut back by the caller-ID harm gate; a change the gate cuts to
        nothing is a recorded decision not to act."""
        self.now = now
        if self.learner is None:
            return None
        n = 0
        for wf in self.registry.workflow_ids():
            planning = self.is_planning_point(wf, now)
            turned_bad = not planning and self._did_turned_bad(wf, now)
            if not planning and not turned_bad and not self.pickup.draining(wf):
                continue
            with self.scope.tx() as tx:
                ws = self._snapshot(tx, wf)
                model = self.learner.did_model(ws)
                reqs = self.pickup.drain_requests(tx, ws, model)
                if planning:
                    reqs += self._without_rolled_back_tables(
                        tx, wf, self.learner.plan(ws, self.business[wf]))
                elif turned_bad:
                    reqs += [r for r in model.plan_caller_ids(ws) if r.cls != P0_INCIDENT]
                if planning or turned_bad:
                    self._record_blocked(tx, wf, model, now)
                if self.engagement is not None:
                    reqs = self.engagement.guardrail_filter(tx, ws, reqs)
                for req in reqs:
                    if self._already_wanted(req):
                        continue
                    did = write_decision(tx, self._decision(
                        wf, req.owner, "patch", _rule_of(req),
                        {"path": list(req.path), "evidence": json_safe(req.evidence)},
                        {"value": _json_value(req.value), "class": req.cls}))
                    self.overlay.add(dataclasses.replace(req, decision_id=did))
                    n += 1
        if self.prompts is not None:
            n += self.prompts.decide()
        if self.engagement is not None:
            n += self.engagement.decide(now)
        return StageCounts(decisions=n)

    def _did_turned_bad(self, wf: str, now: int) -> bool:
        """DETECT just moved a DID that still carries traffic to degraded or unhealthy (the
        floor, the SPRT or the slow path; a fast alarm opens an incident instead, which
        `draining` covers). Its share comes off now, not at the next routine slot. Read from
        `did_state`, so a restart between DETECT and DECIDE sees the same."""
        live = self.rt.mirror.live(wf)[1]
        carried = {d for k in {DEFAULT_COHORT, *live.allocations}
                   for d, w in live.dist(k, "caller_id").items() if w > 0}
        return any(s.status in ("degraded", "unhealthy") and s.since_minute == now
                   and d in carried for d, s in self.did_states(wf).items())

    def _record_blocked(self, tx: Tx, wf: str, model: Any, now: int) -> None:
        """A change the harm gate cut to nothing is a decision not to act (0.1)."""
        for b in model.blocked:
            write_decision(tx, self._decision(
                wf, "learning.did", "no_action", "0.1/did_gate", json_safe(b),
                {"blocked": "harm gate cut the change to nothing"}))
            raise_alert(tx, "HARM_GATE_BLOCK", self.rt.catalog.code(wf),
                        f"{wf}:caller_id:{b['scope']}",
                        f"caller-ID change on {b['scope']} blocked: P(dd < -2%) "
                        f"{b['full_p_dd_below_line']:.3f} > {b['alpha_d']}", sim_minute=now)

    def _without_rolled_back_tables(self, tx: Tx, wf: str, reqs: list[PatchRequest]
                                    ) -> list[PatchRequest]:
        """After a table failed verification (5.8) the curves are not trusted for this workflow:
        proposing them again would undo the restore at the next slot."""
        if not any(r.path[0] == "hour_weights" for r in reqs):
            return reqs
        restored = tx.execute("""SELECT 1 FROM decision WHERE wf = %s AND rule_id = %s LIMIT 1""",
                              (self.rt.catalog.code(wf), RULE_SCHEDULE_RESTORE)).fetchone()
        return [r for r in reqs if r.path[0] != "hour_weights"] if restored else reqs

    def _already_wanted(self, req: PatchRequest) -> bool:
        """A live patch on the same path with the same value already carries this request."""
        want = canonical_json(_json_value(req.value))
        return any(p.wf == req.wf and p.path == tuple(req.path) and p.state in LIVE_STATES
                   and canonical_json(_json_value(p.value)) == want
                   for p in self.overlay.patches.values())

    # ------------------------------------------------------------------ GOVERN and VERIFY
    def govern(self, rt: Runtime, now: int) -> StageCounts:
        self.now = now
        decisions = self.release.run(now, on_decision=lambda d: self._record_release(d, now))
        self.scope.flush()
        return StageCounts(decisions=sum(1 for d in decisions if d.action != "idle"),
                           proposals_submitted=sum(1 for d in decisions
                                                   if d.action == "submitted"))

    def _record_release(self, d: Any, now: int) -> None:
        """The release train's verdict for one workflow (including decisions not to act). Each
        side effect it took already has its own decision row, written before the call."""
        if d.action == "idle":
            return
        with self.scope.tx() as tx:
            write_decision(tx, self._decision(
                d.wf, "governance.release", "release", f"6.2/{d.action}",
                {"now": now}, {"action": d.action, "reason": d.reason,
                               "proposal_id": d.proposal_id,
                               "planned_minute": d.planned_minute,
                               "auto_refused": list(d.auto_refused)}))
            if d.action == "no_safe_window":
                raise_alert(tx, "NO_SAFE_WINDOW", self.rt.catalog.code(d.wf), d.wf, d.reason,
                            sim_minute=now)

    def _all_verifiers(self) -> dict[str, Any]:
        """Engagement's allocation and `none` verifiers, with the pickup levers' caller-ID and
        schedule verifiers winning where both define a kind (I3 owns those two)."""
        eng = self.engagement.verifiers() if self.engagement else {}
        return {**eng, **self._pickup_verifiers(), **self._prompt_verifiers()}

    def _prompt_verifiers(self) -> dict[str, Any]:
        """A prompt fix is verified by its incident (7.1, `incidents/manager.py`): the proposal
        leaves VERIFYING when the incident that asked for it resolves or fails verification."""
        if self.learner is None:
            return {}
        return {"prompt": self._verify_prompt_fix}

    def _verify_prompt_fix(self, p: Proposal, now: int) -> VerifyResult:
        fix = p.proposed_config.prompt_version if p.proposed_config is not None else None
        for inc in sorted(self.pickup.mgr.incidents.values(), key=lambda i: i.incident_id):
            if inc.kind != "prompt_safety" or inc.fix_proposal_id != p.proposal_id:
                continue
            if inc.state == "resolved":
                return VerifyResult("verified", (f"incident {inc.incident_id} resolved",))
            if fix is not None and fix in inc.evidence.get("tried", ()):
                return VerifyResult("rollback", (f"incident {inc.incident_id}: fix {fix} failed "
                                                 "its verification",))
        return VerifyResult("pending")

    def _on_rollback_all(self, p: Proposal, r: VerifyResult) -> None:
        self._on_rollback(p, r)
        if self.engagement is not None and "prompt_version" not in (p.diff or {}).get(
                "changed_dims", ()):      # a failed prompt fix is the incident manager's to retry
            self.engagement.on_verify_failed(p, r)

    def _pickup_verifiers(self) -> dict[str, Any]:
        """The kind verifiers of 6.1 for the levers of this module."""
        if self.learner is None:
            return {}
        return {"caller_id": self._verify_pickup, "schedule": self._verify_pickup}

    def _verify_pickup(self, p: Proposal, now: int) -> VerifyResult:
        with self.scope.tx() as tx:
            ws = self._snapshot(tx, p.wf)
        return self.pickup.verify_change(
            ws, p, lambda at: self.learner.verify_schedule_change(ws, self.business[p.wf], at))

    def _on_rollback(self, p: Proposal, r: VerifyResult) -> None:
        """A failed hour table comes back out as a P1 (5.8, risk-reducing because the old table
        was live in the look-back, 6.5). A failed caller-ID check only pages: the DID detectors
        act on what they find (7.3)."""
        if r.page != "SCHEDULE_UNVERIFIED":
            return
        with self.scope.tx() as tx:
            old = self.rt.mirror.body(p.wf, p.base_version, tx)
            if old is None or p.proposed_config is None:
                return
            new = p.proposed_config
            for k in sorted(set(old.hour_weights) | set(new.hour_weights)):
                before = old.hour_weights.get(k)
                if before == new.hour_weights.get(k):
                    continue
                value = dict(before) if before is not None else None
                did = write_decision(tx, self._decision(
                    p.wf, OWNER_SCHEDULE, "patch", RULE_SCHEDULE_RESTORE,
                    {"proposal_id": p.proposal_id, "cohort": k, "reasons": list(r.reasons)},
                    {"value": value, "class": P1_ROLLBACK}))
                self.overlay.add(PatchRequest(
                    wf=p.wf, owner=OWNER_SCHEDULE, path=("hour_weights", k),
                    value=FrozenDict(value) if value is not None else None, cls=P1_ROLLBACK,
                    p0_rank=None, decision_id=did,
                    evidence=freeze({"kind": "schedule", "cohort": k,
                                     "restores": p.proposal_id})))

    def verify(self, rt: Runtime, now: int) -> StageCounts | None:
        self.now = now
        self.lc.verify(now)
        if self.learner is not None:
            for wf in self.registry.workflow_ids():
                if not any(i.state == "verifying"
                           for i in self.pickup.mgr.open_incidents(wf, "did_degraded")):
                    continue
                with self.scope.tx() as tx:
                    self.pickup.verify(tx, self._snapshot(tx, wf))
            if self.prompts is not None:
                self.prompts.verify()
        return None


def _jsonb(v: Any) -> Any:
    return Jsonb(thaw(v))


def _json_value(v: Any) -> Any:
    return thaw(v)


def _rule_of(req: PatchRequest) -> str:
    if req.path[0] == "alloc" and req.path[2] == "caller_id":
        return "5.9/plan_caller_ids"
    if req.path[0] == "hour_weights":
        return "5.8/plan_hour_weights"
    if req.owner == OWNER_COHORTS:
        return "5.2/build_grid"
    return "patch"


def dataclass_row(obj: Any) -> tuple:
    return tuple(getattr(obj, f.name) for f in dataclasses.fields(obj))


__all__ = ["GovernanceHooks"]



_PROV_SOURCE_BY_CLASS = {1: "rollback", 2: "winner"}


def _provenance_overrides(p: Proposal) -> dict[tuple[str, str], dict[str, Any]]:
    """The proposal behind each (cohort, dim) it changed, from its diff items (9.4 "why heard").
    Allocation changes of a routine proposal are experiment arms (welcome, voice) or caller-ID
    probes; a promotion is a winner, a rollback a rollback."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (p.diff or {}).get("items", ()):
        path = list(item.get("path", ()))
        if len(path) == 3 and path[0] == "alloc":
            cohort, dim = path[1], path[2]
        elif path == ["prompt_version"]:
            cohort, dim = "default", "prompt_version"
        else:
            continue
        source = _PROV_SOURCE_BY_CLASS.get(p.cls) or (
            "probe" if dim == "caller_id" else "arm" if dim in ("welcome_message", "voice") else "default")
        out[(cohort, dim)] = {"source": source, "proposal_id": p.proposal_id,
                              "approval_id": p.approval_id, "decision_id": p.decision_id}
    return out
