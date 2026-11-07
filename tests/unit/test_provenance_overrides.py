"""Which proposal is named as the source of each changed (cohort, dim) in allocation_provenance."""
from controller.loop.governance import _provenance_overrides
from controller.types import Proposal


def _p(cls: int, items: list[dict]) -> Proposal:
    return Proposal(proposal_id="pid", wf="w", cls=cls, route="approval", state="applied",
                    base_version=1, approval_id="apr_1", decision_id=7, diff={"items": items})


def test_routine_allocation_items_are_arms_or_probes() -> None:
    out = _provenance_overrides(_p(3, [{"path": ["alloc", "c1", "voice"]},
                                       {"path": ["alloc", "c1", "caller_id"]},
                                       {"path": ["hours", "c1"]}]))
    assert out[("c1", "voice")]["source"] == "arm"
    assert out[("c1", "caller_id")]["source"] == "probe"
    assert out[("c1", "voice")]["proposal_id"] == "pid" and out[("c1", "voice")]["decision_id"] == 7
    assert len(out) == 2


def test_promotion_rollback_and_prompt() -> None:
    assert _provenance_overrides(_p(2, [{"path": ["alloc", "c", "voice"]}]))[("c", "voice")]["source"] == "winner"
    assert _provenance_overrides(_p(1, [{"path": ["alloc", "c", "voice"]}]))[("c", "voice")]["source"] == "rollback"
    assert ("default", "prompt_version") in _provenance_overrides(_p(0, [{"path": ["prompt_version"]}]))
