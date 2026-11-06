"""Audit CLI: answer the live-review questions from our own store.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.4 and 9.6.

    python -m controller.audit why-changed --workflow ks_cancel --at "D3 14:00"
    python -m controller.audit why-heard --lead <lead_id> --day 3
    python -m controller.audit replay-decision 17 [--no-refit]
    python -m controller.audit replay-sample --n 20 [--seed 0]
    python -m controller.audit weekly-digest --tenant northwind_life [--until "D7 00:00"]

Every command accepts --json (machine-readable output) and --schema run_<fp12>_<n> (default:
$CTL_SCHEMA, else the newest active run in ctl.run_registry). The connection comes from the
controller's settings (PG_DSN, or CTL_PG_*), and every transaction is read-only.

Exit codes: 0 answered (replays equal), 1 a replay differs, 2 not found or bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql

from controller.audit import explain, replay

_SCHEMA_RE = re.compile(r"^run_[0-9a-f]{12}_[0-9]+$")


def latest_schema(conn: Any) -> str | None:
    """The newest active run schema, by registry creation time."""
    row = conn.execute("SELECT schema_name FROM ctl.run_registry WHERE status = 'active' "
                       "ORDER BY created_wall DESC, schema_name DESC LIMIT 1").fetchone()
    return None if row is None else str(row[0])


@contextmanager
def open_connection(schema: str | None = None, dsn: str | None = None,
                    read_only: bool = True) -> Iterator[psycopg.Connection[Any]]:
    """A connection whose search_path is the run schema (read-only unless asked otherwise)."""
    if dsn is None:
        from controller.settings import load_settings
        dsn = load_settings().env.dsn
    with psycopg.connect(dsn, autocommit=True, application_name="formi-audit-cli") as conn:
        schema = schema or os.environ.get("CTL_SCHEMA") or latest_schema(conn)
        if not schema or not _SCHEMA_RE.match(schema):
            raise explain.NotFound(f"no run schema to read (got {schema!r}); pass --schema")
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        if read_only:
            conn.execute("SET default_transaction_read_only = on")
        yield conn


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj.to_json() if hasattr(obj, "to_json") else obj, indent=2,
                         default=str))
    else:
        print(obj.render() if hasattr(obj, "render") else obj)


def _report_json(rep: Any) -> dict[str, Any]:
    from controller.types import thaw
    return {"decision_id": rep.decision_id, "equal": rep.equal, "diffs": list(rep.diffs),
            "settings_hash_match": rep.settings_hash_match,
            "code_version_match": rep.code_version_match, "stored": thaw(rep.stored),
            "recomputed": thaw(rep.recomputed)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m controller.audit",
                                 description="Answer audit questions from the controller store.")
    ap.add_argument("--schema", help="run schema (default: $CTL_SCHEMA or the newest active run)")
    ap.add_argument("--dsn", help="Postgres DSN (default: from controller settings)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("why-changed", help="why did a workflow change at a time (9.4)")
    c.add_argument("--workflow", required=True)
    c.add_argument("--at", required=True, help='"D3 14:00" or a sim minute')
    h = sub.add_parser("why-heard", help="why did a lead hear what it heard on a day (9.4)")
    h.add_argument("--lead", required=True)
    h.add_argument("--day", required=True, type=int)
    r = sub.add_parser("replay-decision", help="recompute a decision from the store (9.6)")
    r.add_argument("decision_id", type=int)
    r.add_argument("--no-refit", action="store_true", help="reuse the stored posterior")
    s = sub.add_parser("replay-sample", help="replay n deterministically sampled decisions")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--no-refit", action="store_true")
    w = sub.add_parser("weekly-digest", help="operational changes of a tenant (9.3 item 8)")
    w.add_argument("--tenant", required=True)
    w.add_argument("--until", help='"D7 00:00" or a sim minute (default: the controller clock)')
    w.add_argument("--days", type=int, default=7)
    for p in (c, h, r, s, w):
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open_connection(a.schema, a.dsn) as conn:
            if a.cmd == "why-changed":
                _print(explain.why_changed(conn, a.workflow, a.at), a.json)
            elif a.cmd == "why-heard":
                _print(explain.why_heard(conn, a.lead, a.day), a.json)
            elif a.cmd == "weekly-digest":
                _print(explain.weekly_digest(conn, a.tenant, a.until, a.days), a.json)
            elif a.cmd == "replay-decision":
                rep = replay.replay_decision(a.decision_id, refit=not a.no_refit, conn=conn)
                _print(_report_json(rep) if a.json else replay.render_report(rep), a.json)
                return 0 if rep.equal else 1
            elif a.cmd == "replay-sample":
                ids = replay.sample_decisions(conn, a.n, a.seed)
                reps = [replay.replay_decision(i, refit=not a.no_refit, conn=conn) for i in ids]
                if a.json:
                    _print([_report_json(x) for x in reps], True)
                else:
                    for x in reps:
                        print(replay.render_report(x))
                    print(f"{sum(x.equal for x in reps)} of {len(reps)} sampled decisions "
                          "replayed equal")
                return 0 if reps and all(x.equal for x in reps) else 1
    except (explain.NotFound, replay.ReplayError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
