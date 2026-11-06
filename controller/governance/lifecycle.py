"""ProposalLifecycle: the table-driven state machine of DESIGN.md 6.1, supersede, pre-apply checks.

Owner: track T2 (milestone M3). Implements DESIGN.md 6.1 (proposal state machine, VERIFYING,
auto-release), 6.2 (supersede in one transaction), 8.1 (idempotency keys of submit, apply and
auto-apply) and the proposal side of 8.2 (every side effect goes through the intent executor).

Layout:
- `TRANSITIONS` is the whole state machine: (state, event) -> next state. Nothing else moves a
  proposal; `next_state` raises on anything not in the table.
- Pure rules: `pre_apply_event` (base version, apply_by - tick, late-decision re-gate),
  `can_supersede` (strictly higher class; the one same-class move is a P0a bundle replacing a
  pending P0b approval), `rejection_plan` (judgement call vs a local check that was wrong) and
  `verify_allocation` (the three checks of an allocation change).
- `ProposalStore` is the persistence port (`proposal`, `proposal_transition`, `approval_mirror`);
  `InMemoryProposalStore` is its reference implementation and enforces `one_in_flight` exactly
  like the partial unique index of 3.2.
- `ProposalLifecycle` drives proposals through the store, the overlay and an executor (the
  write-ahead `IntentExecutor` of track T1 in production; anything with `execute(kind, key,
  payload, decision)` in tests).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from controller.governance.builder import RequestPackage
from controller.governance.policy import needs_notice, sim_verdict
from controller.governance.timing import (ROUTINE_CLASSES, fatigue_ok, is_slot_minute, p_expire)
from controller.planner.overlay import Overlay
from controller.settings import Thresholds
from controller.simclient import errors as E
from controller.types import (IN_FLIGHT_STATES, P0_INCIDENT, P0A_PROMPT_SAFETY, P0B_DID_DRAIN,
                              P1_ROLLBACK, ApplyResult, ApprovalPublic, AutoApplyPolicy,
                              AutoApplyRequest, Composition, Decision, EffectivePolicy,
                              FrozenDict, Hold, Proposal, SimEpoch, freeze, make_proposal_id)

# ---------------------------------------------------------------- the state machine (6.1)
TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "no_change"): "discarded",
    ("draft", "route_approval"): "submitting",
    ("draft", "route_notice"): "noticed",
    ("draft", "route_auto"): "auto_applying",
    ("submitting", "created"): "pending",                # 201
    ("submitting", "duplicate_found"): "pending",        # 409 dup id: reconciled by proposal_id
    ("submitting", "stale"): "stale",                    # 409 stale_base_version
    ("submitting", "invalid"): "failed",                 # 422: a local check was wrong (page)
    ("noticed", "notice_elapsed"): "auto_applying",
    ("noticed", "hold"): "discarded",
    ("noticed", "superseded"): "superseded",
    ("noticed", "stale"): "stale",
    ("auto_applying", "applied"): "applied",             # 200
    ("auto_applying", "duplicate_found"): "applied",     # 409 dup id: confirmed in history
    ("auto_applying", "forbidden"): "failed_policy",     # 403: the replica was wrong (page)
    ("auto_applying", "stale"): "stale",
    ("auto_applying", "invalid"): "failed",
    ("pending", "approved"): "approved",
    ("pending", "rejected"): "rejected",
    ("pending", "expired"): "expired",
    ("pending", "superseded"): "superseded",             # never applied, even if approved later
    ("pending", "stale"): "stale",                       # base changed (supervisor, our P0 drain)
    ("approved", "stale"): "stale",                      # apply never called: no apply_conflict
    ("approved", "lapse_risk"): "lapsed_local",          # apply never called: no 410
    ("approved", "late_gate_failed"): "abandoned",       # decided > 20h after submit
    ("approved", "superseded"): "superseded",
    ("approved", "apply_start"): "applying",
    ("applying", "applied"): "applied",
    ("applying", "already_applied"): "applied",          # 409 not_approved, status applied
    ("applying", "stale"): "stale",
    ("applying", "lapsed"): "lapsed_local",              # 410: should be impossible (page)
    ("applied", "verify_start"): "verifying",
    ("verifying", "verified"): "verified",
    ("verifying", "verify_failed"): "rollback",          # a new P1 proposal reverts it
}
TERMINAL_STATES = frozenset({"discarded", "rejected", "expired", "stale", "lapsed_local",
                             "superseded", "abandoned", "failed", "failed_policy", "verified",
                             "rollback"})
SUPERSEDABLE_STATES = frozenset({"pending", "approved", "noticed"})
"""In-flight states with no HTTP call outstanding: the only ones a supersede may end."""


class IllegalTransition(RuntimeError):
    pass


class OneInFlightViolation(RuntimeError):
    """The `one_in_flight` invariant (3.2) would break: a bug in the caller."""


class SubmitBlocked(RuntimeError):
    def __init__(self, reasons: Sequence[str]):
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


def next_state(state: str, event: str) -> str:
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalTransition(f"no transition from {state!r} on {event!r}") from None


def events_from(state: str) -> list[str]:
    return sorted(e for (s, e) in TRANSITIONS if s == state)


# ---------------------------------------------------------------- pure rules
def pre_apply_event(p: Proposal, now: int, current_version: int, tick: int,
                    t: Thresholds, gates_pass: Callable[[Proposal], bool] | None = None) -> str:
    """APPROVED -> ? (DESIGN.md 6.1). Checked in this order, each before any HTTP call:
    base_version must equal the fresh current version (else STALE: never an apply_conflict);
    `now <= apply_by - tick` (else LAPSED_LOCAL: never a 410); a decision more than 20h after
    submit is re-gated on fresh data (else ABANDONED)."""
    if p.base_version != current_version:
        return "stale"
    if p.apply_by_minute is None or now > p.apply_by_minute - tick:
        return "lapse_risk"
    late = (p.decided_minute is not None and p.submitted_minute is not None
            and p.decided_minute - p.submitted_minute > t.late_decision_recheck_min)
    if late and gates_pass is not None and not gates_pass(p):
        return "late_gate_failed"
    return "apply_start"


def can_supersede(holder: Proposal, cls: int, p0_rank: int | None,
                  bundle_keeps_drain: bool = False) -> bool:
    """May a new composition of class `cls` supersede the in-flight `holder` (ADR 0013)?

    Strictly higher class only (P0 > P1 > P2 > P3), and only while no HTTP call is outstanding.
    Same class: only a P0a prompt-fix bundle that keeps the drain may replace a PENDING P0b drain
    approval. A pending P0a approval is never superseded (nothing outranks it)."""
    if holder.state not in SUPERSEDABLE_STATES:
        return False
    if cls < holder.cls:
        return True
    if (cls == holder.cls == P0_INCIDENT and p0_rank == P0A_PROMPT_SAFETY
            and holder.p0_rank == P0B_DID_DRAIN and holder.state == "pending"):
        return bundle_keeps_drain
    return False


JUDGEMENT_MARK = "judgement call"


@dataclass(frozen=True)
class RejectionPlan:
    kind: str
    """judgement | local_check"""
    action: str
    """resubmit_now | requeue_next_slot | drop_needs_discussion | hold"""
    page: str | None
    """alert code, or None"""


def is_judgement(reasons: Sequence[str]) -> bool:
    return bool(reasons) and all(JUDGEMENT_MARK in r for r in reasons)


def rejection_plan(cls: int, reasons: Sequence[str], prior_judgement_rejections: int
                   ) -> RejectionPlan:
    """DESIGN.md 6.1 rejection rows. A judgement call is the approver's coin flip; any other
    reason (fatigue, evidence, format) means one of our local checks is wrong."""
    if not is_judgement(reasons):
        return RejectionPlan("local_check", "hold", "GOV_INVARIANT")
    if cls in (P0_INCIDENT, P1_ROLLBACK):
        return RejectionPlan("judgement", "resubmit_now", None)
    if prior_judgement_rejections >= 1:
        return RejectionPlan("judgement", "drop_needs_discussion", None)
    return RejectionPlan("judgement", "requeue_next_slot", None)


# ---------------------------------------------------------------- verification (6.1)
@dataclass(frozen=True)
class ShareObservation:
    """(a): configured vs realised share of one option in one cohort and dim."""

    cohort: str
    dim: str
    option: str
    configured: float
    dials_with_option: int
    dials_total: int


@dataclass(frozen=True)
class ArmObservation:
    """(b): an arm whose share rose: arm vs control on the verification segments."""

    cohort: str
    dim: str
    arm: str
    observed_rel: float
    dry_run_ci90_low: float


@dataclass(frozen=True)
class VerifyResult:
    status: str
    """verified | rollback (harm stop) | pending (not due yet)"""
    reasons: tuple[str, ...] = ()
    page: str | None = None


def verify_allocation(shares: Sequence[ShareObservation], arms: Sequence[ArmObservation],
                      ew_p_de_below_1pct: float, cohort_line_passes: bool, t: Thresholds
                      ) -> VerifyResult:
    """Allocation changes over the next 2 segments: (a) realised shares within 3 binomial SE
    of the configured ones (failure only pages COHORT_MISMATCH); (b) every arm whose share rose
    lies inside or above the dry run's 90% interval; (c) the EW posterior of the change still
    has P(de < -1%) <= 10% and passes the cohort line. (b) or (c) failing is a harm stop."""
    reasons, page = [], None
    for s in shares:
        if s.dials_total <= 0:
            continue
        se = math.sqrt(max(s.configured * (1 - s.configured), 1e-12) / s.dials_total)
        realised = s.dials_with_option / s.dials_total
        if abs(realised - s.configured) > t.alloc_verify_se * se:
            page = "COHORT_MISMATCH"
            reasons.append(f"(a) {s.cohort}/{s.dim}/{s.option}: realised {realised:.3f} vs "
                           f"configured {s.configured:.3f} (SE {se:.4f})")
    harm = []
    for a in arms:
        if a.observed_rel < a.dry_run_ci90_low:
            harm.append(f"(b) {a.cohort}/{a.dim}/{a.arm}: observed {a.observed_rel:+.3f} below "
                        f"the dry run's 90% interval (low {a.dry_run_ci90_low:+.3f})")
    if ew_p_de_below_1pct > t.alloc_verify_p_harm:
        harm.append(f"(c) EW P(de < -1%) = {ew_p_de_below_1pct:.3f} > {t.alloc_verify_p_harm}")
    if not cohort_line_passes:
        harm.append("(c) the cohort harm line fails")
    if harm:
        return VerifyResult("rollback", tuple(reasons + harm), page)
    return VerifyResult("verified", tuple(reasons), page)


class Verifier(Protocol):
    """A kind-specific post-apply check (prompt fix 7.1, DID 7.3, schedule 5.8, allocation)."""

    def __call__(self, p: Proposal, now: int) -> VerifyResult: ...


def verify_kind(p: Proposal) -> str:
    """Which verifier a proposal needs, from the dims it changed (prompt first)."""
    dims = set((p.diff or {}).get("changed_dims") or ())
    if "prompt_version" in dims:
        return "prompt"
    if dims & {"welcome_message", "voice"}:
        return "allocation"
    if "caller_id" in dims:
        return "caller_id"
    if "schedule" in dims:
        return "schedule"
    return "none"


# ---------------------------------------------------------------- persistence port
@dataclass(frozen=True)
class TransitionRow:
    proposal_id: str
    seq: int
    from_state: str | None
    to_state: str
    sim_minute: int
    reason: str
    decision_id: int | None


class ProposalStore(Protocol):
    def get(self, proposal_id: str) -> Proposal | None: ...
    def holder(self, wf: str) -> Proposal | None: ...
    def in_states(self, states: Iterable[str], wf: str | None = None) -> list[Proposal]: ...
    def insert(self, p: Proposal, minute: int, reason: str) -> Proposal: ...
    def apply(self, proposal_id: str, event: str, minute: int, reason: str,
              **fields: Any) -> Proposal: ...
    def supersede(self, holder_id: str, new: Proposal, minute: int, reason: str) -> Proposal: ...
    def next_seq(self, wf: str) -> int: ...
    def submitted_minutes(self, wf: str) -> list[int]: ...
    def transitions_of(self, proposal_id: str) -> list[TransitionRow]: ...
    def mirror(self, a: ApprovalPublic, epoch: SimEpoch, minute: int) -> None: ...
    def mirrored_reasons(self, approval_id: str) -> tuple[str, ...]: ...


class InMemoryProposalStore:
    """Reference ProposalStore. `insert` and `supersede` enforce one_in_flight like the partial
    unique index; `supersede` ends the holder and inserts the new proposal as one step."""

    def __init__(self) -> None:
        self.rows: dict[str, Proposal] = {}
        self.transitions: list[TransitionRow] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self._seq: dict[str, int] = {}

    def get(self, proposal_id: str) -> Proposal | None:
        return self.rows.get(proposal_id)

    def holder(self, wf: str) -> Proposal | None:
        held = [p for p in self.rows.values() if p.wf == wf and p.state in IN_FLIGHT_STATES]
        return held[0] if held else None

    def in_states(self, states: Iterable[str], wf: str | None = None) -> list[Proposal]:
        s = set(states)
        return sorted((p for p in self.rows.values()
                       if p.state in s and (wf is None or p.wf == wf)),
                      key=lambda p: p.proposal_id)

    def _log(self, pid: str, frm: str | None, to: str, minute: int, reason: str,
             decision_id: int | None) -> None:
        seq = sum(1 for t in self.transitions if t.proposal_id == pid)
        self.transitions.append(TransitionRow(pid, seq, frm, to, minute, reason, decision_id))

    def _check_lock(self, p: Proposal, ignore: str | None = None) -> None:
        if p.state in IN_FLIGHT_STATES:
            h = self.holder(p.wf)
            if h is not None and h.proposal_id not in (p.proposal_id, ignore):
                raise OneInFlightViolation(f"{p.wf}: {h.proposal_id} is in flight ({h.state})")

    def insert(self, p: Proposal, minute: int, reason: str) -> Proposal:
        if p.proposal_id in self.rows:
            raise ValueError(f"duplicate proposal id {p.proposal_id}")
        self._check_lock(p)
        self.rows[p.proposal_id] = p
        self._log(p.proposal_id, None, p.state, minute, reason, p.decision_id)
        return p

    def apply(self, proposal_id: str, event: str, minute: int, reason: str,
              **fields: Any) -> Proposal:
        p = self.rows[proposal_id]
        new = replace(p, state=next_state(p.state, event), **fields)
        self._check_lock(new)
        self.rows[proposal_id] = new
        self._log(proposal_id, p.state, new.state, minute, f"{event}: {reason}", new.decision_id)
        return new

    def supersede(self, holder_id: str, new: Proposal, minute: int, reason: str) -> Proposal:
        h = self.rows[holder_id]
        ended = replace(h, state=next_state(h.state, "superseded"),
                        superseded_by=new.proposal_id)
        self._check_lock(new, ignore=holder_id)
        self.rows[holder_id] = ended
        self._log(holder_id, h.state, ended.state, minute, f"superseded: {reason}", None)
        return self.insert(new, minute, f"supersedes {holder_id}: {reason}")

    def next_seq(self, wf: str) -> int:
        self._seq[wf] = self._seq.get(wf, 0) + 1
        return self._seq[wf]

    def submitted_minutes(self, wf: str) -> list[int]:
        return sorted(p.submitted_minute for p in self.rows.values()
                      if p.wf == wf and p.route == "approval" and p.submitted_minute is not None
                      and p.approval_id is not None)

    def transitions_of(self, proposal_id: str) -> list[TransitionRow]:
        return [t for t in self.transitions if t.proposal_id == proposal_id]

    def mirror(self, a: ApprovalPublic, epoch: SimEpoch, minute: int) -> None:
        self.approvals[a.approval_id] = {
            "proposal_id": a.proposal_id, "wf": a.workflow_id, "status": a.status,
            "created_minute": epoch.minute(a.created_at),
            "expires_minute": epoch.minute(a.expires_at),
            "decided_minute": None if a.decided_at is None else epoch.minute(a.decided_at),
            "apply_by_minute": None if a.apply_by is None else epoch.minute(a.apply_by),
            "reasons": list(a.reasons), "last_polled_minute": minute}

    def mirrored_reasons(self, approval_id: str) -> tuple[str, ...]:
        return tuple((self.approvals.get(approval_id) or {}).get("reasons") or ())


# ---------------------------------------------------------------- the lifecycle
class SimReader(Protocol):
    def config(self, wf: str) -> Any: ...
    def config_history(self, wf: str) -> list[Any]: ...
    def approvals(self, wf: str | None = None, status: str | None = None
                  ) -> list[ApprovalPublic]: ...
    def approval(self, aid: str) -> ApprovalPublic: ...


class Executor(Protocol):
    def execute(self, kind: str, key: str, payload: dict[str, Any], decision: Decision) -> Any: ...


@dataclass
class LifecycleDeps:
    store: ProposalStore
    overlay: Overlay
    sim: SimReader
    executor: Executor
    policy_of: Callable[[str], EffectivePolicy]
    clock: Callable[[], int]
    epoch: SimEpoch
    run6: str
    settings_hash: str = ""
    code_version: str = "dev"
    t: Thresholds = field(default_factory=Thresholds)
    holds: Callable[[str], Sequence[Hold]] = lambda wf: ()
    regate: Callable[[Proposal, str], bool] | None = None
    """Re-run the harm gate on fresh data ("late_decision" or "notice"); None: always passes."""
    verifiers: Mapping[str, Verifier] = field(default_factory=dict)
    on_alert: Callable[[str, str, str], None] = lambda code, wf, msg: None
    on_rollback: Callable[[Proposal, VerifyResult], None] = lambda p, r: None


@dataclass(frozen=True)
class SubmitResult:
    proposal: Proposal
    outcome: str
    detail: str = ""


class ProposalLifecycle:
    """Moves proposals through TRANSITIONS; the only module that calls submit, apply and
    auto-apply (through the executor). DESIGN.md 11.2 interface plus keyword extensions."""

    def __init__(self, deps: LifecycleDeps):
        self.d = deps
        self.auto_blocked: dict[str, str] = {}
        """wf -> reason: after a 403 every auto-apply on that wf routes to approval until the
        policy is re-read (the replica is wrong there; never retry the body)."""

    # ------------------------------------------------------------ helpers
    def _decision(self, wf: str, kind: str, rule: str, inputs: Mapping[str, Any]) -> Decision:
        return Decision(wf=wf, module="governance.lifecycle", kind=kind, rule_id=rule,
                        inputs=freeze(dict(inputs)), outcome=FrozenDict(), harm_report=None,
                        snapshot_ids=(), seed=0, settings_hash=self.d.settings_hash,
                        code_version=self.d.code_version, sim_minute=self.d.clock())

    def used(self, wf: str) -> list[int]:
        """Request minutes for fatigue: every record `GET /approvals` lists for the workflow
        (the simulator counts all of them), plus any of ours it does not list yet. A list, not
        a set: two requests in one minute (a supersede) count twice."""
        listed = self.d.sim.approvals(wf)
        ids = {a.approval_id for a in listed}
        mins = [self.d.epoch.minute(a.created_at) for a in listed]
        mins += [p.submitted_minute for p in self.d.store.in_states(
                     ("pending", "approved", "applying", "applied", "verifying", "verified",
                      "rejected", "expired", "stale", "lapsed_local", "superseded", "abandoned",
                      "rollback"), wf)
                 if p.route == "approval" and p.approval_id and p.approval_id not in ids
                 and p.submitted_minute is not None]
        return sorted(mins)

    def submit_blockers(self, comp: Composition, now: int, *, superseding: bool = False
                        ) -> list[str]:
        """DRAFT -> SUBMITTING preconditions (6.1 first row), each as a reason."""
        pol = self.d.policy_of(comp.wf)
        out = []
        if p_expire(now, pol) > self.d.t.submit_expiry_p_max:
            out.append(f"window: P(expire) at {now} is {p_expire(now, pol):.4f}")
        if not fatigue_ok(now, pol, self.used(comp.wf), comp.cls, self.d.t):
            out.append("fatigue: no request left in the class budget")
        if comp.cls in ROUTINE_CLASSES and not is_slot_minute(now, pol, t=self.d.t):
            out.append("slot: P2/P3 submit only at a routine slot minute")
        if not superseding and self.d.store.holder(comp.wf) is not None:
            out.append(f"one_in_flight: held by {self.d.store.holder(comp.wf).proposal_id}")
        if comp.cls != P0_INCIDENT and any(h.active(now) for h in self.d.holds(comp.wf)):
            out.append("hold: a supervisor hold is active")
        current = self.d.sim.config(comp.wf).version
        if comp.base_version != current:
            out.append(f"base_version {comp.base_version} != current {current}")
        return out

    def _draft(self, comp: Composition, now: int, route: str, package: RequestPackage | None,
               rationale: str) -> Proposal:
        wf = comp.wf
        pid = make_proposal_id(self.d.run6, wf, now, comp.cls, self.d.store.next_seq(wf))
        prev = sorted({p.proposal_id for p in comp.patches if p.proposal_id})
        stored = comp.proposed.as_stored()
        pkg = package
        evidence = dict(pkg.evidence) if pkg else {"class": comp.cls}
        return Proposal(
            proposal_id=pid, wf=wf, cls=comp.cls, p0_rank=comp.p0_rank, route=route,
            state="draft",
            base_version=comp.base_version, proposed_config=stored,
            change_summary=pkg.change_summary if pkg else rationale,
            diff=pkg.diff if pkg else freeze({"changed_dims": list(comp.changed_dims)}),
            affected_leads=pkg.affected_leads if pkg else 0,
            dry_run=pkg.dry_run if pkg else FrozenDict(),
            impact_score=pkg.impact_score if pkg else 0.0,
            rationale=pkg.rationale if pkg else rationale, evidence=freeze(evidence),
            requeued_from=prev[-1] if prev else None,
            decision_id=min((p.decision_id for p in comp.patches), default=None))

    def _insert(self, draft: Proposal, comp: Composition, now: int, reason: str) -> Proposal:
        p = self.d.store.insert(draft, now, reason)
        self.d.overlay.mark_proposed([x.patch_id for x in comp.patches], p.proposal_id)
        return p

    # ------------------------------------------------------------ submit (approval route)
    def submit(self, comp: Composition, now: int, *, package: RequestPackage | None = None,
               check: bool = True) -> Proposal:
        """DRAFT -> SUBMITTING -> PENDING (or a terminal state) for the approval route."""
        if not comp.changed_dims:
            return self._discard(comp, now)
        if check:
            blockers = self.submit_blockers(comp, now)
            if blockers:
                raise SubmitBlocked(blockers)
        draft = self._draft(comp, now, "approval", package, "")
        p = self._insert(draft, comp, now, "drafted")
        return self._send_approval(p, now)

    def supersede(self, holder: Proposal, by: Composition, *, now: int | None = None,
                  package: RequestPackage | None = None, route: str = "approval",
                  bundle_keeps_drain: bool = False, rationale: str = "") -> Proposal:
        """End the holder as SUPERSEDED and insert the new proposal in one store step (the only
        way past one_in_flight), then send it. The holder's patches return to the overlay."""
        now = self.d.clock() if now is None else now
        if not can_supersede(holder, by.cls, by.p0_rank, bundle_keeps_drain):
            raise SubmitBlocked([f"class {by.cls} may not supersede {holder.proposal_id} "
                                 f"(class {holder.cls}, {holder.state})"])
        if route == "approval":
            blockers = self.submit_blockers(by, now, superseding=True)
            if blockers:
                raise SubmitBlocked(blockers)
        draft = self._draft(by, now, route, package, rationale)
        p = self.d.store.supersede(holder.proposal_id, draft, now,
                                   f"class {by.cls} change takes precedence")
        self.d.overlay.return_to_open(holder.proposal_id)
        self.d.overlay.mark_proposed([x.patch_id for x in by.patches], p.proposal_id)
        if route == "approval":
            return self._send_approval(p, now)
        return self._send_auto(p, now)

    def _discard(self, comp: Composition, now: int) -> Proposal:
        draft = self._draft(comp, now, "approval", None, "no net change")
        p = self._insert(draft, comp, now, "drafted")
        p = self.d.store.apply(p.proposal_id, "no_change", now, "composition has no net change")
        self.d.overlay.drop(p.proposal_id)
        return p

    def _request_body(self, p: Proposal) -> dict[str, Any]:
        return {"proposal_id": p.proposal_id, "workflow_id": p.wf,
                "base_version": p.base_version, "proposed_config": p.proposed_config.body(),
                "change_summary": p.change_summary, "diff": dict(p.diff) or {"note": "-"},
                "affected_leads": int(p.affected_leads),
                "dry_run": dict(p.dry_run) or {"method": "not estimated"},
                "impact_score": float(p.impact_score),
                "rationale": p.rationale or p.change_summary,
                "evidence": dict(p.evidence)}

    def _send_approval(self, p: Proposal, now: int) -> Proposal:
        p = self.d.store.apply(p.proposal_id, "route_approval", now, "gates passed")
        body = self._request_body(p)
        p = replace(p, request=freeze(body))
        decision = self._decision(p.wf, "submit_approval", "6.1/submit",
                                  {"proposal_id": p.proposal_id, "class": p.cls})
        try:
            a = self.d.executor.execute("submit_approval", p.proposal_id, body, decision)
        except E.DuplicateProposalId:
            found = [x for x in self.d.sim.approvals(p.wf) if x.proposal_id == p.proposal_id]
            if not found:
                self.d.on_alert("GOV_INVARIANT", p.wf, f"{p.proposal_id}: duplicate id, no record")
                raise
            a = found[0]
            return self._created(p, a, now, "duplicate_found")
        except E.StaleBaseVersion as e:
            p = self.d.store.apply(p.proposal_id, "stale", now, f"current {e.current_version}")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        except E.Unprocessable as e:
            p = self.d.store.apply(p.proposal_id, "invalid", now, str(e.detail))
            self.d.overlay.hold_proposal(p.proposal_id, now + self.d.t.held_patch_release_min)
            self.d.on_alert("GOV_INVARIANT", p.wf, f"{p.proposal_id}: 422 {e.code}")
            return p
        except E.SimTransportError:
            return p          # SUBMITTING: the reconciler resolves it by proposal_id (8.3)
        return self._created(p, a, now, "created")

    def _created(self, p: Proposal, a: ApprovalPublic, now: int, event: str) -> Proposal:
        self.d.store.mirror(a, self.d.epoch, now)
        p = self.d.store.apply(p.proposal_id, event, now, a.approval_id,
                               approval_id=a.approval_id,
                               submitted_minute=self.d.epoch.minute(a.created_at),
                               request=p.request)
        if a.status != "pending":
            p = self._on_status(p, a, now)
        return p

    # ------------------------------------------------------------ auto route
    def auto_apply(self, comp: Composition, now: int, *, rationale: str,
                   notice_until: int | None = None, package: RequestPackage | None = None
                   ) -> Proposal:
        """DRAFT -> NOTICED (if a notice is due) or AUTO_APPLYING -> APPLIED -> VERIFYING.
        The caller has already passed `policy.auto_apply_permitted` (with notice_until=None the
        notice is posted here and the apply waits for `tick` after it elapses)."""
        if not comp.changed_dims:
            return self._discard(comp, now)
        if self.d.store.holder(comp.wf) is not None:
            raise SubmitBlocked([f"one_in_flight: held by {self.d.store.holder(comp.wf).proposal_id}"])
        draft = self._draft(comp, now, "auto", package, rationale)
        p = self._insert(draft, comp, now, "drafted")
        pol = self.d.policy_of(comp.wf)
        if needs_notice(pol, comp) and (notice_until is None or now < notice_until):
            until = notice_until if notice_until is not None else now + pol.notice_minutes
            return self.d.store.apply(p.proposal_id, "route_notice", now, f"notice until {until}",
                                      notice_until_minute=until)
        return self._send_auto(p, now)

    def _send_auto(self, p: Proposal, now: int) -> Proposal:
        if p.state == "draft":
            p = self.d.store.apply(p.proposal_id, "route_auto", now, "auto-apply permitted")
        body = AutoApplyRequest(proposal_id=p.proposal_id, base_version=p.base_version,
                                proposed_config=p.proposed_config,
                                rationale=p.rationale or p.change_summary or "controller change",
                                evidence=p.evidence).body()
        decision = self._decision(p.wf, "auto_apply", "6.5/auto", {"proposal_id": p.proposal_id})
        try:
            res: ApplyResult = self.d.executor.execute("auto_apply", p.proposal_id, body, decision)
        except E.AutoApplyNotPermitted as e:
            self.auto_blocked[p.wf] = f"403 on {p.proposal_id}: {e.problems}"
            self.d.on_alert("AUTO_APPLY_REJECTED", p.wf, f"{p.proposal_id}: {e.problems}")
            p = self.d.store.apply(p.proposal_id, "forbidden", now, "; ".join(e.problems))
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        except E.DuplicateProposalId:
            hist = [v for v in self.d.sim.config_history(p.wf) if v.proposal_id == p.proposal_id]
            if not hist:
                self.d.on_alert("GOV_INVARIANT", p.wf, f"{p.proposal_id}: duplicate id, no version")
                raise
            return self._applied(p, hist[0].version, now, "duplicate_found")
        except E.StaleBaseVersion as e:
            p = self.d.store.apply(p.proposal_id, "stale", now, f"current {e.current_version}")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        except E.Unprocessable as e:
            p = self.d.store.apply(p.proposal_id, "invalid", now, str(e.detail))
            self.d.overlay.hold_proposal(p.proposal_id, now + self.d.t.held_patch_release_min)
            self.d.on_alert("GOV_INVARIANT", p.wf, f"{p.proposal_id}: 422 {e.code}")
            return p
        except E.SimTransportError:
            return p          # AUTO_APPLYING: reconciled via config history (8.3)
        return self._applied(p, res.version, now, "applied")

    def _applied(self, p: Proposal, version: int, now: int, event: str) -> Proposal:
        p = self.d.store.apply(p.proposal_id, event, now, f"version {version}",
                               applied_version=version)
        self.d.overlay.mark_applied(p.proposal_id)
        return self.d.store.apply(p.proposal_id, "verify_start", now, "verification window opens")

    # ------------------------------------------------------------ tick (4.1 step 5)
    def tick(self, now: int) -> list[Proposal]:
        """Poll every pending and approved proposal, apply visible approvals after the
        pre-apply checks, release noticed auto-applies, and requeue terminal ones."""
        changed = []
        for p in self.d.store.in_states(("pending", "approved", "noticed")):
            if p.state == "noticed":
                q = self._tick_noticed(p, now)
            else:
                a = self.d.sim.approval(p.approval_id)
                self.d.store.mirror(a, self.d.epoch, now)
                q = self._on_status(p, a, now)
            if q.state != p.state:
                changed.append(q)
        # SUPERSEDED proposals are not polled: never applied, even if approved later.
        return changed

    def _on_status(self, p: Proposal, a: ApprovalPublic, now: int) -> Proposal:
        if p.state == "pending":
            if a.status == "approved":
                p = self.d.store.apply(p.proposal_id, "approved", now, a.approval_id,
                                       decided_minute=self.d.epoch.minute(a.decided_at),
                                       apply_by_minute=self.d.epoch.minute(a.apply_by))
            elif a.status in ("rejected", "expired"):
                return self._ended(p, a, now)
            else:
                return p
        if p.state == "approved":
            return self._try_apply(p, now)
        return p

    def _ended(self, p: Proposal, a: ApprovalPublic, now: int) -> Proposal:
        decided = None if a.decided_at is None else self.d.epoch.minute(a.decided_at)
        if a.status == "expired":
            p = self.d.store.apply(p.proposal_id, "expired", now, "expired (re-queued fresh)",
                                   decided_minute=decided)
            self.d.on_alert("APPROVAL_EXPIRED", p.wf, f"{p.proposal_id} expired")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        plan = rejection_plan(p.cls, a.reasons, self._prior_judgements(p))
        p = self.d.store.apply(p.proposal_id, "rejected", now,
                               f"{plan.kind}: {'; '.join(a.reasons)} -> {plan.action}",
                               decided_minute=decided)
        if plan.page:
            self.d.on_alert(plan.page, p.wf, f"{p.proposal_id} rejected: {list(a.reasons)}")
        if plan.action == "hold":
            self.d.overlay.hold_proposal(p.proposal_id, now + self.d.t.held_patch_release_min)
        elif plan.action == "drop_needs_discussion":
            self.d.overlay.drop(p.proposal_id)
        else:
            self.d.overlay.return_to_open(p.proposal_id)
        return p

    def _prior_judgements(self, p: Proposal) -> int:
        n, cur = 0, p.requeued_from
        while cur:
            q = self.d.store.get(cur)
            if q is None:
                break
            if q.state == "rejected" and q.approval_id and is_judgement(
                    self.d.store.mirrored_reasons(q.approval_id)):
                n += 1
            cur = q.requeued_from
        return n

    def _try_apply(self, p: Proposal, now: int) -> Proposal:
        pol = self.d.policy_of(p.wf)
        current = self.d.sim.config(p.wf).version
        gate = (lambda q: self.d.regate(q, "late_decision")) if self.d.regate else None
        event = pre_apply_event(p, now, current, pol.tick, self.d.t, gate)
        if event != "apply_start":
            p = self.d.store.apply(p.proposal_id, event, now,
                                   f"current version {current}, apply_by {p.apply_by_minute}")
            if event == "lapse_risk":
                self.d.on_alert("APPLY_AT_RISK", p.wf, f"{p.proposal_id} not applied in time")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        p = self.d.store.apply(p.proposal_id, "apply_start", now, "pre-apply checks passed")
        decision = self._decision(p.wf, "apply", "6.1/apply",
                                  {"proposal_id": p.proposal_id, "approval_id": p.approval_id})
        try:
            res = self.d.executor.execute("apply", p.approval_id,
                                          {"workflow_id": p.wf, "approval_id": p.approval_id},
                                          decision)
        except E.NotApproved as e:
            if e.approval_status == "applied":
                hist = [v for v in self.d.sim.config_history(p.wf)
                        if v.approval_id == p.approval_id]
                return self._applied(p, hist[0].version if hist else current + 1, now,
                                     "already_applied")
            raise
        except E.StaleBaseVersion as e:
            p = self.d.store.apply(p.proposal_id, "stale", now, f"current {e.current_version}")
            self.d.on_alert("GOV_INVARIANT", p.wf, f"{p.proposal_id}: apply conflict")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        except E.ApprovalLapsed:
            p = self.d.store.apply(p.proposal_id, "lapsed", now, "410 at the simulator")
            self.d.on_alert("APPLY_AT_RISK", p.wf, f"{p.proposal_id} lapsed (410)")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        except E.SimTransportError:
            return p          # APPLYING: retry-safe by approval_id (8.1)
        return self._applied(p, res.version, now, "applied")

    def _tick_noticed(self, p: Proposal, now: int) -> Proposal:
        if p.notice_until_minute is None or now < p.notice_until_minute:
            return p
        held = p.cls != P0_INCIDENT and any(h.active(now) for h in self.d.holds(p.wf))
        current = self.d.sim.config(p.wf).version
        if current != p.base_version:
            p = self.d.store.apply(p.proposal_id, "stale", now, f"current version {current}")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        if held or (self.d.regate is not None and not self.d.regate(p, "notice")):
            p = self.d.store.apply(p.proposal_id, "hold", now,
                                   "hold set" if held else "harm gate fails at notice expiry")
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        if p.wf in self.auto_blocked:
            p = self.d.store.apply(p.proposal_id, "hold", now, self.auto_blocked[p.wf])
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        pol = self.d.policy_of(p.wf)      # the policy may have been re-read during the notice
        live = self.d.sim.config(p.wf).config
        sv = sim_verdict(AutoApplyPolicy(pol.auto_enabled, pol.allowed_dims, pol.max_shift),
                         live, p.proposed_config, "x", margin=self.d.t.max_shift_margin)
        if not sv.permitted:
            p = self.d.store.apply(p.proposal_id, "hold", now,
                                   "policy no longer permits: " + "; ".join(sv.problems(pol.max_shift)))
            self.d.overlay.return_to_open(p.proposal_id)
            return p
        p = self.d.store.apply(p.proposal_id, "notice_elapsed", now, "notice period elapsed")
        return self._send_auto(p, now)

    # ------------------------------------------------------------ verify (6.1)
    def verify(self, now: int) -> list[Proposal]:
        out = []
        for p in self.d.store.in_states(("verifying",)):
            kind = verify_kind(p)
            check = self.d.verifiers.get(kind)
            if check is None:
                continue          # the owning track's verifier is not wired yet: stay VERIFYING
            r = check(p, now)
            if r.status == "pending":
                continue
            if r.page:
                self.d.on_alert(r.page, p.wf, f"{p.proposal_id}: {'; '.join(r.reasons)}")
            event = "verified" if r.status == "verified" else "verify_failed"
            q = self.d.store.apply(p.proposal_id, event, now, "; ".join(r.reasons) or kind,
                                   verify=freeze({"kind": kind, "status": r.status,
                                                  "reasons": list(r.reasons)}))
            if event == "verify_failed":
                self.d.on_rollback(q, r)
            out.append(q)
        return out

    # ------------------------------------------------------------ supervisor edits (6.7 step 3)
    def mark_stale(self, wf: str, version: int, now: int) -> list[Proposal]:
        """Every PENDING, APPROVED or NOTICED proposal with base_version < version becomes STALE
        (never applied); its patches return to the overlay for the rebase."""
        out = []
        for p in self.d.store.in_states(("pending", "approved", "noticed"), wf=wf):
            if p.base_version < version:
                q = self.d.store.apply(p.proposal_id, "stale", now,
                                       f"config changed to v{version} (supervisor)")
                self.d.overlay.return_to_open(p.proposal_id)
                out.append(q)
        return out

    def policy_reread(self, wf: str) -> None:
        """A fresh policy read clears the post-403 auto-apply block (6.5)."""
        self.auto_blocked.pop(wf, None)
