# Independent audit 2 of the merged controller at HEAD `a4f36f8` (read-only)

**Verdict: nothing disqualifying.** One floor-breaking gap (SUBMISSION.md is still a skeleton), a handful of outcome risks, and hygiene items. All of AUDIT-1 is closed in code; the items still open are listed in section 4.

**Scope.** I read the committed tree (`git archive` of `a4f36f8`; the later HEAD `591c463` only adds 3 lines to `tests/audit/fixture_world.py`, so the audit holds). I edited nothing apart from this file, ran no tests and did not start the simulator. I read `scenarios/public_02_incidents.yaml`, `sim/world.py` and `notes/UNDERSTANDING.md` only to check whether controller constants coincide with hidden truth.

Severity scale: disqualifying / constraint-breaking (includes the SUBMISSION floor) / outcome-risk / hygiene.

---

## 1. Findings, ranked

### F1. SUBMISSION.md is not finished (constraint-breaking: README section 6 floor says "SUBMISSION.md is complete")
- **Evidence:** `SUBMISSION.md:3` has `Name: TBD, Date: TBD, Hours: TBD`. 27 `[[RESULT: ...]]` placeholders remain (lines 24, 25, 26, 27, 86, 372, 400, 422, 442, 468, 569, 642, 657, 688, 689, 752, 753, 824, 841, 872, 878, 883 to 887, 919). It cites `reports/public_01.md`, `reports/public_02.md` and `reports/crash_recovery.md`, none of which exist at HEAD (only `reports/kill_test.md` and `reports/public_03_load.md` do). Several sentences are stale against the merged code, for example line 442 says the DID harm gate "was not yet wired" and line 86 asks to "confirm each status against the I3 to I6 reports".
- **Failure scenario:** a grader opening the submission sees a template with unfilled fields and no measured results, which fails the floor regardless of the code.
- **Fix:** run the final public_01 and public_02 end to end through `run.sh`, commit the grade reports under `reports/`, fill every placeholder from I3, I4, I5, R and the final runs, delete the "Predicted, to be replaced" section at line 657, and grep for `[[` and `TBD` before the last commit. Note the lead still owes the stakeholder replies and the "what I would change" text.
- **Owner:** lead (final docs step).

### F2. Incident drain steps go through the guardrail forecast with no P0 exemption (outcome-risk)
- **Evidence:** `controller/loop/governance.py:489` builds `reqs` from `self.pickup.drain_requests(...)` (class P0 incident) and then `governance.py:500` runs `self.engagement.guardrail_filter(tx, ws, reqs)` over the whole list. `controller/learning/engagement_plan.py:194-223` (`filter_by_guardrail`) has no class check, and `forecast_breaches` (`:226`) refuses any predicted drop above `guardrail_drop_tol = 0.001` (`controller/settings.py:265`) or P(breach) above 2.5%. The composed gate (`engagement.py:700`) says P0 work "is judged and recorded but never held", and the prompt fix is exempt (ADR 0031, 7.1). The drain is not.
- **Failure scenario:** a pickup-primary workflow with an engagement guardrail (the KYC shape): a DID is fast-path flagged, the drain moves share onto healthier DIDs, the connect model predicts a small engagement-mix drop above 0.1 points, the step is dropped with an `HARM_GATE_BLOCK` alert, and the flagged DID keeps carrying traffic until the forecast changes. The run public_02 passes (ks_cancel is engagement-primary), so this was never exercised on a pickup-primary flagged workflow.
- **Fix:** in `guardrail_filter` keep any request with `cls == P0_INCIDENT` (record the forecast in the decision, do not drop), and add a unit test with a pickup-primary workflow whose drain would breach.
- **Owner:** I4 (engagement) with I3 (drain).

