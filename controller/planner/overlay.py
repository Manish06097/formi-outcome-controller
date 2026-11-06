"""Overlay: desired config = live config + patches; path ownership, rebase, cooldown.

Owner: track T2 (milestone M3). Implements DESIGN.md 6.7 (supervisor edits: rebase, never
revert) and the compose step of 6.2 (one proposal per workflow carries the whole change set).
ADR 0015.

A patch is owned by one module and covers one path:
    ("alloc", cohort, dim)        value: raw weights {option: w}, or None to drop the override
    ("hour_weights", cohort)      value: {hour: w}, or None to drop the table
    ("prompt",)                   value: a prompt_version
    ("cohorts",)                  value: the full ordered cohort list [{"id", "where"}, ...]
`compose()` copies the live config and applies the chosen patches in a fixed order (cohorts,
allocations, schedule, prompt), drops overrides of cohorts that no longer exist, and labels the
result with the policy replica (changed dims, TV per cohort and dim, risk).

A supervisor edit (`rebase_on_supervisor_edit`):
1. diffs the previous known body against the new one into supervisor paths (override
   distributions compared with a 1e-9 tolerance: the simulator re-normalises every distribution
   on a supervisor edit, so untouched ones can drift in their last bits);
2. invalidates every patch on those paths or on a removed cohort, and reports the experiments
   behind them as interrupted;
3. reports in-flight proposals based on an older version (the lifecycle marks them STALE);
4. starts a cooldown on each path (no P2/P3 patch may touch it) and records the supervisor as
   the path owner;
5. rebases as intent: other cohorts' overrides that still give share to the old control get the
   supervisor's new control instead, except where it is credibly worse there (kept and listed).

State lives in the `patch`, `path_owner` and `cooldown` tables; `rows()` and `Overlay.restore()`
convert to and from them, so a restarted controller rebuilds the overlay from the store.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from controller.governance.policy import classify_risk, tv_by_cohort_dim
from controller.settings import Thresholds
from controller.types import (AGENT_DIMS, DEFAULT_COHORT, P0_INCIDENT, CohortDef, Composition,
                              Config, FrozenDict, Patch, PatchRequest, Proposal, RebaseReport,
                              Substitution, WorkflowSpec, freeze, label, thaw)

Path = tuple[str, ...]
PATH_ORDER = {"cohorts": 0, "alloc": 1, "hour_weights": 2, "prompt": 3}
OPEN = "open"
PROPOSED = "proposed"
APPLIED = "applied"
INVALIDATED = "invalidated_by_supervisor"
HELD = "held"
DROPPED = "dropped"
REPLACED = "replaced"
REFUSED = "refused"
LIVE_STATES = frozenset({OPEN, PROPOSED, HELD})
DIST_TOL = 1e-9


class CompositionInvalid(ValueError):
    """The composed config fails the simulator's 422 checks (a planner bug: page)."""

    def __init__(self, wf: str, problems: Sequence[str], patch_ids: Sequence[int]):
        super().__init__(f"{wf}: composed config invalid: {'; '.join(problems)}")
        self.wf, self.problems, self.patch_ids = wf, tuple(problems), tuple(patch_ids)


@dataclass(frozen=True)
class OverlayContext:
    """What compose needs besides the patches, for one workflow at one step."""

    spec: WorkflowSpec
    did_state: Mapping[str, Any] = field(default_factory=FrozenDict)
    controls: Mapping[tuple[str, str], str] = field(default_factory=FrozenDict)
    """(cohort, dim) -> control option (the incumbent, or the promoted winner in its scope)"""
    history: Sequence[Config] = ()
    """configs live in the schedule-revert lookback (7 days), for classify_risk"""


@dataclass(frozen=True)
class PathOwner:
    owner: str
    since_version: int | None
    since_minute: int | None


@dataclass(frozen=True)
class Cooldown:
    until_minute: int
    source_version: int | None
    reason: str


@dataclass(frozen=True)
class ComposeReport:
    """Patches compose left out, and why (dependency on a removed cohort, cooldown, hold)."""

    wf: str
    used: tuple[int, ...]
    skipped: tuple[tuple[int, str], ...]


