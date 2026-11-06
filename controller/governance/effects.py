"""Governance side effects through the write-ahead intent executor.

Owner: integration step I2 (milestone M3). Implements DESIGN.md 8.2 and 8.3 for the three
governance kinds (`submit_approval`, `auto_apply`, `apply`): the lifecycle's `Executor` port
over T1's `IntentExecutor`, and the registry hooks that make each proposal transition commit in
the same transaction as the intent row it belongs to. ADR 0029.

For one call:

    T1   decision row, intent row (started), the proposal's "before" transition
         (DRAFT -> SUBMITTING, DRAFT or NOTICED -> AUTO_APPLYING, APPROVED -> APPLYING)
    --   HTTP
    T2   intent done + `lifecycle.on_effect_done` (SUBMITTING -> PENDING, ... -> APPLIED and the
         config mirror records our body); or intent failed + `on_effect_failed` (stale,
         invalid, forbidden, lapsed)

After a crash, the reconciler runs before any new side effect and calls the same hooks inside
its own transaction: `done` when the lookup (approvals by proposal id, config history, the
approval's status) finds the effect, `aborted` (-> `on_effect_not_run`, back to DRAFT or
APPROVED) when it does not. A transport error during the call is reconciled the same way at
once; if the call did not take effect, `EffectNotTaken` (a `SimTransportError`) tells the
lifecycle to leave the proposal where the reconciliation put it.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from controller.governance.lifecycle import EFFECT_KINDS, ProposalLifecycle
from controller.governance.store_pg import TxScope
from controller.reliability.intents import IntentAborted, IntentExecutor, IntentRegistry
from controller.simclient.errors import ApiError, SimTransportError
from controller.types import Decision, Intent, Tx


class EffectNotTaken(SimTransportError):
    """The call failed in transit and reconciliation found it never took effect."""


class IntentEffects:
    """The lifecycle's `Executor` in production: every call is a write-ahead intent."""

    def __init__(self, intents: IntentExecutor, scope: TxScope):
        self.intents = intents
        self.scope = scope

    def execute(self, kind: str, key: str, payload: dict[str, Any], decision: Decision, *,
                before: Callable[[], None] | None = None) -> Any:
        hook = None
        if before is not None:
            def hook(tx: Tx) -> None:
                with self.scope.bind(tx):
                    before()
        try:
            return self.intents.execute(kind, key, payload, decision, before=hook)
        except IntentAborted as e:
            raise EffectNotTaken(f"{kind} {key}: {e.resolution}") from e


def attach_effect_hooks(registry: IntentRegistry, lifecycle: Callable[[], ProposalLifecycle],
                        scope: TxScope) -> None:
    """Register the domain transitions of the governance kinds (`IntentRegistry.attach`)."""
    for kind in EFFECT_KINDS:
        registry.attach(kind, **_hooks(kind, lifecycle, scope))


def _hooks(kind: str, lifecycle: Callable[[], ProposalLifecycle], scope: TxScope
           ) -> dict[str, Callable[..., None]]:
    def done(tx: Tx, intent: Intent, response: Mapping[str, Any]) -> None:
        with scope.bind(tx):
            lifecycle().on_effect_done(kind, intent.request, response)

    def failed(tx: Tx, intent: Intent, err: ApiError) -> None:
        with scope.bind(tx):
            lifecycle().on_effect_failed(kind, intent.request, err)

    def aborted(tx: Tx, intent: Intent, info: Mapping[str, Any] | None = None) -> None:
        with scope.bind(tx):
            lifecycle().on_effect_not_run(kind, intent.request, info or {})

    return {"done": done, "failed": failed, "aborted": aborted}


__all__ = ["IntentEffects", "EffectNotTaken", "attach_effect_hooks"]
