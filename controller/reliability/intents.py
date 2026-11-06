"""Write-ahead intents: every simulator side effect is recorded before it happens and resolved
deterministically after a crash.

Owner: track T1 (milestone M2). Implements DESIGN.md 8.1 (keys), 8.2 (the protocol) and 8.3
(reconciliation by kind), and the `IntentExecutor` and `Reconciler` interfaces of 11.2.

Protocol for one side effect (`IntentExecutor.execute`):

    T1  one transaction: decision row (outcome "intended"), intent row (status started, the
        idempotency key and the exact request), the kind's "before" transition, an audit row
    --  the HTTP call, never blindly retried
    T2  one transaction: intent done with the response, the kind's domain transition, the
        decision's outcome, an audit row

A crash between T1 and T2 leaves a `started` intent. `Reconciler.run` resolves every one of them
before the process makes any new side effect: it asks the simulator what happened (advance: the
clock; approvals: the listing by proposal id; auto-apply: config history; apply: the approval's
status; credit-spending calls: the credit balance) and then either completes T2 with what the
simulator says, or aborts the intent (it never ran) so the caller re-plans. A transport timeout
during the call is resolved the same way, immediately.

Kinds are pluggable (`KindHandler`). This module ships the HTTP call and the reconciliation
lookup of every kind in 8.3; the owning tracks attach their domain transitions (`before`, `done`,
`failed`, `aborted`) with `IntentRegistry.attach`.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from controller.audit.writer import record, set_outcome, write_decision
from controller.obs.alerts import raise_alert
from controller.reliability.faults import maybe_crash
from controller.simclient.client import SimClient
from controller.simclient.errors import ApiError, NotFound, SimTransportError
from controller.store import repos
from controller.store.db import Database
from controller.types import (Decision, FrozenDict, Intent, IntentResolution, ReconcileReport,
                              Tx, freeze, thaw)

log = logging.getLogger(__name__)


def to_json(obj: Any) -> Any:
    """A JSON-able form of a simulator response (typed dataclasses use the API's key names)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return thaw({f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)})
    if isinstance(obj, (list, tuple)):
        return [to_json(x) for x in obj]
    if isinstance(obj, Mapping):
        return {k: to_json(v) for k, v in obj.items()}
    return obj


@dataclass(frozen=True)
class Lookup:
    """What the simulator says happened to a started intent."""

    outcome: str
    """done (the call took effect; `response` is its result) | not_run (it did not take effect;
    safe to re-plan) | lost (it took effect but its result is gone: a validation or LLM call
    whose credits were spent) | unknown (contradicts the contract: page)"""
    response: Mapping[str, Any] = field(default_factory=dict)


Hook = Callable[..., None]


@dataclass
class KindHandler:
    kind: str
    call: Callable[[SimClient, Mapping[str, Any]], Any]
    lookup: Callable[[SimClient, Intent], Lookup]
    before: Hook | None = None
    """(tx, intent): in T1, e.g. proposal DRAFT -> SUBMITTING"""
    done: Hook | None = None
    """(tx, intent, response): in T2 or after reconciliation"""
    failed: Hook | None = None
    """(tx, intent, ApiError): a definite error response (no effect happened)"""
    aborted: Hook | None = None
    """(tx, intent): reconciliation found the call never took effect"""
    lost: Hook | None = None
    """(tx, intent, response): the effect happened but its result is lost"""
    required: tuple[str, ...] = ()
    """Request fields the lookup needs after a crash; `execute` refuses a request without them
    before writing anything (ADR 0028: credit-spending kinds carry credits_before and cost)."""


class IntentRegistry:
    def __init__(self) -> None:
        self._h: dict[str, KindHandler] = {}

    def register(self, h: KindHandler) -> None:
        self._h[h.kind] = h

    def attach(self, kind: str, **hooks: Hook) -> None:
        """Attach domain transitions (before, done, failed, aborted, lost) to a kind."""
        h = self._h[kind]
        for name, fn in hooks.items():
            if name not in ("before", "done", "failed", "aborted", "lost"):
                raise ValueError(f"unknown hook {name}")
            setattr(h, name, fn)

    def get(self, kind: str) -> KindHandler | None:
        return self._h.get(kind)


class IntentConflict(RuntimeError):
    """An intent with this key is still started: it must be reconciled first."""


class IntentExecutor:
    """`IntentExecutor` of DESIGN.md 11.2: the only component that performs side effects."""

    def __init__(self, db: Database, client: SimClient, registry: IntentRegistry,
                 audit_actor: str = "controller"):
        self.db = db
        self.client = client
        self.registry = registry
        self.actor = audit_actor
        self.reconciler = Reconciler(db, client, registry)

    def execute(self, kind: str, key: str, payload: Mapping[str, Any], decision: Decision) -> Any:
        """Run one side effect under the write-ahead protocol. Returns the response JSON, or
        raises the simulator's ApiError after recording it. On a transport failure the intent
        is reconciled at once; if the call did not take effect, `IntentAborted` is raised."""
        h = self._handler(kind)
        missing = [f for f in h.required if payload.get(f) is None]
        if missing:
            raise ValueError(f"{kind} request {key} lacks {missing}: the reconciler cannot "
                             "resolve it after a crash without them")
        maybe_crash(kind, "before_t1")
        with self.db.tx() as tx:
            prior = repos.intent_by_key(tx, key)
            if prior is not None and prior.status == "done":
                return thaw(prior.response)
            if prior is not None and prior.status == "started":
                raise IntentConflict(f"intent {key} is started; reconcile before re-executing")
            did = write_decision(tx, decision)
            iid = repos.insert_intent(tx, kind, key, payload, did,
                                      decision.sim_minute if decision.sim_minute is not None
                                      else repos.get_clock(tx).sim_minute)
            intent = Intent(iid, kind, key, freeze(dict(payload)), "started", did,
                            decision.sim_minute or 0)
            if h.before:
                h.before(tx, intent)
            record(tx, wf=decision.wf, actor=self.actor, action="intent_started",
                   subject=("intent", key), summary=f"{kind} {key}",
                   details={"intent_id": iid}, caused_by=did)
        maybe_crash(kind, "after_t1")
        try:
            result = to_json(h.call(self.client, payload))
        except SimTransportError as e:
            log.warning("%s %s: %s; reconciling", kind, key, e)
            res = self.reconciler.reconcile_one(intent)
            if res.resolution == "done":
                with self.db.tx() as tx:
                    row = repos.intent_by_key(tx, key)
                return thaw(row.response) if row and row.response is not None else {}
            raise IntentAborted(kind, key, res.resolution) from e
        except ApiError as e:
            with self.db.tx() as tx:
                repos.finish_intent(tx, iid, "failed", {"status": e.status, "code": e.code,
                                                         "detail": _jsonable(e.detail)})
                if h.failed:
                    h.failed(tx, intent, e)
                set_outcome(tx, did, {"status": "failed", "code": e.code, "http": e.status})
                record(tx, wf=decision.wf, actor=self.actor, action="intent_failed",
                       subject=("intent", key), summary=f"{kind} {key}: {e.status} {e.code}",
                       details={"intent_id": iid}, caused_by=did)
            raise
        maybe_crash(kind, "after_http")
        self._complete(intent, result, decision.wf)
        return result

    def _complete(self, intent: Intent, result: Mapping[str, Any], wf: str | None,
                  via: str = "response") -> None:
        h = self._handler(intent.kind)
        with self.db.tx() as tx:
            repos.finish_intent(tx, intent.intent_id, "done", result)
            if h.done:
                h.done(tx, intent, result)
            if intent.decision_id is not None:
                set_outcome(tx, intent.decision_id, {"status": "done", "via": via})
            record(tx, wf=wf, actor=self.actor, action="intent_done",
                   subject=("intent", intent.idem_key), summary=f"{intent.kind} done ({via})",
                   details={"intent_id": intent.intent_id}, caused_by=intent.decision_id)
            maybe_crash(intent.kind, "in_t2")

    def _handler(self, kind: str) -> KindHandler:
        h = self.registry.get(kind)
        if h is None:
            raise KeyError(f"no intent handler for kind {kind!r}")
        return h


class IntentAborted(RuntimeError):
    """The side effect did not happen (reconciled as not_run); the caller re-plans."""

    def __init__(self, kind: str, key: str, resolution: str):
        super().__init__(f"{kind} {key}: {resolution}")
        self.kind, self.key, self.resolution = kind, key, resolution


def _jsonable(v: Any) -> Any:
    try:
        import json
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


class Reconciler:
    """`Reconciler` of DESIGN.md 11.2: resolves every started intent (8.3)."""

    def __init__(self, db: Database, client: SimClient, registry: IntentRegistry):
        self.db = db
        self.client = client
        self.registry = registry

    def run(self) -> ReconcileReport:
        with self.db.tx() as tx:
            started = repos.open_intents(tx)
        resolved = [self.reconcile_one(i) for i in started]
        alerts = tuple(f"{r.kind} {r.idem_key}: {r.resolution}" for r in resolved
                       if r.resolution == "unknown")
        return ReconcileReport(resolved=tuple(resolved), alerts=alerts)

    def reconcile_one(self, intent: Intent) -> IntentResolution:
        h = self.registry.get(intent.kind)
        look = h.lookup(self.client, intent) if h else Lookup("unknown", {"reason": "no handler"})
        with self.db.tx() as tx:
            row = repos.intent_by_key(tx, intent.idem_key)
            if row is None or row.status != "started":   # resolved by someone else meanwhile
                return IntentResolution(intent.intent_id, intent.kind, intent.idem_key,
                                        row.status if row else "missing")
            if look.outcome == "done":
                repos.finish_intent(tx, intent.intent_id, "done", look.response)
                if h and h.done:
                    h.done(tx, intent, look.response)
            elif look.outcome == "not_run":
                abort_intent(tx, intent)
                if h and h.aborted:
                    h.aborted(tx, intent)
            elif look.outcome == "lost":
                repos.finish_intent(tx, intent.intent_id, "done", {**look.response, "lost": True})
                if h and h.lost:
                    h.lost(tx, intent, look.response)
            else:
                repos.finish_intent(tx, intent.intent_id, "unknown", look.response)
                raise_alert(tx, "INTENT_UNKNOWN", None, intent.idem_key,
                            f"{intent.kind} {intent.idem_key}: the simulator contradicts "
                            f"our record ({dict(look.response)})")
            if intent.decision_id is not None:
                set_outcome(tx, intent.decision_id,
                            {"status": look.outcome, "via": "reconcile"})
            record(tx, wf=_wf_of(intent), actor="reconciler", action="intent_reconciled",
                   subject=("intent", intent.idem_key),
                   summary=f"{intent.kind} reconciled: {look.outcome}",
                   details={"intent_id": intent.intent_id, "lookup": thaw(look.response)},
                   caused_by=intent.decision_id)
        log.warning("reconciled %s %s: %s", intent.kind, intent.idem_key, look.outcome)
        resolution = {"not_run": "aborted", "lost": "lost"}.get(look.outcome, look.outcome)
        return IntentResolution(intent.intent_id, intent.kind, intent.idem_key, resolution,
                                freeze(dict(look.response)))


def abort_intent(tx: Tx, intent: Intent) -> None:
    """Mark an intent aborted and free its idempotency key, so the deterministic re-plan can
    use the same key (`adv:<from>-><to>` is planned again identically, DESIGN.md 8.3)."""
    tx.execute("""UPDATE intent SET status = 'aborted', finished_wall = now(),
                      idem_key = idem_key || '#aborted' || intent_id::text
                  WHERE intent_id = %s""", (intent.intent_id,))


def _wf_of(intent: Intent) -> str | None:
    wf = intent.request.get("workflow_id") if intent.request else None
    return str(wf) if wf else None


# =============================================================================== the kinds of 8.3

def _advance_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return c.advance(int(p["minutes"]), timeout_s=p.get("timeout_s"))


def _advance_lookup(c: SimClient, i: Intent) -> Lookup:
    """`GET /sim/state` waits on the simulator lock, so it answers after a running advance."""
    st = c.state()
    frm, to = int(i.request["from_minute"]), int(i.request["to_minute"])
    if st.sim_minute == frm:
        return Lookup("not_run", {"sim_minute": st.sim_minute})
    if st.sim_minute == min(to, st.end_minute):
        return Lookup("done", {"sim_minute": st.sim_minute, "ts": st.ts,
                               "finished": st.finished, "new_chunks": [],
                               "reconciled": True})
    return Lookup("unknown", {"sim_minute": st.sim_minute, "expected": [frm, to]})


def _submit_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return c.request_approval(dict(p["body"]))


def _submit_lookup(c: SimClient, i: Intent) -> Lookup:
    body = i.request["body"]
    for a in c.approvals(str(body["workflow_id"])):
        if a.proposal_id == body["proposal_id"]:
            return Lookup("done", to_json(a))
    return Lookup("not_run", {})


def _auto_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return c.auto_apply(str(p["workflow_id"]), dict(p["body"]))


def _auto_lookup(c: SimClient, i: Intent) -> Lookup:
    wf, pid = str(i.request["workflow_id"]), i.request["body"]["proposal_id"]
    for v in c.config_history(wf):
        if v.proposal_id == pid:
            return Lookup("done", {"workflow_id": wf, "version": v.version,
                                   "changed_dims": list(v.changed_dims)})
    return Lookup("not_run", {})


def _apply_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return c.apply(str(p["workflow_id"]), str(p["approval_id"]))


def _apply_lookup(c: SimClient, i: Intent) -> Lookup:
    wf, aid = str(i.request["workflow_id"]), str(i.request["approval_id"])
    try:
        a = c.approval(aid)
    except NotFound:
        return Lookup("unknown", {"approval_id": aid, "reason": "not found"})
    if a.status == "applied":
        for v in c.config_history(wf):
            if v.approval_id == aid:
                return Lookup("done", {"workflow_id": wf, "version": v.version,
                                       "changed_dims": list(v.changed_dims)})
        return Lookup("unknown", {"approval_id": aid, "reason": "applied but not in history"})
    # approved (re-apply is the lifecycle's decision), lapsed, rejected...: the apply did not
    # take effect; the owning track's `aborted` hook decides between re-apply and LAPSED_LOCAL.
    return Lookup("not_run", {"approval_status": a.status, "apply_by": a.apply_by})


CREDIT_FIELDS = ("credits_before", "cost")
"""Fields every credit-spending request (validation, suggest_fixes) must carry."""


def _credits_lookup(c: SimClient, i: Intent) -> Lookup:
    """Validation and suggest-fixes have no idempotency key or listing: the request records the
    balance before the call and its cost; a balance that fell by the cost means the call ran
    and its result is lost (`lost_validation_run`, DESIGN.md 8.3)."""
    before = i.request.get("credits_before")
    cost = i.request.get("cost")
    st = c.state()
    if before is None or cost is None:
        return Lookup("unknown", {"credits_remaining": st.credits_remaining,
                                  "reason": "request lacks credits_before or cost"})
    if abs((float(before) - float(cost)) - st.credits_remaining) < 1e-6:
        return Lookup("lost", {"credits_remaining": st.credits_remaining})
    if abs(float(before) - st.credits_remaining) < 1e-6:
        return Lookup("not_run", {"credits_remaining": st.credits_remaining})
    return Lookup("unknown", {"credits_remaining": st.credits_remaining,
                              "expected": [before, float(before) - float(cost)]})


def _validation_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return c.validate(str(p["workflow_id"]), str(p["prompt_version"]), int(p["n"]))


def _suggest_call(c: SimClient, p: Mapping[str, Any]) -> Any:
    return {"suggestions": to_json(c.suggest_fixes(str(p["workflow_id"]), str(p["problem"])))}


def default_registry() -> IntentRegistry:
    """Every kind of DESIGN.md 8.3 with its call and lookup; domain hooks attach later."""
    reg = IntentRegistry()
    reg.register(KindHandler("advance", _advance_call, _advance_lookup))
    reg.register(KindHandler("submit_approval", _submit_call, _submit_lookup))
    reg.register(KindHandler("auto_apply", _auto_call, _auto_lookup))
    reg.register(KindHandler("apply", _apply_call, _apply_lookup))
    reg.register(KindHandler("validation", _validation_call, _credits_lookup,
                             required=CREDIT_FIELDS))
    reg.register(KindHandler("suggest_fixes", _suggest_call, _credits_lookup,
                             required=CREDIT_FIELDS))
    return reg


__all__ = ["IntentExecutor", "Reconciler", "IntentRegistry", "KindHandler", "Lookup",
           "IntentAborted", "IntentConflict", "default_registry", "to_json", "abort_intent",
           "FrozenDict"]
