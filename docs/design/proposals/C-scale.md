# Design proposal C-scale: scale and operability first

Status: proposal (designer C). Inputs: `README.md`, `docs/SIMULATOR.md`, `notes/UNDERSTANDING.md`,
`docs/decisions/0001-stack.md`, `stakeholders/*.md`, `sim/*.py` (read only, to check mechanics).
Stack is fixed by ADR 0001: Python 3.11, Postgres 16, httpx, numpy, psycopg 3, FastAPI, Docker Compose,
no LLM framework.

The angle: one controller that runs hundreds of workflows across tens of tenants without an engineer in
the loop, on 4 vCPU / 8 GB, and that an on-call engineer can debug from its own tables. The learning loop
is complete but deliberately simple: fixed-share factorial experiments with pre-registered boundaries, a
holdout for decay, and per-cohort hour weights from history.

Every number below is either (a) read from the API at runtime, (b) a computed statistic shown with its
formula, or (c) an estimate marked "est." that the M1 load test must replace with a measurement. Public
scenario truth appears only in section 7's predicted timeline, as a sanity check, never as a parameter.

One measured fact used throughout: the file mtimes of the existing baseline run
`runs/base_public_03_load` show the simulator itself produces one public_03 sim day (840,012 events,
about 384 MB of NDJSON in 4 chunks of at most 250k events) in about 6 s of wall time on the dev laptop,
and the 7 history days (23 chunks, about 2.4 GB) in about 35 s. The simulator is not the bottleneck; our
ingest and decision path is.

---

## 1. Scope

| # | Capability | Status | Why | Cost to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | **Built** (deepest) | Every decision is only as fresh as ingest; SRE's 10-minute bar; idempotency is a hard constraint. | - |
| C2 | Lead/feature joins, one shared cohort object | **Built** | Feature cells (lang x tier x age x value) are the unit everything aggregates on; cohorts are versioned sets of cells shared by detection, experiments and the supervisor view. | - |
| C3 | Bootstrapping from history | **Built (thin)** | History ran on the default config only, so it yields baselines (pickup by cell x hour, per-DID pickup ratio, hallucination flag rate, engagement by cell) but no variant priors. Cheap once C1 exists. | - |
| C4 | Call-time optimisation | **Partial**: per-cohort hour weights. Per-person **cut**. | About 2 to 3 dials per person per campaign and a 20 to 40% returning share give too little per-person data; cohort hour curves capture most of the archetype signal. | Per-person: 1.5 days (Beta posterior per person x hour band, 200k-entry cap management, schedule diff size). |
| C5 | Caller-ID health | **Built** | Largest pickup lever (load-driven health) and an incident class with no event. | - |
| C6 | Welcome x voice experiments | **Built (simplified)** | Factorial, fixed shares, harm elimination, Haybittle-Peto efficacy boundary, Bonferroni across challengers. No bandit, no automatic heterogeneity search. | Adaptive allocation and cohort discovery: 2 days. |
| C7 | Promotion, decay detection, rollback | **Built** | Promotion keeps a 20% holdout so decay is measured concurrently, not before/after. | - |
| C8 | Hallucination incident response | **Built** | Two-key detection (live flag rate or prompt-change trigger, confirmed by one validation run), last-known-good fix, internal gate stricter than the approver's, post-apply verification. Prompt *optimisation* (moving to a better safe prompt) is **cut**: one prompt per workflow means no concurrent control. | Prompt optimisation via switchback design: 2 days. |
| C9 | Governance client | **Built** | Hard constraint surface; most governance-hygiene points are lost here. | - |
| C10 | Impact score, affected leads, dry run | **Partial** | Honest formulas from observed arm data and history reweighting. No counterfactual simulator. | 1 day. |
| C11 | Supervisor view | **Partial** | Read-only JSON API plus one server-rendered HTML page per tenant. No auth, no notifications. | 2 days for a real UI. |
| C12 | Decision / audit store | **Built** | Constraint 6 and the live review question. | - |
| C13 | Crash recovery, demonstrated | **Built** | Fault-injection points at every side-effect boundary plus a random kill -9 loop, with a crash-equivalence assertion. | - |
| C14 | Observability of the loop | **Built** | Per-step metrics table, `/ops` view, alert rules with page/ticket split. No Prometheus exporter. | 0.5 day for an exporter. |
| C15 | Fair allocation across tenants | **Cut** (minimal guard only) | A per-tenant credit envelope and a per-workflow reserved approval slot exist because C8/C9 need them; no cross-tenant optimiser. | 1.5 days. |

**Selection principle.** Build the substrate that every hard constraint and every other capability stands
on (C1, C2, C9, C12, C13, C14) to production grade first, because a failure there is disqualifying or
silently corrupts everything above it. Then add levers in order of expected outcome per unit of evidence
available at the *smallest* scale we must handle (about 500 to 1,200 connected calls per workflow-day on the
dev scenarios): caller-ID (C5) and per-cohort call time (C4) are pickup levers measurable within hours from
dials; incident response (C8) protects outcomes and is graded on time-to-fix; experiments (C6, C7) need
days of data so they get a simple, provably-controlled design. Cut what the data cannot support
(per-person timing, prompt optimisation) and what has no grader or stakeholder weight yet (C15).

---

## 2. Architecture

```
                         host (grading box, 4 vCPU / 8 GB)
 +-------------------------------+        +-----------------------------------------------+
 | simulator (python -m sim)     |  HTTP  | controller container (single process)         |
 |  /sim/advance  /events/chunks |<------>|                                               |
 |  /workflows /approvals ...    |        |  main thread: control loop                    |
 +-------------------------------+        |   plan -> advance -> ingest -> refresh ->     |
                                          |   stats -> detect -> decide -> govern -> audit|
                                          |                                               |
                                          |  loader thread: stream chunk n+1 -> COPY      |
                                          |   (bounded queue of 2 chunks)                 |
                                          |  heartbeat thread: 10 s heartbeat row         |
                                          +-------+---------------------------------------+
                                                  | psycopg 3 (2 to 3 connections)
                                                  v
 +---------------------------------------------------------------------------------------+
 | Postgres 16 container (only stateful component)                                       |
 |  schema ctl:           run_registry                                                   |
 |  schema run_<fp>:      ingest (stage_raw UNLOGGED, ingested_chunk)                    |
 |                        facts (lead_dim, call_fact partitioned by sim_day, campaign,   |
 |                               config_event, approval_event, event_other)              |
 |                        rollups (r_engage, r_pickup_hour, r_did_hour, r_wf_hour)       |
 |                        model (cell, cohort_def, config_version, config_cohort,        |
 |                               experiment, experiment_look, did_state, incident,       |
 |                               baseline)                                               |
 |                        governance (proposal, intent)                                  |
 |                        audit_log, loop_step, alert, clock                             |
 +---------------------------------------------------------------------------------------+
                                                  ^ read-only role
                                          +-------+---------------------------------------+
                                          | api container (FastAPI, uvicorn, 1 worker)     |
                                          |  /supervisor/{tenant}  /ops  /audit/...        |
                                          +-----------------------------------------------+
```

**Components and responsibilities**

| Component | Job | State it owns |
|---|---|---|
| Clock | Picks the next stop (section 4), records an advance intent, calls `/sim/advance`, reconciles. | `clock`, `intent(kind=advance)` |
| Ingestor | Fetches chunks in order, streams bytes into `stage_raw` via COPY, transforms set-based in SQL, updates rollups, marks the chunk, all in one transaction per chunk. | `ingested_chunk`, facts, rollups |
| Catalog | Loads `/workflows` specs (options, policy, windows, cadence) at start and after every `config_changed`; fetches live config bodies. | `workflow`, `config_version` |
| Snapshot builder | Builds a read model per workflow from rollups into numpy arrays. | none (derived) |
| Detectors | DID degradation, hallucination spike, guardrail breach, decay, supervisor edit. | `did_state`, `incident` |
| Learners | Call-time weights, DID spread, factorial experiments, promotion and rollback. They emit `ChangeIntent`s; they never call the simulator. | `experiment`, `experiment_look`, `baseline` |
| Governance | Merges intents into one proposal per workflow, rebases on the live config, checks local policy, picks auto-apply or approval, times the submit, applies within `apply_by`, verifies. | `proposal`, `intent` |
| Audit | Append-only decision log, written in the same transaction as each state change. | `audit_log` |
| Ops | Per-step metrics, alert evaluation, heartbeat. | `loop_step`, `alert` |
| API | Read-only views over the same tables. | none |

**Where state lives.** Everything durable is in Postgres. The controller process holds only caches that
can be rebuilt from the database (workflow specs, cell catalog, numpy snapshots). The simulator's chunks
stay on its disk and can be re-fetched, so `stage_raw` is UNLOGGED and disposable.

**Process structure.** One controller process, because the simulator has one global clock and one
`advance` caller must own it. Three threads: the control loop, a chunk loader (I/O bound, psycopg releases
the GIL during COPY), and a heartbeat. The supervisor/ops API is a separate container with a read-only
database role, so a slow page can never stall the loop. A Postgres session-level advisory lock on the run
id (`pg_try_advisory_lock`) guarantees one controller per run: a second instance (for example during a
21:30 deploy overlap) waits on the lock instead of double-acting.

**World identity.** If the simulator restarts it is a new world with the same ids. At start the
controller computes `fingerprint = sha256(scenario, end_minute, tick_minutes, sorted workflow and tenant
ids, metadata of history chunk 0)` and looks it up in `ctl.run_registry`. It resumes an existing run only
if that run is consistent with the live simulator: our `clock.sim_minute <= state.sim_minute`, our max
ingested chunk `< state.chunks`, our max ingested event id `<= state.last_event_id`, and every approval id
we hold is returned by `GET /approvals`. Otherwise it creates a new schema `run_<fp12>_<n>`. Schema per run
keeps keys narrow (no `run_id` column on 8M-row tables) and lets old runs be dropped with one statement.

**What happens on restart** (kill -9 anywhere):

1. Acquire the advisory lock; resolve the run schema as above.
2. Delete `stage_raw` rows for chunks not in `ingested_chunk` (UNLOGGED rows may already be gone).
3. Reconcile every `intent` with status `started` against the simulator (section 8). No new side effect is
   issued until every open intent is resolved.
4. Ingest every chunk the simulator lists that we have not ingested (`GET /events/chunks?kind=history|events&after=`).
5. Rebuild in-memory caches from the database and continue the loop at the step after the last completed
   one. Learned state (rollups, experiment looks, DID states, baselines) is in the database, so nothing is
   forgotten.

---

## 3. Data model

### 3.1 Ingest path: raw lines to typed facts inside Postgres

The decision: **stream chunk bytes from httpx straight into a COPY, and parse JSON inside Postgres with
one set-based transform per chunk** (ELT), instead of parsing 840k JSON objects per sim day in Python.

