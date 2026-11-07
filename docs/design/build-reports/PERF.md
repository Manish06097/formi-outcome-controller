# PERF report: public_03 load, seconds per simulated day

Branch `controller`, tree /Users/recro/assignment/sde-assignment-2026, starting at ad9046c. This Mac (11 CPUs, 18 GB), private Postgres 17.6, no Docker. One thing ran at a time, so timings are not contended.

## Result
| | before (I6 report) | after |
|---|---|---|
| public_03 wall per sim day, mean | 200 s | **95 s** |
| worst day | 215 s | 102 s |
| `./run.sh` total (history plus 5 days) | 1111 s | 573 s |
| controller RSS peak | 325 MB | 363 MB |
| Postgres RSS sum (upper bound) | 4.4 GB | 5.0 GB |

The target (120 s) is met, so I stopped after one change. A box 3x slower would need about 285 s per day against the 600 s bar. Measured with `scripts/load_test.sh` on a fresh private Postgres with the compose settings (shared_buffers 1GB, fsync on), commit 0 below. Full tables in reports/public_03_load.md.

## Profile evidence (public_03, 2 run days, full controller)
Method: `cProfile` around `python -m controller` plus a wrapper timing every `psycopg` `execute` by normalised SQL text (the scripts lived in the session scratchpad, not in the repo; they are about 60 lines). `pg_stat_statements` is not loaded in this Postgres and enabling it needs a restart of a shared instance, so the per-statement timing is the wrapper's. Profiler overhead inflates absolute numbers; ratios are what matter.

Top costs before the change (profiled wall 563 s, of which decision stages 370 s):
1. `snapshot.load_snapshot`: 6,521 calls, **254 s** (69% of the decision stages). Every stage hook and every verifier builds its own `WorkflowSnapshot` per workflow, about 4.5 per workflow per step, and each re-reads the rollups.
2. Inside it, `SELECT ... FROM r_pickup WHERE wf` ordered: 91 s, 6,521 calls, about 32k rows each time, then converted by `numpy.asarray` (31 s over all calls).
3. Inside it, `SELECT cell, count(*) FROM lead_dim WHERE wf AND state = 0 GROUP BY cell`: 70 s (bitmap heap scan of about 8,000 heap blocks; the table is 1.7M rows with heavy update churn, so the partial index is not index-only).
4. Ingest transform SQL (`INSERT INTO ev` 45 s, `UPDATE lead_dim` 37 s, `CREATE TEMP TABLE calls` 36 s over 108 chunks including history): about 140 s, Postgres bound. Not touched.
5. Numerical learning code: `harm._dim_mult` 15 s, engagement `fit` 13 s, allocator gates 15 s. Not touched (behaviour sensitive, small).

The simulator wait (`advance`) is 14 s and the HTTP calls to it 25 s in the 2 days; not ours.

## Changes
0. **RollupCache memo in `load_snapshot`** (commit "Perf: memoise the rollup reads of load_snapshot, keyed by the store's ingested chunks", `controller/snapshot.py`, `controller/loop/governance.py`).
   - Why it is safe: `r_engage`, `r_pickup`, `r_did`, `r_flags` and `lead_dim` are written only by `ingest/transform.sql`, in the same transaction as the chunk's `ingested_chunk` row (checked with grep over controller/ for every writer). The memo key is `(current_schema(), count, max chunk_no, max until_minute, sum events)` of `ingested_chunk`, read from the store on every call. A new chunk, a different run schema, or a restart (the memo starts empty) means a re-read. Everything fed into a decision is therefore what the store returns now; the memo only skips repeating the same SELECT. It is not an in-memory state that a decision depends on and the store cannot rebuild. The arrays are handed out as copies, so no consumer can corrupt the memo.
   - What stays per call: the `cell` table, experiments, looks, decay, holdouts, config changes, did_state, `live` config and the cohort mapping of cells (`cohort_of_cells(live, ...)` depends on the live config, so it is not memoised).
   - Gain: public_03 2 days, unprofiled stage seconds (detect+decide+verify) down by about half in the profile (324 s to 140 s profiled); full load run 200 s to 95 s per day. Wall for public_01 155 s to 107 s, public_02 203 s to 133 s, 2-day public_03 539 s to 330 s.

Not done (levers named in I6, not needed once the target was met): skipping detection for workflows with no new data since the last stop (a behaviour question: DID states and CUSUMs advance on every tick, so skipping needs an argument per detector); sharing one snapshot between detect and decide (the memo gives most of the gain without sharing mutable per-step state).

## Identity proof (behaviour must not change)
Harness (scratchpad, not in the repo): for each scenario, a fresh database, the real simulator as a subprocess, the full controller (`CTL_LEARNER=probe`) via `tests/e2e/platform/helpers.run_controller`, `sim grade` output saved, then `helpers.dump_schema` of **every table of the run schema** (52 tables, wall-clock columns excluded by `WALL_COLUMNS`; this includes decision, proposal, patch, config_version, audit_log, experiment tables, rollups, call_fact) compared with `helpers.diff_dumps`.

| scenario | rows compared | before vs after |
|---|---|---|
| public_01 | 317,341 | identical, grade.md byte-identical |
| public_02 | 368,567 | identical, grade.md byte-identical (decisions 736, proposals 39, config versions 40) |
| public_03, 2 run days | 3,439,393 | identical (decisions 952, proposals 43, config versions 54). Grade output is empty for both because the 2-day run is graded against a 5-day baseline; the tables are the proof here |

Baseline reproducibility: two clean public_01 runs on the unchanged code were already identical (run-independent seeds, ADR 0033), so identical is a meaningful bar. For public_03 the "before" run used a git worktree of ad9046c, the "after" run the tree with the change.

## Tests
`tests/unit tests/audit tests/e2e/platform/test_crash_equivalence.py tests/e2e/test_m3_governance_live.py`: 699 passed in 1552 s.

## Decisions for the lead
- Accept the memo as "recomputable from the store". The argument is above; the weak point would be a future writer of the five tables outside the ingest transform. If one is ever added, it must also change a row in `ingested_chunk`, or the key must grow. A cheap guard: a test that greps for writers (not added).
- The perf harness (profile wrapper, identity runner) is not committed. It is useful as a regression guard for any later perf change; say if you want it under `scripts/`.
- The 4 vCPU / 8 GB box is still unmeasured. 95 s here is about 285 s at 3x. Postgres memory (5.0 GB RSS sum here, compose limit 3 GB) is the larger risk on 8 GB.

## What remains (floor and next levers)
Per day on this Mac: ingest 24 s (Postgres bound: `ev` insert, `lead_dim` update, `calls` temp table), simulator advance 8 s (not ours), detect 24 s, decide 18 s, govern 8 s, lifecycle 8 s, verify 4 s, other.
- The first snapshot of each workflow after each ingest still reads r_pickup (about 14 ms) and the active-lead counts (about 17 ms, heap-bound). Over a 2-day run that is 44 s profiled, about 10 s per day. Next: an incremental or per-day-partitioned read of r_pickup, an active-count maintained in the transform, or autovacuum settings on `lead_dim` so the index-only scan works.
- Numerical stages (harm `_dim_mult`, engagement fit, allocator gates): about 40 s over 2 days profiled; repeated fits of identical data could be memoised by a hash of the input arrays, but float ordering must stay exactly the same, so it needs its own identity run.
- The `lead_dim` table (600 MB, 1.7M rows, most of the database) drives both ingest and the active count; archiving concluded leads would help both.
