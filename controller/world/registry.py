"""WorkflowRegistry: workflow specs and EffectivePolicy (API policy AND directives, the stricter).

Owner: track T2 (milestone M3). Implements DESIGN.md 6.4 (C_eff, reserve, routine budget), 6.5
(directives can only tighten), 6.3 (preferred slot) and 9.7 (every parameter read from the API, and
what changed when it changes).

How each field of `EffectivePolicy` is formed (API = `GET /workflows/{id}`, D = resolved directive):

| field | rule |
|---|---|
| approver_hours, latency, ttl, apply window, mid_flight, min_validation_pass_rate | API |
| tick | `GET /sim/state` tick_minutes |
| c_policy | API max_requests_per_24h |
| c_eff | min(c_policy, D.max_requests_per_day) |
| reserve, routine_budget | 1 if c_eff >= 2 else 0; c_eff - reserve (F2) |
| auto_enabled, max_shift | API (a directive cannot enable or widen) |
| allowed_dims | API allowed_dims, minus customer-facing dims when D says `none` |
| customer_facing_auto | D (only matters for dims the API allows) |
| cohort_harm_line | min(settings default, D) (smaller is stricter) |
| not_before_min, notice_*, avoid_prompt_terms, weekly_digest | D |
| preferred_slot_min | max(open, D.not_before) with a not_before; else open + min(60, 25% of the
  approver window) floored to the tick (the `*` default) |

Every field lives on `types.EffectivePolicy` (the T2 request was merged in step I1, ADR 0028);
`approver_close_min` and `ttl_min` are properties derived from the API fields.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from controller.settings import Thresholds
from controller.types import (CUSTOMER_FACING_DIMS, EffectivePolicy, ResolvedDirective,
                              WorkflowSpec)
from controller.world.clock import apply_window_minutes, floor_to_tick
from controller.world.directives import DirectiveLayer


def preferred_slot_min(approver_hours: tuple[int, int], not_before_min: int | None, tick: int,
                       offset_max_min: int, window_frac: float) -> int:
    """Minute of day of routine slot 1 (DESIGN.md 6.3 'Preferred slot')."""
    open_min, close_min = approver_hours[0] * 60, approver_hours[1] * 60
    if not_before_min is not None:
        return max(open_min, not_before_min)
    offset = min(offset_max_min, int(window_frac * (close_min - open_min)))
    return open_min + floor_to_tick(offset, tick)


def effective_policy(spec: WorkflowSpec, directive: ResolvedDirective, tick: int,
                     t: Thresholds) -> EffectivePolicy:
    """Combine one workflow's API policy with its directive, taking the stricter of each."""
    gov = spec.governance
    c_policy = gov.max_requests_per_24h
    c_eff = c_policy if directive.max_requests_per_day is None else min(
        c_policy, directive.max_requests_per_day)
    reserve = 1 if c_eff >= 2 else 0
    allowed = frozenset(gov.auto_apply.allowed_dims)
    if directive.customer_facing_auto_apply == "none":
        allowed = allowed - CUSTOMER_FACING_DIMS
    return EffectivePolicy(
        tick=tick,
        approver_hours=gov.approver_hours,
        approver_open_min=gov.approver_hours[0] * 60,
        latency_minutes=gov.approver_latency_minutes,
        ttl_hours=gov.proposal_ttl_hours,
        apply_window_min=apply_window_minutes(gov.apply_window_hours),
        c_policy=c_policy, c_eff=c_eff, reserve=reserve, routine_budget=c_eff - reserve,
        auto_enabled=gov.auto_apply.enabled,
        allowed_dims=allowed if gov.auto_apply.enabled else frozenset(),
        max_shift=gov.auto_apply.max_shift,
        customer_facing_auto=directive.customer_facing_auto_apply,
        not_before_min=directive.not_before_min,
        preferred_slot_min=preferred_slot_min(
            gov.approver_hours, directive.not_before_min, tick,
            directive.preferred_slot_offset_max_min, directive.preferred_slot_offset_window_frac),
        notice_minutes=directive.notice_minutes,
        cohort_harm_line=min(t.cohort_harm_line_default, directive.cohort_harm_line),
        mid_flight=spec.mid_flight,
        min_validation_pass_rate=gov.min_validation_pass_rate,
        avoid_prompt_terms=directive.avoid_prompt_terms,
        workflow_id=spec.workflow_id, tenant_id=spec.tenant_id,
        base_reject_rate=gov.base_reject_rate,
        notice_scope=directive.notice_scope,
        notice_inside_approver_hours=directive.notice_inside_approver_hours,
        weekly_digest=directive.weekly_digest,
        dialling_window=spec.dialling_window,
        pre_open_submits=directive.pre_open_submits,
        api_auto_enabled=gov.auto_apply.enabled,
        api_allowed_dims=frozenset(gov.auto_apply.allowed_dims),
        use_case=spec.use_case,
        directive_scopes=directive.scopes,
    )