class CredibilityCheck(Protocol):
    def __call__(self, wf: str, cohort: str, dim: str, option: str) -> bool: ...


def _never_worse(wf: str, cohort: str, dim: str, option: str) -> bool:
    return False


# ---------------------------------------------------------------- paths and diffs
def validate_path(path: Path) -> None:
    ok = ((len(path) == 3 and path[0] == "alloc" and path[2] in AGENT_DIMS and path[1])
          or (len(path) == 2 and path[0] == "hour_weights" and path[1])
          or path in (("prompt",), ("cohorts",)))
    if not ok:
        raise ValueError(f"not an overlay path: {path!r}")


def _dist_differs(a: Mapping[str, float] | None, b: Mapping[str, float] | None) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    if set(a) != set(b):
        return True
    return any(abs(a[o] - b[o]) > DIST_TOL for o in a)


def diff_paths(old: Config | None, new: Config) -> tuple[Path, ...]:
    """Overlay paths whose value differs between two bodies. old None: every path of new."""
    if old is None:
        return tuple(p for p in _all_paths(new))
    out: list[Path] = []
    if old.cohorts != new.cohorts:
        out.append(("cohorts",))
    for k in sorted(set(old.allocations) | set(new.allocations)):
        for d in AGENT_DIMS:
            if _dist_differs((old.allocations.get(k) or {}).get(d),
                             (new.allocations.get(k) or {}).get(d)):
                out.append(("alloc", k, d))
    for k in sorted(set(old.hour_weights) | set(new.hour_weights)):
        if old.hour_weights.get(k) != new.hour_weights.get(k):
            out.append(("hour_weights", k))
    if old.prompt_version != new.prompt_version:
        out.append(("prompt",))
    if old.lead_preferred_hour != new.lead_preferred_hour:
        out.append(("lead_preferred_hour",))
    return tuple(out)


def _all_paths(cfg: Config) -> Iterable[Path]:
    yield ("cohorts",)
    for k in sorted(cfg.allocations):
        for d in AGENT_DIMS:
            if d in cfg.allocations[k]:
                yield ("alloc", k, d)
    for k in sorted(cfg.hour_weights):
        yield ("hour_weights", k)
    yield ("prompt",)


def path_text(path: Path) -> str:
    """Plain words for a path ("the voice for cohort age60p")."""
    if path[0] == "alloc":
        what = {"welcome_message": "welcome message", "voice": "voice",
                "caller_id": "caller ID"}[path[2]]
        return f"the {what}" + ("" if path[1] == DEFAULT_COHORT else f" for {path[1]}")
    if path[0] == "hour_weights":
        return "the call times" + ("" if path[1] == DEFAULT_COHORT else f" for {path[1]}")
    return {"prompt": "the prompt", "cohorts": "the cohorts",
            "lead_preferred_hour": "the per-person call times"}.get(path[0], path[0])


def dominant(dist: Mapping[str, float]) -> str | None:
    """The option with the largest share (ties: first in sorted order)."""
    if not dist:
        return None
    return max(sorted(dist), key=lambda o: dist[o])


# ---------------------------------------------------------------- applying patches
def apply_patch(cfg: Config, path: Path, value: Any) -> Config:
    kind = path[0]
    if kind == "cohorts":
        cohorts = tuple(c if isinstance(c, CohortDef) else CohortDef.from_api(c)
                        for c in (value or ()))
        out = cfg.with_cohorts(cohorts)
        return _drop_orphans(out)
    if kind == "alloc":
        _, cohort, dim = path
        if value is None and cohort == DEFAULT_COHORT:
            raise ValueError("the default allocation cannot be removed")
        return cfg.with_allocation(cohort, dim, dict(value or {}))
    if kind == "hour_weights":
        return cfg.with_hour_weights(path[1], None if value is None else
                                     {int(h): float(w) for h, w in dict(value).items()})
    if kind == "prompt":
        return cfg.with_prompt(str(value))
    raise ValueError(f"not an overlay path: {path!r}")


