# public_03 load test

All numbers **measured on this Mac on 2026-11-08** on the final code (HEAD of `controller`, includes ADR 0035) with the **full controller** (`CTL_LEARNER=probe`, the default: detection, learning, governance, incident hooks, audit writes). Mac15,6, 11 CPUs, 18 GB RAM, **no Docker** (the daemon hangs): `CTL_NO_DOCKER=1 ./run.sh` against a private Postgres 17.6 started with the compose settings (shared_buffers 1GB, work_mem 64MB, max_wal_size 4GB, synchronous_commit on, fsync on), a fresh empty cluster. Another agent was running unit tests on the same machine during part of the run (the load average was not controlled), so the slower days (D2, D4) may include that noise. The grading box (4 vCPU / 8 GB Linux) is slower and has no such headroom; see "What this means for the grading box" below. Produced by `scripts/load_test.sh`:

```bash
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55489 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55489 \
    CTL_LOAD_SIM_PORT=8784 PGDATA_DIR=/tmp/final_pgdata scripts/load_test.sh scenarios/public_03_load.yaml
```

**Verdict: mean 100 s per simulated day, worst 114 s (previous measurement: mean 95 s, worst 102 s). That meets the 120 s target and the 600 s hard bar on this Mac; a box 3x slower would take about 300 s per day, 6x slower about 600 s.** Compared with the previous report the mean is 5 s higher; the final code adds the DID-line enforcement of ADR 0035 and the ENG-GAP changes, and the host was not idle, so the difference is within what I would expect from noise and I did not isolate it.

Scenario `scenarios/public_03_load.yaml`: 5 run days, 9,737,762 events in 174 chunks, run schema `run_0c308db58dc0_1`.

## Correctness

- Ingested events 9,737,762 in 174 chunks; `/sim/state` last_event_id 9,737,762, chunks 174: **equal**.
- Rollups vs a from-scratch recomputation over `call_fact` (post-D0 rows that differ): r_engage 0, r_pickup 0, r_did 0, r_flags 0.
- Config versions above the business default: 89 (the grader lists 89 applied changes).
- Audit store: 2,249 decision rows, 853 audit rows, 103 proposals, 0 intents left `started`.

## Wall time

- Simulator start, including generating the history: 33 s (simulator cost, not ours).
- `./run.sh` total (history ingest, all run days, final checks; no image build): 600 s.
- History ingest at startup: 5,537,531 events, 23 chunks, 2.35 GB NDJSON in 100 s wall (55,384 events/s end to end).

| Sim day | Steps | Events | Wall s | Events/s (end to end) | Advance s | Ingest s | Controller RSS MB | Database GB (all schemas) |
|---|---|---|---|---|---|---|---|---|
| D0 | 84 | 840,060 | 95.9 | 8,757 | 6.4 | 24.5 | 248 | 0.58 |
| D1 | 28 | 840,052 | 89.5 | 9,384 | 7.6 | 23.0 | 323 | 0.70 |
| D2 | 34 | 840,039 | 114.5 | 7,339 | 8.3 | 25.5 | 332 | 0.82 |
| D3 | 18 | 840,047 | 88.5 | 9,493 | 8.3 | 22.8 | 347 | 0.96 |
| D4 | 27 | 840,033 | 110.9 | 7,572 | 8.8 | 27.0 | 347 | 1.09 |

Mean wall time per sim day **100 s** (499.3 s over 5 days), worst 114.5 s (target under 120 s, SRE bar 600 s). `Advance` is the simulator computing the day (single-threaded; we wait for it), `Ingest` is streaming, COPY and the set-based transform; the other stages are in the next table.

## Memory and storage

| Process | Peak memory MB | Limit |
|---|---|---|
| controller process (loop_step.rss_mb, /proc) | 347 | 1 GB target |
| Postgres, sum of RSS of all its processes | 4580 | not capped here (compose caps it at 3 GB) |
| simulator process (ps RSS) | 611 | host |

This run's schema at the end: 1.08 GB including indexes. The Postgres figure is an upper bound: RSS counts the 1 GB of shared buffers once per backend that touched it. It is above the 3 GB compose limit, so the true resident set under the limit is unmeasured; if the container limit binds, Postgres would shrink its cache, not die, but ingest and the decision queries would slow. Treat Postgres memory as the open risk on an 8 GB box.

## Where the time goes (full controller, from `loop_step.phase_ms`, seconds per simulated day)

| Phase | D0 | D1 | D2 | D3 | D4 | 5-day total |
|---|---|---|---|---|---|---|
| ingest | 24.5 | 23.0 | 25.5 | 22.8 | 27.0 | 122.8 |
| detect (drift, caller-ID, flag detectors, snapshots) | 30.9 | 21.5 | 28.5 | 15.6 | 24.8 | 121.3 |
| decide (fits, harm gates, allocator, winners, decay) | 19.0 | 22.2 | 30.1 | 18.1 | 22.7 | 112.1 |
| govern (release train, gates) | 9.7 | 7.3 | 8.9 | 7.3 | 8.3 | 41.5 |
| lifecycle (proposal state machine, applies) | 2.9 | 3.3 | 11.1 | 9.3 | 10.1 | 36.7 |
| advance (simulator) | 6.4 | 7.6 | 8.3 | 8.3 | 8.8 | 39.4 |
| verify (post-apply verification) | 2.7 | 5.0 | 4.8 | 2.9 | 3.8 | 19.2 |
| plan, refresh | 0.9 | 0.5 | 1.0 | 0.5 | 0.8 | 3.7 |

Ingest and detect are the largest phases (about 24% each), decide next. D0 takes 84 short steps (15-minute steps while a caller ID is suspect) instead of 18 to 34.

## What this means for the grading box (4 vCPU / 8 GB)

Not measured. The simulator, Postgres and the controller share 4 vCPUs there; this Mac gave them 11. At 100 s per day here, a 3x slower box takes about 300 s, under the 600 s bar with a 2x margin. The decision stages (detect, decide, govern, lifecycle: about 310 of 500 s) are Python and numpy on one thread plus Postgres queries, so they do not shrink with more cores. The floor is set by the simulator advance (about 8 s per day, not ours), the ingest transform (about 24 s per day, Postgres bound) and the per-step re-read of the rollups after each ingest. Levers are listed in `docs/design/build-reports/PERF.md` "What remains".

## Outcome on this scenario (`sim grade` against `runs/base_public_03_load`)

Conclusion rate +10.8% overall (range +1.5% `kest_kyc_1` to +15.4% `nort_kyc_1`), pickup +6.6%, engagement +2.7%, engagement gap captured 16%. 89 config changes applied (57 neutral, 32 beneficial). Hallucinated calls 4,886 against 4,667 for the baseline (no incident in this scenario; slightly more, not fewer). The previous report had +11.4%, +7.7% pickup, 94 changes: the controller's outcome moves with the code and the experiment draws; one run is one sample.
