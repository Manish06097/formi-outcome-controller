# ENG-GAP: why nw_renewal ends below the no-op baseline in the p02 family

Worktree branch from `controller` a4f36f8. Postgres 55479, simulator 8750. Every number below is
the simulator's own grader (`engagement_gap_captured`, DESIGN 5.x); "pre" is a4f36f8, "post" is the
commits below. Runs are deterministic: the same code gave the same gap to three digits twice.

## Verdict
Not the supervisor edit, not a rebase bug, not the holdout. Two things in the controller, one of them
fixed here (the gate), and one property of the world plus a design gap that this report cannot fix
without a design change (a decaying arm is ramped before it can be detected).

## Reproduction (public_02, real controller, served simulator)
`runs/p02` pre: nw_renewal gap **-17%** (I4 saw -14%), engagement -1.0% vs baseline, ks_cancel +39%,
0 harmful. The baseline itself captures **+8%** on nw_renewal (the supervisor's V2 default is good for
`ta`), so the controller gave back about 25 points against doing nothing.

Exact commands (from the worktree; `runs/t/egrun.sh` is the wrapper used for every run, it makes a
database per run, serves the scenario on 8750, runs `python -m controller`, then grades):
```
.venv/bin/python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base_p02
.venv/bin/python -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/run_p02 --port 8750 --force &
CTL_SIM_URL=http://127.0.0.1:8750 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=eg_p02" \
  CTL_LEARNER=probe CTL_SIM_WAIT=60 .venv/bin/python -m controller
.venv/bin/python -m sim grade --run runs/run_p02 --baseline runs/base_p02
```

## Hypotheses, each tested with a controlled run (nw_renewal gap captured)
| run | what changed | nw_renewal | reading |
|---|---|---|---|
| p02 pre | nothing | -17% | reproduction |
| engagement learner off (`CTL_LEARNER=probe_only`), supervisor edit on | learner | **+5%** | the edit alone is harmless; the loss is the engagement learner |
| supervisor edit removed from the scenario, learner on | edit | **-11%** | not the edit (the loss starts on D3.5, the edit is on D5) |
| W3 decay removed from the scenario, learner on | truth | **+21%** (engagement +4.3%) | the decaying welcome arm is the driver |

