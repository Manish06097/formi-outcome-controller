"""Hand-built workflow specs and policies for the governance unit tests (track T2).

Every policy is built through the real `world.registry.effective_policy` from an API-shaped
workflow dict, so the tests exercise the same path as the controller.
"""
from __future__ import annotations

import copy
from typing import Any

from controller.settings import Thresholds
from controller.types import Config, WorkflowSpec
from controller.world.directives import load_directives
from controller.world.registry import ExtendedPolicy, effective_policy

BASE_SPEC: dict[str, Any] = {
    "workflow_id": "wf_test", "tenant_id": "acme", "use_case": "renewal reminder",
    "languages": ["hi", "en"], "dialling_window": [9, 21], "dials_per_hour": 300,
    "cadence": [2, 3, 3], "min_gap_minutes": 60,
    "daily_batch": {"size": 1000, "at_hour": 8, "returning_share": 0.3},
    "mid_flight": "allowed",
    "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
    "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2", "V3"],
                "caller_id": ["D1", "D2", "D3"], "prompt_version": ["P1", "P2", "P3"]},
    "prompt_catalog": {"P1": "prod", "P2": "new", "P3": "grounded"},
    "governance": {
        "auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"],
                       "max_shift": 0.2},
        "approver_hours": [10, 19], "approver_latency_minutes": [30, 240],
        "proposal_ttl_hours": 24, "apply_window_hours": 2, "max_requests_per_24h": 3,
        "base_reject_rate": 0.1, "min_validation_pass_rate": 0.97},
    "current_version": 1,
}


def spec(**gov: Any) -> WorkflowSpec:
    """A spec with governance fields overridden (auto_apply keys go under `auto_apply`)."""
    d = copy.deepcopy(BASE_SPEC)
    for k, v in gov.items():
        if k in ("enabled", "allowed_dims", "max_shift"):
            d["governance"]["auto_apply"][k] = v
        elif k in ("tenant_id", "use_case", "workflow_id", "mid_flight", "dialling_window"):
            d[k] = v
        else:
            d["governance"][k] = v
    return WorkflowSpec.from_api(d)


def policy(tick: int = 15, t: Thresholds | None = None, **gov: Any) -> ExtendedPolicy:
    s = spec(**gov)
    layer = load_directives()
    d = layer.resolve(s.tenant_id, s.workflow_id, s.use_case)
    return effective_policy(s, d, tick, t or Thresholds())


def hm(day: int, hh: int, mm: int = 0) -> int:
    """Minute of D{day} HH:MM."""
    return day * 1440 + hh * 60 + mm


def base_config(**alloc: dict[str, float]) -> Config:
    allocations = {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                               "caller_id": {"D1": 0.5, "D2": 0.5}}}
    for k, v in alloc.items():
        allocations["default"][k] = v
    return Config.from_body({"cohorts": [], "allocations": allocations, "prompt_version": "P1",
                             "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}})
