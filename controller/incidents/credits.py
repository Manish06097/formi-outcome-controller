"""CreditBudget: ordered credit pools R, S, U, LOW mode, authorize, ledger.

Owner: track T4 (milestone M6). Implements DESIGN.md 7.2 and ADR 0021.

    C0 = credits_remaining at start (API);  K = largest prompt catalog among the workflows (API)
    W  = 13 K (+ K if the client gate is tight)          worst incident (FULL verdicts on the new
         (+ 3 for the suggest-fixes call, 17.1)          prompt and every candidate, plus one
                                                         evidence run per candidate if tight)
    R  = min(0.8 C0, max(0.3 C0, 2 W))   incident reserve, shared across tenants
    S  = min(0.25 C0, C0 - R)            D0 screen pool, split across tenants by workflow count
    U  = C0 - R - S                      unallocated; tops R back up to W after an incident;
                                         unspent screen credit returns here
    NORMAL iff R + U >= W, else LOW: every verdict is STAGE1_ONLY (3 credits, W_low = 3 K
    (+ K if tight)), no D0 screens except escalated ones, no suggest-fixes call.
    402 (out of credits): stop all non-incident spend.

The pools are filled once at start (`plan_pools`) and persisted in `credit_envelope`; every spend
names its pool in `credit_ledger` (`record`). `authorize` debits in memory, so a refused spend is
never attempted; the caller persists the debit with `record` inside the intent's transaction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from controller.settings import Thresholds
from controller.types import CreditEnvelope, Tx

SHARED = "*"
INCIDENT_PURPOSES = frozenset({"verdict_full", "verdict_cusum", "verdict_lkg", "fix_candidate",
                               "gate_evidence", "suggest_fixes", "verdict_incumbent"})
"""Purposes that may spend the incident reserve R (only after a detector fired, 7.2)."""


def worst_incident(k: int, tight: bool, t: Thresholds, llm_suggest: bool = False) -> float:
    """W = 13 + 13 (K - 1) (+ K if tight) = 13 K (+ K), plus the one suggest-fixes call of
    DESIGN.md 17.1 (3 credits) when that call is enabled."""
    return (t.credit_full_verdict * k + (k if tight else 0)
            + (t.cost_suggest_fixes * t.llm_suggest_per_incident if llm_suggest else 0))


def worst_incident_low(k: int, tight: bool, t: Thresholds) -> float:
    """W_low = 3 K (+ K if tight): stage-1-only verdicts on the new prompt and every candidate."""
    stage1 = math.ceil(t.verdict_stage1_n / 100) * t.cost_validation_per_100
    return stage1 * k + (k if tight else 0)


@dataclass(frozen=True)
class PoolPlan:
    c0: float
    k: int
    tight: bool
    w: float
    r: float
    s: float
    u: float
    s_by_tenant: Mapping[str, float]

    @property
    def start_mode(self) -> str:
        return "normal" if self.r + self.u >= self.w else "low"


def plan_pools(c0: float, k: int, tight: bool, workflows_by_tenant: Mapping[str, int],
               t: Thresholds | None = None, llm_suggest: bool = True) -> PoolPlan:
    """The ordered pools of 7.2. S is split across tenants by workflow count (C15 envelope).
    `llm_suggest` (settings `CTL_LLM_SUGGEST`, on by default per 17.1) adds the per-incident
    suggest-fixes call to W, so the reserve covers every credit one worst incident spends."""
    t = t or Thresholds()
    w = worst_incident(k, tight, t, llm_suggest)
    r = min(t.credit_reserve_max_frac * c0, max(t.credit_reserve_min_frac * c0,
                                                t.credit_reserve_worst_mult * w))
    s = max(0.0, min(t.credit_screen_frac * c0, c0 - r))
    u = max(0.0, c0 - r - s)
    n = sum(workflows_by_tenant.values())
    s_by = {ten: (s * cnt / n if n else 0.0) for ten, cnt in sorted(workflows_by_tenant.items())}
    return PoolPlan(c0, k, tight, w, r, s, u, s_by)


@dataclass
class _Pool:
    allotted: float
    spent: float = 0.0

    @property
    def left(self) -> float:
        return self.allotted - self.spent


@dataclass
class OrderedCreditBudget:
    """The concrete `CreditBudget` (types.CreditBudget protocol)."""

    plan: PoolPlan
    t: Thresholds = field(default_factory=Thresholds)
    pools: dict[tuple[str, str], _Pool] = field(default_factory=dict)
    out_of_credits: bool = False
    api_remaining: float | None = None
    _last: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.pools:
            self.pools[("R", SHARED)] = _Pool(self.plan.r)
            self.pools[("U", SHARED)] = _Pool(self.plan.u)
            for ten, s in self.plan.s_by_tenant.items():
                self.pools[("S", ten)] = _Pool(s)

    # ------------------------------------------------------------------ persistence
    @classmethod
    def from_envelopes(cls, plan: PoolPlan, rows: list[CreditEnvelope],
                       t: Thresholds | None = None) -> "OrderedCreditBudget":
        """Rebuild after a restart from `credit_envelope` rows (the plan is recomputed from C0
        at start and persisted; the rows hold what was spent)."""
        b = cls(plan, t or Thresholds(), pools={(r.pool, r.tenant): _Pool(r.allotted, r.spent)
                                                 for r in rows})
        return b

    def envelopes(self) -> list[CreditEnvelope]:
        m = self.mode()
        return [CreditEnvelope(pool=p, tenant=ten, allotted=v.allotted, spent=v.spent, mode=m)
                for (p, ten), v in sorted(self.pools.items())]

    # ------------------------------------------------------------------ state
    def left(self, pool: str, tenant: str = SHARED) -> float:
        key = (pool, SHARED if pool in ("R", "U") else tenant)
        p = self.pools.get(key)
        return p.left if p else 0.0

    @property
    def w(self) -> float:
        return self.plan.w

    def mode(self) -> Literal["normal", "low"]:
        """NORMAL iff R + U can still cover one worst incident (and, when known, so can the
        API's balance); re-evaluated before every incident spend."""
        cover = self.left("R") + self.left("U")
        if self.api_remaining is not None:
            cover = min(cover, self.api_remaining)
        return "normal" if cover >= self.plan.w and not self.out_of_credits else "low"

    def sync(self, credits_remaining: float) -> None:
        """Record the API's balance (`/sim/state`, validation responses)."""
        self.api_remaining = credits_remaining

    def on_402(self) -> None:
        """Out of credits: page; stop all non-incident spend (the DID path needs no credits)."""
        self.out_of_credits = True
        self.api_remaining = 0.0

    # ------------------------------------------------------------------ spending
    def authorize(self, tenant: str, pool: Literal["R", "S", "U"], purpose: str,
                  amount: float) -> bool:
        """Debit `amount` from one pool if it can pay. R pays only incident purposes; S pays only
        that tenant's D0 screens; nothing non-incident is paid after a 402."""
        if amount <= 0:
            return True
        if pool == "R" and purpose not in INCIDENT_PURPOSES:
            return False
        if self.out_of_credits and purpose not in INCIDENT_PURPOSES:
            return False
        if self.api_remaining is not None and amount > self.api_remaining + 1e-9:
            return False
        key = (pool, SHARED if pool in ("R", "U") else tenant)
        p = self.pools.get(key)
        if p is None or p.left + 1e-9 < amount:
            return False
        p.spent += amount
        if self.api_remaining is not None:
            self.api_remaining -= amount
        self._last = {"tenant": tenant, "pool": pool, "purpose": purpose, "amount": amount}
        return True

    def authorize_incident(self, tenant: str, purpose: str, amount: float) -> str | None:
        """An incident spend: from R, else from U (which exists to top R up). Returns the pool
        that paid, or None."""
        for pool in ("R", "U"):
            if self.authorize(tenant, pool, purpose, amount):  # type: ignore[arg-type]
                return pool
        return None

    def refund(self, tenant: str, pool: str, amount: float) -> None:
        """A spend that provably did not happen (a 4xx before the call charged)."""
        key = (pool, SHARED if pool in ("R", "U") else tenant)
        if key in self.pools:
            self.pools[key].spent = max(0.0, self.pools[key].spent - amount)
            if self.api_remaining is not None:
                self.api_remaining += amount

    def top_up_reserve(self) -> float:
        """After an incident: move U into R until R has W left again. Returns the amount moved."""
        need = self.plan.w - self.left("R")
        if need <= 0:
            return 0.0
        move = min(need, self.left("U"))
        self.pools[("U", SHARED)].allotted -= move
        self.pools[("R", SHARED)].allotted += move
        return move

    def release_screens(self) -> float:
        """D0 screens done: unspent screen credit returns to U."""
        moved = 0.0
        for (pool, ten), p in self.pools.items():
            if pool == "S" and p.left > 0:
                moved += p.left
                p.allotted = p.spent
        self.pools[("U", SHARED)].allotted += moved
        return moved

    def verdict_cost(self, mode: str) -> float:
        """Upper bound of a verdict's cost in a mode (for planning)."""
        per = self.t.cost_validation_per_100
        s1 = math.ceil(self.t.verdict_stage1_n / 100) * per
        if mode == "stage1_only":
            return s1
        return s1 + math.ceil(self.t.verdict_stage2_n / 100) * per

    # ------------------------------------------------------------------ ledger
    def record(self, tx: Tx, intent_id: int, pool: str, cost: float, remaining_after: float,
               *, tenant: str | None = None, wf: int | None = None, purpose: str | None = None,
               sim_minute: int | None = None) -> None:
        """Persist one spend (`credit_ledger`) and the pool's spent total (`credit_envelope`) in
        the caller's transaction. Tenant and purpose default to the last authorization."""
        tenant = tenant or self._last.get("tenant", SHARED)
        purpose = purpose or self._last.get("purpose", "unknown")
        key_tenant = SHARED if pool in ("R", "U") else tenant
        p = self.pools.get((pool, key_tenant))
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO credit_ledger (sim_minute, tenant, wf, pool, purpose, cost,"
                " remaining_after, intent_id) VALUES (COALESCE(%s, (SELECT sim_minute FROM clock"
                " WHERE id = 1), 0), %s, %s, %s, %s, %s, %s, %s)",
                (sim_minute, tenant, wf, pool, purpose, cost, remaining_after, intent_id))
            if p is not None:
                cur.execute(
                    "INSERT INTO credit_envelope (pool, tenant, allotted, spent, mode)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (pool, tenant) DO UPDATE SET"
                    " allotted = EXCLUDED.allotted, spent = EXCLUDED.spent, mode = EXCLUDED.mode",
                    (pool, key_tenant, p.allotted, p.spent, self.mode()))
        self.sync(remaining_after)


__all__ = ["OrderedCreditBudget", "PoolPlan", "plan_pools", "worst_incident",
           "worst_incident_low", "INCIDENT_PURPOSES", "SHARED"]
