"""Synthetic WorkflowSnapshot builders for the T4 pure tests (cohorts, schedule, caller ID,
detectors, incidents). Test-only: shapes follow controller/types.py; numbers are invented, never
public truth.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from controller.cohorts.cells import CellCatalog
from controller.cohorts.cohortset import GridCohortSet
from controller.types import (Config, DidArrays, EffectivePolicy, EngageArrays, FlagSeries,
                              FrozenDict, PickupArrays, WorkflowSnapshot, WorkflowSpec)

LANGS = ("en", "hi", "ta")
TIERS = ("1", "2", "3")
AGES = ("18-30", "31-45", "46-60", "60+")
VALUES = ("low", "mid", "high")


def spec_dict(wf: str = "wf_a", *, dids: Sequence[str] = ("D1", "D2", "D3"),
              prompts: Sequence[str] = ("P1", "P2", "P3", "P4", "P5"),
              window: tuple[int, int] = (9, 21), dials_per_hour: int = 400,
              tenant: str = "tenant_a", g: float = 0.97, auto: bool = True,
              allowed: Sequence[str] = ("caller_id", "schedule"), max_shift: float = 0.2,
              mid_flight: str = "allowed", primary: str = "engagement_rate") -> dict[str, Any]:
    return {
        "workflow_id": wf, "tenant_id": tenant, "use_case": "synthetic renewal",
        "languages": list(LANGS), "dialling_window": list(window),
        "dials_per_hour": dials_per_hour, "cadence": [2, 3, 3], "min_gap_minutes": 60,
        "daily_batch": {"size": 1000, "at_hour": 8, "returning_share": 0.3},
        "mid_flight": mid_flight,
        "objective": {"primary": primary, "guardrail": {"pickup_rate_max_drop": 0.02}},
        "options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"],
                    "caller_id": list(dids), "prompt_version": list(prompts)},
        "prompt_catalog": {p: f"prompt {p}" for p in prompts},
        "governance": {"auto_apply": {"enabled": auto, "allowed_dims": list(allowed),
                                      "max_shift": max_shift},
                       "approver_hours": [10, 19], "approver_latency_minutes": [20, 120],
                       "proposal_ttl_hours": 24, "apply_window_hours": 2,
                       "max_requests_per_24h": 4, "base_reject_rate": 0.0,
                       "min_validation_pass_rate": g},
        "current_version": 1,
    }


def make_spec(**kw: Any) -> WorkflowSpec:
    return WorkflowSpec.from_api(spec_dict(**kw))


def make_policy(spec: WorkflowSpec, tick: int = 15, **over: Any) -> EffectivePolicy:
    gov = spec.governance
    base = dict(tick=tick, approver_hours=gov.approver_hours,
                approver_open_min=gov.approver_hours[0] * 60,
                latency_minutes=gov.approver_latency_minutes, ttl_hours=gov.proposal_ttl_hours,
                apply_window_min=int(gov.apply_window_hours * 60), c_policy=4, c_eff=4,
                reserve=1, routine_budget=2, auto_enabled=gov.auto_apply.enabled,
                allowed_dims=gov.auto_apply.allowed_dims, max_shift=gov.auto_apply.max_shift,
                customer_facing_auto="risk_reducing_only", not_before_min=None,
                preferred_slot_min=660, notice_minutes=0, cohort_harm_line=0.02,
                mid_flight=spec.mid_flight,
                min_validation_pass_rate=gov.min_validation_pass_rate, avoid_prompt_terms=(),
                workflow_id=spec.workflow_id, tenant_id=spec.tenant_id,
                dialling_window=spec.dialling_window)
    base.update(over)
    return EffectivePolicy(**base)


def full_catalog() -> CellCatalog:
    cat = CellCatalog()
    for lang in LANGS:
        for t in TIERS:
            for a in AGES:
                for v in VALUES:
                    cat.add({"lang": lang, "city_tier": t, "age_band": a, "value_band": v})
    return cat


def config(dids: Mapping[str, float] | None = None, prompt: str = "P1",
           hour_weights: Mapping[str, Mapping[int, float]] | None = None,
           cohorts: Sequence = ()) -> Config:
    body = {"cohorts": [c.body() for c in cohorts],
            "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                        "caller_id": dict(dids or {"D1": 1.0})}},
            "prompt_version": prompt,
            "schedule": {"hour_weights": {k: {str(h): w for h, w in t.items()}
                                          for k, t in (hour_weights or {}).items()}}}
    return Config.from_body(body)


def empty_engage() -> EngageArrays:
    z = np.zeros(0, dtype=np.int64)
    return EngageArrays(z.copy(), z.copy(), z.copy(), z.copy(), z.copy(), z.copy(), z.copy(),
                        z.copy(), z.copy())


def pickup_arrays(rows: Iterable[tuple[int, int, int, int, int, int]]) -> PickupArrays:
    """rows: (sim_day, hour, cell, attempt_bucket, dials, connected)."""
    a = np.array(list(rows), dtype=np.int64).reshape(-1, 6)
    return PickupArrays(*(a[:, i].copy() for i in range(6)))


def did_arrays(rows: Iterable[tuple[int, int, int, int, int, int]]) -> DidArrays:
    """rows: (tick_no, hour, cohort_code, did_code, dials, connected)."""
    a = np.array(list(rows), dtype=np.int64).reshape(-1, 6)
    return DidArrays(*(a[:, i].copy() for i in range(6)))


def flag_series(rows: Iterable[tuple[int, int, int, int, int]]) -> FlagSeries:
    """rows: (sim_hour, prompt_code, connected, flags, engaged)."""
    a = np.array(list(rows), dtype=np.int64).reshape(-1, 5)
    return FlagSeries(*(a[:, i].copy() for i in range(5)))


def snapshot(spec: WorkflowSpec, *, live: Config | None = None, now: int = 0,
             cohorts: GridCohortSet | None = None, pickup: PickupArrays | None = None,
             did: DidArrays | None = None, flags: FlagSeries | None = None,
             did_state: Mapping | None = None, policy: EffectivePolicy | None = None,
             live_version: int = 1) -> WorkflowSnapshot:
    cat = cohorts.catalog if cohorts else full_catalog()
    cs = cohorts or GridCohortSet(spec.workflow_id, (), cat)
    return WorkflowSnapshot(
        spec=spec, policy=policy or make_policy(spec), live=live or config(),
        live_version=live_version, cohorts=cs, engage=empty_engage(),
        pickup=pickup if pickup is not None else pickup_arrays([]),
        did=did if did is not None else did_arrays([]),
        flags=flags if flags is not None else flag_series([]), experiments=(),
        did_state=FrozenDict(did_state or {}), in_flight=None, used_24h=(), now=now)


# ------------------------------------------------------------------------------- simulators

def simulate_did_day(rng: np.random.Generator, *, day: int, tick: int, window: tuple[int, int],
                     dials_per_hour: int, shares: Mapping[int, float], p: float,
                     r: Mapping[int, float] | None = None,
                     r_after: Mapping[int, tuple[int, float]] | None = None,
                     cohort_code: int = 0) -> list[tuple[int, int, int, int, int, int]]:
    """r_did rows for one dialling day: each tick's dials split multinomially over DIDs, each DID
    connecting at `p x r_d`; `r_after[d] = (minute, r)` switches DID d's ratio from that minute
    (a flag)."""
    rows = []
    per_tick = dials_per_hour * tick // 60
    codes = sorted(shares)
    probs = np.array([shares[c] for c in codes], dtype=float)
    probs = probs / probs.sum()
    for minute in range(day * 1440 + window[0] * 60, day * 1440 + window[1] * 60, tick):
        n = rng.multinomial(per_tick, probs)
        for c, nd in zip(codes, n):
            if nd == 0:
                continue
            rr = (r or {}).get(c, 1.0)
            if r_after and c in r_after and minute >= r_after[c][0]:
                rr = r_after[c][1]
            k = rng.binomial(int(nd), min(p * rr, 0.95))
            rows.append((minute // tick, (minute % 1440) // 60, cohort_code, c, int(nd), int(k)))
    return rows
