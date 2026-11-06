# 0016. Ingest as Postgres ELT

Status: accepted (2026-11-06), milestone M1

## Context
- public_03 emits about 840k events per simulated day (about 370 MB of NDJSON) after a history of
  about 5.9M events (2.3 GB), on a 4 vCPU / 8 GB box shared with the simulator. SRE's bar is one
  sim day ingested and decided in under 10 wall minutes; our target is under 2.
- Every event must land exactly once, including after `kill -9` in the middle of a chunk. Chunks
  are numbered globally, never change and can be fetched again, so the chunk number is a natural
  idempotency key (DESIGN.md 8.1).
- Postgres is the only state (ADR 0001). The controller is single-threaded Python with numpy;
  parsing 10M JSON lines in Python and inserting row by row is the obvious bottleneck.

## Decision
1. **Load raw, transform in SQL.** A loader thread streams each chunk from httpx in 1 MiB pieces
   straight into `COPY stage_raw (chunk_no, line) FROM STDIN (FORMAT csv, DELIMITER E'\x02',
   QUOTE E'\x01')`. Each NDJSON line lands verbatim in one text column: JSON encoders escape
   control characters, so neither control byte can occur and no quoting ever happens. The chunk
   number is prefixed to every line while the bytes stream through (a `bytes.replace` per piece),
   so a chunk is never held in memory. The loader stages chunk k+1 while the main thread
   transforms chunk k.
2. **One transaction per chunk** runs `controller/ingest/transform.sql`: parse with
   `jsonb_to_record` into a typed temp table, check the count and first/last event id against the
   chunk metadata (mismatch: roll back, stage again once, else fail), then cells, campaigns, leads,
   calls, lead state, config and approval events, unknown event types (`event_other`), the four
   rollups as additive upserts, delete the staged lines and insert the `ingested_chunk` row. That
   row is the exactly-once guard: a chunk that did not commit is ingested again, a committed one
   is skipped.
3. **History calls go to rollups and lead state only**; post-D0 calls also go to `call_fact`.
   Nothing per call about the business-default history is ours to explain, and it saves about
   4.5M inserts at public_03.
4. **Facts derived from the tick, not the timestamp.** The simulator randomises a call's `ts`
   inside its tick, so hour, segment and `tick_no` come from the tick start
   `floor(minute / tick) * tick`; `epoch0` comes from `/sim/state`. Events are ordered by
   `event_id`, never by `ts`.
5. **Deterministic keys.** `lead_sk` is the lead event's `event_id` (not a sequence: a rolled
   back transaction would burn sequence values and a crashed run would differ from a clean one);
   cell ids and cohort codes are assigned in sorted order (`store/repos.py: COHORT_CODES_SQL`, one
   statement shared with the config mirror, so codes are stable per workflow across versions).
6. **Python fallback behind the same pipeline** (`CTL_INGEST=python`): `json.loads` and
   `COPY ... write_row` fill the same typed temp table inside the chunk transaction. Everything
   after parsing is the same SQL, so the paths cannot drift; a hand-written fixture test checks
   identical facts and rollups.
7. **Rollups are checkable.** `ingest.pipeline.rollup_mismatches` recomputes every post-D0
   rollup from `call_fact` and diffs it with the incremental one (0 rows expected).

## Alternatives
- **Parse in Python, insert with executemany or COPY of typed rows** (the fallback). Simpler to
  read, but the parse runs on the controller's single core; measured on the public_01 history
  chunk it is about as fast as the SQL path on this Mac, and the SQL path's parse runs inside
  Postgres where the box has spare cores while the controller thread waits. Kept as the fallback,
  selected by one setting if a slower box ever measures it faster.
- **Insert history calls into `call_fact` too.** Doubles the largest table for data no decision
  reads per call.
- **A broker or Redis stream in front of Postgres.** New stateful infrastructure SRE asked us to
  avoid, and the simulator already keeps chunks durable and re-fetchable.
- **Idempotency by event id (ON CONFLICT on every fact).** Works, but costs an index probe per
  event and still needs a cursor; the chunk row is one probe per 250k events.

## Consequences
- Exactly-once is a database fact, independent of the process: `scripts/kill_test.sh` and the
  crash matrix kill the controller mid-COPY and inside the transform and get identical rollups.
- `stage_raw` is UNLOGGED: a Postgres crash empties it, which is harmless because only chunks
  that are not in `ingested_chunk` are ever staged, and restart truncates it.
- Lead exhaustion by time is applied once per campaign, in the chunk whose `until_minute` first
  reaches the campaign's last cadence day, so lead state depends on chunk boundaries; those are
  the simulator's, identical in every run of one world.
- Throughput is measured, not estimated, in `reports/public_03_load.md`.
