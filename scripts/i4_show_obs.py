"""Print the Beta-binomial observations a stored engagement decision saw (test tooling, step I4).

    .venv/bin/python scripts/i4_show_obs.py <dbname> <decision_id> [--dsn-base "host=... "]

Decodes the decision's EW posterior snapshot (learning/ws_codec.py), refits the model and prints
each (dim, arm, cohort) observation with its arm and control counts, so a posterior that is too
sure of itself can be traced to the counts behind it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.audit.replay import decode_inputs  # noqa: E402
from controller.learning.engagement import BetaBinomialModel  # noqa: E402
from controller.learning.ws_codec import decode_snapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("decision_id", type=int)
    ap.add_argument("--dsn-base", default="host=127.0.0.1 port=55480 user=postgres password=ctl")
    a = ap.parse_args()
    with psycopg.connect(f"{a.dsn_base} dbname={a.db}") as c:
        s = c.execute("select nspname from pg_namespace where nspname like 'run_%' "
                      "order by oid desc limit 1").fetchone()[0]
        c.execute(f"set search_path={s}")
        ids = c.execute("select snapshot_ids from decision where decision_id = %s",
                        (a.decision_id,)).fetchone()[0]
        row = c.execute("select inputs from posterior_snapshot where snapshot_id = any(%s) "
                        "and kind = 'ew'", (ids,)).fetchone()
    ws = decode_snapshot(decode_inputs(row[0]))
    post = BetaBinomialModel().fit(ws, "ew")
    sd = np.sqrt(np.diag(post.cov))
    for nm, m, s in zip(post.index.names, post.mean, sd):
        print(f"{nm:55s} mean {m:+.3f} sd {s:.3f}")
    for o in post.summary["observations"]:
        print(dict(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())
