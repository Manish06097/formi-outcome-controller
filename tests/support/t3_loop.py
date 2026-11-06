"""A minimal learning loop over the synthetic world, wired from the real T3 modules.

It stands in for the orchestrator (track T5) only as far as the learning loop needs. Time moves
in steps of 1 / `steps_per_seg` of a segment (default 4: 1.5 dialling hours on a 12-hour
window). Every `plan_every` steps, with data current to that moment, it fits the model
(stationary and EW), folds the decay monitor over completed segments, and then, in this order
and one change in flight at a time: a decay rollback, a harm stop, a winner promotion (with its
holdout), or an allocation plan. A change is applied `apply_delay` steps later (the approval
latency, passed to the gates as t_apply) and graded at that moment with the grader's rule
against the world's truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from controller.learning.allocator import harm_stops, plan_allocation_detail
from controller.learning.decay import PageDecayMonitor
from controller.learning.engagement import BetaBinomialModel, RidgeLogitModel
from controller.learning.winners import evaluate_looks
from controller.settings import Thresholds
from controller.snapshot import HoldoutState, LookRecord
from controller.stats.draws import derive_seed
from controller.types import Config, Experiment, ExperimentArm
from tests.support.t3_world import SyntheticWorld


@dataclass
class Change:
    pos_planned: float
    pos_applied: float
    kind: str
    verdict: str
    de: float
    detail: dict[str, Any] = field(default_factory=dict)
    old: Config | None = None
    new: Config | None = None

    def raises(self, dim: str, arm: str) -> bool:
        """Did this change raise `arm`'s share in any cohort (a ramp or promotion of it)?"""
        if self.old is None or self.new is None:
            return False
        keys = list(self.new.cohort_ids()) + ["default"]
        return any(self.new.dist(k, dim).get(arm, 0.0) > self.old.dist(k, dim).get(arm, 0.0)
                   + 1e-9 for k in keys)


@dataclass
class LoopResult:
    changes: list[Change]
    looks: list[LookRecord]
    winners: list[Any]
    final: Config
    experiments: tuple[Experiment, ...]
    w1_crossings: dict[tuple[str, str, str], int]
    """(exp, arm, scope) -> the first look number at which W1 alone held"""


def _set_arm_state(exps: tuple[Experiment, ...], exp_id: str, arm: str, scope: str,
                   state: str, minute: int) -> tuple[Experiment, ...]:
    out = []
    for e in exps:
        if e.exp_id == exp_id:
            rows = [r for r in e.arm_rows if not (r.arm == arm and r.scope == scope)]
            rows.append(ExperimentArm(exp_id=exp_id, arm=arm, scope=scope, state=state,
                                      share=0.0, removed_minute=minute))
            e = replace(e, arm_rows=tuple(rows))
        out.append(e)
    return tuple(out)


def _apply_patches(cfg: Config, patches) -> Config:
    for p in patches:
        cfg = cfg.with_allocation(p.path[1], p.path[2], dict(p.value or {}))
    return cfg


