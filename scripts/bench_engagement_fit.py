"""Fit-time benchmark of both engagement models at public_03 scale, on synthetic data.

Usage: .venv/bin/python scripts/bench_engagement_fit.py [--repeat 3]

Scale (DESIGN.md 3.3, 5.3, 8.7; volumes of one public_03 workflow, read from the design, not
from scenario files): 252 cells (7 languages x 3 tiers x 4 age bands x 3 value bands), 13,500
connected calls a day, 7 history days on the default config then 5 run days with welcome x voice
exploration (3 welcomes, 4 voices), two prompts (a supervisor edit on D2), 14 grid cohorts.
Reports the median wall time of: each model's stationary and EW fit, 4,000 seeded draws, one
allocation plan and one round of winner looks (Beta-binomial), plus the problem sizes.
Numpy only; no simulator, no database.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.learning.allocator import open_experiments, plan_allocation_detail  # noqa: E402
from controller.learning.engagement import BetaBinomialModel, RidgeLogitModel  # noqa: E402
from controller.learning.winners import evaluate_looks  # noqa: E402
from controller.types import CohortDef  # noqa: E402
from tests.support.t3_world import (base_config, make_cells, make_spec,  # noqa: E402
                                    uniform_world)

LANGS = ("en", "hi", "ta", "te", "kn", "ml", "mr")
AGES = ("18-30", "31-45", "46-60", "60+")


def mult(dim: str, option: str, cell, days: float) -> float:
    table = {("welcome_message", "W2"): 1.08, ("welcome_message", "W3"): 0.95,
             ("voice", "V2"): 1.10 if cell.lang == "en" else 1.0,
             ("voice", "V3"): 1.20 if cell.age == "60+" else 0.9}
    return table.get((dim, option), 1.0)


def timed(fn, repeat: int) -> tuple[float, object]:
    out, times = None, []
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    spec = make_spec(window=(8, 20), dials_per_hour=4500)
    cells = make_cells(langs=LANGS, tiers=("t1", "t2", "t3"), ages=AGES,
                       values=("low", "mid", "high"))
    world = uniform_world(seed=7, connected_per_day=13_500, mult=mult, cells=cells, spec=spec)
    cohorts = [CohortDef.make(f"{l}_{'old' if old else 'young'}~{i:06d}",
                              {"lang": [l], "age_band": list(AGES[2:] if old else AGES[:2])})
               for i, (l, old) in enumerate((l, o) for l in LANGS for o in (False, True))]
    cfg0 = base_config(spec, cohorts)
    t_sim = time.perf_counter()
    for seg in range(-14, 0):                         # 7 history days, default config only
        world.simulate_segment(cfg0, seg)
    exps = open_experiments(world.snapshot(cfg0, 0))
    cfg = cfg0.with_allocation("default", "welcome_message", {"W1": 0.8, "W2": 0.1, "W3": 0.1})
    cfg = cfg.with_allocation("default", "voice", {"V1": 0.7, "V2": 0.1, "V3": 0.1, "V4": 0.1})
    for seg in range(0, 10):                          # 5 run days, prompt edit on D2
        world.simulate_segment(cfg, seg, prompt=0 if seg < 4 else 1)
    ws = world.snapshot(cfg, 10, experiments=exps)
    sim_s = time.perf_counter() - t_sim
    print(f"synthetic data: {len(ws.engage)} r_engage rows, {len(cells)} cells, "
          f"{int(ws.engage.connected.sum())} connected calls, built in {sim_s:.1f} s")
    rows = []
    for model in (BetaBinomialModel(), RidgeLogitModel()):
        t_stat, post_s = timed(lambda: model.fit(ws, "stationary"), args.repeat)
        t_ew, post_e = timed(lambda: model.fit(ws, "ew"), args.repeat)
        t_draw, draws = timed(lambda: model.draws(post_e, 11, 4000), args.repeat)
        extra = post_s.summary.get("rows", "")
        stat_d = model.draws(post_s, 10, 4000)
        t_plan, plan = timed(lambda: plan_allocation_detail(ws, draws), 1)
        t_look, _ = timed(lambda: evaluate_looks(ws, stat_d, draws), 1)
        rows.append((model.name, len(post_s.mean), extra, t_stat, t_ew, t_draw, t_plan, t_look))
    print("\n| model | params | design rows | stationary fit (s) | EW fit (s) | 4,000 draws (s) "
          "| allocation plan (s) | winner looks (s) |")
    print("|---|---|---|---|---|---|---|---|")
    for name, n, extra, a, b, c, d, e in rows:
        print(f"| {name} | {n} | {extra} | {a:.3f} | {b:.3f} | {c:.3f} | {d:.2f} | {e:.2f} |")


if __name__ == "__main__":
    main()
