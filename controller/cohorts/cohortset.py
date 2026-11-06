"""CohortSet: the one shared cohort object with a versioned identity.

Owner: track T4 (milestone M4). Implements DESIGN.md 5.2 ("Identity", "Business cohorts first",
"Minimum sizes") and the `CohortSet` contract of 11.2.

Every module (experiments, detectors, schedule, dry runs, the supervisor view) addresses cohorts
by the same `cohort_key`, and the key IS the `id` written into the simulator config, so the
`cohort_id` on every call event is our key. A changed definition is a new key; nothing is
redefined in place. Business cohorts (from the default config) keep their own ids, stay first,
in order, and are supervisor-owned.

Pure: membership is resolved against a `CellCatalog`; persistence (`cohort_def`,
`config_cohort`) is the caller's job, using `rows()`.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal, Mapping, Sequence

import numpy as np

from controller.cohorts.cells import CellCatalog
from controller.settings import Thresholds
from controller.types import DEFAULT_COHORT, Cohort, CohortDef, FrozenDict, sha256_hex

if TYPE_CHECKING:  # pragma: no cover
    from controller.types import WorkflowSnapshot

Owner = Literal["ours", "supervisor"]

WINNER_SCOPE_RAMP: tuple[float, ...] = (0.038, 0.12, 0.25, 0.30, 0.40, 0.50)
"""Arm share per day along the real ramp of DESIGN.md 5.5 ("about 3.8% on D0, 10% to 15% on D1,
20% to 30% on D2 and D3, then up to the 50% ceiling"), used to project when an arm reaches the
n = 800 look in a cohort. Later days stay at the last value (the arm ceiling). Not a [T]: it is
the harm gate's ramp as the design computed it; see the T4 report (contract change requested to
move it into settings)."""


def _slug(where: Mapping[str, Sequence[str]] | Sequence[tuple[str, Sequence[str]]]) -> str:
    items = where.items() if isinstance(where, Mapping) else where
    parts = []
    short = {"lang": "", "city_tier": "t", "age_band": "age", "value_band": "v"}
    for f, vals in sorted(items):
        vs = "_".join(re.sub(r"[^0-9a-zA-Z]+", "", v.replace("+", "p")) for v in vals)
        parts.append(f"{short.get(f, f)}{vs}")
    s = "_".join(p for p in parts if p) or "all"
    return s[:40]


def canonical_where(where: Sequence[tuple[str, Sequence[str]]]) -> dict[str, list[str]]:
    """Canonical where: features sorted, values sorted (OR within a list is order-free for
    membership; the config keeps the order we write, which is this one)."""
    return {f: sorted(vals) for f, vals in sorted(where)}


def cohort_key(workflow_id: str, where: Sequence[tuple[str, Sequence[str]]], index: int,
               slug: str | None = None) -> str:
    """`<readable slug>~<sha1(workflow_id + canonical where + order index)[:6]>` (DESIGN.md 5.2),
    for example `age60p_ta~3fa2c1`."""
    canon = json.dumps(canonical_where(where), sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1(f"{workflow_id}|{canon}|{index}".encode()).hexdigest()[:6]
    return f"{slug or _slug(where)}~{h}"


@dataclass(frozen=True)
class CohortEntry:
    """One cohort of the set, in first-match order."""

    key: Cohort
    definition: CohortDef
    origin: str
    """grid | business | supervisor | segment"""
    owner: Owner
    matched_cells: frozenset[int]
    """Cells the definition matches on its own (before first-match)."""


class GridCohortSet:
    """The concrete `CohortSet` (types.CohortSet protocol) for one workflow.

    `version` is the cohort-set version: 1 for the D0 grid, +1 for every adopted supervisor
    edit of `cohorts` (regrid is cut, DESIGN.md 5.2).
    """

    def __init__(self, workflow_id: str, entries: Sequence[CohortEntry], catalog: CellCatalog,
                 version: int = 1, end_minute: int | None = None,
                 thresholds: Thresholds | None = None, evidence: Mapping | None = None):
        keys = [e.key for e in entries]
        if len(set(keys)) != len(keys) or DEFAULT_COHORT in keys:
            raise ValueError(f"cohort keys must be unique and not 'default': {keys}")
        self.workflow_id = workflow_id
        self.entries: tuple[CohortEntry, ...] = tuple(entries)
        self.catalog = catalog
        self.version = version
        self.end_minute = end_minute
        self.t = thresholds or Thresholds()
        self.evidence = FrozenDict(evidence or {})
        self._first: dict[int, Cohort] = {}
        self._versions: dict[int, tuple[CohortDef, ...]] = {}
        self._resolve()

    # ------------------------------------------------------------------ membership
    def _resolve(self) -> None:
        self._first = {c: self._first_match(self.to_config(), c) for c in self.catalog.ids()}

    def _first_match(self, cohorts: Sequence[CohortDef], cell: int) -> Cohort:
        feats = self.catalog.get(cell).features()
        for c in cohorts:
            if c.matches(feats):
                return c.id
        return DEFAULT_COHORT

    def refresh_cells(self) -> None:
        """Re-resolve after new cells were added to the catalog (an unseen language)."""
        self.entries = tuple(
            CohortEntry(e.key, e.definition, e.origin, e.owner,
                        self.catalog.matching(e.definition)) for e in self.entries)
        self._resolve()

    def cohort_of(self, cell: int) -> Cohort:
        """First match, business cohorts first; "default" when nothing matches."""
        got = self._first.get(cell)
        if got is None:  # a cell added to the catalog after construction
            got = self._first_match(self.to_config(), cell)
            self._first[cell] = got
        return got

    def effective_cells(self) -> dict[Cohort, frozenset[int]]:
        """Cohort -> cells it actually holds under first-match ("default" included)."""
        out: dict[Cohort, set[int]] = {e.key: set() for e in self.entries}
        out[DEFAULT_COHORT] = set()
        for c in self.catalog.ids():
            out[self.cohort_of(c)].add(c)
        return {k: frozenset(v) for k, v in out.items()}

    def register_version(self, config_version: int, cohorts: Sequence[CohortDef]) -> None:
        """Record the cohort list a config version carried (from the config mirror)."""
        self._versions[config_version] = tuple(cohorts)

    def membership(self, config_version: int) -> Mapping[Cohort, frozenset[int]]:
        """First-match membership under the cohort list of `config_version`
        (`config_cohort.effective_cells`). Unregistered versions raise KeyError."""
        cohorts = self._versions[config_version]
        out: dict[Cohort, set[int]] = {c.id: set() for c in cohorts}
        out[DEFAULT_COHORT] = set()
        for cell in self.catalog.ids():
            out[self._first_match(cohorts, cell)].add(cell)
        return FrozenDict({k: frozenset(v) for k, v in out.items()})

    # ------------------------------------------------------------------ identity and config
    def to_config(self) -> tuple[CohortDef, ...]:
        return tuple(e.definition for e in self.entries)

    def keys(self) -> tuple[Cohort, ...]:
        return tuple(e.key for e in self.entries)

    def key(self) -> str:
        """Identity of the whole set: hash of the ordered definitions (a changed set is a new
        identity, as a changed cohort is a new key)."""
        return sha256_hex([c.body() for c in self.to_config()])[:12]

    def entry(self, cohort: Cohort) -> CohortEntry:
        for e in self.entries:
            if e.key == cohort:
                return e
        raise KeyError(cohort)

    def owner(self, cohort: Cohort) -> Owner:
        """Business and supervisor cohorts are supervisor-owned (DESIGN.md 5.2, 6.7); grid
        cohorts and "default" are ours."""
        if cohort == DEFAULT_COHORT:
            return "ours"
        return self.entry(cohort).owner

    def origin(self, cohort: Cohort) -> str:
        return "default" if cohort == DEFAULT_COHORT else self.entry(cohort).origin

    def rows(self, created_minute: int | None = None) -> list[dict]:
        """`cohort_def` rows for the store."""
        eff = self.effective_cells()
        return [{"cohort_key": e.key, "where_canon": canonical_where(e.definition.where),
                 "def_hash": sha256_hex(e.definition.body())[:16],
                 "cells": sorted(eff[e.key]), "origin": e.origin,
                 "created_minute": created_minute} for e in self.entries]

    def adopt(self, cohorts: Sequence[CohortDef]) -> "GridCohortSet":
        """A new set version from a supervisor's `cohorts` edit. Cohorts we already know keep
        their origin and owner; new ones are supervisor-owned (origin "supervisor")."""
        known = {e.key: e for e in self.entries}
        entries = []
        for c in cohorts:
            old = known.get(c.id)
            if old is not None and old.definition == c:
                entries.append(old)
            else:
                entries.append(CohortEntry(c.id, c, "supervisor", "supervisor",
                                           self.catalog.matching(c)))
        new = GridCohortSet(self.workflow_id, entries, self.catalog, self.version + 1,
                            self.end_minute, self.t, self.evidence)
        new._versions = dict(self._versions)
        return new

    # ------------------------------------------------------------------ scope floors (5.2)
    def connected_share(self, ws: "WorkflowSnapshot", days: int = 7) -> dict[Cohort, float]:
        """Each cohort's share of daily connected calls over the trailing `days` days of
        `r_pickup` that have dials (history included)."""
        per_day = self.connected_per_day(ws, days)
        total = sum(per_day.values())
        return {k: (v / total if total > 0 else 0.0) for k, v in per_day.items()}

    def connected_per_day(self, ws: "WorkflowSnapshot", days: int = 7) -> dict[Cohort, float]:
        pk = ws.pickup
        out = {k: 0.0 for k in (*self.keys(), DEFAULT_COHORT)}
        if len(pk) == 0:
            return out
        day_list = np.unique(pk.sim_day[pk.dials > 0])
        use = day_list[-days:]
        mask = np.isin(pk.sim_day, use)
        for cell, conn in zip(pk.cell[mask].tolist(), pk.connected[mask].tolist()):
            out[self.cohort_of(int(cell))] += float(conn)
        n_days = max(len(use), 1)
        return {k: v / n_days for k, v in out.items()}

    def freeze_start(self) -> int | None:
        """Start of the end-game freeze, `end - max(1 day, 15% of the scenario)` (DESIGN.md 4.2)."""
        if self.end_minute is None:
            return None
        span = max(self.end_minute, 0)
        return self.end_minute - int(max(self.t.freeze_min_days * 1440, self.t.freeze_frac * span))

    def projected_arm_calls(self, connected_per_day: float, now: int) -> float:
        """Connected calls an arm in a cohort with `connected_per_day` reaches before the freeze,
        along the real ramp (`WINNER_SCOPE_RAMP`), starting now."""
        fs = self.freeze_start()
        if fs is None:
            return math.inf
        days = max(0.0, (fs - now) / 1440.0)
        total, d = 0.0, 0
        while days > 0:
            share = WINNER_SCOPE_RAMP[min(d, len(WINNER_SCOPE_RAMP) - 1)]
            total += connected_per_day * share * min(1.0, days)
            days -= 1.0
            d += 1
        return total

    def winner_scope_eligible(self, cohort: Cohort, ws: "WorkflowSnapshot") -> bool:
        """A grid cohort can host a cohort-scope winner iff it holds at least 15% of daily
        connected calls and an arm in it is projected to reach the n = 800 look before the freeze
        (DESIGN.md 5.2, 5.5). Business, supervisor and default cohorts never are."""
        if cohort == DEFAULT_COHORT or self.origin(cohort) != "grid":
            return False
        per_day = self.connected_per_day(ws)
        total = sum(per_day.values())
        if total <= 0:
            return False
        if per_day.get(cohort, 0.0) / total < self.t.winner_scope_min_share:
            return False
        return (self.projected_arm_calls(per_day[cohort], ws.now)
                >= self.t.winner_scope_projected_n)


def business_entries(cohorts: Iterable[CohortDef], catalog: CellCatalog) -> list[CohortEntry]:
    """Business-default cohorts, verbatim and in order, supervisor-owned (DESIGN.md 5.2)."""
    return [CohortEntry(c.id, c, "business", "supervisor", catalog.matching(c)) for c in cohorts]


__all__ = ["GridCohortSet", "CohortEntry", "cohort_key", "canonical_where", "business_entries",
           "WINNER_SCOPE_RAMP"]
