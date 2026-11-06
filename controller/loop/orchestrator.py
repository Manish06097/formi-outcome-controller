"""The control loop: startup (lock, run resolution, reconcile, catch-up ingest, bootstrap) and the
per-step PLAN .. OPS sequence.

Owner: track T1 (milestone M2). Implements DESIGN.md 2 (restart sequence), 4.1 (per-step
sequence), 4.2 (the step rule, via `loop/stepper.py`), 8.2 and 8.3 (every advance goes through the
write-ahead intent protocol and is reconciled after a crash) and the `Orchestrator` interface of
11.2.

Per step:
  1  PLAN      next stop from `plan_stop` over a PlannerContext built from the API and the store
  2  ADVANCE   decision + intent `adv:<from>-><to>` committed, POST /sim/advance, result and
               clock committed together; never blindly retried
  3  INGEST    the advance's chunks plus any gap; decisions wait until ingest lag is 0
  4  REFRESH   GET /sim/state; config bodies of new versions (ConfigMirror); the
               supervisor-edit handler hook, in one transaction with an audit row per version
  5-9          LIFECYCLE, DETECT, DECIDE, GOVERN, VERIFY: hooks (`LoopHooks`), no-ops until the
               governance, learning and incident tracks plug in
 10  OPS       loop_step row, run registry
 11  finished: final ingest and reconcile, run_registry.status = done, exit 0

Exit codes: 0 the scenario finished; 1 anything transient (run.sh restarts us, and the restart
sequence makes that safe); 3 a fatal configuration error (run.sh does not restart).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from controller.audit.writer import config_version_subject, record, write_decision
from controller.ingest.loader import reset_stage
from controller.ingest.pipeline import IngestError, IngestPipeline
from controller.loop.stepper import StopPlan, basic_policy, plan_state, plan_stop
from controller.obs.alerts import raise_alert
from controller.obs.metrics import Heartbeat, PhaseTimer, rss_mb
from controller.reliability import faults
from controller.reliability.intents import (IntentAborted, IntentExecutor, IntentRegistry,
                                            Reconciler, default_registry)
from controller.settings import Settings
from controller.simclient.client import SimClient
from controller.simclient.errors import BadMinutes, Finished, SimTransportError
from controller.store import repos
from controller.store.db import AdvisoryLock, Database, MigrationError
from controller.store.repos import WorkflowCatalog
from controller.types import (AdvanceResult, ConfigChange, ConfigEvent, Decision,
                              EffectivePolicy, FrozenDict, IngestReport, PlannerContext,
                              ReconcileReport, ResumeReport, SimEpoch, SimState, StepReport, Tx,
                              advance_key)
from controller.world.clock import floor_to
from controller.world.configs import ConfigMirror, cell_count
from controller.world.fingerprint import WorldIdentity, resolve_run, world_identity

log = logging.getLogger(__name__)

EXIT_DONE, EXIT_RESTART, EXIT_FATAL = 0, 1, 3


class FatalConfig(RuntimeError):
    """A configuration error a restart cannot fix (exit 3)."""


# =============================================================================== markers and hooks

@dataclass(frozen=True)
class Marker:
    """A synthetic governance marker for tests of the step rule (DESIGN.md 15.1 M2), parsed from
    `EnvSettings.synthetic_markers` (CTL_SYNTHETIC_MARKERS)."""

    kind: str
    """pending | approved"""
    workflow_id: str
    from_minute: int
    to_minute: int
    """pending: active while from <= now < to. approved: apply_by = to."""


@dataclass
class StageCounts:
    decisions: int = 0
    proposals_submitted: int = 0
    applies: int = 0

    def add(self, other: "StageCounts | None") -> None:
        if other is not None:
            self.decisions += other.decisions
            self.proposals_submitted += other.proposals_submitted
            self.applies += other.applies


class LoopHooks:
    """Extension points of the loop. Every method is a no-op here; the governance, learning and
    incident tracks subclass this (or compose several) and pass it to the Orchestrator.

    Stage hooks return StageCounts (or None) for the loop_step row. `on_config_changes` runs in
    the REFRESH transaction (the supervisor-edit handler, DESIGN.md 6.7)."""

    def policy(self, rt: "Runtime", workflow_id: str) -> EffectivePolicy | None:
        return None

    def plan_facts(self, rt: "Runtime", now: int, workflow_id: str) -> Mapping[str, Any]:
        """Extra WorkflowPlanState fields (planned_slots, notice_expiries, suspect_prompt...)."""
        return {}

    def bootstrap(self, rt: "Runtime") -> StageCounts | None:
        return None

    def full_sweep(self, rt: "Runtime") -> None:
        """Restart step 5: every non-terminal proposal against GET /approvals and history."""

    def on_config_changes(self, rt: "Runtime", tx: Tx, changes: list[ConfigChange]) -> None:
        pass

    def lifecycle(self, rt: "Runtime", now: int) -> StageCounts | None:
        return None

    def detect(self, rt: "Runtime", now: int) -> StageCounts | None:
        return None

    def decide(self, rt: "Runtime", now: int) -> StageCounts | None:
        return None

    def govern(self, rt: "Runtime", now: int) -> StageCounts | None:
        return None

    def verify(self, rt: "Runtime", now: int) -> StageCounts | None:
        return None

    def on_finished(self, rt: "Runtime") -> None:
        pass


@dataclass
class Runtime:
    """What hooks may use. Rebuilt from Postgres on every start (only Postgres holds state)."""

    settings: Settings
    db: Database
    client: SimClient
    ident: WorldIdentity
    schema: str
    catalog: WorkflowCatalog
    epoch: SimEpoch
    mirror: ConfigMirror
    ingest: IngestPipeline
    intents: IntentExecutor
    registry: IntentRegistry
    state: SimState
    run6: str
    """Prefix of our proposal ids: the fingerprint's first 6 characters, so two runs of one
    world issue identical ids (DESIGN.md 8.6); the simulator's id namespace is per world."""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def tick(self) -> int:
        return self.state.tick_minutes

    def decision(self, *, wf: str | None, module: str, kind: str, rule_id: str,
                 inputs: Mapping[str, Any], outcome: Mapping[str, Any] | None = None,
                 sim_minute: int | None = None) -> Decision:
        return Decision(wf=wf, module=module, kind=kind, rule_id=rule_id,
                        inputs=FrozenDict(inputs), outcome=FrozenDict(outcome or {}),
                        harm_report=None, snapshot_ids=(), seed=0,
                        settings_hash=self.settings.settings_hash(),
                        code_version=self.settings.env.code_version, sim_minute=sim_minute)


