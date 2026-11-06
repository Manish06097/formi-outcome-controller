"""False-winner rate per hypothesis, by scope, under the real adaptive allocation (DESIGN.md
5.5, 10.1, ADR 0005). numpy only: no simulator.

Every arm is null (multiplier 1 everywhere). The real loop runs: the Beta-binomial model, the
harm-gated probability-matching allocator moving shares every day, information-doubling looks,
the full winner rule W1 to W6 per scope, promotions with holdouts and decay rollbacks. A
hypothesis is (arm, scope): 5 challenger arms (2 welcome, 3 voice) x 3 scopes (population, a
50% cohort, the default cohort). Each replicate is 10 days at 3,000 connected calls a day, so
every scope reaches looks of 1,000+ arm calls and the full doubling schedule is exercised.

The design's union bounds are 0.54% (population) and 0.47% (cohort) per hypothesis over FOUR
looks (200 to 1,600). At this volume the doubling schedule goes on to 3,200 and 6,400 calls, and
each further look adds about 0.35% of null crossings to W1 alone, so W1 over all looks is not
covered by that bound (measured about 0.9% at population scope; see the T3 report and ADR 0005).
The acceptance lines, each at most 1% per hypothesis and scope:
- the full rule (W1 to W6) over every look, which is what declares winners;
- W1 alone within the first four looks, the claim F13 makes.
"""
from __future__ import annotations

import dataclasses

from controller.settings import Thresholds
from controller.stats.boundaries import null_crossing
from controller.stats.ratios import wilson
from controller.types import CohortDef
from tests.support.t3_loop import run_loop
from tests.support.t3_world import base_config, make_cells, make_spec, uniform_world

N_REPS = 300
DAYS = 10
ARMS = 5
COHORT_SCOPES = ("old~000001", "default")


def test_false_winner_rate_per_hypothesis_is_at_most_one_percent_in_each_scope():
    spec = make_spec()
    cells = make_cells(langs=("en", "hi"), tiers=("t1", "t2"), ages=("18-30", "60+"),
                       values=("low",))
    t = dataclasses.replace(Thresholds(), n_draws=1000)
    cohorts = [CohortDef.make("old~000001", {"age_band": ["60+"]})]
    wins = {"population": 0, "cohort": 0}
    w1 = {"population": 0, "cohort": 0}
    w1_any = {"population": 0, "cohort": 0}
    max_n = {"population": 0, "cohort": 0}
    looks = 0
    bound = {"population": 0.0, "cohort": 0.0}
    for rep in range(N_REPS):
        world = uniform_world(seed=10_000 + rep, connected_per_day=3000, cells=cells, spec=spec)
        r = run_loop(world, base_config(spec, cohorts), DAYS, seed=rep, thresholds=t,
                     steps_per_seg=1, plan_every=2, apply_delay=1)   # daily planning points
        for win in r.winners:
            wins["population" if win.scope == "population" else "cohort"] += 1
        for (_, _, scope), look_no in r.w1_crossings.items():
            k = "population" if scope == "population" else "cohort"
            w1_any[k] += 1
            w1[k] += look_no <= 4
        for lk in r.looks:
            k = "population" if lk.scope == "population" else "cohort"
            max_n[k] = max(max_n[k], lk.n_at_look)
        looks += len(r.looks)
        # the union bound at the information levels the looks actually happened at
        for lk in r.looks:
            if lk.look_no <= 4:
                k = "population" if lk.scope == "population" else "cohort"
                tau = 0.06 if k == "population" else 0.12
                c = t.w1_population if k == "population" else t.w1_cohort
                bound[k] += null_crossing(lk.n_at_look / 0.82, tau, c)
        assert all(c.verdict != "harmful" for c in r.changes)   # null world: nothing can harm
    hyps = {"population": N_REPS * ARMS, "cohort": N_REPS * ARMS * len(COHORT_SCOPES)}
    rates = {k: wins[k] / hyps[k] for k in hyps}
    w1_rates = {k: w1[k] / hyps[k] for k in hyps}
    w1_any_rates = {k: w1_any[k] / hyps[k] for k in hyps}
    print(f"\nfalse winners (full rule): {wins} of {hyps} hypotheses -> {rates}; "
          f"W1 alone, first 4 looks: {w1_rates}; W1 alone, all looks: {w1_any_rates}; "
          f"looks {looks}; largest look n {max_n}; union bound at the actual first four looks "
          f"{ {k: bound[k] / hyps[k] for k in hyps} }")
    assert max_n["population"] >= 1600 and max_n["cohort"] >= 800   # the schedule is exercised
    for k in hyps:
        assert rates[k] <= 0.01, (k, rates[k])
        assert w1_rates[k] <= 0.01, (k, w1_rates[k])
        # and the rate is credibly below the line, not just by luck
        assert wilson(w1[k], hyps[k], 0.90)[1] <= 0.015, (k, w1[k])
