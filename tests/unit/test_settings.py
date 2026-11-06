"""Settings: every threshold documented, the 5.11 register values, and settings_hash behaviour."""
from __future__ import annotations

import dataclasses
import math

import pytest

from controller.settings import (EnvSettings, Settings, Thresholds, load_settings,
                                 threshold_register)


def test_every_threshold_has_a_section_and_a_derivation():
    for f in dataclasses.fields(Thresholds):
        assert f.metadata.get("section"), f.name
        assert len(f.metadata.get("doc", "")) > 10, f.name
        assert "tunable" in f.metadata, f.name


def test_register_values_match_design_5_11():
    """The register of DESIGN.md 5.11, row by row (hand-copied fixture)."""
    t = Thresholds()
    expected = {
        "alpha_h": 0.025, "alpha_h_rollback": 0.10, "alpha_d": 0.05,
        "cohort_harm_line_default": 0.02, "tau_e": 0.13, "tau_h_floor": 0.06,
        "ew_half_life_segments": 2.0, "arm_floor": 0.02, "arm_ceiling": 0.50,
        "bisection_iters": 12, "min_exploit_gain": 0.0025, "harm_stop_p": 0.95,
        "harm_stop_loss": 0.005, "arm_removal_p": 0.80, "futility_p": 0.05,
        "w1_population": 0.995, "w1_cohort": 0.998, "w2_p_best": 0.95, "w3_ew_p_pos": 0.90,
        "w3_ew_ratio": 0.5, "look_first_n": 200, "look_first_days": 2, "look_growth": 2.0,
        "targeting_cohort_min_share": 0.05, "winner_scope_min_share": 0.15,
        "winner_scope_projected_n": 800, "holdout_grid": (0.05, 0.10, 0.15, 0.20),
        "holdout_se_target": 0.075, "decay_cusum_k": 0.5, "decay_cusum_h": 4.0,
        "decay_arm_share": 0.10, "rollback_tolerance": 0.10, "hour_curve_prior": 200.0,
        "hour_weight_kappa": 2.0, "hour_weight_min": 0.25, "hour_weight_max": 4.0,
        "schedule_refresh_l1": 0.1, "schedule_verify_min_dials": 30,
        "schedule_verify_fail_frac": 0.10, "grid_f1_p": 0.001, "grid_f2_lang_min_langs": 2,
        "grid_f2_lang_max_langs": 5, "grid_f2_lang_min_share": 0.10,
        "did_prior_p_flagged": 0.2, "did_r_flag": 0.35, "sprt_r_unhealthy": 0.70,
        "sprt_r_healthy": 1.0, "sprt_alpha": 0.01, "sprt_beta": 0.05, "sprt_n_max": 1000,
        "did_probe_min": 0.005, "did_probe_max": 0.028, "did_probe_dials_per_day": 400,
        "did_slow_windows_h": (12, 24, 48), "sprt_indifference_r": 0.85, "did_readmit_days": 3,
        "did_fast_windows_h": (2, 4, 8), "did_fast_z": -4.0, "did_fast_r": 0.75,
        "did_fast_min_dials": 100, "did_suspect_window_h": 1, "did_suspect_z": -2.5,
        "drain_verify_ratio": 0.9, "drain_verify_after_h": 2, "drain_wait_holder_max_min": 60,
        "drain_chain_first_max_min": 30, "guardrail_window_days": 2, "guardrail_p_breach": 0.025,
        "guardrail_mix_attrib_frac": 0.5, "drift_k": 0.5, "drift_h": 6.0,
        "drift_armed_dials": 2000, "verdict_stage1_n": 300, "verdict_stage1_unsafe_f": 10,
        "verdict_stage1_safe_f": 3, "verdict_stage2_n": 1000, "verdict_pooled_unsafe_frac": 0.02,
        "fix_verify_connected": 300, "fix_verify_flag_ratio": 2.0, "fix_verify_engage_lcb": -0.05,
        "flag_cusum_ratio": 2.0, "flag_cusum_horizon_days": 30.0, "flag_cusum_min_flags": 5,
        "flag_check_ratio": 1.6, "flag_check_z": 4.0, "flag_check_min_connected": 2000,
        "credit_reserve_max_frac": 0.8, "credit_reserve_min_frac": 0.3,
        "credit_reserve_worst_mult": 2.0, "credit_screen_frac": 0.25, "credit_full_verdict": 13,
        "submit_expiry_p_max": 0.01, "slot_offset_max_min": 60, "slot_offset_window_frac": 0.25,
        "slot2_offset_min": 240, "cap1_spacing_min": 2880, "supervisor_cooldown_min": 1440,
        "late_decision_recheck_min": 1200, "held_patch_release_min": 360,
        "held_patch_max_holds": 2, "alloc_verify_segments": 2, "alloc_verify_se": 3.0,
        "alloc_verify_p_harm": 0.10, "suspect_defer_min": 120, "freeze_min_days": 1.0,
        "freeze_frac": 0.15, "ramp_stop_days": 1.0, "base_step_min": 60,
    }
    for name, value in expected.items():
        assert getattr(t, name) == value, name
    assert math.isclose(t.futility_margin, math.log(1.02))
    # the pooled bar is 26 failures out of 1,300 (7.1 table)
    n = t.verdict_stage1_n + t.verdict_stage2_n
    assert math.ceil(t.verdict_pooled_unsafe_frac * n) == 26


