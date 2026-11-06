"""Cell catalog: one cell per observed (lang, city_tier, age_band, value_band) combination.

Owner: track T4 (milestone M4). Implements DESIGN.md 5.2 ("Cells").

A cell is the smallest unit every rollup is keyed by, so any cohort's statistics are sums over
rows. The dictionary is built from observed lead features (an unseen language just adds cells),
never from a scenario file. Ids are dense small integers in first-seen order, which is the order
the ingest assigns them (`cell` table); `CellCatalog.from_rows` rebuilds the catalog from that
table on start. Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from controller.types import FEATURE_NAMES, Cell, CohortDef

FEATURE_ATTR = {"lang": "lang", "city_tier": "tier", "age_band": "age", "value_band": "value"}
"""API feature name -> `Cell` attribute."""


@dataclass
class CellCatalog:
    """The cell dictionary of one run (cells are shared by all workflows; cohorts are not)."""

    cells: dict[int, Cell] = field(default_factory=dict)
    _by_key: dict[tuple[str, str, str, str], int] = field(default_factory=dict, repr=False)

    @classmethod
    def from_rows(cls, rows: Iterable[Cell | Sequence[object]]) -> "CellCatalog":
        """From `cell` table rows `(cell_id, lang, tier, age, value)` or `Cell` values."""
        cat = cls()
        for r in rows:
            c = r if isinstance(r, Cell) else Cell(int(r[0]), str(r[1]), str(r[2]), str(r[3]),
                                                   str(r[4]))
            cat._put(c)
        return cat

    def _put(self, c: Cell) -> None:
        key = (c.lang, c.tier, c.age, c.value)
        if c.cell_id in self.cells or key in self._by_key:
            raise ValueError(f"duplicate cell {c}")
        self.cells[c.cell_id] = c
        self._by_key[key] = c.cell_id

    def add(self, features: Mapping[str, str]) -> int:
        """Id of the cell with these features, creating it (next dense id) if unseen."""
        key = tuple(str(features[f]) for f in FEATURE_NAMES)
        cid = self._by_key.get(key)  # type: ignore[arg-type]
        if cid is not None:
            return cid
        cid = max(self.cells, default=-1) + 1
        self._put(Cell(cid, *key))
        return cid

    def __len__(self) -> int:
        return len(self.cells)

    def __iter__(self):
        return iter(sorted(self.cells))

    def ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.cells))

    def get(self, cell_id: int) -> Cell:
        return self.cells[cell_id]

    def feature(self, cell_id: int, feature: str) -> str:
        return getattr(self.cells[cell_id], FEATURE_ATTR[feature])

    def levels(self, feature: str, cells: Iterable[int] | None = None) -> tuple[str, ...]:
        """Sorted distinct levels of a feature (over `cells`, default every cell)."""
        ids = self.ids() if cells is None else cells
        return tuple(sorted({self.feature(c, feature) for c in ids}))

    def matching(self, cohort: CohortDef, cells: Iterable[int] | None = None) -> frozenset[int]:
        """Cells a cohort definition matches (AND across features, OR within a list)."""
        ids = self.ids() if cells is None else cells
        return frozenset(c for c in ids if cohort.matches(self.cells[c].features()))


__all__ = ["CellCatalog", "FEATURE_ATTR"]
