"""Request builder (DESIGN.md 6.6, ADR 0020): text, affected leads, dry run, impact score."""
from __future__ import annotations

import math

import numpy as np
import pytest

from controller.governance.builder import (BuildContext, affected_leads, build_request,
                                           change_items, change_summary, changed_cells, pct,
                                           prompt_fix_impact, replay_caller_id,
                                           replay_engagement)
from controller.planner.overlay import Overlay, OverlayContext
from controller.types import (P0_INCIDENT, P3_ROUTINE, Cell, CohortDef, DidPosterior, Draws,
                              FrozenDict, ParamIndex, PatchRequest)
from tests.unit.gov_fixtures import base_config, spec

CELLS = [Cell(1, "hi", "1", "60+", "mid"), Cell(2, "en", "2", "18-30", "low"),
         Cell(3, "hi", "3", "31-45", "high")]


def draws_voice_v3(ratios: list[float]) -> Draws:
    idx = ParamIndex(names=("voice:V3",), cell_ids=(1, 2, 3),
                     effect_maps=FrozenDict({("voice", "V3"): np.ones((3, 1))}))
    return Draws(kind="ew", seed=7, snapshot_id=1,
                 params=np.log(np.array(ratios)).reshape(-1, 1), index=idx)


# ---------------------------------------------------------------- text
def test_pct():
    assert (pct(0.04), pct(0.025), pct(1 / 3), pct(1.0)) == ("4%", "2.5%", "33.3%", "100%")


def test_change_item_lines():
    live = base_config(voice={"V1": 0.96, "V3": 0.04})
    new = live.with_allocation("default", "voice", {"V1": 0.9, "V3": 0.1}) \
        .with_allocation("default", "caller_id", {"D1": 0.4, "D2": 0.5, "D3": 0.1})
    items = change_items(live, new)
    assert [i.text for i in items] == [
        "Voice: raise V3 from 4% to 10%; lower V1 from 96% to 90%",
        "Caller ID: move 10% from D1 to D3"]
    arm = base_config().with_allocation("default", "welcome_message", {"W1": 0.96, "W2": 0.04})
    assert change_items(base_config(), arm)[0].text == \
        "Welcome message: try W2 on 4% of calls (new test); lower W1 from 100% to 96%"
    fix = base_config().with_prompt("P3")
    assert change_items(base_config().with_prompt("P2"), fix,
                        {("prompt",): {"validation_run_id": "val_00004"}})[0].text == \
        "Prompt: switch from P2 to P3 (validation run val_00004)"
    coh = base_config().with_cohorts([CohortDef.make("senior", {"age_band": ["60+"]})])
    assert change_items(base_config(), coh)[0].text == "Cohorts: add senior"


def test_change_summary_batches_and_notes():
    live = base_config()
    new = live.with_allocation("default", "caller_id", {"D1": 0.4, "D2": 0.5, "D3": 0.1}) \
        .with_prompt("P3")
    s = change_summary(change_items(live, new), ["changes the voice your supervisor set on D5 15:00"])
    assert s == ("2 changes: Caller ID: move 10% from D1 to D3; Prompt: switch from P1 to P3. "
                 "Note: this changes the voice your supervisor set on D5 15:00")


# ---------------------------------------------------------------- affected leads
def test_changed_cells_and_affected_leads():
    live = base_config().with_cohorts([CohortDef.make("senior", {"age_band": ["60+"]})])
    new = live.with_allocation("senior", "voice", {"V1": 0.9, "V3": 0.1})
    assert changed_cells(live, new, CELLS) == [1]
    everyone = live.with_prompt("P3")
    assert changed_cells(live, everyone, CELLS) == [1, 2, 3]
    active = {1: 100, 2: 50, 3: 10}
    share = {1: 0.2, 2: 0.3, 3: 0.5}
    # active 150 in cells 1-2 plus 1000 x 0.5 of the next batch
    assert affected_leads([1, 2], active, 1000, share, False) == 650
    assert affected_leads([1, 2], active, 1000, share, True) == 500   # next_campaign_only


# ---------------------------------------------------------------- dry run
def test_replay_engagement_hand_values():
    live = base_config()
    new = live.with_allocation("default", "voice", {"V1": 0.9, "V3": 0.1})
    r = replay_engagement(live, new, CELLS, {1: 1, 2: 1, 3: 1},
                          draws_voice_v3([1.1, 0.8, 1.0, 1.2]))
    # per draw de = 0.1 (r - 1): 0.01, -0.02, 0, 0.02
    assert r.mean_rel == pytest.approx(0.0025)
    assert r.p_harm == pytest.approx(0.25)
    assert r.q20_rel == pytest.approx(-0.008)        # numpy linear quantile of the 4 draws
    assert r.ci90[0] == pytest.approx(-0.02 + 0.15 * 0.02)


