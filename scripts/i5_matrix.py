"""Run the real controller on private variants and print the M6 numbers (test tooling; reads
scenarios/private and runs/, which controller code never does).

    .venv/bin/python scripts/i5_matrix.py name [name ...] [--env K=V ...]
Per variant: governance hygiene, changes by verdict, supervisor-edit MTTR, credits used.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run_one(name: str, env: list[str]) -> dict:
    scen = REPO / (f"scenarios/{name}.yaml" if (REPO / f"scenarios/{name}.yaml").exists()
                   else f"scenarios/private/{name}.yaml")
    base = REPO / f"runs/base_{name}"
    if not (base / "meta.json").exists() and not base.exists():
        subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(scen),
                        "--run-dir", str(base)], cwd=REPO, check=True, capture_output=True)
    out = REPO / f"runs/i5/{name}"
    shutil.rmtree(out, ignore_errors=True)
    cmd = [sys.executable, "scripts/m3_run.py", str(scen), "--out", str(out),
           "--baseline", str(base)]
    for e in env:
        cmd += ["--env", e]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    res = {"name": name, "exit": r.returncode}
    g = out / "grade.md"
    if g.exists():
        md = g.read_text()
        m = re.search(r"Credits used: (\S+) . validation runs: (\d+) . LLM fix calls: (\d+)", md)
        res["credits"] = m.groups() if m else None
        res["changes"] = re.search(r"\d+ applied[^\n]*", md).group(0) if "applied —" in md or re.search(r"\d+ applied", md) else None
        res["edits"] = [(e.get("workflow"), e.get("at"), e.get("mttr_minutes")) for e in
                        json.loads(md.split("## Incidents", 1)[1].split("```")[1].lstrip("json\n"))
                        if e.get("type") == "supervisor_edit"]
        res["gov"] = json.loads(md.split("## Governance", 1)[1].split("```")[1])
        res["gov"] = {k: res["gov"][k] for k in ("auto_apply_rejected", "apply_conflicts", "lapsed")}
        res["gov"]["expired"] = json.loads(md.split("## Governance", 1)[1].split("```")[1])["decisions"].get("expired", 0)
        res["pickup_all"] = re.search(r"\| ALL \|[^\n]*", md).group(0)
    shutil.rmtree(out / "run", ignore_errors=True)
    if r.returncode:
        res["stderr"] = (r.stdout + r.stderr)[-1500:]
    return res


if __name__ == "__main__":
    args = sys.argv[1:]
    env = []
    names = []
    while args:
        a = args.pop(0)
        if a == "--env":
            env.append(args.pop(0))
        else:
            names.append(a)
    for n in names:
        print(json.dumps(run_one(n, env), default=str), flush=True)
