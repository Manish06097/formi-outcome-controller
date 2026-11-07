"""ReleaseTrain: priority classes, precedence, batching and notice (DESIGN.md 6.2, 6.5).

Owner: track T2 (milestone M3). Implements DESIGN.md 6.2 (one change in flight, priority
classes, the P0a-before-P0b order and every row of the precedence table), the batching rule
(one proposal per workflow; auto-applicable operational patches first), and the notice handling
of 6.5 and 4.1. ADRs 0013 and 0025.

The rules are pure functions (`precedence`, `worst_remaining_minutes`, `drain_chain_steps`,
`notice_until`, `composition_classes`); `ReleaseTrain.run(now)` applies them per workflow with
the overlay, the policy replica, the timing search and the lifecycle, and returns one
`ReleaseDecision` per workflow (including decisions not to act) for the audit store.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from controller.governance.builder import BuildContext, RequestPackage, build_request
from controller.governance.lifecycle import ProposalLifecycle, SubmitBlocked
from controller.governance.policy import check_auto_apply, needs_notice
from controller.governance.timing import ROUTINE_CLASSES, choose_submit, p_expire, routine_slots
from controller.planner.overlay import CompositionInvalid, Overlay
from controller.settings import Thresholds
from controller.governance.policy import is_risk_reducing, tv_by_cohort_dim
from controller.types import (AGENT_DIMS, DEFAULT_COHORT, P0_INCIDENT, P0A_PROMPT_SAFETY,
                              P0B_DID_DRAIN, P1_ROLLBACK, Composition, Config, EffectivePolicy,
                              FrozenDict, Hold, Proposal, VelocityLedger)
from controller.world.clock import (ceil_to_tick, in_approver_hours, next_open,
                                    worst_decide_minute)

# ---------------------------------------------------------------- pure rules
CHAIN_SLACK = 1e-9
"""TV a chain step stays under `max_shift - margin`: far above float noise (1e-16), far below
anything that changes the drain."""

def drain_chain_steps(share: float, max_shift: float) -> int:
    """k = ceil(share / max_shift): one max_shift step per tick drains `share` (6.2, 7.3)."""
    if max_shift <= 0:
        raise ValueError("a drain chain needs max_shift > 0 (otherwise the drain is an approval)")
    return max(1, math.ceil(share / max_shift - 1e-12))


def chain_lambda(comp: Composition, pol: EffectivePolicy, t: Thresholds) -> float:
    """The share of the way to the target one chain step may go: TV is linear along the
    straight line from live to target, so lambda = cap / max TV keeps every (cohort, dim)
    within max_shift - margin. 1.0 when the whole change fits in one step."""
    cap = pol.max_shift - t.max_shift_margin
    worst = max(comp.tv_by_cohort_dim.values(), default=0.0)
    if worst <= cap or cap <= 0:
        return 1.0 if worst <= cap else 0.0
    # a hair under the cap: at exactly cap / worst the interpolated, normalised step lands on
    # the cap with float noise and fails the check about one time in six (ADR 0030)
    return (cap - CHAIN_SLACK) / worst


def interpolate(live: Config, target: Config, lam: float) -> Config:
    """live + lam x (target - live) on every allocation override that differs; everything else
    (cohorts, schedule, prompt) taken from live, so a chain step moves allocations only."""
    out = live
    for k in sorted(set(live.allocations) | set(target.allocations)):
        for dim in AGENT_DIMS:
            a = dict(live.dist(k, dim))
            b = dict(target.dist(k, dim))
            own = (target.allocations.get(k) or {}).get(dim) is not None or k == DEFAULT_COHORT
            if a == b or not own:
                continue
            mix = {o: a.get(o, 0.0) + lam * (b.get(o, 0.0) - a.get(o, 0.0))
                   for o in sorted(set(a) | set(b))}
            out = out.with_allocation(k, dim, {o: w for o, w in mix.items() if w > 1e-15})
    return out


def chain_steps_left(comp: Composition, pol: EffectivePolicy, t: Thresholds) -> int:
    """k_rem of 6.2 for a composition: steps of max_shift - margin to reach its target."""
    cap = pol.max_shift - t.max_shift_margin
    worst = max(comp.tv_by_cohort_dim.values(), default=0.0)
    if worst == 0:
        return 0
    if cap <= 0:
        raise ValueError("max_shift 0: no chain")
    return max(1, math.ceil(worst / cap - 1e-12))


def worst_remaining_minutes(holder: Proposal, now: int, pol: EffectivePolicy) -> int:
    """Worst-case minutes until the holder is decided and applied (6.2, P0b row)."""
    if holder.state == "pending" and holder.submitted_minute is not None:
        d = worst_decide_minute(holder.submitted_minute, pol.latency_minutes, pol.approver_hours)
        return max(0, ceil_to_tick(d, pol.tick) + pol.tick - now)
    if holder.state == "noticed" and holder.notice_until_minute is not None:
        return max(0, holder.notice_until_minute - now) + pol.tick
    return pol.tick        # approved or mid-call: applied at the next stop


def composition_classes(need_cls: int) -> set[int]:
    """Which classes ride in one composition. Urgent work (P0, P1) carries only classes at
    least as urgent, so it stays risk-reducing and is never delayed to a slot; routine work at
    a slot carries everything (Kestrel: 'one request with five things in it')."""
    return set(range(0, need_cls + 1)) if need_cls <= P1_ROLLBACK else {0, 1, 2, 3}


@dataclass(frozen=True)
class Precedence:
    action: str
    """submit | wait | supersede | bundle_supersede | finish_chain_first | bundle_stop_chain |
    defer_suspect"""
    reason: str


def precedence(holder: Proposal | None, need_cls: int, need_rank: int | None, now: int,
               pol: EffectivePolicy, t: Thresholds, *, caller_id_auto: bool = False,
               chain_k_rem: int | None = None, suspect_since: int | None = None
               ) -> Precedence:
    """The 6.2 table. `chain_k_rem` is the remaining steps of a running P0b drain chain."""
    if holder is None:
        if need_cls in ROUTINE_CLASSES and suspect_since is not None \
                and now - suspect_since < t.suspect_defer_min:
            return Precedence("defer_suspect", "a DID is suspect: routine submits wait up to "
                                               f"{t.suspect_defer_min} min for alarm or clear")
        if need_cls == P0_INCIDENT and need_rank == P0A_PROMPT_SAFETY and chain_k_rem:
            if (chain_k_rem - 1) * pol.tick <= t.drain_chain_first_max_min:
                return Precedence("finish_chain_first",
                                  f"{chain_k_rem} drain steps left: finish them, then submit "
                                  "the prompt fix on the post-drain base")
            return Precedence("bundle_stop_chain",
                              f"{chain_k_rem} drain steps left: stop the chain and bundle the "
                              "rest of the drain into the prompt-fix approval")
        return Precedence("submit", "workflow free")
    h_rank = holder.p0_rank
    if need_cls in ROUTINE_CLASSES:
        return Precedence("wait", f"{holder.proposal_id} in flight; routine work waits")
    if holder.state not in ("pending", "approved", "noticed"):
        return Precedence("wait", f"{holder.proposal_id} is mid-call ({holder.state})")
    if need_cls == P0_INCIDENT and need_rank == P0A_PROMPT_SAFETY:
        if holder.cls == P0_INCIDENT and h_rank == P0B_DID_DRAIN and holder.state == "pending":
            return Precedence("bundle_supersede", "replace the pending drain approval with one "
                                                  "bundle: prompt fix plus drain")
        if holder.cls > P0_INCIDENT:
            return Precedence("supersede", f"P0a outranks class {holder.cls}")
        return Precedence("wait", f"P0 {holder.proposal_id} in flight")
    if need_cls == P0_INCIDENT:        # P0b drain
        if holder.cls == P0_INCIDENT:
            return Precedence("wait", "a P0 approval is pending: never staled by a drain "
                                      "(alert if the wait exceeds "
                                      f"{t.alert_drain_wait_min} min)")
        if caller_id_auto and worst_remaining_minutes(holder, now, pol) \
                <= t.drain_wait_holder_max_min:
            return Precedence("wait", "holder finishes within "
                                      f"{t.drain_wait_holder_max_min} min; drain right after")
        return Precedence("supersede", f"P0b outranks class {holder.cls}")
    if need_cls == P1_ROLLBACK and holder.cls in ROUTINE_CLASSES:
        return Precedence("supersede", f"P1 outranks class {holder.cls}")
    return Precedence("wait", f"{holder.proposal_id} (class {holder.cls}) in flight")


def notice_until(now: int, pol: EffectivePolicy) -> int:
    """When a notice posted for an auto-apply wanted at `now` elapses. Kestrel-like directives
    require the notice to be posted inside approver hours: outside them it is posted at the
    next opening (4.1 'planning point')."""
    post = now
    if pol.notice_inside_approver_hours and not in_approver_hours(now, pol.approver_hours):
        post = next_open(now, pol.approver_hours)
    return post + pol.notice_minutes


def no_submit_reason(now: int, pol: EffectivePolicy, cls: int, t: Thresholds) -> str:
    """Why `choose_submit` found no minute: `no_safe_window` only when no expiry-safe minute
    exists at all (today's and tomorrow's routine slots for P2/P3, the next 24h of ticks for
    P0/P1), which pages NO_SAFE_WINDOW; otherwise the fatigue budget (at cap 1, the 48h routine
    rule of 6.4) is what makes the work wait, which is the policy working and pages nothing."""
    if cls in ROUTINE_CLASSES:
        day = now // 1440
        if routine_slots(day, pol, t=t) or routine_slots(day + 1, pol, t=t):
            return "fatigue_wait"
        return "no_safe_window"
    start = ceil_to_tick(now, pol.tick)
    if any(p_expire(m, pol) <= t.submit_expiry_p_max
           for m in range(start, start + 1440, pol.tick)):
        return "fatigue_wait"
    return "no_safe_window"


def is_operational(path: tuple[str, ...]) -> bool:
    return path[0] == "hour_weights" or (path[0] == "alloc" and path[2] == "caller_id")


def no_prompt(path: tuple[str, ...]) -> bool:
    return path != ("prompt",)


# ---------------------------------------------------------------- the train
@dataclass(frozen=True)
class ReleaseDecision:
    wf: str
    action: str
    reason: str
    proposal_id: str | None = None
    planned_minute: int | None = None
    """for the step rule: the minute this work is planned to be submitted"""
    auto_refused: tuple[str, ...] = ()
    """why the composition could not auto-apply, when it went to approval instead (audit)"""


@dataclass
class ReleaseDeps:
    overlay: Overlay
    lifecycle: ProposalLifecycle
    policy_of: Callable[[str], EffectivePolicy]
    workflows: Callable[[], Sequence[str]]
    live: Callable[[str], tuple[int, object]]
    """wf -> (version, Config) read fresh from the API"""
    build_context: Callable[[str], BuildContext]
    holds: Callable[[str], Sequence[Hold]] = lambda wf: ()
    suspect_since: Callable[[str], int | None] = lambda wf: None
    t: Thresholds = field(default_factory=Thresholds)
    gate: Callable[[Composition, int], tuple[Sequence[int], str] | None] | None = None
    """The harm gate on a composed change before it is routed (DESIGN.md 0.1): None when it
    passes, else (patch ids to hold, reason). None here: no gate (governance unit tests)."""


class ReleaseTrain:
    """Per workflow, in priority order: compose -> route (auto or approval) -> timing -> act."""

    def __init__(self, deps: ReleaseDeps):
        self.d = deps
        self._chain_refused: tuple[str, ...] = ()
        """Why the last `_chain_step` could not chain (audit, on the routing decision)."""

    def ledger(self, wf: str, now: int | None = None) -> VelocityLedger:
        """The rolling velocity ledger (6.5 rule 4), derived from the applied auto-apply
        proposals in the store, so a restart rebuilds exactly what a clean run holds (ADR
        0029). Entries older than the window (and the chaining look-back of one tick) are
        irrelevant to every rule and are not loaded."""
        now = self.d.lifecycle.d.clock() if now is None else now
        since = now - max(self.d.t.velocity_window_min, self.d.policy_of(wf).tick) - 1
        return VelocityLedger(wf, tuple(self.d.lifecycle.d.store.velocity_entries(wf, since)))

    def run(self, now: int,
            on_decision: Callable[[ReleaseDecision], None] | None = None
            ) -> list[ReleaseDecision]:
        """One decision per workflow. `on_decision` is called as soon as each workflow is done
        (the loop records it then, so a crash in a later workflow loses no earlier record)."""
        out = []
        for wf in self.d.workflows():
            try:
                d = self.run_workflow(wf, now)
            except CompositionInvalid as e:
                self.d.overlay.hold(e.patch_ids, now + self.d.t.held_patch_release_min)
                d = ReleaseDecision(wf, "hold", f"composition invalid: {e.problems}")
            out.append(d)
            if on_decision is not None:
                on_decision(d)
        return out

    def _package(self, comp: Composition) -> RequestPackage:
        """The approval package (also used for the auto-apply rationale and evidence)."""
        ctx = self.d.build_context(comp.wf)
        notes = self.d.overlay.supervisor_notes(comp.wf, [p.path for p in comp.patches])
        if notes and not ctx.supervisor_notes:
            ctx = BuildContext(**{**ctx.__dict__, "supervisor_notes": tuple(notes)})
        return build_request(comp, ctx)

    def run_workflow(self, wf: str, now: int) -> ReleaseDecision:
        ov, lc, pol = self.d.overlay, self.d.lifecycle, self.d.policy_of(wf)
        open_patches = ov.open_patches(wf)
        if not open_patches:
            return ReleaseDecision(wf, "idle", "no open patches")
        need_cls = min(p.cls for p in open_patches)
        ranks = [p.p0_rank for p in open_patches if p.cls == P0_INCIDENT and p.p0_rank is not None]
        need_rank = min(ranks) if ranks else None
        holder = lc.d.store.holder(wf)
        caller_id_auto = "caller_id" in pol.allowed_dims and pol.max_shift > 0
        k_rem = None
        if need_rank == P0A_PROMPT_SAFETY and P0B_DID_DRAIN in ranks and caller_id_auto \
                and holder is None:
            k_rem = chain_steps_left(self._compose(wf, {P0_INCIDENT}, no_prompt), pol, self.d.t)
        prec = precedence(holder, need_cls, need_rank, now, pol, self.d.t,
                          caller_id_auto=caller_id_auto, chain_k_rem=k_rem,
                          suspect_since=self.d.suspect_since(wf))
        if prec.action in ("wait", "defer_suspect"):
            return ReleaseDecision(wf, prec.action, prec.reason,
                                   holder.proposal_id if holder else None)
        if prec.action == "finish_chain_first":
            return self._finish_chain_then_fix(wf, now, pol, prec)
        if prec.action == "bundle_supersede":
            ov.return_to_open(holder.proposal_id)      # the bundle must keep the drain
        comp = self._compose(wf, composition_classes(need_cls))
        if not comp.patches:
            skipped = self.d.overlay.last_compose[wf].skipped
            return ReleaseDecision(wf, "skipped", "; ".join(f"patch {i}: {why}"
                                                            for i, why in skipped))
        comp, blocked = self._gated(comp, now, composition_classes(need_cls))
        if comp is None:
            return ReleaseDecision(wf, "harm_gate", blocked)
        if not comp.changed_dims:
            p = lc.submit(comp, now, check=False)
            return ReleaseDecision(wf, "discard", "no net change", p.proposal_id)
        if prec.action in ("supersede", "bundle_supersede"):
            return self._supersede(holder, comp, now, pol, prec)
        return self._route(comp, now, pol)

    def _gated(self, comp: Composition, now: int, classes: set[int]
               ) -> tuple[Composition | None, str]:
        """The harm gate on the composed change before routing (0.1). The patches of a failing
        lever are held (re-checked by the auto-release, 6.1) and the rest is recomposed, so a
        failing allocation never delays a caller-ID or schedule change riding with it."""
        reasons: list[str] = []
        for _ in range(3):
            if self.d.gate is None or not comp.changed_dims:
                return comp, ""
            verdict = self.d.gate(comp, now)
            if verdict is None:
                return comp, ""
            held, why = verdict
            reasons.append(why)
            self.d.overlay.hold(held, now + self.d.t.held_patch_release_min)
            comp = self._compose(comp.wf, classes)
            if not comp.patches:
                return None, "; ".join(reasons)
        return None, "; ".join(reasons)

    def _compose(self, wf: str, classes: set[int], paths=None) -> Composition:
        version, live = self.d.live(wf)
        return self.d.overlay.compose(wf, live, classes, base_version=version, paths=paths)

    def _finish_chain_then_fix(self, wf: str, now: int, pol: EffectivePolicy,
                               prec: Precedence) -> ReleaseDecision:
        """6.2: a short drain chain finishes first (one step per tick); the prompt fix is
        submitted in the step of the last chain step, right after it, on the post-drain base."""
        drain = self._compose(wf, {P0_INCIDENT}, no_prompt)
        d = self._route(drain, now, pol)
        if chain_steps_left(self._compose(wf, {P0_INCIDENT}, no_prompt), pol, self.d.t) > 0:
            return ReleaseDecision(wf, "drain_step", prec.reason + f"; {d.action}",
                                   d.proposal_id)
        fix = self._compose(wf, {P0_INCIDENT})
        return self._route(fix, now, pol)

    def _chain_step(self, comp: Composition, now: int, pol: EffectivePolicy
                    ) -> ReleaseDecision | None:
        """A risk-reducing P0/P1 change larger than max_shift is applied as a chain: one
        straight-line step of at most max_shift per tick (6.5 'Chaining, bounded'). Steps carry
        no patches, so the patches stay open until the step that reaches the target."""
        self._chain_refused = ()
        if comp.cls > P1_ROLLBACK or not is_risk_reducing(comp.risk):
            self._chain_refused = (f"no chain: class {comp.cls}, risk {dict(comp.risk)}",)
            return None
        lam = chain_lambda(comp, pol, self.d.t)
        if not 0.0 < lam < 1.0:
            self._chain_refused = (f"no chain: lambda {lam}",)
            return None
        cfg = interpolate(comp.live, comp.proposed.as_stored(), lam)
        stored = cfg.as_stored()
        tvs = tv_by_cohort_dim(comp.live, stored)
        step = Composition(wf=comp.wf, base_version=comp.base_version, live=comp.live,
                           proposed=cfg, patches=(),
                           changed_dims=tuple(comp.live.changed_dims(stored)),
                           tv_by_cohort_dim=tvs,
                           risk=FrozenDict({k: v for k, v in comp.risk.items() if k in tvs}),
                           cls=comp.cls, p0_rank=comp.p0_rank)
        r = self._auto_report(step, now, pol, None)
        if not r.permitted:
            self._chain_refused = tuple(f"chain step: {x}" for x in r.reasons)
            return None
        pkg = self._package(comp)
        p = self.d.lifecycle.auto_apply(step, now, rationale=f"chain step ({lam:.3f} of the "
                                        f"way): {pkg.rationale}", package=pkg)
        return ReleaseDecision(comp.wf, "chain_step",
                               f"{chain_steps_left(comp, pol, self.d.t)} steps to target",
                               p.proposal_id)

    def _auto_report(self, comp: Composition, now: int, pol: EffectivePolicy,
                     notice: int | None):
        return check_auto_apply(pol, comp, self.ledger(comp.wf, now), self.d.holds(comp.wf), now,
                                notice_until=notice, t=self.d.t)

    def _route(self, comp: Composition, now: int, pol: EffectivePolicy) -> ReleaseDecision:
        lc = self.d.lifecycle
        wf = comp.wf
        if wf not in lc.auto_blocked:
            notice = notice_until(now, pol) if needs_notice(pol, comp) else None
            r = self._auto_report(comp, now, pol, None)
            notice_only = r.reasons and all(x.startswith("notice") for x in r.reasons)
            if r.permitted or notice_only:
                pkg = self._package(comp)
                p = lc.auto_apply(comp, now, rationale=pkg.rationale, notice_until=notice,
                                  package=pkg)
                return ReleaseDecision(wf, "auto_apply" if r.permitted else "notice",
                                       "policy, directive and velocity permit", p.proposal_id,
                                       p.notice_until_minute)
            d = self._chain_step(comp, now, pol)
            if d is not None:
                return d
            # operational part first (6.2 batching): auto-apply it, then submit the rest
            if any(is_operational(p.path) for p in comp.patches) and not all(
                    is_operational(p.path) for p in comp.patches):
                d = self._operational_first(comp, now, pol)
                if d is not None:
                    return d
            return dataclasses.replace(self._submit_approval(comp, now, pol),
                                       auto_refused=tuple(r.reasons)
                                       + self._chain_refused)
        return self._submit_approval(comp, now, pol)

    def _operational_first(self, comp: Composition, now: int, pol: EffectivePolicy
                           ) -> ReleaseDecision | None:
        ov = self.d.overlay
        version, live = self.d.live(comp.wf)
        op = ov.compose(comp.wf, live, composition_classes(comp.cls), base_version=version,
                        paths=is_operational)
        if not op.changed_dims or needs_notice(pol, op):
            return None
        r = self._auto_report(op, now, pol, None)
        if not r.permitted:
            return None
        if comp.cls in ROUTINE_CLASSES and choose_submit(
                now, pol, self.d.lifecycle.used(comp.wf), comp.cls,
                self._slot1_terminal(comp.wf, now), t=self.d.t) != now:
            return None        # apply the operational part right before the approval only
        pkg = self._package(op)
        p = self.d.lifecycle.auto_apply(op, now, rationale=pkg.rationale, package=pkg)
        version, live = self.d.live(comp.wf)
        rest = ov.compose(comp.wf, live, composition_classes(comp.cls), base_version=version)
        if not rest.changed_dims:
            return ReleaseDecision(comp.wf, "auto_apply", "operational part covered it all",
                                   p.proposal_id)
        return self._submit_approval(rest, now, pol)

    def _slot1_terminal(self, wf: str, now: int) -> bool:
        """Slot 2 only if slot 1's proposal is terminal: with one change in flight that is
        exactly 'nothing holds the workflow now'."""
        return self.d.lifecycle.d.store.holder(wf) is None

    def _submit_approval(self, comp: Composition, now: int, pol: EffectivePolicy
                         ) -> ReleaseDecision:
        lc = self.d.lifecycle
        when = choose_submit(now, pol, lc.used(comp.wf), comp.cls,
                             self._slot1_terminal(comp.wf, now), t=self.d.t)
        if when is None:
            why = no_submit_reason(now, pol, comp.cls, self.d.t)
            if why == "fatigue_wait":
                return ReleaseDecision(comp.wf, why, "the request budget of the next 24h is used "
                                       "(at cap 1: one routine request per 48h); waiting")
            return ReleaseDecision(comp.wf, why,
                                   "no expiry-safe submit minute (NO_SAFE_WINDOW)")
        if when != now:
            return ReleaseDecision(comp.wf, "planned", f"submit planned at {when}",
                                   planned_minute=when)
        try:
            p = lc.submit(comp, now, package=self._package(comp))
        except SubmitBlocked as e:
            return ReleaseDecision(comp.wf, "blocked", "; ".join(e.reasons))
        return ReleaseDecision(comp.wf, "submitted", p.state, p.proposal_id)

    def _supersede(self, holder: Proposal, comp: Composition, now: int, pol: EffectivePolicy,
                   prec) -> ReleaseDecision:
        lc = self.d.lifecycle
        bundle = prec.action == "bundle_supersede"
        r = self._auto_report(comp, now, pol, None)
        route = "auto" if (r.permitted and comp.wf not in lc.auto_blocked
                           and not [x for x in r.reasons if not x.startswith("one_in_flight")]
                           ) else "approval"
        if route == "approval":
            when = choose_submit(now, pol, lc.used(comp.wf), comp.cls, True, t=self.d.t)
            if when != now:
                return ReleaseDecision(comp.wf, "planned" if when else "no_safe_window",
                                       f"supersede of {holder.proposal_id} planned at {when}",
                                       planned_minute=when)
        pkg = self._package(comp)
        try:
            p = lc.supersede(holder, comp, now=now, package=pkg, route=route,
                             bundle_keeps_drain=bundle, rationale=pkg.rationale)
        except SubmitBlocked as e:
            return ReleaseDecision(comp.wf, "blocked", "; ".join(e.reasons))
        return ReleaseDecision(comp.wf, prec.action, prec.reason, p.proposal_id)


def p0_order(patch_ranks: Mapping[int, int | None]) -> list[int]:
    """Patch ids in the fixed P0 order: P0a prompt safety before P0b DID drain."""
    return sorted(patch_ranks, key=lambda pid: (patch_ranks[pid] is None,
                                                patch_ranks[pid] or 0, pid))
