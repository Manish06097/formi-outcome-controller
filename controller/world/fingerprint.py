"""World identity: the fingerprint, the resume consistency checks and run-schema resolution.

Owner: track T1 (milestone M1). Implements DESIGN.md 8.5 (F9) and restart step 1 of section 2.

The simulator keeps its world in memory and has no run id, so a restarted simulator, or a re-run
of the same scenario, looks exactly like the old one by name. Two layers separate them:

1. `fingerprint` = sha256 of what the API says the world is: scenario name, end minute, tick, every
   workflow's (id, tenant, governance, options), and history chunk 0's metadata. Runs with another
   fingerprint are never candidates.
2. A candidate schema with the same fingerprint (status not done, newest first) is resumed only
   if our store is consistent with the live simulator: our clock is not ahead of it, our chunks
   and event ids are a prefix of its own (the newest one matching chunk for chunk), every
   approval we hold exists with the same proposal id, every controller version we recorded is in
   its config history with the same proposal id, and no workflow is behind our highest version.

A restarted simulator fails these (its clock, ids and chunks are behind ours), so it gets a
fresh schema `run_<fp12>_<n+1>` and the old data stays for audit. A restarted controller against
the same live world passes them and resumes. The advisory lock is taken before this runs
(DESIGN.md 2), so resolution is single-owner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from controller.simclient.client import SimClient
from controller.store import repos
from controller.store.db import Database
from controller.types import ChunkMeta, SimState, WorkflowSpec, sha256_hex

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorldIdentity:
    state: SimState
    specs: tuple[WorkflowSpec, ...]
    history0: ChunkMeta | None
    fingerprint: str


def fingerprint(state: SimState, specs: Sequence[WorkflowSpec],
                history0: ChunkMeta | None) -> str:
    """sha256 of the world's API-visible identity (DESIGN.md 8.5). Leaves out everything that
    changes while the world runs (clock, credits, chunk count, config versions)."""
    wfs = sorted(([s.workflow_id, s.tenant_id, dict(s.raw["governance"]), dict(s.raw["options"])]
                  for s in specs), key=lambda w: w[0])
    h0 = None if history0 is None else [history0.events, history0.last_event_id]
    return sha256_hex({"scenario": state.scenario, "end_minute": state.end_minute,
                       "tick_minutes": state.tick_minutes, "workflows": wfs, "history0": h0})


def world_identity(client: SimClient) -> WorldIdentity:
    state = client.state()
    specs = tuple(sorted(client.workflows(), key=lambda s: s.workflow_id))
    history = client.list_chunks("history", -1)
    h0 = min(history, key=lambda c: c.chunk) if history else None
    return WorldIdentity(state, specs, h0, fingerprint(state, specs, h0))


@dataclass(frozen=True)
class Consistency:
    schema: str
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def check_consistency(db: Database, schema: str, client: SimClient,
                      ident: WorldIdentity) -> Consistency:
    """Every resume check of DESIGN.md 8.5 for one candidate schema; returns all failures."""
    st = ident.state
    fails: list[str] = []
    with db.connect(schema=schema) as conn:
        try:
            clock = repos.get_clock(conn)
        except LookupError:
            return Consistency(schema, ("no clock row: the schema was never initialised",))
        cur = repos.ingest_cursor(conn)
        last = repos.last_ingested_meta(conn)
        held = repos.held_approvals(conn)
        ours = repos.controller_versions(conn)
        highest = repos.max_versions(conn)
    if clock.sim_minute > st.sim_minute:
        fails.append(f"our clock {clock.sim_minute} is ahead of the simulator's {st.sim_minute}")
    if cur.max_chunk >= st.chunks:
        fails.append(f"we ingested chunk {cur.max_chunk}, the simulator has {st.chunks} chunks")
    if cur.max_event_id > st.last_event_id:
        fails.append(f"our max event id {cur.max_event_id} > simulator {st.last_event_id}")
    if last is not None and not fails:
        fails += _chunk_matches(client, last)
    if held:
        live = {a.approval_id: a.proposal_id for a in client.approvals()}
        for aid, pid in held:
            if live.get(aid) != pid:
                fails.append(f"approval {aid} ({pid}) is not in the simulator's approvals")
    history = {}
    for wid in sorted({w for w, _, _ in ours} | set(highest)):
        try:
            history[wid] = {v.version: v.proposal_id for v in client.config_history(wid)}
        except Exception as e:  # unknown workflow: a different world
            fails.append(f"config history of {wid} unavailable: {e}")
            history[wid] = {}
    for wid, version, pid in ours:
        if history.get(wid, {}).get(version, "<missing>") != pid:
            fails.append(f"our {wid} v{version} ({pid}) is not in the simulator's history")
    for wid, v in highest.items():
        if history.get(wid) and max(history[wid]) < v:
            fails.append(f"{wid}: simulator is at v{max(history[wid])}, we recorded v{v}")
    return Consistency(schema, tuple(fails))


def _chunk_matches(client: SimClient, last: tuple[int, int, int]) -> list[str]:
    n, events, last_id = last
    listed = {c.chunk: c for kind in ("history", "events")
              for c in client.list_chunks(kind, n - 1)}
    c = listed.get(n)
    if c is None or (c.events, c.last_event_id) != (events, last_id):
        return [f"our chunk {n} ({events} events, last id {last_id}) differs from the "
                f"simulator's ({None if c is None else (c.events, c.last_event_id)})"]
    return []


@dataclass(frozen=True)
class RunResolution:
    schema: str
    resumed: bool
    rejected: tuple[Consistency, ...] = field(default_factory=tuple)


def resolve_run(db: Database, client: SimClient, ident: WorldIdentity,
                sim_url: str) -> RunResolution:
    """Resume the newest consistent candidate, else create and register a fresh schema.
    Rejected candidates are marked abandoned (their world is gone) and kept for audit."""
    db.ensure_ctl()
    rejected = []
    for cand in db.run_candidates(ident.fingerprint):
        res = check_consistency(db, cand.schema_name, client, ident)
        if res.ok:
            log.info("resuming run schema %s", cand.schema_name)
            return RunResolution(cand.schema_name, True, tuple(rejected))
        log.warning("run schema %s is not this world: %s", cand.schema_name,
                    "; ".join(res.failures))
        db.update_run(cand.schema_name, status="abandoned")
        rejected.append(res)
    sim_days = max(1, -(-ident.state.end_minute // 1440))
    schema = db.create_run_schema(ident.fingerprint, sim_url, sim_days=sim_days)
    log.info("created run schema %s", schema)
    return RunResolution(schema, False, tuple(rejected))
