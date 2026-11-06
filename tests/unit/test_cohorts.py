"""Cohorts (DESIGN.md 5.2): cells, the D0 grid, business cohorts first, minimum sizes, versioned
identity, the winner-scope floor."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.cohorts.cells import CellCatalog
from controller.cohorts.cohortset import GridCohortSet, business_entries, cohort_key
from controller.cohorts.grid import (GridInputs, additive_logit_deviance, build_grid, chi2_sf)
from controller.settings import Thresholds
from controller.types import DEFAULT_COHORT, CohortDef
from tests.support import synth

HOURS = tuple(range(9, 21))
LANG_MIX = {"en": 0.3, "hi": 0.5, "ta": 0.2}
AGE_MIX = {"18-30": 0.3, "31-45": 0.3, "46-60": 0.25, "60+": 0.15}


def age_curve(age: str, h: int) -> float:
    """Morning people are old, evening people young (an invented, strong F1 signal)."""
    base = 0.25
    if age == "60+":
        return base * (1.6 if h < 12 else 0.7)
    if age == "18-30":
        return base * (1.6 if h >= 18 else 0.75)
    return base


def history(seed: int = 1, days: int = 14, leads_per_day: float = 1200.0, flat: bool = False):
    rng = np.random.default_rng(seed)
    cat = synth.full_catalog()
    rows, leads = [], {}
    for c in cat.ids():
        cell = cat.get(c)
        share = (LANG_MIX[cell.lang] * AGE_MIX[cell.age] / 3 / 3)
        leads[c] = leads_per_day * share
        for d in range(-days, 0):
            for h in HOURS:
                n = rng.poisson(leads[c] * 2.2 / len(HOURS))
                if n == 0:
                    continue
                p = 0.25 if flat else age_curve(cell.age, h)
                rows.append((d, h, c, 1, int(n), int(rng.binomial(n, p))))
    return cat, synth.pickup_arrays(rows), leads


def test_chi2_sf_matches_known_values():
    assert chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    assert chi2_sf(18.307038053275146, 10) == pytest.approx(0.05, abs=1e-9)
    assert chi2_sf(0.0, 3) == 1.0
    assert chi2_sf(200.0, 30) < 1e-20


def test_additive_deviance_is_zero_for_an_additive_table():
    n = np.full((3, 2), 1000.0)
    # logit(p) = a_r + b_c exactly
    lo = np.array([[-1.0, -0.5], [-0.7, -0.2], [0.0, 0.5]])
    y = n / (1 + np.exp(-lo))
    dev, df = additive_logit_deviance(n, y)
    assert df == 2 and dev == pytest.approx(0.0, abs=1e-6)


def test_cells_are_a_dictionary_of_observed_values():
    cat = CellCatalog()
    a = cat.add({"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"})
    b = cat.add({"lang": "xx", "city_tier": "1", "age_band": "60+", "value_band": "low"})
    assert (a, b) == (0, 1)
    assert cat.add({"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"}) == 0
    assert cat.levels("lang") == ("hi", "xx")
    again = CellCatalog.from_rows([(0, "hi", "1", "60+", "low"), (1, "xx", "1", "60+", "low")])
    assert again.ids() == (0, 1) and again.get(1).lang == "xx"


def test_grid_picks_age_as_f1_and_lang_as_f2():
    cat, pk, leads = history()
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads))
    assert gs.evidence["f1"] == "age_band"
    assert gs.evidence["f2"] == "lang"
    assert gs.evidence["f1_scores"]["age_band"]["p"] < 1e-3
    assert 0 < len(gs.entries) <= 20
    # every grid cohort constrains F1, specific ones also F2, buckets (if any) come last
    kinds = [dict(e.definition.where).keys() >= {"age_band", "lang"} for e in gs.entries]
    assert kinds == sorted(kinds, reverse=True)


def test_grid_respects_the_targeting_minimum():
    cat, pk, leads = history()
    t = Thresholds()
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads), t)
    total = sum(leads.values())
    eff = gs.effective_cells()
    for e in gs.entries:
        share = sum(leads[c] for c in eff[e.key]) / total
        assert share >= t.targeting_cohort_min_share - 1e-9, (e.key, share)
    # the default cohort holds what fell through, everything is assigned exactly once
    assert sum(len(v) for v in eff.values()) == len(cat)


def test_no_significant_hour_signal_means_no_f1():
    cat, pk, leads = history(flat=True)
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads))
    assert gs.evidence["f1"] is None
    assert gs.evidence["f2"] == "lang"
    assert [dict(e.definition.where) for e in gs.entries] == [
        {"lang": ("en",)}, {"lang": ("hi",)}, {"lang": ("ta",)}]


def test_business_cohorts_stay_first_verbatim_and_supervisor_owned():
    cat, pk, leads = history()
    biz = (CohortDef.make("vip", {"value_band": ["high"], "lang": ["ta", "en"]}),
           CohortDef.make("seniors", {"age_band": ["60+"]}))
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, business_cohorts=biz, leads_by_cell=leads))
    assert gs.to_config()[:2] == biz
    assert gs.owner("vip") == "supervisor" and gs.origin("seniors") == "business"
    assert len(gs.entries) <= 20
    # first match: business cohorts keep every lead they matched
    for c in cat.ids():
        feats = cat.get(c).features()
        if biz[0].matches(feats):
            assert gs.cohort_of(c) == "vip"
        elif biz[1].matches(feats):
            assert gs.cohort_of(c) == "seniors"
        else:
            assert gs.cohort_of(c) not in ("vip", "seniors")
    grid_keys = [e.key for e in gs.entries[2:]]
    assert all(gs.owner(k) == "ours" for k in grid_keys)


def test_cap_counts_business_cohorts():
    cat, pk, leads = history()
    biz = tuple(CohortDef.make(f"b{i}", {"value_band": ["high"], "city_tier": [str(i % 3 + 1)],
                                         "age_band": ["60+"] if i < 3 else ["18-30"]})
                for i in range(6))
    t = Thresholds(targeting_cohort_min_share=0.0)   # force every cell to be its own cohort
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, business_cohorts=biz,
                               leads_by_cell=leads), t)
    assert len(gs.entries) <= 20
    assert gs.to_config()[:6] == biz


def test_cohort_identity_is_deterministic_and_changes_with_the_definition():
    w = (("age_band", ("60+",)), ("lang", ("ta",)))
    k1 = cohort_key("nw_renewal", w, 3)
    assert k1 == cohort_key("nw_renewal", w, 3)
    assert k1.startswith("age60p_ta~") and len(k1.split("~")[1]) == 6
    assert cohort_key("nw_renewal", (("age_band", ("60+",)), ("lang", ("hi",))), 3) != k1
    assert cohort_key("nw_renewal", w, 4) != k1
    assert cohort_key("other_wf", w, 3) != k1
    cat, pk, leads = history()
    a = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads))
    b = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads))
    assert a.to_config() == b.to_config() and a.key() == b.key()


def test_membership_per_config_version_and_supervisor_adoption():
    cat, pk, leads = history()
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads))
    gs.register_version(1, ())
    gs.register_version(2, gs.to_config())
    assert set(gs.membership(1)) == {DEFAULT_COHORT}
    assert gs.membership(2)[gs.entries[0].key] == gs.effective_cells()[gs.entries[0].key]
    with pytest.raises(KeyError):
        gs.membership(9)
    edited = (CohortDef.make("sup_new", {"lang": ["en"]}),) + gs.to_config()
    gs2 = gs.adopt(edited)
    assert gs2.version == gs.version + 1
    assert gs2.owner("sup_new") == "supervisor" and gs2.origin("sup_new") == "supervisor"
    assert gs2.origin(gs.entries[0].key) == "grid"


def test_winner_scope_floor():
    cat, pk, leads = history()
    gs = build_grid(GridInputs("wf_a", cat, pk, HOURS, leads_by_cell=leads, end_minute=10 * 1440))
    spec = synth.make_spec()
    ws = synth.snapshot(spec, cohorts=gs, pickup=pk, now=0)
    shares = gs.connected_share(ws)
    big = max(shares, key=lambda k: shares[k] if k != DEFAULT_COHORT else -1)
    small = min((k for k in shares if k != DEFAULT_COHORT), key=lambda k: shares[k])
    assert shares[small] < 0.15
    assert not gs.winner_scope_eligible(small, ws)
    assert not gs.winner_scope_eligible(DEFAULT_COHORT, ws)
    per_day = gs.connected_per_day(ws)[big]
    eligible = shares[big] >= 0.15 and gs.projected_arm_calls(per_day, 0) >= 800
    assert gs.winner_scope_eligible(big, ws) == eligible
    # near the freeze nothing can reach the look
    late = synth.snapshot(spec, cohorts=gs, pickup=pk, now=8 * 1440)
    assert not gs.winner_scope_eligible(big, late)


def test_projected_ramp_matches_the_design_table():
    """5.5: a 50% cohort at 950 connected/day reaches n = 800 around D6 (sanity check)."""
    cat = synth.full_catalog()
    gs = GridCohortSet("wf", (), cat, end_minute=100 * 1440)
    reached = [gs.projected_arm_calls(475.0, 0) for _ in range(1)]
    assert reached[0] > 800
    days = 0
    gs_short = GridCohortSet("wf", (), cat, end_minute=None)
    assert math.isinf(gs_short.projected_arm_calls(1.0, 0))
    # cumulative 475 x (0.038, 0.12, 0.25, 0.30, 0.40, 0.50): 18, 75, 194, 336, 526, 764, 1001
    cum = 0.0
    for share in (0.038, 0.12, 0.25, 0.30, 0.40, 0.50, 0.50):
        cum += 475 * share
        days += 1
        if cum >= 800:
            break
    assert days == 7   # reached during D6, as the design says


def test_business_entries_helper():
    cat = synth.full_catalog()
    e = business_entries([CohortDef.make("x", {"lang": ["en"]})], cat)
    assert e[0].owner == "supervisor" and len(e[0].matched_cells) == 36
