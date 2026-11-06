"""Run the real controller (`python -m controller`, what run.sh runs inside its container) against
a served simulator and a throwaway database, then grade the run against a baseline.

Test tooling for DESIGN.md 15.1 M3 (integration step I2); it reads scenario files and run
directories, which controller code never does.

    .venv/bin/python scripts/m3_run.py <scenario.yaml> --out runs/m3/<name> \
        [--baseline runs/base_<name>] [--port 8720] [--dsn "host=... dbname=postgres"] \
        [--env CTL_FAULT=apply:1:after_http ...] [--keep-db]

Writes <out>/run (the simulator run dir), <out>/controller.log, <out>/grade.md and
<out>/summary.json (exit codes, wall time, grade governance block, schema name).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.e2e.platform.helpers import (create_database, drop_database, latest_schema,  # noqa: E402
                                        run_controller)
from tests.support.sim_process import SimProcess  # noqa: E402

DEFAULT_DSN = "host=127.0.0.1 port=55470 user=postgres password=ctl dbname=postgres"


def grade(run_dir: Path, baseline: Path, out: Path) -> dict:
    md = subprocess.run([sys.executable, "-m", "sim", "grade", "--run", str(run_dir),
                         "--baseline", str(baseline)], cwd=REPO, capture_output=True, text=True,
                        check=True).stdout
    (out / "grade.md").write_text(md)
    gov = md.split("## Governance", 1)[1].split("```", 2)[1]
    return json.loads(gov)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline")
    ap.add_argument("--port", type=int, default=8720)
    ap.add_argument("--dsn", default=os.environ.get("CTL_TEST_PG_DSN", DEFAULT_DSN))
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--max-restarts", type=int, default=5)
    ap.add_argument("--keep-db", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    extra = {"CTL_LEARNER": "probe"}
    extra.update(dict(kv.split("=", 1) for kv in a.env))
    dsn = create_database(a.dsn)
    sim = SimProcess(a.scenario, out, port=a.port, ready_timeout_s=600).start()
    t0 = time.monotonic()
    try:
        run = run_controller(sim.url, dsn, out / "controller.log", extra,
                             timeout_s=3600, max_restarts=a.max_restarts)
        wall = time.monotonic() - t0
        schema = latest_schema(dsn)
    finally:
        sim.stop()
        if not a.keep_db:
            drop_database(a.dsn, dsn)
    summary = {"exit_codes": run.exit_codes, "wall_s": round(wall, 1), "schema": schema,
               "dsn": dsn if a.keep_db else None}
    if a.baseline:
        summary["governance"] = grade(sim.run_dir, Path(a.baseline), out)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if run.exit_codes[-1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
