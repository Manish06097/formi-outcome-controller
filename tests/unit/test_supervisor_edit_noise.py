"""ENG-GAP: the simulator's `changed_dims` compares shares exactly, and it re-normalises every
allocation it stores, so a supervisor edit of the default voice came back listing
`welcome_message` too (a cohort's share 0.6253 stored as 0.6253000000000001). That interrupted the
welcome experiment on public_02. The mirror now keeps only the dims that really changed."""
from __future__ import annotations

from controller.types import CohortDef
from controller.world.configs import changed_paths, noise_free_dims
from tests.support.t3_world import base_config, make_spec

SPEC = make_spec()
COHORT = CohortDef.make("c0~000000", {"lang": ["ta"]})


def _cfg(w_cohort: dict[str, float], v_default: dict[str, float]):
    c = base_config(SPEC, [COHORT]).with_allocation("c0~000000", "welcome_message", w_cohort)
    return c.with_allocation("default", "voice", v_default)


def test_a_one_ulp_difference_in_an_untouched_dim_is_not_a_change():
    old = _cfg({"W1": 0.6253, "W2": 0.2404, "W3": 0.1343}, {"V1": 1.0})
    new = _cfg({"W1": 0.6253000000000001, "W2": 0.2404, "W3": 0.13430000000000003}, {"V2": 1.0})
    assert old.changed_dims(new) == ["welcome_message", "voice"]       # what the simulator says
    assert noise_free_dims(old, new, old.changed_dims(new)) == ("voice",)
    assert [p for p in changed_paths(old, new)] == [("alloc", "default", "voice")]


def test_a_real_change_is_kept_and_an_unknown_body_keeps_everything():
    old = _cfg({"W1": 0.6, "W2": 0.4}, {"V1": 1.0})
    new = _cfg({"W1": 0.5, "W2": 0.5}, {"V1": 1.0})
    assert noise_free_dims(old, new, ["welcome_message"]) == ("welcome_message",)
    assert noise_free_dims(None, new, ["welcome_message", "voice"]) == ("welcome_message", "voice")