| | Python parse (`json.loads` + `copy.write_row`) | Postgres ELT (chosen) |
|---|---|---|
| Python CPU per sim day | est. 10 to 14 s under the GIL (840k x about 6 us parse + 3 us tuple build + 1.5 us COPY row, plus rollup dict updates) | est. under 1 s (byte passthrough) |
| Parallelism | One core; competes with the decision loop for the GIL | Postgres backend processes, independent of the GIL; splits by workflow at 10x |
| Exactly-once | Facts, rollups and chunk marker must still be committed together | Facts, rollups and chunk marker in one SQL transaction by construction |
| Debuggability | Python stack traces | SQL; mitigated by `event_other` capturing unknown types and by fixture tests |

COPY uses `FORMAT csv, DELIMITER E'\x02', QUOTE E'\x01'` so each NDJSON line lands verbatim in one text
column. This is safe because JSON encoders escape control characters (`\u0001`), so neither byte can
appear in a line. The transform parses each line once with `jsonb_to_record` into a temp table and fans out
by `type`.

```sql
-- one transaction per chunk n (pseudo-SQL)
BEGIN;
  -- guard: exactly-once by chunk number
  SELECT 1 FROM ingested_chunk WHERE chunk_no = :n;            -- if found: ROLLBACK, skip
  -- loader thread already ran: COPY stage_raw(chunk_no, line) FROM STDIN (streamed, 1 MB writes)
  CREATE TEMP TABLE ev ON COMMIT DROP AS
    SELECT r.*, ((extract(epoch FROM r.ts) - :epoch0) / 60)::int AS minute
    FROM stage_raw s,
         LATERAL jsonb_to_record(s.line::jsonb) AS r(
           type text, event_id bigint, ts timestamptz, workflow_id text, tenant_id text,
           campaign_id text, lead_id text, person_id text, attempt_no int, campaign_day_index int,
           config_version int, cohort_id text, variant jsonb, connected bool, engaged bool,
           disposition text, duration_s int, turns int, qa jsonb, features jsonb, leads int,
           config_version_at_creation int, version int, actor text, changed_dims jsonb,
           proposal_id text, approval_id text, status text, reasons jsonb)
    WHERE s.chunk_no = :n;
  INSERT INTO cell(lang, tier, age, value) SELECT DISTINCT ... FROM ev WHERE type='lead'
    ON CONFLICT DO NOTHING;                                     -- dynamic feature vocabulary
  INSERT INTO campaign ... FROM ev WHERE type='campaign_created';
  INSERT INTO lead_dim (...) SELECT ... FROM ev JOIN cell USING (...) WHERE type='lead';
  INSERT INTO call_fact (...) SELECT ... FROM ev JOIN lead_dim USING (lead_id) WHERE type='call';
  UPDATE lead_dim SET concluded_minute = ... FROM ev WHERE ev.type='call' AND ev.engaged;
  INSERT INTO config_event / approval_event / event_other ...;
  INSERT INTO r_engage ... GROUP BY ... ON CONFLICT (...) DO UPDATE SET n = r_engage.n + excluded.n, ...;
  INSERT INTO r_pickup_hour / r_did_hour / r_wf_hour ... (same pattern);
  DELETE FROM stage_raw WHERE chunk_no = :n;
  INSERT INTO ingested_chunk (chunk_no, kind, events, first_event_id, last_event_id, until_minute,
                              bytes, txn_ms) VALUES (...);
  -- sanity: count(ev) must equal chunk meta events, else ROLLBACK and alert (truncated download)
COMMIT;
```

`epoch0` is derived from the API, not hard-coded: `epoch0 = parse(state.ts) - state.sim_minute * 60 s`.
Ordering is by `event_id`, never `ts` (ts is not monotonic within a chunk). Leads always precede their
calls in event-id order, so the in-order transform always finds the lead. A call whose lead is missing gets
`cell = NULL`, is counted in `loop_step.orphan_calls`, and raises a ticket alert.

**Pipelining.** The loader thread streams chunk n+1 into `stage_raw` (its own connection and transaction)
while the main thread transforms chunk n. Transforms run strictly in chunk order. The queue holds at most
2 staged chunks, so staging never exceeds about 2 x 127 MB (est.) of disk and the controller never holds
a chunk in memory (httpx `iter_bytes(1 MiB)` into `copy.write`).

### 3.2 Tables

Feature cells: `cell_id` is a dense smallint over observed `(lang, tier, age, value)` combinations, at most
7 x 3 x 4 x 3 = 252 under the current vocabulary, but the dictionary is built from observed values so an
unseen language in a hidden scenario just adds cells.

```sql
-- catalog and identity
workflow(wf_sk smallint PK, workflow_id text UNIQUE, tenant_id text, spec jsonb, spec_hash text,
         fetched_minute int)
cell(cell_id smallint PK, lang text, tier text, age text, value text, UNIQUE(lang, tier, age, value))
cohort_def(cohort_key text PK,           -- e.g. 'age_60p~3fa2c1' : readable prefix + sha1(canonical where)[:6]
           where_canon jsonb,            -- sorted features, sorted values
           cells smallint[],             -- raw membership (before first-match resolution)
           origin text, created_minute int)
config_version(wf_sk, version int, actor text, config jsonb, config_hash text, applied_minute int,
               proposal_id text, approval_id text, body_source text,  -- 'fetched' | 'ours' | 'unknown'
               PRIMARY KEY (wf_sk, version))
config_cohort(wf_sk, version, position smallint, cohort_id text, cohort_key text,
              effective_cells smallint[],  -- after first-match-wins resolution
              PRIMARY KEY (wf_sk, version, cohort_id))

-- ingest
stage_raw(chunk_no int, line text)  UNLOGGED, no index
ingested_chunk(chunk_no int PK, kind text, events int, first_event_id bigint, last_event_id bigint,
               until_minute int, bytes bigint, ingested_wall timestamptz, txn_ms int)

-- facts (raw, typed, compact)
campaign(campaign_id text PK, wf_sk, created_minute, sim_day smallint, leads int, config_version_at_creation int)
lead_dim(lead_sk bigint identity PK, lead_id text UNIQUE, person_id text, wf_sk smallint,
         campaign_id text, cell smallint, created_minute int, concluded_minute int NULL)
  INDEX (wf_sk, created_minute) WHERE concluded_minute IS NULL      -- active-lead counts
call_fact(event_id bigint, wf_sk smallint, lead_sk bigint, minute int, sim_day smallint, hour smallint,
          attempt_no smallint, day_index smallint, config_version int, cohort_id text, cell smallint,
          w text, v text, did text, prompt text, connected bool, engaged bool, interested bool,
          halluc_flag bool, flow_adherence real, duration_s smallint, turns smallint)
  PARTITION BY RANGE (sim_day)       -- one partition per sim day, history days are negative
  INDEX BRIN (event_id), BRIN (minute), BTREE (lead_sk)              -- lead_sk for compliance lookups
config_event(event_id PK, wf_sk, version, actor, changed_dims text[], proposal_id, approval_id, minute)
approval_event(event_id PK, approval_id, wf_sk, status, reasons jsonb, minute)
event_other(event_id PK, type text, body jsonb)                      -- forward compatibility

-- rollups (derived, maintained in the ingest transaction, rebuildable from call_fact)
r_engage(wf_sk, sim_day, config_version, cohort_id, cell, w, v, prompt,
         connected int, engaged int, halluc_flags int, PRIMARY KEY (all key columns))
r_pickup_hour(wf_sk, sim_day, hour, cell, attempt_bucket smallint,   -- 1, 2, 3+
              dials int, connected int, PRIMARY KEY (...))
r_did_hour(wf_sk, sim_day, hour, did, dials int, connected int, PRIMARY KEY (...))
r_wf_hour(wf_sk, sim_hour int, prompt, dials int, connected int, engaged int, halluc_flags int,
          short_hangups int, PRIMARY KEY (...))

-- learned model state
baseline(wf_sk, metric text, scope text, value double precision, n bigint, window text, computed_minute int,
         PRIMARY KEY (wf_sk, metric, scope))
experiment(exp_id PK, wf_sk, dim_set text[], status text, incumbent jsonb, arms jsonb, share_per_arm real,
           mde_rel real, n_target int, alpha_spec jsonb, launched_version int, launched_minute int,
           decided_minute int, result jsonb)
experiment_look(exp_id, look_no, minute, per_arm jsonb, per_cohort jsonb, decision text, PRIMARY KEY (exp_id, look_no))
did_state(wf_sk, did, status text, ratio real, z real, n_dials int, since_minute int, evidence jsonb,
          PRIMARY KEY (wf_sk, did))
incident(incident_id PK, wf_sk, kind, status, opened_minute, confirmed_minute, resolved_minute,
         evidence jsonb, fix jsonb, credits_spent real)

-- governance and reliability
proposal(proposal_id text PK, wf_sk, kind text, priority smallint, path text, state text,
         base_version int, proposed_config jsonb, components jsonb, change_summary text, diff jsonb,
         affected_leads int, dry_run jsonb, impact_score real, rationale text, evidence jsonb,
         approval_id text, submitted_minute int, decided_minute int, apply_by_minute int,
         applied_version int, superseded_by text, verify jsonb)
  UNIQUE INDEX one_in_flight ON proposal(wf_sk)
    WHERE state IN ('submitting','pending','approved','applying','auto_applying')
intent(intent_id bigint identity PK, kind text, idem_key text UNIQUE, request jsonb, status text,
       response jsonb, sim_minute int, started_wall timestamptz, finished_wall timestamptz)

-- audit and ops
audit_log(audit_id bigint identity PK, sim_minute int, wall timestamptz, wf_sk smallint NULL,
          actor text, action text, subject_type text, subject_id text, summary text,
          details jsonb, caused_by bigint NULL REFERENCES audit_log)
  INDEX (wf_sk, sim_minute), INDEX (subject_type, subject_id)
loop_step(step_no PK, from_minute, to_minute, phase_ms jsonb, chunks int, events int, orphan_calls int,
          decisions int, proposals_submitted int, applies int, credits_remaining real, rss_mb int,
          db_bytes bigint, ingest_lag_wall_ms int)
alert(alert_id PK, severity text, code text, wf_sk, opened_wall, closed_wall, message text, runbook text)
clock(id smallint PK CHECK (id = 1), sim_minute int, end_minute int, tick int, last_step_no int)
```

The partial unique index `one_in_flight` makes "one change in flight per workflow" a database invariant,
which is what protects us from our own auto-apply staling our own pending approval.

### 3.3 Raw versus aggregated