### F3. Guardrail enforcement in the live loop is guarded by unit tests only (outcome-risk, AUDIT-1 #3 partially verified)
- **Where enforced:** `loop/governance.py:500` (filter before `overlay.add`), `loop/engagement.py:378-398` (records a `5.10/forecast` no_action decision and an alert), `loop/engagement.py:635-660` (post-hoc breach check and rollback), `learning/engagement_plan.py:173` (`forecast`), composed gate carries `v.guard` (`engagement.py:1026`), request text set via `BuildContext.guardrail` (`governance/builder.py:405`).
- **Tests:** `tests/unit/test_connects.py:115-138` (`filter_by_guardrail`), `tests/unit/test_guardrail.py`, and `tests/e2e/test_m5_engagement_live.py:243` which only asserts that the word "guardrail" appears in the dry run.
- **Gap:** no e2e test shows the live loop refusing a breaching schedule or rolling back a breached one; nothing asserts that a `5.10/forecast` or `5.10/breach_rollback` decision is ever written. Also, `guardrail_filter` is only called `if self.engagement is not None` (`governance.py:499`), so `CTL_LEARNER=probe_only` or `none` switches the guardrail off for pickup levers (default is `probe`, `settings.py:493`, so the shipped run is covered).
- **Fix:** add a live test with a synthetic pickup-primary world that makes a schedule change breach the engagement guardrail and asserts the refusal decision, or reduce `guardrail_max_drop` in a variant to force it. Consider making the guardrail filter independent of the engagement learner.
- **Owner:** I4.

