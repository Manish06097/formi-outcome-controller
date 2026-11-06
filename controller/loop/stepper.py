"""The advance step-size rule `next_stop(now, ctx)` (pure).

Owner: track T1 (milestone M2). Implements DESIGN.md 4.2.

The rule, in order:
1. Base step `ctx.base_step` (60 in hours, a multiple of tick) [T].
2. While a workflow has a pending approval whose decision could become visible before the next
   stop, the step is at most `S_watch_max - tick` (90 at W = 120, tick 15), so an approval decided
   at any minute is seen and applied before `apply_by` (F12). A pending P0 past its minimum
   latency, an open incident, a suspect prompt, a running drain chain, or a suspect DID while
   its workflow dials cut it to one tick.
3. The step never crosses a boundary: dialling open and close, approver open, approver close +
   tick (the last minute a decision can become visible), the daily batch, planned submit slots,
   notice expiries, `apply_by - tick` of anything approved, and the scenario end.
4. Overnight jump: when nothing dials, no pending approval can be decided before the next
   boundary and nothing is approved-but-unapplied, the step goes straight to that boundary.

Everything comes from `PlannerContext` (built from the API and the store), so the rule is a pure
function with unit tests over hand-built contexts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from controller.settings import Thresholds
from controller.types import (P0_INCIDENT, EffectivePolicy, PlannerContext, WorkflowPlanState,
                              WorkflowSpec)
from controller.world.clock import (floor_to, may_decide_between, next_daily, s_watch_max)


@dataclass(frozen=True)
class StopPlan:
    """`next_stop` with the reason that bound it (written into the advance decision row)."""

    to_minute: int
    step: int
    bound: str
    overnight_jump: bool
    watch_bound: int | None


def next_stop(now: int, ctx: PlannerContext) -> int:
    """The minute the next advance should stop at (DESIGN.md 4.2)."""
    return plan_stop(now, ctx).to_minute


def plan_stop(now: int, ctx: PlannerContext) -> StopPlan:
    tick = ctx.tick
    step, bound = ctx.base_step, "base_step"
    watch: int | None = None
    for wf in ctx.workflows:
        g = wf.policy
        if wf.has_pending and _may_decide(now, now + step, tick, wf):
            cap = s_watch_max(tick, g.apply_window_min) - tick
            watch = cap if watch is None else min(watch, cap)
            if cap < step:
                step, bound = cap, f"apply_window:{wf.workflow_id}"
            if (wf.pending_class == P0_INCIDENT and wf.pending_created is not None
                    and now >= wf.pending_created + g.latency_minutes[0]):
                step, bound = tick, f"incident_pending:{wf.workflow_id}"
        # a suspect DID only gets new evidence while its workflow dials (ADR 0030): at night
        # the same window would be re-read every tick
        suspect_did = wf.suspect_did and dials_between(now, now + tick, [wf.dialling_window])
        if wf.open_incident or suspect_did or wf.suspect_prompt or wf.drain_chain_pending:
            step, bound = tick, f"incident_or_suspect:{wf.workflow_id}"
    b_minute, b_name = min(boundaries(now, ctx))
    if b_minute - now < step:
        step, bound = b_minute - now, b_name
    jump = False
    if overnight_allowed(now, b_minute, ctx):
        jump = b_minute - now > step
        step, bound = b_minute - now, b_name
    step = max(tick, floor_to(step, tick))
    to = min(now + step, ctx.end_minute)
    return StopPlan(to_minute=to, step=to - now, bound=bound, overnight_jump=jump,
                    watch_bound=watch)


def _may_decide(now: int, until: int, tick: int, wf: WorkflowPlanState) -> bool:
    """A pending decision not yet visible was taken after now - tick, so it may be decided in
    [now - tick + 1, until)."""
    return may_decide_between(now - tick + 1, until, wf.policy.approver_hours)


def boundaries(now: int, ctx: PlannerContext) -> list[tuple[int, str]]:
    """Every minute strictly after `now` that a stop must land on, with its name."""
    tick = ctx.tick
    out: list[tuple[int, str]] = [(ctx.end_minute, "scenario_end")]
    for wf in ctx.workflows:
        lo, hi = wf.dialling_window
        a_lo, a_hi = wf.policy.approver_hours
        name = wf.workflow_id
        out += [(next_daily(now, lo * 60), f"dialling_open:{name}"),
                (next_daily(now, hi * 60), f"dialling_close:{name}"),
                (next_daily(now, a_lo * 60), f"approver_open:{name}"),
                (next_daily(now, a_hi * 60 + tick), f"approver_close_plus_tick:{name}"),
                (next_daily(now, wf.daily_batch_at_min), f"daily_batch:{name}")]
        out += [(t, f"planned_slot:{name}") for t in wf.planned_slots if t > now]
        out += [(t, f"notice_expiry:{name}") for t in wf.notice_expiries if t > now]
        out += [(max(t - tick, now + tick), f"apply_by_minus_tick:{name}")
                for t in wf.approved_apply_by]
    return [(t, n) for t, n in out if t > now]


def dials_between(start: int, end: int, windows: Iterable[tuple[int, int]]) -> bool:
    """True if any dialling window [lo, hi) hours intersects [start, end)."""
    for lo, hi in windows:
        if may_decide_between(start, end, (lo, hi)):
            return True
    return False


def overnight_allowed(now: int, boundary: int, ctx: PlannerContext) -> bool:
    """The jump to the next boundary is allowed only when nothing dials before it, no pending
    approval can be decided before it, and nothing is approved but unapplied (DESIGN.md 4.2:
    the assertion checked at jump time)."""
    if dials_between(now, boundary, [wf.dialling_window for wf in ctx.workflows]):
        return False
    for wf in ctx.workflows:
        if wf.approved_apply_by:
            return False
        if wf.has_pending and _may_decide(now, boundary, ctx.tick, wf):
            return False
        if wf.open_incident or wf.suspect_prompt or wf.drain_chain_pending:
            return False
    return True


def basic_policy(spec: WorkflowSpec, tick: int, t: Thresholds | None = None) -> EffectivePolicy:
    """The API policy alone, with no directive layer, in the `EffectivePolicy` shape.

    Used by the loop until `world/registry.py` (T2) provides the real EffectivePolicy (API AND
    directives). The step rule reads only tick, approver hours, latency and the apply window,
    which a directive never changes, so `next_stop` is exact with this policy."""
    t = t or Thresholds()
    g = spec.governance
    c = g.max_requests_per_24h
    reserve = 1 if c >= 2 else 0
    a_lo, a_hi = g.approver_hours
    offset = min(t.slot_offset_max_min, int(t.slot_offset_window_frac * (a_hi - a_lo) * 60))
    slot = a_lo * 60 + floor_to(offset, tick)
    return EffectivePolicy(
        tick=tick, approver_hours=g.approver_hours, approver_open_min=a_lo * 60,
        latency_minutes=g.approver_latency_minutes, ttl_hours=g.proposal_ttl_hours,
        apply_window_min=int(g.apply_window_hours * 60), c_policy=c, c_eff=c, reserve=reserve,
        routine_budget=c - reserve, auto_enabled=g.auto_apply.enabled,
        allowed_dims=g.auto_apply.allowed_dims, max_shift=g.auto_apply.max_shift,
        customer_facing_auto="none", not_before_min=None, preferred_slot_min=slot,
        notice_minutes=0, cohort_harm_line=t.cohort_harm_line_default, mid_flight=spec.mid_flight,
        min_validation_pass_rate=g.min_validation_pass_rate, avoid_prompt_terms=(),
        workflow_id=spec.workflow_id, tenant_id=spec.tenant_id,
        base_reject_rate=g.base_reject_rate, dialling_window=spec.dialling_window)


def plan_state(spec: WorkflowSpec, policy: EffectivePolicy, **facts: object) -> WorkflowPlanState:
    """A `WorkflowPlanState` for one workflow from its spec, its policy and store facts."""
    return WorkflowPlanState(workflow_id=spec.workflow_id, policy=policy,
                             dialling_window=spec.dialling_window,
                             daily_batch_at_min=spec.daily_batch.at_hour * 60, **facts)  # type: ignore[arg-type]
