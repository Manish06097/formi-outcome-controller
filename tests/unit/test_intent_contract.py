"""The write-ahead intent conventions of ADR 0028: a credit-spending request that the reconciler
could not resolve after a crash is refused before anything is written or sent."""
from __future__ import annotations

import pytest

from controller.reliability.intents import CREDIT_FIELDS, IntentExecutor, default_registry
from controller.types import Decision, FrozenDict


class Untouchable:
    """Stands in for the database and the simulator: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"touched {name} before the request was checked")


def _decision() -> Decision:
    return Decision(wf="wf_a", module="incidents.safety", kind="validation", rule_id="gate",
                    inputs=FrozenDict(), outcome=FrozenDict(), harm_report=None,
                    snapshot_ids=(), seed=0, settings_hash="x", code_version="dev")


@pytest.mark.parametrize("kind,payload", [
    ("validation", {"workflow_id": "wf_a", "prompt_version": "P1", "n": 100}),
    ("validation", {"workflow_id": "wf_a", "prompt_version": "P1", "n": 100,
                    "credits_before": 50.0}),
    ("suggest_fixes", {"workflow_id": "wf_a", "problem": "hallucination", "cost": 3}),
])
def test_credit_spending_intents_need_balance_and_cost(kind, payload):
    ex = IntentExecutor(Untouchable(), Untouchable(), default_registry())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lacks"):
        ex.execute(kind, f"{kind}:1", payload, _decision())


def test_only_credit_spending_kinds_declare_the_fields():
    reg = default_registry()
    assert {k for k, h in reg._h.items() if h.required} == {"validation", "suggest_fixes"}
    assert CREDIT_FIELDS == ("credits_before", "cost")
