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
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ContextManager, Iterable, Mapping, Protocol, Sequence

from controller.governance.builder import RequestPackage
from controller.governance.policy import needs_notice, sim_verdict
from controller.governance.timing import (ROUTINE_CLASSES, fatigue_ok, is_slot_minute, p_expire)
from controller.planner.overlay import Overlay
from controller.settings import Thresholds
from controller.simclient import errors as E
from controller.types import (IN_FLIGHT_STATES, P0_INCIDENT, P0A_PROMPT_SAFETY, P0B_DID_DRAIN,
                              P1_ROLLBACK, ApplyResult, ApprovalPublic, AutoApplyPolicy,
                              AutoApplyRequest, Composition, Decision, EffectivePolicy,
                              FrozenDict, Hold, Proposal, SimEpoch, VelocityEntry, freeze,
                              make_proposal_id, thaw)

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
    # Crash recovery (DESIGN.md 8.3, ADR 0029). A drafted proposal whose request never reached the
    # simulator goes back to DRAFT and is either resent with the same id and body (every SUBMITTING
    # precondition still holds) or aborted; an apply that did not take effect returns to APPROVED,
    # where the pre-apply checks run again.
    ("submitting", "not_sent"): "draft",
    ("auto_applying", "not_sent"): "draft",
    ("draft", "abort"): "discarded",
    ("applying", "not_applied"): "approved",
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
    def atomic(self) -> ContextManager[Any]:
        """Group several writes into one transaction (a no-op in memory)."""
        ...
    def velocity_entries(self, wf: str, since_minute: int) -> list[VelocityEntry]:
        """Ledger entries of applied auto-applies with `applied_minute > since_minute`."""
        ...


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

    def atomic(self) -> ContextManager[Any]:
        return nullcontext()

    def velocity_entries(self, wf: str, since_minute: int) -> list[VelocityEntry]:
        out = [e for p in self.rows.values() if p.wf == wf for e in p.velocity_entries()
               if e.minute > since_minute]
        return sorted(out, key=lambda e: (e.minute, e.proposal_id, e.cohort, e.dim))


# ---------------------------------------------------------------- the lifecycle
class SimReader(Protocol):
    def config(self, wf: str) -> Any: ...
    def config_history(self, wf: str) -> list[Any]: ...
    def approvals(self, wf: str | None = None, status: str | None = None
                  ) -> list[ApprovalPublic]: ...
    def approval(self, aid: str) -> ApprovalPublic: ...


class Executor(Protocol):
    """Runs one side effect. `before` (when given) is the proposal's "before" transition: the
    production executor (`governance/effects.py` over T1's IntentExecutor) runs it inside the
    write-ahead T1 transaction, after the decision and intent rows; a test executor runs it
    first. The response is the typed API value or its JSON form."""

    def execute(self, kind: str, key: str, payload: dict[str, Any], decision: Decision, *,
                before: Callable[[], None] | None = None) -> Any: ...


EFFECT_KINDS = ("submit_approval", "auto_apply", "apply")
"""The intent kinds whose domain transitions this module owns (8.3)."""


def _approval(x: Any) -> ApprovalPublic:
    return x if isinstance(x, ApprovalPublic) else ApprovalPublic.from_api(x)


def _apply_result(x: Any) -> ApplyResult:
    return x if isinstance(x, ApplyResult) else ApplyResult.from_api(x)


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
    on_applied: Callable[[Proposal, int], None] = lambda p, version: None
    """Called in the transaction that marks a proposal APPLIED (the config mirror records the
    body we sent as `ours`, DESIGN.md 4.1 step 4)."""


@dataclass(frozen=True)
class SubmitResult:
    proposal: Proposal
    outcome: str
    detail: str = ""


