# public_03 load test

All numbers **measured on this Mac on 2026-11-07** with the **full controller** (`CTL_LEARNER=probe`: detection, learning, governance, incident hooks, audit writes; the earlier version of this report measured the no-op loop). Mac15,6, 11 CPUs, 18 GB RAM, **no Docker** (the daemon hangs): `CTL_NO_DOCKER=1 ./run.sh` against a private Postgres 17.6 started with the compose settings (shared_buffers 1GB, work_mem 64MB, max_wal_size 4GB, synchronous_commit on, fsync on). The grading box (4 vCPU / 8 GB Linux) is slower and has no such headroom; see "What this means for the grading box" below. Produced by `scripts/load_test.sh` (`CTL_NO_DOCKER=1 PG_DSN=... CTL_PG_PORT=55489 CTL_LOAD_SIM_PORT=8764 PGDATA_DIR=/tmp/i6_pgdata scripts/load_test.sh scenarios/public_03_load.yaml`).

**Verdict: mean 200 s per simulated day, worst 215 s. That meets the 600 s hard bar on this Mac and misses the 120 s target by about 1.7x.**

Scenario `scenarios/public_03_load.yaml`: 5 run days, 9,737,765 events in 187 chunks, run schema `run_0c308db58dc0_1`.

## Correctness

- Ingested events 9,737,765 in 187 chunks; `/sim/state` last_event_id 9,737,765, chunks 187: **equal**.
- Rollups vs a from-scratch recomputation over `call_fact` (post-D0 rows that differ): r_engage 0, r_pickup 0, r_did 0, r_flags 0.
- Config versions above the business default: 93 (controller changes: 69 via approval, 24 auto-applied).

## Wall time

- Simulator start, including generating the history: 34 s (simulator cost, not ours).
- `./run.sh` total (history ingest, all run days, final checks; no image build): 1111 s.
- History ingest at startup: 5,537,531 events, 23 chunks, 2.35 GB NDJSON in 112 s wall (49,494 events/s end to end; transform transactions alone 52,040 events/s).

| Sim day | Steps | Events | Wall s | Events/s (end to end) | Advance s | Ingest s | Ops s | Controller RSS MB | Database GB (all schemas) |
|---|---|---|---|---|---|---|---|---|---|
| D0 | 84 | 840,059 | 214.8 | 3,911 | 6.8 | 28.3 | 0.0 | 233 | 0.55 |
| D1 | 33 | 840,047 | 170.5 | 4,926 | 7.9 | 25.0 | 0.0 | 288 | 0.66 |
| D2 | 34 | 840,047 | 207.5 | 4,048 | 8.2 | 25.1 | 0.0 | 320 | 0.81 |
| D3 | 24 | 840,045 | 192.6 | 4,361 | 8.5 | 24.6 | 0.0 | 325 | 0.96 |
| D4 | 29 | 840,036 | 214.4 | 3,918 | 8.6 | 26.1 | 0.0 | 325 | 1.10 |

Mean wall time per sim day **200 s**, worst 215 s (target under 120 s, SRE bar 600 s). `Advance` is the simulator computing the day (its single-threaded loop; we wait for it), `Ingest` is streaming, COPY and the set-based transform. The table's `Ops s` column is 0 because the full controller's stages are not in that column: they are in the next table. "Events/s end to end" divides the day's events by the whole day's wall time, decision stages included; pure ingest runs at about 33k events/s (840k events in 25 s).

## Memory and storage

| Process | Peak memory MB | Limit |
|---|---|---|
| controller process (ps RSS, sampled every 2 s) | 325 | 1 GB target |
| controller RSS (loop_step.rss_mb, /proc) | 325 | |
| Postgres, sum of RSS of all its processes | 4413 | not capped here (compose caps it at 3 GB) |
| simulator process (ps RSS) | 596 | host |

This run's schema at the end: 1.09 GB including indexes (call_fact partitions 0.52 GB); the whole database, which also holds earlier runs' schemas, is 1.10 GB. Largest tables:

