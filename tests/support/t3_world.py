"""A synthetic engagement world for the T3 learning tests (numpy only, no simulator).

It mirrors the simulator's engagement mechanics that matter to the learning loop
(sim/world.py:288-305): engagement given connect = min(e0[cell] x welcome_mult x voice_mult,
0.95), welcome and voice sampled independently per call from the lead's cohort distributions
(first-match cohorts), multipliers that may depend on the cell and on time (decay). It also
computes the grader's judgement of a change (grader.py:67-73): expected engagement over the
active leads under the old and the new config, harmful if it falls by more than 1%.

Truth lives only here; the controller code under test sees only the snapshot it builds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import numpy as np

from controller.snapshot import (HoldoutState, LookRecord, build_snapshot, cohort_of_cells,
                                 engage_from_columns, segment_start_minute)
from controller.types import (DEFAULT_COHORT, ActivePopulation, Cell, CohortDef, Config,
                              DecayMonitorState, EffectivePolicy, Experiment, FrozenDict,
                              WorkflowSpec)

WINDOW = (9, 21)
LANGS = ("en", "hi", "ta")
TIERS = ("t1", "t2", "t3")
AGES = ("18-30", "31-45", "46-60", "60+")
VALUES = ("low", "high")


def make_spec(welcomes: Sequence[str] = ("W1", "W2", "W3"),
              voices: Sequence[str] = ("V1", "V2", "V3", "V4"),
              primary: str = "engagement_rate", workflow_id: str = "wf_test",
              window: tuple[int, int] = WINDOW, dials_per_hour: int = 300) -> WorkflowSpec:
    guard = {"pickup_rate_max_drop": 0.02} if primary == "engagement_rate" else \
        {"engagement_rate_max_drop": 0.02}
    return WorkflowSpec.from_api({
        "workflow_id": workflow_id, "tenant_id": "tenant_test", "use_case": "renewal",
        "languages": list(LANGS), "dialling_window": list(window),
        "dials_per_hour": dials_per_hour, "cadence": [0, 1, 2], "min_gap_minutes": 120,
        "daily_batch": {"size": 1000, "at_hour": 8, "returning_share": 0.0},
        "mid_flight": "allowed", "objective": {"primary": primary, "guardrail": guard},
        "options": {"welcome_message": list(welcomes), "voice": list(voices),
                    "caller_id": ["D1", "D2", "D3"], "prompt_version": ["P1", "P2", "P3"]},
        "prompt_catalog": {}, "current_version": 1,
        "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"],
                                      "max_shift": 0.2},
                       "approver_hours": [10, 19], "approver_latency_minutes": [30, 240],
                       "proposal_ttl_hours": 24, "apply_window_hours": 2,
                       "max_requests_per_24h": 3, "base_reject_rate": 0.1,
                       "min_validation_pass_rate": 0.97}})


def make_policy(cohort_harm_line: float = 0.02, workflow_id: str = "wf_test") -> EffectivePolicy:
    return EffectivePolicy(
        tick=15, approver_hours=(10, 19), approver_open_min=600, latency_minutes=(30, 240),
        ttl_hours=24.0, apply_window_min=120, c_policy=3, c_eff=3, reserve=1, routine_budget=2,
        auto_enabled=True, allowed_dims=frozenset({"schedule", "caller_id"}), max_shift=0.2,
        customer_facing_auto="none", not_before_min=None, preferred_slot_min=660,
        notice_minutes=0, cohort_harm_line=cohort_harm_line, mid_flight="allowed",
        min_validation_pass_rate=0.97, avoid_prompt_terms=(), workflow_id=workflow_id,
        tenant_id="tenant_test", dialling_window=WINDOW)


def make_cells(langs: Sequence[str] = LANGS, tiers: Sequence[str] = TIERS,
               ages: Sequence[str] = AGES, values: Sequence[str] = VALUES) -> dict[int, Cell]:
    out, i = {}, 0
    for lang in langs:
        for tier in tiers:
            for age in ages:
                for val in values:
                    out[i] = Cell(i, lang, tier, age, val)
                    i += 1
    return out


def base_config(spec: WorkflowSpec, cohorts: Sequence[CohortDef] = (),
                control_w: str | None = None, control_v: str | None = None) -> Config:
    w = control_w or spec.options["welcome_message"][0]
    v = control_v or spec.options["voice"][0]
    dids = spec.options["caller_id"]
    return Config.from_body({"cohorts": [c.body() for c in cohorts],
                             "allocations": {"default": {"welcome_message": {w: 1.0},
                                                         "voice": {v: 1.0},
                                                         "caller_id": {d: 1.0 for d in dids}}},
                             "prompt_version": "P1", "schedule": {"hour_weights": {}}})


MultFn = Callable[[str, str, Cell, float], float]
"""(dim, option, cell, exposure_days) -> multiplier."""


def null_mult(dim: str, option: str, cell: Cell, days: float) -> float:
    return 1.0


@dataclass
class SyntheticWorld:
    spec: WorkflowSpec
    cells: dict[int, Cell]
    e0: np.ndarray
    """base engagement per cell (index = cell id)"""
    leads: np.ndarray
    """active leads per cell (also its share of connected calls)"""
    mult: MultFn = null_mult
    seed: int = 0
    connected_per_day: int = 950
    first_exposure: dict[tuple[str, str], float] = field(default_factory=dict)
    """(dim, option) -> segment position of the first call with that option (decay clock)."""
    rows: dict[str, list[int]] = field(default_factory=lambda: {k: [] for k in (
        "seg", "cell", "config_version", "prompt", "w", "v", "connected", "engaged", "flags")})

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    # ------------------------------------------------------------------ truth
    def exposure_days(self, dim: str, option: str, seg: float) -> float:
        start = self.first_exposure.get((dim, option))
        return 0.0 if start is None else max(0.0, (seg - start) / 2.0)

    def multiplier(self, dim: str, option: str, cell: Cell, seg: float) -> float:
        return self.mult(dim, option, cell, self.exposure_days(dim, option, seg))

    def expected_engagement(self, cfg: Config, seg: float) -> float:
        """Grader: mean over active leads of E[engaged | connected] under cfg at time seg."""
        ids = sorted(self.cells)
        coh = cohort_of_cells(cfg, self.cells, ids)
        tot = 0.0
        for c, k in zip(ids, coh):
            cell = self.cells[c]
            m = 1.0
            for dim in ("welcome_message", "voice"):
                m *= sum(p * self.multiplier(dim, o, cell, seg) for o, p in cfg.dist(k, dim).items())
            tot += self.leads[c] * min(self.e0[c] * m, 0.95)
        return tot / float(self.leads[ids].sum())

    def grade(self, old: Config, new: Config, seg: float) -> tuple[str, float]:
        eo, en = self.expected_engagement(old, seg), self.expected_engagement(new, seg)
        de = en / eo - 1.0
        return ("harmful" if de < -0.01 else "beneficial" if de > 0.01 else "neutral"), de

    # ------------------------------------------------------------------ data
    def simulate_segment(self, cfg: Config, seg: int, version: int = 1, prompt: int = 0) -> None:
        """Connected calls of one half-day segment under cfg (counts aggregated like r_engage)."""
        n_total = self.connected_per_day // 2
        ids = sorted(self.cells)
        share = self.leads[ids] / self.leads[ids].sum()
        per_cell = self.rng.multinomial(n_total, share)
        coh = cohort_of_cells(cfg, self.cells, ids)
        wopts = list(self.spec.options["welcome_message"])
        vopts = list(self.spec.options["voice"])
        for c, k, n in zip(ids, coh, per_cell):
            if n == 0:
                continue
            cell = self.cells[c]
            pw = np.array([cfg.dist(k, "welcome_message").get(o, 0.0) for o in wopts])
            pv = np.array([cfg.dist(k, "voice").get(o, 0.0) for o in vopts])
            joint = np.outer(pw, pv).ravel()
            counts = self.rng.multinomial(n, joint / joint.sum())
            for idx in np.flatnonzero(counts):
                wi, vi = divmod(int(idx), len(vopts))
                for dim, o in (("welcome_message", wopts[wi]), ("voice", vopts[vi])):
                    self.first_exposure.setdefault((dim, o), float(seg))
                m = (self.multiplier("welcome_message", wopts[wi], cell, seg + 0.5)
                     * self.multiplier("voice", vopts[vi], cell, seg + 0.5))
                p = min(self.e0[c] * m, 0.95)
                eng = int(self.rng.binomial(int(counts[idx]), p))
                for key, val in (("seg", seg), ("cell", c), ("config_version", version),
                                 ("prompt", prompt), ("w", wi), ("v", vi),
                                 ("connected", int(counts[idx])), ("engaged", eng), ("flags", 0)):
                    self.rows[key].append(val)

    def active(self, cfg: Config) -> ActivePopulation:
        ids = np.array(sorted(self.cells), dtype=np.int64)
        leads = self.leads[ids].astype(float)
        coh = cohort_of_cells(cfg, self.cells, ids.tolist())
        return ActivePopulation(cells=ids, leads=leads, mix=leads / leads.sum(),
                                cohort_of_cell=FrozenDict({int(c): k for c, k in zip(ids, coh)}))

    def snapshot(self, cfg: Config, seg_now: int, *, policy: EffectivePolicy | None = None,
                 experiments: Sequence[Experiment] = (), looks: Sequence[LookRecord] = (),
                 decay: Sequence[DecayMonitorState] = (), holdouts: Sequence[HoldoutState] = (),
                 version: int = 1, run_id: str = "run_test"):
        now = segment_start_minute(seg_now, self.spec.dialling_window) + 30
        return build_snapshot(
            spec=self.spec, policy=policy or make_policy(), live=cfg, live_version=version,
            now=now, engage=engage_from_columns(self.rows), experiments=experiments,
            cells=self.cells, active=self.active(cfg), looks=looks, decay=decay,
            holdouts=holdouts, run_id=run_id)


def uniform_world(seed: int = 0, connected_per_day: int = 950, e: float = 0.55,
                  mult: MultFn = null_mult, cells: dict[int, Cell] | None = None,
                  spec: WorkflowSpec | None = None) -> SyntheticWorld:
    cells = cells or make_cells()
    n = max(cells) + 1
    return SyntheticWorld(spec=spec or make_spec(), cells=cells, e0=np.full(n, e),
                          leads=np.ones(n), mult=mult, seed=seed,
                          connected_per_day=connected_per_day)


def cohorts_by_age(spec: WorkflowSpec) -> list[CohortDef]:
    """Two grid-like cohorts: ages 46+ (about half the leads) and the rest in default."""
    return [CohortDef.make("age46p~000001", {"age_band": ["46-60", "60+"]})]


def dist_map(cfg: Config, dim: str) -> Mapping[str, Mapping[str, float]]:
    return {k: dict(cfg.dist(k, dim)) for k in list(cfg.cohort_ids()) + [DEFAULT_COHORT]}