| Data | Kept as | Why |
|---|---|---|
| NDJSON lines | Not kept (staged, then deleted in the same transaction) | The simulator keeps chunks and they are re-fetchable; in production raw events belong in object storage, not the OLTP database. |
| Calls | `call_fact`, typed and compact, every call | Compliance needs "why did this customer hear this, on that day" per call; rollups must be rebuildable; dry runs replay it. |
| Leads | `lead_dim`, every lead | Feature join for calls (only lead events carry features), affected-lead counts. |
| Experiment, detection, call-time inputs | Rollups only | Decisions read at most thousands of rollup rows per workflow, never scan facts. |
| Config bodies, proposal payloads | Stored by us | `GET /approvals` has no `proposed_config` and config history has no bodies. |

Note on config bodies: we can only fetch the *current* body. If two config changes land inside one step
(for example a supervisor edit followed by another), the intermediate body is lost; we record
`body_source='unknown'` for it and say so in the audit trail. Steps of at most 60 to 105 minutes make this
rare.

### 3.4 Sizes at public_03 scale

12 workflows x 4,500 dials/hour x 12 hours = 648k calls per day plus 12 x 16,000 = 192k lead events per day
(both read from `/workflows`: `dials_per_hour`, `dialling_window`, `daily_batch.size`). The run covers 7
history days plus 5 days = 12 days.

| Object | Rows | Bytes per row (est.) | Size (est.) |
|---|---|---|---|
| `call_fact` heap | 12 x 648k = 7.8M | about 95 (24 header + 70 data) | 740 MB |
| `call_fact` indexes | BTREE lead_sk 7.8M x about 22 B; 2 BRIN | | 175 MB |
| `lead_dim` + unique index on `lead_id` | 12 x 192k = 2.3M | about 120 + 45 | 380 MB |
| `r_pickup_hour` | up to 12 h x 252 cells x 3 buckets = 9k per workflow-day, est. 100k/day | about 50 | 60 MB |
| `r_engage` | est. 36k per day (cells x W x V x versions occupied) | about 60 | 26 MB |
| `r_did_hour`, `r_wf_hour` | about 600 and 150 per day | | under 1 MB |
| governance, audit, metrics | thousands | | under 10 MB |
| WAL | bounded by `max_wal_size = 4GB` | | 1 to 2 GB typical |
| **Database total** | | | **about 1.4 GB + WAL** |

The simulator's own run directory is about 4.1 GB for public_03 (measured: `history/` 2.3 GB,
`events/` 1.8 GB). Disk needed on the box: about 8 GB. Dev scenarios (about 13.7k events per day) are
under 50 MB in total.

Postgres settings (compose): `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=64MB`,
`maintenance_work_mem=256MB`, `max_wal_size=4GB`, `checkpoint_timeout=15min`, `wal_compression=on`,
`jit=off` (JIT compile time dominates short set-based transforms), `synchronous_commit=on` (audit
durability is a constraint), autovacuum scale factor 0.05 on rollup tables (upsert churn).

---

## 4. The control loop

### 4.1 Per-step sequence

```
startup: lock -> resolve run -> reconcile intents -> ingest missing history+events -> load caches
         -> bootstrap baselines from history (once) -> loop

loop (one iteration = one "step"):
  1  plan      : to = next_stop(now)                                  (4.2, pure function)
  2  intent    : INSERT intent(kind='advance', idem_key='adv:<from>-><to>', status='started'); COMMIT
  3  advance   : POST /sim/advance {minutes: to-now}; read timeout 900 s; never retried blindly
                 on success: intent.status='done', response=new_chunks; clock.sim_minute=to; COMMIT
  4  ingest    : for chunk in new_chunks (plus any gap from GET /events/chunks?after=max_ingested):
                 loader COPY -> transform txn (3.1). Decisions are blocked until ingest lag is 0.
  5  refresh   : for each workflow with a config_changed event this step: GET /workflows/{id}/config,
                 store body in config_version, resolve config_cohort, run supervisor-edit handler (6.6).
                 For each approval_decided event on our proposals: GET /approvals/{id} for apply_by.
  6  apply     : for each APPROVED proposal: preflight (version == base_version, now <= apply_by) then
                 intent -> POST /workflows/{id}/apply -> record. Done before any new submit, so the
                 apply never races our own next change.
  7  stats     : build WorkflowSnapshot for every workflow touched by new data (rollups -> numpy).
  8  detect    : DID detector, hallucination detector, guardrail monitor, decay monitor (section 7).
                 Each finding is written with its audit row in one transaction.
  9  decide    : learners emit ChangeIntents: incident fixes, rollbacks, experiment looks, promotions,
                 call-time refresh (daily), DID spread.
 10  govern    : per workflow: merge intents -> one ProposalDraft rebased on the live config ->
                 local policy check -> path (auto or approval) -> timing gate -> submit or hold.
                 Every submit/auto-apply: proposal row + audit row + intent row COMMIT, then HTTP call,
                 then outcome COMMIT.
 11  verify    : post-apply checks whose windows have elapsed (incident fixed? guardrail ok?).
 12  ops       : loop_step row, alert evaluation, heartbeat.
 13  if state.finished: final reconcile, run_registry.status='done', exit 0.
```

Steps 8 to 10 write their audit rows *before or atomically with* the state change they justify, and the
state change is committed before any HTTP side effect (intent pattern, section 8).

### 4.2 Advance step-size rule

All inputs come from the API: `tick = state.tick_minutes`, `end = state.end_minute`, and per workflow
`dialling_window`, `governance.approver_hours`, `apply_window_hours`, `approver_latency_minutes`,
`proposal_ttl_hours`.

```python
def next_stop(now, ctx) -> int:
    cands = [ctx.end]
    for wf in ctx.workflows:
        g = wf.governance
        L = g.apply_window_hours * 60 - tick            # 105 min for a 2 h window and 15 min tick
        if wf.has_approved_unapplied:                    # should not happen after step 6; safety
            return now                                   # apply first, do not advance
        if wf.has_pending:
            if in_hours(now, g.approver_hours):
                cands.append(now + L)
            else:                                        # nothing can be decided before opening
                cands.append(next_open(now, g.approver_hours) + L)
        if wf.planned_submit_at is not None:             # routine slot or verification window end
            cands.append(wf.planned_submit_at)
        for t in wf.verify_due_at:
            cands.append(t)
        if in_window(now, wf.dialling_window):
            cands.append(now + wf.detect_interval)       # see below
        else:
            cands.append(next_window_start(now, wf.dialling_window) + wf.detect_interval)
    t = min(cands)
    step = max(tick, floor_to_multiple(t - now, tick))
    return now + min(step, ctx.max_step)                 # max_step = 1440
```

Why `L = apply_window - tick` is safe: the simulator decides a pending approval at the first tick start at
or after its hidden decide minute `d` and checks lapse as `now > d + window`. If `d` falls just after our
stop `S`, the decision is processed during the next advance and is visible when that advance returns at
`S + L`; applying at `S + L <= d + window` holds for any `d >= S`, with one tick of slack. Every decision
we can see is therefore applied in the same step it becomes visible.

`detect_interval` per workflow is the time for each live DID to collect enough dials for the detector's
2-hour window to be meaningful: `ceil_to_tick(60 * N_hour / (dials_per_hour / n_live_dids))` clamped to
[60, 120] minutes, with `N_hour = 100` dials per DID per hour. Dev workflows (200 to 400 dials/hour, 2 to 3
DIDs) and public_03 (4,500/hour) both resolve to 60 minutes. With hundreds of workflows the global step is
the minimum over all of them, so in practice: 60 minutes inside dialling hours, at most 105 minutes while
anything is pending in approver hours, and one overnight jump from the last window close to the next
window start or approver opening, whichever is first. About 14 steps per sim day.

Before an overnight jump the planner requires that no proposal is APPROVED-unapplied (step 6 runs every
iteration, so this is an assertion that raises a page alert if violated).

### 4.3 Wall-clock budget per public_03 sim day (est., to be measured in M1)

| Phase | Per day | Basis |
|---|---|---|
| Simulator advance | 6 s (laptop, measured), est. 15 s on the grading box | baseline run mtimes |
| Chunk fetch + COPY (384 MB) | 4 to 8 s, overlapped | loopback HTTP + COPY of one text column |
| SQL transform + rollups (840k events) | 15 to 25 s | jsonb parse est. 40 to 80k events/s per backend |
| Snapshots + detect + decide (12 wf x 14 steps) | 10 to 20 s | about 50 to 100 ms per workflow-step |
| Governance HTTP calls | under 1 s | a handful of calls per step |
| **Total** | **about 40 to 70 s** | target in SUBMISSION: under 2 minutes; SRE bar 10 minutes |

History bootstrap (5.9M events, 2.4 GB) is a one-time cost of est. 2 to 3 minutes, pipelined.

---

## 5. Learning loop

### 5.1 Cohorts: one shared object with versioned identity

- **Unit of aggregation:** the feature cell. Every rollup is keyed by `cell`, so any cohort is a set of
  cells and its statistics are a sum over rows, with no re-scan of facts.
- **Identity:** `cohort_key = <readable prefix> + "~" + sha1(canonical where)[:6]`, for example
  `age_60p~3fa2c1`. The config `cohorts[].id` we write *is* the `cohort_key`, so detection, experiments,
  the supervisor view and the simulator's `cohort_id` on call events all use one id. A different
  definition always gets a different id; nothing is ever redefined in place.
- **Effective membership:** first match wins and order matters, so `config_cohort.effective_cells` is
  resolved per config version. Supervisor-defined cohorts get their ids as-is plus a `cohort_def` row.
- **Partition choice (from history only):** for each feature f, fit the hour-by-value interaction for
  pickup in history (`connected ~ hour + f` versus `connected ~ hour * f`, binomial deviance from
  `r_pickup_hour`). Pick f* with the largest deviance drop per degree of freedom with p < 0.001. Cohorts
  are the values of f*, with any value below the minimum size merged into its nearest neighbour by hour
  profile. If nothing is significant, no cohorts: default only.
- **Minimum cohort size:** a cohort must expect at least `max(300, 0.08 x daily dials)` dials per day.
  Reason: hour weights need about 1,000 dials per cohort-hour over the history window to estimate a pickup
  index with a standard error near 5% relative (pickup about 0.25, 14 history days); experiments need
  cohorts large enough that a per-cohort harm check has some power. Cost: on nw_renewal-sized workflows
  (3,600 dials/day) at most about 6 to 10 cohorts can exist, so finer heterogeneity is not targetable.
  That is the speed versus false-winner trade Product asked us to state.
- At most 6 cohorts by default (cap 20), so the partition never fragments experiment traffic.

### 5.2 Experiment design (engagement-primary workflows only)

Pickup-primary workflows (`objective.primary == pickup_rate`) get no welcome/voice experiments: those
levers can only move their guardrail.

- **Factorial:** welcome and voice are sampled independently per dial, so one experiment tests both
  dimensions on the same traffic. Assumption (stated in SUBMISSION): no welcome x voice interaction. We
  check it at the final look with a 2x2 interaction z-test on the winning pair and report it; we do not
  act on it.
