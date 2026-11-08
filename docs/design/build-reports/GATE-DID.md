# GATE-DID: a caller-ID change applied with a failed DID line

Date 2026-11-08, branch `controller`, base f3b785e. Verdict: **real bug, fixed** (ADR 0035).

## The symptom
public_02, `4bdc1f-nw_renewal-D2T1100-3-0007` (class P3, auto, version 8): raise D4 2.8% to 24.3%
(21.6% in the rerun), approval package "Harm gate FAILED: P(de < -1%) = 0.0000, P(dd < -2%) = 0.0635
... failed ['did']", yet auto-applied. CLAUDE.md and ADR 0002: every change except a P0 prompt fix
passes the gate.

## Trace: where the DID line is evaluated and what each place does with a failure

| # | Where | Code | Posterior draws | On failure |
|---|---|---|---|---|
| 1 | Pickup learner's cut | `DidHealthModel.gate` (`learning/did.py`), called by `plan_caller_ids` and `_probe_count`, `loop/pickup.py` via `DECIDE` | `did_posterior(ws, classify(ws), gate_seed(ws))`, 4000 draws, per scope | **Trims** the step to the largest lam with P(dd < -2%) <= 0.05 (bisection). Cut to nothing: `no_action` decision + `HARM_GATE_BLOCK`. Drain (P0b, fast path): recorded, never blocks. |
| 2 | Composed gate at release | `ReleaseTrain._gated` -> `EngagementHooks.gate` (`loop/engagement.py`) -> `ep.run_gate` -> `GateContext._did` (`learning/harm.py`) | `did_post_for(ws, ws.did_state, seed_for(ws,"gate","did"))`, `n_draws`, lead-weighted over cohorts | Records the verdict (decision `0.1/harm_gate`, `harm_report.failed`). **Held the caller-ID patches only if the change raised an unproven DID** (unknown, probing, degraded, unhealthy, suspect; `_adds_unproven_did`, ADR 0031). Otherwise **recorded only**. P0: never held. |
| 3 | Late-decision / notice re-gate | `EngagementHooks.regate` -> `_lever_lines_pass` | as 2 | Same exemption as 2; failing abandons / holds the proposal. |
| 4 | Held-patch re-check | `release_ok` -> `_lever_lines_pass` | as 2 | Same exemption; 2 holds then drop. |
| 5 | Allocator / winners / decay (engagement planning) | `allocator.py:396`, `winners.py`, `decay.py` pass `did_post`, `bad = {population, guardrail, did, did_unmodeled}` | engagement plan only | Cuts or rejects the engagement plan (they never change caller ID). |
| 6 | Verification | `_verify_allocation` passes `did_post=None`; `caller_id` verifier is the 7.3 check | n/a | Not a DID-line check (post-apply outcome check). |

Which verdict the package shows: `enrich` (`loop/engagement.py`) hands the builder the **row 2** verdict
(the in-memory `_last_gate` of the very composition, else a fresh `_judge`), and `_rationale` prints
"Harm gate FAILED" from it. Which one decided release: row 1 trimmed the change, row 2 judged and then
let it through because D4 had been cleared by the SPRT (healthy), so the exemption in
`_to_hold` applied. The text was therefore accurate about the composed gate; the gate was not enforced.

