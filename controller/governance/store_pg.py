"""Postgres persistence of governance: proposals, transitions, the approval mirror, the overlay's
patches, path owners and cooldowns, and supervisor holds.

Owner: integration step I2 (milestone M3). Implements the `ProposalStore` port of
`governance/lifecycle.py` over the section 3.2 tables (`proposal`, `proposal_transition`,
`approval_mirror`, `patch`, `path_owner`, `cooldown`, `hold`), DESIGN.md 6.1, 6.7 and 8.2.
ADR 0029.

Transactions. Every store method runs in the scope's current transaction when one is bound, and
in a short transaction of its own otherwise (`TxScope`). The write-ahead intent executor binds
its T1 and T2 transactions (`governance/effects.py`), so a proposal's "before" transition commits
with its intent row and its result transition commits with the intent's result: there is no
moment at which the store and the intent log disagree. The overlay lives in memory (it is the
planner's working set) and is flushed to its tables at the end of every scoped transaction, so
an overlay change commits atomically with the proposal transition that caused it.

The one-in-flight invariant is the partial unique index `one_in_flight` (3.2): `insert` and
`apply` raise `OneInFlightViolation` when the database refuses, and `supersede` ends the holder
and inserts the new proposal in one transaction, the only way past it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields as dc_fields
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import psycopg
from psycopg.types.json import Json, Jsonb

from controller.governance.lifecycle import (OneInFlightViolation, TransitionRow, next_state)
from controller.planner.overlay import Overlay, OverlayContext
from controller.store.db import Database
from controller.store.repos import WorkflowCatalog
from controller.types import (ApprovalPublic, Config, FrozenDict, Hold, Patch, Proposal,
                              SimEpoch, Tx, VelocityEntry, freeze, thaw)


# =============================================================================== transactions

class TxScope:
    """The governance unit of work: one ambient transaction at a time (main thread only).

    `tx()` joins the bound transaction or opens a short one and binds it; `bind(tx)` binds a
    transaction someone else opened (the intent executor's T1 or T2, the reconciler's, REFRESH's).
    Flushers (the overlay) run at the end of the outermost scoped block, inside the transaction,
    so whatever they write commits or rolls back with it."""

    def __init__(self, db: Database):
        self.db = db
        self._cur: ContextVar[Tx | None] = ContextVar(f"gov_tx_{id(self)}", default=None)
        self.flushers: list[Callable[[Tx], None]] = []

    @property
    def current(self) -> Tx | None:
        return self._cur.get()

    @contextmanager
    def tx(self) -> Iterator[Tx]:
        cur = self._cur.get()
        if cur is not None:
            yield cur
            return
        with self.db.tx() as tx:
            token = self._cur.set(tx)
            try:
                yield tx
                self._flush(tx)
            finally:
                self._cur.reset(token)

    @contextmanager
    def bind(self, tx: Tx) -> Iterator[Tx]:
        cur = self._cur.get()
        if cur is not None:
            if cur is not tx:
                raise RuntimeError("a different transaction is already bound to the scope")
            yield tx
            return
        token = self._cur.set(tx)
        try:
            yield tx
            self._flush(tx)
        finally:
            self._cur.reset(token)

    def flush(self) -> None:
        """Flush in-memory state changed outside any store call (e.g. an overlay hold)."""
        with self.tx():
            pass

    def _flush(self, tx: Tx) -> None:
        for f in self.flushers:
            f(tx)


def _json(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(thaw(v))


def _exact(v: Any) -> Json | None:
    """For the `json` columns (migration 0015): the text keeps the dict's key order."""
    return None if v is None else Json(thaw(v))


# =============================================================================== proposals

_COLS = ("proposal_id", "wf", "class", "route", "state", "base_version", "proposed_config",
         "request", "change_summary", "diff", "affected_leads", "dry_run", "impact_score",
         "rationale", "evidence", "approval_id", "submitted_minute", "decided_minute",
         "apply_by_minute", "notice_until_minute", "applied_version", "superseded_by",
         "requeued_from", "decision_id", "verify", "p0_rank", "velocity", "applied_minute")
_FIELD_TO_COL = {"cls": "class"}
_JSON_FIELDS = {"proposed_config", "request", "diff", "dry_run", "evidence", "verify",
                "velocity"}
_PROPOSAL_FIELDS = {f.name for f in dc_fields(Proposal)}


class PgProposalStore:
    """`ProposalStore` (governance/lifecycle.py) over the run schema."""

    def __init__(self, scope: TxScope, catalog: WorkflowCatalog):
        self.scope = scope
        self.catalog = catalog

    # ------------------------------------------------------------ row mapping
    def _value(self, name: str, v: Any) -> Any:
        if name == "wf":
            return self.catalog.code(v)
        if name == "proposed_config":
            return None if v is None else Jsonb(v.stored())
        if name == "velocity":
            return Jsonb([[k, d, float(tv), bool(inc)] for k, d, tv, inc in (v or ())])
        if name == "request":
            return _exact(v)
        if name in _JSON_FIELDS:
            return _json(v)
        return v

    def _row(self, p: Proposal) -> list[Any]:
        out = []
        for col in _COLS:
            name = "cls" if col == "class" else col
            out.append(self._value(name, getattr(p, name)))
        return out

    def _proposal(self, r: Sequence[Any]) -> Proposal:
        d = dict(zip(_COLS, r))
        d["cls"] = d.pop("class")
        d["wf"] = self.catalog.by_code(d["wf"]).workflow_id
        cfg = d.pop("proposed_config")
        vel = d.pop("velocity") or ()
        kw = {k: v for k, v in d.items() if v is not None or k in ("p0_rank",)}
        for k in ("request", "diff", "dry_run", "evidence", "verify"):
            kw[k] = freeze(d.get(k) or {})
        if kw.get("impact_score") is not None:
            kw["impact_score"] = float(kw["impact_score"])
        return Proposal(**kw, proposed_config=None if cfg is None else Config.from_stored(cfg),
                        velocity=tuple((str(k), str(dm), float(tv), bool(inc))
                                       for k, dm, tv, inc in vel))

    def _select(self, tx: Tx, where: str, params: Sequence[Any], lock: bool = False
                ) -> list[Proposal]:
        q = f"SELECT {', '.join(_COLS)} FROM proposal WHERE {where} ORDER BY proposal_id"
        if lock:
            q += " FOR UPDATE"
        return [self._proposal(r) for r in tx.execute(q, params).fetchall()]

    def _transition(self, tx: Tx, pid: str, frm: str | None, to: str, minute: int, reason: str,
                    decision_id: int | None) -> None:
        seq = tx.execute("SELECT count(*) FROM proposal_transition WHERE proposal_id = %s",
                         (pid,)).fetchone()[0]
        tx.execute("""INSERT INTO proposal_transition (proposal_id, seq, from_state, to_state,
                                                       sim_minute, reason, decision_id)
                      VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                   (pid, seq, frm, to, minute, reason, decision_id))

    # ------------------------------------------------------------ reads
    def get(self, proposal_id: str) -> Proposal | None:
        with self.scope.tx() as tx:
            rows = self._select(tx, "proposal_id = %s", (proposal_id,))
        return rows[0] if rows else None

    def holder(self, wf: str) -> Proposal | None:
        from controller.types import IN_FLIGHT_STATES
        with self.scope.tx() as tx:
            rows = self._select(tx, "wf = %s AND state = ANY(%s)",
                                (self.catalog.code(wf), sorted(IN_FLIGHT_STATES)))
        return rows[0] if rows else None

    def in_states(self, states: Iterable[str], wf: str | None = None) -> list[Proposal]:
        s = sorted(set(states))
        with self.scope.tx() as tx:
            if wf is None:
                return self._select(tx, "state = ANY(%s)", (s,))
            return self._select(tx, "state = ANY(%s) AND wf = %s", (s, self.catalog.code(wf)))

    def submitted_minutes(self, wf: str) -> list[int]:
        with self.scope.tx() as tx:
            rows = tx.execute("""SELECT submitted_minute FROM proposal WHERE wf = %s
                                 AND route = 'approval' AND submitted_minute IS NOT NULL
                                 AND approval_id IS NOT NULL ORDER BY 1""",
                              (self.catalog.code(wf),)).fetchall()
        return [int(r[0]) for r in rows]

    def transitions_of(self, proposal_id: str) -> list[TransitionRow]:
        with self.scope.tx() as tx:
            rows = tx.execute("""SELECT proposal_id, seq, from_state, to_state, sim_minute,
                                        reason, decision_id FROM proposal_transition
                                 WHERE proposal_id = %s ORDER BY seq""",
                              (proposal_id,)).fetchall()
        return [TransitionRow(*r) for r in rows]

    def mirrored_reasons(self, approval_id: str) -> tuple[str, ...]:
        with self.scope.tx() as tx:
            r = tx.execute("SELECT reasons FROM approval_mirror WHERE approval_id = %s",
                           (approval_id,)).fetchone()
        return tuple((r[0] if r else None) or ())

    def auto_block_history(self) -> tuple[dict[str, tuple[int, str, str]], dict[str, int]]:
        """(latest `forbidden` transition per workflow, last `policy_changed` minute per
        workflow) for `ProposalLifecycle.restore_auto_blocks` (AUDIT-2 F5)."""
        with self.scope.tx() as tx:
            f = tx.execute("""SELECT DISTINCT ON (p.wf) p.wf, t.sim_minute, p.proposal_id,
                                     coalesce(t.reason, '')
                              FROM proposal_transition t JOIN proposal p USING (proposal_id)
                              WHERE t.to_state = 'failed_policy'
                              ORDER BY p.wf, t.sim_minute DESC, t.seq DESC""").fetchall()
            c = tx.execute("""SELECT wf, max(sim_minute) FROM audit_log
                              WHERE action = 'policy_changed' AND wf IS NOT NULL
                              GROUP BY wf""").fetchall()
        ids = {int(v): k for k, v in self.catalog.codes.items()}
        return ({ids[int(r[0])]: (int(r[1]), str(r[2]), str(r[3])) for r in f},
                {ids[int(r[0])]: int(r[1]) for r in c})

    def velocity_entries(self, wf: str, since_minute: int) -> list[VelocityEntry]:
        with self.scope.tx() as tx:
            rows = self._select(tx, """wf = %s AND route = 'auto' AND applied_minute IS NOT NULL
                                       AND applied_minute > %s""",
                                (self.catalog.code(wf), since_minute))
        out = [e for p in rows for e in p.velocity_entries()]
        return sorted(out, key=lambda e: (e.minute, e.proposal_id, e.cohort, e.dim))

    # ------------------------------------------------------------ writes
    def atomic(self):
        return self.scope.tx()

    def next_seq(self, wf: str) -> int:
        """Per-workflow sequence of proposal ids: one more than the proposals stored, so a draft
        whose transaction rolled back leaves no gap and a re-plan reuses the same id (8.6)."""
        with self.scope.tx() as tx:
            n = tx.execute("SELECT count(*) FROM proposal WHERE wf = %s",
                           (self.catalog.code(wf),)).fetchone()[0]
        return int(n) + 1

    def insert(self, p: Proposal, minute: int, reason: str) -> Proposal:
        with self.scope.tx() as tx:
            try:
                with tx.transaction():
                    tx.execute(f"INSERT INTO proposal ({', '.join(_COLS)}) VALUES "
                               f"({', '.join(['%s'] * len(_COLS))})", self._row(p))
            except psycopg.errors.UniqueViolation as e:
                if "one_in_flight" in str(e):
                    raise OneInFlightViolation(f"{p.wf}: another proposal is in flight") from e
                raise ValueError(f"duplicate proposal id {p.proposal_id}") from e
            self._transition(tx, p.proposal_id, None, p.state, minute, reason, p.decision_id)
        return p

    def apply(self, proposal_id: str, event: str, minute: int, reason: str,
              **fields: Any) -> Proposal:
        bad = set(fields) - _PROPOSAL_FIELDS
        if bad:
            raise ValueError(f"unknown proposal fields {sorted(bad)}")
        with self.scope.tx() as tx:
            rows = self._select(tx, "proposal_id = %s", (proposal_id,), lock=True)
            if not rows:
                raise KeyError(proposal_id)
            p = rows[0]
            state = next_state(p.state, event)
            sets = {"state": state, **fields}
            cols = [_FIELD_TO_COL.get(k, k) for k in sets]
            vals = [self._value(k, v) for k, v in sets.items()]
            try:
                with tx.transaction():
                    tx.execute(f"UPDATE proposal SET {', '.join(f'{c} = %s' for c in cols)} "
                               "WHERE proposal_id = %s", (*vals, proposal_id))
            except psycopg.errors.UniqueViolation as e:
                raise OneInFlightViolation(f"{p.wf}: {proposal_id} -> {state}") from e
            new = Proposal(**{**{f: getattr(p, f) for f in _PROPOSAL_FIELDS}, **sets})
            self._transition(tx, proposal_id, p.state, state, minute, f"{event}: {reason}",
                             new.decision_id)
        return new

    def supersede(self, holder_id: str, new: Proposal, minute: int, reason: str) -> Proposal:
        with self.scope.tx():
            self.apply(holder_id, "superseded", minute, reason, superseded_by=new.proposal_id)
            return self.insert(new, minute, f"supersedes {holder_id}: {reason}")

    def mirror(self, a: ApprovalPublic, epoch: SimEpoch, minute: int) -> None:
        def m(ts: str | None) -> int | None:
            return None if ts is None else epoch.minute(ts)
        with self.scope.tx() as tx:
            tx.execute(
                """INSERT INTO approval_mirror (approval_id, proposal_id, wf, status,
                       created_minute, expires_minute, decided_minute, apply_by_minute, reasons,
                       last_polled_minute)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (approval_id) DO UPDATE SET status = excluded.status,
                       decided_minute = excluded.decided_minute,
                       apply_by_minute = excluded.apply_by_minute, reasons = excluded.reasons,
                       last_polled_minute = excluded.last_polled_minute""",
                (a.approval_id, a.proposal_id, self.catalog.code(a.workflow_id), a.status,
                 m(a.created_at), m(a.expires_at), m(a.decided_at), m(a.apply_by),
                 Jsonb(list(a.reasons)), minute))


# =============================================================================== overlay

class OverlayStore:
    """The overlay's `patch`, `path_owner` and `cooldown` rows: `load` rebuilds an Overlay on
    start, `flush` writes whatever changed since the last flush (registered as a scope flusher,
    so it runs inside the transaction that made the change)."""

    def __init__(self, scope: TxScope, catalog: WorkflowCatalog):
        self.scope = scope
        self.catalog = catalog
        self.overlay: Overlay | None = None
        self._patches: dict[int, Patch] = {}
        self._owners: dict[Any, Any] = {}
        self._cooldowns: dict[Any, Any] = {}

    def load(self, context: Callable[[str], OverlayContext], clock: Callable[[], int],
             **kw: Any) -> Overlay:
        with self.scope.db.tx() as tx:
            patches = [self._patch_row(r) for r in tx.execute(
                """SELECT patch_id, wf, owner, path, value, class, p0_rank, state, proposal_id,
                          decision_id, supervisor_touch, held_until_minute, hold_count, evidence
                   FROM patch ORDER BY patch_id""").fetchall()]
            owners = [{"wf": self.catalog.by_code(r[0]).workflow_id, "path": list(r[1]),
                       "owner": r[2], "since_version": r[3], "since_minute": r[4]}
                      for r in tx.execute("""SELECT wf, path, owner, since_version, since_minute
                                             FROM path_owner ORDER BY wf, path""").fetchall()]
            cools = [{"wf": self.catalog.by_code(r[0]).workflow_id, "path": list(r[1]),
                      "until_minute": r[2], "source_version": r[3], "reason": r[4]}
                     for r in tx.execute("""SELECT wf, path, until_minute, source_version, reason
                                            FROM cooldown ORDER BY wf, path""").fetchall()]
        ov = Overlay.restore({"patch": patches, "path_owner": owners, "cooldown": cools},
                             context, clock, **kw)
        self.attach(ov)
        return ov

    def attach(self, ov: Overlay) -> None:
        self.overlay = ov
        self._patches = dict(ov.patches)
        self._owners = dict(ov.owners)
        self._cooldowns = dict(ov.cooldowns)
        if self.flush not in self.scope.flushers:
            self.scope.flushers.append(self.flush)

    def _patch_row(self, r: Sequence[Any]) -> dict[str, Any]:
        return {"patch_id": int(r[0]), "wf": self.catalog.by_code(r[1]).workflow_id,
                "owner": r[2], "path": list(r[3]), "value": r[4], "cls": int(r[5]),
                "p0_rank": r[6], "state": r[7], "proposal_id": r[8], "decision_id": r[9],
                "supervisor_touch": r[10], "held_until_minute": r[11], "hold_count": int(r[12]),
                "evidence": r[13] or {}}

    def flush(self, tx: Tx) -> None:
        ov = self.overlay
        if ov is None:
            return
        for pid, p in ov.patches.items():
            if self._patches.get(pid) is p:
                continue
            tx.execute(
                """INSERT INTO patch (patch_id, wf, owner, path, value, class, p0_rank, state,
                       proposal_id, decision_id, supervisor_touch, held_until_minute, hold_count,
                       evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (patch_id) DO UPDATE SET value = excluded.value,
                       state = excluded.state, proposal_id = excluded.proposal_id,
                       supervisor_touch = excluded.supervisor_touch,
                       held_until_minute = excluded.held_until_minute,
                       hold_count = excluded.hold_count, evidence = excluded.evidence""",
                (p.patch_id, self.catalog.code(p.wf), p.owner, list(p.path), _exact(p.value),
                 p.cls, p.p0_rank, p.state, p.proposal_id, p.decision_id, p.supervisor_touch,
                 p.held_until_minute, p.hold_count, _exact(p.evidence or {})))
            self._patches[pid] = p
        for key, o in ov.owners.items():
            if self._owners.get(key) is o:
                continue
            wf, path = key
            tx.execute("""INSERT INTO path_owner (wf, path, owner, since_version, since_minute)
                          VALUES (%s, %s, %s, %s, %s)
                          ON CONFLICT (wf, path) DO UPDATE SET owner = excluded.owner,
                              since_version = excluded.since_version,
                              since_minute = excluded.since_minute""",
                       (self.catalog.code(wf), list(path), o.owner, o.since_version,
                        o.since_minute))
            self._owners[key] = o
        for key, c in ov.cooldowns.items():
            if self._cooldowns.get(key) is c:
                continue
            wf, path = key
            tx.execute("""INSERT INTO cooldown (wf, path, until_minute, source_version, reason)
                          VALUES (%s, %s, %s, %s, %s)
                          ON CONFLICT (wf, path) DO UPDATE SET until_minute = excluded.until_minute,
                              source_version = excluded.source_version, reason = excluded.reason""",
                       (self.catalog.code(wf), list(path), c.until_minute, c.source_version,
                        c.reason))
            self._cooldowns[key] = c


# =============================================================================== holds

def holds_of(scope: TxScope, catalog: WorkflowCatalog, wf: str) -> list[Hold]:
    """Supervisor holds of a workflow (`hold`, written by the API as `ctl_api`, 9.3)."""
    with scope.tx() as tx:
        rows = tx.execute("""SELECT hold_id, set_by, coalesce(reason, ''), from_minute,
                                    until_minute, released_minute, released_by
                             FROM hold WHERE wf = %s ORDER BY hold_id""",
                          (catalog.code(wf),)).fetchall()
    return [Hold(hold_id=int(r[0]), wf=wf, set_by=r[1], reason=r[2], from_minute=int(r[3]),
                 until_minute=r[4], released_minute=r[5], released_by=r[6]) for r in rows]


__all__ = ["TxScope", "PgProposalStore", "OverlayStore", "holds_of"]