- **Arms:** incumbent keeps `1 - (k-1) a` of its dimension; each of the `k-1` challengers gets `a`.
- **Share a:** `a = 0.10` by default, raised to at most 0.15 if the planned horizon would not fit in the
  remaining days minus 2. If even 0.15 does not fit, test only one challenger per dimension (lowest
  option index not yet tested; no priors, so no smarter order exists).
- **Harm budget:** with `a = 0.10` and 2 welcome + 3 voice challengers, the expected engagement change at
  apply is `sum_i a (m_i - 1)`. If one challenger is 0.88x, the launch is about -1.2% and may be graded
  harmful. We accept that risk explicitly at launch because no exploration means no lift, and we bound
  its duration with daily harm elimination.
- **Launch path:** customer-facing, so always via approval, bundled into the workflow's foundation
  proposal on D0 (section 6.2).

### 5.3 Winner rule, power and false-winner rate

Test per challenger j versus incumbent 0 on engagement rate (engaged / connected), one-sided two-proportion
z-test with pooled variance, computed only from call events (`r_engage`): an arm that never ran has n = 0
and cannot win.

| Rule | Boundary | Alpha spent |
|---|---|---|
| Harm elimination, daily, non-binding | `z <= -1.645` and n_j >= 150 connected: drop arm j, its share returns to the incumbent | none (dropping cannot create a false winner) |
| Interim efficacy at 50% of n_target | `z >= 3.29` (Haybittle-Peto) | 0.0005 |
| Final efficacy at n_target | `z >= 2.346` | 0.0095 |
| Per challenger total | | **0.01** |
| Family (5 challengers: 2 welcome + 3 voice) | Bonferroni | **FWER <= 0.05** |

Plus a practical gate: point estimate of relative lift >= +2% (above the grader's 1% harm line and above
noise in the promotion change itself), and the guardrail check (5.7).

**Sample size.** Per-challenger connected calls needed, with unequal allocation r = n_inc / n_j:

`n_j = (z_a + z_b)^2 x (p0 q0 / r + p1 q1) / (p1 - p0)^2`, with `z_a = 2.346`, `z_b = 0.84` (power 0.8),
so `(z_a + z_b)^2 = 10.15`. Baseline engagement p0 = 0.55 (typical value from the baseline report; the
controller uses each workflow's own history baseline).

| MDE (relative) | p1 | r | n_j | Dev renewal (940 conn/day, a=0.10: 94/day) | Dev renewal a=0.15 (141/day) | public_03 workflow (13.5k/day, a=0.10: 1,350/day) |
|---|---|---|---|---|---|---|
| +20% | 0.660 | 8 | 10.15 x (0.0309 + 0.2244) / 0.0121 = **214** | 2.3 days | 1.5 days | 0.2 day |
| +15% | 0.6325 | 8 | 10.15 x (0.0309 + 0.2324) / 0.0068 = **393** | 4.2 days | 2.8 days | 0.3 day |
| +10% | 0.605 | 8 | 10.15 x (0.0309 + 0.2390) / 0.0030 = **906** | 9.6 days | 6.4 days | 0.7 day |
| +5% | 0.5775 | 8 | 10.15 x (0.0309 + 0.2440) / 0.00076 = **3,690** | infeasible | infeasible | 2.7 days |

(r = 8 because the welcome incumbent keeps 0.8 when two challengers get 0.1 each; for voice the incumbent
keeps 0.7, r = 7, which changes n_j by under 2%.)

**What this means.** On dev-sized workflows the design can confirm effects of about +15% or larger within
the scenario, and anything smaller stays at the incumbent. That is the honest cost of a 1% false-winner
rate per challenger. On public_03-sized workflows +5% effects are confirmable in under 3 days.

**False-winner rate.** At most 1% per challenger under the null, at most 5% per workflow. Across 12
public_03 workflows, if every challenger were null, the expected number of false promotions is at most
12 x 0.05 = 0.6 per run (Bonferroni bound, real value lower). Harm elimination at z <= -1.645 drops a truly
null arm 5% of the time per look; that costs power, not correctness.

**Heterogeneity (cohort-safe promotion).** At the final look we also compute the challenger's effect per
partition cohort. The winner is promoted workflow-wide *except* in any cohort where the point estimate is
below -3% relative and the cohort-level `z <= -1.645`; those cohorts get a cohort-level allocation that keeps
the incumbent. This only withholds a promotion, so it spends no alpha. A challenger that is not significant
workflow-wide but has cohort-level `z >= 3.02` in one cohort (alpha 0.01 split over at most 6 cohorts, then
over 5 challengers) can be promoted in that cohort only. Power for that is low at dev scale (a +22% effect
in a 25% cohort at a = 0.15 needs about 8 days), and we say so.

### 5.4 Promotion, holdout, decay and rollback

- **Promotion:** winner gets `1 - h` of the dimension in its scope, the previous incumbent keeps a holdout
  `h = 0.2`, other challengers are removed. Via approval (customer-facing).
- **Decay monitor (daily and at each look):** rolling 2-day window, winner versus holdout, from call
  events. Roll back when both hold: (a) current-window lift <= 0 (the winner is no longer better than the
  holdout), and (b) the drop from the lift measured at promotion is significant, `z >= 2.5` on the
  difference of lifts. Rule (a) avoids rolling back at decay onset, when the variant is still at full
  strength and the rollback itself would be graded harmful.
- **Power (dev renewal, 940 connected/day):** winner 752/day, holdout 188/day, 2-day window: SE of lift =
  sqrt(0.25/1504 + 0.2475/376) = 0.0287. A winner that went from +15% (0.6325 vs 0.55) to -7% (0.5115)
  swings the lift by 0.121; SE of the difference of two such windows is about 0.0415, so z = 2.9 when fully
  decayed. Detection therefore lands about 1 to 2 days after the multiplier crosses below the holdout. At
  public_03 scale the SE is about 3.9 times smaller and detection takes hours.
- **Rollback target:** the holdout (last known good), 100%. Rollback is a priority proposal (it may use the
  reserved incident slot). If time remains (>= horizon + 2 days), surviving challengers are relaunched.
- **Holdout cost:** 0.2 x lift of the winner, for example 3% of a +15% effect. Paid for the ability to see
  decay at all.
- **Freeze:** no launches when remaining days < horizon + 2; no promotions in the last 12 sim hours; only
  rollbacks and incident fixes in the final day.

### 5.5 Call time (per cohort)

Hour weights decide who is dialled in an hour, so what matters is a cohort's pickup *relative to the other
cohorts in that same hour*. From `r_pickup_hour` over history plus live data:

```
index[c,h]  = p[c,h] / p[.,h]                     # cohort pickup relative to all leads, same hour
p[c,h]      = (conn[c,h] + k * p[.,h] * I[c]) / (dials[c,h] + k)    # shrink to cohort overall index I[c], k = 200
weight[c,h] = clip(index[c,h], 0.25, 4) ** 2      # sharpen; every in-window hour stays positive
```

The ratio form cancels most of the eligibility bias (later hours over-represent hard-to-reach leads for
every cohort alike). `attempt_bucket = 1` rows are used first; buckets 2 and 3+ are pooled only where a
cell-hour has fewer than 200 first-attempt dials. Every in-window hour gets a positive weight (floor
0.25^2) so no capacity is wasted. Cohort-level hour weights need the cohorts to exist, so they ship in the
foundation approval; daily refreshes go through auto-apply where `schedule` is allowed and no proposal is
in flight, else into the next routine approval. Refresh only if the L1 distance of normalised weights
moves by more than 0.1 (avoids churn and version bumps).

### 5.6 Caller ID

- **Allocation rule:** spread uniformly over all DIDs in `options.caller_id` whose `did_state` is not
  `degraded`. Rationale (domain prior, stated as an assumption): carrier spam scoring rises with per-number
  volume, so spreading load is the default. It is verified after the fact: per-DID pickup ratios are
  measured concurrently (the DID is sampled independently of the lead, so DIDs are compared within the same
  hours and cohort mix) and before/after pickup is reported in the verification record.
- **Unknown DIDs** (never used in history) are included in the spread. The grader's DID-health delta is
  positive whenever a new DID's health exceeds the load-degraded health of the current ones, and the
  detector removes a bad one within hours.
- **Degraded DIDs** get weight 0. Re-admission of a degraded DID is a probe of at most 5% share, offset in
  the same change by spreading the rest, and only after 3 days.

### 5.7 Guardrail

Guardrail definition (assumption stated in SUBMISSION): `max_drop` from `objective.guardrail` is read as a
**relative** drop of the guardrail KPI (the stricter reading). Two checks:

1. **Pre-promotion:** for engagement experiments, pickup per arm (dials of that arm) is compared to the
   incumbent concurrently. Promotion is blocked if the relative drop exceeds `max_drop` and the one-sided
   `z <= -1.645`.
2. **Post-apply monitor (every change):** for 2 dialling days after an apply, the guardrail KPI of the
   workflow is compared to the 3 days before, matched by hour and attempt bucket (direct standardisation
   over `r_pickup_hour` cells). A breach (relative drop > `max_drop`, `z <= -3`) opens a rollback proposal
   for the most recent change and a ticket alert.

Proving non-inferiority at 2% relative is not feasible at dev scale (it needs tens of thousands of dials
per arm), so the guardrail is enforced as "no evidence of harm beyond the threshold", and that limitation is
stated.

---

## 6. Governance client

### 6.1 State machine

```
             intents
                |
                v
           +---------+  no net change / superseded
           |  DRAFT  |------------------------------> DISCARDED
           +----+----+
     approval   |                    auto (policy-permitted and our policy allows)
          +-----+-----------------------------+
          v                                   v
    +------------+                     +---------------+  403 (local check bug) -> FAILED + page
    | SUBMITTING |                     | AUTO_APPLYING |  409 stale -> STALE -> rebase DRAFT
    +-----+------+                     +-------+-------+
  422 -> FAILED + ticket                       | 200
          | 200 (approval_id)                  v
          v                               +---------+
    +----------+ --- expired ---------->  | EXPIRED | -> new DRAFT from fresh data (Product: expired = rejection)
    | PENDING  | --- rejected --------->  | REJECTED| -> 24 h cooldown for same components, then may requeue
    +----+-----+ --- base changed ------> | STALE   | -> new DRAFT rebased on live config
         | approved                       +---------+
         v
    +----------+ --- now > apply_by -----> LAPSED (never reached by design; page) -> new DRAFT
    | APPROVED | --- version != base ----> STALE (detected by preflight, apply never called)
    +----+-----+
         | preflight ok -> intent
         v
    +----------+  409 stale -> STALE; 410 -> LAPSED; 409 not_approved -> reconcile
    | APPLYING |
    +----+-----+
         v
    +---------+       +-----------+  window elapsed, checks pass   +-----------+
    | APPLIED | ----> | VERIFYING | -----------------------------> | CONFIRMED |
    +---------+       +-----+-----+                                +-----------+
                            | guardrail breach / incident not fixed
                            v
                       ROLLBACK or ESCALATE (new priority DRAFT, ticket)
```

Every transition writes an `audit_log` row in the same transaction. `proposal_id =
"<run8>:<workflow_id>:<seq>"` with `seq` allocated in the database when the DRAFT commits, so retries after
a crash reuse the id (the simulator's id namespace is global and consumed on POST).

### 6.2 Batching

- One **foundation proposal** per workflow on D0: cohorts (5.1) + hour weights (5.5) + DID spread (5.6) +
  experiment launch (5.2, engagement-primary only). One approval instead of four, and cohorts must exist
  before cohort hour weights anyway.
- Afterwards, at most **one routine proposal per workflow per day**, at a planned slot. All intents that
  accumulated since the last proposal are merged, rebased on the live config, and dropped if the net diff is
  empty.
- **Priority proposals** (incident fix, rollback) bypass the slot, but still obey fatigue and timing rules.
- Merge rule: intents are patches on dims scoped to cohorts. Later intents on the same (cohort, dim)
  replace earlier ones; incident intents override routine ones on the same (cohort, dim); a supervisor
  edit drops routine intents on the dims it touched (6.6).

### 6.3 Timing

All quantities from the workflow's `governance` block and the clock.

- **Slot:** `day_start + approver_open*60 + 60` (one hour after approvers open). This honours "never before
  11:00" generically, without encoding any tenant's name.
- **No-expiry rule:** submit only if `next_working(now + latency_max) <= now + ttl*60`, where
  `next_working` is the simulator's documented rule (outside approver hours the decision moves to the next
  opening). `next_working` is monotone in latency, so the worst case is `latency_max`. For a 12 h TTL,
  10 to 19 approvers and 120 min max latency, the forbidden submit band is 17:00 to 21:59.
- **Fatigue:** we count every request record for the workflow in the trailing 24 h, whatever its status,
  from our own `proposal` table (cross-checked against `GET /approvals?workflow_id=`). Routine proposals need
  `count + 1 <= min(2, cap - 1)`; priority proposals need `count + 1 <= cap`. This keeps one slot per
  workflow per 24 h for incidents and caps routine traffic at 2 per day, which is what the Kestrel
  supervisor asked for and is stricter than every shipped policy.
- **Overnight:** no submit after the last time the no-expiry rule allows; the planner never jumps past an
  approver close with an approval APPROVED-unapplied.

### 6.4 Auto-apply policy

The local policy checker re-implements the documented rules exactly: `changed_dims` (cohort list compared
in order, weights normalised, any `lead_preferred_hour` change counts as `schedule`), `max_shift` as the
maximum total-variation distance over every cohort id in old or new config and every agent dim, and
`allowed_dims`/`enabled`. A 403 should therefore never happen; one is a bug and pages.

| Dim | Policy allows auto-apply | We auto-apply? |
|---|---|---|
| `caller_id` | if listed | Yes for incident mitigation (one step <= `max_shift`) and for routine spread when TV <= `max_shift`; otherwise approval |
| `schedule` | if listed | Yes for hour-weight refresh on cohorts that already exist, only when nothing is in flight |
| `welcome_message`, `voice` | where listed | **Never.** Customer-facing; Northwind Compliance requires human approval and the Kestrel supervisor wants to be told first. Voluntary restraint, stated in SUBMISSION. |
| `cohorts`, `prompt_version` | not in any public policy | Approval only |

We never chain several `max_shift`-sized auto-applies to move further than one step allows; the rest of a
move goes through an approval. Auto-apply skips approver fatigue and hours, so every auto-apply is shown in
the supervisor view's "changed today" list and in the weekly summary (Compliance asked for that).

### 6.5 Impact score, affected leads, dry run

- **Affected leads** = active leads (campaign within cadence days, not concluded) whose cell is in a
  cohort whose effective allocation, schedule or prompt changes, plus expected new leads in the next 24 h
  in those cells (batch size x cell share over the last 3 batches). For `mid_flight: next_campaign_only`,
  allocation changes count only new leads. A prompt change counts all active leads.
- **Impact score**: `impact = clip(Phi(delta_hat / se) x min(1, |delta_rel| / 0.10) x reach, 0, 1)` where
  `delta_hat` is the expected change of the workflow's primary KPI computed from observed per-cell, per-arm
  rates (or history reweighting for schedule, per-DID ratios for caller_id), `se` its standard error, and
  `reach` the share of daily dials affected. Read as: 0.5 means "probably about +5% on the whole
  workflow's primary KPI". Incident fixes and rollbacks are 1.0 with the reason stated, so they sort first.
- **Dry run** (non-empty, structured, honest about method):

```json
{"method": "observed_arm_replay", "window": "D2 00:00 to D4 00:00", "kpi": "engagement_rate",
 "arms": {"V3": {"n_connected": 412, "rate": 0.621, "ci95": [0.574, 0.668]},
          "V1": {"n_connected": 2950, "rate": 0.552, "ci95": [0.534, 0.570]}},
 "per_cohort": {"age_60p~3fa2c1": {"delta_rel": 0.19, "z": 2.4}, "age_18_30~91bc0e": {"delta_rel": -0.06, "z": -1.1}},
 "expected_delta_rel": 0.094, "expected_delta_ci95": [0.031, 0.157],
 "excluded_cohorts": [], "guardrail": {"pickup_rel_delta": -0.004, "z": -0.3},
 "caveats": ["no welcome x voice interaction assumed", "estimate from 2 days of concurrent traffic"]}
```

### 6.6 Supervisor edits

On a `config_changed` event with `actor = supervisor`:

1. Fetch and store the new body; compute the dims and cohorts it touched.
2. Any of our proposals in PENDING or APPROVED for that workflow is marked STALE immediately (its apply
   would 409 and be logged as `apply_conflict`). We never call apply on it.
3. Rebase: our next draft starts from the supervisor's config. Routine intents on dims the supervisor
   touched are dropped: the supervisor's choice becomes the new incumbent. Intents on other dims are kept.
4. Experiments on a touched dim are closed at the edit's config version ("only what happened counts":
   looks use only call events up to that version) and replanned with the supervisor's choice as incumbent if
   time allows.