def test_replay_engagement_weights_cells_by_mix():
    live = base_config().with_cohorts([CohortDef.make("senior", {"age_band": ["60+"]})])
    new = live.with_allocation("senior", "voice", {"V3": 1})    # only cell 1 hears V3
    r = replay_engagement(live, new, CELLS, {1: 1, 2: 3, 3: 0}, draws_voice_v3([1.2]))
    assert r.mean_rel == pytest.approx(0.2 * 1 / 4)


def test_replay_caller_id_hand_values():
    live = base_config()
    new = live.with_allocation("default", "caller_id", {"D1": 0.5, "D2": 0.4, "D3": 0.1})
    post = DidPosterior(wf="wf", dids=("D1", "D2", "D3"),
                        draws=np.array([[1.0, 0.5, 1.0]]), status=FrozenDict())
    r = replay_caller_id(live, new, CELLS, {1: 1, 2: 1, 3: 1}, post)
    assert r.mean_rel == pytest.approx(0.8 / 0.75 - 1)


def test_prompt_fix_impact():
    assert prompt_fix_impact(0.03, 0.006, 0.6, 0.2, 0.55) == pytest.approx(0.024 * 0.4 / 0.55)
    assert prompt_fix_impact(0.006, 0.03, 0.6, 0.2, 0.55) == 0.0


# ---------------------------------------------------------------- the package
def test_build_request_bundle_scores_highest_class_item():
    s = spec()
    ov = Overlay(lambda wf: OverlayContext(spec=s, did_state={"D1": "healthy", "D2": "suspect"},
                                           controls={("default", "voice"): "V1"}),
                 lambda: 600)
    live = base_config().with_prompt("P2")
    ov.add(PatchRequest("wf_test", "incidents", ("prompt",), "P3", P0_INCIDENT, 0,
                        FrozenDict({"rule_id": "7.1/lkg", "validation_run_id": "val_00004",
                                    "pooled_n": 1300}), 11))
    ov.add(PatchRequest("wf_test", "allocator", ("alloc", "default", "voice"),
                        {"V1": 0.9, "V3": 0.1}, P3_ROUTINE, None,
                        FrozenDict({"rule_id": "5.4/explore"}), 12))
    comp = ov.compose("wf_test", live, {0, 3}, base_version=6)
    ctx = BuildContext(primary_kpi="engagement_rate", batch_size=1000, mid_flight="allowed",
                       cells=CELLS, active_by_cell={1: 100, 2: 50, 3: 10},
                       hist_share_by_cell={1: 0.2, 2: 0.3, 3: 0.5}, mix={1: 1, 2: 1, 3: 1},
                       draws=draws_voice_v3([1.1, 1.05, 1.0, 1.2]), prompt_impact=0.0175)
    pkg = build_request(comp, ctx)
    assert pkg.impact_kpi == "engagement_rate" and pkg.impact_score == pytest.approx(0.0175)
    assert [s.lever for s in pkg.lever_scores] == ["prompt", "voice"]
    assert pkg.evidence["validation_run_id"] == "val_00004"
    assert pkg.evidence["class"] == 0 and pkg.evidence["p0_rank"] == 0
    assert pkg.affected_leads == 1160                       # prompt reaches every cell
    assert isinstance(pkg.affected_leads, int)
    assert pkg.dry_run["primary"]["kpi"] == "engagement_rate"
    assert pkg.dry_run["leads_hearing_a_different_variant_24h"] == round(
        0.1 * (100 + 200 + 50 + 300 + 10 + 500))
    assert "Prompt: switch from P2 to P3 (validation run val_00004)" in pkg.change_summary
    assert "rule 7.1/lkg, decision #11 (pooled_n=1300, validation_run_id=val_00004)" in \
        pkg.rationale
    assert pkg.diff["base_version"] == 6 and pkg.diff["items"]
    # deterministic: the same inputs give the same package
    assert build_request(comp, ctx) == pkg
    assert math.isfinite(pkg.impact_score) and 0 <= pkg.impact_score <= 1


def test_build_request_without_posterior_is_honest():
    s = spec()
    ov = Overlay(lambda wf: OverlayContext(spec=s), lambda: 600)
    ov.add(PatchRequest("wf_test", "did", ("alloc", "default", "caller_id"),
                        {"D1": 0.45, "D2": 0.45, "D3": 0.1}, P3_ROUTINE, None, FrozenDict(), 3))
    comp = ov.compose("wf_test", base_config(), {3}, base_version=2)
    pkg = build_request(comp, BuildContext(primary_kpi="engagement_rate", batch_size=1000,
                                           mid_flight="allowed"))
    assert pkg.impact_score == 0.0 and pkg.impact_kpi == "pickup_rate"
    assert pkg.dry_run["primary"]["note"].startswith("not estimated")
    assert pkg.change_summary and pkg.rationale and pkg.diff
