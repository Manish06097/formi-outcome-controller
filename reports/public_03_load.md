# public_03 load test

All numbers **measured on this Mac** (2026-11-06): Mac15,6, 11 CPUs, 18 GB RAM; Docker Desktop VM 11 CPUs, 8217890816 bytes. The grading box (4 vCPU / 8 GB Linux) is slower; DESIGN.md 8.7 assumes about 3x for the simulator. Produced by `scripts/load_test.sh` (no-op decide and govern stages, milestone M1/M2), SQL ingest path.

Scenario `scenarios/public_03_load.yaml`: 5 run days, 9,737,591 events in 93 chunks, run schema `run_86f11233c32e_2`.

## Correctness

- Ingested events 9,737,591 in 93 chunks; `/sim/state` last_event_id 9,737,591, chunks 93: **equal**.
- Rollups vs a from-scratch recomputation over `call_fact` (post-D0 rows that differ): r_engage 0, r_pickup 0, r_did 0, r_flags 0.
- Config versions above the business default: 0 (the no-op loop changes nothing).

## Wall time

- Simulator start, including generating the history: 33 s (simulator cost, not ours).
- `./run.sh` total (image build cached, Postgres start, history ingest, all run days, final checks): 317 s.
- History ingest at startup: 5,537,531 events, 23 chunks, 2.35 GB NDJSON in 152 s wall (36,501 events/s end to end; transform transactions alone 37,013 events/s).

| Sim day | Steps | Events | Wall s | Events/s (end to end) | Advance s | Ingest s | Ops s | Controller RSS MB | Database GB (all schemas) |
|---|---|---|---|---|---|---|---|---|---|
| D0 | 15 | 840,012 | 29.6 | 28,388 | 5.6 | 23.8 | 0.0 | 93 | 1.90 |
| D1 | 15 | 840,012 | 34.0 | 24,718 | 5.8 | 28.0 | 0.0 | 94 | 2.02 |
| D2 | 15 | 840,012 | 32.3 | 25,970 | 5.8 | 26.4 | 0.0 | 94 | 2.14 |
| D3 | 15 | 840,012 | 32.2 | 26,098 | 5.8 | 26.2 | 0.0 | 93 | 2.28 |
| D4 | 16 | 840,012 | 32.9 | 25,503 | 5.9 | 26.8 | 0.0 | 93 | 2.40 |

Mean wall time per sim day **32 s**, worst 34 s (target under 120 s, SRE bar 600 s). `Advance` is the simulator computing the day (its single-threaded loop; we wait for it), `Ingest` is streaming, COPY and the set-based transform.

## Memory and storage

| Process | Peak memory MB | Limit |
|---|---|---|
| controller container (docker stats) | 104 | 1 GB |
| controller RSS (loop_step.rss_mb, /proc) | 94 | |
| Postgres container (docker stats) | 1753 | 3 GB |
| simulator process (ps RSS) | 306 | host |

This run's schema at the end: 1.04 GB including indexes (call_fact partitions 0.52 GB); the whole database, which also holds earlier runs' schemas, is 2.40 GB. Largest tables:

- `lead_dim`: 591 MB
- `call_fact_d002`: 84 MB
- `call_fact_d001`: 84 MB
- `call_fact_d003`: 84 MB
- `call_fact_d000`: 84 MB
- `call_fact_d004`: 84 MB
- `r_pickup`: 41 MB
- `r_engage`: 5 MB

## Against the DESIGN.md 8.7 estimates

| Phase per sim day | Estimate (8.7) | Measured here |
|---|---|---|
| Simulator advance | about 5 s (laptop), 15 s (grading box) | 5.8 s |
| Fetch + COPY + SQL transform and rollups | 19 to 33 s | 26.2 s |
| Snapshots, detect, decide, fits, governance | 15 to 45 s | not built yet (M3 to M6 add them) |
| History bootstrap (one time) | 2 to 3 min | 2.5 min |

Ingest is the measured bottleneck of the platform: about 26k to 37k events/s per transform transaction, slower than the 40k to 80k estimated, mostly the lead lookups and lead-state updates against a growing `lead_dim`. At the measured rate a day leaves about 85 s of the 120 s target for the decision stages, and the SRE bar of 600 s has a wide margin even at a 3x slower box.