5. Safety overrides: if the edit introduces a hallucination incident (section 7) the fix is proposed
   through an approval with evidence, never applied silently.
6. A cohort-level allocation of ours still overrides the supervisor's new default inside that cohort.
   The supervisor view says so explicitly, so the supervisor can see where their default does not reach.

---

## 7. Incidents

### 7.1 Detectors

| Detector | Signal and test | Threshold | Expected false positives |
|---|---|---|---|
| DID degradation | Per DID, pickup versus pooled other DIDs in the same hours, two-proportion z, over the last 2, 4 and 8 dialling hours | `z <= -4.0` and ratio <= 0.75 and >= 150 dials on the DID | P(z <= -4) = 3.2e-5 per test. Tests per day = 14 steps x 3 windows x DIDs. Dev (about 10 DIDs): 420/day, at most 0.013/day, 0.13 per 10-day run (Bonferroni bound, overlapping windows make it lower). public_03 (about 42 DIDs): at most 0.056/day. At 300 workflows we would move to z <= -4.5 (3.4e-6 per test, about 0.13/day). |
| Hallucination spike (live) | Flags on connected calls in the last 4 dialling hours versus the workflow's own history baseline, one-sided Poisson | p < 1e-3 and rate ratio >= 2 | At most 14 tests/day per workflow x 1e-3 = 0.014/day per workflow; every hit is confirmed by validation before acting, so false *actions* are near zero |
| Prompt-change trigger | `config_changed` with `prompt_version` in `changed_dims` (any actor) | event | not statistical; it only triggers the confirmation run |
| Confirmation | One validation run of the live prompt, n = 300 (3 credits) | pass_rate < `min_validation_pass_rate` | See below |
| Guardrail breach | Section 5.7 | relative drop > max_drop, z <= -3 | 0.0013 per test |
| Decay | Section 5.4 | lift <= 0 and drop z >= 2.5 | 0.006 per test |
| Workflow pickup/engagement drift | CUSUM on hourly KPI versus matched baseline | alert only (ticket), no automatic change unless attributed to one of the above | - |

**Confirmation numbers.** Validation pass rate is a binomial on n synthetic conversations. With n = 300,
the standard deviation at a pass rate near 0.99 is sqrt(0.01 x 0.99 / 300) = 0.0057, so a prompt whose
true pass rate is 0.99 fails the 0.97 gate with probability P(z < -3.5), about 0.02%. A prompt with true
pass rate 0.945 (sd 0.0131) passes with probability P(z > 1.87), about 3%; in that case the live detector
still catches it within hours.

**DID detection time (computed).** Dev workflow at 400 dials/hour spread over 3 DIDs (133 per DID per
hour), pickup 0.18, a DID dropping to a third of normal pickup (0.063): after 2 hours n = 266 versus 532,
`SE = sqrt(0.18 x 0.82 / 532 + 0.063 x 0.937 / 266) = 0.0223`, `z = -5.2`: detected at the second hourly
stop. A milder drop to 0.6x takes about 6 dialling hours at that volume.

**Hallucination live detection time (computed).** Dev cancel-sized workflow: 72 connected per hour,
baseline flag rate f0 estimated from history. With f0 = 0.0147, a 4-hour window expects 4.2 flags; the
p < 1e-3 threshold is 13 or more flags. A prompt with a 3.6x flag rate (0.0535) expects 15.4 flags in 4
hours, so power at 4 hours is about 0.76, and near 1 by 6 hours. The prompt-change trigger gets to the
confirmation run at the first stop after the edit, which is usually faster.

### 7.2 Fix selection

- **DID degradation:** immediate mitigation by auto-apply if `caller_id` is allowed: move as much share
  off the bad DID as one `max_shift` step permits, onto the other non-degraded DIDs. Then one priority
  approval to finish the move (bad DID to 0, uniform over the rest). If auto-apply is not allowed, only
  the approval. The auto-apply goes first so the approval's `base_version` is the post-mitigation version.
