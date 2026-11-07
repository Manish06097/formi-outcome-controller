# public_03 load test

All numbers **measured on this Mac on 2026-11-08** with the **full controller** (`CTL_LEARNER=probe`: detection, learning, governance, incident hooks, audit writes), **after the perf step** (docs/design/build-reports/PERF.md). Mac15,6, 11 CPUs, 18 GB RAM, **no Docker** (the daemon hangs): `CTL_NO_DOCKER=1 ./run.sh` against a private Postgres 17.6 started with the compose settings (shared_buffers 1GB, work_mem 64MB, max_wal_size 4GB, synchronous_commit on, fsync on). The grading box (4 vCPU / 8 GB Linux) is slower and has no such headroom; see "What this means for the grading box" below. Produced by `scripts/load_test.sh` (`CTL_NO_DOCKER=1 PG_DSN=... CTL_PG_PORT=55489 CTL_LOAD_SIM_PORT=8784 PGDATA_DIR=/tmp/perf_pgdata scripts/load_test.sh scenarios/public_03_load.yaml`).

**Verdict: mean 95 s per simulated day, worst 102 s (before the perf step: mean 200 s, worst 215 s). That meets the 120 s target and the 600 s hard bar on this Mac; a box 3x slower would take about 285 s per day, 6x slower about 570 s.**

Scenario `scenarios/public_03_load.yaml`: 5 run days, 9,737,763 events in 169 chunks, run schema `run_0c308db58dc0_1`.

## Correctness

- Ingested events 9,737,763 in 169 chunks; `/sim/state` last_event_id 9,737,763, chunks 169: **equal**.
- Rollups vs a from-scratch recomputation over `call_fact` (post-D0 rows that differ): r_engage 0, r_pickup 0, r_did 0, r_flags 0.
- Config versions above the business default: 94.
- Behaviour identity of the perf change: PERF.md (full store dump and grade output identical before and after on public_01, public_02 and a 2-day public_03).

## Wall time

- Simulator start, including generating the history: 31 s (simulator cost, not ours).
- `./run.sh` total (history ingest, all run days, final checks; no image build): 573 s (was 1111 s).
- History ingest at startup: 5,537,531 events, 23 chunks, 2.35 GB NDJSON in 99 s wall (56,005 events/s end to end).

| Sim day | Steps | Events | Wall s | Events/s (end to end) | Advance s | Ingest s | Controller RSS MB | Database GB (all schemas) |
|---|---|---|---|---|---|---|---|---|
| D0 | 84 | 840,059 | 100.6 | 8,353 | 6.5 | 25.3 | 252 | 0.58 |
| D1 | 35 | 840,054 | 101.7 | 8,262 | 7.9 | 24.8 | 325 | 0.70 |
| D2 | 26 | 840,044 | 97.3 | 8,633 | 8.0 | 23.2 | 353 | 0.81 |
| D3 | 22 | 840,046 | 88.2 | 9,522 | 8.0 | 22.5 | 363 | 0.95 |
| D4 | 19 | 840,029 | 86.4 | 9,719 | 7.9 | 22.8 | 363 | 1.10 |

Mean wall time per sim day **95 s**, worst 102 s (target under 120 s, SRE bar 600 s). `Advance` is the simulator computing the day (single-threaded; we wait for it), `Ingest` is streaming, COPY and the set-based transform; the other stages are in the next table. Pure ingest runs at about 35k events/s (840k events in 23 to 25 s).

## Memory and storage

| Process | Peak memory MB | Limit |
|---|---|---|
| controller process (loop_step.rss_mb, /proc) | 363 | 1 GB target |
| Postgres, sum of RSS of all its processes | 5032 | not capped here (compose caps it at 3 GB) |
| simulator process (ps RSS) | 723 | host |

This run's schema at the end: 1.09 GB including indexes. Largest tables: `lead_dim` 600 MB, `call_fact_d000` to `_d004` 84 MB each, `r_pickup` 45 MB, `posterior_snapshot` 16 MB.