def _drop_orphans(cfg: Config) -> Config:
    """Remove overrides and hour tables of cohorts that are not defined any more."""
    ids = set(cfg.cohort_ids()) | {DEFAULT_COHORT}
    allocs = FrozenDict({k: v for k, v in cfg.allocations.items() if k in ids})
    hours = FrozenDict({k: v for k, v in cfg.hour_weights.items() if k in ids})
    wire = FrozenDict({k: v for k, v in cfg.wire.items() if k[0] in ids})
    return replace(cfg, allocations=allocs, hour_weights=hours, wire=wire)


def _depends_on(path: Path) -> str | None:
    if path[0] in ("alloc", "hour_weights") and path[1] != DEFAULT_COHORT:
        return path[1]
    return None


# ---------------------------------------------------------------- the overlay
class Overlay:
    """Patches per workflow, path ownership and supervisor cooldowns (DESIGN.md 6.7)."""

    def __init__(self, context: Callable[[str], OverlayContext], clock: Callable[[], int],
                 thresholds: Thresholds | None = None,
                 in_flight: Callable[[str], Sequence[Proposal]] | None = None,
                 credibly_worse: CredibilityCheck = _never_worse):
        self.context = context
        self.clock = clock
        self.t = thresholds or Thresholds()
        self.in_flight = in_flight or (lambda wf: ())
        self.credibly_worse = credibly_worse
        self.patches: dict[int, Patch] = {}
        self.owners: dict[tuple[str, Path], PathOwner] = {}
        self.cooldowns: dict[tuple[str, Path], Cooldown] = {}
        self.supervisor_cohorts: dict[str, frozenset[str]] = {}
        self.last_compose: dict[str, ComposeReport] = {}
        self._next_id = 1

    # ------------------------------------------------------------ patches
    def add(self, req: PatchRequest) -> Patch:
        """Accept a patch. A newer patch on the same path replaces an open one; a patch from a
        different module on a path another module still owns is refused unless its class is
        strictly higher (more urgent). Cooldown is applied at compose time, so a P2/P3 patch on
        a supervisor path waits instead of being lost.

        The request must carry the id of the decision that produced it (PatchRequest docstring,
        ADR 0028): a patch never exists without its decision row."""
        validate_path(tuple(req.path))
        if (req.cls == P0_INCIDENT) != (req.p0_rank is not None):
            raise ValueError("p0_rank is set exactly for class 0 patches")
        if req.decision_id is None:
            raise ValueError(f"{req.owner} patch on {req.path} has no decision_id: write the "
                             "decision and stamp the request before Overlay.add")
        path = tuple(req.path)
        state = OPEN
        for old in self._live_on(req.wf, path):
            if old.state == PROPOSED:
                continue        # in flight: the new patch composes on top of its result later
            if old.owner == req.owner or req.cls < old.cls:
                self.patches[old.patch_id] = replace(old, state=REPLACED)
            else:
                state = REFUSED
        patch = Patch(patch_id=self._next_id, wf=req.wf, owner=req.owner, path=path,
                      value=freeze(req.value), cls=req.cls, state=state,
                      decision_id=req.decision_id, p0_rank=req.p0_rank,
                      evidence=freeze(dict(req.evidence)))
        self._next_id += 1
        self.patches[patch.patch_id] = patch
        if state == OPEN:
            owner = self.owners.get((req.wf, path))
            if owner is None or owner.owner != "supervisor":
                self.owners[(req.wf, path)] = PathOwner(req.owner, None, self.clock())
        return patch

    def _live_on(self, wf: str, path: Path) -> list[Patch]:
        return [p for p in self.patches.values()
                if p.wf == wf and p.path == path and p.state in LIVE_STATES]

    def open_patches(self, wf: str) -> list[Patch]:
        return sorted((p for p in self.patches.values() if p.wf == wf and p.state == OPEN),
                      key=lambda p: p.patch_id)

    def set_state(self, patch_ids: Iterable[int], state: str, **kw: Any) -> None:
        for pid in patch_ids:
            self.patches[pid] = replace(self.patches[pid], state=state, **kw)

    def mark_proposed(self, patch_ids: Iterable[int], proposal_id: str) -> None:
        self.set_state(patch_ids, PROPOSED, proposal_id=proposal_id)

    def mark_applied(self, proposal_id: str) -> list[int]:
        ids = [p.patch_id for p in self.patches.values()
               if p.proposal_id == proposal_id and p.state == PROPOSED]
        self.set_state(ids, APPLIED)
        return ids

    def return_to_open(self, proposal_id: str) -> list[int]:
        """REQUEUE: a terminal-unapplied proposal's still-wanted patches go back to the overlay
        (a new proposal with fresh data, rebased on the live config)."""
        ids = [p.patch_id for p in self.patches.values()
               if p.proposal_id == proposal_id and p.state == PROPOSED]
        back = []
        for pid in ids:
            p = self.patches[pid]
            newer = [q for q in self._live_on(p.wf, p.path)
                     if q.state == OPEN and q.patch_id > pid]
            if newer:        # superseded by a newer patch on the same path while in flight
                self.patches[pid] = replace(p, state=REPLACED)
            else:            # proposal_id kept: the proposal it last rode (requeue)
                self.patches[pid] = replace(p, state=OPEN)
                back.append(pid)
        return back

    def drop(self, proposal_id: str) -> list[int]:
        """Drop a proposal's patches ('needs discussion', or no net change)."""
        ids = [p.patch_id for p in self.patches.values()
               if p.proposal_id == proposal_id and p.state in (PROPOSED, OPEN)]
        self.set_state(ids, DROPPED)
        return ids

    def hold_proposal(self, proposal_id: str, until: int) -> list[int]:
        ids = [p.patch_id for p in self.patches.values()
               if p.proposal_id == proposal_id and p.state == PROPOSED]
        self.hold(ids, until)
        return ids

    def hold(self, patch_ids: Iterable[int], until: int) -> None:
        """Held after a fatigue, evidence or format rejection (6.1); re-checked at `until`."""
        for pid in patch_ids:
            p = self.patches[pid]
            self.patches[pid] = replace(p, state=HELD, held_until_minute=until,
                                        hold_count=p.hold_count + 1)

    def release_held(self, now: int, recheck: Callable[[Patch], bool]) -> dict[str, list[int]]:
        """Auto-release (6.1): at the first planning point after `held_until`, re-check each held
        patch; release it, hold it again, or drop it after `held_patch_max_holds` holds."""
        out: dict[str, list[int]] = {"released": [], "held": [], "dropped": []}
        for p in sorted(self.patches.values(), key=lambda p: p.patch_id):
            if p.state != HELD or p.held_until_minute is None or now < p.held_until_minute:
                continue
            if recheck(p):
                self.patches[p.patch_id] = replace(p, state=OPEN, held_until_minute=None)
                out["released"].append(p.patch_id)
            elif p.hold_count >= self.t.held_patch_max_holds:
                self.patches[p.patch_id] = replace(p, state=DROPPED)
                out["dropped"].append(p.patch_id)
            else:
                self.hold([p.patch_id], now + self.t.held_patch_release_min)
                out["held"].append(p.patch_id)
        return out

    # ------------------------------------------------------------ ownership and cooldown
    def mark_supervisor_cohorts(self, wf: str, cohort_ids: Iterable[str]) -> None:
        """Business cohorts are supervisor-owned from the start (ADR 0015): a cohorts patch
        that changes or removes one of them is refused at compose time."""
        self.supervisor_cohorts[wf] = frozenset(cohort_ids)

    def cooldown_active(self, wf: str, path: Path, now: int) -> Cooldown | None:
        c = self.cooldowns.get((wf, path))
        return c if c is not None and now < c.until_minute else None

    def owner(self, wf: str, path: Path) -> PathOwner | None:
        return self.owners.get((wf, path))

    def supervisor_notes(self, wf: str, paths: Iterable[Path]) -> list[str]:
        """'changes the voice your supervisor set on D5 15:00' for every supervisor-owned path
        a proposal touches (6.7 step 4), in path order."""
        notes = []
        for path in sorted(set(paths)):
            o = self.owners.get((wf, path))
            if o is not None and o.owner == "supervisor":
                when = "" if o.since_minute is None else f" on {label(o.since_minute)}"
                notes.append(f"changes {path_text(path)} your supervisor set{when}")
        return notes

    # ------------------------------------------------------------ compose
    def _eligible(self, p: Patch, classes: set[int], now: int) -> str | None:
        if p.cls not in classes:
            return "class not in this release"
        if p.cls >= 2 and self.cooldown_active(p.wf, p.path, now) is not None:
            return "supervisor cooldown"
        return None

    def compose(self, wf: str, live: Config, classes: set[int], *,
                base_version: int = 0,
                paths: Callable[[Path], bool] | None = None) -> Composition:
        """Live config plus every open patch of `wf` in `classes`, in the fixed path order.

        `base_version` is the version `live` was read at (the 11.2 signature has no slot for
        it; the release train passes it). `paths` optionally restricts the patches (the release
        train composes the operational auto-applicable part first, 6.2). Raises
        CompositionInvalid if the result would be refused with 422."""
        now = self.clock()
        ctx = self.context(wf)
        chosen, skipped = [], []
        for p in self.open_patches(wf):
            if paths is not None and not paths(p.path):
                continue
            why = self._eligible(p, classes, now)
            (skipped.append((p.patch_id, why)) if why else chosen.append(p))
        chosen.sort(key=lambda p: (PATH_ORDER[p.path[0]], p.path, p.patch_id))
        cfg = live
        used: list[Patch] = []
        sup_cohorts = self.supervisor_cohorts.get(wf, frozenset())
        for p in chosen:
            if p.path == ("cohorts",) and not self._keeps_supervisor_cohorts(cfg, p.value,
                                                                             sup_cohorts):
                skipped.append((p.patch_id, "changes a supervisor-owned cohort"))
                continue
            dep = _depends_on(p.path)
            if dep is not None and dep not in cfg.cohort_ids():
                skipped.append((p.patch_id, f"cohort {dep} is not defined"))
                continue
            cfg = apply_patch(cfg, p.path, thaw(p.value))
            used.append(p)
        problems = cfg.problems(ctx.spec)
        if problems:
            raise CompositionInvalid(wf, problems, [p.patch_id for p in used])
        stored = cfg.as_stored()
        dims = tuple(live.changed_dims(stored))
        self.last_compose[wf] = ComposeReport(wf, tuple(p.patch_id for p in used),
                                              tuple(skipped))
        cls = min((p.cls for p in used), default=3)
        ranks = [p.p0_rank for p in used if p.cls == P0_INCIDENT and p.p0_rank is not None]
        risk = (classify_risk(live, stored, ctx.did_state, ctx.controls, ctx.history)
                if dims else FrozenDict())
        return Composition(wf=wf, base_version=base_version, live=live, proposed=cfg,
                           patches=tuple(used), changed_dims=dims,
                           tv_by_cohort_dim=tv_by_cohort_dim(live, stored), risk=risk,
                           cls=cls, p0_rank=min(ranks) if ranks else None)

    @staticmethod
    def _keeps_supervisor_cohorts(cfg: Config, value: Any, sup: frozenset[str]) -> bool:
        if not sup:
            return True
        new = {c.id: c for c in (CohortDef.from_api(x) if not isinstance(x, CohortDef) else x
                                 for x in (thaw(value) or ()))}
        old = {c.id: c for c in cfg.cohorts}
        return all(new.get(cid) == old.get(cid) for cid in sup if cid in old)

    # ------------------------------------------------------------ supervisor edits (6.7)
    def rebase_on_supervisor_edit(self, wf: str, old: Config | None, new: Config,
                                  v: int) -> RebaseReport:
        now = self.clock()
        sup_paths = diff_paths(old, new)
        removed = (set(old.cohort_ids()) - set(new.cohort_ids())) if old is not None else set()
        invalidated, experiments = [], []
        for p in sorted(self.patches.values(), key=lambda p: p.patch_id):
            if p.wf != wf or p.state not in LIVE_STATES:
                continue
            if p.path in sup_paths or _depends_on(p.path) in removed:
                self.patches[p.patch_id] = replace(p, state=INVALIDATED, supervisor_touch=v)
                invalidated.append(p.patch_id)
                exp = (p.evidence or {}).get("exp_id")
                if exp and exp not in experiments:
                    experiments.append(str(exp))
        stale = [pr.proposal_id for pr in self.in_flight(wf)
                 if pr.base_version < v and pr.in_flight]
        until = now + self.t.supervisor_cooldown_min
        for path in sup_paths:
            self.cooldowns[(wf, path)] = Cooldown(until, v, f"supervisor edit v{v}")
            self.owners[(wf, path)] = PathOwner("supervisor", v, now)
        subs = self._rebase_controls(wf, old, new, sup_paths) if old is not None else []
        return RebaseReport(wf=wf, version=v, sup_paths=tuple(sup_paths),
                            invalidated_patches=tuple(invalidated),
                            interrupted_experiments=tuple(experiments),
                            stale_proposals=tuple(stale), cooldown_paths=tuple(sup_paths),
                            cooldown_until=until if sup_paths else None,
                            substitutions=tuple(subs))

    def _rebase_controls(self, wf: str, old: Config, new: Config,
                         sup_paths: Sequence[Path]) -> list[Substitution]:
        """Step 5: the supervisor's new default becomes the control in every override that
        still gives share to the old control."""
        subs: list[Substitution] = []
        for dim in AGENT_DIMS:
            if ("alloc", DEFAULT_COHORT, dim) not in sup_paths:
                continue
            c_old = dominant(old.dist(DEFAULT_COHORT, dim))
            c_new = dominant(new.dist(DEFAULT_COHORT, dim))
            if c_old is None or c_new is None or c_old == c_new:
                continue
            for p in sorted(self.patches.values(), key=lambda p: p.patch_id):
                if (p.wf != wf or p.state not in (OPEN, PROPOSED) or p.path[0] != "alloc"
                        or p.path[2] != dim or p.path[1] == DEFAULT_COHORT or p.value is None):
                    continue
                weights = dict(p.value)
                if weights.get(c_old, 0) <= 0:
                    continue
                cohort = p.path[1]
                if self.credibly_worse(wf, cohort, dim, c_new):
                    subs.append(Substitution(p.patch_id, cohort, dim, c_old, c_new, False))
                    continue
                w = weights.pop(c_old)
                weights[c_new] = weights.get(c_new, 0) + w
                self.patches[p.patch_id] = replace(p, value=freeze(weights))
                subs.append(Substitution(p.patch_id, cohort, dim, c_old, c_new, True))
        return subs

    # ------------------------------------------------------------ persistence
    def rows(self) -> dict[str, list[dict[str, Any]]]:
        """Rows for the `patch`, `path_owner` and `cooldown` tables (JSON-friendly)."""
        return {
            "patch": [{**{f.name: thaw(getattr(p, f.name)) for f in dataclasses.fields(p)},
                       "path": list(p.path)} for p in self.patches.values()],
            "path_owner": [{"wf": wf, "path": list(path), "owner": o.owner,
                            "since_version": o.since_version, "since_minute": o.since_minute}
                           for (wf, path), o in self.owners.items()],
            "cooldown": [{"wf": wf, "path": list(path), "until_minute": c.until_minute,
                          "source_version": c.source_version, "reason": c.reason}
                         for (wf, path), c in self.cooldowns.items()],
        }

    @classmethod
    def restore(cls, rows: Mapping[str, Sequence[Mapping[str, Any]]],
                context: Callable[[str], OverlayContext], clock: Callable[[], int],
                **kw: Any) -> "Overlay":
        ov = cls(context, clock, **kw)
        for r in rows.get("patch", ()):
            d = dict(r)
            d["path"] = tuple(d["path"])
            d["value"] = freeze(d.get("value"))
            d["evidence"] = freeze(d.get("evidence") or {})
            p = Patch(**d)
            ov.patches[p.patch_id] = p
            ov._next_id = max(ov._next_id, p.patch_id + 1)
        for r in rows.get("path_owner", ()):
            ov.owners[(r["wf"], tuple(r["path"]))] = PathOwner(r["owner"], r["since_version"],
                                                                r["since_minute"])
        for r in rows.get("cooldown", ()):
            ov.cooldowns[(r["wf"], tuple(r["path"]))] = Cooldown(
                r["until_minute"], r["source_version"], r["reason"])
        return ov