- **Unsafe prompt:** candidates in order:
  1. **Last known good:** the most recent prompt that was live for at least one full dialling day with a
     flag rate not above the history baseline (Poisson test), and passed or never needed validation.
  2. Otherwise call `/llm/suggest-fixes` once (3 credits) to get candidates; the LLM only proposes.
  Each candidate must pass our **internal gate**: one validation run, n = 1,000 (10 credits), pass rate
  >= 0.985, stricter than the approver's 0.97. A candidate that fails is blacklisted for that workflow.
  We never re-run a failed validation hoping for a pass (cherry-picking). A last-known-good prompt
  only needs the approver's gate: one n = 300 run (3 credits).
  The internal gate exists because the approver's 0.97 gate cannot tell a 0.6% hallucination prompt from a
  2.5% one: at n = 300 a prompt with true pass rate 0.9725 passes 0.97 about 60% of the time, while at
  n = 1,000 it passes 0.985 with probability P(z > 2.4), about 0.8%, and a 0.99 prompt passes with
  probability about 92%.
- Deterministic choice among passing candidates: last known good first, then highest pass rate, then
  option order. The proposal says which prompt the supervisor had chosen and why we propose to move off it.

### 7.3 Credit budget

All from `state.credits_remaining` at start (C0).

- **Incident reserve** `R = max(30, 0.3 x C0)`. Routine spending (none planned: we run no prompt
  experiments) may never take the balance below R.
- **Per incident cost:** confirmation 3 + last-known-good validation 3 = 6 credits typical; worst case
  confirmation 3 + suggest 3 + two internal-gate runs 20 = 26. With C0 = 200: R = 60, so at least 2 worst
  case or 10 typical incidents are covered.
- **Per tenant envelope** (the minimal C15 guard): R is split across tenants by workflow count, plus a
  shared 25% pool, so one tenant's incident storm cannot starve another's.
- **Cache:** one confirmation run per (workflow, prompt) per 3 sim days; a prompt seen safe stays known safe.
- 402 out of credits: page alert; no blind prompt changes; the DID path needs no credits.

### 7.4 Post-apply verification

| Fix | Window | Pass condition | If it fails |
|---|---|---|---|
| DID move | 2 dialling hours after apply | bad DID dials = 0 (or mitigated share), workflow pickup ratio versus pre-incident baseline >= 0.9 | re-run detector on remaining DIDs; ticket |
| Prompt fix | 4 dialling hours | flag-rate Poisson test against history baseline not significant (p > 0.05) and rate ratio < 1.5 | reopen incident, next candidate, page |
| Any change | 2 dialling days | guardrail monitor (5.7) | rollback proposal |

### 7.5 Predicted public_02 timeline (sanity check only)

Using public truth only to check the design would find what it should. Hours are sim time.

| When | What happens | Our action |
|---|---|---|
| D0 00:00 | Start; ingest 14 history days (one chunk, about 190k events) in seconds | Baselines; partition feature chosen per workflow from hour-profile deviance |
| D0 11:00 | First routine slot (approvers open 10:00 + 60) | Foundation proposals: nw_renewal and ks_cancel (cohorts + hour weights + DID spread + factorial launch, a = 0.10 to 0.15), nw_kyc (cohorts + hour weights + DID spread over its 3 DIDs; no engagement experiment, primary is pickup) |
| D0 11:20 to 19:00 | Decisions (latency 20 to 480 min by workflow) | Applied at the first stop after each decision (<= 105 min). nw_kyc allocations reach the D1 08:00 campaign (next_campaign_only), its schedule applies at once |
| D1 to D2 | Daily looks | Harm elimination of bad arms in the D1/D2 routine slot (one approval each, only if allocations change) |
| D2 10:00 | ks_cancel D2 flagged (no event) | D2 has 1/3 of ks_cancel's dials after our spread |
| D2 12:00 stop | 2 h of evidence: z about -5 | `did_state = degraded`; auto-apply D2 share 0.333 -> 0.133 (TV 0.2 = max_shift); priority approval D2 -> 0, D1/D3 0.5 each. If a routine ks_cancel approval is pending, the auto-apply stales it (logged, re-proposed next day) |
| D2 about 12:20 to 14:00 | Decision (latency 20 to 120) | Applied at the next stop, by about 15:45 at worst. Predicted D2 flagged-DID daily share about 0.11, then 0 from D3 |
| D3 11:30 | Kestrel supervisor switches ks_cancel to P2 | - |
| D3 12:00 stop | `config_changed` (prompt) seen | Confirmation run on P2, n = 300 (3 credits): expected pass 0.945, fails the gate (about 97%). Last known good = P1 (live for all of history). Validation P1, n = 300 (3 credits). Priority approval to restore P1, with both runs as evidence and a summary addressed to the supervisor. Fatigue: within cap 4 |
| D3 12:20 to 14:00 | Approved (5% judgement-call rejection risk; on rejection, re-propose with fresh data) | Applied at the next stop. **Predicted MTTR 2.5 to 4.3 h.** Verification by 4 dialling hours of flag rate |
| D5 15:00 | Northwind supervisor sets default voice V2 mid-experiment | Seen at 16:00 stop. Pending nw_renewal proposals marked STALE (no apply call, so no apply_conflict). Voice experiment closed at the prior version; V2 becomes the incumbent for default; welcome experiment continues. Our cohort-level allocations, if any, still apply inside those cohorts, and the view says so |
| D6 onward | Promotions where boundaries are crossed; decay monitor on promoted winners | Freeze rules from about D7 |
| D9 | Final day | Only rollbacks and incident fixes |

Credit use predicted: about 6 to 9 of 200.

---

## 8. Reliability

### 8.1 Intent log and idempotency keys

Every side-effecting HTTP call follows: (1) commit the intent row (and the proposal state change and audit
row it belongs to), (2) make the call, (3) commit the outcome. A crash between 1 and 3 leaves an intent in
`started`, resolved at restart before anything else runs.

| Call | Idempotency key | Reconciliation after a crash |
|---|---|---|
| `POST /sim/advance` | `adv:<from>-><to>` | `GET /sim/state` (it waits on the simulator lock, so it returns after an in-flight advance finishes). `sim_minute == to` (or `end`): done, take chunks from `GET /events/chunks?after=`. `sim_minute == from`: not done, re-plan (may re-issue). Anything else: record the actual minute, page (should be impossible). |
| `POST /approvals` | `proposal_id` | `GET /approvals?workflow_id=` and look for our `proposal_id`. Found: record `approval_id`, state PENDING. Not found: the POST never landed; re-POST with the same `proposal_id` (a 409 duplicate here means it landed after all: look up again). |
| `POST /workflows/{id}/apply` | `approval_id` | `GET /workflows/{id}/config/history`: a version carrying our `approval_id` means applied. Otherwise preflight again and re-apply (a duplicate apply returns 409 not_approved, never a double apply). |
| `PUT /workflows/{id}/config` | `proposal_id` | Config history: a version with our `proposal_id` means applied. Otherwise retry with the same id (auto-apply consumes the id only on success). |
| `POST /validation/runs` | none offered by the API | Unknowable after a crash (no list endpoint). Mark intent `unknown`, count the credits as possibly spent (re-read `credits_remaining`), re-run only if still needed. At most one run lost per crash. |
| `POST /llm/suggest-fixes` | none offered | Same as validation. |
| Chunk ingest | `chunk_no` | `ingested_chunk` row exists or not; the transaction makes facts, rollups and marker atomic. |

### 8.2 Simulator errors and timeouts

| Response | Meaning for us | Handling |
|---|---|---|
| Connection error or 5xx on GET | transient | Retry with exponential backoff (0.5 s to 30 s, jitter), up to 10 min, then page and keep retrying |
| Timeout on `POST /sim/advance` | unknown whether time moved | Never retry blindly. Reconcile via `/sim/state` as in 8.1. Read timeout is 900 s because a long advance is legal |
| Timeout or 5xx on other POSTs | unknown | Reconcile by key as in 8.1, then retry |
| 409 `stale_base_version` | config moved under us | Proposal -> STALE, rebase. Should be rare because of preflight checks |
| 409 `duplicate_proposal_id` | our earlier attempt landed | Reconcile |
| 409 `not_approved` on apply | already applied or not approved | Reconcile from config history |
| 409 `finished` | scenario over | Final reconcile, exit 0 |
| 410 `approval_lapsed` | we missed `apply_by` | LAPSED, page (the planner should make this impossible), re-propose |
| 403 `auto_apply_not_permitted` | local policy checker is wrong | FAILED, page, disable auto-apply for that workflow for the rest of the run |
| 402 `out_of_credits` | budget exhausted | Page; incident path continues for DIDs only |
| 422 | our payload is invalid | FAILED, ticket, proposal not retried |
| 404 on chunk | chunk not yet listed | Re-list; if persistent, page |

---

## 9. Observability and supervisor view

### 9.1 Loop health (C14)

Written every step to `loop_step` and exposed at `GET /ops` (JSON plus one HTML table):

| Metric | Definition | Target |
|---|---|---|
| Ingest throughput | events / transform wall seconds, per chunk and per day | >= 30k events/s (est.) |
| Ingest lag (sim) | `clock.sim_minute - max(ingested until_minute)` at decision time | always 0 (decisions are blocked otherwise) |
| Ingest lag (wall) | advance return to last chunk committed | < 10 s per step at public_03 |
| Decision lag (wall) | advance return to end of govern phase | < 15 s per step |
| Detection lag (sim) | incident `confirmed_minute - opened_minute`, and opened versus first evidence hour | reported per incident |
| Wall per sim day | sum of step wall time per sim day | < 2 min at public_03 (est.), SRE bar 10 min |
| Decisions per sim day and per wall hour | audit rows with action in (propose, apply, rollback, eliminate, promote) | reported |
| Credit burn | credits used per sim day, remaining versus incident reserve | above reserve |
| Governance health | pending, approved-unapplied, 24 h fatigue per workflow, rejections, expiries, lapses, 403s, apply conflicts | lapses, 403s and conflicts always 0 |
| Resources | controller RSS (from `/proc/self/status`), `pg_database_size`, WAL size | RSS < 500 MB |
| Liveness | heartbeat age, restarts in last hour | heartbeat < 60 s |

### 9.2 Alerts

**Page** (needs a human now, message says what to look at):

| Code | Condition | Message points to |
|---|---|---|
| `INGEST_STALLED` | chunks listed but not ingested for 5 wall min | last `ingested_chunk`, transform error in `alert.message` |
| `APPLY_AT_RISK` | an APPROVED proposal within 30 sim min of `apply_by` and not applying | proposal id, preflight failure reason |
| `GOV_INVARIANT` | any 403, lapse or apply conflict | proposal id and local policy check output; auto-apply disabled for that workflow |
| `CREDITS_BELOW_RESERVE` | balance < incident reserve | burn table by kind |
| `INCIDENT_UNRESOLVED` | incident confirmed and not resolved within 6 sim hours | incident id, stuck state |
| `CRASH_LOOP` | more than 3 restarts in 10 wall min | last exception in container logs, last intent |
| `DB_DISK` | database or WAL > 80% of volume | largest partitions |

**Ticket or daily digest** (can wait until morning): approval rejected, proposal expired, experiment
ended without a winner, orphan calls, drift CUSUM alerts, DID degraded and handled, supervisor edit
staled one of our proposals.

