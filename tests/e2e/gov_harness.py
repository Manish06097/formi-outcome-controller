"""A minimal governance harness for e2e tests (track T2): registry, overlay, lifecycle and
release train wired to a real simulator, stepping time itself.

`DirectExecutor` calls the SimClient without write-ahead intents. It is a TEST stand-in for
track T1's `reliability.intents.IntentExecutor` (same `execute(kind, key, payload, decision)`
shape); production code never uses it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from controller.governance.builder import BuildContext
from controller.governance.lifecycle import (InMemoryProposalStore, LifecycleDeps,
                                             ProposalLifecycle)
from controller.governance.release import ReleaseDecision, ReleaseDeps, ReleaseTrain
from controller.planner.overlay import Overlay, OverlayContext
from controller.simclient import SimClient
from controller.types import (IN_FLIGHT_STATES, Decision, FrozenDict, PatchRequest, SimEpoch)
from controller.world.clock import in_approver_hours
from controller.world.directives import load_directives
from controller.world.registry import WorkflowRegistry


class DirectExecutor:
    def __init__(self, client: SimClient):
        self.c = client
        self.calls: list[tuple[str, str]] = []

    def execute(self, kind: str, key: str, payload: dict[str, Any], decision: Decision, *,
                before=None) -> Any:
        self.calls.append((kind, key))
        if before is not None:
            before()
        if kind == "submit_approval":
            return self.c.request_approval(payload["body"])
        if kind == "apply":
            return self.c.apply(payload["workflow_id"], payload["approval_id"])
        if kind == "auto_apply":
            return self.c.auto_apply(payload["workflow_id"], payload["body"])
        raise ValueError(kind)


@dataclass
class Harness:
    client: SimClient
    run_dir: Path
    now: int = 0
    decisions: list[tuple[int, ReleaseDecision]] = field(default_factory=list)
    alerts: list[tuple[str, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        st = self.client.state()
        self.now = st.sim_minute
        self.tick = st.tick_minutes
        self.epoch = SimEpoch.from_state(st)
        self.registry = WorkflowRegistry(load_directives())
        self.registry.load(self.client.workflows(), self.tick)
        self.store = InMemoryProposalStore()
        self.overlay = Overlay(self._ctx, lambda: self.now,
                               in_flight=lambda wf: self.store.in_states(IN_FLIGHT_STATES, wf))
        self.executor = DirectExecutor(self.client)
        self.lifecycle = ProposalLifecycle(LifecycleDeps(
            store=self.store, overlay=self.overlay, sim=self.client, executor=self.executor,
            policy_of=self.registry.policy, clock=lambda: self.now, epoch=self.epoch,
            run6="e2e001", on_alert=lambda c, w, m: self.alerts.append((c, w, m))))
        self.release = ReleaseTrain(ReleaseDeps(
            overlay=self.overlay, lifecycle=self.lifecycle, policy_of=self.registry.policy,
            workflows=self.registry.workflow_ids, live=self._live,
            build_context=self._build_ctx))
        self._seq = 0
        self.known = {wf: self._live(wf) for wf in self.registry.workflow_ids()}
        self.rebases: list = []

    def _ctx(self, wf: str) -> OverlayContext:
        spec = self.registry.spec(wf)
        dids = {d: "healthy" for d in spec.options["caller_id"]}
        return OverlayContext(spec=spec, did_state=dids,
                              controls={("default", "voice"): "V1",
                                        ("default", "welcome_message"): "W1"})

    def _live(self, wf: str):
        snap = self.client.config(wf)
        return snap.version, snap.config

    def _build_ctx(self, wf: str) -> BuildContext:
        spec = self.registry.spec(wf)
        return BuildContext(primary_kpi=spec.objective.primary,
                            batch_size=spec.daily_batch.size, mid_flight=spec.mid_flight)

    # ------------------------------------------------------------ driving
    def patch(self, wf: str, path: tuple[str, ...], value: Any, cls: int = 3,
              owner: str = "test") -> None:
        self._seq += 1
        self.overlay.add(PatchRequest(wf=wf, owner=owner, path=path, value=value, cls=cls,
                                      p0_rank=0 if cls == 0 else None,
                                      evidence=FrozenDict({"rule_id": "e2e"}),
                                      decision_id=self._seq))

    def observe_versions(self) -> None:
        """4.1 step 4 stand-in (track T1 owns it): on a supervisor version, rebase the overlay
        and mark older pending work STALE before the lifecycle runs."""
        for wf in self.registry.workflow_ids():
            version, cfg = self._live(wf)
            old_v, old_cfg = self.known[wf]
            if version == old_v:
                continue
            actors = {v.version: v.actor for v in self.client.config_history(wf)}
            if any(actors.get(v) == "supervisor" for v in range(old_v + 1, version + 1)):
                self.rebases.append(self.overlay.rebase_on_supervisor_edit(wf, old_cfg, cfg,
                                                                           version))
                self.lifecycle.mark_stale(wf, version, self.now)
            self.known[wf] = (version, cfg)

    def step(self) -> None:
        """One stop: refresh, lifecycle (apply visible approvals), then the release train."""
        self.observe_versions()
        self.lifecycle.tick(self.now)
        for d in self.release.run(self.now):
            if d.action not in ("idle",):
                self.decisions.append((self.now, d))
        for wf in self.registry.workflow_ids():      # our own versions are known bodies
            self.known[wf] = self._live(wf)

    def advance(self, minutes: int) -> None:
        r = self.client.advance(minutes)
        self.now = r.sim_minute

    def run_until(self, end: int, night_from_h: int = 21, morning_h: int = 7) -> None:
        """15-minute stops in the day; one jump overnight. A jump is only taken when no
        decision can land before the morning (every approver window closed) and nothing of
        ours is approved-but-unapplied."""
        while self.now < end:
            self.step()
            m = self.now % 1440
            approved = self.store.in_states(("approved", "applying"))
            open_any = any(in_approver_hours(self.now, self.registry.policy(w).approver_hours)
                           for w in self.registry.workflow_ids())
            if m >= night_from_h * 60 and not approved and not open_any:
                nxt = (self.now // 1440 + 1) * 1440 + morning_h * 60
                self.advance(min(nxt, end) - self.now)
            else:
                self.advance(self.tick)
        self.step()

    # ------------------------------------------------------------ evidence from the run dir
    def sim_log(self) -> list[dict[str, Any]]:
        p = self.run_dir / "sim_log.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def log_kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.sim_log():
            k = e.get("kind") or e.get("event") or e.get("type")
            out[k] = out.get(k, 0) + 1
        return out