## Why it happened (two causes)
1. **The release train did not enforce what it recorded** for changes that add only SPRT-cleared DIDs
   (ADR 0031: "enforcing it held almost every spread and cost about 2% pickup on public_01 with the I2
   learner, P(dd < -2%) about 0.48"). That reason is stale: since ADR 0030 the learner cuts to the line,
   and in the run below every other applied caller-ID change reads 0.000 to 0.005 in the composed gate.
2. **The learner cut on different draws than the ones that judge at release** (`gate_seed` vs
   `seed_for`). It maximises along a line, so on its own draws the result reads exactly 0.050; on other
   draws it reads high by selection (0.0622 here, 0.0635 in the committed run). The full step was 0.082.

No documented exemption covers the change: the only DID-line exemption is P0 incident work (drains,
ADR 0008/0025). A probe on a never-measured DID is not exempt (its size comes out of this same line,
ADR 0008 point 2); D4 had in any case been classified healthy at D1 (SPRT accept) before v8.

## Reproduction (before)
Real controller, public_02, `CTL_LEARNER=probe`, schema `run_4bdc1ff90f7d_1`:
```
export CTL_T1_SIM_PORT=8790 CTL_TEST_PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl"
.venv/bin/python scripts/m3_run.py scenarios/public_02_incidents.yaml --out runs/gate_did/before \
    --baseline runs/base_public_02_incidents --port 8790 --dsn "$CTL_TEST_PG_DSN dbname=postgres" --keep-db
```
v8 reproduced (same proposal id, FAILED, auto-applied). Table of every applied controller change touching
caller_id, before the fix (learner lam / full-step P is the learner's own gate; "composed" is the stored
`0.1/harm_gate` row of the patches):

| wf | ver | applied | class | route | learner lam / full P | composed failed lines | composed P(dd<-2%) | package |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | v2 | D0 13:00 | P3 | approval | 1.0 / 0.000 | none | 0.000 | passed |
| ks_cancel | v3 | D1 13:00 | P3 | approval | 1.0 / 0.000 | none | 0.000 | passed |
| ks_cancel | v4 | D2 11:30 | P0 | auto | 1.0 / 0.000 | none | 0.000 | passed (drain, exempt) |
| ks_cancel | v5 | D2 11:45 | P0 | auto | 1.0 / 0.000 | none | 0.000 | passed (drain, exempt) |
| ks_cancel | v12 | D7 12:00 | P3 | auto | 1.0 / 0.000 | none | 0.000 | passed |
| nw_kyc | v2 | D0 16:00 | P3 | approval | 1.0 / 0.000 | none | 0.000 | passed |
| nw_kyc | v3 | D1 17:00 | P3 | approval | 1.0 / 0.000 | none | 0.000 | passed |
| nw_kyc | v4 | D5 13:00 | P3 | approval | 1.0 / 0.000 | none | 0.000 | passed |
| nw_renewal | v2 | D0 11:00 | P3 | auto | 1.0 / 0.000 | none | 0.000 | passed |
| nw_renewal | v4 | D0 15:00 | P3 | auto | 1.0 / 0.000 | none | 0.000 | passed |
| nw_renewal | v5 | D1 11:00 | P3 | auto | 1.0 / 0.004 | none | 0.0047 | passed |
| **nw_renewal** | **v8** | D2 11:00 | P3 | auto | **0.774 / 0.082** | **did, apply_time:did** | **0.0622** | **FAILED** |
| nw_renewal | v16 | D6 11:00 | P3 | auto | 1.0 / 0.000 | none (a cohort line failed on a sibling composition and was held, then passed) | 0.000 | passed |

13 changes, 1 failed gate line applied (v8), no exempt-class failure. No applied controller change of
any dimension had a FAILED package other than v8. Rows 4-5 are P0 drains (exempt by design).

After the fix (same command, `--out runs/gate_did/after`): v8 is cut to lam 0.690 (full step 0.087),
composed P(dd < -2%) = 0.0500 on the same draws, passes, applied. 13 caller-ID changes applied, none
with a failed line, no FAILED package of any dimension. The table, after:

| wf | ver | applied | class | learner lam / full P | composed P(dd<-2%) | failed | package |
|---|---|---|---|---|---|---|---|
| ks_cancel | v2, v3 | D0 13:00, D1 13:00 | P3 | 1.0 / 0.000 | 0.000 | none | passed |
| ks_cancel | v4, v5 | D2 11:30, 11:45 | P0 | 1.0 / 0.000 | 0.000 | none | passed (drain) |
| ks_cancel | v10 | D4 13:00 | P3 | 1.0 / 0.000 | 0.000 | none | passed |
| nw_kyc | v2, v3 | D0 16:00, D1 17:00 | P3 | 1.0 / 0.000 | 0.000 | none | passed |
| nw_kyc | v4 | D5 12:30 | P3 | 1.0 / 0.0025 | 0.0025 | none | passed |
| nw_renewal | v2, v4 | D0 11:00, 15:00 | P3 | 1.0 / 0.000 | 0.000 | none | passed |
| nw_renewal | v5 | D1 11:00 | P3 | 1.0 / 0.0047 | 0.0047 | none | passed |
| nw_renewal | v8 | D2 11:00 | P3 | 0.690 / 0.087 | 0.0500 | none | passed |
| nw_renewal | v12 | D5 11:00 | P3 | 1.0 / 0.000 | 0.000 | none | passed |

## Fix (commits)
- `80bcf98` guard test for RollupCache writers (part 4, below).
- `c69572b` the end-to-end reproduction as a test, red on purpose (`tests/e2e/test_gate_did_live.py`,
  wrapper `tests/support/controller_generous_did_cut.py`): real controller + real simulator, the learner's cut
  overshoots by 1.5x so a spread among cleared DIDs reaches the release train with a failing DID line.
  It failed with `4bdc1f-nw_renewal-D2T1100-3-0007` applied as v8 with FAILED text, and no HARM_GATE_BLOCK.
- `07ef72c` the fix: `_to_hold` holds caller-ID patches on any `did`/`did_unmodeled` failure of a non-P0 change;
  `regate` and `release_ok` use `v.passes`; `_adds_unproven_did`, `_lever_lines_pass`, `UNPROVEN_DID` removed;
  `gate_seed` is `seed_for(ws,"gate","did")` and the learner's posterior uses `n_draws`. The same test passes.
  ADR `0035-did-line-enforced-on-every-change.md` supersedes the 0031 exemption.
No threshold changed.

## Grade comparison (public_02, vs `runs/base_public_02_incidents`)
Harmful changes: before 0, after 0 (the grader lists 35 applied before, 32 after, all neutral or beneficial).

| | pickup ALL | ks_cancel | nw_kyc | nw_renewal | engagement ALL | conclusion ALL |
|---|---|---|---|---|---|---|
| before | +22.8% | +38.6% | +16.9% | +11.0% | +0.5% | +26.1% |
| after | +22.1% | +39.2% | +14.5% | +10.2% | +1.7% | +27.1% |

Pickup lost 0.7 point overall. The two runs are different trajectories (any changed decision moves
everything after it), so only nw_kyc (-2.4) and nw_renewal (-0.8) are plausibly the stricter gate; ks_cancel
moved the other way. A single run per arm cannot separate that from noise.

## Part 4: RollupCache guard
`tests/unit/test_rollup_cache_writers.py` greps `controller/` and `scripts/` (.py, .sql, .sh) for INSERT INTO,
UPDATE, DELETE FROM, TRUNCATE and COPY on r_engage, r_pickup, r_did, r_flags, lead_dim outside
`controller/ingest/transform.sql` and `controller/store/migrations/`. On failure it names the file and the
statement and says RollupCache's validity depends on it. Self-checks of the pattern and of the allow-list are
included. Current tree: no violation (tests/ fixtures write these tables but are not scanned, by the brief).

## Final tests (HEAD 07ef72c plus this report)
Env: `CTL_T1_SIM_PORT=8790 CTL_TEST_PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl"`.
Without `CTL_TEST_PG_DSN` the first PG test runs `docker run`, which hangs here.

| suite | result |
|---|---|
| `tests/unit` | 543 passed (includes the new RollupCache guard) |
| `tests/e2e/test_gate_did_live.py` | 3 passed after the fix; failed before it (the reproduction) |
| `tests/audit` | 49 passed |
| `tests/e2e/platform/test_crash_equivalence.py` | 57 passed |
| `tests/e2e/test_m3_governance_live.py` | 53 passed, **1 failed**: `test_peak_requests_within_c_eff[public_01]` |
| `tests/e2e/test_m4_pickup_live.py` | 29 passed, 15 skipped, **1 failed**: `test_flag_day_share_within_the_7_3_bound_and_zero_after[d_max_shift_005]` |

- **m4 d_max_shift_005 is not caused by this change.** It fails the same way on 80bcf98 (the commit before the
  fix, checked in a separate worktree): the fast detector opened the incident 150 min after the flag, the test
  allows 2 h (`incident[0][1] - at <= T_ALARM_H * 60`, 3630 - 3480).
- **m3 public_01 peak is a trajectory effect, not a rule break.** The fix changes the learner's caller-ID draws,
  so later decisions differ. ks_cancel then had one routine P3 (D5 11:00, superseded) and two P1 decay
  rollbacks (D5 12:00, D5 15:00) inside 24h = 3. The workflow's own cap is 4 (`max_requests_per_24h`), a
  directive lowers c_eff to 2, and DESIGN 6.4 (`timing.fatigue_ok`) lets P0/P1 exceed a directive cap up to the
  policy cap. The test asserts `peak <= c_eff`, which is stricter than the design. On 80bcf98 the same test
  passes only because that trajectory has no second P1 in a day. The test and the design disagree; I changed
  neither. (m3 public_02 and the other m3 variants pass.)

## Decisions for the lead
1. ADR 0035 removes the ADR 0031 DID-line exemption entirely. If you prefer to keep a narrow exemption (for
   example risk-reducing P3 spreads per ADR 0025), it is one condition in `_to_hold`; I did not add it.
2. Multi-scope compositions (cohort overrides) are judged by the lead-weighted population health in the
   composed gate and per scope in the learner, so they can still disagree. The effect is a held patch that
   drops after 2 holds and is re-planned, never a harmful apply. Say if you want the learner to judge the
   composed form.
3. Pickup moved -0.7 points overall on one run; a multi-seed comparison (the p4_like family) would settle
   whether the stricter gate costs anything. Not run.
4. Hygiene: tests with `tests/conftest.py` start `docker run` unless `CTL_TEST_PG_DSN` is set; with Docker
   hanging, running `tests/unit` without it blocks forever on the first PG test.