**Never alert:** single-call anomalies, routine proposal submissions, auto-applied schedule refreshes.

### 9.3 Supervisor view (C11, partial)

`GET /supervisor/{tenant_id}` (HTML) and `/api/v1/tenants/{tenant_id}/overview` (JSON), one screen:

1. **Needs you:** pending approvals with change summary, impact score, affected leads, expiry time.
2. **Changed today:** every config version today (ours approved, ours auto-applied, supervisor) with
   summary and who.
3. **Being tested:** experiment arms with n, rate, 95% CI, decision ETA.
4. **Winning:** promoted variants with current lift versus holdout.
5. **Incidents and caller-ID health:** open and resolved incidents, DID table with status and ratio.
6. **Where your default does not reach:** cohorts whose own allocations override the default.

Compliance endpoints: `/api/v1/workflows/{wf}/leads/{lead_id}/explain` (per-call variant, config
version, cohort, the proposal and approval that made that config live, and the rationale) and
`/api/v1/tenants/{t}/weekly-summary` (all changes including auto-applies, with reasons).

**The review question** ("why did workflow X change at T, who approved it, what happened next") is one
query:

```sql
WITH RECURSIVE v AS (                         -- the version that went live at or just before :t
  SELECT cv.* FROM config_version cv JOIN workflow w USING (wf_sk)
  WHERE w.workflow_id = :wf AND cv.applied_minute <= :t
  ORDER BY cv.applied_minute DESC LIMIT 1),
chain AS (                                   -- the proposal's audit rows plus everything they caused
  SELECT a.* FROM audit_log a, v
  WHERE a.subject_type = 'proposal' AND a.subject_id = v.proposal_id
  UNION
  SELECT a.* FROM audit_log a JOIN chain c ON a.caused_by = c.audit_id)
SELECT c.sim_minute, c.actor, c.action, c.summary,
       c.details->>'approval_id' AS approval_id, c.details->'evidence' AS evidence
FROM chain c ORDER BY c.audit_id;
-- a supervisor version has proposal_id NULL; the CLI then starts the chain from the
-- supervisor-edit audit row (subject_type = 'config_version') and shows our reaction to it
```

It returns the detection or look that created the intent, the draft with its diff and dry run, the submit,
the approval decision (approver identity is not exposed by the API, only `approval_id` and `decided_at`;
stated as a gap), the apply, and the verification outcome. A CLI wrapper `python -m controller.audit why
--workflow X --at "D3 14:00"` prints it.

---

## 10. Testing strategy and crash-recovery demonstration

End-to-end first: tests start the real simulator as a subprocess (allowed for tests, never for controller
code) and drive the real controller over HTTP against a real Postgres. Tests may read the simulator's run
directory and run the grader; the controller never does.

| Layer | Test | Asserts |
|---|---|---|
| E2E | `test_public01_full_run` | runs to finish; grader: `auto_apply_rejected = 0`, `lapsed = 0`, `apply_conflicts = 0`, `expired = 0`; every config version in the simulator's history has a matching `proposal` and audit chain in our store |
| E2E | `test_public02_incidents` | flagged-DID share from D3 onward < 5%; MTTR finite and under 8 h; supervisor voice edit is never reverted without an approval; credits above reserve |
| E2E | `test_ingest_counts_public03_day` | after D0, `count(call_fact) + count(lead_dim new) + ...` equals `state.last_event_id` minus history; rollup sums equal a from-scratch recomputation over `call_fact` |
| E2E | `test_determinism` | two clean public_01 runs produce identical proposal and config histories |
| Crash | `test_crash_point[point]` (parameterised) | see below |
| Contract | `test_sim_contract` | the simulator behaviours we rely on: lazy lapse, proposal id consumed on POST, auto-apply id consumed only on success, decision visible after the tick, chunk listing during idle. If graders' copy differs, this fails first |
| Unit | stats | z-tests, sample-size formula, boundaries, Poisson tail, against hand-computed values from this doc |
| Unit | policy | `changed_dims` and `max_shift` on the edge cases (cohort order, normalised weights, `lead_preferred_hour`) |
| Unit | planner | `next_stop` and the no-expiry rule as pure functions over synthetic clocks and policies |
| Unit | transform | hand-written NDJSON fixture (not copied from `runs/`) through the SQL transform |
| Load | `scripts/load_test.sh` | public_03: per-day wall, events/s, peak RSS (`docker stats`), DB size; writes `reports/public_03_load.md` |

**Fault injection.** The controller reads `CTL_FAULT=<point>:<nth>` and calls `os._exit(137)` (same
effect as kill -9) the nth time it reaches the named point:

`after_advance_intent`, `after_advance_response`, `mid_chunk_copy`, `before_transform_commit`,
`after_submit_intent`, `after_submit_response`, `after_apply_intent`, `after_apply_response`,
`after_auto_apply_intent`, `after_auto_apply_response`, `after_validation_intent`, `mid_audit_write`.

Each parameterised test runs public_01 once with the fault, restarts the controller (as `run.sh` would),
runs to the end, and asserts **crash equivalence** with a clean run: identical simulator config history,
identical approval list (ids, statuses), identical `proposal` table. This holds because the simulator is
deterministic given the same request sequence (approval outcomes are seeded by `proposal_id`, dial
randomness by call order) and the controller is deterministic given the same database state. The two
validation points cannot be equivalent (an unknowable run may be repeated), so they assert the invariants
only: no double apply, no lost approval, no lapse, credits within one run of the clean path.

**Random kill demonstration.** `scripts/kill_test.sh public_02`: runs `run.sh`, and from a second shell
`docker kill --signal KILL` the controller container at random wall intervals of 10 to 60 s until the
scenario ends (the `run.sh` restart loop brings it back). It then runs the grader and prints a log for
`reports/crash_recovery.md`: kill times, the open intents found at each restart and how each was
reconciled, and the final invariants (zero lapses, zero conflicts, zero duplicate applies, ingested event
count equals `last_event_id`). One kill run on public_03 shows recovery mid-COPY at load.

---

## 11. Module breakdown

```
controller/
  __main__.py            entry: env config, lock, run resolution, reconcile, loop
  settings.py            env-driven settings and tunables (no scenario values)
  model.py               frozen dataclasses shared by all modules (the M0 contract)
  simclient/
    http.py              httpx client, timeouts, retry policy, error classification
    api.py               typed wrappers for every endpoint
  store/
    db.py                connection pool, tx(), advisory lock, schema-per-run, migrations runner
    migrations/*.sql     DDL from section 3
    repos.py             small typed read/write helpers per table group
  ingest/
    loader.py            stream chunk -> COPY stage_raw (thread)
    transform.sql        set-based transform and rollups
    pipeline.py          ordering, exactly-once, gap filling, metrics
    catalog.py           /workflows specs, live config bodies, config_cohort resolution
  clock/
    planner.py           next_stop() pure function
    advance.py           advance with intent and reconcile
  cohorts/
    cells.py             cell catalog
    cohort.py            canonical where, cohort_key, effective membership, partition choice
  stats/
    proportions.py       z-tests, CIs, sample size, Poisson tail
    boundaries.py        Haybittle-Peto, Bonferroni bookkeeping
  snapshot.py            WorkflowSnapshot from rollups (numpy)
  learn/
    calltime.py          hour weights
    callerid.py          DID spread and re-admission
    experiments.py       launch, looks, elimination, promotion
    decay.py             holdout monitor, rollback intents
    guardrail.py         pre-promotion and post-apply checks
  incidents/
    detectors.py         DID, hallucination, drift
    response.py          fix selection, validation, verification
    credits.py           reserve and tenant envelopes
  governance/
    policy.py            changed_dims, max_shift, auto-apply check (replica of the contract)
    batcher.py           merge intents, rebase on live config
    timing.py            slots, no-expiry rule, fatigue
    evidence.py          impact score, affected leads, dry run
    lifecycle.py         state machine, submit, apply, preflight
  reconcile.py           startup reconciliation of intents
  audit/
    log.py               record(); used inside callers' transactions
    queries.py           why-did-X-change, explain-lead, weekly summary
  obs/
    metrics.py           loop_step rows, RSS, DB size
    alerts.py            rules from 9.2
  api/
    app.py               FastAPI read-only views
tests/e2e, tests/unit, scripts/ (load_test.sh, kill_test.sh), compose.yml, compose.linux.yml,
compose.desktop.yml, Dockerfile, run.sh
```

**Public interfaces (the contracts teams build against):**

```python
# model.py
@dataclass(frozen=True)
class ChangeIntent:
    wf: str; source: str              # 'calltime' | 'callerid' | 'experiment' | 'decay' | 'incident' | ...
    priority: int                     # 0 routine, 1 rollback, 2 incident
    patch: ConfigPatch                # dims scoped to cohort ids (or 'default')
    cohorts_needed: tuple[CohortDef, ...]
    reason: str; evidence: dict       # goes verbatim into rationale / audit

@dataclass(frozen=True)
class WorkflowSnapshot:
    spec: WorkflowSpec; live: LiveConfig; cohorts: dict[str, frozenset[int]]
    engage: EngageArrays; pickup: PickupArrays; did: DidArrays; health: HealthSeries
    experiments: tuple[Experiment, ...]; did_state: dict[str, DidState]
    in_flight: Proposal | None; fatigue_24h: int; now: int

# simclient/api.py
class SimClient:
    def state(self) -> SimState: ...
    def advance(self, minutes: int) -> AdvanceResult: ...          # no internal retry
    def list_chunks(self, kind: str, after: int) -> list[ChunkMeta]: ...
    def stream_chunk(self, n: int) -> Iterator[bytes]: ...
    def workflows(self) -> list[WorkflowSpec]: ...
    def config(self, wf: str) -> LiveConfig: ...
    def config_history(self, wf: str) -> list[ConfigHistoryRow]: ...
    def request_approval(self, req: ApprovalRequest) -> ApprovalView: ...
    def approvals(self, wf: str | None = None, status: str | None = None) -> list[ApprovalView]: ...
    def apply(self, wf: str, approval_id: str) -> ApplyResult: ...
    def auto_apply(self, wf: str, req: AutoApplyRequest) -> ApplyResult: ...
    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun: ...
    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]: ...

# ingest/pipeline.py
class Ingestor:
    def ingest(self, chunks: list[ChunkMeta]) -> IngestReport: ...  # exactly-once, in order

# clock/planner.py
def next_stop(now: int, ctx: PlannerContext) -> int: ...            # pure

# learn/*.py and incidents/detectors.py: pure functions of a snapshot
def propose(ws: WorkflowSnapshot) -> list[ChangeIntent]: ...
def detect(ws: WorkflowSnapshot) -> list[Finding]: ...

# governance
class PolicyChecker:
    def changed_dims(self, old: Config, new: Config) -> list[str]: ...
    def max_shift(self, old: Config, new: Config) -> float: ...
    def auto_apply_ok(self, spec: WorkflowSpec, old: Config, new: Config) -> tuple[bool, list[str]]: ...
class Batcher:
    def build(self, ws: WorkflowSnapshot, intents: list[ChangeIntent]) -> ProposalDraft | None: ...
class Timing:
    def submit_allowed(self, ws: WorkflowSnapshot, priority: int) -> tuple[bool, int | None]: ...  # (now?, next ok minute)
class Lifecycle:
    def submit(self, draft: ProposalDraft) -> None: ...
    def apply_due(self) -> None: ...
    def on_config_changed(self, wf: str, version: int, actor: str) -> None: ...

# audit/log.py
def record(tx, *, wf: str | None, actor: str, action: str, subject: tuple[str, str],
           summary: str, details: dict, caused_by: int | None = None) -> int: ...
```

