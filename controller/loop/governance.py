"""Governance in the real loop: the hooks that run the overlay, the proposal lifecycle and the
release train inside the orchestrator's stages, over Postgres and the write-ahead intents.

Owner: integration step I2 (milestone M3). Implements DESIGN.md 4.1 steps 4, 5, 8 and 9 (REFRESH's
supervisor-edit handler, LIFECYCLE, GOVERN, VERIFY), 2 restart step 5 (the proposal sweep), 6.1 to
6.7 through `governance/*` and `planner/overlay.py`, and 8.2 and 8.3 through
`governance/effects.py`. It also runs the probe-only learner (`learning/probe.py`) in DETECT and
DECIDE when `CTL_LEARNER=probe` or `probe_only`, and the engagement learner (`loop/engagement.py`,
step I4, ADR 0031) in DETECT, DECIDE, the release train's composed-change gate, the lifecycle's
re-gate and verifiers, and the held-patch re-check, when `CTL_LEARNER=probe`. ADR 0029.

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
  DETECT     DID states (probe learner): SPRT on probe dials, slow path, persisted every step
  DECIDE     at each workflow's planning point (its routine slot minutes): learner patches, each
             with its decision row, into the overlay
  GOVERN     `ReleaseTrain.run`: compose, route, timing, act; one decision row per workflow that
             had something to do
  VERIFY     `ProposalLifecycle.verify` (kind verifiers are wired by later steps)
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from controller.audit.writer import record, write_decision
from controller.governance.builder import BuildContext
from controller.governance.effects import IntentEffects, attach_effect_hooks
from controller.governance.lifecycle import LifecycleDeps, ProposalLifecycle
from controller.governance.release import ReleaseDeps, ReleaseTrain
from controller.governance.store_pg import OverlayStore, PgProposalStore, TxScope, holds_of
from controller.governance.timing import choose_submit, routine_slots
from controller.learning.probe import OWNER_COHORTS, ProbeLearner, did_rows
from controller.loop.engagement import EngagementLoop
from controller.loop.orchestrator import LoopHooks, Runtime, StageCounts
from controller.obs.alerts import raise_alert
from controller.planner.overlay import LIVE_STATES, OverlayContext, dominant
from controller.snapshot import load_snapshot
from controller.store import repos
from controller.types import (DEFAULT_COHORT, IN_FLIGHT_STATES, CohortDef, Config,
                              ConfigChange, Decision, DidState, EffectivePolicy, FrozenDict,
                              Hold, PatchRequest, Proposal, Tx, WorkflowSnapshot, canonical_json,
                              freeze, thaw)
from controller.world.configs import load_cells
from controller.world.directives import load_directives
from controller.world.registry import WorkflowRegistry

log = logging.getLogger(__name__)

SCHEDULE_REVERT_LOOKBACK_MIN = 7 * 1440
"""classify_risk: a schedule table equal to one live in the last 7 days is a revert (6.5)."""


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
        self.lc = ProposalLifecycle(LifecycleDeps(
            store=self.store, overlay=self.overlay, sim=rt.client,
            executor=IntentEffects(rt.intents, self.scope), policy_of=self.registry.policy,
            clock=self._clock, epoch=rt.epoch, run6=rt.run6,
            settings_hash=rt.settings.settings_hash(), code_version=rt.settings.env.code_version,
            t=self.t, holds=self._holds, on_alert=self._alert, on_applied=self._on_applied,
            regate=self.engagement.regate if self.engagement else None,
            verifiers=self.engagement.verifiers() if self.engagement else {},
            on_rollback=self.engagement.on_verify_failed if self.engagement
            else (lambda p, r: None)))
        self.release = ReleaseTrain(ReleaseDeps(
            overlay=self.overlay, lifecycle=self.lc, policy_of=self.registry.policy,
            workflows=self.registry.workflow_ids, live=self._live,
            build_context=self._build_context, holds=self._holds,
            suspect_since=self._suspect_since, t=self.t,
            gate=self.engagement.gate if self.engagement else None))
        attach_effect_hooks(rt.registry, lambda: self.lc, self.scope)

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
                     or r.applied_minute > self.now - SCHEDULE_REVERT_LOOKBACK_MIN)]

    def _build_context(self, wf: str) -> BuildContext:
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
        return self.engagement.enrich(wf, ctx) if self.engagement else ctx

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
        return {"planned_slots": tuple(sorted(s for s in slots if s > now)),
                "notice_expiries": tuple(sorted(notices))}

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
        fresh read clears the post-403 auto-apply block."""
        changes = self.registry.load(rt.client.workflows(), rt.tick)
        if not changes:
            return
        with self.scope.tx() as tx:
            for c in changes:
                record(tx, wf=c.workflow_id, actor="controller", action="policy_changed",
                       subject=("workflow", c.workflow_id), summary=f"{c.field} changed",
                       details={"old": str(c.old), "new": str(c.new)})
        for wf in {c.workflow_id for c in changes}:
            self.lc.policy_reread(wf)

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

    def detect(self, rt: Runtime, now: int) -> StageCounts | None:
        """DID states every step (the SPRT reads each tick's probe dials once)."""
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
                changed = {n: s for n, s in states.items() if ws.did_state.get(n) != s}
                if changed:
                    self._write_did_states(tx, wf, changed)
                for f in findings:
                    write_decision(tx, self._decision(
                        wf, "learning.did", "did_state", f"5.9/{f.kind}",
                        {"did": f.scope, "stat": f.stat, "threshold": f.threshold},
                        {"status": changed[f.scope].status} if f.scope in changed else {}))
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
        self.now = now
        if self.learner is None:
            return None
        n = 0
        for wf in self.registry.workflow_ids():
            if not self.is_planning_point(wf, now):
                continue
            with self.scope.tx() as tx:
                ws = self._snapshot(tx, wf)
                reqs = self.learner.plan(ws, self.business[wf])
                if self.engagement is not None:
                    reqs = self.engagement.guardrail_filter(tx, ws, reqs)
                for req in reqs:
                    if self._already_wanted(req):
                        continue
                    did = write_decision(tx, self._decision(
                        wf, req.owner, "patch", _rule_of(req),
                        {"path": list(req.path), "evidence": thaw(req.evidence)},
                        {"value": _json_value(req.value), "class": req.cls}))
                    self.overlay.add(dataclasses.replace(req, decision_id=did))
                    n += 1
        if self.engagement is not None:
            n += self.engagement.decide(now)
        return StageCounts(decisions=n)

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
                               "planned_minute": d.planned_minute}))
            if d.action == "no_safe_window":
                raise_alert(tx, "NO_SAFE_WINDOW", self.rt.catalog.code(d.wf), d.wf, d.reason,
                            sim_minute=now)

    def verify(self, rt: Runtime, now: int) -> StageCounts | None:
        self.now = now
        self.lc.verify(now)
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

