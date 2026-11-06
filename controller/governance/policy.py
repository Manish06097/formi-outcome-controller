"""Local policy replica: normalise, changed_dims, max_shift, classify_risk, auto_apply_permitted.

Owner: track T2 (milestone M3). Implements DESIGN.md 6.5 (auto-apply policy, directives, risk
labels, chaining). ADRs 0014 (local policy replica) and 0025 (risk-reducing changes).

Two layers, kept apart on purpose:

1. `sim_verdict` is an exact replica of the simulator's auto-apply check (world.py `auto_apply`:
   enabled, every changed dim allowed, max TV shift <= max_shift + 1e-9, at least one change, a
   rationale). `tests/e2e/test_policy_differential.py` checks it against the live simulator.
2. `auto_apply_permitted` is OUR rule. It calls the replica with a stricter margin
   (`max_shift - 1e-6`) and adds what the simulator does not know: directives, risk labels,
   velocity, chaining, one change in flight, holds and the notice period. It can only refuse
   more than the simulator, never less, so a 403 means the replica is wrong (page, never retry).

The proposed config is always compared in its STORED form (`proposed.as_stored()`): the simulator
re-normalises every body, and about 0.5% of weight vectors drift in their last bits (ADR 0027).

Float detail: the simulator sums TV terms in set-iteration order, which differs between
processes; we sum in sorted order. The two can differ by a few ulps, far inside the 1e-6 margin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from controller.settings import Thresholds
from controller.types import (AGENT_DIMS, CUSTOMER_FACING_DIMS, DEFAULT_COHORT, P0_INCIDENT,
                              P1_ROLLBACK, AutoApplyPolicy, Composition, Config, DidState,
                              EffectivePolicy, FrozenDict, Hold, Proposal, VelocityLedger)

RiskLabel = Literal["reducing", "increasing"]
ChangeKind = Literal["reducing", "ramp", "new_arm", "other"]

SIM_SHIFT_TOLERANCE = 1e-9
"""The simulator's own tolerance (`sh > max_shift + 1e-9` refuses)."""
FLOAT_EQ = 1e-12
"""Two shares closer than this are the same share for risk labelling (normalisation noise)."""
BAD_DID_STATES = frozenset({"suspect", "degraded", "unhealthy"})
COHORTS_KEY = ("*", "cohorts")
PROMPT_KEY = (DEFAULT_COHORT, "prompt_version")
LPH_KEY = ("lead_preferred_hour", "schedule")
APPROVAL_ONLY_DIMS = frozenset({"cohorts", "prompt_version"})
"""Never auto-applied by design, whatever the API allows (ADR 0025)."""


# ---------------------------------------------------------------- the simulator's diff
def normalise(body: Mapping[str, Any]) -> Config:
    """A body as the simulator will store it (weights normalised, zero weights dropped)."""
    return Config.from_body(body)


def changed_dims(old: Config, new: Config) -> list[str]:
    """Exactly `sim/world.py:changed_dims` (delegates to `Config.changed_dims`)."""
    return old.changed_dims(new)