Per-day comparison with the baseline (`runs/t/perday.py`, `runs/t/arms.py`): the excess over the
default is +20 expected engaged calls on D0 to D3, then about -21 a day from D4 to D9 (total -110 vs
the baseline's +60). Observed arm shares from the call events: W3 went from 5% to **30% of nw_renewal
traffic by D3** and stayed there; V4 (x0.9 everywhere) held 17% to the end. The arms' observed
engagement shows the decay: W3 0.61 on D2, 0.49 to 0.52 from D4 (W1 0.55). The grader's oracle also
drops on D3 (0.68 to 0.60) because the decaying W3 stops being the best option, so the gap
denominator shrinks to about a quarter while our loss stays: -2% engagement is -60% of the gap.

## Cause 1 (fixed): plans sit on the gate's boundary and the release gate holds the whole lever
The decay rollback planner (`decay._gate_limited`) and the allocator (`allocator.gate`) bisect for the
largest step their own gate allows, so the plan has a cohort tail of exactly alpha. The release gate
(`0.1/harm_gate`) then re-evaluates the same change with a few draws of Monte Carlo noise and fails one
to five of the nine cohorts by 0.0005 to 0.014 (`de_k` tails 0.1005, 0.1038, 0.1138 against alpha
0.1). A failing engagement line holds **every** welcome and voice patch for 360 sim minutes
(`engagement._to_hold`, `release._gated`), the planner replaces them next tick and the cycle repeats.
Evidence, public_02, nw_renewal, decision log:
- 55 of 66 class-1 (rollback) composed changes failed the gate; 337 decay rollback patches ended
  `replaced`, 2 `applied`. CUSUM alarm for W3 at minute 9540; first applied cut at 11160 and it only
  reached the default cohort (W3 is overridden to 20% to 40% in every other cohort).
- 61 HARM_GATE_BLOCK digests in the run.
- 7 of 17 nw_renewal proposals ended in `rollback` through the verifier, and their revert
  patches were held by the same gate (decision 258: 18 patches held).
Fix: `GateContext.evaluate(..., planning=True)` judges the tails against `alpha - 2 Monte Carlo SE`
(`harm.tail_margin`, 0.0095 at alpha 0.1 and 4,000 draws); the allocator and the decay planner use it,
the release gate itself is unchanged. No threshold moved.
Second half of the same cause, found when the e2e test still failed on the decaying-winner scenario
(12 of 28 decay-rollback gates failed with the margin alone): rollbacks are planned in the urgent
stage with fit purpose `urgent`, but `_judge` always refit with purpose `plan`, so the release gate
drew independent numbers (seed = run, workflow, minute, purpose, kind). Tails of a plan at the edge
of its gate then scatter by +-0.01, which a 0.0095 margin does not cover. `_judge` now uses the
`urgent` seed for class 1 and below (replays use the stored seeds, so they are unaffected).
decaying_winner: 12 of 28 failed, now 5 of 19 (the rest are later re-gates of held patches at another
minute). On public_02 this change is a no-op (same trajectory, -5.1%), the margin was enough there.
After: class-1 gate failures 1 of 9; HARM_GATE_BLOCK 13; W3 share peaks at 16% and is cut to 3%;
nw_renewal gap -17% to **-5%**.

## Cause 2 (fixed, no measurable effect on the outcome): a voice-only supervisor edit interrupted the welcome experiment
The simulator re-normalises stored allocations, so an untouched cohort comes back one ulp away
(0.6253 vs 0.6253000000000001) and the event's `changed_dims` for the D5 edit listed
`welcome_message` and `voice`. `_interrupt_on_supervisor_edit` read that list, interrupted the welcome
experiment, and `changed_paths` made the cohort path supervisor-owned for 24h. Fix:
`world.configs.noise_free_dims` and a tolerant `changed_paths` (1e-9) in the mirror; the version row
now keeps what really changed. Result on public_02: only the voice experiment is interrupted; the gap
is unchanged (-5.1% both ways), so this is correctness, not the gap.

## Cause 3 (not fixed, needs a design decision): a decaying arm is ramped before it can be seen to decay
W3 starts at +18% and falls to x0.9 after two exposure days. The allocator ramps it on the pooled
posterior while it looks good (5% on D0, 10% on D2.6, 30% in cohorts by D3). The monitor only starts
at 10% share, the CUSUM alarm fired at D6.6 (pre) or D6.9 (post), about 4 days after onset against the
1.5 to 2 days of 5.7's sanity check, because the first segments after the monitor starts are still
inside the reference. With the gate fixed the rollback follows the alarm within hours (D7.5), but the
loss is already taken: in `seed_p02_s2202` post the cumulative excess is +18 on D0 to D3, -57 on D4 to D6,
and recovers to -56 by the end. Exploring W2, V3 (true x1.0) costs nothing and V4 (x0.9) costs about
10% x 10%; none of the true winners (V2 on `ta` = 20% of leads) can pay that back in 10 days at 1,000
connected calls a day. No controller can do better than the exploration-cost floor, but ours is above
it because the ramp outruns the detection.

## Results, pre and post (gap captured, nw_renewal; 0 harmful changes in every run)
| scenario | pre | post (commit 85e15da) |
|---|---|---|
| public_02 | -17% (I4: -14%) | -5.1% |
| seed_p02_s2201 | -16.4% | -10.7% |
| seed_p02_s2202 | -6.3% | -8.2% |
| effects_flip_resize_p02 | +15.7% | +16.0% |
| effects_flipped_p01 | -8.9% | -2.2% |
Mean of the four negative ones: -12.2% to -6.7%. s2202 got worse by 1.9 points with the same
mechanism and a different trajectory (the changed gate changes which proposals land; there is no
repeat-run noise to average out). ks_cancel stays 37% to 41% on the p02 family. Harmful changes: 0 of
about 30 per run in every post run.

## Tests run
`CTL_T1_SIM_PORT=8750 CTL_TEST_PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl" .venv/bin/python -m pytest -q tests/e2e/test_m5_engagement_live.py tests/e2e/platform/test_crash_equivalence.py`
gives **104 passed, 7 skipped, 2 xfailed** (27 min; the two xfails are the known public_02 nw_renewal
gap and the decaying share at onset + 2 days, both open for the reason in Cause 3). Unit tests for the
touched modules (harm gate, allocator, decay, decaying winner, replay, determinism, winners, the new
supervisor-edit test) pass.

## Commits
- 79b3831 Plan against alpha minus two Monte Carlo errors so a boundary plan does not fail the release gate
- 85e15da Judge a rollback with the draws its planner used
- 458b572 and 3b42107 Keep only the dims a supervisor edit really changed
- tests: `tests/unit/test_harm_gate.py::test_a_planning_gate_leaves_two_monte_carlo_errors_below_the_tail`,
  `tests/unit/test_supervisor_edit_noise.py`, and the e2e
  `test_a_rollback_the_planner_passed_is_not_held_by_the_release_gate` in
  `tests/e2e/test_m5_engagement_live.py` (decay-rollback gate failures at most 35% on every scenario that has them).

## What remains
nw_renewal in the p02 family is still negative (public_02 -5%, s2201 -10%, s2202 -9%). The test's
public_02 xfail stays, with a pointer here.

## Decisions for the lead
1. **Cap an unproven arm's share until the decay monitor has seen it** (design change to 5.4/5.7;
   I4 Decision 3 option b, now with evidence it is the same problem). Keep every non-control arm at or
   below the monitoring floor (10%) until it has two clean CUSUM segments, then allow the ramp. Cost: a
   genuine winner is exploited about 1.5 days later; 5.5 already needs 2 calendar days and 200 calls
   before a look. Benefit: the W3-type loss in this report (the D4 to D6 dip) is not taken. Recommendation: do it. No threshold changes.