The Postgres figure is an upper bound: RSS counts the 1 GB of shared buffers once per backend that touched it. It is above the 3 GB compose limit, so the true resident set under the limit is unmeasured; if the container limit binds, Postgres would shrink its cache, not die, but ingest and the decision queries would slow. Treat Postgres memory as the open risk on an 8 GB box. The perf step adds a small memo to the controller (the rollup arrays of the 12 workflows for the current ingest state, a few MB); controller RSS peak is 363 MB against 325 MB before.

## Where the time goes (full controller, from `loop_step.phase_ms`, seconds per simulated day)

| Phase | D0 | D1 | D2 | D3 | D4 | 5-day total | Before (5-day) |
|---|---|---|---|---|---|---|---|
| ingest | 25.3 | 24.8 | 23.2 | 22.5 | 22.8 | 119 | 129 |
| detect (drift, caller-ID, flag detectors, snapshots) | 33.2 | 27.4 | 21.6 | 19.2 | 17.1 | 118 | 316 |
| decide (fits, harm gates, allocator, winners, decay) | 19.1 | 23.5 | 11.9 | 13.9 | 21.5 | 90 | 199 |
| govern (release train, gates) | 9.7 | 7.5 | 8.3 | 9.2 | 7.1 | 42 | 76 |
| lifecycle (proposal state machine, applies) | 1.8 | 3.5 | 18.3 | 11.1 | 6.4 | 41 | 82 |
| advance (simulator) | 6.5 | 7.9 | 8.0 | 8.0 | 7.9 | 38 | 40 |
| verify (post-apply verification) | 2.7 | 6.0 | 4.9 | 3.3 | 2.7 | 20 | 151 |
| plan, refresh | 0.9 | 0.6 | 0.8 | 0.7 | 0.6 | 4 | 4 |

Ingest is now the largest phase (25%), with detect, decide and govern next. D0 is slower because the loop takes 84 short steps (15-minute steps while a caller ID is suspect) instead of about 30.

## What this means for the grading box (4 vCPU / 8 GB)

Not measured. The simulator, Postgres and the controller share 4 vCPUs there; this Mac gave them 11. At 95 s per day here, a 3x slower box takes about 285 s, under the 600 s bar with a 2x margin. The decision stages that remain (detect, decide, govern, lifecycle: 290 of 470 s) are Python and numpy on one thread plus Postgres queries, so they do not shrink with more cores. The floor is set by: the simulator advance (about 8 s per day, not ours), the ingest transform (about 23 s per day, Postgres bound: the `ev` insert, the `lead_dim` update and the `calls` temp table are 118 of 140 s of its SQL), and the per-step re-read of the rollups after each ingest (r_pickup and active lead counts, about 10 s per day). Further levers are listed in PERF.md "What remains".

## Outcome on this scenario (`sim grade` against `runs/base_public_03_load`)

Conclusion rate +11.4% overall (range +1.8% `kest_kyc_1` to +23.0% `arav_kyc_1`), pickup +7.7%, engagement +2.0%, engagement gap captured 13%. 94 config changes applied. Hallucinated calls 4,947 against 4,667 for the baseline (no incident in this scenario; slightly more, not fewer). These numbers are from this run (the grade was recomputed in the perf step; the approval and audit counts of the earlier report were not re-collected). They differ a little from the earlier report because the earlier report ran on an earlier commit (the ENG-GAP merge came after it); the perf change itself changes no decision (PERF.md).

## Against the DESIGN.md 8.7 estimates

| Phase per sim day | Estimate (8.7) | Measured here |
|---|---|---|
| Simulator advance | about 5 s (laptop), 15 s (grading box) | 7.7 s |
| Fetch + COPY + SQL transform and rollups | 19 to 33 s | 23.7 s |
| Snapshots, detect, decide, fits, governance | 15 to 45 s | about 62 s (detect 24, decide 18, govern 8, lifecycle 8, verify 4; was about 170 s) |
| History bootstrap (one time) | 2 to 3 min | 1.7 min |

The estimate for the decision stages was off by roughly 4x before the perf step and is about 1.4x over the top of the range now; ingest and bootstrap are inside theirs.
