"""Environment settings and every threshold of the design, each with its derivation.

Owner: M0. Implements DESIGN.md 5.11 (threshold register) and the `[T]` values of every other
section, plus the environment of 11.3 (run.sh and compose).

Three kinds of number live here, and each field says which it is:
- `[T]` thresholds (`tunable=True`): tune ONLY on private scenario variants written by our test
  harness, never on public-scenario truth (DESIGN.md notation, 5.11).
- Contract and grader constants (`tunable=False`): rules of the simulator contract or the grader
  that the API does not expose (SIMULATOR.md, UNDERSTANDING.md 5). Changing them is a bug.
- Environment settings (`EnvSettings`): where to connect and what to run; not part of decisions,
  except the few marked `decision=True` (they enter `settings_hash`).

Every policy parameter the API does expose (tick, hours, TTL, caps, windows, options) is read
from the API at runtime and is deliberately NOT here (CLAUDE.md hard rules).

`settings_hash` is stored on every decision row (DESIGN.md 9.6), so `replay-decision` can say
when the running thresholds differ from the ones a decision was made with.

Overrides: any threshold can be overridden by `CTL_T_<FIELD_NAME_UPPER>` (for the private
variants harness); an override changes `settings_hash`, so it is visible in replay.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping


def T(value: Any, section: str, doc: str) -> Any:
    """A tunable `[T]` threshold."""
    return field(default=value, metadata={"section": section, "doc": doc, "tunable": True})


def C(value: Any, section: str, doc: str) -> Any:
    """A contract or grader constant (not tunable)."""
    return field(default=value, metadata={"section": section, "doc": doc, "tunable": False})


@dataclass(frozen=True)
class Thresholds:
    """Every threshold the controller decides with. Field order follows DESIGN.md 5.11."""

    # ------------------------------------------------------------------ harm gate (0.1)
    alpha_h: float = T(0.025, "0.1, 5.4", "Harm gate tail for exploration, ramps and promotions: "
                       "P(de < -1%) <= alpha_h. Mirrors the grader's 1% engagement line with a "
                       "2.5% one-sided tail.")
    alpha_h_rollback: float = T(0.10, "0.1, 5.7", "Harm gate tail for rollbacks: a decaying "
                                "variant costs engagement every hour and the target is a known "
                                "baseline (cost asymmetry).")
    alpha_d: float = T(0.05, "0.1, 5.9", "Caller-ID harm gate tail: P(dd < -2%) <= alpha_d, the "
                       "grader's 2% DID-health line.")
    cohort_harm_line_default: float = T(0.02, "0.1, 5.4", "x_k, the per-cohort harm line "
                                        "P(de_k < -x_k) <= alpha_h. 2% by default; a client "
                                        "directive can tighten it (Northwind 1%, directives.yaml). "
                                        "At 2% the first exploration is not shrunk (3.8% per arm).")
    grader_engagement_line: float = C(0.01, "0.1", "Grader rule: a change is harmful if expected "
                                      "engagement falls more than 1% (grader.py:67-73).")
    grader_did_line: float = C(0.02, "0.1", "Grader rule: harmful if expected caller-ID health "
                               "falls more than 2%.")

    # ------------------------------------------------------------------ effect model (5.3, 5.4)
    tau_e: float = T(0.13, "5.3", "Prior sd of arm effects on the logit scale (about 0.06 on the "
                     "log-probability scale at e = 0.55): plausible lever sizes +3% to +15%.")
    tau_h_floor: float = T(0.06, "5.4", "Floor of the per-arm prior sd used by the harm gate; "
                           "tau_H = max(0.06, empirical-Bayes dispersion). 1.96 x 0.06 x "
                           "sqrt(sum s^2) <= 0.01 gives 3.8% per arm for 5 arms.")
    nuisance_prior_sd: float = T(2.0, "5.3", "Prior sd of cell, segment and prompt intercepts "
                                 "(beta, gamma, pi ~ N(0, 2^2)): weakly informative.")
    ew_half_life_segments: float = T(2.0, "5.3", "EW posterior segment weight 2^(-age/2): a "
                                     "half-life of 2 half-window segments, 12 dialling hours on a "
                                     "12-hour window; about a 1.7-day effective window.")
    n_draws: int = T(4000, "5.3, 5.4", "Posterior draws per fit; Monte Carlo error of a 2.5% "
                     "tail is about 0.25 points.")
    engagement_model_primary_kpi: str = C("engagement_rate", "5.3", "Welcome and voice "
                                          "experiments run only where objective.primary is this KPI.")

    # ------------------------------------------------------------------ allocation (5.4)
    arm_floor: float = T(0.02, "5.4", "Every live undecided arm keeps at least 2% of its cohort, "
                         "so it keeps learning.")
    arm_ceiling: float = T(0.50, "5.4", "An arm not yet declared winner gets at most 50% of its "
                           "cohort: bounds exposure before the winner rule.")
    bisection_iters: int = T(12, "5.4", "Bisection iterations for lambda_k: resolution 1/4096.")
    min_exploit_gain: float = T(0.0025, "5.4", "Skip an exploitation move whose expected gain on "
                                "the lever's KPI is below 0.25% of that KPI's daily numerator in "
                                "the affected cohorts (slot economy). Exploration and "
                                "risk-reducing moves are exempt.")
    harm_stop_p: float = T(0.95, "5.4", "Harm stop: EW P(theta < 0) >= 0.95 ...")
    harm_stop_loss: float = T(0.005, "5.4", "... and expected loss share x |E theta| >= 0.5% of "
                              "scope engagement: a P1 risk-reducing move to the control.")
    arm_removal_p: float = T(0.80, "5.4", "Arm removal at planning points when P(theta < 0) >= "
                             "0.8 (a P3 risk-reducing patch).")
    futility_p: float = T(0.05, "5.4, 5.5", "Futility drop at a look when P(theta > ln 1.02) <= "
                          "5%: the arm is very unlikely to be even 2% better.")
    futility_margin: float = T(math.log(1.02), "5.4", "The ln 1.02 margin of the futility rule.")

    # ------------------------------------------------------------------ winners (5.5)
    w1_population: float = T(0.995, "5.5", "W1 at population scope: stationary P(theta > 0) >= "
                             "0.995. Union bound over 4 doubling looks under unequal sizes: "
                             "0.54% per null hypothesis.")
    w1_cohort: float = T(0.998, "5.5", "W1 at cohort scope (prior sd 0.12): union bound 0.47%.")
    w2_p_best: float = T(0.95, "5.5", "W2: stationary P(a is best in scope) >= 0.95, one winner "
                         "per dimension and scope.")
    w3_ew_p_pos: float = T(0.90, "5.5", "W3 recency: EW P(theta > 0) >= 0.90 ...")
    w3_ew_ratio: float = T(0.5, "5.5", "... and E_EW[theta] >= 0.5 x E_stat[theta]: a "
                           "peaked-then-decaying arm fails (F1).")
    w4_cusum_frac: float = T(0.5, "5.5", "W4: the arm's decay CUSUM statistic is below h/2.")
    look_first_n: int = T(200, "5.5", "First look when the arm has >= 200 connected calls in "
                          "scope ...")
    look_first_days: int = T(2, "5.5", "... over at least 2 calendar days.")
    look_growth: float = T(2.0, "5.5", "Later looks once the in-scope count has doubled since the "
                           "previous look (200, 400, 800, 1,600).")
    look_max: int = T(0, "5.5", "Cap on winner looks per hypothesis (arm, scope); 0 = no cap. 4 "
                      "bounds W1 alone by the 0.54% union bound when volume pushes first looks "
                      "past 200 calls (T3 measured 0.73% uncapped; the full W1-W6 rule stays at "
                      "0 to 0.07%).")

    # ------------------------------------------------------------------ cohorts (5.2)
    targeting_cohort_min_share: float = T(0.05, "5.2", "A targeting cohort holds >= 5% of daily "
                                          "leads: about 200 history dials per hour cell, matching "
                                          "the hour-curve prior strength.")
    winner_scope_min_share: float = T(0.15, "5.2, 5.5", "A cohort can host a cohort-scope winner "
                                      "only with >= 15% of daily connected calls ...")
    winner_scope_projected_n: int = T(800, "5.2, 5.5", "... and a projected n = 800 look before "
                                      "the freeze (real ramp table, 5.5).")
    winner_scope_ramp: tuple[float, ...] = T(
        (0.038, 0.12, 0.25, 0.30, 0.40, 0.50), "5.2, 5.5", "Arm share per day along the real "
        "ramp of 5.5 (3.8% on D0, 10-15% on D1, 20-30% on D2 and D3, then up to the 50% "
        "ceiling), used to project when an arm reaches the n = 800 look in a cohort; later days "
        "keep the last value. Derived from the harm gate's ramp, not an independent knob: if the "
        "gate or arm_ceiling changes, re-derive it.")
    grid_f1_p: float = T(0.001, "5.2", "F1 = the feature whose hour curves differ most: deviance "
                         "test of connected ~ hour * f vs hour + f at p < 0.001.")
    grid_f2_lang_min_langs: int = T(2, "5.2", "F2 = lang if the workflow has 2 to 5 languages ...")
    grid_f2_lang_max_langs: int = T(5, "5.2", "... (upper bound of the language count) ...")
    grid_f2_lang_min_share: float = T(0.10, "5.2", "... each with >= 10% of leads.")
    max_cohorts: int = C(20, "5.2", "Simulator contract: at most 20 cohorts (SIMULATOR.md 3); "
                         "not exposed by the API, a 422 above it.")

    # ------------------------------------------------------------------ holdout, decay (5.6, 5.7)
    holdout_grid: tuple[float, ...] = T((0.05, 0.10, 0.15, 0.20), "5.6", "Holdout sizes tried, "
                                        "smallest first, capped at 20%.")
    holdout_se_target: float = T(0.075, "5.6", "Smallest holdout whose one-day SE of "
                                 "log(winner / holdout) is <= 0.075: one-day decay visibility.")
    decay_cusum_k: float = T(0.5, "5.7", "Page CUSUM reference value k (in SE units).")
    decay_cusum_h: float = T(4.0, "5.7", "Decay alarm at h = 4: one-sided in-control ARL about "
                             "336 segments (168 dialling days; 168 is the two-sided value), "
                             "out-of-control ARL 8.4 segments at 1 SE.")
    decay_arm_share: float = T(0.10, "5.7", "Monitor every non-control arm with >= 10% share in "
                               "some scope (lowered from 15% per F1).")
    rollback_tolerance: float = T(0.10, "5.7", "Rollback when E[de_rollback(t_a)] > 0 and "
                                  "P(de_rollback(t_a) < -1%) <= 10%.")
    rollback_trend_segments: int = T(4, "5.7", "Local linear trend over the last 4 segments to "
                                     "predict the effect at the expected apply time.")
    apply_time_trend_p: float = T(0.90, "0.1, 5.7", "Ramps and promotions must also pass on draws "
                                  "projected to the expected apply time when the arm's "
                                  "4-segment trend is falling at this one-sided level (the "
                                  "grader judges a change at its apply time).")

    # ------------------------------------------------------------------ call time (5.8)
    hour_curve_prior: float = T(200.0, "5.8", "Hour curves are shrunk toward the pooled curve "
                                "with 200 dials of prior strength per (cell, hour).")
    hour_weight_kappa: float = T(2.0, "5.8", "Weight sharpness: w = clip((p/pbar)^kappa / mean).")
    hour_weight_min: float = T(0.25, "5.8", "Weight clip floor: every in-window hour gets >= 0.25 "
                               "(a missing hour gets 0 and wastes business-owned capacity).")
    hour_weight_max: float = T(4.0, "5.8", "Weight clip ceiling.")
    schedule_refresh_l1: float = T(0.1, "5.8", "Refresh a schedule only if the L1 distance between "
                                   "normalised weight tables moves by more than 0.1 (slot economy).")
    schedule_verify_min_dials: int = T(30, "5.8", "Schedule verification uses (cohort, hour) cells "
                                       "with >= 30 dials in the 2 segments after the change ...")
    schedule_verify_fail_frac: float = T(0.10, "5.8", "... and fails if more than 10% fall below "
                                         "their 95% predictive band.")
    schedule_no_history_days: int = T(3, "5.8, 9.7", "With history_days = 0 no table is proposed "
                                      "until 3 dialling days of our own dials exist.")

    # ------------------------------------------------------------------ caller ID (5.9, 7.3)
    did_prior_p_flagged: float = T(0.2, "5.9", "Prior P(an untested DID is flagged).")
    did_r_flag: float = T(0.35, "5.9", "Relative pickup of a flagged DID under the prior.")
    did_r_unknown_max: float = T(1.1, "6.6", "An unknown DID's relative pickup ranges over "
                                 "[r_flag, 1.1] in the dry run.")
    did_probe_gate_dd: float = T(0.018, "5.9", "A probe must be safe even if the DID is flagged: "
                                 "s x (1 - r_flag) <= 0.018 gives s_gate = 2.8% (inside the 2% "
                                 "grader line with margin).")
    did_probe_min: float = T(0.005, "5.9", "Probe share floor (volume rule lower bound).")
    did_probe_max: float = T(0.028, "5.9", "Probe share ceiling from the DID harm gate.")
    did_probe_dials_per_day: int = T(400, "5.9", "Probe s = max(0.5%, min(2.8%, 400 / "
                                     "dials_per_day)): 400 dials a day covers the r = 1.0 SPRT "
                                     "case (245 dials) inside one dialling day.")
    did_max_probes_per_change: int = T(2, "5.9", "Probe at most two unknown DIDs per change "
                                       "(P(at least two of three flagged) = 0.104 > 5%).")
    sprt_r_unhealthy: float = T(0.70, "5.9", "SPRT H_U: r = 0.70 ...")
    sprt_r_healthy: float = T(1.0, "5.9", "... against H_H: r = 1.0.")
    sprt_alpha: float = T(0.01, "5.9", "SPRT alpha = P(call healthy | r = 0.70).")
    sprt_beta: float = T(0.05, "5.9", "SPRT beta = P(call unhealthy | r = 1.0).")
    sprt_n_max: int = T(1000, "5.9", "Truncation at 1,000 dials: classify healthy iff r_hat >= the "
                        "indifference point.")
    sprt_indifference_r: float = T(0.85, "5.9", "Indifference point used at truncation and by the "
                                   "slow path: either answer is harmless there.")
    did_slow_windows_h: tuple[int, ...] = T((12, 24, 48), "5.9", "Slow path: degraded if the "
                                            "UCB95 of r_d is below 0.85 in any trailing window "
                                            "of 12, 24 or 48 dialling hours (covers 0.75 to 0.85).")
    did_slow_conf: float = T(0.95, "5.9", "Confidence of the slow-path upper bound.")
    did_readmit_days: int = T(3, "5.9", "Re-admission of a degraded DID is a probe after 3 days "
                              "(load decays with tau 2 days).")
    did_fast_windows_h: tuple[int, ...] = T((2, 4, 8), "7.3", "Fast detector windows (dialling "
                                            "hours, exact because r_did is keyed by tick).")
    did_fast_min_dials: int = T(100, "7.3", "Alarm needs >= 100 dials on the DID in the window ...")
    did_fast_z: float = T(-4.0, "7.3", "... z <= -4.0 (3.2e-5 per test) ...")
    did_fast_r: float = T(0.75, "7.3", "... and r_d <= 0.75 (excludes natural load imbalance, "
                          "about 0.90).")
    did_suspect_window_h: int = T(1, "7.3", "Suspect window: the trailing 1 dialling hour ...")
    did_suspect_z: float = T(-2.5, "7.3", "... z <= -2.5 ...")
    did_suspect_min_dials: int = T(50, "7.3", "... with >= 50 dials: tick-sized steps until alarm "
                                   "or clear, P2/P3 submits deferred.")
    drain_verify_ratio: float = T(0.9, "7.3", "Drain verified when workflow pickup is >= 0.9 of "
                                  "the pre-incident 24h at the same hours ...")
    drain_verify_after_h: int = T(2, "7.3", "... measured 2 dialling hours after the last step.")
    drain_wait_holder_max_min: int = T(60, "6.2", "A P0b drain waits for a P1-P3 holder whose "
                                       "worst-case remaining time is <= 60 min; else supersedes.")
    drain_chain_first_max_min: int = T(30, "6.2, 7.3", "A running drain chain finishes before a "
                                       "P0a prompt fix if (k_rem - 1) x tick <= 30 min; else one "
                                       "bundled P0 approval.")

    # ------------------------------------------------------------------ guardrail (5.10)
    guardrail_window_days: int = T(2, "5.10", "Guardrail drop measured over a rolling 2-day window "
                                   "(absolute drop of objective.guardrail.*_max_drop).")
    guardrail_baseline_days: int = T(3, "5.10", "Without a concurrent comparator: the trailing "
                                     "3-day baseline adjusted by the model.")
    guardrail_p_breach: float = T(0.025, "5.10", "A proposal must predict no guardrail drop "
                                  "(point estimate <= 0) and P(drop > x) <= 2.5%.")
    guardrail_breach_conf: float = T(0.975, "5.10", "Breach when the lower 97.5% bound of the raw "
                                     "drop exceeds x and the drop is attributable to our change.")
    guardrail_mix_attrib_frac: float = T(0.5, "5.10", "Schedule or caller-ID attribution: the mix "
                                         "term explains at least half of the drop ...")
    guardrail_mix_attrib_segments: int = T(2, "5.10", "... and the mix shift started within 2 "
                                           "segments of our change.")

    # ------------------------------------------------------------------ KPI drift (7.5)
    drift_k: float = T(0.5, "7.5", "Downward Page CUSUM per dialling-hour block, k = 0.5 ...")
    drift_h: float = T(6.0, "7.5", "... alarm at h = 6: ARL0 about 2,570 blocks (214 dialling "
                       "days); a drift alarm is a digest item, never a change.")
    drift_armed_dials: int = T(2000, "7.5", "Pickup drift armed after 2,000 baseline dials.")
    drift_armed_connects: int = T(1000, "7.5", "Engagement drift armed after 1,000 baseline "
                                  "connects.")
    drift_no_history_days: int = T(2, "7.5", "No history: the first 2 dialling days of our own "
                                   "data are the baseline.")

    # ------------------------------------------------------------------ prompt safety (7.1)
    verdict_stage1_n: int = T(300, "7.1", "Stage 1 of the safety verdict: n = 300 (3 credits).")
    verdict_stage1_unsafe_f: int = T(10, "7.1", "Stage 1 UNSAFE at f1 >= 10 (3.3% observed).")
    verdict_stage1_safe_f: int = T(3, "7.1", "Stage 1 SAFE at f1 <= 3 (SEQUENTIAL mode only).")
    verdict_stage2_n: int = T(1000, "7.1", "Stage 2: n = 1,000 more (10 credits), pooled N = 1,300.")
    verdict_pooled_unsafe_frac: float = T(0.02, "7.1", "Pooled UNSAFE at F >= ceil(0.02 N) = 26: "
                                          "a P1-like safe prompt fails about 0.3% of the time.")
    verdict_stage1_only_safe_f: int = T(9, "7.1, 7.2", "LOW credit mode (STAGE1_ONLY): SAFE iff "
                                        "f1 <= 9.")
    gate_q_ref: float = T(0.01, "7.1", "Failure rate of a clearly safe prompt, used to size the "
                          "designated gate-evidence run: P_pass(n) = P(Binom(n, q_ref) <= "
                          "floor((1 - g) n)).")
    gate_tight_p: float = T(0.95, "7.1", "The client gate is 'tight' iff P_pass(300) < 0.95; then "
                            "a separate evidence run is designated.")
    gate_n_grid: tuple[int, ...] = T((50, 100, 200, 300, 500, 1000), "7.1", "Candidate sizes of a "
                                     "designated evidence run (argmax of P_pass).")
    fix_verify_connected: int = T(300, "7.1", "Fix verified after 300 connected calls under it ...")
    fix_verify_flag_ratio: float = T(2.0, "7.1", "... with a flag-rate UCB95 below 2 p0 ...")
    fix_verify_engage_lcb: float = T(-0.05, "7.1", "... and 24h engagement vs the 72h before the "
                                     "incident (matched by hour) with LCB95(relative) >= -5%.")
    fix_verify_before_h: int = T(72, "7.1", "Pre-incident comparison window (hours).")
    fix_verify_after_h: int = T(24, "7.1", "Post-fix comparison window (hours).")
    lkg_min_live_days: float = T(1.0, "7.1", "Last known good: live >= 1 dialling day ...")
    lkg_flag_p: float = T(0.05, "7.1", "... with a live flag rate not above baseline (Poisson "
                          "test p > 0.05) and a SAFE verdict.")
    flag_cusum_ratio: float = T(2.0, "7.1", "Live flag CUSUM designed for a doubling: p1 = 2 p0 "
                                "(about 1,730 connected calls of delay at 2x, 460 at 3.6x).")
    flag_cusum_horizon_days: float = T(30.0, "7.1", "h = ln(30 days x connected per day): "
                                       "in-control ARL at least 30 days.")
    flag_cusum_min_flags: int = T(5, "7.1", "Alarm also needs at least 5 flags since the reset.")
    flag_live_open_conf: float = T(0.99, "7.1", "An incident opens on live evidence alone when the "
                                   "live flag rate's lower 99% bound exceeds 2 p0.")
    flag_check_ratio: float = T(1.6, "7.1", "Cross-workflow flag check at D0: r_w / r_min >= 1.6 "
                                "...")
    flag_check_z: float = T(4.0, "7.1", "... and Poisson z >= 4 escalates to a FULL verdict ...")
    flag_check_min_connected: int = T(2000, "7.1", "... among workflows with >= 2,000 history "
                                      "connected calls (F7).")
    flag_engage_drift_frac: float = T(0.5, "7.5", "Engagement drift checks the flag CUSUM; above "
                                      "h/2 a FULL verdict on the incumbent is paid from R.")
    llm_suggest_per_incident: int = T(1, "17.1", "suggest-fixes is called once per hallucination "
                                      "incident (3 credits from R; skipped in LOW mode); the "
                                      "fixed statistical rank still decides.")

    # ------------------------------------------------------------------ credits (7.2)
    credit_reserve_max_frac: float = T(0.8, "7.2", "R = min(0.8 C0, max(0.3 C0, 2 W)) ...")
    credit_reserve_min_frac: float = T(0.3, "7.2", "... the 0.3 C0 floor ...")
    credit_reserve_worst_mult: float = T(2.0, "7.2", "... and 2 worst incidents.")
    credit_screen_frac: float = T(0.25, "7.2", "S = min(0.25 C0, C0 - R) for D0 screens.")
    credit_full_verdict: int = T(13, "7.2", "W = 13 K (+ K if the gate is tight): one FULL verdict "
                                 "costs 3 + 10 credits.")
    cost_suggest_fixes: float = C(3.0, "SIMULATOR.md 5", "Contract: suggest-fixes costs 3 credits.")
    cost_validation_per_100: float = C(1.0, "SIMULATOR.md 5", "Contract: a validation run costs "
                                       "ceil(n / 100) credits, n in [50, 2000].")
    validation_n_min: int = C(50, "SIMULATOR.md 5", "Contract: minimum validation n.")
    validation_n_max: int = C(2000, "SIMULATOR.md 5", "Contract: maximum validation n.")

    # ------------------------------------------------------------------ governance (6.1 to 6.7)
    submit_expiry_p_max: float = T(0.01, "6.3", "Submit only when P(expire) <= 1% (exact, from the "
                                   "uniform latency).")
    slot_offset_max_min: int = T(60, "6.3", "`*` default routine slot: approver_open + min(60, "
                                 "25% of the approver window) ...")
    slot_offset_window_frac: float = T(0.25, "6.3", "... the 25% part.")
    slot2_offset_min: int = T(240, "6.3", "Slot 2 (routine budget 2) at slot1 + 240 if slot 1's "
                              "proposal is terminal by then.")
    cap1_spacing_min: int = T(2880, "6.4", "At C_eff = 1, a P2 or P3 request only if none of ours "
                              "in the last 48h (F2).")
    supervisor_cooldown_min: int = T(1440, "6.7", "24h cooldown on supervisor-owned paths for P2/P3.")
    late_decision_recheck_min: int = T(1200, "6.3", "An approval decided more than 20h after "
                                       "submit is re-gated on fresh data before apply.")
    held_patch_release_min: int = T(360, "6.1", "A held patch set is re-checked after 6 sim hours "
                                    "...")
    held_patch_max_holds: int = T(2, "6.1", "... and dropped ('needs discussion') after 2 holds.")
    alloc_verify_segments: int = T(2, "6.1", "Allocation changes verified over the next 2 "
                                   "segments ...")
    alloc_verify_se: float = T(3.0, "6.1", "... realised shares within 3 binomial SE ...")
    alloc_verify_p_harm: float = T(0.10, "6.1", "... and EW P(de < -1%) <= 10%.")
    suspect_defer_min: int = T(120, "6.2", "P2/P3 submits deferred up to 2 h while a DID is "
                               "suspect.")
    velocity_window_min: int = T(1440, "6.5", "Exposure-increasing auto-applies: cumulative TV per "
                                 "(cohort, dim) over a rolling 24h stays within max_shift.")
    max_shift_margin: float = T(1e-6, "6.5", "Local check max_shift(cur, new) <= max_shift - "
                                "1e-6 (the simulator allows + 1e-9).")
    schedule_revert_lookback_min: int = T(10080, "6.5", "A schedule change is risk-reducing iff it "
                                          "equals a table live in the last 7 days (a revert).")
    rounding: float = T(1e-9, "8.6", "Floats are rounded to 1e-9 before comparison with gates.")

    # ------------------------------------------------------------------ loop (4.1, 4.2)
    base_step_min: int = T(60, "4.2", "In-hours base step (a multiple of tick); inside the apply "
                           "bound S_watch_max - tick at the public apply window.")
    refit_every_dialling_h: int = T(4, "4.1", "Models refit at planning points or every 4 "
                                    "dialling hours; detectors update every step.")
    freeze_min_days: float = T(1.0, "4.2", "End-game freeze: no new arms or probes in the last "
                               "max(1 day, 15% of the scenario) ...")
    freeze_frac: float = T(0.15, "4.2", "... the 15% part.")
    ramp_stop_days: float = T(1.0, "4.2", "Ramps stop in the last 1 day.")

    # ------------------------------------------------------------------ ops (9.2)
    alert_ingest_stalled_s: int = T(300, "9.2", "INGEST_STALLED: a listed chunk not committed "
                                    "after 5 wall minutes.")
    alert_incident_no_fix_min: int = T(120, "9.2", "INCIDENT_UNRESOLVED: no fix submitted within 2 "
                                       "sim hours ...")
    alert_incident_unresolved_min: int = T(360, "9.2", "... or unresolved after 6.")
    alert_drain_wait_min: int = T(120, "9.2, 6.2", "A DID drain waiting more than 2 hours behind a "
                                  "P0a approval pages.")
    alert_crash_loop_restarts: int = T(3, "9.2", "CRASH_LOOP: more than 3 restarts ...")
    alert_crash_loop_window_s: int = T(600, "9.2", "... in 10 minutes.")
    alert_get_retry_page_s: int = T(300, "8.4", "GET retries page after 5 minutes (and keep "
                                    "retrying).")


def _env_str(env: Mapping[str, str], *names: str, default: str | None = None) -> str | None:
    for n in names:
        v = env.get(n)
        if v not in (None, ""):
            return v
    return default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    v = env.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_num(env: Mapping[str, str], name: str, default: float, cast: type = float) -> Any:
    v = env.get(name)
    return default if v in (None, "") else cast(v)


def _positive_or_none(env: Mapping[str, str], name: str) -> int | None:
    v = env.get(name)
    if v in (None, ""):
        return None
    n = int(v)
    return n if n > 0 else None


SyntheticMarker = tuple[str, str, int, int]
"""(kind, workflow_id, from_minute, to_minute); kind is pending | approved."""


def parse_synthetic_markers(raw: str | None) -> tuple[SyntheticMarker, ...]:
    """`CTL_SYNTHETIC_MARKERS=pending:<wf>:<from>:<to>,approved:<wf>:<from>:<apply_by>`.
    Raises ValueError on a malformed entry (a configuration error, exit 3)."""
    out: list[SyntheticMarker] = []
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        bits = part.strip().split(":")
        if len(bits) != 4:
            raise ValueError(f"CTL_SYNTHETIC_MARKERS entry {part!r} must be kind:wf:from:to")
        kind, wf, a, b = bits
        if kind not in ("pending", "approved"):
            raise ValueError(f"CTL_SYNTHETIC_MARKERS kind {kind!r}")
        out.append((kind, wf, int(a), int(b)))
    return tuple(out)


@dataclass(frozen=True)
class EnvSettings:
    """Where to connect and what to run (DESIGN.md 11.3, 8.4, 10.2)."""

    sim_url: str = "http://127.0.0.1:8700"
    """CTL_SIM_URL (run.sh) or SIM_URL."""
    pg_dsn: str | None = None
    """PG_DSN, used verbatim when set (no-Docker mode); otherwise built from CTL_PG_*."""
    pg_host: str = "127.0.0.1"
    pg_port: int = 55432
    """CTL_PG_PORT: run.sh picks the first free port in 55432 to 55451."""
    pg_user: str = "postgres"
    pg_password: str = "ctl"
    pg_db: str = "postgres"
    pg_pool_size: int = 3
    """Main thread, loader thread, heartbeat thread (DESIGN.md 2)."""
    api_port: int = 8780
    """CTL_API_PORT: the read-only supervisor and ops API (9.3)."""
    sim_wait_s: float = 900.0
    """CTL_SIM_WAIT: poll /health for up to this long (the simulator builds history first)."""
    health_poll_s: float = 2.0
    lock_poll_s: float = 5.0
    """A second controller polls the advisory lock every 5 s instead of acting (DESIGN.md 2)."""
    heartbeat_s: float = 10.0
    http_timeout_s: float = 30.0
    """Connect and read timeout of ordinary requests."""
    advance_timeout_min_s: float = 120.0
    """Read timeout of advance: max(120 s, 20 x measured wall s per sim hour x S/60) (8.4)."""
    get_retry_base_s: float = 0.5
    get_retry_max_s: float = 30.0
    """Exponential backoff 0.5 s to 30 s with jitter for idempotent GETs (8.4)."""
    get_retry_budget_s: float = 1800.0
    """CTL_GET_RETRY_BUDGET_S: give up a GET after this long and exit 1 (AUDIT-1 finding 15; the
    orchestrator pages SIM_UNREACHABLE at alert_get_retry_page_s). 0 = retry forever. A restart then
    waits CTL_SIM_WAIT for /health and exits 3, so a dead simulator ends run.sh in bounded time."""
    ingest: str = "sql"
    """CTL_INGEST: sql | python (the fallback parser, 3.1)."""
    llm_suggest: bool = True
    """CTL_LLM_SUGGEST: DESIGN.md 17.1 calls suggest-fixes once per hallucination incident;
    set 0 to skip it (it then changes no decision, 7.1)."""
    engagement_model: str = "beta_binomial"
    """CTL_ENGAGEMENT_MODEL: beta_binomial (ships first, 17.2) | ridge_logit."""
    learner: str = "probe"
    """CTL_LEARNER: which patch-emitting learners the loop runs. `probe` (DESIGN.md 15.1 M3):
    caller-ID probes and spreads (5.9) plus per-cohort hour weights from history (5.8); `none`:
    governance runs but nothing emits patches (the M2 no-op loop, used by the platform tests)."""
    fault: str | None = None
    """CTL_FAULT=<kind>:<n>:<phase> crash injection (10.2)."""
    max_restarts: int = 30
    """CTL_MAX_RESTARTS (run.sh restart loop): restarts with no progress in the store (ADR 0019).
    30 leaves room for every hook point to reach safe mode at one step."""
    safe_mode_crashes: int = 3
    """CTL_SAFE_MODE_CRASHES: exceptions in one hook at one step before it is guarded
    (loop/safemode.py). Not a decision threshold, so not part of settings_hash."""
    safe_mode_hard_crashes: int = 8
    """CTL_SAFE_MODE_HARD_CRASHES: process deaths inside one hook at one step (no exception) before
    it is disabled. Higher than safe_mode_crashes because a death may be someone's kill -9 and not
    the hook's fault: random kills must not switch a healthy hook off."""
    stage_watchdog_s: float = 1200.0
    """CTL_STAGE_WATCHDOG_S: a hook running longer than this is hung: the process exits so it can
    be judged by a restart. 0 disables. The wall budget of a whole sim day is 600 s (DESIGN.md 15)."""
    hook_fault: str | None = None
    """CTL_HOOK_FAULT=<point>:<from_minute>:<raise|kill|hang>[,...]: test-only failures of a hook
    point from a sim minute on, to exercise safe mode end to end (reliability/faults.py)."""
    stop_after_steps: int | None = None
    """CTL_STOP_AFTER_STEPS=n: exit 1 after n steps of this process (tests of resume)."""
    synthetic_markers: tuple[SyntheticMarker, ...] = ()
    """CTL_SYNTHETIC_MARKERS (test only): markers the step rule treats like a pending or
    approved proposal of the store (DESIGN.md 15.1 M2)."""
    prefetch_chunks: int = 1
    """CTL_PREFETCH_CHUNKS=k: chunks the loader thread stages ahead of the transform (0 = none
    ahead; staging still runs on the loader thread)."""
    no_docker: bool = False
    code_version: str = "dev"
    """CTL_CODE_VERSION (git sha baked into the image); stored on every decision row."""
    log_level: str = "INFO"

    DECISION_FIELDS = ("llm_suggest", "engagement_model", "learner")
    """Environment fields that change decisions, so they enter settings_hash."""

    @property
    def dsn(self) -> str:
        if self.pg_dsn:
            return self.pg_dsn
        return (f"host={self.pg_host} port={self.pg_port} user={self.pg_user} "
                f"password={self.pg_password} dbname={self.pg_db}")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EnvSettings":
        d = cls()
        model = _env_str(env, "CTL_ENGAGEMENT_MODEL", default=d.engagement_model)
        if model not in ("beta_binomial", "ridge_logit"):
            raise ValueError(f"CTL_ENGAGEMENT_MODEL must be beta_binomial or ridge_logit, not {model}")
        ingest = _env_str(env, "CTL_INGEST", default=d.ingest)
        if ingest not in ("sql", "python"):
            raise ValueError(f"CTL_INGEST must be sql or python, not {ingest}")
        learner = _env_str(env, "CTL_LEARNER", default=d.learner)
        if learner not in ("probe", "none"):
            raise ValueError(f"CTL_LEARNER must be probe or none, not {learner}")
        return cls(
            sim_url=_env_str(env, "CTL_SIM_URL", "SIM_URL", default=d.sim_url) or d.sim_url,
            pg_dsn=_env_str(env, "PG_DSN", "CTL_PG_DSN"),
            pg_host=_env_str(env, "CTL_PG_HOST", default=d.pg_host) or d.pg_host,
            pg_port=int(_env_num(env, "CTL_PG_PORT", d.pg_port, int)),
            pg_user=_env_str(env, "CTL_PG_USER", default=d.pg_user) or d.pg_user,
            pg_password=_env_str(env, "CTL_PG_PASSWORD", default=d.pg_password) or d.pg_password,
            pg_db=_env_str(env, "CTL_PG_DB", default=d.pg_db) or d.pg_db,
            pg_pool_size=int(_env_num(env, "CTL_PG_POOL_SIZE", d.pg_pool_size, int)),
            api_port=int(_env_num(env, "CTL_API_PORT", d.api_port, int)),
            sim_wait_s=float(_env_num(env, "CTL_SIM_WAIT", d.sim_wait_s)),
            health_poll_s=float(_env_num(env, "CTL_HEALTH_POLL_S", d.health_poll_s)),
            lock_poll_s=float(_env_num(env, "CTL_LOCK_POLL_S", d.lock_poll_s)),
            heartbeat_s=float(_env_num(env, "CTL_HEARTBEAT_S", d.heartbeat_s)),
            http_timeout_s=float(_env_num(env, "CTL_HTTP_TIMEOUT_S", d.http_timeout_s)),
            advance_timeout_min_s=float(_env_num(env, "CTL_ADVANCE_TIMEOUT_S",
                                                 d.advance_timeout_min_s)),
            get_retry_base_s=float(_env_num(env, "CTL_GET_RETRY_BASE_S", d.get_retry_base_s)),
            get_retry_max_s=float(_env_num(env, "CTL_GET_RETRY_MAX_S", d.get_retry_max_s)),
            get_retry_budget_s=float(_env_num(env, "CTL_GET_RETRY_BUDGET_S",
                                              d.get_retry_budget_s)),
            ingest=ingest or d.ingest,
            llm_suggest=_env_bool(env, "CTL_LLM_SUGGEST", d.llm_suggest),
            engagement_model=model or d.engagement_model,
            learner=learner or d.learner,
            fault=_env_str(env, "CTL_FAULT"),
            max_restarts=int(_env_num(env, "CTL_MAX_RESTARTS", d.max_restarts, int)),
            safe_mode_crashes=max(1, int(_env_num(env, "CTL_SAFE_MODE_CRASHES",
                                                  d.safe_mode_crashes, int))),
            safe_mode_hard_crashes=max(1, int(_env_num(env, "CTL_SAFE_MODE_HARD_CRASHES",
                                                       d.safe_mode_hard_crashes, int))),
            stage_watchdog_s=float(_env_num(env, "CTL_STAGE_WATCHDOG_S", d.stage_watchdog_s)),
            hook_fault=_env_str(env, "CTL_HOOK_FAULT"),
            stop_after_steps=_positive_or_none(env, "CTL_STOP_AFTER_STEPS"),
            synthetic_markers=parse_synthetic_markers(env.get("CTL_SYNTHETIC_MARKERS")),
            prefetch_chunks=max(0, int(_env_num(env, "CTL_PREFETCH_CHUNKS", d.prefetch_chunks,
                                                int))),
            no_docker=_env_bool(env, "CTL_NO_DOCKER", d.no_docker),
            code_version=_env_str(env, "CTL_CODE_VERSION", default=d.code_version) or "dev",
            log_level=_env_str(env, "CTL_LOG_LEVEL", default=d.log_level) or d.log_level,
        )