class ProposalLifecycle:
    """Moves proposals through TRANSITIONS; the only module that calls submit, apply and
    auto-apply (through the executor). DESIGN.md 11.2 interface plus keyword extensions.

    Side effects and their transitions (DESIGN.md 8.2, ADR 0029):
    - the "before" transition (DRAFT -> SUBMITTING, DRAFT/NOTICED -> AUTO_APPLYING,
      APPROVED -> APPLYING) is passed to the executor and commits with the intent row (T1);
    - `on_effect_done`, `on_effect_failed` and `on_effect_not_run` are the domain transitions.
      The production executor calls them inside the intent's T2 (or the reconciler's)
      transaction; the code after `execute` calls them again, which is a no-op when the state
      already moved, so a test executor without hooks gets the same transitions;
    - `recover(now)` resolves whatever a crash left between those transactions: a proposal still
      in a call state is looked up at the simulator, and a DRAFT (never sent) is resent with the
      same id and body when every precondition still holds, else aborted and its patches
      requeued."""

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

    def _get(self, pid: str) -> Proposal:
        p = self.d.store.get(pid)
        if p is None:
            raise KeyError(pid)
        return p

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
        return self._blockers(comp.wf, comp.cls, comp.base_version, now, superseding=superseding)

    def _blockers(self, wf: str, cls: int, base_version: int, now: int, *,
                  superseding: bool = False) -> list[str]:
        pol = self.d.policy_of(wf)
        out = []
        if p_expire(now, pol) > self.d.t.submit_expiry_p_max:
            out.append(f"window: P(expire) at {now} is {p_expire(now, pol):.4f}")
        if not fatigue_ok(now, pol, self.used(wf), cls, self.d.t):
            out.append("fatigue: no request left in the class budget")
        if cls in ROUTINE_CLASSES and not is_slot_minute(now, pol, t=self.d.t):
            out.append("slot: P2/P3 submit only at a routine slot minute")
        holder = self.d.store.holder(wf)
        if not superseding and holder is not None:
            out.append(f"one_in_flight: held by {holder.proposal_id}")
        if cls != P0_INCIDENT and any(h.active(now) for h in self.d.holds(wf)):
            out.append("hold: a supervisor hold is active")
        current = self.d.sim.config(wf).version
        if base_version != current:
            out.append(f"base_version {base_version} != current {current}")
        return out

    def _auto_blockers(self, p: Proposal, now: int) -> list[str]:
        """What must still hold before an unsent auto-apply is resent (8.3 auto_apply row)."""
        out = []
        snap = self.d.sim.config(p.wf)
        if snap.version != p.base_version:
            out.append(f"base_version {p.base_version} != current {snap.version}")
        holder = self.d.store.holder(p.wf)
        if holder is not None and holder.proposal_id != p.proposal_id:
            out.append(f"one_in_flight: held by {holder.proposal_id}")
        if p.cls != P0_INCIDENT and any(h.active(now) for h in self.d.holds(p.wf)):
            out.append("hold: a supervisor hold is active")
        if p.wf in self.auto_blocked:
            out.append(self.auto_blocked[p.wf])
        pol = self.d.policy_of(p.wf)
        sv = sim_verdict(AutoApplyPolicy(pol.auto_enabled, pol.allowed_dims, pol.max_shift),
                         snap.config, p.proposed_config, "x", margin=self.d.t.max_shift_margin)
        if not sv.permitted:
            out += [f"policy: {x}" for x in sv.problems(pol.max_shift)]
        return out

    def _draft(self, comp: Composition, now: int, route: str, package: RequestPackage | None,
               rationale: str) -> Proposal:
        """The proposal row, with the exact request body it will send (so a resend after a
        crash sends the same bytes) and its velocity entries (counted once applied)."""
        wf = comp.wf
        pid = make_proposal_id(self.d.run6, wf, now, comp.cls, self.d.store.next_seq(wf))
        prev = sorted({p.proposal_id for p in comp.patches if p.proposal_id})
        stored = comp.proposed.as_stored()
        pkg = package
        evidence = dict(pkg.evidence) if pkg else {"class": comp.cls}
        velocity = tuple((k, d, float(tv), comp.risk.get((k, d)) == "increasing")
                         for (k, d), tv in sorted(comp.tv_by_cohort_dim.items()) if tv > 0)
        p = Proposal(
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
            decision_id=min((p.decision_id for p in comp.patches), default=None),
            velocity=velocity)
        body = self._request_body(p) if route == "approval" else self._auto_body(p)
        return replace(p, request=freeze(body))

    def _insert(self, draft: Proposal, comp: Composition, now: int, reason: str) -> Proposal:
        with self.d.store.atomic():
            p = self.d.store.insert(draft, now, reason)
            self.d.overlay.mark_proposed([x.patch_id for x in comp.patches], p.proposal_id)
        return p

    def _move(self, pid: str, event: str, now: int, reason: str, *, overlay: str | None = None,
              until: int | None = None, **fields: Any) -> Proposal:
        """One transition and its overlay consequence in one transaction."""
        with self.d.store.atomic():
            p = self.d.store.apply(pid, event, now, reason, **fields)
            if overlay == "open":
                self.d.overlay.return_to_open(pid)
            elif overlay == "hold":
                self.d.overlay.hold_proposal(pid, until if until is not None
                                             else now + self.d.t.held_patch_release_min)
            elif overlay == "drop":
                self.d.overlay.drop(pid)
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
        with self.d.store.atomic():
            p = self.d.store.supersede(holder.proposal_id, draft, now,
                                       f"class {by.cls} change takes precedence")
            self.d.overlay.return_to_open(holder.proposal_id)
            self.d.overlay.mark_proposed([x.patch_id for x in by.patches], p.proposal_id)
        if route == "approval":
            return self._send_approval(p, now)
        return self._send_auto(p, now)

    def _discard(self, comp: Composition, now: int) -> Proposal:
        draft = self._draft(comp, now, "approval", None, "no net change")
        with self.d.store.atomic():
            p = self._insert(draft, comp, now, "drafted")
            return self._move(p.proposal_id, "no_change", now, "composition has no net change",
                              overlay="drop")

    def _request_body(self, p: Proposal) -> dict[str, Any]:
        return {"proposal_id": p.proposal_id, "workflow_id": p.wf,
                "base_version": p.base_version, "proposed_config": p.proposed_config.body(),
                "change_summary": p.change_summary, "diff": thaw(p.diff) or {"note": "-"},
                "affected_leads": int(p.affected_leads),
                "dry_run": thaw(p.dry_run) or {"method": "not estimated"},
                "impact_score": float(p.impact_score),
                "rationale": p.rationale or p.change_summary,
                "evidence": thaw(p.evidence)}

    def _auto_body(self, p: Proposal) -> dict[str, Any]:
        return AutoApplyRequest(proposal_id=p.proposal_id, base_version=p.base_version,
                                proposed_config=p.proposed_config,
                                rationale=p.rationale or p.change_summary or "controller change",
                                evidence=p.evidence).body()

    def _payload(self, p: Proposal) -> dict[str, Any]:
        if p.route == "approval":
            body = thaw(p.request) or self._request_body(p)
        else:
            body = thaw(p.request) or self._auto_body(p)
        return {"workflow_id": p.wf, "proposal_id": p.proposal_id, "body": body}

    def _send_approval(self, p: Proposal, now: int) -> Proposal:
        pid = p.proposal_id
        payload = self._payload(p)
        decision = self._decision(p.wf, "submit_approval", "6.1/submit",
                                  {"proposal_id": pid, "class": p.cls})
        before = None
        if p.state == "draft":
            def before() -> None:
                self.d.store.apply(pid, "route_approval", now, "gates passed")
        try:
            resp = self.d.executor.execute("submit_approval", pid, payload, decision,
                                           before=before)
        except E.DuplicateProposalId:
            found = [x for x in self.d.sim.approvals(p.wf) if x.proposal_id == pid]
            if not found:
                self.d.on_alert("GOV_INVARIANT", p.wf, f"{pid}: duplicate id, no record")
                raise
            self.on_effect_done("submit_approval", payload, found[0])
            return self._after_submit(pid, found[0], now)
        except E.ApiError as e:
            self.on_effect_failed("submit_approval", payload, e)
            return self._get(pid)
        except E.SimTransportError:
            return self._get(pid)      # reconciled by proposal_id (8.3): PENDING or DRAFT
        a = _approval(resp)
        self.on_effect_done("submit_approval", payload, a)
        return self._after_submit(pid, a, now)

    def _after_submit(self, pid: str, a: ApprovalPublic, now: int) -> Proposal:
        p = self._get(pid)
        if p.state == "pending" and a.status != "pending":
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
        pol = self.d.policy_of(comp.wf)
        if needs_notice(pol, comp) and (notice_until is None or now < notice_until):
            until = notice_until if notice_until is not None else now + pol.notice_minutes
            with self.d.store.atomic():
                p = self._insert(draft, comp, now, "drafted")
                return self.d.store.apply(p.proposal_id, "route_notice", now,
                                          f"notice until {until}", notice_until_minute=until)
        p = self._insert(draft, comp, now, "drafted")
        return self._send_auto(p, now)

    def _send_auto(self, p: Proposal, now: int) -> Proposal:
        pid = p.proposal_id
        payload = self._payload(p)
        decision = self._decision(p.wf, "auto_apply", "6.5/auto", {"proposal_id": pid})
        event = {"draft": ("route_auto", "auto-apply permitted"),
                 "noticed": ("notice_elapsed", "notice period elapsed")}.get(p.state)
        before = None
        if event is not None:
            def before() -> None:
                self.d.store.apply(pid, event[0], now, event[1])
        try:
            res = self.d.executor.execute("auto_apply", pid, payload, decision, before=before)
        except E.DuplicateProposalId:
            hist = [v for v in self.d.sim.config_history(p.wf) if v.proposal_id == pid]
            if not hist:
                self.d.on_alert("GOV_INVARIANT", p.wf, f"{pid}: duplicate id, no version")
                raise
            self.on_effect_done("auto_apply", payload,
                                ApplyResult(p.wf, hist[0].version, tuple(hist[0].changed_dims)))
            return self._get(pid)
        except E.ApiError as e:
            self.on_effect_failed("auto_apply", payload, e)
            return self._get(pid)
        except E.SimTransportError:
            return self._get(pid)      # reconciled via config history (8.3): APPLIED or DRAFT
        self.on_effect_done("auto_apply", payload, res)
        return self._get(pid)

    def _applied(self, p: Proposal, version: int, now: int, event: str) -> Proposal:
        with self.d.store.atomic():
            p = self.d.store.apply(p.proposal_id, event, now, f"version {version}",
                                   applied_version=version, applied_minute=now)
            self.d.overlay.mark_applied(p.proposal_id)
            self.d.on_applied(p, version)
            return self.d.store.apply(p.proposal_id, "verify_start", now,
                                      "verification window opens")

    # ------------------------------------------------------------ effect transitions (8.2, 8.3)
    def on_effect_done(self, kind: str, request: Mapping[str, Any], response: Any
                       ) -> Proposal | None:
        """The call took effect: SUBMITTING -> PENDING, AUTO_APPLYING or APPLYING -> APPLIED.
        Idempotent: a proposal that already moved is returned unchanged."""
        p = self.d.store.get(str(request["proposal_id"]))
        if p is None:
            return None
        now = self.d.clock()
        if kind == "submit_approval" and p.state == "submitting":
            a = _approval(response)
            with self.d.store.atomic():
                self.d.store.mirror(a, self.d.epoch, now)
                return self.d.store.apply(p.proposal_id, "created", now, a.approval_id,
                                          approval_id=a.approval_id,
                                          submitted_minute=self.d.epoch.minute(a.created_at))
        if (kind, p.state) in (("auto_apply", "auto_applying"), ("apply", "applying")):
            return self._applied(p, _apply_result(response).version, now, "applied")
        return p

    def on_effect_failed(self, kind: str, request: Mapping[str, Any], err: E.ApiError
                         ) -> Proposal | None:
        """A definite error response (no effect happened). Duplicate ids and an apply that finds
        the approval already applied need a lookup and are resolved by the caller (or by
        `recover`); every other error has its transition here. Idempotent."""
        p = self.d.store.get(str(request["proposal_id"]))
        if p is None:
            return None
        now, pid, wf = self.d.clock(), p.proposal_id, p.wf
        call_state = {"submit_approval": "submitting", "auto_apply": "auto_applying",
                      "apply": "applying"}[kind]
        if p.state != call_state:
            return p
        if isinstance(err, E.StaleBaseVersion):
            if kind == "apply":
                self.d.on_alert("GOV_INVARIANT", wf, f"{pid}: apply conflict")
            return self._move(pid, "stale", now, f"current {err.current_version}",
                              overlay="open")
        if isinstance(err, E.AutoApplyNotPermitted) and kind == "auto_apply":
            self.auto_blocked[wf] = f"403 on {pid}: {err.problems}"
            self.d.on_alert("AUTO_APPLY_REJECTED", wf, f"{pid}: {err.problems}")
            return self._move(pid, "forbidden", now, "; ".join(err.problems), overlay="open")
        if isinstance(err, E.Unprocessable) and kind != "apply":
            self.d.on_alert("GOV_INVARIANT", wf, f"{pid}: 422 {err.code}")
            return self._move(pid, "invalid", now, str(err.detail), overlay="hold")
        if isinstance(err, E.ApprovalLapsed) and kind == "apply":
            self.d.on_alert("APPLY_AT_RISK", wf, f"{pid} lapsed (410)")
            return self._move(pid, "lapsed", now, "410 at the simulator", overlay="open")
        return p

    def on_effect_not_run(self, kind: str, request: Mapping[str, Any],
                          info: Mapping[str, Any]) -> Proposal | None:
        """Reconciliation found that the call never took effect. A submit or auto-apply goes
        back to DRAFT (resent or aborted by `resume_unsent`); an apply goes back to APPROVED,
        where the pre-apply checks decide again, unless the approval has lapsed meanwhile."""
        p = self.d.store.get(str(request["proposal_id"]))
        if p is None:
            return None
        now = self.d.clock()
        if (kind, p.state) in (("submit_approval", "submitting"),
                               ("auto_apply", "auto_applying")):
            return self._move(p.proposal_id, "not_sent", now, "the request never took effect")
        if kind == "apply" and p.state == "applying":
            status = info.get("approval_status")
            if status == "approved":
                return self._move(p.proposal_id, "not_applied", now, "the apply did not run")
            if status == "lapsed":
                self.d.on_alert("APPLY_AT_RISK", p.wf, f"{p.proposal_id} lapsed")
                return self._move(p.proposal_id, "lapsed", now, "lapsed at the simulator",
                                  overlay="open")
        return p

    # ------------------------------------------------------------ crash recovery (8.3, sweep)
    def recover(self, now: int) -> list[Proposal]:
        """Restart step 5 and the start of every LIFECYCLE stage: resolve proposals a crash
        left in a call state with no open intent, then resend or abort unsent drafts."""
        return self.sweep(now) + self.resume_unsent(now)

    def sweep(self, now: int) -> list[Proposal]:
        out = []
        for p in self.d.store.in_states(("submitting", "auto_applying", "applying")):
            payload = self._payload(p)
            if p.state == "submitting":
                found = [a for a in self.d.sim.approvals(p.wf) if a.proposal_id == p.proposal_id]
                if found:
                    self.on_effect_done("submit_approval", payload, found[0])
                    q = self._after_submit(p.proposal_id, found[0], now)
                else:
                    q = self.on_effect_not_run("submit_approval", payload, {})
            elif p.state == "auto_applying":
                hist = [v for v in self.d.sim.config_history(p.wf)
                        if v.proposal_id == p.proposal_id]
                q = (self.on_effect_done("auto_apply", payload, ApplyResult(
                         p.wf, hist[0].version, tuple(hist[0].changed_dims))) if hist
                     else self.on_effect_not_run("auto_apply", payload, {}))
            else:
                payload = {"workflow_id": p.wf, "approval_id": p.approval_id,
                           "proposal_id": p.proposal_id}
                a = self.d.sim.approval(p.approval_id)
                hist = [v for v in self.d.sim.config_history(p.wf)
                        if v.approval_id == p.approval_id]
                q = (self.on_effect_done("apply", payload, ApplyResult(
                         p.wf, hist[0].version, tuple(hist[0].changed_dims)))
                     if a.status == "applied" and hist
                     else self.on_effect_not_run("apply", payload, {"approval_status": a.status}))
            if q is not None:
                out.append(q)
        return out

    def resume_unsent(self, now: int) -> list[Proposal]:
        """Every DRAFT is a request that never reached the simulator. Resend it with the same id
        and body if every precondition still holds (8.3: "all still hold: resend the same id and
        payload"); otherwise abort it and requeue its patches for a fresh composition."""
        out = []
        for p in self.d.store.in_states(("draft",)):
            if p.route == "approval":
                reasons = self._blockers(p.wf, p.cls, p.base_version, now)
            else:
                reasons = self._auto_blockers(p, now)
            if reasons:
                out.append(self._move(p.proposal_id, "abort", now,
                                      "not resent: " + "; ".join(reasons), overlay="open"))
            elif p.route == "approval":
                out.append(self._send_approval(p, now))
            else:
                out.append(self._send_auto(p, now))
        return out

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
                with self.d.store.atomic():
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
            self.d.on_alert("APPROVAL_EXPIRED", p.wf, f"{p.proposal_id} expired")
            return self._move(p.proposal_id, "expired", now, "expired (re-queued fresh)",
                              overlay="open", decided_minute=decided)
        plan = rejection_plan(p.cls, a.reasons, self._prior_judgements(p))
        if plan.page:
            self.d.on_alert(plan.page, p.wf, f"{p.proposal_id} rejected: {list(a.reasons)}")
        overlay = {"hold": "hold", "drop_needs_discussion": "drop"}.get(plan.action, "open")
        return self._move(p.proposal_id, "rejected", now,
                          f"{plan.kind}: {'; '.join(a.reasons)} -> {plan.action}",
                          overlay=overlay, decided_minute=decided)

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
            if event == "lapse_risk":
                self.d.on_alert("APPLY_AT_RISK", p.wf, f"{p.proposal_id} not applied in time")
            return self._move(p.proposal_id, event, now,
                              f"current version {current}, apply_by {p.apply_by_minute}",
                              overlay="open")
        pid, aid = p.proposal_id, p.approval_id
        payload = {"workflow_id": p.wf, "approval_id": aid, "proposal_id": pid}
        decision = self._decision(p.wf, "apply", "6.1/apply",
                                  {"proposal_id": pid, "approval_id": aid})

        def before() -> None:
            self.d.store.apply(pid, "apply_start", now, "pre-apply checks passed")
        try:
            res = self.d.executor.execute("apply", aid, payload, decision, before=before)
        except E.NotApproved as e:
            if e.approval_status != "applied":
                raise
            hist = [v for v in self.d.sim.config_history(p.wf) if v.approval_id == aid]
            version = hist[0].version if hist else current + 1
            self.on_effect_done("apply", payload, ApplyResult(
                p.wf, version, tuple(hist[0].changed_dims) if hist else ()))
            return self._get(pid)
        except E.ApiError as e:
            self.on_effect_failed("apply", payload, e)
            return self._get(pid)
        except E.SimTransportError:
            return self._get(pid)      # retry-safe by approval_id (8.1): APPLIED or APPROVED
        self.on_effect_done("apply", payload, res)
        return self._get(pid)

    def _tick_noticed(self, p: Proposal, now: int) -> Proposal:
        if p.notice_until_minute is None or now < p.notice_until_minute:
            return p
        held = p.cls != P0_INCIDENT and any(h.active(now) for h in self.d.holds(p.wf))
        current = self.d.sim.config(p.wf).version
        if current != p.base_version:
            return self._move(p.proposal_id, "stale", now, f"current version {current}",
                              overlay="open")
        if held or (self.d.regate is not None and not self.d.regate(p, "notice")):
            return self._move(p.proposal_id, "hold", now,
                              "hold set" if held else "harm gate fails at notice expiry",
                              overlay="open")
        if p.wf in self.auto_blocked:
            return self._move(p.proposal_id, "hold", now, self.auto_blocked[p.wf],
                              overlay="open")
        pol = self.d.policy_of(p.wf)      # the policy may have been re-read during the notice
        live = self.d.sim.config(p.wf).config
        sv = sim_verdict(AutoApplyPolicy(pol.auto_enabled, pol.allowed_dims, pol.max_shift),
                         live, p.proposed_config, "x", margin=self.d.t.max_shift_margin)
        if not sv.permitted:
            return self._move(p.proposal_id, "hold", now, "policy no longer permits: "
                              + "; ".join(sv.problems(pol.max_shift)), overlay="open")
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
            event = "verified" if r.status == "verified" else "verify_failed"
            with self.d.store.atomic():      # the verdict, its page and the rollback patch: one fact
                if r.page:
                    self.d.on_alert(r.page, p.wf, f"{p.proposal_id}: {'; '.join(r.reasons)}")
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
                out.append(self._move(p.proposal_id, "stale", now,
                                      f"config changed to v{version} (supervisor)",
                                      overlay="open"))
        return out

    def policy_reread(self, wf: str) -> None:
        """A fresh policy read clears the post-403 auto-apply block (6.5)."""
        self.auto_blocked.pop(wf, None)