def test_settings_hash_is_stable_and_ignores_connection_details():
    a = load_settings({})
    b = load_settings({"CTL_PG_PORT": "55441", "CTL_SIM_URL": "http://127.0.0.1:8711",
                       "CTL_FAULT": "advance:1:after_t1"})
    assert a.settings_hash() == b.settings_hash() == Settings().settings_hash()
    assert len(a.settings_hash()) == 16


def test_settings_hash_changes_with_a_threshold_or_a_decision_flag():
    base = load_settings({}).settings_hash()
    assert load_settings({"CTL_T_ALPHA_H": "0.03"}).settings_hash() != base
    assert load_settings({"CTL_ENGAGEMENT_MODEL": "ridge_logit"}).settings_hash() != base
    assert load_settings({"CTL_LLM_SUGGEST": "0"}).settings_hash() != base


def test_threshold_overrides_coerce_types():
    s = load_settings({"CTL_T_DID_FAST_WINDOWS_H": "2,4", "CTL_T_SPRT_N_MAX": "500",
                       "CTL_T_ALPHA_D": "0.04"})
    assert s.t.did_fast_windows_h == (2, 4)
    assert s.t.sprt_n_max == 500 and isinstance(s.t.sprt_n_max, int)
    assert s.t.alpha_d == 0.04


def test_settings_are_frozen_and_hashable():
    s = load_settings({})
    assert hash(s) == hash(load_settings({}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.t.alpha_h = 0.5  # type: ignore[misc]


def test_env_parsing_and_dsn():
    e = EnvSettings.from_env({"SIM_URL": "http://h:1", "CTL_PG_PORT": "55441",
                              "CTL_LLM_SUGGEST": "0"})
    assert e.sim_url == "http://h:1" and e.pg_port == 55441 and not e.llm_suggest
    assert "port=55441" in e.dsn
    assert EnvSettings.from_env({"PG_DSN": "postgresql://x/y"}).dsn == "postgresql://x/y"
    assert EnvSettings.from_env({"CTL_SIM_URL": "http://a:1", "SIM_URL": "http://b:2"}).sim_url \
        == "http://a:1"
    with pytest.raises(ValueError):
        EnvSettings.from_env({"CTL_ENGAGEMENT_MODEL": "llm"})


def test_register_lists_every_field():
    reg = threshold_register()
    assert len(reg) == len(dataclasses.fields(Thresholds))
    assert {r["name"] for r in reg if not r["tunable"]} >= {"grader_engagement_line",
                                                            "grader_did_line", "max_cohorts"}


def test_loop_knobs_live_in_env_settings():
    """ADR 0028: the loop's test and tuning knobs are EnvSettings fields (one parser)."""
    e = EnvSettings.from_env({"CTL_STOP_AFTER_STEPS": "5", "CTL_PREFETCH_CHUNKS": "0",
                              "CTL_SYNTHETIC_MARKERS": "pending:wf_a:600:900, approved:wf_b:0:90"})
    assert e.stop_after_steps == 5 and e.prefetch_chunks == 0
    assert e.synthetic_markers == (("pending", "wf_a", 600, 900), ("approved", "wf_b", 0, 90))
    d = EnvSettings.from_env({})
    assert (d.stop_after_steps, d.prefetch_chunks, d.synthetic_markers) == (None, 1, ())
    for bad in ("held:wf:0:1", "pending:wf:0", "pending:wf:x:1"):
        with pytest.raises(ValueError):
            EnvSettings.from_env({"CTL_SYNTHETIC_MARKERS": bad})
    # not decision inputs: the hash ignores them
    assert Settings(env=e).settings_hash() == Settings(env=d).settings_hash()
