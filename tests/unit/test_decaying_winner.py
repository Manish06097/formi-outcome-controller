"""A synthetic decaying winner: no ramp or promotion may be graded harmful (DESIGN.md F1, 5.5,
5.7). numpy only: no simulator.

Truth (our own, not public): one welcome variant starts strong and decays below the control a
few days after its first dial, the decay clock starting at first exposure, as in the simulator
(world.py effect_mult_now). The real loop runs the Beta-binomial model, probability matching,
the winner rule, promotion with holdout and the decay monitor with timed rollbacks. Every applied
change is graded at its apply time by the grader's rule (grader.py:67-73) on expected engagement
computed from the synthetic truth: harmful if it falls by more than 1%.

Assertions (DESIGN.md F1 and 0.1):
- no change that RAISES the decaying arm's share anywhere (exploration, ramp or promotion of it)
  is graded harmful, in any replicate: the EW posterior, the W3 recency check and the gates'
  apply-time scenario must stop it from the moment the decline is in the data;
- no promotion of any arm and no rollback is graded harmful;
- over all changes, the harmful share stays within the gate's design tail alpha_H = 2.5%: the
  gate is a calibrated posterior bound, so a mild arm cut on posterior noise is possible at
  that rate (seen: a +5% voice cut in one cohort), and it is reported, not hidden.
"""
from __future__ import annotations

import dataclasses

import pytest

from controller.settings import Thresholds
from controller.types import CohortDef
from tests.support.t3_loop import run_loop
from tests.support.t3_world import base_config, make_cells, make_spec, uniform_world

GROWTH_KINDS = ("exploration", "exploitation", "promotion")


def decaying(peak: float, to: float, after_days: float, over_days: float):
    def mult(dim, option, cell, days):
        if (dim, option) == ("welcome_message", "W2"):
            if days <= after_days:
                return peak
            f = min((days - after_days) / over_days, 1.0)
            return peak + (to - peak) * f
        if (dim, option) == ("voice", "V3"):
            return 1.05
        return 1.0
    return mult


CASES = [
    # (connected per day, peak, to, after, over): dev-like and load-like volumes,
    # slow (public_01-like shape) and fast (public_02-like shape) decays
    (950, 1.15, 0.93, 3.0, 2.0),
    (950, 1.18, 0.90, 2.0, 1.0),
    (3000, 1.15, 0.93, 3.0, 2.0),
    (3000, 1.18, 0.90, 2.0, 1.0),
]


@pytest.mark.parametrize("per_day,peak,to,after,over", CASES)
def test_no_ramp_or_promotion_of_a_decaying_winner_is_harmful(per_day, peak, to, after, over):
    spec = make_spec()
    cells = make_cells(langs=("en", "hi"), tiers=("t1", "t2"), ages=("18-30", "60+"),
                       values=("low",))
    t = dataclasses.replace(Thresholds(), n_draws=2000)
    cohorts = [CohortDef.make("old~000001", {"age_band": ["60+"]})]
    harmful, rollbacks, promotions, decay_ramps, total = [], [], [], [], 0
    for seed in range(4):
        world = uniform_world(seed=500 + seed, connected_per_day=per_day, cells=cells,
                              mult=decaying(peak, to, after, over), spec=spec)
        r = run_loop(world, base_config(spec, cohorts), 10, seed=seed, thresholds=t)
        total += len(r.changes)
        for c in r.changes:
            row = (seed, c.kind, c.pos_applied, c.verdict, round(c.de, 4))
            if c.verdict == "harmful":
                harmful.append(row)
            if c.raises("welcome_message", "W2"):
                decay_ramps.append(row)
            if c.kind == "rollback":
                rollbacks.append(row)
            if c.kind == "promotion":
                promotions.append((seed, c.pos_applied, c.detail.get("arm"), c.verdict))
    print(f"\nper_day={per_day} decay {peak}->{to} after {after}d over {over}d: "
          f"{total} changes; harmful {harmful}; promotions {promotions}; "
          f"rollbacks {[(r_[2], r_[3]) for r_ in rollbacks]}")
    assert [r_ for r_ in decay_ramps if r_[3] == "harmful"] == []
    assert all(v != "harmful" for _, _, _, v in promotions)
    assert all(r_[3] != "harmful" for r_ in rollbacks)
    assert len(harmful) <= 0.025 * total, (len(harmful), total)