@dataclass(frozen=True)
class PolicyChange:
    """One field of a workflow's effective policy that changed between two reads (9.7)."""

    workflow_id: str
    field: str
    old: Any
    new: Any


def policy_diff(old: EffectivePolicy, new: EffectivePolicy) -> list[PolicyChange]:
    out = []
    for f in dataclasses.fields(new):
        a, b = getattr(old, f.name, None), getattr(new, f.name)
        if a != b:
            out.append(PolicyChange(new.workflow_id, f.name, a, b))
    return out


@dataclass(frozen=True)
class AutoApplyConflict:
    """'Policy permits, client agreement forbids' (F15): dims the API allows but we will not
    auto-apply because of a directive. Shown in the supervisor view."""

    workflow_id: str
    dims: frozenset[str]
    directive_source: str


class WorkflowRegistry:
    """Specs and effective policies of every workflow, rebuilt from the API on every refresh."""

    def __init__(self, directives: DirectiveLayer, thresholds: Thresholds | None = None):
        self.directives = directives
        self.t = thresholds or Thresholds()
        self._specs: dict[str, WorkflowSpec] = {}
        self._policies: dict[str, EffectivePolicy] = {}
        self._resolved: dict[str, ResolvedDirective] = {}
        self._tick: int | None = None

    def load(self, specs: Iterable[WorkflowSpec], tick: int) -> list[PolicyChange]:
        """(Re)load every spec. Returns the policy fields that changed since the last load, so
        the caller can audit a constraint change (9.7). The first load returns []."""
        changes: list[PolicyChange] = []
        self._tick = tick
        for spec in specs:
            d = self.directives.resolve(spec.tenant_id, spec.workflow_id, spec.use_case)
            pol = effective_policy(spec, d, tick, self.t)
            old = self._policies.get(spec.workflow_id)
            if old is not None:
                changes.extend(policy_diff(old, pol))
            self._specs[spec.workflow_id] = spec
            self._policies[spec.workflow_id] = pol
            self._resolved[spec.workflow_id] = d
        return changes

    @property
    def tick(self) -> int:
        if self._tick is None:
            raise RuntimeError("registry not loaded")
        return self._tick

    def workflow_ids(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, wf: str) -> WorkflowSpec:
        return self._specs[wf]

    def policy(self, wf: str) -> EffectivePolicy:
        return self._policies[wf]

    def directive(self, wf: str) -> ResolvedDirective:
        return self._resolved[wf]

    def policies(self) -> Mapping[str, EffectivePolicy]:
        return dict(self._policies)

    def auto_apply_conflict(self, wf: str) -> AutoApplyConflict | None:
        """The F15 flag: API-permitted dims the directive removes (e.g. nort_cancel_2)."""
        pol = self._policies[wf]
        if not pol.api_auto_enabled:
            return None
        forbidden = pol.api_allowed_dims - pol.allowed_dims
        if not forbidden:
            return None
        src = self._resolved[wf].sources.get("customer_facing_auto_apply", "")
        return AutoApplyConflict(wf, frozenset(forbidden), src)