def run_loop(world: SyntheticWorld, cfg: Config, days: int, *, seed: int = 0,
             model: str = "beta_binomial", steps_per_seg: int = 4, plan_every: int = 2,
             apply_delay: int = 1, thresholds: Thresholds | None = None,
             rollbacks: bool = True, promote: bool = True) -> LoopResult:
    t = thresholds or Thresholds()
    m = BetaBinomialModel(t) if model == "beta_binomial" else RidgeLogitModel(t)
    exps: tuple[Experiment, ...] = ()
    looks: list[LookRecord] = []
    winners: list[Any] = []
    holdouts: list[HoldoutState] = []
    changes: list[Change] = []
    pending: list[tuple[int, Config, str, dict[str, Any]]] = []
    mon = PageDecayMonitor(t)
    w1: dict[tuple[str, str, str], int] = {}
    version = 1
    frac = 1.0 / steps_per_seg
    for step in range(2 * days * steps_per_seg):
        pos = step * frac
        seg = int(pos)
        for item in [p for p in pending if p[0] == step]:
            _, new, kind, info = item
            if not cfg.equivalent(new):
                verdict, de = world.grade(cfg, new, pos)
                changes.append(Change(info["planned"], pos, kind, verdict, de, info, cfg, new))
                cfg = new
                version += 1
                if kind == "promotion":   # the winner is monitored from its effect now
                    ws_p = world.snapshot(cfg, seg, experiments=exps, holdouts=holdouts,
                                          version=version, now=world.minute(pos))
                    mon.restart(ws_p, info["scope"], info["dim"], info["arm"],
                                m.draws(m.fit(ws_p, "stationary"), derive_seed(seed, step, "r")))
        pending = [p for p in pending if p[0] > step]
        if step % plan_every == 0 and not pending:
            now = world.minute(pos)
            t_apply = world.minute((step + apply_delay) * frac)
            ws = world.snapshot(cfg, seg, experiments=exps, looks=looks, decay=mon.states(),
                                holdouts=holdouts, version=version, now=now)
            stat = m.draws(m.fit(ws, "stationary"), derive_seed(seed, step, "stationary"))
            ew = m.draws(m.fit(ws, "ew"), derive_seed(seed, step, "ew"))
            if seg > 0:
                mon.update(ws, seg - 1, stat)
                ws = replace(ws, decay=mon.states())
            info: dict[str, Any] = {"planned": pos}
            change: tuple[Config, str] | None = None
            if rollbacks:
                rb = mon.rollback_plan(ws, ew, t_apply)
                if rb:
                    change = (_apply_patches(cfg, rb), "rollback")
            if change is None:
                stops = harm_stops(ws, ew, settings=t)
                if stops:
                    change = (_apply_patches(cfg, stops), "harm_stop")
            if change is None and promote:
                out = evaluate_looks(ws, stat, ew, settings=t, t_apply=t_apply)
                looks += list(out.looks)
                for d in out.detail:
                    if d["w1"] >= d["c_k"]:
                        w1.setdefault((str(d["exp_id"]), str(d["arm"]), str(d["scope"])),
                                      int(d["look_no"]))
                for exp_id, arm, scope in out.futile:
                    exps = _set_arm_state(exps, exp_id, arm, scope, "futile", ws.now)
                if out.winners:
                    win = out.winners[0]
                    winners.append(win)
                    promo = out.promotions[(win.dim, win.scope)]
                    exp = next(e for e in exps if e.exp_id == win.exp_id)
                    exps = _set_arm_state(exps, exp.exp_id, win.arm, win.scope, "winner", ws.now)
                    for a in exp.arms:
                        if a not in (win.arm, exp.control):
                            exps = _set_arm_state(exps, exp.exp_id, a, win.scope, "removed",
                                                  ws.now)
                    key = "default" if win.scope == "population" else win.scope
                    h = dict(promo.dist(key, win.dim)).get(exp.control, 0.0)
                    holdouts.append(HoldoutState(wf=ws.spec.workflow_id, scope=win.scope,
                                                 dim=win.dim, winner_arm=win.arm,
                                                 control=exp.control, share=h))
                    info.update(arm=win.arm, scope=win.scope, dim=win.dim)
                    change = (promo, "promotion")
            if change is None:
                plan = plan_allocation_detail(ws, ew, settings=t, t_apply=t_apply)
                exps = tuple(exps) + plan.new_experiments
                if plan.patches:
                    change = (plan.proposed, plan.kind)
            if change is not None:
                pending.append((step + apply_delay, change[0], change[1], info))
        world.simulate_segment(cfg, seg, version=version, frac=frac, pos=pos + frac / 2)
    return LoopResult(changes=changes, looks=looks, winners=winners, final=cfg,
                      experiments=exps, w1_crossings=w1)