**Dependency order and parallel streams**

```
M0  model.py + migrations + settings                     (1 engineer, first, short)
     |
     +--> Stream A (platform): simclient -> store -> ingest -> clock -> reconcile -> __main__ ->
     |                         compose/run.sh -> fault injection -> kill_test, load_test
     +--> Stream B (learning, pure): cohorts -> stats -> snapshot (against fixture DB) ->
     |                         learn/* -> incidents/detectors (tested on synthetic snapshots)
     +--> Stream C (governance): policy -> timing -> evidence -> batcher -> lifecycle
     +--> Stream D (surface): audit/log -> audit/queries -> obs -> api
M1  ingest + load test on public_03 (the main measured risk; done before learning lands)
M2  loop with no-op decisions + crash harness green
M3  governance + DID + call time; public_01 e2e green
M4  experiments + decay + incidents; public_02 e2e green
M5  ops/supervisor views, reports, SUBMISSION
```

Streams B and C depend only on `model.py` and fixtures, so they proceed while A builds the plumbing.

### 11.1 run.sh and Docker Compose

```bash
#!/usr/bin/env bash
# Requires Docker with Compose v2. Starts Postgres and the controller in containers and runs until the
# scenario at SIM_URL finishes. Restarts the controller after a crash; state lives in Postgres.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-formi_ctl}"
case "$(uname -s)" in
  Linux)  FILES="-f compose.yml -f compose.linux.yml";   CTL_SIM_URL="$SIM_URL" ;;   # host network
  *)      FILES="-f compose.yml -f compose.desktop.yml"
          CTL_SIM_URL="$(printf '%s' "$SIM_URL" | sed -E 's#//(127\.0\.0\.1|localhost)#//host.docker.internal#')" ;;
esac
export CTL_SIM_URL
docker compose $FILES build --quiet
docker compose $FILES up -d --wait db               # healthcheck: pg_isready
restarts=0
while true; do
  set +e; docker compose $FILES run --rm controller; code=$?; set -e
  [ "$code" -eq 0 ] && break                       # scenario finished
  [ "$code" -eq 3 ] && { echo "fatal configuration error, not restarting" >&2; exit 3; }
  restarts=$((restarts + 1))
  [ "$restarts" -ge "${CTL_MAX_RESTARTS:-20}" ] && { echo "controller failed $restarts times" >&2; exit "$code"; }
  sleep 2
done
docker compose $FILES up -d api                    # leave the read-only views up for inspection
```

- **Reaching the simulator.** The simulator binds 127.0.0.1 by default, which a bridge-networked container
  on Linux cannot reach through `host.docker.internal` (that resolves to the docker0 gateway, not
  loopback). So on Linux the controller uses `network_mode: host` and talks to `SIM_URL` unchanged; Postgres
  stays on the bridge and publishes `127.0.0.1:55432`. On Docker Desktop (macOS, Windows) the controller
  stays on the bridge and `host.docker.internal` does reach host loopback, so `run.sh` rewrites the URL.
  A remote `SIM_URL` works in both modes.
- **Health polling.** The controller polls `GET {SIM_URL}/health` every 2 s for up to `CTL_SIM_WAIT`
  (default 900 s, because the simulator generates history before it listens: about 35 s for public_03 on a
  laptop, possibly minutes on the grading box), then `GET /sim/state`. If it never answers: exit 3.
- **Resources.** `db` mem_limit 3 GB, `controller` 1 GB, `api` 256 MB; leaves about 3.5 GB for the
  simulator and the OS.
- **Data volume.** Named volume `pgdata`, never wiped by `run.sh`; runs are separated by schema (section 2).
- **No-Docker fallback.** `CTL_NO_DOCKER=1 PG_DSN=postgresql://... ./run.sh <SIM_URL>` runs the controller
  from a local venv against an existing Postgres 16, for a box where images cannot be built.

---

## 12. Stakeholder positions (brief)

| Memo | Accept | Push back | Wrong claim |
|---|---|---|---|
| 01 Sales | Per-workflow lift numbers on the supervisor view, each KPI separately | Blended score; "winning by lunch, live by evening" (a morning has a few hundred calls, our boundary needs days at dev volumes); P4 everywhere (we only ship prompts that pass our stricter internal gate) | Kestrel "auto-apply for everything" (only schedule, caller_id, welcome, voice, max_shift 0.2; never prompt or cohorts). Northwind KYC auto-apply (disabled by policy) |
| 02 Northwind Compliance | No customer-facing auto-apply even where policy allows; validation evidence on every prompt change; per-call explanation endpoint; weekly summary including auto-applies | "Named human approval": the API exposes only `approval_id` and time, so we record that and ask Formi to expose approver identity | "Timing and caller-ID rotation are covered by policy" is true for renewal, not KYC (auto-apply disabled there) |
| 03 SRE | Kill-safe and idempotent (section 8, 10); Postgres only, no Redis or new stateful infra; actionable pages only; loop health without logs (`/ops`); 10x analysis | None | None found |
| 04 Kestrel Ops | At most 2 routine requests a day, never before approvers open + 60 min; batched; never undo her prompt edits silently; one screen | We will still propose reverting an unsafe prompt edit, through an approval with evidence; we will not suppress an incident fix to stay under 2/day (one slot is reserved for it) | No false claim, but a blind spot: her own mid-day prompt edits are exactly the incident class we must catch. The word-for-word "fix" she fears is a catalog option the LLM can suggest; our fix rule prefers the last known good prompt (observed engagement) over any unobserved candidate |
| 05 Product | Separate KPIs; one cohort object with versioned ids; expired = rejection, re-queued from fresh data; only observed calls count as evidence; impact score with a defined meaning | Per-person call time as P0 (cut: data too thin; per-cohort weights ship); supervisor view as P0 (partial) | "Minimum cohort size is open": we pick one and state its cost (5.1) |
| 06 CTO | Design for the envelope (sections 3.4, 4.3 and 12.1); spend credits only where they change a decision (about 6 per incident); say what we cut | "Credits are cheap here" is relative: 200 to 400 per scenario and one internal-gate run costs 10 | Confirmed: several memo claims contradict the enforced policy |

### 12.1 What breaks first at 10x and how to partition

| Order | What breaks | At 1x (public_03) | At 10x (8.4M events/day) | Fix |
|---|---|---|---|---|
| 1 | Lead join and fact indexes outgrow RAM | `lead_dim` 2.3M rows, hot in cache | 23M leads, about 3.8 GB with index; random I/O in the per-chunk join | Partition `lead_dim` by campaign day; join only campaigns within cadence days (about 3 days of leads); drop `call_fact` partitions after N days (compliance archive elsewhere) |
| 2 | Serial SQL transform | est. 15 to 25 s/day | est. 150 to 250 s/day plus simulator time | P-way parallel transform: the loader splits lines by `hash(workflow_id) % P` into P staging partitions (cheap byte search, no JSON parse), P backends transform concurrently, chunk complete when all `(chunk_no, part)` markers exist |
| 3 | Serial per-workflow decide | 12 wf x 14 steps x about 75 ms = 13 s/day | 120 wf: about 2 min/day | Process pool by `hash(tenant) % W`; snapshots are pure reads, intents written per workflow, governance per workflow already isolated by `one_in_flight` |
| 4 | Disk | about 1.4 GB DB + 4.1 GB sim chunks | about 14 GB DB | Retention, partition drops, bigger volume |
| 5 | Credits and approver attention | per-workflow caps suffice | many tenants share ops time | C15 proper: per-tenant budgets and a fair queue for approval slots |

In production there is no `advance`: the clock is real and events arrive as a stream. The same design
partitions by tenant: each worker owns a set of tenants via a lease row (advisory lock per partition),
consumes that partition's event stream into its own staging, and runs detect/decide/govern only for its
workflows. Audit and governance tables are already keyed by workflow, so nothing is shared across
partitions except the read-only views.

---

## 13. Top risks

| Risk | Impact | Mitigation |
|---|---|---|
| Postgres-side JSON transform slower than estimated | Misses the SRE number | M1 measures it before any learning code lands. The Python-parse path sits behind the same `Ingestor` interface as a fallback, and the P-way split is designed already |
| Docker networking or image build fails on the grading box | Floor failure | Host network on Linux, `host.docker.internal` rewrite on Desktop, no-Docker fallback with `PG_DSN`, health polling with long timeout, exit code 3 for fatal configuration so the restart loop does not spin |
| Underpowered experiments at dev volume | Little engagement lift | Factorial design, a up to 0.15, harm elimination; MDE table published; pickup levers (DID, call time) carry dev-scale outcomes |
| Exploration arms graded harmful at apply | Hygiene score | Arm shares bounded at 0.10 to 0.15; daily elimination; no exploration on pickup-primary workflows; accepted and stated |
| Approval lapses or our own changes stale our approvals | Governance hygiene | Step rule `apply_window - tick`; apply before submit; `one_in_flight` unique index; preflight before every apply |
| Crash between validation POST and record | Up to one lost run (3 to 10 credits) per crash | Intent marked unknown; reserve covers it; documented |
| Simulator restarted under us | Corrupted state | World fingerprint plus consistency checks; new schema per world |
| Supervisor edits lost between steps | Wrong base for rebase | Fetch body every step a change is seen; `body_source='unknown'` when an intermediate body is missed; rebase always on the live body |
| Heterogeneity on features outside the chosen partition | Missed cohort-specific wins, possible hidden harm in a sub-cohort | Cohort-level harm exclusion on the partition; stated as a known weakness; cohort discovery is the first item in "cost to finish" |
| Guardrail ambiguity | Rubric disagreement | Stated interpretation (relative, stricter), enforced pre-promotion and post-apply |
| Tuning to public truth by accident | Hidden-scenario failure | Every parameter comes from the API or a formula in this doc; public truth appears only in the 7.5 timeline; contract tests catch simulator behaviour changes |