# =============================================================================== orchestrator

class Orchestrator:
    """`Orchestrator` of DESIGN.md 11.2."""

    def __init__(self, settings: Settings, hooks: LoopHooks | None = None):
        self.settings = settings
        self.hooks = hooks or LoopHooks()
        self.markers = tuple(Marker(*m) for m in settings.env.synthetic_markers)
        self.client = SimClient.from_settings(settings)
        self.db = Database.from_settings(settings)
        self.lock = AdvisoryLock(settings.env.dsn, settings.env.sim_url)
        self.rt: Runtime | None = None
        self.heartbeat: Heartbeat | None = None
        self._step_no = 0
        self._adv_wall_per_sim_h: float | None = None

    # ------------------------------------------------------------------ resume (section 2)
    def resume(self) -> ResumeReport:
        env = self.settings.env
        log.info("waiting for the simulator at %s", env.sim_url)
        try:
            self.client.wait_healthy(env.sim_wait_s, env.health_poll_s)
        except SimTransportError as e:
            raise FatalConfig(f"simulator at {env.sim_url} never answered /health: {e}") from e
        self.lock.acquire(poll_s=env.lock_poll_s,
                          on_wait=lambda w: log.warning("another controller holds the lock "
                                                        "for %s (%.0fs)", env.sim_url, w))
        ident = world_identity(self.client)
        res = resolve_run(self.db, self.client, ident, env.sim_url)
        self.db.open(res.schema)
        self._install_faults()
        epoch = SimEpoch.from_state(ident.state)
        with self.db.tx() as tx:
            catalog = repos.register_workflows(tx, ident.specs, ident.state.sim_minute)
            repos.init_clock(tx, ident.state)
            mirror = ConfigMirror(self.client, catalog, epoch)
            mirror.bootstrap(tx)
            if res.rejected:
                raise_alert(tx, "NEW_WORLD", None, res.schema,
                            f"started fresh schema {res.schema}; rejected: " + "; ".join(
                                f"{r.schema}: {', '.join(r.failures)}" for r in res.rejected))
            self._step_no = repos.get_clock(tx).last_step_no
        registry = default_registry()
        ingest = IngestPipeline(self.db, self.client, int(epoch.epoch0.timestamp()),
                                ident.state.tick_minutes, parser=env.ingest,
                                loader_source=SimClient.from_settings(self.settings),
                                prefetch=env.prefetch_chunks)
        executor = IntentExecutor(self.db, self.client, registry)
        self.rt = rt = Runtime(self.settings, self.db, self.client, ident, res.schema, catalog,
                               epoch, mirror, ingest, executor, registry, ident.state,
                               run6=ident.fingerprint[:6])
        self._attach_advance(registry)
        self.heartbeat = Heartbeat(self.db, self.lock, env.heartbeat_s,
                                   lambda: self._step_no).start()
        reset_stage(self.db)                                           # restart step 2
        reconcile = Reconciler(self.db, self.client, registry).run()   # restart step 3
        self._adopt_simulator_clock()
        cells_before = self._cells()
        caught = rt.ingest.catch_up()                                  # restart step 4
        self._after_ingest(cells_before)
        self._refresh()
        self.hooks.full_sweep(rt)                                      # restart step 5
        rerun = self._bootstrap()                                      # restart step 6
        clock = self._clock()
        log.info("run %s %s at %s (%d chunks caught up, %d intents reconciled)", res.schema,
                 "resumed" if res.resumed else "started", clock.sim_minute, len(caught.chunks),
                 len(reconcile.resolved))
        return ResumeReport(schema_name=res.schema, resumed=res.resumed,
                            sim_minute=clock.sim_minute, reconcile=reconcile,
                            chunks_ingested=len(caught.chunks), bootstrap_rerun=rerun,
                            alerts=reconcile.alerts)

    def _install_faults(self) -> None:
        try:
            specs = faults.parse_faults(self.settings.env.fault)
        except ValueError as e:
            raise FatalConfig(str(e)) from e
        if not specs:
            return

        def fired(spec: faults.FaultSpec) -> bool:
            with self.db.tx() as tx:
                return tx.execute("""SELECT 1 FROM audit_log WHERE action = 'fault_injected'
                                     AND subject_id = %s""", (spec.text(),)).fetchone() is not None

        def on_fire(spec: faults.FaultSpec) -> None:
            with self.db.tx() as tx:
                record(tx, wf=None, actor="fault_injector", action="fault_injected",
                       subject=("fault", spec.text()), summary=f"CTL_FAULT {spec.text()}",
                       details={})

        faults.install(faults.FaultInjector(specs, on_fire=on_fire, already_fired=fired))

    def _adopt_simulator_clock(self) -> None:
        """After reconciliation our clock equals the simulator's (the advance protocol commits
        them together). If not, the store contradicts the contract: page and follow the
        simulator, since an advance is relative and cannot be undone."""
        st = self.client.state()
        self._rt().state = st
        with self.db.tx() as tx:
            clock = repos.get_clock(tx)
            if clock.sim_minute != st.sim_minute:
                raise_alert(tx, "CLOCK_MISMATCH", None, "clock",
                            f"our clock {clock.sim_minute}, simulator {st.sim_minute}")
                repos.set_clock(tx, st.sim_minute)
            self._step_no = repos.get_clock(tx).last_step_no

    def _bootstrap(self) -> bool:
        """Run the bootstrap once (restart step 6): rerun while `completed_minute` is NULL."""
        rt = self._rt()
        with self.db.tx() as tx:
            row = repos.bootstrap_row(tx)
            if row is not None and row.completed_minute is not None:
                return False
            cur = repos.ingest_cursor(tx)
            hist = tx.execute("SELECT count(*) FROM ingested_chunk WHERE kind = 'history'"
                              ).fetchone()[0]
            d = rt.decision(wf=None, module="loop", kind="bootstrap", rule_id="bootstrap_once",
                            inputs={"history_chunks": hist, "events": cur.events,
                                    "rerun": row is not None},
                            sim_minute=repos.get_clock(tx).sim_minute)
            did = write_decision(tx, d)
            repos.start_bootstrap(tx, repos.get_clock(tx).sim_minute, hist, did)
        faults.maybe_crash("bootstrap", "mid")
        self.hooks.bootstrap(rt)
        with self.db.tx() as tx:
            repos.complete_bootstrap(tx, repos.get_clock(tx).sim_minute)
            record(tx, wf=None, actor="controller", action="bootstrap_completed",
                   subject=("bootstrap", "1"), summary="bootstrap completed",
                   details={"rerun": row is not None})
        return row is not None

    # ------------------------------------------------------------------ the loop
    def run(self) -> int:
        try:
            self.resume()
            steps = 0
            while True:
                if self._finished():
                    self._finish()
                    return EXIT_DONE
                report = self.run_step()
                steps += 1
                if report.finished:
                    self._finish()
                    return EXIT_DONE
                stop_after = self.settings.env.stop_after_steps
                if stop_after and steps >= stop_after:
                    log.warning("CTL_STOP_AFTER_STEPS=%d reached: exiting 1", stop_after)
                    return EXIT_RESTART
        except (FatalConfig, MigrationError, BadMinutes) as e:
            log.critical("fatal configuration error: %s", e)
            return EXIT_FATAL
        except Exception:
            log.exception("controller failed; run.sh restarts it")
            return EXIT_RESTART
        finally:
            if self.heartbeat:
                self.heartbeat.stop()

    def run_step(self) -> StepReport:
        rt = self._rt()
        timer = PhaseTimer()
        clock = self._clock()
        now = clock.sim_minute
        counts = StageCounts(decisions=1)
        with timer("plan"):
            ctx = self.planner_context(now)
            plan = self._plan(now, ctx)
        with timer("advance"):
            result = self._advance(now, plan, ctx)
        with timer("ingest"):
            cells_before = self._cells()
            ing = rt.ingest.ingest(list(result.new_chunks))
            gap = rt.ingest.ingest(rt.ingest.pending_chunks())
            self._after_ingest(cells_before)
        with timer("refresh"):
            rt.state = state = self.client.state()
            self._check_lag(state)
            self._refresh()
        for stage in ("lifecycle", "detect", "decide", "govern", "verify"):
            with timer(stage):
                counts.add(getattr(self.hooks, stage)(rt, state.sim_minute))
        report = StepReport(step_no=self._step_no, from_minute=now, to_minute=state.sim_minute,
                            phase_ms=FrozenDict(timer.ms),
                            chunks=len(ing.chunks) + len(gap.chunks),
                            events=ing.events + gap.events,
                            orphan_calls=ing.orphan_calls + gap.orphan_calls,
                            decisions=counts.decisions,
                            proposals_submitted=counts.proposals_submitted,
                            applies=counts.applies, credits_remaining=state.credits_remaining,
                            finished=state.finished)
        with timer("ops"):
            self._write_step(report)
        log.info("step %d: %d -> %d (%s) %d chunks %d events in %d ms", report.step_no, now,
                 report.to_minute, plan.bound, report.chunks, report.events, timer.total_ms())
        return report

    # ------------------------------------------------------------------ PLAN
    def planner_context(self, now: int) -> PlannerContext:
        rt = self._rt()
        t = self.settings.t
        tick = rt.tick
        with self.db.tx() as tx:
            facts = repos.plan_facts(tx)
            end = repos.get_clock(tx).end_minute
        states = []
        for spec in rt.catalog.specs:
            wid = spec.workflow_id
            wf = rt.catalog.code(wid)
            pol = self.hooks.policy(rt, wid) or basic_policy(spec, tick, t)
            pending = facts.pending.get(wf)
            approved = list(facts.approved_apply_by.get(wf, ()))
            has_pending = pending is not None
            for m in self.markers:
                if m.workflow_id != wid:
                    continue
                if m.kind == "pending" and m.from_minute <= now < m.to_minute:
                    has_pending = True
                if m.kind == "approved" and m.from_minute <= now < m.to_minute:
                    approved.append(m.to_minute)
            extra = dict(self.hooks.plan_facts(rt, now, wid) or {})
            states.append(plan_state(
                spec, pol, has_pending=extra.pop("has_pending", has_pending),
                pending_class=extra.pop("pending_class", pending[0] if pending else None),
                pending_created=extra.pop("pending_created", pending[1] if pending else None),
                approved_apply_by=tuple(sorted(extra.pop("approved_apply_by", approved))),
                open_incident=extra.pop("open_incident", wf in facts.open_incident),
                suspect_did=extra.pop("suspect_did", wf in facts.suspect_did), **extra))
        base = max(tick, floor_to(t.base_step_min, tick))
        return PlannerContext(tick=tick, base_step=base, end_minute=end,
                              workflows=tuple(states))

    def _plan(self, now: int, ctx: PlannerContext) -> StopPlan:
        plan = plan_stop(now, ctx)
        if plan.overnight_jump:
            with self.db.tx() as tx:
                if repos.local_approved_count(tx):
                    raise_alert(tx, "APPLY_AT_RISK", None, "overnight_jump",
                                "overnight jump planned with a local APPROVED proposal")
        return plan

    # ------------------------------------------------------------------ ADVANCE
    def _attach_advance(self, registry: IntentRegistry) -> None:
        def done(tx: Tx, intent: Any, response: Mapping[str, Any]) -> None:
            repos.set_clock(tx, int(response["sim_minute"]), int(intent.request["step_no"]))

        registry.attach("advance", done=done)

    def _advance(self, now: int, plan: StopPlan, ctx: PlannerContext) -> AdvanceResult:
        rt = self._rt()
        minutes = plan.to_minute - now
        step_no = self._step_no + 1
        key = advance_key(now, plan.to_minute)
        timeout = self.settings.env.advance_timeout_min_s
        if self._adv_wall_per_sim_h:
            timeout = max(timeout, 20 * self._adv_wall_per_sim_h * minutes / 60)
        pending = [w.workflow_id for w in ctx.workflows if w.has_pending]
        approved = [w.workflow_id for w in ctx.workflows if w.approved_apply_by]
        d = rt.decision(wf=None, module="loop", kind="advance", rule_id="next_stop_4.2",
                        inputs={"from": now, "to": plan.to_minute, "step": plan.step,
                                "bound": plan.bound, "overnight_jump": plan.overnight_jump,
                                "watch_bound": plan.watch_bound, "pending": pending,
                                "approved": approved},
                        outcome={"status": "intended"}, sim_minute=now)
        payload = {"minutes": minutes, "from_minute": now, "to_minute": plan.to_minute,
                   "step_no": step_no, "timeout_s": round(timeout, 1)}
        t0 = time.monotonic()
        try:
            out = rt.intents.execute("advance", key, payload, d)
        except Finished:
            log.warning("advance refused: the scenario has finished")
            return AdvanceResult(sim_minute=rt.state.end_minute, ts=rt.state.ts,
                                 finished=True, new_chunks=())
        except IntentAborted as e:
            if e.resolution != "unknown":
                log.warning("advance %s did not run; re-planning", key)
                return AdvanceResult(sim_minute=now, ts=rt.state.ts, finished=False,
                                     new_chunks=())
            self._adopt_simulator_clock()
            st = rt.state
            return AdvanceResult(sim_minute=st.sim_minute, ts=st.ts, finished=st.finished,
                                 new_chunks=())
        wall = time.monotonic() - t0
        rate = wall / max(minutes / 60, 0.25)
        self._adv_wall_per_sim_h = rate if self._adv_wall_per_sim_h is None else (
            0.7 * self._adv_wall_per_sim_h + 0.3 * rate)
        self._step_no = step_no
        return AdvanceResult.from_api(out)

    # ------------------------------------------------------------------ INGEST and REFRESH
    def _cells(self) -> int:
        with self.db.tx() as tx:
            return cell_count(tx)

    def _after_ingest(self, cells_before: int) -> None:
        with self.db.tx() as tx:
            if cell_count(tx) != cells_before:
                self._rt().mirror.refresh_effective_cells(tx)

    def _check_lag(self, state: SimState) -> None:
        """Decisions wait until ingest lag is 0 (DESIGN.md 4.1 step 3)."""
        rt = self._rt()
        with self.db.tx() as tx:
            cur = repos.ingest_cursor(tx)
        if cur.max_event_id == state.last_event_id and cur.chunks == state.chunks:
            return
        log.warning("ingest lag: ours %d events / %d chunks, simulator %d / %d; catching up",
                    cur.max_event_id, cur.chunks, state.last_event_id, state.chunks)
        rt.ingest.catch_up()
        with self.db.tx() as tx:
            cur = repos.ingest_cursor(tx)
            if cur.max_event_id != state.last_event_id:
                raise_alert(tx, "INGEST_STALLED", None, "ingest",
                            f"ingested up to event {cur.max_event_id}, simulator at "
                            f"{state.last_event_id}")
        if cur.max_event_id != state.last_event_id:
            raise IngestError(f"ingest lag after catch-up: {cur.max_event_id} vs "
                              f"{state.last_event_id}")

    def _refresh(self) -> list[ConfigChange]:
        """REFRESH for every config_changed event not yet handled (also the ones ingested by a
        process that crashed before its REFRESH): bodies, the hook, one audit row per version,
        all in one transaction."""
        rt = self._rt()
        with self.db.tx() as tx:
            events = unrefreshed_config_events(tx)
            if not events:
                return []
            changes = rt.mirror.refresh(tx, events)
            mark_refreshed(tx, events)
            for ch in changes:
                record(tx, wf=ch.workflow_id, actor="controller", action="config_refreshed",
                       subject=("config_version",
                                config_version_subject(ch.workflow_id, ch.version)),
                       summary=f"{ch.workflow_id} v{ch.version} by {ch.actor} "
                               f"({ch.body_source} body): {', '.join(ch.changed_dims)}",
                       details={"changed_paths": [list(p) for p in ch.changed_paths],
                                "body_source": ch.body_source})
            self.hooks.on_config_changes(rt, tx, changes)
        return changes

    # ------------------------------------------------------------------ OPS and finish
    def _write_step(self, report: StepReport) -> None:
        rt = self._rt()
        with self.db.tx() as tx:
            repos.insert_loop_step(tx, report, report.orphan_calls, rss_mb(), repos.db_bytes(tx))
        self.db.update_run(rt.schema, last_sim_minute=report.to_minute)

    def _finished(self) -> bool:
        clock = self._clock()
        return clock.sim_minute >= clock.end_minute or self._rt().state.finished

    def _finish(self) -> None:
        rt = self._rt()
        rt.ingest.catch_up()
        rt.state = state = self.client.state()
        self._check_lag(state)
        self._refresh()
        Reconciler(self.db, self.client, rt.registry).run()
        self.hooks.on_finished(rt)
        with self.db.tx() as tx:
            record(tx, wf=None, actor="controller", action="run_finished",
                   subject=("world", rt.ident.fingerprint[:12]), summary="scenario finished",
                   details={"sim_minute": state.sim_minute, "events": state.last_event_id})
        self.db.update_run(rt.schema, status="done", last_sim_minute=state.sim_minute)
        log.info("scenario finished at %d; run %s done", state.sim_minute, rt.schema)

    # ------------------------------------------------------------------ helpers
    def _rt(self) -> Runtime:
        if self.rt is None:
            raise RuntimeError("resume() has not run")
        return self.rt

    def _clock(self) -> repos.ClockRow:
        with self.db.tx() as tx:
            return repos.get_clock(tx)