def _coerce(default: Any, raw: str) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        items = [s.strip() for s in raw.split(",") if s.strip()]
        kind = type(default[0]) if default else str
        return tuple(kind(s) for s in items)
    return raw


def thresholds_from_env(env: Mapping[str, str]) -> Thresholds:
    """Thresholds with `CTL_T_<NAME>` overrides applied (private-variant harness only)."""
    base = Thresholds()
    changes: dict[str, Any] = {}
    for f in dataclasses.fields(Thresholds):
        raw = env.get(f"CTL_T_{f.name.upper()}")
        if raw not in (None, ""):
            changes[f.name] = _coerce(getattr(base, f.name), raw)
    return dataclasses.replace(base, **changes) if changes else base


@dataclass(frozen=True)
class Settings:
    """Everything configurable. Frozen and hashable; `settings_hash()` identifies the decision-
    relevant part (all thresholds plus `EnvSettings.DECISION_FIELDS`)."""

    env: EnvSettings = field(default_factory=EnvSettings)
    t: Thresholds = field(default_factory=Thresholds)

    def decision_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {"thresholds": dataclasses.asdict(self.t)}
        view["env"] = {k: getattr(self.env, k) for k in EnvSettings.DECISION_FIELDS}
        return view

    def settings_hash(self) -> str:
        """sha256 (first 16 hex chars) of the canonical JSON of `decision_view()`."""
        blob = json.dumps(self.decision_view(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(env=EnvSettings.from_env(e), t=thresholds_from_env(e))


def settings_hash(s: Settings | None = None) -> str:
    return (s or load_settings()).settings_hash()


def threshold_register() -> list[dict[str, Any]]:
    """The register of 5.11 generated from the code: name, value, tunable, section, doc."""
    t = Thresholds()
    return [{"name": f.name, "value": getattr(t, f.name), "tunable": f.metadata["tunable"],
             "section": f.metadata["section"], "doc": f.metadata["doc"]}
            for f in dataclasses.fields(Thresholds)]