### F4. Acceptance misses and open problems still on record after I5 (outcome-risk)
From the I4 and I5 reports, none fixed in the commits after them (`e2cce0d` to `a4f36f8` touch ADR 0003/0023, the approval package gate, the pre-open slot and prompt-fix verification):
- public_02 `nw_renewal` engagement gap is negative (-13% in the M5 run, -9% in the open problem), xfail in `test_m5_engagement_live.py` (I4 open 1).
- Decaying winner still holds 0.38 share at onset + 2 days against a bar of 10% (I4 open 2; I4 decision 3 recommends capping a new arm's share at the monitoring floor, not yet done).
- One run produced a harmful (-1.3%) `ks_cancel` voice change (I4 open 3; the gate allows a 2.5% tail per change over about 20 changes, so this is expected about half the time per scenario).
- A default-cohort-only change when every cohort overrides the dimension is still routed with `affected_leads` 0 (I4 open 4), which wastes an approval slot and approver attention.
- About 4 of 10 `nw_renewal` public_02 proposals end in `rollback` through the allocation verifier (I4 open 5, not investigated).
- Prompt fixes graded harmful in the P4-like worlds (I5 decision 1: ranked safe fix engages 3 to 5 percent worse than the supervisor's prompt). The README grader counts harmful changes, so on hidden scenarios of this shape the score takes a hit even though 7.1 exempts P0 prompt fixes.
- **Fix:** lead decisions needed; see each report. The cheap wins are I4 open 4 (drop zero-lead patches before routing) and I4 decision 3 option (b).
- **Owner:** I4 and lead.

### F5. Restart forgets the post-403 auto-apply block (outcome-risk, small)
- **Evidence:** `controller/governance/lifecycle.py:471` `self.auto_blocked: dict[str, str] = {}`, set at `:795` on a 403 from `/auto-apply`, consulted at `:539`, `:990`, cleared only by `policy_reread` (`:1037`, called when the policy changes, `loop/governance.py:348`). It is never persisted or rebuilt.
- **Failure scenario:** the simulator refuses an auto-apply (policy and replica disagree, or the supervisor changed policy between reads). The controller records `forbidden`, blocks the workflow, and then crashes and restarts. The new process has an empty `auto_blocked` and tries another auto-apply on the same workflow, getting a second 403 and a second `AUTO_APPLY_REJECTED` alert. The grader counts rejected auto-applies, and no double apply happens, so it is a score and noise issue, not a safety one.
- **Fix:** derive the block from the store at attach: workflows whose latest proposal transition is `forbidden` after the last `policy_changed` audit row.
- **Owner:** I2 governance (lifecycle).

### F6. No end-to-end demonstration of crash recovery on the final code; full suite never run on the merged HEAD (outcome-risk for the grader's "show it")
- **Evidence:** `reports/kill_test.md` was produced on the I1 code (public_01, no learner); R open problems and I4 report both state that a final full-suite run was not completed; `SUBMISSION.md:752-753` placeholder asks for the 20-SIGKILL run on public_02 with all learners. The crash matrix (`tests/e2e/platform/test_crash_equivalence.py:132, 177, 236`: 12 probe cells, 47 full-learner cells, 10 money cells) is strong, but it injects faults, it does not kill at random with all hooks on.
- **Failure scenario:** a regression in the last four commits (they changed the request builder, slot choice and VERIFYING handling, all on the crash-sensitive path) would not be seen.
- **Fix:** run `scripts/kill_test.sh` on public_02 at HEAD and the full suite once, commit `reports/crash_recovery.md`.
- **Owner:** lead (run), R (script).

### F7. Run-time defaults close to simulator truth (hygiene, provenance)
- `controller/settings.py:182` `did_r_flag = 0.35`, `:181` `did_prior_p_flagged = 0.2`; `notes/UNDERSTANDING.md:188` states the public_02 flagged DID health is "about 0.30" and `sim/world.py:860` samples the hallucination detector with 0.8/0.01. `settings.py:301` `gate_q_ref = 0.01` equals that 1% false-positive rate, and `verdict_pooled_unsafe_frac = 0.02`, `verdict_stage1_unsafe_f = 10` sit right on it.
- These are labelled `[T]` priors and thresholds with a derivation, and SUBMISSION says they were tuned on the private variants. I found no read of a hidden field, so this is not a disqualifier, but a grader reading the settings will ask where 0.35 and 0.01 came from.
- **Fix:** in ADR 0009/0030 or DESIGN 5.9/7.1 state the provenance (for example "documented noise of an imperfect detector, SIMULATOR.md section 5; prior chosen wide") and show the sensitivity run on the private variants. If either number was read off the public scenario, move it to a wider prior.
- **Owner:** lead (decision record).

### F8. `late_decision_recheck_min` and similar fixed minute constants are not derived from the API policy (hygiene)
- `settings.py:359` `late_decision_recheck_min = 1200` ("decided more than 20h after submit"), `:356` `cap1_spacing_min = 2880`, `:357` `supervisor_cooldown_min = 1440`, `:354` `slot2_offset_min = 240`, `:379` `base_step_min = 60`. All are design thresholds with an ADR reference and none is a public-scenario id, but the 20h value only makes sense for a TTL of at least 24 hours, so on the public TTL it never fires and on a private TTL variant it fires at a point unrelated to the TTL.
- `base_step_min` is correctly capped by the apply bound (`loop/stepper.py:6-8`, exercised by the `apply_window_1h` variant), so no action there.
- **Fix:** express the recheck as a fraction of `proposal_ttl_hours`, or write in the ADR why a fixed 20h is right.
- **Owner:** I2 governance.

### F9. Hygiene list
- **Fallbacks that look like public values:** `controller/obs/alerts.py:368` falls back to tick `15` when the clock row is missing; `controller/types.py:1044` defaults `dialling_window` to `(0, 24)` in `EffectivePolicy`. Both are only reachable before the first API read, but a literal 15 is exactly the public tick. Raise instead of defaulting.
- **Directives keyed to tenant names:** `controller/directives.yaml:44, 52` name `northwind_life` and `kestrel_finance`. R added `DIRECTIVE_UNMATCHED` (alert code `obs/alerts.py:100`, raised at `loop/orchestrator.py:314-325`, test `tests/e2e/platform/test_world_edges.py:132`), so a rename is loud, not silent. The memo promises would still be unapplied; mention in SUBMISSION assumption 4/5.
- **`_gate_seen` duplicate decision rows:** `controller/loop/engagement.py:109, 711-713` suppresses the `0.1/harm_gate` decision row for a repeated identical change in one process and rewrites it after a restart (and `attach` clears it, `:116`). Only the audit log differs between a clean and a crashed run (the equivalence harness already masks decision ids, `tests/e2e/platform/helpers.py:48-61`). No decision input depends on it. Same class as the `_last_gate` bug, but it feeds no persisted decision, so I rate it hygiene.
- **DID step cache keyed with `id()`:** `controller/learning/did.py:326` uses `id(ws.did)` in the cache key. Python reuses ids after garbage collection, so a stale entry could in principle be returned within the same minute and live version. Replace by a content hash or drop the cache.
- **Unknown lead features ignored:** AUDIT-1 14b is still open. `controller/cohorts/cells.py:48` indexes `features[f]` for the four fixed names and any other key is dropped silently; a missing key raises `KeyError` and loops through safe mode. Log extra keys once.
- **Production image carries test hooks:** `CTL_FAULT`, `CTL_HOOK_FAULT`, `CTL_STOP_AFTER_STEPS`, `CTL_SYNTHETIC_MARKERS` (`controller/settings.py`) are read in the runtime. They default off and need an env var, so they cannot fire in the grader's run; document them in `docs/RUNNING.md` as test-only.
- **Credit costs are constants:** `settings.py:343-346` (3 credits per suggest-fixes, 1 per 100 validation dials, n in [50, 2000]) are marked `C` (contract, SIMULATOR.md section 5). If a hidden scenario changed them, the ledger reconciliation (`reliability/intents.py:357-376`) would flag the mismatch rather than overspend, but the budget plan would be off. Acceptable; note it.
- **Default Postgres passwords** `ctl` / `ctl_api` in `compose.yml:9, 75`. Bound to 127.0.0.1 only. Fine for a grader box, state it.
- **En dashes** (not banned, listed for completeness) in `docs/design/build-reports/T3.md` and `I2.md`.
- **Limp mode** (`controller/loop/limp.py`, `run.sh:80-100`) advances the simulator with no governance, so approvals pending at that moment lapse. It is a deliberate last resort when the controller cannot run at all (R decision 3). Keep, but SUBMISSION should say so, since constraint 8 says "lose a pending approval".

---

## 2. Disqualifier checks (area a): all clean

- **No `sim` import.** `git grep` over `controller/`, `run.sh`, `Dockerfile`, `compose*.yml` finds no `import sim` or `from sim`. The only `importlib` is `controller/audit/replay.py:40,129,135`, which imports `controller.*` packages only. `Dockerfile` + `.dockerignore` (`*` then `!controller/`, `!requirements-runtime.lock`, `!vendor/wheels/`) put only `controller/` in the image; `tests/e2e/platform/test_packaging.py:66` asserts no `!sim` line and the lock-only install.
- **No reads of `runs/`, chunk files or scenarios.** Grep for `runs/`, `scenarios`, `.yaml`, `open(`, `Path(`, `glob`, `listdir`, `os.walk`, `subprocess`, `Popen`, `os.system` over `controller/`: the only file reads are the controller's own `ingest/transform.sql` (`ingest/pipeline.py:33`), `store/migrations/*.sql` (`store/db.py:39,63`), `directives.yaml` (`world/directives.py:21,224`) and `/proc/self/statm` (`obs/metrics.py:48`). The `runs/i4/smoke1` hit at `loop/engagement.py:766` is a comment. No `subprocess` anywhere in `controller/`. Chunks arrive through `GET /events/chunks/{n}` (`simclient/client.py:160`) straight into Postgres COPY.
- **No volume mounts of data.** `compose.yml:33` mounts only the `pgdata` volume; `run.sh:245` mounts only `${PROJECT}_plain_pgdata`. No bind mount of `runs/`, `scenarios/` or the repo. The controller container gets only `CTL_SIM_URL` and Postgres settings (`compose.yml:56-60`).
- **`run.sh` and limp mode** talk to the simulator by HTTP only (`controller/loop/limp.py:39-41`, `urllib`); `run.sh` reads no scenario file (grep of `scenario|runs|.yaml` shows comments and log text).
- **No hidden-truth field names.** `base_pickup`, `base_engage`, `did_base_health`, `engage_mult`, `archetype` do not appear in `controller/` (the `base_engagement` hits in `governance/builder.py` are the controller's own variable).
- **No public ids or option names in code.** No `nw_*`, `ks_*` workflow ids and no `W1/V1/D1/P1`-style option literals in `controller/**/*.py` or `.sql`. Tenant names appear only in `directives.yaml` and in comments/docstrings.
- **Never modifies `sim/`:** `git diff` of `sim/` against the initial commit was not needed; `sim/` is outside the audited tree and no controller path writes to it.

---

## 3. Hard rules and constraints: where enforced, what guards it (areas b, e)

| Rule | Enforced at | Guarded by | Status |
|---|---|---|---|
| HTTP only | section 2 above | `test_packaging.py:66` (image has no `sim`); no static test greps `controller/` for `import sim`/`runs/` | clean, but add a grep test (F9 note below) |
| C1 / governance boundary absolute | every mutation is in `reliability/intents.py:295,312,324,380,384`; callers `governance/lifecycle.py:673 (submit_approval), 730 (auto_apply), 961 (apply)`, `loop/orchestrator.py:622 (advance)`, `loop/incidents_hooks.py:107,130`. Routing `governance/release.py:378 (_route)`; local policy replica `governance/policy.py:118,294`; notice re-check `lifecycle.py:~990`. `one_in_flight` partial unique index `store/migrations/0006_governance.sql:73`. No other `client.apply/auto_apply/request_approval/validate/suggest_fixes` call exists (grep) | `tests/e2e/test_m3_governance_live.py:144` (0 rejected), `:162` (every approval applied in time), `test_policy_differential.py:229` (replica agrees with simulator) | clean |
| Supervisor edits never silently overwritten | `planner/overlay.py:435 (rebase_on_supervisor_edit)`, `governance/lifecycle.py:1026 (mark_stale)`, `lifecycle.py:118 (pre_apply_event refuses base_version != current)`, 24 h cooldown `settings.py:358` | `tests/unit/test_gov_overlay.py:166`, `tests/e2e/test_governance_flow.py:159`, `test_m3_governance_live.py:219`. I5 relaxed `:219` because a prompt-safety fix may replace a supervisor's unsafe prompt; that goes through approval with evidence, so it is not silent | clean |
| C2 business owns when/whether to call | no code writes `dialling_window`, `dials_per_hour`, `cadence`, retry (grep: only reads at `types.py:373-409`, `snapshot.py:116`, `connects.py:68`); hour weights clipped to [0.25, 4] (`settings.py:168,170`, `learning/schedule.py:56`) so no in-window hour is silenced | `test_m4_pickup_live.py:146 (every in-window hour has positive weight)` | clean |
| C3 pickup and engagement separate | welcome/voice experiments only where `objective.primary == engagement_rate` (`learning/allocator.py:179,272`, `engagement_plan.py:94`, `loop/engagement.py:128,364`); guardrail on the other KPI (F3) | `test_guardrail.py:32`, `ADR 0007` | clean with F2/F3. Pickup levers (caller ID, schedule) do run on engagement-primary workflows; DESIGN 15.x/1575 justifies it by conclusion rate and the guardrail forecast covers it. Defend it explicitly in SUBMISSION |
| C4 deterministic winners; LLM proposes only | `incidents/manager.py:306` is the only `suggest_fixes` call; suggestions only enter `rank_candidates` (`incidents/safety.py:333-368`), which sorts by a fixed key, and every candidate still needs a validation verdict (`manager.py:348-364`). Winners come from `learning/winners.py` and sequential looks | `tests/unit/test_winners.py`, `tests/unit/test_safety.py:66`, `test_false_winner_rate.py`; RNG seeded from the run schema (`stats/draws.py:32`), no unseeded RNG in decisions (client jitter only, `simclient/client.py:52`) | clean |
| C5 populations, not anecdotes | all detectors have count floors: `did_fast_min_dials 100`, `did_suspect_min_dials 50`, `did_slow_min_dials 100` (`settings.py:233,247,220`), `flag_cusum_min_flags 5`, `flag_check_min_connected 2000` (`:321,327`), `verdict_stage1_n 300`, drift armed at 2000 dials (`:281`) | `tests/unit/test_detectors.py`, `test_did.py`, `test_incident_manager.py:66,99` | clean. No test named for "a single call changes nothing"; the unit tests cover it implicitly |
| C6 every decision auditable before/atomically | `audit/writer.py:32 write_decision`; DECIDE writes the decision row and `overlay.add` in one transaction (`loop/governance.py:503-508`, `incidents_hooks.py:443`, `engagement.py:195`, `governance.py:643`); side effects use T1 (decision + intent + transition) before the HTTP call (`reliability/intents.py:127-140`); holds are audited (migration `0031_hold_audit.sql`, `tests/audit/test_hold_audit.py`) | `test_crash_equivalence.py`, `tests/audit/test_replay.py`, `test_m5_engagement_live.py:170,199` (every engagement change has a gate decision, replays exactly) | clean |
| C7 credits finite, reserve for incidents | `incidents/credits.py:36-65` (pool plan R/S/U), `:152 authorize`, `:183 authorize_incident`, LOW mode `:134`; every spending call carries `credits_before` and `cost` and is reconciled by balance (`intents.py:357-376`); envelopes persisted (`incidents_hooks.py:226-240`) and rebuilt on start (`:164-184`) | `tests/unit/test_credits.py:56,68`, `test_intent_contract.py:30,36`, `test_m6_prompt_live.py:128 (books balance, no 402), :185 (<= 40 credits), :242 (credits-50 LOW mode)`, money cells in the crash matrix | clean |
| C8 crash safety | write-ahead intents with per-kind reconcile (`reliability/intents.py`), `loop_stage` markers, run schema by fingerprint under advisory lock (`store/db.py:154`), learned state in Postgres, safe mode (`loop/safemode.py`) and restart budget in `run.sh` | `tests/e2e/platform/test_crash_equivalence.py:132,177,236`, `test_safe_mode.py`, `reports/kill_test.md` (old code) | clean in design; see F5 (auto_blocked) and F6 (no final-code demo) |
| Read parameters from API | tick, approver hours, latency, TTL, apply window, caps, auto-apply dims, `max_shift`, dialling window, options all parsed from `/workflows` (`types.py:314-409`) and re-read every step with audit (`loop/governance.py:332-348`) | `test_world_edges.py`, `tests/variants/test_variants.py` (56 private variants incl. `apply_window_1h`, `approvers_14_16_ttl24`) | clean; see F7/F8/F9 for constants |

---

## 4. AUDIT-1 follow-up: status of all 15 findings at HEAD

| # | Status | Evidence |
|---|---|---|
| 1 drains only at slot minutes | closed | `loop/governance.py:479-488` (`_did_turned_bad`, draining keeps the stop active); `test_m4_pickup_live.py:199` |
| 2 no harm gate/regate/verifier | closed | `loop/governance.py:113-114` (`regate`, `verifiers`, `on_rollback`); caller-ID gate in `learning/did.py`/`probe.py`; engagement gate `engagement.py:700`; `test_m5_engagement_live.py:216,229` |
| 3 guardrail enforcement | closed in code, see F2, F3 | `governance.py:500`, `engagement.py:378`, `engagement_plan.py:194` |
| 4 incidents/credits not wired, bypass intent log | closed | `loop/incidents_hooks.py` (six call sites), `IntentValidationClient`/`IntentSuggestClient`, migration `0020`, `test_m6_prompt_live.py`, money cells |
| 5 run.sh network/pins | closed (R) | digest-pinned `Dockerfile`, `requirements-runtime.lock` with hashes, `test_packaging.py:40-90` |
| 6 no compose v2 | closed (R) | three backends in `run.sh`, `test_run_sh.py` |
| 7 directive tenant keys | closed in effect (alert) | `DIRECTIVE_UNMATCHED`; names still in yaml (F9) |
| 8 slow-path min dials | closed | `settings.py:220` `did_slow_min_dials = 100` |
| 9 held patches released blindly | closed | `0.1/recheck_held` decisions (I4), `test_m5_engagement_live.py:229` |
| 10 restart cap / crash loop | closed (R) | `loop/safemode.py`, budget reset, `test_safe_mode.py` |
| 11 run.sh kill leaves container | closed (R) | trap + orphan replacement, `test_run_sh.py` |
| 12 fingerprint includes governance | closed (R) | `world/fingerprint.py`, test fails with old formula (R report); side effect: seeds changed, public_01 pickup +7.5% became +5.6% (R decision 1), treat as seed variance but say so |
| 13 workflow appears mid-run | closed | `loop/governance.py:336-340`, `test_workflow_appears_midrun.py:90` |
| 14 small hard-codings | partly closed | schedule look-back now `t.schedule_revert_lookback_min`; unknown feature keys still silent (F9) |
| 15 GET retry forever / containers stay up / holds not audited | closed | `get_retry_budget_s = 1800` (`settings.py:482`); run.sh stops Postgres; holds audited (`0031`) |

---

## 5. Crash-safety review (area d): per-process state

I listed every instance attribute, module global and cache in `controller/loop`, `learning`, `incidents`, `governance`, `planner`, `world`, `cohorts`, `ingest`, `reliability`, `api`, `audit` that is a dict, set or list created empty, and traced whether a persisted decision reads it.

**Recomputable from store/API (clean):**
- `EngagementLoop._last_gate` (`engagement.py:110`): now used only when the proposed and live hashes match (`:1018-1023`), otherwise the gate is rerun. The bug just fixed. No other reader.
- `PromptSafety._written_verdicts`, `flag_state` (`incidents_hooks.py:156-157`) restored from `prompt_verdict`, `validation_run`, `flag_monitor` (`:213-251`). Credit envelopes and the designated gate-evidence run are rebuilt the same way.
- `ProbeLearner._grids/_did/_curves` (`learning/probe.py:55-57`): built from history, which never changes; `p_hist` is a history constant.
- `IncidentManagerImpl.did_overrides` (`incidents/manager.py:89,173`): reset before every `detect`/`drain_requests` call and persisted in the same transaction (`loop/pickup.py:98-115, 164-170`, `_flush`).
- `OverlayStore` patches/owners/cooldowns (`governance/store_pg.py:312-340`), proposals (`PgProposalStore`; its id sequence is `count(*)+1`, `:232`, not the in-memory `_seq` of the test store at `lifecycle.py:306`), `business` cohorts (`loop/governance.py:98`, from config version 1), `HookGuard` states (`hook_guard` table).
- Per-fit caches `EffectCache`, `ConnectModel._memo`, `ConfigMirror` live for one snapshot or are rebuilt from the API on start; `_mult_memo`/`_coh_memo` in `learning/harm.py:79-80` belong to an object constructed per evaluation.
- `release._chain_refused` (`governance/release.py:228,345-364,403`): reset and refilled inside the same `compose` call that writes it into the rationale, so it cannot cross a restart.
- Metrics-only state (`orchestrator._adv_wall_per_sim_h`, `_sim_unreachable_paged`) feeds no decision.

**Not recomputable (findings):** `lifecycle.auto_blocked` (F5). Hygiene only: `_gate_seen` and the `id()`-keyed DID cache (F9).

**Write-ahead:** the only external mutations are the six in `reliability/intents.py`; the apply, auto-apply and submit paths use the intent executor at `lifecycle.py:673, 730, 961`; advance at `orchestrator.py:622`; validation and suggest-fixes at `incidents_hooks.py:107, 130` with `credits_before`/`cost` and refunds on replay (`tests/unit/test_credits.py`, `fb38a5f`). I found no decision, apply or credit path that bypasses it.

---

## 6. Docs hygiene (area f)

- **Em dashes in our docs and comments:** none. `grep -P '\x{2014}'` over the tree (excluding `sim/`, `scenarios/`, `stakeholders/`) hits only `README.md` (3), `docs/SIMULATOR.md` (9), `SUBMISSION_TEMPLATE.md` (1), all the assignment's own files from the initial commit, and `scripts/i5_matrix.py:39`, where the dash is inside a regex string that matches the grader's output text (needed, not a comment). `CLAUDE.md`, `SUBMISSION.md`, `docs/decisions/*`, `docs/design/*`, `RUNBOOK.md`, `RUNNING.md` and every `controller/` file are clean.
- **Placeholders:** `TODO`, `XXX`, `FIXME`, `lorem` do not occur outside the template. `TBD` and `[[RESULT: ...]]` occur in `SUBMISSION.md` only (F1). `docs/RUNBOOK.md`, `docs/RUNNING.md`, `docs/design/DESIGN.md` and all ADRs have none.
- **Broken references in SUBMISSION.md:** `reports/public_01.md`, `reports/public_02.md`, `reports/crash_recovery.md` (F1). The working tree has an untracked `reports/public_01_dev.md`; commit or delete it.

---

## 7. Suggested guard tests (cheap, high value)

1. A static test that walks `controller/` and fails on `import sim`/`from sim`, the strings `runs/` or `scenarios/` outside comments, any `subprocess` import, and any `open(` outside an allow-list (`transform.sql`, `migrations`, `directives.yaml`, `/proc/self/statm`). Today the rule is held only by the image contents and by review.
2. A test that greps `controller/**/*.py` (outside comments and `directives.yaml`) for the public workflow ids and option ids, so a future edit cannot add one.
3. The live guardrail refusal test (F3) and the P0 drain exemption test (F2).

## 8. What I did not check
- I did not run the simulator, the tests, Docker or `run.sh`; every statement about test outcomes comes from the build reports.
- I did not re-derive the statistics (gate calibration, SPRT, CUSUM) beyond reading their thresholds.
- Linux-box behaviour of `run.sh` (R's open "not exercised" list: bare x86_64, real Linux Docker daemon, docker-compose v1, offline wheels) is unchanged since R and remains unverified.