2. Start the monitor at the arm's first dial instead of 10% share (the reference is then the
   stationary mean and the first segments count). More data per arm early at the same cost; it changes
   5.7's armed condition. Alternative to 1, weaker: it detects earlier but the arm is still at 30%.
3. Hold granularity: an engagement line failure holds every welcome and voice patch of the composed
   change. A per-cohort hold (only the failing cohort's patches) would stop one noisy cohort from
   delaying eight others for six sim hours. Not done: with the planning margin the case is rare
   (1 of 9 rollback gates), and it changes `release._gated`.
4. Accept the residual: the p02 family keeps a small negative nw_renewal gap, ks_cancel and public_01
   stay strongly positive. If the hidden scenarios resemble p02 the cost is about 2% of nw_renewal
   engagement.

## Housekeeping
Run directories under `runs/` (gitignored) and the per-run databases `eg_*` on 55479 are deleted at
the end.

# Second round (lead decisions on the above)

Merged `controller` (I6: seeds from the fingerprint only), so every number below is on the final seed
behaviour and differs from the first-round table. Same code, `CTL_T_DECAY_CLEAN_SEGMENTS` 0 against 2.

## Option 1 (arm cap): built, measured, NOT enabled
ADR `docs/decisions/0034-unproven-arm-share-cap.md` (0033 was taken by I6). `allocator._bounds` holds
an experiment arm at the 10% monitoring floor until its decay monitor has folded
`decay_clean_segments` segments without an alarm (`decay_monitor.seg_first`, migration 0040). The
guard failed: on the seven stable p01 scenarios the cap costs **7.3 points of ks_cancel gap and 6.2 of
nw_renewal** on average (bar: about 2), and on the p02 family it gains 0.8 points of nw_renewal. So
`decay_clean_segments` defaults to 0 (off); the code, the setting and the unit test stay. Table (gap
captured in points, off / on; 0 harmful changes in all 22 runs):

| scenario | ks_cancel | nw_renewal |
|---|---|---|
| public_01 | +3.4 / +1.3 | +9.9 / +5.1 |
| seed_p01_s1101 | +10.4 / +2.9 | +10.7 / +5.0 |
| seed_p01_s1102 | +9.9 / +0.8 | +13.2 / +4.7 |
| effects_large_p01 | +16.1 / +2.3 | +14.1 / +5.3 |
| effects_small_p01 | +5.6 / +0.8 | +3.1 / +3.0 |
| decaying_winner_dev_p01 | +8.1 / +3.8 | +11.3 / +3.7 |
| decaying_winner_highvol_p01 | +10.8 / +1.2 | +13.1 / +5.2 |
| public_02 | +41.3 / +34.3 | -7.0 / -6.2 |
| effects_flip_resize_p02 | +42.2 / +42.2 | +7.2 / +2.4 |
| seed_p02_s2201 | +39.5 / +34.1 | -10.8 / -3.8 |
| seed_p02_s2202 | +40.4 / +34.8 | -5.4 / -5.2 |

With the cap off (the default) the ENG-GAP fixes give public_02 -7.0%, s2201 -10.8%, s2202 -5.4% on
the new seeds (still negative: Cause 3 stands) and 0 harmful everywhere.
With the cap on, the unit test `test_no_ramp_or_promotion_of_a_decaying_winner_is_harmful` failed on
one replicate (a W2 ramp to 50% at day 6 graded -1.5%): less exposure means less data on the decay, so
the arm is ramped later on staler beliefs.

## AUDIT-2 items
- **F2** (`engagement_plan.filter_by_guardrail`): P0 incident requests are kept even when the forecast
  breaches; the breach is recorded in the `5.10/forecast` decision (`exempt_p0`). Unit test
  `test_an_incident_class_change_is_never_refused_by_the_forecast`. (The loop hunk of this commit
  shares a commit with the arm cap because both touch `loop/engagement.py`.)
- **F3** `tests/e2e/test_guardrail_live.py`: runs the real controller on `seed_p01_s1101`, which
  breaches naturally (a caller-ID spread on nw_renewal forecasts a 0.18 point pickup drop against the
  0.1 point tolerance) and asserts the refusal decision `5.10/forecast`, that the dropped path is not
  in `kept`, and the matching HARM_GATE_BLOCK alert. No fixture was needed.
- **F5** `lifecycle.restore_auto_blocks` + `PgProposalStore.auto_block_history`, called in `attach`:
  a 403 newer than the workflow's last `policy_changed` audit row blocks again after a restart.
  `tests/unit/test_auto_block_restart.py` (reproduced first: a fresh lifecycle has an empty block;
  the SQL reader is tested against a real schema). A live 403 cannot be provoked in a world, so the
  crash-equivalence matrix cannot show it; this is the honest limit.
- **F9** `_gate_seen` removed; the `0.1/harm_gate` decision is written when the change's verdict flips,
  read from the store through a `gate_key` in the decision inputs, so a restart neither repeats nor
  loses the row.

## Commits (second round)
3007855 F5; 13f402c arm cap + F2 (+ migration 0040); then F9 and the F3 test; the merge of
`controller`; the `decay_clean_segments = 0` switch; the default-off commit; this report and ADR 0034.

## Tests run at the end
One run of `test_m5_engagement_live.py`, `test_crash_equivalence.py`, `test_m3_governance_live.py`,
`test_guardrail_live.py` and `tests/unit`: 677 passed, 9 skipped, 2 xfailed, **20 failed** (48 min).
- 19 failures are crash-equivalence governance and money cells (`probe_only` and `probe`
  submit_approval, apply, auto_apply; `validation:1` and `suggest_fixes:1`). They fail identically on
  the untouched `controller` tip (same 19 ids rerun there, all 19 fail): the crashed run's
  `allocation_provenance` rows carry a different proposal id from the clean run's (I6 report open
  item 6, identity values after a restart). Not caused by this work; the lead should look at it,
  because the crash matrix no longer passes on `controller`.
- 1 failure was ours: `test_supervisor_edits_rebased_never_reverted[public_02]` read the simulator's
  `changed_dims` of the D5 edit, which lists `caller_id` through the same one-ulp noise, and our later
  DID change touched it. The test now treats caller_id like the other sanctioned dims; the whole
  `test_m3_governance_live.py` then passes (54 passed).
- m5 live tests, `test_guardrail_live.py` and all unit tests pass (the decaying-winner unit test
  needed the cap off, which is the default).
