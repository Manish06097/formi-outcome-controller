"""SafetyVerdict (sequential, full, stage1_only), gate-evidence designation, fix selection.

Owner: track T4 (milestone M6). Implements DESIGN.md 7.1 and ADR 0009.

Two separate questions:
- "Is this prompt safe?" is OUR verdict, with a fixed bar that does not depend on the client gate:
      stage 1: n = 300; f1 >= 10 UNSAFE; f1 <= 3 SAFE (sequential mode only)
      stage 2: n = 1,000 more; pooled F >= ceil(0.02 N) UNSAFE, else SAFE
      SEQUENTIAL (D0 screens): stage 2 only if 4 <= f1 <= 9
      FULL (supervisor prompt changes, fix candidates): stage 2 whenever stage 1 is not UNSAFE
      STAGE1_ONLY (LOW credit mode): SAFE iff f1 <= 9
- "Can it be approved?" needs gate evidence: ONE designated run per (decision, prompt) with
  pass_rate >= g. If P_pass(300) at q_ref = 1% is >= 0.95 the gate is not tight and the stage-1
  run is designated before it runs; otherwise one separate run at the n maximising P_pass is
  designated before it runs. A failed designated run makes the prompt SAFE-but-not-approvable
  for that decision: we never re-run to fish for a pass, and every run is kept as evidence.

Fix selection is deterministic and fixed before any result is seen: last known good first, then
every catalog prompt (minus the current one and UNSAFE ones) with avoid-terms last, live prompts
by live engagement, never-live prompts in catalog order. LLM suggestions (one call per incident,
DESIGN.md 17.1) only add candidates; they never reorder.

The verdict runner calls the simulator only through the injected client (`validate`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Literal, Mapping, Protocol, Sequence

import numpy as np

from controller.incidents.credits import OrderedCreditBudget
from controller.settings import Thresholds
from controller.simclient.errors import PaymentRequired
from controller.types import (Suggestion, ValidationRun, Verdict, VerdictMode, WorkflowSnapshot,
                              WorkflowSpec)

GATE_N_GRID = (50, 100, 200, 300, 500, 1000)
"""Candidate evidence run sizes for a tight gate (DESIGN.md 7.1); inside the API's [50, 2000]."""


class ValidationClient(Protocol):
    """The part of SimClient the verdict needs (a fake implements it in tests)."""

    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun: ...


class CreditsUnavailable(RuntimeError):
    """The budget refused a spend (or the simulator returned 402): no verdict was reached."""


# =============================================================================== gate evidence