def unrefreshed_config_events(tx: Tx) -> list[ConfigEvent]:
    """config_changed events REFRESH has not handled yet (`refreshed_minute IS NULL`)."""
    rows = tx.execute(
        """SELECT e.event_id, w.workflow_id, e.version, e.actor, e.changed_dims, e.proposal_id,
                  e.approval_id, e.minute
           FROM config_event e JOIN workflow w USING (wf)
           WHERE e.refreshed_minute IS NULL
           ORDER BY e.event_id""").fetchall()
    return [ConfigEvent(event_id=r[0], workflow_id=r[1], version=r[2], actor=r[3],
                        changed_dims=tuple(r[4] or ()), proposal_id=r[5], approval_id=r[6],
                        minute=r[7]) for r in rows]


def mark_refreshed(tx: Tx, events: list[ConfigEvent]) -> None:
    """Stamp handled events with the clock minute, in REFRESH's transaction (migration 0011).
    The clock is the same after a crash and resume (resume refreshes before the next advance),
    so the column is identical to a clean run's."""
    tx.execute("""UPDATE config_event SET refreshed_minute = (SELECT sim_minute FROM clock
                                                              WHERE id = 1)
                  WHERE event_id = ANY(%s)""", ([e.event_id for e in events],))


__all__ = ["Orchestrator", "LoopHooks", "Runtime", "StageCounts", "Marker",
           "FatalConfig", "EXIT_DONE", "EXIT_RESTART", "EXIT_FATAL", "ReconcileReport",
           "IngestReport"]
