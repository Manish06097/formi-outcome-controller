"""The 17.2 model selection: both engagement models on the same scenarios, one table.

Test tooling (step I4). It reads scenario files and run directories, which controller code never
does. Each (scenario, model) cell runs the real controller against a served simulator and its own
throwaway database through `scripts/m3_run.py`, then the simulator's grader judges it.

    .venv/bin/python scripts/variants/model_selection.py --out runs/i4/select -j 4 \
        --dsn "host=127.0.0.1 port=55480 user=postgres password=ctl dbname=postgres" \
        --write docs/measurements/engagement-model-selection.md [scenario ...]

A finished cell (its summary.json exists) is not run again, so a cut-off selection resumes.
`--dsn` and the simulator ports are per cell: ports are --port, --port + 1, ... (one per worker).

Decision rule (ADR 0003, DESIGN.md 17.2): ridge-logit becomes the default only if it captures more
of the engagement gap than the Beta-binomial, summed over engagement-primary workflows and
scenarios, AND has no more harmful engagement changes AND none on any scenario. A tie or a
loss keeps the Beta-binomial.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.grader import grade  # noqa: E402

MODELS = ("beta_binomial", "ridge_logit")
DEFAULT_SCENARIOS = (
    "public_01", "public_02", "seed_p01_s1101", "seed_p01_s1102", "seed_p02_s2201",
    "seed_p02_s2202", "effects_flipped_p01", "effects_small_p01", "effects_large_p01",
    "effects_flip_resize_p02", "last_listed_signflip_p01", "decaying_winner_dev_p01",
    "decaying_winner_highvol_p01")
PUBLIC = {"public_01": "scenarios/public_01_dev.yaml", "public_02": "scenarios/public_02_incidents.yaml"}


def scenario_file(name: str) -> str:
    return PUBLIC.get(name, f"scenarios/private/{name}.yaml")


@dataclass(frozen=True)
class Cell:
    scenario: str
    model: str

    def dir(self, out: Path) -> Path:
        return out / f"{self.scenario}__{self.model}"


def run_cell(cell: Cell, out: Path, port: int, dsn: str, extra_env: list[str]) -> None:
    d = cell.dir(out)
    if (d / "summary.json").exists():
        return
    d.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "scripts/m3_run.py", scenario_file(cell.scenario), "--out", str(d),
           "--baseline", f"runs/base_{cell.scenario}", "--port", str(port), "--dsn", dsn,
           "--env", f"CTL_ENGAGEMENT_MODEL={cell.model}"]
    for e in extra_env:
        cmd += ["--env", e]
    with open(str(d) + ".log", "w") as log:
        subprocess.run(cmd, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, check=False)


def engagement_changes_harmful(g: dict) -> tuple[int, int]:
    """(harmful changes that touched welcome or voice or graded on engagement, all harmful)."""
    bad = [c for c in g["controller_changes"]["detail"] if c["verdict"] == "harmful"]
    eng = [c for c in bad if c["engage_delta"] < -0.01]
    return len(eng), len(bad)


def engagement_workflows(g: dict, run_dir: Path) -> list[str]:
    """Engagement-primary workflows of the run, from the scenario the run wrote."""
    import yaml  # test tooling only
    scen = next((run_dir / "scenario").glob("*.yaml"))
    doc = yaml.safe_load(scen.read_text())
    return sorted(w["id"] for w in doc["workflows"]
                  if w["objective"]["primary"] == "engagement_rate")


def summarise(cell: Cell, out: Path) -> dict | None:
    d = cell.dir(out)
    if not (d / "summary.json").exists():
        return None
    s = json.loads((d / "summary.json").read_text())
    if not s.get("exit_codes") or s["exit_codes"][-1] != 0:
        return {"failed": True}
    g = grade(str(d / "run"), str(REPO / f"runs/base_{cell.scenario}"))
    wfs = engagement_workflows(g, d)
    eng_bad, all_bad = engagement_changes_harmful(g)
    gaps = {w: g["kpis"][w]["engagement_gap_captured"] for w in wfs}
    deltas = {w: g["delta_vs_baseline"][w]["engagement_rate"] for w in wfs}
    return {"failed": False, "gaps": gaps, "deltas": deltas, "eng_bad": eng_bad, "all_bad": all_bad,
            "changes": g["controller_changes"]["count"],
            "lapsed": g["governance"]["lapsed"],
            "rejected": g["governance"]["auto_apply_rejected"],
            "conflicts": g["governance"]["apply_conflicts"],
            "decay": g["decaying_variant_share_after_onset"], "wall": s.get("wall_s")}


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.1f}%"


def table(cells: list[Cell], rows: dict[Cell, dict | None], scenarios: list[str]) -> str:
    lines = ["| scenario | model | engagement gap captured per engagement-primary workflow | "
             "harmful engagement | harmful all | changes | wall s |", "|---|---|---|---|---|---|---|"]
    for sc in scenarios:
        for m in MODELS:
            r = rows.get(Cell(sc, m))
            if r is None or r.get("failed"):
                lines.append(f"| {sc} | {m} | {'failed' if r else 'not run'} | | | | |")
                continue
            gaps = ", ".join(f"{w} {pct(v)}" for w, v in sorted(r["gaps"].items()))
            lines.append(f"| {sc} | {m} | {gaps} | {r['eng_bad']} | {r['all_bad']} | "
                         f"{r['changes']} | {r['wall']} |")
    return "\n".join(lines)


def totals(rows: dict[Cell, dict | None], scenarios: list[str]) -> str:
    lines = ["| model | scenarios with a result | mean gap captured | workflows at or below 0 | "
             "harmful engagement, total | scenarios with any |", "|---|---|---|---|---|---|"]
    for m in MODELS:
        vals, low, bad, bad_sc, done = [], 0, 0, 0, 0
        for sc in scenarios:
            r = rows.get(Cell(sc, m))
            if r is None or r.get("failed"):
                continue
            done += 1
            for v in r["gaps"].values():
                if v is not None:
                    vals.append(v)
                    low += v <= 0
            bad += r["eng_bad"]
            bad_sc += r["eng_bad"] > 0
        mean = sum(vals) / len(vals) if vals else None
        lines.append(f"| {m} | {done} | {pct(mean)} | {low} of {len(vals)} | {bad} | {bad_sc} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", default=list(DEFAULT_SCENARIOS))
    ap.add_argument("--out", default="runs/i4/select")
    ap.add_argument("--port", type=int, default=8730)
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--dsn", default=os.environ.get("CTL_TEST_PG_DSN", ""))
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--write")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    scenarios = a.scenarios or list(DEFAULT_SCENARIOS)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cells = [Cell(sc, m) for sc in scenarios for m in MODELS]
    if not a.report_only:
        q: queue.Queue[Cell] = queue.Queue()
        for c in cells:
            q.put(c)

        def worker(port: int) -> None:
            while True:
                try:
                    c = q.get_nowait()
                except queue.Empty:
                    return
                run_cell(c, out, port, a.dsn, a.env)

        threads = [threading.Thread(target=worker, args=(a.port + i,)) for i in range(a.j)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    rows = {c: summarise(c, out) for c in cells}
    text = table(cells, rows, scenarios) + "\n\n" + totals(rows, scenarios) + "\n"
    print(text)
    if a.write:
        Path(a.write).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