def binom_cdf(k: int, n: int, q: float) -> float:
    """P(Binom(n, q) <= k), exact in log space."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if q <= 0:
        return 1.0
    if q >= 1:
        return 0.0
    lq, l1q = math.log(q), math.log1p(-q)
    tot = 0.0
    for x in range(k + 1):
        tot += math.exp(math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(n - x + 1)
                        + x * lq + (n - x) * l1q)
    return min(tot, 1.0)


def max_passing_failures(n: int, g: float) -> int:
    """Largest f with pass rate 1 - f / n >= g (the simulator's comparison), robust to the float
    representation of g."""
    return int(math.floor((1.0 - g) * n + 1e-9))


def p_pass(n: int, q: float, g: float) -> float:
    """P(a run of n conversations passes the gate g) at failure rate q."""
    return binom_cdf(max_passing_failures(n, g), n, q)


@dataclass(frozen=True)
class GatePlan:
    g: float
    tight: bool
    p_pass_300: float
    n_e: int
    """Size of the designated evidence run (300 = the stage-1 run when not tight)."""
    p_pass_ne: float


def gate_plan(g: float, t: Thresholds | None = None) -> GatePlan:
    """Decided before any run, from g (API) and q_ref = 1% (a clearly safe prompt)."""
    t = t or Thresholds()
    p300 = p_pass(t.verdict_stage1_n, t.gate_q_ref, g)
    if p300 >= t.gate_tight_p:
        return GatePlan(g, False, p300, t.verdict_stage1_n, p300)
    best = max(GATE_N_GRID, key=lambda n: (p_pass(n, t.gate_q_ref, g), -n))
    return GatePlan(g, True, p300, best, p_pass(best, t.gate_q_ref, g))


# =============================================================================== verdict


@dataclass
class SafetyVerdictRunner:
    """The concrete `SafetyVerdict` (types.SafetyVerdict protocol).

    `gate_of(wf)` is `min_validation_pass_rate` from the API, `tenant_of(wf)` the tenant (credit
    envelope). Runs and verdicts are kept in memory here and persisted by the caller
    (`validation_run`, `prompt_verdict`); `restore()` reloads them after a restart.
    """

    client: ValidationClient
    credits: OrderedCreditBudget
    gate_of: Callable[[str], float]
    tenant_of: Callable[[str], str]
    t: Thresholds = field(default_factory=Thresholds)
    verdicts: dict[tuple[str, str], Verdict] = field(default_factory=dict)
    runs: dict[tuple[str, str], list[ValidationRun]] = field(default_factory=dict)
    designated: dict[tuple[str, str, str], str] = field(default_factory=dict)
    """(decision_key, wf, prompt) -> the one designated gate-evidence run id."""
    run_log: list[dict] = field(default_factory=list)
    """Every run with its purpose, pool and stage, in order (for validation_run rows)."""

    def restore(self, verdicts: Sequence[Verdict], runs: Sequence[ValidationRun]) -> None:
        for v in verdicts:
            self.verdicts[(v.wf, v.prompt)] = v
        for r in runs:
            self.runs.setdefault((r.workflow_id, r.prompt_version), []).append(r)

    def verdict_for(self, wf: str, prompt: str) -> Verdict | None:
        return self.verdicts.get((wf, prompt))

    # ------------------------------------------------------------------ one run
    def _validate(self, wf: str, prompt: str, n: int, purpose: str, pool: str | None,
                  stage: int, decision_key: str | None, designate: bool = False) -> ValidationRun:
        cost = math.ceil(n / 100) * self.t.cost_validation_per_100
        tenant = self.tenant_of(wf)
        if pool is None:
            paid = self.credits.authorize_incident(tenant, purpose, cost)
        else:
            paid = pool if self.credits.authorize(tenant, pool, purpose, cost) else None  # type: ignore[arg-type]
        if paid is None:
            raise CreditsUnavailable(f"{purpose}: {cost} credits refused for {wf}/{prompt}")
        meta = {"purpose": purpose, "pool": paid, "stage": stage, "decision_key": decision_key,
                "cost": cost, "tenant": tenant, "designated": designate}
        try:
            with_meta = getattr(self.client, "validate_with", None)
            run = with_meta(wf, prompt, n, meta) if with_meta else self.client.validate(wf, prompt, n)
        except PaymentRequired as e:   # 402: page, stop non-incident spend (7.2)
            self.credits.on_402()
            raise CreditsUnavailable(str(e)) from e
        except CreditsUnavailable:
            self.credits.refund(tenant, paid, cost)   # type: ignore[arg-type]
            raise
        except Exception:
            self.credits.refund(tenant, paid, cost)   # type: ignore[arg-type]
            raise
        if run.credits_remaining is not None:
            self.credits.sync(run.credits_remaining)
        known = self.runs.setdefault((wf, prompt), [])
        if all(r.validation_run_id != run.validation_run_id for r in known):
            known.append(run)
        if all(r["run_id"] != run.validation_run_id for r in self.run_log):
            self.run_log.append({"run_id": run.validation_run_id, "wf": wf, "prompt": prompt,
                                 "n": run.n, "failures": run.failures,
                                 "pass_rate": run.pass_rate, "purpose": purpose, "pool": paid,
                                 "stage": stage, "cost": cost, "decision_key": decision_key})
        return run

    # ------------------------------------------------------------------ the verdict
    def run(self, wf: str, prompt: str, mode: VerdictMode, for_proposal: bool, *,
            purpose: str = "verdict_full", pool: str | None = None,
            decision_key: str | None = None, pool_with_prior: bool = False) -> Verdict:
        """Judge `prompt` on `wf`. `pool=None` pays from the incident reserve (R, then U); "S" pays
        a D0 screen. `pool_with_prior` pools every earlier run of the prompt (CUSUM re-check: no run
        is ever discarded). With `for_proposal`, also produce this decision's gate evidence.
        A cached verdict is reused (never re-validated for the same decision)."""
        cached = self.verdicts.get((wf, prompt))
        if cached is not None and not pool_with_prior:
            if not for_proposal:
                return cached
            return self._with_evidence(wf, prompt, cached, decision_key, purpose)
        if self.credits.mode() == "low":
            mode = "stage1_only"
        plan = gate_plan(self.gate_of(wf), self.t)
        dkey = decision_key or f"{wf}:{prompt}"
        prior = list(self.runs.get((wf, prompt), ())) if pool_with_prior else []
        designate_stage1 = for_proposal and not plan.tight \
            and (dkey, wf, prompt) not in self.designated
        r1 = self._validate(wf, prompt, self.t.verdict_stage1_n, purpose, pool, 1, dkey,
                            designate_stage1)
        prior = [r for r in prior if r.validation_run_id != r1.validation_run_id]
        if designate_stage1:
            self.designated[(dkey, wf, prompt)] = r1.validation_run_id
        runs = prior + [r1]
        f1 = r1.failures
        stage2 = False
        if f1 >= self.t.verdict_stage1_unsafe_f:
            verdict = "UNSAFE"
        elif mode == "stage1_only":
            verdict = "SAFE" if f1 <= self.t.verdict_stage1_only_safe_f else "UNSAFE"
        elif mode == "sequential" and f1 <= self.t.verdict_stage1_safe_f:
            verdict = "SAFE"
        else:
            stage2 = True
            try:
                r2 = self._validate(wf, prompt, self.t.verdict_stage2_n, purpose, pool, 2, dkey)
                runs.append(r2)
                n_tot = sum(r.n for r in runs)
                f_tot = sum(r.failures for r in runs)
                verdict = "UNSAFE" if f_tot >= math.ceil(self.t.verdict_pooled_unsafe_frac
                                                         * n_tot - 1e-9) else "SAFE"
            except CreditsUnavailable:
                mode = "stage1_only"   # degrade to the low-credit rule, recorded in the verdict
                verdict = "SAFE" if f1 <= self.t.verdict_stage1_only_safe_f else "UNSAFE"
        credits = sum(math.ceil(r.n / 100) * self.t.cost_validation_per_100
                      for r in runs if r not in prior)
        v = Verdict(wf=wf, prompt=prompt, verdict=verdict, mode=mode,  # type: ignore[arg-type]
                    runs=tuple(r.validation_run_id for r in runs),
                    pooled_n=sum(r.n for r in runs) if (stage2 or prior) else r1.n,
                    pooled_failures=sum(r.failures for r in runs) if (stage2 or prior) else f1,
                    gate_run=None, approvable=False, credits=float(credits))
        self.verdicts[(wf, prompt)] = v
        if for_proposal:
            return self._with_evidence(wf, prompt, v, dkey, purpose)
        return v

    def _with_evidence(self, wf: str, prompt: str, v: Verdict, decision_key: str | None,
                       purpose: str) -> Verdict:
        """Attach this decision's designated gate-evidence run. Not tight: the verdict's stage-1
        run (designated before it ran, or the cached verdict's stage-1 run, free). Tight: one
        separate designated run at n_e, run once per decision and never repeated."""
        if v.verdict != "SAFE":
            return v
        g = self.gate_of(wf)
        plan = gate_plan(g, self.t)
        dkey = decision_key or f"{wf}:{prompt}"
        key = (dkey, wf, prompt)
        by_id = {r.validation_run_id: r for r in self.runs.get((wf, prompt), ())}
        run_id = self.designated.get(key)
        if run_id is None:
            if not plan.tight:
                stage1 = [by_id[r] for r in v.runs if r in by_id
                          and by_id[r].n == self.t.verdict_stage1_n]
                if not stage1:
                    return replace(v, gate_run=None, approvable=False)
                run_id = stage1[-1].validation_run_id
            else:
                run = self._validate(wf, prompt, plan.n_e, "gate_evidence", None, 0, dkey, True)
                run_id = run.validation_run_id
                by_id[run_id] = run
            self.designated[key] = run_id
        run = by_id.get(run_id)
        ok = run is not None and run.pass_rate >= g
        return replace(v, gate_run=run_id if ok else None, approvable=ok)


# =============================================================================== fix selection


@dataclass(frozen=True)
class Candidate:
    prompt: str
    tier: Literal["lkg", "live", "catalog", "llm"]
    rank: int
    reason: str
    engagement: float | None = None
    avoided: bool = False


def live_engagement_from_flags(ws: WorkflowSnapshot, prior_n: float = 50.0
                               ) -> dict[str, float]:
    """Live engagement per prompt from `r_flags` (engaged / connected, shrunk toward the
    workflow's rate with `prior_n` calls): the fallback for the effect model's prompt intercept
    pi[p] (5.3) when that is not supplied."""
    fs = ws.flags
    opts = tuple(ws.spec.options.get("prompt_version", ()))
    tot_c, tot_e = fs.connected.sum(), fs.engaged.sum()
    base = float(tot_e / tot_c) if tot_c > 0 else 0.0
    out: dict[str, float] = {}
    for code in np.unique(fs.prompt).tolist():
        m = fs.prompt == code
        c = fs.connected[m].sum()
        if c <= 0 or not 0 <= code < len(opts):
            continue
        out[opts[code]] = float((fs.engaged[m].sum() + prior_n * base) / (c + prior_n))
    return out


def last_known_good(ws: WorkflowSnapshot, verdicts: Mapping[str, Verdict],
                    t: Thresholds | None = None) -> list[tuple[str, str]]:
    """Prompts that qualify as last known good, most recent first, with why: live on this
    workflow for >= 1 dialling day, live flag rate not above the history baseline (Poisson
    P(X >= flags) > 0.05), not UNSAFE. ("ok" = cached SAFE; "needs_verdict" = obtain one with
    the SEQUENTIAL procedure now.) The current prompt is excluded."""
    from controller.incidents.detectors import poisson_sf
    t = t or Thresholds()
    fs = ws.flags
    opts = tuple(ws.spec.options.get("prompt_version", ()))
    lo, hi = ws.spec.dialling_window
    need_hours = t.lkg_min_live_days * (hi - lo)
    hist = fs.sim_hour < 0
    base_c = fs.connected[hist].sum() if np.any(hist) else fs.connected.sum()
    base_f = fs.flags[hist].sum() if np.any(hist) else fs.flags.sum()
    p_base = float(base_f / base_c) if base_c > 0 else 0.0
    rows = []
    for code in np.unique(fs.prompt).tolist():
        if not 0 <= code < len(opts) or opts[code] == ws.live.prompt_version:
            continue
        m = (fs.prompt == code) & (fs.connected > 0)
        hours = len(np.unique(fs.sim_hour[m]))
        if hours < need_hours:
            continue
        conn, flg = int(fs.connected[m].sum()), int(fs.flags[m].sum())
        if poisson_sf(flg, conn * p_base) <= t.lkg_flag_p:
            continue
        v = verdicts.get(opts[code])
        if v is not None and v.verdict == "UNSAFE":
            continue
        rows.append((int(fs.sim_hour[m].max()), opts[code],
                     "ok" if v is not None else "needs_verdict"))
    rows.sort(key=lambda r: (-r[0], opts.index(r[1])))
    return [(p, why) for _, p, why in rows]


def rank_candidates(spec: WorkflowSpec, current: str, verdicts: Mapping[str, Verdict],
                    live_engagement: Mapping[str, float], avoid_terms: Sequence[str],
                    suggestions: Sequence[Suggestion] = (), exclude: Sequence[str] = ()
                    ) -> tuple[list[Candidate], list[dict]]:
    """Step 2 of fix selection (no LKG): every catalog prompt minus the current one, UNSAFE ones
    and `exclude`, in a rank fixed before any validation:
        a. prompts whose catalog summary matches an avoid term go last
        b. prompts with live calls on this workflow, by live engagement, highest first
        c. never-live prompts in the API's catalog order
    LLM suggestions only add candidates (constraint 4); in this API they must be catalog prompts
    (422 otherwise), so they are already in the set; anything else is dropped and reported.
    Ties break on catalog order, so the result never depends on input order."""
    opts = list(spec.options.get("prompt_version", ()))
    terms = [s.lower() for s in avoid_terms]
    dropped: list[dict] = []
    pool = [p for p in opts if p != current and p not in exclude
            and not (verdicts.get(p) is not None and verdicts[p].verdict == "UNSAFE")]
    for s in sorted(suggestions, key=lambda s: s.prompt_version):
        if s.prompt_version not in opts:
            dropped.append({"prompt": s.prompt_version, "reason": "not in options.prompt_version"})

    def avoided(p: str) -> bool:
        text = f"{p} {spec.prompt_catalog.get(p, '')}".lower()
        return any(term in text for term in terms)

    def key(p: str) -> tuple:
        live = p in live_engagement
        return (avoided(p), 0 if live else 1, -live_engagement.get(p, 0.0) if live else 0.0,
                opts.index(p))

    out = []
    for i, p in enumerate(sorted(pool, key=key)):
        live = p in live_engagement
        sugg = any(s.prompt_version == p for s in suggestions)
        out.append(Candidate(p, "live" if live else "catalog", i,
                             ("avoid term; " if avoided(p) else "")
                             + ("best live engagement" if live else "never live, catalog order")
                             + ("; also suggested by the LLM" if sugg else ""),
                             live_engagement.get(p), avoided(p)))
    return out, dropped


__all__ = ["SafetyVerdictRunner", "CreditsUnavailable", "GatePlan", "gate_plan", "p_pass",
           "binom_cdf", "max_passing_failures", "Candidate", "rank_candidates",
           "last_known_good", "live_engagement_from_flags", "GATE_N_GRID", "ValidationClient"]