def tv(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Unweighted total-variation distance of two option distributions."""
    return 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in sorted(set(a) | set(b)))


def shift_keys(old: Config, new: Config) -> list[str]:
    """The cohorts the simulator's max_shift ranges over: default plus every old and new id."""
    return sorted({DEFAULT_COHORT} | set(old.cohort_ids()) | set(new.cohort_ids()))


def tv_by_cohort_dim(old: Config, new: Config) -> FrozenDict:
    """(cohort, agent dim) -> TV of the effective distribution, for every pair that moved."""
    out = {}
    for k in shift_keys(old, new):
        for dim in AGENT_DIMS:
            d = tv(old.dist(k, dim), new.dist(k, dim))
            if d > 0.0:
                out[(k, dim)] = d
    return FrozenDict(out)


def max_shift(old: Config, new: Config) -> float:
    """The simulator's max_shift: the largest TV of any agent dim over default and every cohort
    id of either config, unweighted (a tiny cohort counts as much as the population)."""
    return max(tv_by_cohort_dim(old, new).values(), default=0.0)


@dataclass(frozen=True)
class SimVerdict:
    """The simulator's auto-apply answer, structured. `permitted` iff no problem."""

    enabled: bool
    changed_dims: tuple[str, ...]
    bad_dims: tuple[str, ...]
    shift: float
    shift_exceeds: bool
    no_change: bool
    rationale_missing: bool

    @property
    def permitted(self) -> bool:
        return (self.enabled and not self.bad_dims and not self.shift_exceeds
                and not self.no_change and not self.rationale_missing)

    def problems(self, max_shift_value: float) -> list[str]:
        """The simulator's problem strings, in its order."""
        p = []
        if not self.enabled:
            p.append("auto-apply is not enabled for this workflow")
        if self.bad_dims:
            p.append(f"dims not permitted for auto-apply: {list(self.bad_dims)}")
        if self.shift_exceeds:
            p.append(f"allocation shift {self.shift:.3f} exceeds max_shift {max_shift_value}")
        if self.no_change:
            p.append("no change")
        if self.rationale_missing:
            p.append("rationale is required")
        return p


def sim_verdict(aa: AutoApplyPolicy, live: Config, proposed_stored: Config,
                rationale: str = "x", margin: float = -SIM_SHIFT_TOLERANCE) -> SimVerdict:
    """Replica of `World.auto_apply`'s checks. `proposed_stored` must be the stored form.

    `margin` is subtracted from max_shift: the simulator's own rule is `margin = -1e-9`
    (allow 1e-9 over); ours is `+1e-6` (stay 1e-6 under)."""
    dims = tuple(live.changed_dims(proposed_stored))
    sh = max_shift(live, proposed_stored)
    return SimVerdict(
        enabled=aa.enabled, changed_dims=dims,
        bad_dims=tuple(d for d in dims if d not in aa.allowed_dims),
        # sh == 0 never exceeds: with max_shift 0 a schedule-only change still auto-applies (9.7)
        shift=sh, shift_exceeds=sh > 0.0 and sh > aa.max_shift - margin, no_change=not dims,
        rationale_missing=not rationale)


# ---------------------------------------------------------------- risk labels (ADR 0025)
def _status(s: Any) -> str:
    return s.status if isinstance(s, DidState) else str(s)


def _moves(a: Mapping[str, float], b: Mapping[str, float]) -> tuple[set[str], set[str], set[str]]:
    """(options whose share rises, options whose share falls, options that enter)."""
    opts = set(a) | set(b)
    rises = {o for o in opts if b.get(o, 0.0) > a.get(o, 0.0) + FLOAT_EQ}
    falls = {o for o in opts if b.get(o, 0.0) < a.get(o, 0.0) - FLOAT_EQ}
    enters = {o for o in b if a.get(o, 0.0) <= FLOAT_EQ < b[o]}
    return rises, falls, enters


def control_of(controls: Mapping[tuple[str, str], str], cohort: str, dim: str) -> str | None:
    """The cohort's control for dim: its own entry, else the default cohort's."""
    return controls.get((cohort, dim)) or controls.get((DEFAULT_COHORT, dim))


def _agent_label(dim: str, cohort: str, a: Mapping[str, float], b: Mapping[str, float],
                 did_state: Mapping[str, Any], controls: Mapping[tuple[str, str], str]
                 ) -> RiskLabel:
    rises, falls, enters = _moves(a, b)
    if not rises and not falls:
        return "reducing"   # normalisation noise only
    if dim == "caller_id":
        status = {d: _status(s) for d, s in did_state.items()}
        ok_falls = all(status.get(o, "unknown") in BAD_DID_STATES for o in falls)
        ok_rises = all(status.get(o, "unknown") == "healthy" for o in rises)
        return "reducing" if ok_falls and ok_rises else "increasing"
    control = control_of(controls, cohort, dim)
    if control is None or enters or not rises <= {control}:
        return "increasing"
    return "reducing"


def classify_risk(cur: Config, new: Config, did_state: Mapping[str, Any],
                  controls: Mapping[tuple[str, str], str], history: Sequence[Config]
                  ) -> FrozenDict:
    """Risk label per changed (cohort, dim), computed from the composed config (DESIGN.md 6.5).

    welcome, voice: reducing iff every option whose share rises is that cohort's control and no
      option enters.
    caller_id: reducing iff share leaves only suspect/degraded/unhealthy DIDs and every DID whose
      share rises is healthy.
    schedule: reducing iff the new table equals a table live in the lookback (`history` holds the
      configs live in the last 7 days; the caller selects them).
    cohorts, prompt_version: always increasing (approval only).
    Keys: (cohort, dim) for agent dims and hour tables, ("*", "cohorts"),
    ("default", "prompt_version"), ("lead_preferred_hour", "schedule")."""
    out: dict[tuple[str, str], RiskLabel] = {}
    if cur.cohorts != new.cohorts:
        out[COHORTS_KEY] = "increasing"
    if cur.prompt_version != new.prompt_version:
        out[PROMPT_KEY] = "increasing"
    for k in shift_keys(cur, new):
        for dim in AGENT_DIMS:
            a, b = cur.dist(k, dim), new.dist(k, dim)
            if a != b:
                out[(k, dim)] = _agent_label(dim, k, a, b, did_state, controls)
    for k in sorted(set(cur.hour_weights) | set(new.hour_weights)):
        ta, tb = cur.hour_weights.get(k), new.hour_weights.get(k)
        if ta != tb:
            revert = any(h.hour_weights.get(k) == tb for h in history)
            out[(k, "schedule")] = "reducing" if revert else "increasing"
    if cur.lead_preferred_hour != new.lead_preferred_hour:
        revert = any(h.lead_preferred_hour == new.lead_preferred_hour for h in history)
        out[LPH_KEY] = "reducing" if revert else "increasing"
    return FrozenDict(out)


def change_kinds(cur: Config, new: Config, risk: Mapping[tuple[str, str], str]
                 ) -> FrozenDict:
    """Per changed (cohort, welcome|voice): reducing, ramp (shares of existing options move, no
    option enters) or new_arm. Used by the `ramps_and_risk_reducing` directive."""
    out = {}
    for (k, dim), label in risk.items():
        if dim not in ("welcome_message", "voice"):
            continue
        if label == "reducing":
            out[(k, dim)] = "reducing"
            continue
        _, _, enters = _moves(cur.dist(k, dim), new.dist(k, dim))
        out[(k, dim)] = "new_arm" if enters else "ramp"
    return FrozenDict(out)


def is_risk_reducing(risk: Mapping[tuple[str, str], str]) -> bool:
    """A composition is risk-reducing only if it changes something and every label is reducing."""
    return bool(risk) and all(v == "reducing" for v in risk.values())


def dims_of_risk_key(key: tuple[str, str]) -> str:
    return key[1]


# ---------------------------------------------------------------- notice (6.5 rule 6)
def needs_notice(pol: EffectivePolicy, comp: Composition) -> bool:
    """Whether an auto-apply of this composition must first be noticed for `notice_minutes`.

    Incident fixes (P0) never wait. `non_incident` scope (Kestrel-like) covers every other
    auto-apply; `customer_facing` scope (the `*` default) covers those changing welcome/voice."""
    if pol.notice_minutes <= 0 or comp.cls == P0_INCIDENT:
        return False
    if pol.notice_scope == "non_incident":
        return True
    return any(d in CUSTOMER_FACING_DIMS for d in comp.changed_dims)


# ---------------------------------------------------------------- the auto-apply rule
@dataclass(frozen=True)
class PermitReport:
    permitted: bool
    reasons: tuple[str, ...]
    sim: SimVerdict
    risk: FrozenDict = field(default_factory=FrozenDict)


def _directive_reasons(pol: EffectivePolicy, risk: Mapping[tuple[str, str], str],
                       kinds: Mapping[tuple[str, str], str]) -> list[str]:
    cf = [k for k in risk if k[1] in ("welcome_message", "voice")]
    if not cf:
        return []
    mode = pol.customer_facing_auto
    if mode == "none":
        return ["directive: no customer-facing auto-apply (routed to approval)"]
    bad = []
    for key in cf:
        if risk[key] == "reducing":
            continue
        if mode == "ramps_and_risk_reducing" and kinds.get(key) == "ramp":
            continue
        bad.append(f"{key[0]}/{key[1]}")
    if bad:
        return [f"directive {mode}: exposure-increasing customer-facing change on {sorted(bad)}"]
    return []


def _velocity_reasons(pol: EffectivePolicy, comp: Composition, velocity: VelocityLedger,
                      tvs: Mapping[tuple[str, str], float], now: int, th: Thresholds
                      ) -> list[str]:
    reasons = []
    cap = pol.max_shift - th.max_shift_margin
    for (k, dim), label in sorted(comp.risk.items()):
        if dim not in AGENT_DIMS:
            continue
        step = tvs.get((k, dim), 0.0)
        if label == "increasing":
            used = velocity.used(k, dim, now, th.velocity_window_min)
            if used + step > cap:
                reasons.append(f"velocity: {k}/{dim} would move {used + step:.4f} TV in "
                               f"{th.velocity_window_min} min (cap {pol.max_shift})")
        elif comp.cls not in (P0_INCIDENT, P1_ROLLBACK):
            if any(e.cohort == k and e.dim == dim and now - pol.tick <= e.minute <= now
                   for e in velocity.entries):
                reasons.append(f"chaining: {k}/{dim} moved at the previous tick; only P0 and P1 "
                               "risk-reducing changes may chain")
    return reasons


def check_auto_apply(pol: EffectivePolicy, comp: Composition, velocity: VelocityLedger,
                     holds: Sequence[Hold], now: int, *, in_flight: Proposal | None = None,
                     notice_until: int | None = None, rationale: str = "x",
                     t: Thresholds | None = None) -> PermitReport:
    """Every rule of DESIGN.md 6.5 for one composition, with the reasons for each refusal."""
    th = t or Thresholds()
    stored = comp.proposed.as_stored()
    aa = AutoApplyPolicy(enabled=pol.auto_enabled, allowed_dims=pol.allowed_dims,
                         max_shift=pol.max_shift)
    sv = sim_verdict(aa, comp.live, stored, rationale, margin=th.max_shift_margin)
    reasons: list[str] = []
    # 1 and 2: the simulator's checks, stricter margin, directive-narrowed allowed_dims
    reasons += [f"policy: {p}" for p in sv.problems(pol.max_shift)]
    if tuple(sv.changed_dims) != tuple(comp.changed_dims):
        reasons.append(f"composition stale: changed dims {list(comp.changed_dims)} vs stored "
                       f"{list(sv.changed_dims)}")
    approval_only = sorted(set(sv.changed_dims) & APPROVAL_ONLY_DIMS)
    if approval_only:
        reasons.append(f"design: {approval_only} are approval-only")
    # 3: directives
    kinds = change_kinds(comp.live, stored, comp.risk)
    reasons += _directive_reasons(pol, comp.risk, kinds)
    # 4: velocity and chaining
    reasons += _velocity_reasons(pol, comp, velocity, tv_by_cohort_dim(comp.live, stored),
                                 now, th)
    # 5: one change in flight
    if in_flight is not None:
        reasons.append(f"one_in_flight: {in_flight.proposal_id} ({in_flight.state}, class "
                       f"{in_flight.cls}) holds the workflow; supersede it first if allowed")
    # 6: holds and notice
    if comp.cls != P0_INCIDENT:
        active = [h for h in holds if h.wf == comp.wf and h.active(now)]
        if active:
            reasons.append(f"hold {active[0].hold_id} by {active[0].set_by} is active")
    if needs_notice(pol, comp) and (notice_until is None or now < notice_until):
        reasons.append("notice period not elapsed" if notice_until is not None
                       else "notice required and not yet posted")
    return PermitReport(not reasons, tuple(reasons), sv, comp.risk)


def auto_apply_permitted(pol: EffectivePolicy, comp: Composition, velocity: VelocityLedger,
                         holds: Sequence[Hold], now: int, *, in_flight: Proposal | None = None,
                         notice_until: int | None = None, t: Thresholds | None = None
                         ) -> tuple[bool, list[str]]:
    """DESIGN.md 11.2 signature: (permitted, reasons). See `check_auto_apply`."""
    r = check_auto_apply(pol, comp, velocity, holds, now, in_flight=in_flight,
                         notice_until=notice_until, t=t)
    return r.permitted, list(r.reasons)