- `lead_dim`: 590 MB
- `call_fact_d002`: 84 MB
- `call_fact_d004`: 84 MB
- `call_fact_d000`: 84 MB
- `call_fact_d001`: 84 MB
- `call_fact_d003`: 84 MB
- `r_pickup`: 47 MB
- `posterior_snapshot`: 26 MB

The Postgres figure is an upper bound: RSS counts the 1 GB of shared buffers once per backend that touched it. It is above the 3 GB compose limit, so the true resident set under the limit is unmeasured; if the container limit binds, Postgres would shrink its cache, not die, but ingest and the decision queries would slow. Treat Postgres memory as the open risk on an 8 GB box.

## Where the time goes (full controller, from `loop_step.phase_ms`, seconds per simulated day)

| Phase | D0 | D1 | D2 | D3 | D4 | 5-day total |
|---|---|---|---|---|---|---|
| detect (drift, caller-ID, flag detectors, snapshots) | 97.4 | 51.7 | 60.7 | 46.9 | 58.9 | 316 |
| decide (fits, harm gates, allocator, winners, decay) | 43.7 | 35.3 | 43.2 | 32.5 | 44.4 | 199 |
| verify (post-apply verification) | 21.5 | 32.3 | 35.3 | 29.7 | 31.9 | 151 |
| ingest | 28.3 | 25.0 | 25.5 | 24.2 | 26.1 | 129 |
| lifecycle (proposal state machine, applies) | 6.0 | 6.2 | 32.8 | 23.9 | 13.5 | 82 |
| govern (release train, gates) | 16.3 | 10.1 | 14.2 | 17.5 | 17.8 | 76 |
| advance (simulator) | 6.8 | 7.9 | 8.2 | 8.6 | 8.5 | 40 |
| plan, refresh | 1.0 | 0.5 | 0.8 | 0.8 | 0.5 | 4 |

`detect` is the largest phase and `decide` plus `verify` the next two; ingest, the earlier bottleneck, is now 13% of the wall time. D0 is slower because the loop takes 84 short steps (15-minute steps while a caller ID is suspect) instead of about 30.

## What this means for the grading box (4 vCPU / 8 GB)

Not measured. The simulator, Postgres and the controller share 4 vCPUs there; this Mac gave them 11. If the whole run is 2x to 3x slower, a day takes 400 to 600 s, which is at or over the 600 s hard bar. The decision stages (detect, decide, verify: 666 of 1000 s) are Python and numpy on a single thread plus Postgres queries, so they do not shrink with more cores; the ingest and Postgres parts do. This is a real risk and is not tuned away here; the first lever would be skipping detection work on steps that ingest nothing new for a workflow, and caching the cell-level snapshot between the detect and decide phases.

## Outcome on this scenario (`sim grade` against `runs/base_public_03_load`)

Conclusion rate +11.0% overall (range +1.9% `kest_kyc_1` to +16.6% `kest_renewal_0`), pickup +6.9%, engagement +2.4%, engagement gap captured 16%. 82 approval requests (74 approved, 7 declined by the approver, 0 expired), 24 auto-applies, 0 apply conflicts, 0 lapses, 0 auto-apply rejections. Hallucinated calls 4,975 against 4,667 for the baseline (no incident in this scenario; slightly more, not fewer). Audit: 2,557 decision rows, 107 proposals, 0 intents left `started`. Three `COHORT_MISMATCH` pages (the realised-share check at 3 SE, about what chance gives over a few hundred shares; see RUNBOOK).

## Against the DESIGN.md 8.7 estimates

| Phase per sim day | Estimate (8.7) | Measured here |
|---|---|---|
| Simulator advance | about 5 s (laptop), 15 s (grading box) | 8.0 s |
| Fetch + COPY + SQL transform and rollups | 19 to 33 s | 25.8 s |
| Snapshots, detect, decide, fits, governance | 15 to 45 s | about 170 s (detect 63, decide 40, verify 30, lifecycle 16, govern 15) |
| History bootstrap (one time) | 2 to 3 min | 1.9 min |

The estimate for the decision stages was off by roughly 4x; ingest and bootstrap landed inside theirs.
