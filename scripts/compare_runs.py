"""Compare two run schemas of one world (scripts/kill_test.sh; DESIGN.md 10.2).

    python scripts/compare_runs.py --dsn "<dsn>" --clean <schema> --crashed <schema> \
        --state <crashed run's /sim/state json> [--out report.md]

Exit 0 when the crashed run equals the clean run: the same advances in the same order, every
event ingested exactly once, and identical facts, rollups, catalog and config tables.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg import sql

TABLES = ("cell", "campaign", "lead_dim", "call_fact", "config_event", "approval_event",
          "event_other", "config_version", "config_cohort", "r_engage", "r_pickup", "r_did",
          "r_flags", "workflow", "allocation_provenance")
EXCLUDE = {"ingested_chunk": {"ingested_wall", "txn_ms"}}


def digest(conn: psycopg.Connection, schema: str, table: str) -> tuple[int, str]:
    cols = [r[0] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns WHERE table_schema = %s
           AND table_name = %s ORDER BY ordinal_position""", (schema, table))]
    keep = [c for c in cols if c not in EXCLUDE.get(table, set())]
    q = sql.SQL("SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '') "
                "FROM (SELECT {} FROM {}.{}) t").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in keep), sql.Identifier(schema),
        sql.Identifier(table))
    n, h = conn.execute(q).fetchone()
    return int(n), str(h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--clean", required=True)
    ap.add_argument("--crashed", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    state = json.loads(Path(a.state).read_text())
    lines, ok = [], True
    with psycopg.connect(a.dsn) as conn:
        for t in TABLES + ("ingested_chunk",):
            x, y = digest(conn, a.clean, t), digest(conn, a.crashed, t)
            same = x == y
            ok &= same
            lines.append(f"| `{t}` | {x[0]:,} | {y[0]:,} | {'identical' if same else 'DIFFERENT'} |")
        adv = {s: [r[0] for r in conn.execute(sql.SQL(
            "SELECT idem_key FROM {}.intent WHERE kind = 'advance' AND status = 'done' "
            "ORDER BY intent_id").format(sql.Identifier(s)))] for s in (a.clean, a.crashed)}
        ev = conn.execute(sql.SQL("SELECT sum(events), count(*) FROM {}.ingested_chunk").format(
            sql.Identifier(a.crashed))).fetchone()
        recon = conn.execute(sql.SQL(
            """SELECT summary FROM {}.audit_log WHERE action IN ('intent_reconciled',
               'fault_injected') ORDER BY audit_id""").format(sql.Identifier(a.crashed))).fetchall()
        started = conn.execute(sql.SQL("SELECT count(*) FROM {}.intent WHERE status = 'started'"
                                       ).format(sql.Identifier(a.crashed))).fetchone()[0]
    same_adv = adv[a.clean] == adv[a.crashed]
    exact = ev[0] == state["last_event_id"] and ev[1] == state["chunks"]
    ok &= same_adv and exact and started == 0
    out = [f"Clean schema `{a.clean}`, crashed schema `{a.crashed}`.", "",
           f"- Advances: {len(adv[a.clean])} clean, {len(adv[a.crashed])} crashed, "
           f"{'same stops in the same order' if same_adv else 'DIFFERENT'}.",
           f"- Ingested events {ev[0]:,} in {ev[1]} chunks vs /sim/state last_event_id "
           f"{state['last_event_id']:,} and {state['chunks']} chunks: "
           f"{'exact' if exact else 'NOT EXACT'}.",
           f"- Intents left started: {started}.",
           "- Faults fired and intents reconciled (audit log): " +
           ("; ".join(r[0] for r in recon) if recon else "none"), "",
           "| Table | Clean rows | Crashed rows | Content |", "|---|---|---|---|", *lines, "",
           f"**{'PASS' if ok else 'FAIL'}**"]
    text = "\n".join(out)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
