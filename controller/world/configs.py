"""ConfigMirror: every config version with its body when we can know it, the cohort code of every
cohort, and what changed in each version (the input of the supervisor-edit handler).

Owner: track T1 (milestone M1). Implements DESIGN.md 4.1 step 4 (REFRESH), 3.2
(`config_version`, `config_cohort`), 6.7 step 1 (changed paths) and 9.4 (provenance of versions we
did not write).

The API keeps no past bodies (`config/history` has metadata only), so a body is known in three
ways: `ours` (the body we sent, recorded by governance at apply), `fetched` (GET config returned
that version as current), or `unknown` (two edits inside one step: the earlier body is never
seen). Bodies are recorded as `Config.stored()`, exactly what the simulator stores.

Cohort codes are per workflow and stable across versions (`store.repos.COHORT_CODES_SQL`, shared
with ingest, which may meet a cohort on a call before the mirror sees the body).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from controller.audit.writer import provenance
from controller.simclient.client import SimClient
from controller.store import repos
from controller.store.repos import WorkflowCatalog
from controller.types import (Cell, Config, ConfigChange, ConfigEvent, ConfigSnapshot, SimEpoch,
                              Tx, iter_paths)

log = logging.getLogger(__name__)


def path_value(cfg: Config, path: tuple[str, ...]) -> Any:
    """The value an overlay path addresses (DESIGN.md 6.7): None when the path is absent."""
    if path == ("cohorts",):
        return cfg.cohorts
    if path == ("prompt",):
        return cfg.prompt_version
    if path[0] == "alloc":
        _, cohort, dim = path
        own = (cfg.allocations.get(cohort) or {}).get(dim)
        return dict(own) if own else None
    if path[0] == "hour_weights":
        t = cfg.hour_weights.get(path[1])
        return None if t is None else dict(t)
    raise ValueError(f"unknown path {path}")


def changed_paths(old: Config | None, new: Config) -> tuple[tuple[str, ...], ...]:
    """Overlay paths whose value differs between two bodies (all of the new body's paths when
    the old body is unknown). `lead_preferred_hour` is not an overlay path."""
    new_paths = list(iter_paths(new))
    if old is None:
        return tuple(new_paths)
    seen = set(new_paths)
    paths = new_paths + [p for p in iter_paths(old) if p not in seen]
    return tuple(p for p in paths
                 if _safe_value(old, p) != _safe_value(new, p))


def _safe_value(cfg: Config, path: tuple[str, ...]) -> Any:
    try:
        return path_value(cfg, path)
    except KeyError:
        return None


class ConfigMirror:
    """The `ConfigMirror` interface of DESIGN.md 11.2 over the run schema."""

    def __init__(self, client: SimClient, catalog: WorkflowCatalog, epoch: SimEpoch):
        self.client = client
        self.catalog = catalog
        self.epoch = epoch
        self._bodies: dict[tuple[str, int], Config] = {}
        self._live: dict[str, tuple[int, Config]] = {}
        self._snapshots: dict[str, ConfigSnapshot] = {}

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, tx: Tx) -> None:
        """Record every version the simulator lists and the body of each current version.
        Idempotent; run on a fresh schema before history is ingested, and on every resume."""
        for spec in self.catalog.specs:
            wid = spec.workflow_id
            wf = self.catalog.code(wid)
            repos.record_history(tx, wf, self.client.config_history(wid), self.epoch.minute)
            snap = self.client.config(wid)
            self._record_body(tx, wid, snap.version, snap.actor, snap.config, "fetched",
                              self._minute(snap.applied_at))
            self._live[wid] = (snap.version, snap.config)

    def _minute(self, ts: str | None) -> int | None:
        return None if ts is None else self.epoch.minute(ts)

    # ------------------------------------------------------------------ REFRESH (4.1 step 4)
    def refresh(self, tx: Tx, events: Iterable[ConfigEvent]) -> list[ConfigChange]:
        """Handle this step's config_changed events: one GET config per touched workflow, then
        `on_config_changed` per event in version order."""
        by_wf: dict[str, list[ConfigEvent]] = {}
        for e in events:
            by_wf.setdefault(e.workflow_id, []).append(e)
        changes = []
        for wid in sorted(by_wf):
            self._snapshots[wid] = self.client.config(wid)
            for e in sorted(by_wf[wid], key=lambda e: e.version):
                changes.append(self.on_config_changed(tx, e))
            self._snapshots.pop(wid, None)
        return changes

    def on_config_changed(self, tx: Tx, event: ConfigEvent) -> ConfigChange:
        wid = event.workflow_id
        wf = self.catalog.code(wid)
        snap = self._snapshots.get(wid) or self.client.config(wid)
        known = repos.config_version(tx, wf, event.version)
        if known is not None and known.config is not None:
            body, source = known.config, known.body_source
        elif snap.version == event.version:
            body, source = snap.config, "fetched"
        else:
            body, source = None, "unknown"
        repos.upsert_config_version(tx, wf, event.version, event.actor, None, "unknown",
                                    applied_minute=event.minute, proposal_id=event.proposal_id,
                                    approval_id=event.approval_id,
                                    changed_dims=event.changed_dims)
        if body is not None and source == "fetched":
            self._record_body(tx, wid, event.version, event.actor, body, "fetched", event.minute)
        prev = self.body(wid, event.version - 1, tx)
        if snap.version >= event.version:
            self._live[wid] = (snap.version, snap.config)
        paths = changed_paths(prev, body) if body is not None else ()
        if body is None:
            log.warning("%s v%d: body unknown (a later version was already current)", wid,
                        event.version)
        return ConfigChange(workflow_id=wid, version=event.version, actor=event.actor,
                            body_source=source, config=body, changed_dims=event.changed_dims,
                            changed_paths=paths)

    # ------------------------------------------------------------------ writes
    def record_ours(self, tx: Tx, workflow_id: str, version: int, config: Config, actor: str,
                    applied_minute: int, proposal_id: str, approval_id: str | None,
                    changed_dims: Iterable[str]) -> None:
        """Governance records the body it sent (as the simulator stores it) right after an apply,
        in the same transaction as the proposal transition."""
        wf = self.catalog.code(workflow_id)
        stored = config.as_stored()
        repos.upsert_config_version(tx, wf, version, actor, stored, "ours", applied_minute,
                                    proposal_id, approval_id, tuple(changed_dims))
        self._write_cohorts(tx, wf, version, stored)
        self._bodies[(workflow_id, version)] = stored
        self._live[workflow_id] = (version, stored)

    def record_provenance(self, tx: Tx, workflow_id: str, version: int, config: Config,
                          overrides: dict[tuple[str, str], dict[str, Any]]) -> None:
        """Materialise `allocation_provenance` for a version of ours (DESIGN.md 9.4). Without it
        why-heard reports COHORT_MISMATCH for every call dialled under a controller version.
        `overrides` names the proposal behind each (cohort, dim) the proposal changed; every
        other row carries the default source, as for a supervisor version."""
        provenance(tx, self.catalog.code(workflow_id), version, config,
                   self.catalog.spec(workflow_id), {"source": "default"}, overrides)

    def _record_body(self, tx: Tx, wid: str, version: int, actor: str, config: Config,
                     source: str, applied_minute: int | None) -> None:
        wf = self.catalog.code(wid)
        repos.upsert_config_version(tx, wf, version, actor, config, source, applied_minute)
        self._write_cohorts(tx, wf, version, config)
        self._bodies[(wid, version)] = config
        if not actor.startswith("controller"):
            src = "supervisor" if actor == "supervisor" else "default"
            provenance(tx, wf, version, config, self.catalog.spec(wid), {"source": src})

    def _write_cohorts(self, tx: Tx, wf: int, version: int, config: Config) -> None:
        ids = [c.id for c in config.cohorts]
        rows = [(wf, version, i, cid) for i, cid in enumerate(ids)]
        rows.append((wf, version, len(ids), "default"))
        repos.ensure_cohort_codes(tx, rows)
        self._effective_cells(tx, wf, version, config)

    def _effective_cells(self, tx: Tx, wf: int, version: int, config: Config) -> None:
        """For each cohort of the version, the cells of this workflow whose first match it is."""
        seen = workflow_cells(tx, wf)
        cells: dict[str, list[int]] = {}
        for c in load_cells(tx):
            if c.cell_id not in seen:
                continue
            cells.setdefault(config.cohort_of(c.features()), []).append(c.cell_id)
        for cid, members in cells.items():
            tx.execute("""UPDATE config_cohort SET effective_cells = %s
                          WHERE wf = %s AND version = %s AND cohort_id = %s""",
                       (sorted(members), wf, version, cid))

    def refresh_effective_cells(self, tx: Tx) -> None:
        """Recompute `effective_cells` of every version with a known body (new cells appeared)."""
        for spec in self.catalog.specs:
            wf = self.catalog.code(spec.workflow_id)
            for v in repos.config_versions(tx, wf):
                if v.config is not None:
                    self._effective_cells(tx, wf, v.version, v.config)

    # ------------------------------------------------------------------ reads
    def body(self, wf: str, version: int, tx: Tx | None = None) -> Config | None:
        key = (wf, version)
        if key in self._bodies:
            return self._bodies[key]
        if tx is None or version < 1:
            return None
        row = repos.config_version(tx, self.catalog.code(wf), version)
        if row is not None and row.config is not None:
            self._bodies[key] = row.config
            return row.config
        return None

    def live(self, wf: str) -> tuple[int, Config]:
        if wf not in self._live:
            snap = self.client.config(wf)
            self._live[wf] = (snap.version, snap.config)
        return self._live[wf]

    def live_versions(self) -> Mapping[str, int]:
        return {k: v for k, (v, _) in self._live.items()}


def load_cells(conn: Tx) -> list[Cell]:
    return [Cell(*r) for r in conn.execute(
        "SELECT cell_id, lang, tier, age, value FROM cell ORDER BY cell_id")]


def workflow_cells(conn: Tx, wf: int) -> set[int]:
    """Cells observed in a workflow: dialled (r_pickup) or among its active leads."""
    return {r[0] for r in conn.execute(
        """SELECT cell FROM r_pickup WHERE wf = %s
           UNION SELECT cell FROM lead_dim WHERE wf = %s AND state = 0 AND cell IS NOT NULL""",
        (wf, wf))}


def cell_count(conn: Tx) -> int:
    return int(conn.execute("SELECT count(*) FROM cell").fetchone()[0])
