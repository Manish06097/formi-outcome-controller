"""Request builder: summary, diff, affected leads, dry run, impact score, rationale.

Owner: track T2 (milestone M3). Implements DESIGN.md 6.6 (impact score, affected leads, dry run)
and the batching text of 6.2. ADR 0020 (impact score definition).

Everything here is deterministic: the same composition and inputs give the same text and
numbers (sorted iteration, templated sentences, seeded posterior draws from the caller). No LLM
text anywhere.

Dry run = stratified posterior replay (F14, SNIPS cut). For welcome and voice, per cell c and
posterior draw s, with r_v the draw's engagement ratio of option v against the dim's control:
    f_d(c) = sum_v pi(v | cohort of c) r_v           for d in (welcome_message, voice)
    E(c)   = f_welcome(c) x f_voice(c)                 (no welcome x voice interaction, A-1)
    de     = sum_c m_c E_new(c) / sum_c m_c E_old(c) - 1
m_c is the next-24h connected mix of the cell (optionally times its base engagement). The draws
give the mean, the 90% interval, P_harm = P(de < -1%) and Q20. Caller ID is the same with the
per-DID relative pickup draws r_d in place of r_v (dd, P(dd < -2%)).

Impact score (ADR 0020) = clip(Q20 of the expected relative change of the KPI the lever moves,
0, 1), stored with that KPI's label; a bundle gets the score of its highest-class item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from controller.governance.policy import tv
from controller.planner.overlay import diff_paths, path_text
from controller.types import (AGENT_DIMS, DEFAULT_COHORT, ApprovalRequest, Cell, Composition,
                              Config, DidPosterior, Draws, FrozenDict, GuardrailForecast,
                              HarmReport, Patch, freeze, thaw)

ENGAGEMENT_DIMS = ("welcome_message", "voice")
DIM_WORDS = {"welcome_message": "Welcome message", "voice": "Voice", "caller_id": "Caller ID"}
KPI_OF_LEVER = {"welcome_message": "engagement_rate", "voice": "engagement_rate",
                "caller_id": "pickup_rate", "hour_weights": "pickup_rate",
                "prompt": "engagement_rate", "cohorts": None}
HARM_LINE_E = -0.01
HARM_LINE_D = -0.02


# ---------------------------------------------------------------- formatting
def pct(x: float) -> str:
    """0.04 -> '4%', 0.025 -> '2.5%', 0.3333 -> '33.3%' (at most one decimal, no trailing 0)."""
    v = round(100 * x, 1)
    return f"{v:.0f}%" if v == int(v) else f"{v:.1f}%"


def scope_words(cohort: str) -> str:
    return "" if cohort == DEFAULT_COHORT else f" for {cohort}"


# ---------------------------------------------------------------- change items
@dataclass(frozen=True)
class ChangeItem:
    """One plain-language line of the batch, with the values behind it."""

    path: tuple[str, ...]
    text: str
    before: Any
    after: Any


def _alloc_line(dim: str, cohort: str, a: Mapping[str, float], b: Mapping[str, float],
                override_removed: bool) -> str:
    head = f"{DIM_WORDS[dim]}{scope_words(cohort)}: "
    if override_removed:
        return head + "use the default mix again"
    rises = sorted((o for o in set(a) | set(b) if b.get(o, 0) > a.get(o, 0) + 1e-12),
                   key=lambda o: (-(b.get(o, 0) - a.get(o, 0)), o))
    falls = sorted((o for o in set(a) | set(b) if b.get(o, 0) < a.get(o, 0) - 1e-12),
                   key=lambda o: (-(a.get(o, 0) - b.get(o, 0)), o))
    if dim == "caller_id" and len(falls) == 1 and len(rises) >= 1:
        moved = a.get(falls[0], 0) - b.get(falls[0], 0)
        return head + f"move {pct(moved)} from {falls[0]} to " + " and ".join(rises)
    parts = []
    for o in rises:
        if a.get(o, 0) <= 1e-12:
            parts.append(f"try {o} on {pct(b[o])} of calls (new test)")
        else:
            parts.append(f"raise {o} from {pct(a[o])} to {pct(b[o])}")
    for o in falls:
        parts.append(f"stop {o} (was {pct(a[o])})" if b.get(o, 0) <= 1e-12
                     else f"lower {o} from {pct(a[o])} to {pct(b[o])}")
    return head + "; ".join(parts)


def _hours_line(cohort: str, a: Mapping[int, float] | None, b: Mapping[int, float] | None) -> str:
    head = f"Call times{scope_words(cohort)}: "
    if b is None:
        return head + "remove the hour table (every hour weighted equally again)"
    top = sorted(b, key=lambda h: (-b[h], h))[:3]
    word = "new" if a is None else "update"
    return head + f"{word} hour weights, highest at " + ", ".join(f"{h}:00" for h in sorted(top))


def _cohort_line(a: Config, b: Config) -> str:
    old, new = list(a.cohort_ids()), list(b.cohort_ids())
    added = [c for c in new if c not in old]
    removed = [c for c in old if c not in new]
    changed = [c.id for c in b.cohorts if c.id in old and c != a.cohorts[old.index(c.id)]]
    parts = []
    if added:
        parts.append("add " + ", ".join(added))
    if removed:
        parts.append("remove " + ", ".join(removed))
    if changed:
        parts.append("redefine " + ", ".join(changed))
    if not parts:
        parts.append("reorder (first match wins)")
    return "Cohorts: " + "; ".join(parts)


def change_items(live: Config, proposed: Config,
                 evidence_by_path: Mapping[tuple[str, ...], Mapping[str, Any]] | None = None
                 ) -> list[ChangeItem]:
    """One item per changed overlay path, in path order (cohorts, allocations, schedule,
    prompt), comparing the live config with the proposed one as the simulator will store it."""
    ev = evidence_by_path or {}
    out: list[ChangeItem] = []
    for path in diff_paths(live, proposed):
        kind = path[0]
        if kind == "cohorts":
            out.append(ChangeItem(path, _cohort_line(live, proposed),
                                  [c.body() for c in live.cohorts],
                                  [c.body() for c in proposed.cohorts]))
        elif kind == "alloc":
            _, k, dim = path
            a, b = live.dist(k, dim), proposed.dist(k, dim)
            if a == b and k != DEFAULT_COHORT:
                continue        # an override equal to what it replaces: no effective change
            removed = (k != DEFAULT_COHORT and dim in (live.allocations.get(k) or {})
                       and dim not in (proposed.allocations.get(k) or {}))
            out.append(ChangeItem(path, _alloc_line(dim, k, a, b, removed), dict(a), dict(b)))
        elif kind == "hour_weights":
            a, b = live.hour_weights.get(path[1]), proposed.hour_weights.get(path[1])
            out.append(ChangeItem(path, _hours_line(path[1], a, b),
                                  None if a is None else {str(h): w for h, w in a.items()},
                                  None if b is None else {str(h): w for h, w in b.items()}))
        elif kind == "prompt":
            run = (ev.get(path) or {}).get("validation_run_id")
            extra = f" (validation run {run})" if run else ""
            out.append(ChangeItem(path, f"Prompt: switch from {live.prompt_version} to "
                                        f"{proposed.prompt_version}{extra}",
                                  live.prompt_version, proposed.prompt_version))
        elif kind == "lead_preferred_hour":
            n = len(set(live.lead_preferred_hour.items()) ^ set(proposed.lead_preferred_hour.items()))
            out.append(ChangeItem(path, f"Per-person call times: {n} entries changed",
                                  len(live.lead_preferred_hour), len(proposed.lead_preferred_hour)))
    return out


def change_summary(items: Sequence[ChangeItem], supervisor_notes: Sequence[str] = ()) -> str:
    """The one line a supervisor reads first: every item, then the supervisor notes."""
    if not items:
        return ""
    head = items[0].text if len(items) == 1 else f"{len(items)} changes: " + "; ".join(
        i.text for i in items)
    if supervisor_notes:
        head += ". Note: this " + "; ".join(supervisor_notes)
    return head


# ---------------------------------------------------------------- affected leads (6.6)
def effective_signature(cfg: Config, features: Mapping[str, str]) -> tuple[Any, ...]:
    """Everything a lead of these features gets from a config: its dists, hour table, prompt."""
    k = cfg.cohort_of(features)
    table = cfg.hour_table(k)
    return (tuple(tuple(sorted(cfg.dist(k, d).items())) for d in AGENT_DIMS),
            None if table is None else tuple(sorted(table.items())), cfg.prompt_version)


def changed_cells(live: Config, proposed: Config, cells: Iterable[Cell]) -> list[int]:
    return [c.cell_id for c in cells
            if effective_signature(live, c.features()) != effective_signature(proposed,
                                                                              c.features())]


def affected_leads(changed: Iterable[int], active_by_cell: Mapping[int, int], batch_size: int,
                   hist_share_by_cell: Mapping[int, float], next_campaign_only_alloc: bool
                   ) -> int:
    """DESIGN.md 6.6: active leads now in the changed cells (unless the change only reaches new
    campaigns) plus the next 24h batch share of those cells. An integer, as the API requires."""
    ch = list(changed)
    batch = batch_size * sum(hist_share_by_cell.get(c, 0.0) for c in ch)
    active = 0 if next_campaign_only_alloc else sum(active_by_cell.get(c, 0) for c in ch)
    return int(round(active + batch))


def leads_hearing_different_variant(live: Config, proposed: Config, cells: Iterable[Cell],
                                    leads_by_cell: Mapping[int, float]) -> int:
    """sum over cells of leads x P(the lead hears a different variant), with the dims coupled
    independently: 1 - prod_d (1 - TV_d). A 4% probe touches every lead in scope but changes
    what only 4% of them hear."""
    total = 0.0
    for c in cells:
        f = c.features()
        ka, kb = live.cohort_of(f), proposed.cohort_of(f)
        same = 1.0
        for d in AGENT_DIMS:
            same *= 1.0 - tv(live.dist(ka, d), proposed.dist(kb, d))
        total += leads_by_cell.get(c.cell_id, 0.0) * (1.0 - same)
    return int(round(total))


# ---------------------------------------------------------------- dry run (6.6)
@dataclass(frozen=True)
class KpiReplay:
    kpi: str
    mean_rel: float
    ci90: tuple[float, float]
    p_harm: float
    q20_rel: float
    n_draws: int

    def to_json(self) -> dict[str, Any]:
        return {"kpi": self.kpi, "mean_rel": round(self.mean_rel, 6),
                "ci90": [round(self.ci90[0], 6), round(self.ci90[1], 6)],
                "p_harm": round(self.p_harm, 6), "q20_rel": round(self.q20_rel, 6),
                "n_draws": self.n_draws}


def _summarise(kpi: str, rel: np.ndarray, line: float) -> KpiReplay:
    return KpiReplay(kpi=kpi, mean_rel=float(rel.mean()),
                     ci90=(float(np.quantile(rel, 0.05)), float(np.quantile(rel, 0.95))),
                     p_harm=float((rel < line).mean()), q20_rel=float(np.quantile(rel, 0.2)),
                     n_draws=int(rel.shape[0]))


def _mix_vector(cells: Sequence[Cell], mix: Mapping[int, float],
                base: Mapping[int, float] | None) -> np.ndarray:
    m = np.array([mix.get(c.cell_id, 0.0) * (1.0 if base is None else base.get(c.cell_id, 1.0))
                  for c in cells], dtype=float)
    if m.sum() <= 0:
        raise ValueError("dry run needs a positive next-24h mix")
    return m


def replay_engagement(live: Config, proposed: Config, cells: Sequence[Cell],
                      mix: Mapping[int, float], draws: Draws,
                      base_engagement: Mapping[int, float] | None = None) -> KpiReplay:
    """Stratified posterior replay of the engagement change (see module docstring)."""
    col = {cid: j for j, cid in enumerate(draws.index.cell_ids)}
    m = _mix_vector(cells, mix, base_engagement)
    n = draws.params.shape[0]
    e_old = np.ones((n, len(cells)))
    e_new = np.ones((n, len(cells)))
    for dim in ENGAGEMENT_DIMS:
        opts = sorted({o for c in cells for cfg in (live, proposed)
                       for o in cfg.dist(cfg.cohort_of(c.features()), dim)})
        ratio = {o: np.exp(draws.cell_effects(dim, o)) for o in opts}
        f_old = np.zeros((n, len(cells)))
        f_new = np.zeros((n, len(cells)))
        for i, c in enumerate(cells):
            j = col.get(c.cell_id)
            for cfg, f in ((live, f_old), (proposed, f_new)):
                dist = cfg.dist(cfg.cohort_of(c.features()), dim)
                for o, w in dist.items():
                    f[:, i] += w * (ratio[o][:, j] if j is not None else 1.0)
        e_old *= f_old
        e_new *= f_new
    rel = (e_new @ m) / (e_old @ m) - 1.0
    return _summarise("engagement_rate", rel, HARM_LINE_E)


def replay_caller_id(live: Config, proposed: Config, cells: Sequence[Cell],
                     mix: Mapping[int, float], did_post: DidPosterior) -> KpiReplay:
    """Relative caller-ID pickup change dd from the per-DID draws (6.6)."""
    m = _mix_vector(cells, mix, None)
    idx = {d: j for j, d in enumerate(did_post.dids)}
    n = did_post.draws.shape[0]
    h_old = np.zeros((n, len(cells)))
    h_new = np.zeros((n, len(cells)))
    for i, c in enumerate(cells):
        for cfg, h in ((live, h_old), (proposed, h_new)):
            for d, w in cfg.dist(cfg.cohort_of(c.features()), "caller_id").items():
                h[:, i] += w * (did_post.draws[:, idx[d]] if d in idx else 1.0)
    rel = (h_new @ m) / (h_old @ m) - 1.0
    return _summarise("pickup_rate", rel, HARM_LINE_D)


# ---------------------------------------------------------------- impact score (ADR 0020)
def clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def prompt_fix_impact(flag_rate_now: float, flag_rate_baseline: float, e_unflagged: float,
                      e_flagged: float, e_now: float) -> float:
    """Measured engagement deficit of a hallucination incident on our calls (6.6 table):
    (flag_rate_now - flag_rate_baseline) x (e_unflagged - e_flagged) / e_now, clipped."""
    if e_now <= 0:
        return 0.0
    return clip01((flag_rate_now - flag_rate_baseline) * (e_unflagged - e_flagged) / e_now)


def lever_of(patch: Patch) -> str:
    return patch.path[2] if patch.path[0] == "alloc" else patch.path[0]


@dataclass(frozen=True)
class LeverScore:
    lever: str
    kpi: str
    score: float
    source: str


# ---------------------------------------------------------------- the package
@dataclass(frozen=True)
class BuildContext:
    """Inputs of `build_request` beyond the composition. Everything optional degrades to an
    honest 'not estimated' entry, never to an invented number."""

    primary_kpi: str
    batch_size: int
    mid_flight: str
    cells: Sequence[Cell] = ()
    active_by_cell: Mapping[int, int] = field(default_factory=FrozenDict)
    hist_share_by_cell: Mapping[int, float] = field(default_factory=FrozenDict)
    mix: Mapping[int, float] = field(default_factory=FrozenDict)
    base_engagement: Mapping[int, float] | None = None
    draws: Draws | None = None
    did_post: DidPosterior | None = None
    guardrail: GuardrailForecast | None = None
    schedule_q20_rel: float | None = None
    """Q20 of the relative pickup change of a schedule change (5.8, from T4)."""
    prompt_impact: float | None = None
    """`prompt_fix_impact(...)` for a prompt fix (from the incident manager)."""
    harm: HarmReport | None = None
    supervisor_notes: Sequence[str] = ()
    window: Mapping[str, Any] = field(default_factory=FrozenDict)
    """{"from": "D2T00:00", "to": "D4T00:00", "connected": 1912}"""
    assumptions: Sequence[str] = ("no welcome x voice interaction (A-1, not tested)",
                                  "hour does not change engagement")


@dataclass(frozen=True)
class RequestPackage:
    """Everything `POST /approvals` needs besides ids and the config body."""

    change_summary: str
    items: tuple[ChangeItem, ...]
    diff: FrozenDict
    affected_leads: int
    dry_run: FrozenDict
    impact_score: float
    impact_kpi: str
    lever_scores: tuple[LeverScore, ...]
    rationale: str
    evidence: FrozenDict

    def approval_request(self, proposal_id: str, workflow_id: str, base_version: int,
                         proposed: Config) -> ApprovalRequest:
        return ApprovalRequest(proposal_id=proposal_id, workflow_id=workflow_id,
                               base_version=base_version, proposed_config=proposed,
                               change_summary=self.change_summary, diff=self.diff,
                               affected_leads=self.affected_leads, dry_run=self.dry_run,
                               impact_score=self.impact_score, rationale=self.rationale,
                               evidence=self.evidence)


def _lever_scores(comp: Composition, ctx: BuildContext, eng: KpiReplay | None,
                  did: KpiReplay | None) -> list[LeverScore]:
    out: list[LeverScore] = []
    seen: set[str] = set()
    for p in sorted(comp.patches, key=lambda p: (p.cls, p.patch_id)):
        lever = lever_of(p)
        if lever in seen:
            continue
        seen.add(lever)
        kpi = KPI_OF_LEVER.get(lever) or ctx.primary_kpi
        if lever in ENGAGEMENT_DIMS:
            score, src = (clip01(eng.q20_rel), "dry run Q20") if eng else (0.0, "no posterior")
        elif lever == "caller_id":
            score, src = (clip01(did.q20_rel), "DID posterior Q20") if did else (0.0, "no DID posterior")
        elif lever == "hour_weights":
            score, src = ((clip01(ctx.schedule_q20_rel), "schedule expectation Q20")
                          if ctx.schedule_q20_rel is not None else (0.0, "not estimated"))
        elif lever == "prompt":
            score, src = ((clip01(ctx.prompt_impact), "measured incident deficit")
                          if ctx.prompt_impact is not None else (0.0, "not estimated"))
        else:
            score, src = 0.0, "structural change (cohorts): buys targeting, no direct effect"
        out.append(LeverScore(lever, kpi, round(score, 6), src))
    return out


def _dry_run(comp: Composition, ctx: BuildContext, stored: Config,
             eng: KpiReplay | None, did: KpiReplay | None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "method": "stratified posterior replay over recorded calls, cells weighted by "
                  "next-24h mix",
        "window": thaw(ctx.window) or {"note": "no replay window (no posterior inputs)"},
        "assumptions": list(ctx.assumptions),
    }
    if eng is not None:
        d["primary" if ctx.primary_kpi == "engagement_rate" else "engagement"] = eng.to_json()
    if did is not None:
        d["primary" if ctx.primary_kpi == "pickup_rate" else "caller_id"] = did.to_json()
    if "primary" not in d:
        d["primary"] = {"kpi": ctx.primary_kpi,
                        "note": "not estimated for this lever; see components"}
    if ctx.guardrail is not None:
        g = ctx.guardrail
        d["guardrail"] = {"kpi": g.kpi, "current": round(g.current, 6),
                          "proposed": round(g.proposed, 6), "max_drop_abs": g.max_drop_abs,
                          "p_breach": round(g.p_breach, 6)}
    if ctx.cells:
        leads = {c: ctx.active_by_cell.get(c, 0) + ctx.batch_size * ctx.hist_share_by_cell.get(c, 0)
                 for c in (x.cell_id for x in ctx.cells)}
        d["leads_hearing_a_different_variant_24h"] = leads_hearing_different_variant(
            comp.live, stored, ctx.cells, leads)
    d["tv_by_cohort_dim"] = {f"{k}/{dim}": round(v, 6)
                             for (k, dim), v in sorted(comp.tv_by_cohort_dim.items())}
    return d


def _rationale(comp: Composition, ctx: BuildContext) -> str:
    lines = [f"Class P{comp.cls}" + (f" (P0{'ab'[comp.p0_rank]})" if comp.p0_rank is not None
                                      else "") + f", base version {comp.base_version}."]
    for p in sorted(comp.patches, key=lambda p: (p.cls, p.patch_id)):
        ev = dict(p.evidence or {})
        rule = ev.pop("rule_id", "-")
        facts = ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(ev.items())
                          if isinstance(v, (int, float, str, bool)))
        lines.append(f"{path_text(p.path).capitalize()}: {p.owner}, rule {rule}, "
                     f"decision #{p.decision_id}" + (f" ({facts})" if facts else "") + ".")
    if ctx.harm is not None:
        h = ctx.harm
        lines.append(f"Harm gate {'passed' if h.passes else 'FAILED'}: P(de < -1%) = "
                     f"{h.p_de_below_1pct:.4f}, P(dd < -2%) = {h.p_dd_below_2pct:.4f}, "
                     f"alpha {h.alpha_used}" + (f"; failed {list(h.failed)}" if h.failed else "")
                     + ".")
    return " ".join(lines)


def _fmt(v: Any) -> str:
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def build_request(comp: Composition, ctx: BuildContext) -> RequestPackage:
    """The approval package for a composition (DESIGN.md 6.6)."""
    stored = comp.proposed.as_stored()
    ev_by_path = {p.path: p.evidence for p in comp.patches}
    items = change_items(comp.live, stored, ev_by_path)
    eng = did = None
    if ctx.draws is not None and ctx.cells and any(d in comp.changed_dims for d in ENGAGEMENT_DIMS):
        eng = replay_engagement(comp.live, stored, ctx.cells, ctx.mix, ctx.draws,
                                ctx.base_engagement)
    if ctx.did_post is not None and ctx.cells and "caller_id" in comp.changed_dims:
        did = replay_caller_id(comp.live, stored, ctx.cells, ctx.mix, ctx.did_post)
    scores = _lever_scores(comp, ctx, eng, did)
    head = scores[0] if scores else LeverScore("none", ctx.primary_kpi, 0.0, "no change")
    alloc_only = all(p.path[0] in ("alloc", "cohorts") for p in comp.patches)
    affected = affected_leads(changed_cells(comp.live, stored, ctx.cells), ctx.active_by_cell,
                              ctx.batch_size, ctx.hist_share_by_cell,
                              alloc_only and ctx.mid_flight == "next_campaign_only")
    dry = _dry_run(comp, ctx, stored, eng, did)
    dry["impact"] = {"kpi": head.kpi, "score": head.score, "source": head.source,
                     "per_lever": [{"lever": s.lever, "kpi": s.kpi, "score": s.score}
                                   for s in scores]}
    diff = {"base_version": comp.base_version, "changed_dims": list(comp.changed_dims),
            "items": [{"path": list(i.path), "change": i.text, "before": thaw(i.before),
                       "after": thaw(i.after)} for i in items]}
    evidence: dict[str, Any] = {"class": comp.cls, "p0_rank": comp.p0_rank,
                                "patch_ids": [p.patch_id for p in comp.patches],
                                "decision_ids": sorted({p.decision_id for p in comp.patches})}
    for p in comp.patches:
        run = (p.evidence or {}).get("validation_run_id")
        if p.path == ("prompt",) and run:
            evidence["validation_run_id"] = run
    return RequestPackage(
        change_summary=change_summary(items, ctx.supervisor_notes), items=tuple(items),
        diff=freeze(diff), affected_leads=affected, dry_run=freeze(dry),
        impact_score=head.score, impact_kpi=head.kpi, lever_scores=tuple(scores),
        rationale=_rationale(comp, ctx), evidence=freeze(evidence))
