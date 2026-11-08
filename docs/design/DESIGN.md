# Final design: Formi outcome-first controller

Status: final design for implementation (synthesis of proposals A-outcomes, B-governance and C-scale,
after two judge reviews). Stack fixed by ADR 0001: Python 3.11, Postgres 16, httpx, numpy,
psycopg 3, FastAPI, Docker Compose, no LLM framework.

**Lineage.** Base: proposal A (the learning loop: a harm gate that mirrors the grader, a factorial
ridge-logit effect model with arm x feature interactions, seeded probability matching, holdout and
Page CUSUM). Grafted from B: exact apply-window maths, submit-window search with expiry as a hard
precondition, SUPERSEDED and priority classes, the directives layer, the overlay and path-ownership
model with a supervisor cooldown, the stricter bar for never-live prompts, policy fuzzing, the
differential policy test and the crash-equivalence matrix. Grafted from C: Postgres-side ELT
ingest, the `one_in_flight` partial unique index, the advisory lock, schema-per-run with resume
consistency checks, the Linux-safe `run.sh`, the wall budget and 10x table, the multi-window DID
detector and contract tests. Fixes for every fatal flaw either judge raised are listed in
section 0.3 with the section that implements each. Revision 3 applies a 38-item critique: section
0.5 maps each item to the section that applies it, and section 16 lists what was not applied and why.

**Notation.** `[T]` marks a threshold that may be tuned **only** on private scenario variants
written by our test harness, never on public-scenario truth. Every other number is either read from
the API at runtime, derived by a formula stated here, or a grader rule (the 1% engagement and 2%
caller-ID harm lines). Public truth (UNDERSTANDING.md section 7) appears only in rows labelled
"sanity check".

---

## 0. Design in one page

### 0.1 The one idea

The grader judges every controller change at apply time, against the config it replaced, over the
active-lead population: harmful if expected engagement falls by more than 1% or expected caller-ID
health falls by more than 2% (grader.py:67-73, world.py:634-655). So every change the controller
makes passes one computation that mirrors that judgement, from our own posterior:

```
harm gate (engagement, population):  P( de(new vs current) < -1% | data ) <= alpha_H
harm gate (engagement, per cohort):  P( de_k(new vs current) < -x_k | data ) <= alpha_H   for every cohort k
harm gate (caller ID):               P( dd(new vs current) < -2% | data ) <= alpha_D
  de   = E_new / E_cur - 1   expected engagement over OUR tracked active leads
  de_k = the same ratio over the active leads of cohort k
  dd   = H_new / H_cur - 1   expected relative caller-ID pickup over the same leads
alpha_H = 2.5% for exploration, ramps and promotions [T]; 10% for rollbacks [T]
alpha_D = 5% [T]
x_k     = 2% by default [T]; 1% where a client directive asks for it (Northwind, memo 02 item 4)
```

The population line mirrors the grader. The per-cohort line is ours: the grader judges the
population only, but Compliance ("worse for a cohort of our customers, even briefly") and the CTO
("an improvement that hurts a cohort is worth less than nothing") judge cohorts. Uniform 3.8% arms
can push one cohort below -1% while the population stays neutral. Its cost is computed in 5.4: at
`x_k = 2%` it does not shrink the first exploration (3.8% per arm), at `x_k = 1%` it halves it (1.9%).

Exploration size, ramps, promotions, rollbacks, DID probes, DID drains and supervisor rebases are
all this one computation with a different proposed allocation. Posteriors used by the gate are
**recency-weighted** (section 5.5), so a variant that peaked and is now decaying cannot be ramped or
promoted on stale evidence.

**One stated exemption: P0 prompt fixes.** A hallucination fix is applied even when it lowers
expected engagement (section 7.1, "stated tension"), because an unsafe prompt is a compliance
failure. Its gate is the safety verdict plus the guardrail check, not the engagement harm gate. Every
other change, including DID drains, passes this section's gate.

### 0.2 What the controller does, in order of value

1. Ingests every event exactly once into Postgres (ELT via COPY) and keeps per-cell rollups.
2. Caller-ID health: probes unknown DIDs at a harm-safe share sized from volume (at most 2.8%),
   classifies them with a dial-count SPRT, spreads load over healthy DIDs, detects a flagged DID
   from concurrent pickup ratios and drains it within about 2 hours at dev volume. This is the
   largest pickup lever. A comparator-free KPI drift CUSUM covers single-DID workflows and
   everything else that drifts.
3. Per-cohort call-time weights from history (pickup lever, graded neutral, cheap).
4. Factorial welcome x voice learning on engagement-primary workflows, with cohort-specific effects
   learned through interaction terms, a population and per-cohort harm gate, probability-matching
   allocation, a deterministic winner rule with a per-scope threshold and a computed false-winner
   bound, a holdout and decay rollback.
5. Prompt safety: a two-stage validation verdict on every prompt change and on business defaults,
   kept separate from the client-gate evidence run, a live flag CUSUM backstop, fix by
   last-known-good first, then catalog candidates in a fixed statistical rank.
6. Governance: one change in flight per workflow (a database invariant), batching, submit times
   chosen by an expiry-probability search over the whole day, apply before `apply_by` guaranteed
   by the step rule, rebase on supervisor edits, never a 403, lapse, expiry or apply conflict.
7. Audit: every decision, including decisions not to act, is committed before or atomically with
   its action; "why did X change" and "why did this customer hear this" are single queries.

### 0.3 Judge findings and where each is fixed

| # | Finding (judge) | Fix | Section |
|---|---|---|---|
| F1 | Decaying winner ramped or promoted on the stationary posterior (J2, all proposals) | Gates use an exponentially weighted posterior; promotion also needs a recency check and no decline alarm; CUSUM armed from 10% share | 5.5, 5.6, 5.8 |
| F2 | Fatigue cap 1 leaves zero routine slots (J2) | `reserve = 1 if C_eff >= 2 else 0`; at `C_eff = 1` routine at most once per 48h | 6.4 |
| F3 | No submit window for TTL 12h with latency 480, or approvers 14-16 with TTL 4h (J2, all) | Search every tick of the day, including before approvers open, with `P(expire) <= 1%` from the uniform latency | 6.3 |
| F4 | DID drain stales a pending incident approval (J2, A and B); C's index blocked the drain | Priority classes with a same-class P0 order (prompt safety before DID drain); a pending P0 approval is never staled or superseded by a drain; short drain chains finish first, long ones are bundled into the prompt-fix approval | 6.2, 7.3 |
| F5 | Incumbent prompt "validated" by one pass at the client gate (J2, B and C) | Two-stage verdict (n = 300, then pooled n = 1,300 at a 2% failure bar); a single gate pass is never cached as safe | 7.1 |
| F6 | CUSUM delay misstated (J2, A) | Redesigned for a doubling (p1 = 2 p0); computed delays: about 460 connected calls for a 3.6x jump, about 1,730 for a 2x jump | 7.1 |
| F7 | Unsafe business-default prompt is invisible to relative detectors (J2) | D0 safety screen of every default prompt plus a free cross-workflow flag-rate comparison | 7.1 |
| F8 | `max_shift` 0.05 drains take 7 hours (J2, A) | Risk-reducing P0 and P1 changes chain one `max_shift` step per tick; chains are bounded by the share being removed | 6.5, 7.3 |
| F9 | World fingerprint collides on re-run or simulator restart (J2, A and B) | Schema per run, resumed only after consistency checks against the live simulator | 8.5 |
| F10 | `run.sh` unspecified for Linux loopback (J2) | Host networking on Linux, URL rewrite on Desktop, restart loop, exit code 3, no-Docker fallback | 11.3 |
| F11 | `expires_in_hours` at the minimum leaves zero margin (J1) | Left at the policy TTL; approvals decided more than 20h after submit are re-checked before apply | 6.3 |
| F12 | Step-rule comment wrong (J1) | `S_watch_max = tick * floor((W - tick + 1) / tick)`, derived and stated with its binding cases | 4.2 |
| F13 | "Null ever" false-winner rate not computed (J1) | Information-doubling looks, unequal arm and control sizes, a W1 threshold per scope (0.995 population, 0.998 cohort), and a union bound from the effective boundaries: at most 0.54% (population) and 0.47% (cohort) per null hypothesis over 4 looks | 5.5 |
| F14 | Complexity risk of SNIPS replay (J1) | SNIPS cut; dry run is a stratified posterior replay | 6.6 |
| F15 | nort_cancel_2 policy-vs-memo conflict not flagged (J1) | Directive forbids it; supervisor view and Compliance reply flag the conflict | 6.5, 12 |
| F16 | No paste-ready replies (J1, all) | Six replies of 120 words or fewer | 12.2 |
| F17 | Wall-time fact taken from `runs/` mtimes (J1, C) | Replaced by `docs/measurements/simulator-baseline.md` (simulator run by the harness) and re-measured over HTTP in M1 | 8.7, 15 |

### 0.4 TL;DR (draft of SUBMISSION section 0; results filled in after M7)

1. A Python controller with Postgres as its only state, started by `./run.sh`, that ingests every
   event exactly once and passes every change through one harm gate that mirrors the grader
   (population) and protects cohorts (our stricter line).
2. Pickup comes from caller-ID health (SPRT probes, spread, a 2-hour flagged-DID drain) and
   per-cohort call times; engagement from harm-gated factorial welcome x voice learning with a
   holdout and decay rollback; hallucination incidents are fixed through an evidence-backed approval.
3. Governance never produces a 403, lapse, expiry or apply conflict by construction (one change in
   flight, exact expiry maths, step bound), and every decision is replayable from the audit store.
4. Results on public_01 and public_02: to be pasted from `reports/` (targets: 0 harmful, 0
   governance violations, hallucination MTTR about 2 hours, about 23 credits).
5. Not built: per-person call time, prompt A/B outside incidents, regrid, SNIPS replay, an HTML
   supervisor page, cross-tenant approver fairness.

### 0.5 Critique round 3: where each item is applied

| Item | Topic | Applied in |
|---|---|---|
| 1 | False-winner rate under unequal sizes and cohort scope | 5.5, 10.1, ADR 0005 |
| 2 | Minimum-gain rule blocked exploration | 5.4, 5.11 |
| 3 | Per-cohort harm gate | 0.1, 5.4, 11.2 |
| 4 | Drain chain vs prompt fix, same-class P0 order | 6.2, 7.3, ADR 0013 |
| 5 | Flagged-DID day share formula and test bounds | 7.3, 10.1, 15.1 |
| 6 | DID classification sample sizes, SPRT | 5.9 |
| 7 | KPI drift detector, slow path, no-healthy-DID behaviour | 7.5, 5.9 |
| 8 | Deterministic fix selection; P0 exemption | 7.1, 0.1 (partly, see 16) |
| 9 | Verdict vs client-gate evidence, g >= 0.99 | 7.1, 13.4 |
| 10 | MTTR formula; P0 rejection resubmit | 7.1, 6.1 |
| 11 | Scope and honest hours | 1, 15 |
| 12 | Directive scope per workflow | 6.5, 13.3 |
| 13 | public_02 timeline | 7.4 |
| 14 | Real ramp for looks and cohort minimum | 5.2, 5.5 |
| 15 | Credit pools and low-credit mode | 7.2, 13.1, ADR 0021 |
| 16 | Guardrail composition effects | 5.10, 6.6 (partly, see 16) |
| 17 | why-heard SQL | 3.2, 9.4 |
| 18 | Schema gaps | 3.2 |
| 19 | SUBMISSION coverage | 0.4, 12.3, 13.4, 13.5, 15.4 |
| 20 | Live review: constraint table, decision replay | 9.6, 9.7 |
| 21 | Threshold register and undefined terms | 5.4, 5.8, 5.11 |
| 22 | Risk-reducing definition and chaining | 6.5, 13.3 |
| 23 | Interfaces | 11.2 |
| 24 | VERIFYING for allocation changes | 6.1 |
| 25 | Auto-release of held patches | 6.1 |
| 26 | Business configs with mixed variants or cohorts | 5.2, 5.3 |
| 27 | Crash-equivalence claim | 8.6, 10.2 |
| 28 | Impact score units per lever | 6.6 |
| 29 | Missing assumptions | 13.3 |
| 30 | Advisory lock key | 2 |
| 31 | API write role | 2, 9.3 |
| 32 | One pickup rate in detection maths | 7.3 |
| 33 | Prompt A/B days; Sales figure | 1, 12.2 |
| 34 | Runbooks and local-run doc | 9.5, 15.1 |
| 35 | Fixed-minute slots enforced in code | 6.3 |
| 36 | Overnight-jump assertion | 4.2 |
| 37 | Parameter count and fit time | 5.3, 8.7 |
| 38 | Cross-workflow verdict pooling | 12.3 (rejected alternative) |

---

## 1. Scope

Ranked by what the grade and the hard constraints need first. Hours are human-equivalent build
plus tests, taken from the milestones in section 15 (a milestone's hours are split over the
capabilities it builds). README section 6 asks for "five done properly": the five are rows 1 to 5.
Rows 6 to 8 are the floor and the SRE non-negotiables, which every other row stands on. Rows 9 and
10 are small because rows 1 to 5 cannot work without them. Everything from row 11 down is
deliberately minimal or cut.

| Rank | # | Capability | Status | Hours | Why it earns its place | Cost to finish |
|---|---|---|---|---|---|---|
| 1 | C9 | Governance client | **Built** | 2.2 | Every outcome passes through it, and governance hygiene is graded on hidden policies: state machine, priority classes, submit-window search, fatigue reserve, apply guarantee, rebase, local policy replica. | - |
| 2 | C5 | Caller-ID health (with the KPI drift detector) | **Built** | 1.4 | Largest pickup lever (about +38% on a single-DID KYC-like workflow, sanity check), auto-applicable on most policies, and an incident class with no event. | - |
| 3 | C8 | Hallucination incident response | **Built** | 1.4 | Graded on time to fix; a compliance failure if missed. Two-stage verdict separate from gate evidence, live flag CUSUM, D0 default screen, last-known-good fix, ranked catalog fallback, post-apply verification. | - |
| 4 | C6 | Welcome x voice experimentation | **Built** | 1.3 | The only engagement lever with per-cohort targeting. Factorial ridge-logit with arm x feature interactions, population and per-cohort harm gate, probability matching, per-scope winner rule. | - |
| 5 | C7 | Promotion, decay, rollback | **Built** | 0.9 | Decay rollback is graded; holdout, Page CUSUM, recency-weighted gates, rollback timed to the predicted apply time. | - |
| 6 | C1 | Event ingestion at load scale | **Built** | 2.5 | Floor and base of every decision; SRE's 10-minute bar. COPY plus a set-based SQL transform, exactly-once by chunk number, Python fallback. | - |
| 7 | C13 | Crash recovery, demonstrated | **Built** | 1.2 | SRE non-negotiable. Write-ahead intents, fault injection at every side-effect boundary, crash-equivalence matrix, random kill loop. | - |
| 8 | C12 | Audit store | **Built** | 0.6 | Floor, and the live-review question. Includes `replay-decision`. | - |
| 9 | C2 | One shared cohort object | **Built (small)** | 0.3 | Rows 2 to 5 and the view address the same versioned cohort ids; without it nothing is explainable. | - |
| 10 | C3 | Bootstrap from history | **Built (narrow)** | 0.3 | Baselines only: hour curves, cell mix, per-DID pickup, flag-rate baseline, active leads, and variant data where the business default already mixed variants. | - |
| 11 | C4 | Call-time optimisation | **Partial** | 0.3 | Per-cohort `hour_weights` built (pickup lever, graded neutral, cheap). Per-person `lead_preferred_hour` **cut**: about 2 to 3 dials per person per campaign, and the x0.25 penalty outside the preferred hour makes a wrong guess costly. | About 1.5 days: Beta-binomial per returning person on hour bands, cap management, A/B on returning persons. |
| 12 | C10 | Impact score, affected leads, dry run | **Partial (minimal)** | 0.3 | Impact score labelled with the lever's KPI, integer affected leads, stratified posterior replay. SNIPS **cut**. | Half a day for SNIPS with variance control. |
| 13 | C14 | Loop observability | **Partial** | 0.2 | `loop_step` table, `/ops` JSON, alert rules with runbooks. No exporter. | Half a day for a Prometheus exporter. |
| 14 | C11 | Supervisor view | **Partial** | 0.2 | JSON API per tenant and the holds endpoint. HTML page **cut**. | About 1 day for an HTML page, auth and an email digest. |
| 15 | C15 | Fair allocation across tenants | **Partial (minimal)** | 0.1 | Ordered credit pools with per-tenant screen envelopes (7.2). Approver attention is per workflow in this API, so there is nothing to share. | About 1 day once approvers are shared across workflows. |
| - | - | Contracts, run.sh and compose, loop orchestrator, docs and reports | - | 2.8 | M0 contracts (1.0), run.sh and compose (0.5), orchestrator and step rule (0.8), RUNNING.md, RUNBOOK.md, reports, SUBMISSION (0.5). | - |
| - | - | Prompt experimentation outside incidents | **Cut** | 0 | One prompt per workflow means no concurrent control. A +3% effect at e = 0.55 needs about 14,400 connected calls per arm (alpha 0.05, power 0.8); a switchback runs both arms in turn, about 28,800 connected calls, about 30 days of a dev workflow at 950 a day. | Switchback design, 2 days, low value. |
| - | - | Interaction LR test, regrid, policy fuzzing beyond the J2 cases | **Cut** | 0 | Each refines a built capability rather than adding one; their failure modes are stated instead (A-1, risk 7, 13.4). | About 1 day together. |
| - | - | Cross-workflow promotion of winners | **Cut** | 0 | Product: later. | - |
| | | **Total** | | **16.0** | Top of the 12 to 16 hour budget. | |

**Selection principle.** (1) What the floor and the hard constraints need, built to production
grade: C1, C9, C12, C13. (2) Levers in order of expected outcome per unit of evidence at the
**smallest** volume we must handle (500 to 1,200 connected calls per workflow-day): C5 and per-cohort
C4 (pickup, measurable in hours), C8 (graded on time to fix), C6 and C7 (need days, so they get the
most careful statistics). (3) What makes those trustworthy to a supervisor, in minimal form: C2,
C10, C11. Cut what cannot reach significance inside a scenario (prompt A/B, per-person timing), what
refines rather than adds a capability (LR test, regrid, fuzzing beyond the attack cases), and what
the API offers no lever for (cross-tenant approver fairness).

**Effort, honestly.** 16.0 human-equivalent hours of agent-built code plus tests, at the top of the
budget. The items most likely to overrun are the ridge-logit allocator (M5) and the SQL transform
(M1); each has a stated fallback (13.1), and section 15.3 gives the cut order if the build runs over.

---

## 2. Architecture

```
            host: 4 vCPU / 8 GB Linux (grading box)
 +--------------------------------+          +-------------------------------------------------+
 | simulator (python -m sim)      |   HTTP   | controller container (one process)              |
 | /sim/advance /events/chunks    |<-------->|                                                 |
 | /workflows* /approvals*        |          | main thread: loop.Orchestrator                  |
 | /workflows/*/apply|config      |          |   plan -> advance -> ingest -> refresh ->       |
 | /validation/runs /llm/...      |          |   lifecycle -> detect -> decide -> govern ->    |
 +--------------------------------+          |   verify -> ops                                 |
                                             | loader thread: stream chunk k+1 -> COPY stage   |
                                             | heartbeat thread: 10 s row                      |
                                             +------------------------+------------------------+
                                                                      | psycopg 3 (3 connections)
                                                                      v
 +-----------------------------------------------------------------------------------------------+
 | Postgres 16 container: the only stateful component                                            |
 |  schema ctl: run_registry                                                                     |
 |  schema run_<fp12>_<n>: ingest | facts | rollups | model | governance | audit | ops            |
 +-----------------------------------------------------------------------------------------------+
                                                                      ^ role ctl_api: SELECT, plus INSERT/UPDATE on hold only
                                             +------------------------+------------------------+
                                             | api container (FastAPI, 1 worker)               |
                                             | /supervisor/{tenant} /explain/* /ops /holds     |
                                             +-------------------------------------------------+
```

**Data flow inside the controller.**

```
ingest (SQL ELT) -> rollups -> snapshot.WorkflowSnapshot (numpy, per workflow)
   -> incidents.detect (DID, prompt trigger, flag CUSUM, drift, guardrail, decay)
   -> learning.* (engagement model, allocator, winners, decay, DID, schedule)
        each emits PatchRequest(path, value, class, evidence) + a decision row
   -> planner.Overlay.compose(live config + patches)  (rebase, path ownership)
   -> governance: harm gate re-check -> router (auto or approval) -> timing -> lifecycle
   -> reliability.IntentExecutor (write-ahead intent, HTTP, result)
```

Learning and incident modules never call the simulator for config changes. They emit patches;
governance is the only path to a change.

**Components and the state they own.**

| Component | Job | State |
|---|---|---|
| `simclient` | Typed HTTP client, error classes, timeouts, retries only for idempotent GETs | none |
| `store` | Pool, `tx()`, advisory lock, schema per run, migrations | `ctl.run_registry` |
| `ingest` | Chunk loader (COPY), SQL transform, rollups, lead state, mirrors `config_changed` and `approval_decided` | `ingested_chunk`, facts, rollups |
| `world` | Workflow specs, effective policy (API policy AND directives), config mirror, clock helpers | `workflow`, `config_version`, `directive` |
| `cohorts` | Cells, cohort grid, canonical identity, first-match membership | `cell`, `cohort_def`, `config_cohort` |
| `stats` | Pure numpy: ridge IRLS, Laplace posterior, seeded draws, MH ratio, CUSUMs, binomial and Poisson tails | none |
| `learning` | Engagement model, harm gate, allocator, winners, holdout, decay, DID health, schedule, guardrail | `experiment`, `posterior_snapshot`, `did_state`, `decay_monitor` |
| `incidents` | Detectors, incident state machine, prompt safety verdicts, credit budget | `incident`, `validation_run`, `prompt_verdict`, `credit_ledger` |
| `planner` | Overlay of patches on the live config, rebase, path ownership | `patch` |
| `governance` | Local policy replica, timing, lifecycle, release train, package builder | `proposal`, `proposal_transition`, `approval_mirror` |
| `reliability` | Write-ahead intents, reconciler, fault injection | `intent` |
| `audit` | Decision rows, audit log, provenance, explain queries | `decision`, `audit_log`, `allocation_provenance` |
| `obs` | Step metrics, alert rules | `loop_step`, `alert` |
| `api` | Read-only views plus `POST /supervisor/holds`, connected as role `ctl_api` (SELECT on the run schema, INSERT and UPDATE on `hold` only, nothing else writable) | `hold` |

**Where state lives.** Only in Postgres. The process holds caches rebuilt from Postgres on start:
workflow specs, the cell catalog, numpy snapshots, current posteriors. Path-dependent statistics
(CUSUMs, EW sums) are persisted after every step in `decay_monitor`, `did_state` and `incident`.

**Single owner.** `pg_try_advisory_lock(hashtext('formi_ctl:' || normalised SIM_URL))` at start,
before anything else. The key must exist before the run schema is resolved (resolution reads and may
create schemas, so it must already be single-owner), and `SIM_URL` identifies the world a controller
drives. A second controller (deploy overlap) polls the lock every 5 s instead of acting.

**Restart sequence** (details in section 8):
1. Acquire the lock on the SIM_URL key; then resolve the run schema by fingerprint plus consistency
   checks (8.5).
2. Truncate `stage_raw` rows of chunks not in `ingested_chunk`.
3. Reconcile every intent in `started` before any new side effect (8.3).
4. Ingest every listed chunk above the cursor.
5. Full proposal sweep against `GET /approvals` and `config/history`.
6. If `bootstrap.completed_minute` is NULL, rerun the bootstrap (it is idempotent: every bootstrap
   write is keyed and upserted). Rebuild caches and resume at the next step. Decisions are a deterministic function of the store
   plus API answers, so the run continues as it would have without the crash.

---

## 3. Data model

One schema per run, `run_<fp12>_<n>` (section 8.5), so 8M-row tables carry no run column and old
runs drop with one statement. `wf smallint` is a dense workflow code. Option codes are smallints
indexed into the workflow's option lists from the API.

### 3.1 Ingest path: raw lines to typed facts inside Postgres

The loader thread streams chunk bytes from httpx (`iter_bytes(1 MiB)`) straight into
`COPY stage_raw(chunk_no, line) FROM STDIN (FORMAT csv, DELIMITER E'\x02', QUOTE E'\x01')`, so each
NDJSON line lands verbatim in one text column. Neither control byte can appear in a line because
JSON encoders escape control characters. The main thread then runs one transaction per chunk:

```sql
BEGIN;
  -- exactly-once guard
  SELECT 1 FROM ingested_chunk WHERE chunk_no = :n;          -- found: ROLLBACK, skip
  CREATE TEMP TABLE ev ON COMMIT DROP AS
    SELECT r.*, ((extract(epoch FROM r.ts) - :epoch0) / 60)::int AS minute
    FROM stage_raw s, LATERAL jsonb_to_record(s.line::jsonb) AS r(
      type text, event_id bigint, ts timestamptz, workflow_id text, tenant_id text,
      campaign_id text, lead_id text, person_id text, attempt_no int, campaign_day_index int,
      config_version int, cohort_id text, variant jsonb, connected bool, engaged bool,
      disposition text, duration_s int, turns int, qa jsonb, features jsonb, leads int,
      config_version_at_creation int, version int, actor text, changed_dims jsonb,
      proposal_id text, approval_id text, status text, reasons jsonb)
    WHERE s.chunk_no = :n;
  -- sanity: SELECT count(*) FROM ev must equal chunk meta `events`, else ROLLBACK + page
  INSERT INTO cell ... SELECT DISTINCT features FROM ev WHERE type = 'lead' ON CONFLICT DO NOTHING;
  INSERT INTO campaign ... WHERE type = 'campaign_created';
  INSERT INTO lead_dim ... JOIN cell ... WHERE type = 'lead';
  INSERT INTO call_fact ... WHERE type = 'call' AND :kind = 'events';    -- history: rollups only
  UPDATE lead_dim SET state, attempts, last_attempt_min, concluded_min FROM (calls in ev) ...;
  INSERT INTO config_event / approval_event / event_other ...;
  INSERT INTO r_engage, r_pickup, r_did, r_flags ... GROUP BY ...
    ON CONFLICT (...) DO UPDATE SET n = r.n + excluded.n, ...;
  DELETE FROM stage_raw WHERE chunk_no = :n;
  INSERT INTO ingested_chunk VALUES (:n, :kind, :events, :first_id, :last_id, :until_min, :bytes, now(), :txn_ms);
COMMIT;
```

- `epoch0 = parse(state.ts) - state.sim_minute * 60 s`, from the API, never hard-coded.
- Order by `event_id`, never `ts` (ts is not monotonic inside a chunk). Leads precede their calls
  in event-id order; a call whose lead is missing gets `cell = NULL`, is counted in
  `loop_step.orphan_calls` and raises a digest alert.
- **History calls go into rollups only** (A): no per-call question about the default-config
  history is ours to answer, and it saves about 4.5M inserts at public_03.
- Fallback: `ingest/python_parse.py` implements the same `Ingestor` interface with `json.loads`
  and `COPY ... write_row`. Selected by `CTL_INGEST=python`; used if M1 measures the SQL path
  slower than the Python path.

### 3.2 Tables

```sql
-- control schema
ctl.run_registry(schema_name text PK, fingerprint text, sim_url text, created_wall timestamptz,
                 status text, last_sim_minute int)

-- catalog and identity
workflow(wf smallint PK, workflow_id text UNIQUE, tenant_id text, spec jsonb, spec_hash text,
         fetched_minute int)
directive(tenant_scope text, key text, value jsonb, source text, PRIMARY KEY (tenant_scope, key))
cell(cell_id smallint PK, lang text, tier text, age text, value text, UNIQUE (lang, tier, age, value))
cohort_def(cohort_key text PK, wf smallint, where_canon jsonb, def_hash text, cells smallint[],
           origin text,                 -- grid | business | supervisor | segment
           created_minute int)
config_version(wf, version int, actor text, config jsonb NULL, config_hash text, applied_minute int,
               proposal_id text, approval_id text, changed_dims text[],
               body_source text,        -- ours | fetched | unknown
               PRIMARY KEY (wf, version))
config_cohort(wf, version, position smallint, cohort_id text, cohort_key text,
              cohort_code smallint,     -- the dictionary code call_fact.cohort_code uses; 0 = default
              owner text,               -- ours | supervisor (business cohorts, 5.2)
              effective_cells smallint[], PRIMARY KEY (wf, version, cohort_id),
              UNIQUE (wf, version, cohort_code))    -- one row per version also for 'default'

-- ingest
stage_raw(chunk_no int, line text) UNLOGGED
ingested_chunk(chunk_no int PK, kind text, events int, first_event_id bigint, last_event_id bigint,
               until_minute int, bytes bigint, ingested_wall timestamptz, txn_ms int)

-- facts
campaign(campaign_id text PK, wf, created_minute int, sim_day smallint, leads int,
         config_version_at_creation int)
lead_dim(lead_sk bigint identity PK, lead_id text UNIQUE, person_id text, wf, campaign_id text,
         cell smallint, created_minute int, state smallint,   -- 0 active 1 concluded 2 exhausted
         attempts smallint, last_attempt_min int, concluded_min int NULL)
  INDEX (wf, cell) WHERE state = 0
call_fact(event_id bigint, wf, lead_sk bigint, minute int, sim_day smallint, hour smallint,
          seg smallint,               -- half of the dialling window: 2 * sim_day + (0 | 1)
          attempt_no smallint, day_index smallint, config_version int, cohort_code smallint,
          cell smallint, w smallint, v smallint, did smallint, prompt smallint,
          connected bool, engaged bool, interested bool, flag bool, adherence real,
          duration_s smallint, turns smallint)
  PARTITION BY RANGE (sim_day); BRIN (event_id), BRIN (minute), BTREE (wf, lead_sk)
config_event(event_id PK, wf, version, actor, changed_dims text[], proposal_id, approval_id, minute)
approval_event(event_id PK, approval_id, wf, status, reasons jsonb, minute)
event_other(event_id PK, type text, body jsonb)             -- forward compatibility

-- rollups (same transaction as facts; rebuildable from call_fact for post-D0 data)
r_engage(wf, seg, cell, config_version, prompt, w, v, connected int, engaged int, flags int, PK all keys)
r_pickup(wf, sim_day, hour, cell, attempt_bucket smallint,  -- 1 | 2-3 | 4+
         dials int, connected int, PK all keys)
r_did(wf, tick_no int, hour smallint, cohort_code, did, dials int, connected int, PK all keys)
                              -- tick_no = minute / tick, so the 1/2/4/8h windows are exact at any stop
r_flags(wf, sim_hour int, prompt, connected int, flags int, engaged int, PK all keys)

-- learned state
bootstrap(id smallint PK CHECK (id = 1), started_minute int, completed_minute int NULL,
          history_chunks int, decision_id bigint)          -- NULL completed_minute: rerun on start
experiment(exp_id PK, wf, dim text, arms smallint[], control smallint, cohort_set_version int,
           state text, started_version int, started_minute int, interrupted_by int NULL)
experiment_arm(exp_id, arm smallint, scope text,           -- 'population' or a cohort_key
               state text,                                  -- live | frozen | removed | futile | winner | control
               share real, first_dial_minute int, removed_minute int NULL, decision_id bigint,
               PRIMARY KEY (exp_id, arm, scope))
winner_look(exp_id, arm smallint, scope text, look_no smallint, n_at_look int, sim_minute int,
            w1 real, w2 real, w3 real, passed boolean, decision_id bigint,
            PRIMARY KEY (exp_id, arm, scope, look_no))      -- the doubling schedule survives a crash
winner(winner_id PK, exp_id, wf, dim text, arm smallint, scope text, look_no smallint,
       declared_minute int, decision_id bigint, proposal_id text, state text)  -- declared | promoted | rolled_back
holdout(wf, scope text, dim text, winner_id, control smallint, share real, since_version int,
        state text, PRIMARY KEY (wf, scope, dim))
posterior_snapshot(snapshot_id PK, wf, decision_id, kind text,   -- stationary | ew
                   inputs bytea,              -- npz of the aggregated count arrays, weights and prior
                   inputs_hash text, mean bytea, cov bytea, summary jsonb, seed bigint,
                   code_version text, settings_hash text)    -- enough for replay-decision (9.6)
decay_monitor(wf, scope text, dim text, arm smallint, cusum real, seg_last smallint,
              theta_ref real, alarm bool, PRIMARY KEY (wf, scope, dim, arm))
did_state(wf, did smallint, status text,  -- unknown | probing | healthy | unhealthy | degraded | suspect
          r_hat real, r_lcb real, r_ucb real, n_dials int, sprt_llr real, since_minute int,
          evidence jsonb, PRIMARY KEY (wf, did))
drift_monitor(wf, kpi text, cusum real, last_block int, baseline_kind text, alarm bool,
              PRIMARY KEY (wf, kpi))                        -- 7.5
baseline(wf, metric text, scope text, value double precision, n bigint, computed_minute int,
         PRIMARY KEY (wf, metric, scope))
incident(incident_id PK, wf, kind text, state text, opened_minute int, confirmed_minute int,
         resolved_minute int, evidence jsonb, fix_proposal_id text, credits_spent real)
validation_run(run_id text PK, wf, prompt smallint, n int, failures int, pass_rate real,
               purpose text, stage smallint, incident_id text, intent_id bigint)
prompt_verdict(wf, prompt smallint, verdict text,   -- SAFE | UNSAFE
               runs text[], pooled_n int, pooled_failures int, decided_minute int,
               PRIMARY KEY (wf, prompt))
credit_ledger(seq bigint identity PK, sim_minute int, tenant text, wf, pool text, purpose text,
              cost real, remaining_after real, intent_id bigint)
credit_envelope(pool text, tenant text, allotted real, spent real, mode text,  -- 7.2
                PRIMARY KEY (pool, tenant))                 -- tenant '*' for shared pools

-- governance and reliability
patch(patch_id PK, wf, owner text, path text[], value jsonb, class smallint, state text,
      proposal_id text NULL, decision_id bigint, supervisor_touch int NULL,
      held_until_minute int NULL, hold_count smallint DEFAULT 0)     -- auto-release (6.1)
path_owner(wf, path text[], owner text,       -- module name | supervisor
           since_version int, since_minute int, PRIMARY KEY (wf, path))
cooldown(wf, path text[], until_minute int, source_version int, reason text,
         PRIMARY KEY (wf, path))               -- 6.7 step 4
proposal(proposal_id text PK, wf, class smallint,   -- 0 incident 1 rollback 2 promotion 3 routine
         route text, state text, base_version int, proposed_config jsonb, request jsonb,
         change_summary text, diff jsonb, affected_leads int, dry_run jsonb, impact_score real,
         rationale text, evidence jsonb, approval_id text, submitted_minute int,
         decided_minute int, apply_by_minute int, notice_until_minute int, applied_version int,
         superseded_by text, requeued_from text, decision_id bigint, verify jsonb)
  UNIQUE INDEX one_in_flight ON proposal (wf)
    WHERE state IN ('submitting','pending','approved','applying','noticed','auto_applying')
proposal_transition(proposal_id, seq smallint, from_state, to_state, sim_minute, reason, decision_id,
                    PRIMARY KEY (proposal_id, seq))
approval_mirror(approval_id PK, proposal_id UNIQUE, wf, status, created_minute, expires_minute,
                decided_minute, apply_by_minute, reasons jsonb, last_polled_minute,
                approver_name text NULL)    -- the API never returns it; filled by the client's system
intent(intent_id bigint identity PK, kind text, idem_key text UNIQUE, request jsonb,
       status text,                   -- started | done | failed | aborted | unknown
       response jsonb, decision_id bigint, sim_minute int, started_wall, finished_wall)
hold(hold_id bigint identity PK, wf, set_by text, reason text, from_minute int, until_minute int,
     released_minute int NULL)   INDEX (wf, from_minute)

-- audit and ops
decision(decision_id bigint identity PK, sim_minute int, wf, module text, kind text, rule_id text,
         inputs jsonb, outcome jsonb, harm_report jsonb, parent_decision_id bigint NULL,
         snapshot_ids bigint[], seed bigint, settings_hash text, code_version text)
audit_log(audit_id bigint identity PK, sim_minute int, wall timestamptz, wf NULL, actor text,
          action text, subject_type text, subject_id text, summary text, details jsonb,
          caused_by bigint NULL)  INDEX (wf, sim_minute), INDEX (subject_type, subject_id)
allocation_provenance(wf, version, cohort_key, dim text, option smallint, share real,
          source text,          -- default | arm | winner | holdout | probe | supervisor | rollback
          inherited boolean,    -- true: the cohort has no override for dim; row copied from 'default'
          experiment_id, proposal_id, approval_id, decision_id,
          PRIMARY KEY (wf, version, cohort_key, dim, option))
          -- materialised at write time for EVERY cohort of the version (and 'default') and every
          -- dim incl. prompt_version (cohort_key 'default'), so the effective distribution is one row
          -- per (cohort, dim, option); option codes are per dim, so dim is always part of the key
loop_step(step_no PK, from_minute, to_minute, phase_ms jsonb, chunks int, events int,
          orphan_calls int, decisions int, proposals_submitted int, applies int,
          credits_remaining real, rss_mb int, db_bytes bigint)
alert(alert_id PK, severity text, code text, wf, opened_wall, closed_wall, message text, runbook text)
clock(id smallint PK CHECK (id = 1), sim_minute int, end_minute int, tick int, last_step_no int)
```

`one_in_flight` turns "one change in flight per workflow" into a database invariant (C). A
higher-class proposal that supersedes the holder marks it `superseded` and inserts itself in the
**same transaction**, which is the only way past the index (J2 fix to C's contradiction). Every
transition writes an `audit_log` row and a `proposal_transition` row in the same transaction.

### 3.3 Sizes at public_03 scale

Volumes read from `/workflows`: 12 workflows x 4,500 dials/hour x 12 hours = 648k calls/day and
12 x 16,000 = 192k leads/day; 7 history days plus 5 run days.

| Object | Rows | Size (est., M1 measures) |
|---|---|---|
| `call_fact` (post-D0 only) | 5 x 648k = 3.24M | about 310 MB heap + 90 MB indexes |
| `lead_dim` + unique `lead_id` | 12 x 192k = 2.3M | about 380 MB |
| `r_pickup` | about 100k per day, 1.2M | about 70 MB |
| `r_engage` | about 36k per day, 0.4M | about 30 MB |
| `r_did`, `r_flags` | under 100k | under 10 MB |
| governance, audit, metrics | tens of thousands | under 50 MB |
| **Total** | | **about 0.95 GB + WAL** (`max_wal_size = 4GB`) |

Postgres settings (compose): `shared_buffers=1GB`, `effective_cache_size=3GB`, `work_mem=64MB`,
`maintenance_work_mem=256MB`, `max_wal_size=4GB`, `checkpoint_timeout=15min`, `wal_compression=on`,
`jit=off`, `synchronous_commit=on` (audit durability is a constraint), autovacuum scale factor 0.05
on rollups. Memory: db 3 GB limit, controller 1 GB, api 256 MB, which leaves about 3.5 GB for the
simulator (measured peak 805 MB on public_03, docs/measurements/simulator-baseline.md) and the OS.

---

## 4. The control loop

### 4.1 Per-step sequence

```
startup: lock -> resolve run -> reconcile intents -> ingest missing chunks -> load caches
         -> once: bootstrap (baselines, cohort grid, DID states, D0 prompt screen plan)

loop:
  1  PLAN      to = next_stop(now)                                (4.2, pure function)
  2  ADVANCE   intent(advance, key "adv:<from>-><to>") committed; POST /sim/advance;
               never blind-retried; result committed with clock.sim_minute
  3  INGEST    every new chunk (plus any gap from GET /events/chunks?after=cursor), in order.
               Decisions are blocked until ingest lag is 0.
  4  REFRESH   GET /sim/state (credits); for every workflow with a config_changed event:
               GET /workflows/{id}/config, store the body if version == current
               (else body_source = unknown); supervisor-edit handler (6.7)
  5  LIFECYCLE apply every visible approval (pre-apply checks first), resolve rejected,
               expired, stale, superseded; return still-wanted patches to the overlay
  6  DETECT    DID detector and SPRT, prompt trigger, flag CUSUM, KPI drift CUSUM (7.5),
               guardrail, decay monitors
  7  DECIDE    incidents.respond() first (may spend credits); then learning: harm stops (5.4) at
               every step; allocator, winners and schedule only at the workflow's planning point
  8  GOVERN    per workflow, in priority-class order: compose -> harm gate -> router -> timing
               -> submit, notice, auto-apply or hold (with a decision row either way)
  9  VERIFY    post-apply checks whose windows have elapsed
 10  OPS       loop_step row, alert rules, heartbeat
 11  if state.finished: final ingest and reconcile, run_registry.status = done, exit 0
```

Order matters (B): lifecycle runs before detection, so a visible approval is applied before an
incident fix is built on top of it; incidents run before routine work, so a routine release is
always based on a version that already contains the incident fix. Every decision row is committed
before its HTTP call; the response and the resulting transition are committed together afterwards.
The simulator is frozen between advances, so every pre-check is race-free.

**Planning point.** Per workflow, the step that lands on its next routine slot (6.3), plus one
evening point at the dialling-window close for auto-apply-routable work. Where a directive requires
a notice posted inside approver hours (Kestrel-like), the evening point may only execute auto-applies
whose notice was posted at least `notice_minutes` earlier and inside approver hours; otherwise the
work waits for the next day's notice at approver open. Models (ridge-logit,
hour curves) are refit only at planning points or every 4 dialling hours; detectors update every
step in O(new events).

### 4.2 Advance step-size rule

**Derivation of the apply bound (B, corrected comment).** The simulator decides a pending approval
during the first tick that starts at or after its hidden decide minute `d` (world.py:589-603), so
the decision is processed at `T = ceil_to_tick(d)` and becomes visible when an advance ends at or
after `T + tick`. It lapses if we apply at a minute later than `d + W`, `W = apply_window_hours x 60`
(world.py:443-447). Take stops on a grid of step `S` starting at a stop `s0`. The worst case is
`d = s0 - tick + 1`: then `T = s0`, the decision is visible at `s0 + S`, and we need
`s0 + S <= d + W = s0 - tick + 1 + W`, so

```
S <= S_watch_max = tick * floor((W - tick + 1) / tick)       # W = 120, tick = 15 -> 105
```

Our in-hours base step is 60, which is already inside the bound; the bound binds when the base
step is larger (night, or a future `BASE_STEP` above `S_watch_max - tick`).

**Decision horizon.** Decisions happen only at `d` in `[open, close)` of approver hours, so a
decision can become visible as late as `close + tick` (a decision at `close - 1` is processed at
tick `close` and visible after it). An overnight jump is therefore allowed only from
`now >= close + tick` for every workflow with a pending approval.

```python
def next_stop(now: int, ctx: PlannerContext) -> int:
    tick = ctx.tick
    S = ctx.base_step                        # 60 (a multiple of tick) [T]
    for wf in ctx.workflows:
        g = wf.policy
        if wf.has_pending and may_decide_before(now + S, wf):   # decision horizon open
            S = min(S, s_watch_max(tick, g.apply_window_min) - tick)   # 90 public, one tick spare
            if wf.pending_class == INCIDENT and now >= wf.pending_created + g.lat_lo:
                S = tick                     # fast apply: cuts expected MTTR by about 22 min
        if wf.open_incident or wf.suspect_did or wf.suspect_prompt or wf.drain_chain_pending:
            S = tick
    B = min(boundaries(now, ctx))            # dialling window open and close, approver open,
                                             # close + tick, daily batch time, planned slots,
                                             # notice expiry, apply_by - tick of anything
                                             # approved, scenario end
    S = min(S, B - now)
    if nothing_dials(now, ctx) and no_decision_possible(now, ctx) and nothing_due_before(B):
        S = B - now                          # overnight jump, e.g. 21:00 -> 08:00 in one advance
    return now + max(tick, floor_to_tick(S))
```

Before any jump, step 5 has already applied every visible approval. The assertion checked at jump
time is: no proposal of ours is in the local state `APPROVED` or `APPLYING`. Proposals whose
simulator status is `approved` but whose local state is terminal without an apply (`STALE`,
`SUPERSEDED`, `ABANDONED`, `LAPSED_LOCAL`) are legitimately never applied and are excluded. A
violation pages (`APPLY_AT_RISK`). About 14 advances per simulated day; incident days
add about 8 tick steps.

**Decision freshness.** Data for minute m drives a decision by `ceil_to_step(m)` plus ingest wall
time: at most 60 simulated minutes, 15 during incidents.

**End-game freeze.** No new arms or probes in the last `max(1 day, 15% of the scenario)`
[T]; ramps stop in the last 1 day [T]; safety, rollbacks, DID drains and already-declared
promotions continue to the end.

---

## 5. Learning loop

### 5.1 Volumes the design must work with

All read at runtime: dials/day = `dials_per_hour x (window_end - window_start)`; connected and
engaged rates from history rollups. Public numbers for sanity only:

| Workflow (public_01/02) | Primary | Dials/day | Connected/day | Base rate |
|---|---|---|---|---|
| nw_renewal | engagement | 3,600 | about 950 | engagement about 0.555 |
| nw_kyc | pickup | 2,000 | about 465 | pickup about 0.23 |
| ks_cancel | engagement | 4,800 | about 1,200 (800 after the DID flag) | engagement about 0.47 |
| each public_03 workflow | mixed | 54,000 | about 13,500 | |

### 5.2 Cohorts: one shared object with a versioned identity

**Cells.** A cell is one observed `(lang, city_tier, age_band, value_band)` combination
(`cell_id`, dictionary built from observed values, so an unseen language just adds cells). Every
rollup is keyed by cell, so any cohort's statistics are sums over rows.

**Grid at D0, from history only, per workflow** (A):
1. `F1` = the feature whose levels have the most different hour-of-day pickup curves: binomial
   deviance drop of `connected ~ hour * f` over `connected ~ hour + f` per degree of freedom, on
   first-attempt rows of `r_pickup`, required `p < 0.001` [T]. It drives the schedule.
2. `F2` = `lang` if the workflow has 2 to 5 languages each with at least 10% of leads [T], else the
   remaining feature with the most balanced mass. Stated prior (welcome and voice are language
   bound), not a learned fact.
3. Cells of `F1 x F2` under **5% of daily leads** [T] merge with their `F1` neighbour within the
   same `F2` level, then into an `other` bucket per `F1` level. Cap 20 (API); if still above 20,
   merge `F2` levels by size.
4. Cohorts are ordered most specific first, merged buckets last, so first-match is unambiguous.

**Business cohorts first** (hidden default configs may already define cohorts, mixed allocations
or schedules; the schema allows it). Cohorts in the business default are kept verbatim, in their
order, at the front of the list, and registered as supervisor-owned paths (`path_owner`, 6.7): we
never redefine, reorder or delete them, and we change their allocations or tables only by an
approval whose summary names them. Our grid cohorts are appended after them, so first-match gives
the business cohorts their leads unchanged, and only `20 - n_business` grid cohorts are built.
Mixed business allocations are used as data: history calls carry their `variant`, so `r_engage`
from history already holds concurrent comparisons between the variants the business ran (5.3).

**Minimum sizes: two different roles, two different floors** (recomputed with the real ramp,
5.5).
- **Targeting cohort: at least 5% of daily leads [T].** A targeting cohort carries its own hour
  weights and its own allocation override. Its binding need is the hour curve: 14 history days x 5%
  x 3,600 dials (nw_renewal, sanity) = about 2,520 history dials, about 210 per window hour, which
  matches the shrinkage prior strength of 200 (5.8). It also gives the effect model a cohort-level
  posterior, but that posterior narrows at the speed of its feature levels, not of the cohort (5.4).
- **Winner scope: at least 15% of daily connected calls [T] and a projected n = 800 look before
  the freeze.** A cohort-scope winner needs an arm to reach the 800-call look in that cohort. Along
  the real ramp (5.5), a 50% cohort at nw_renewal volume gets there around D6 and a 5% cohort never
  inside a 10-day run (it would need 20+ days). At public_03 volume a 15% cohort gets there in
  about 2.5 days. Cohorts below the winner floor still benefit from cohort effects, but only
  through probability-matching allocation (up to the 50% ceiling) and through population winners,
  never through a declared cohort winner. Smaller cells exist only as merged buckets.

**Identity** (A, B, C agree). `cohort_key = <readable slug> + "~" + sha1(workflow_id + canonical
where + order index)[:6]`, for example `age60p_ta~3fa2c1`. The `id` we write into the simulator
config **is** the `cohort_key`, so `cohort_id` on call events, experiment rows, detector rows, dry
runs and the supervisor view all carry the same id. A changed definition is a new key; nothing is
redefined in place. Effective (first-match) membership is resolved per config version into
`config_cohort.effective_cells`; ingest checks the simulator's `cohort_id` on every call against our
resolution and pages on mismatch (our model of the config would be wrong).

**Allocation layering** (A). Uniform exploration lives in `allocations.default`. A cohort gets its
own override for a dimension only when its allocation diverges (cohort-specific exploitation or a
promotion). So a supervisor's edit to `default` reaches every cohort without an override, as they
expect, and diffs stay small.

**Regrid: cut** (section 1). The D0 grid stays for the run; the effect model still estimates
off-grid heterogeneity through its feature terms, and the allocator can only act on it inside
existing cohorts. Supervisor edits to `cohorts` are adopted as a new set version; experiments whose
cohorts disappeared become `interrupted` (data kept, decisions paused) and are re-planned.

### 5.3 Experiment design: welcome x voice, factorial, cohort-aware

**Scope by objective.** Welcome and voice experiments run only where `objective.primary ==
engagement_rate`. On pickup-primary workflows they cannot move the primary KPI, can only hurt the
guardrail, and would consume the approval slots that caller-ID and schedule changes need.

**Factorial.** The simulator samples welcome, voice and caller ID independently per dial from the
lead's cohort distributions (world.py:776-789), so every product allocation is a full factorial and
each call informs both dimensions, roughly doubling effective sample size versus one dimension at a
time. All arms are explored at once (A), so options listed late are tested as early as options
listed first (the J2 attack on B's option-order queue).

**Effect model** (per workflow; deterministic; refit at planning points):

```
For connected call i in cell c(i), segment j(i), prompt p(i), welcome a(i), voice b(i):
logit P(engaged_i) = beta[c] + gamma[j] + pi[p]
                   + theta_W[a] + sum_f delta_W[a, f, level_f(i)]
                   + theta_V[b] + sum_f delta_V[b, f, level_f(i)]
control arms (incumbent welcome, incumbent voice): theta = delta = 0
f ranges over lang, age_band, city_tier (all three, not only the grid features)
priors: theta, delta ~ N(0, tau_E^2), tau_E = 0.13 on the logit scale [T]
        (about 0.06 on the log-probability scale at e = 0.55); beta, gamma, pi ~ N(0, 2^2)
fit: ridge IRLS on aggregated counts from r_engage (cell x segment x prompt x welcome x voice),
     Laplace posterior (mean, covariance)
size: beta per cell 108 to 252 + gamma per segment (about 20 in 10 days) + pi per prompt (<= 5)
     + per arm 1 theta and one delta per level of the three features (1 + 3 + 5 + 3 = 12),
     5 arms -> 60; total about 300 to 450 parameters
cost: about 20k non-empty aggregated rows, about 11 non-zeros per row; X'WX accumulated with
     np.bincount on flattened (i, j) indices (about 2.4M adds), Cholesky of a 450 x 450 matrix,
     6 to 10 IRLS iterations: about 100 to 300 ms per fit (estimate; M5 measures), plus about
     100 ms for 4,000 draws; cohort effects are computed per cell (<= 252), never per lead
two fits per planning point:
     STATIONARY: every segment weight 1
     EW:         segment weight 2^(-age_segments / 2)   (half-life 12 dialling hours) [T]
```

- Data come only from calls whose recorded `variant` shows the arm ran (Product: only what
  happened counts). On `next_campaign_only` workflows the event's `variant` decides the arm, not the
  locked `config_version`. History calls count too: if the business default already mixed welcome
  or voice variants, those calls are concurrent comparisons and enter the fit like any other (their
  segments get their own `gamma`). If the default ran one variant per dimension, history informs only
  `beta` and the arms start at the prior.
- Segment and prompt intercepts absorb supervisor prompt edits and day-level shocks so they do not
  leak into arm effects.
- **Cohort-level effects**: for arm a in cohort k, average over the cohort's active leads of the
  predicted engagement ratio arm vs control, from 4,000 posterior draws with seed
  `hash(run, workflow, decision_id, fit_kind)`. Same seed and data give the same answer, so winner
  selection is deterministic (ADR 0004).
- **Interaction test: cut** (section 1). Assumption A-1 (no welcome x voice interaction) is stated,
  not tested. Its failure mode: a winner chosen on main effects can be the wrong combination, which
  the holdout and the decay monitor see after promotion as a smaller-than-predicted effect. The
  sketch kept for later: refit with `(k_W - 1)(k_V - 1)` combination terms and a likelihood-ratio
  test at alpha 0.01 (chi-square critical value 16.8 at 6 df for 3 welcomes x 4 voices).

Why this beats one challenger per dimension at workflow level (B) on hidden scenarios: a voice
that is x1.22 for one age band and x0.88 for another averages toward null at workflow level, so a
workflow-level test never finds it; the interaction terms estimate each band's effect from all
calls in that band without splitting traffic into separate experiments.

### 5.4 Allocation: harm-gated probability matching

At each governance opportunity, per cohort k and dimension (ADR 0004):

```
target_k   = probability matching on the EW posterior: share_a proportional to
             P(a is best in k | data), from the seeded EW draws
             floors: every live undecided arm >= 2% [T]; control >= holdout h_k (5.6)
             ceiling: an arm not yet declared winner <= 50% of its cohort [T]
             arms with an active decay alarm (5.7): share may not increase
proposal   = current + lambda_k * (target - current),  lambda_k in [0, 1], one per cohort k
lambda_k*  = largest lambda_k passing ALL of
               population harm gate on EW draws:  P(de < -1%) <= 2.5%      (Monte Carlo, 4,000 draws)
               cohort harm gate on EW draws:      P(de_k < -x_k) <= 2.5%   for this cohort
               TV per cohort <= max_shift                                 (only if routed to auto-apply)
               guardrail dry run: predicted guardrail drop <= 0 and P(drop > x) <= 2.5%   (5.10)
             found by bisection (12 iterations each, deterministic, cohorts in a fixed order;
             the population gate is re-checked after all cohorts and every lambda_k scaled by the
             largest common factor that passes it)
             a cohort whose lambda_k is below the default's gets its own override (5.2 layering)
minimum gain (exploitation moves only): skip if the expected gain on the KPI THE LEVER MOVES
             is < 0.25% [T] of that KPI's daily numerator in the affected cohorts
             lever KPI: welcome, voice, prompt -> engagement; caller_id, schedule -> pickup
             exempt: information-gathering moves (a new arm, a probe, a first exploration share,
                     which the gate scores at prior mean 0, so their expected gain is 0 by design)
                     and risk-reducing moves (6.5: removals, rollbacks, harm stops, drains)
```

**Harm stop** (runs every step, not only at planning points). For each live non-control arm a and
scope k (population or cohort) with share above the 2% floor: if on the EW posterior
`P(theta_{a,k} < 0) >= 0.95` [T] and the expected loss `share_{a,k} x |E[theta_{a,k}]| >= 0.5%` [T]
of the scope's engagement, emit a P1 risk-reducing patch moving that arm's share in that scope to the
control. It chains per tick if `max_shift` limits it (6.5). **Arm removal** (planning points):
`P(theta_{a,k} < 0) >= 0.8` [T], a P3 risk-reducing patch. **Futility drop** (at a look, 5.5):
`P(theta_{a,k} > ln 1.02) <= 5%` [T], meaning the arm is very unlikely to be even 2% better, so its
traffic goes back to the other arms; risk-reducing, reported in the digest.

**Cost of the per-cohort gate, computed.** A cohort's effect is `theta + one delta per feature`
(lang, age, tier), four terms each with prior sd 0.06, so its prior sd is `0.06 x sqrt(4) = 0.12`.
Uniform arms of share `s_i` in that cohort give `sd(de_k) = 0.12 x sqrt(sum s_i^2)`:

```
x_k = 2% (default):   1.96 x 0.12 x sqrt(sum s^2) <= 0.02  ->  sqrt(sum s^2) <= 0.085
                      5 arms: 3.8% each, the same as the population gate: no cost at launch
x_k = 1% (Northwind): sqrt(sum s^2) <= 0.0425  ->  5 arms: 1.9% each (9.5% total exposure)
                      cost: half the arm calls while posteriors are wide; nw_renewal arms get about
                      18 connected a day instead of 36, so every look in 5.5 arrives about 1.5 days
                      later (the "slow and right" Northwind asked for, memo 02 item 4)
```

The cohort posterior narrows faster than the cohort's own traffic suggests: each `delta` is
estimated from every call in that feature level (for example all 60+ leads, in every language), so a
5% cohort's gate loosens at the speed of its feature levels.

**Why not a fixed split** (C): 10% on each of 5 challengers fails the harm gate on day 0, and one
x0.88 arm makes the launch about -1.2%, graded harmful. **Why not plain Thompson sampling**: random
per decision (constraint 4), moves traffic every step (each move is a governance action), no bound
on per-change harm. **Why probability matching with seeded draws** (ADR 0004): it is Thompson's
allocation rule computed as a deterministic expectation, so it is reproducible from the audit store
and explainable as "each arm gets traffic in proportion to the chance it is best, limited by the
harm gate".

**First exploration size from the gate.** Untested arms are evaluated under the prior with mean 0
(deliberately pessimistic) and sd `tau_H` per arm. The population change is
`de ~ sum_i s_i theta_i` with sd `tau_H * sqrt(sum s_i^2)`, so

```
P(de < -1%) <= 2.5%   <=>   1.96 * tau_H * sqrt(sum s_i^2) <= 0.01
tau_H = 0.06 [T]:  sqrt(sum s_i^2) <= 0.085
  5 challenger arms (3 welcome, 4 voice options): s = 3.8% each, 19% total exposure
  3 arms: 4.9% each;  2 arms: 6.0% each;  1 arm: 8.5%
```

`tau_H = max(0.06, tau_EB)`, `tau_EB` being the empirical-Bayes dispersion of arm effects from day 1
on: more dispersion than assumed makes the gate stricter, never looser. Exploitation pays for
exploration: in one change, moving traffic onto an arm with a confidently positive posterior gives
a positive expected `de` that offsets new exploration variance, and removing an arm whose posterior
turned negative counts as a gain.

**Ramp speed at dev volume** (nw_renewal, 950 connected/day, 5 arms): 3.8% per arm gives about 36
connected per arm per day. Posterior sd of a population-level arm effect after n arm calls is
`1 / sqrt(1/tau^2 + n/0.82)`: 0.056 after 36, 0.047 after 150, 0.035 after 500. Expected
trajectory: 4% on D0, 10% to 15% on D1, 20% to 30% for promising arms by D2 and D3; an arm is
removed as soon as `P(theta < 0) >= 0.8` in its scope [T] (removals are risk-reducing). Under the
Northwind 1% cohort line the same trajectory starts at 1.9% and runs about 1.5 days behind. At load
(13.5k connected/day), 3.8% gives about 510 calls per arm per day and a posterior sd of 0.034 after
one day: arms separate in one to two days.

**Routing.** Each allocation change goes to the router (6.5). Under the default directive,
exposure-increasing customer-facing changes go by approval in the routine slot (one or two a day),
and arm removals may auto-apply where policy allows. The harm gate, not the slot count, is the
binding limit at dev volume: each change may add about 4% to 6% per arm while posteriors are wide.

### 5.5 Deciding a winner

**Looks.** A hypothesis (arm a, scope k), where a scope is the population or one grid cohort that
meets the winner-scope floor (5.2), is looked at only at planning points on an
**information-doubling schedule**: the first look when the arm has at least 200 connected calls in
scope over at least 2 calendar days [T], later looks only once the arm's in-scope count has doubled
since the previous look (200, 400, 800, 1,600, 3,200). Each look is a `winner_look` row with
`n_at_look`, so the schedule survives a crash and is replayable.

**Rule.** Arm a is declared the winner in scope k at a look when all hold:

| # | Condition | Purpose |
|---|---|---|
| W1 | `P(theta_{a,k} > 0) >= c_k` on the STATIONARY posterior: **c = 0.995 for the population, 0.998 for a cohort** [T] | evidence accumulates over the whole arm life; per-scope threshold because cohort priors are wider |
| W2 | `P(a is best in k)` >= 0.95 on the STATIONARY posterior [T] | one winner per dimension and scope |
| W3 | Recency: on the EW posterior `P(theta_{a,k} > 0) >= 0.90` and `E_EW[theta] >= 0.5 x E_stat[theta]` [T] | a peaked-then-decaying arm fails (F1) |
| W4 | No decay alarm, and the arm's CUSUM statistic is below `h/2` (5.7) | no declining trend |
| W5 | The promotion change passes the population and cohort harm gates on EW draws at 2.5% | grader-equivalent safety at apply, plus our cohort line |
| W6 | Guardrail check (5.10) | no predicted guardrail drop |

**Effective boundary.** Under the prior `N(0, v)` and an estimate `theta_hat` with variance `s^2`,
W1 is equivalent to `z = theta_hat / s >= z_c x sqrt(1 + s^2 / v)`. Two facts set `s^2` and `v`:
- **Unequal sizes.** The control keeps most of the traffic while arms are small, so the variance
  of the log rate ratio is dominated by the arm: `s^2 = (1 - e) / (e n) = 0.82 / n` at e = 0.55
  (n = arm calls in scope), not the equal-size `1.64 / n`.
- **Scope.** A population effect has prior sd `tau = 0.06` (A-2), `v = 0.0036`. A cohort effect is
  `theta + three deltas`, prior sd 0.12, `v = 0.0144`.

| Look (arm n) | s^2 = 0.82/n | Population, c = 0.99 | Population, **c = 0.995** | Cohort, c = 0.99 | Cohort, **c = 0.998** |
|---|---|---|---|---|---|
| 200 | 0.00410 | 3.402 | 3.767 | 2.636 | 3.262 |
| 400 | 0.00205 | 2.914 | 3.227 | 2.486 | 3.076 |
| 800 | 0.001025 | 2.636 | 2.920 | 2.407 | 2.979 |
| 1,600 | 0.000513 | 2.486 | 2.753 | 2.367 | 2.929 |
| **Union bound, 4 looks** | | 1.28% | **0.54%** | 2.8% | **0.47%** |

Null crossing probabilities per look, chosen thresholds: population 0.0000826, 0.000625, 0.00175,
0.00295; cohort 0.000552, 0.00105, 0.00145, 0.00170. A single W1 of 0.99 would give 1.28% per
population hypothesis and 2.8% per cohort hypothesis (2.1% even with equal sizes), which is why the
threshold is set per scope. Looks are strongly correlated, so true rates lie between the last-look
value and the union bound. The M5 acceptance test measures the rate by simulation of the exact rule
under the real adaptive allocation (10.1).

**Family.** Hypotheses per workflow = arms x (1 + eligible cohort scopes). Worst case 5 arms x (1 +
8): expected null winners per workflow-run under a global null at most `5 x 0.54% + 40 x 0.47% =
0.22` (union bound); W2 lowers it further. At dev volume only 0 to 2 cohorts meet the winner-scope
floor, so the bound is `5 x 0.54% + 10 x 0.47% = 0.074`.

**What a false winner costs.** A null winner is graded neutral (de about 0) and costs one
approval slot. A negative winner is what matters, and W5 requires `P(de < -1%) <= 2.5%` (and the
cohort line) on the promotion itself on recency-weighted data. Harm needs `share x |theta| > 1%`:
promoting 50% of a 50% cohort onto an arm with true multiplier 0.97 is de = -0.75%, still neutral.

**When the looks arrive, along the real ramp** (not 50/50 from day 0). Arm share per day follows
the gate (5.4): about 3.8% on D0, 10% to 15% on D1, 20% to 30% on D2 and D3, then up to the 50%
ceiling.

| Workflow volume and cohort line (sanity labels) | Arm calls per day along the ramp | Population n = 800 | Population n = 1,600 | 50% cohort n = 800 |
|---|---|---|---|---|
| 950 connected/day, `x_k = 2%` | 36, 114, 237, 285, 380, 475 | about D4.3 | about D6.2 | about D6.2 |
| 950 connected/day, `x_k = 1%` (Northwind) | 18, 48, 95, 190, 285, 380, 475 | about D5.4 | about D7.2 | about D7.2 |
| 1,200 connected/day, `x_k = 2%` | 46, 144, 300, 360, 480, 600 | about D3.9 | about D5.5 | about D5.5 |
| 13,500 connected/day (public_03) | 513, 1,620, ... | about D1.2 | about D1.7 | about D1.7 |

So at dev volume a population winner is possible from about D4, a cohort winner only for a cohort of
about half the traffic and only from about D6, and a 5% cohort would need 20+ days. That is why the
winner scope has its own floor (5.2) and why small-cohort effects act through allocation only.

**Product's question: minimum cohort size, evaluation window, and the cost.** Targeting cohort 5%
of daily leads, winner scope 15% of daily connected calls with a projected look before the freeze
(5.2). Evidence floor 200 connected per arm over 2 days. Windows: all data since the arm went live
for W1 and W2, a 12-dialling-hour half-life for W3, W5 and allocation. Cost of the threshold at
population scope, for a true +10% effect (theta = 0.0953) with `s^2 = 0.82 / n` (power is the
marginal probability of crossing at that look, a lower bound on "declared by then"):

| W1 at population scope | Null bound, 4 looks | P(declared at the n = 800 look) | P(declared at the n = 1,600 look) |
|---|---|---|---|
| 0.95 | 9.8% | 0.87 | 0.99 |
| 0.975 | 4.0% | 0.78 | 0.98 |
| 0.99 | 1.28% | 0.63 | 0.96 |
| **0.995 (chosen)** | **0.54%** | **0.52** | **0.93** |
| 0.998 (cohort choice) | 0.18% | 0.39 | 0.87 |

Moving from 0.995 to 0.95 raises the chance of a decision at the 800 look from 0.52 to 0.87 and
multiplies the null bound by about 18. At cohort scope (0.998, prior sd 0.12) a +10% effect has power
0.50 at 800 and 0.90 at 1,600; a +22% cohort effect (theta = 0.199) has power
`Phi(0.199 / sqrt(0.82/400) - 3.076) = Phi(4.40 - 3.08) = 0.91` already at the n = 400 look.

### 5.6 Promotion with holdout

A promotion sets the scope's allocation to `1 - h_k` on the winner and `h_k` on the control and
drops the other arms of that dimension in that scope (always by approval: customer-facing exposure
increase).

```
h_k = smallest of {5%, 10%, 15%, 20%} such that the one-day SE of log(winner / holdout)
      sqrt( (1-e)/(e N_k h) + (1-e)/(e N_k (1-h)) ) <= 0.075 [T],   capped at 20%
N_k = connected calls per day in the promoted scope (from rollups)
```

| Case | N_k | h | One-day SE |
|---|---|---|---|
| population-wide winner, nw_renewal | 950 | 20% | 0.0725 |
| 50% cohort, nw_renewal | 475 | 20% (cap) | 0.103 |
| population-wide winner, public_03 | 13,500 | 5% | 0.035 |

Holdout cost is `h x theta` (20% x 10% = 2% of the scope's engagement while the winner is good). It
buys decay visibility and a concurrent guardrail comparator. A holdout is never removed late in a
scenario: the remaining gain cannot pay for losing decay visibility.

### 5.7 Decay detection and rollback timing

Facts that shape it (UNDERSTANDING section 5): decay starts some time after a variant's first dial
anywhere, declines gradually, and a rollback at onset is graded harmful because the variant is still
at full strength.

**Monitor** on every non-control arm with at least 10% share in some scope [T] (A armed at 15%;
lowered per F1), and on every promoted winner against its holdout:
- Per half-window segment j: `z_j = (log RR_j - theta_ref) / SE_j`, `RR_j` the arm-vs-control ratio
  in that segment (Mantel-Haenszel across cells), `theta_ref` the stationary posterior mean when
  monitoring started (or at promotion).
- One-sided Page CUSUM for a decrease: `S_j = max(0, S_{j-1} - z_j - k)`, `k = 0.5`, alarm at
  `h = 4` [T]. In-control ARL about 168 segments (84 dialling days: about a 12% chance of a false
  alarm per monitored arm in a 10-day run); out-of-control ARL about 8.4 segments at a 1-SE shift and
  3.3 at 2 SE.
- An alarm freezes increases for that arm (5.4) and starts the rollback evaluation below; it never
  rolls anything back by itself. Below 10% share the EW posterior alone limits the arm: as recent
  data weakens, its probability-matching target and its harm-gate headroom both shrink.

**Rollback decision** (every step once an alarm is on; every planning point regardless):

```
use the EW posterior of the arm's current effect
predict to the expected apply time t_a (now for auto-apply; now + expected approval latency inside
  approver hours, from the timing calculator, for approval), with a local linear trend fitted to
  the last 4 segments
rollback (move the arm's share to the control or the next-best arm) when
   E[de_rollback(t_a)] > 0  and  P(de_rollback(t_a) < -1%) <= 10% [T]
partial rollback = the largest share cut that passes the 10% gate
```

The 10% tolerance (vs 2.5% elsewhere) is deliberate: a decaying variant costs engagement every hour,
the target is a known baseline, and the grader's decay metric counts every day the variant stays.
Predicting to `t_a` means we propose when the variant is expected to be below the control by the
time it is applied, not when it crosses (A).

**Detection lag at dev volume** (sanity check: a variant falling about 0.27 in log-multiplier over 2
days, nw_renewal, 20% holdout, segment SE about 0.10): drift about 0.07 per segment, so the CUSUM
crosses after about 3 to 4 segments, 1.5 to 2 days after onset, when the variant is near or below
the control and the rollback grades beneficial. At public_03 volume (segment SE about 0.03) the alarm
comes within one segment. Residual risk at dev volume, stated: a fast decay costs one to two days of
exposure. What F1 changes: during those 1.5 to 2 days the arm can no longer be ramped or promoted,
because W3 and the EW harm gate see the decline first (EW effective window about 1.7 dialling days).

Not done: staggering exploration so that decay clocks start only when we can exploit them. That
would be tuning to a simulator mechanism; real novelty decay would not care.

Built and measured, off by default (ADR 0034, `decay_clean_segments`): holding an arm at the 10%
monitoring floor until its monitor has folded two clean segments. It cost about 7 points of gap on
stable worlds and gained under 1 on the p02 family, so the residual stated above stays. ENG-GAP also
fixed the gate defects that delayed the rollback after the alarm (plans are made against the tail
minus two Monte Carlo errors, and a rollback is judged on its planner's draws).

### 5.8 Call time (per cohort)

**From history:** pickup by (cell, hour, attempt bucket {1, 2-3, 4+}) from `r_pickup`. History ran
on a flat schedule, but later hours over-represent hard-to-reach leads, so curves are averaged over a
fixed attempt mix, and the remaining bias is stated. Curves are shrunk toward the workflow's pooled
curve with a prior strength of 200 dials per (cell, hour) [T].

**Weights** (comparative advantage, because the dialler ranks eligible leads within an hour by weight
times Gumbel noise, world.py:756-770, and capacity is binding):

```
w_k(h) = clip( (p_k(h) / pbar_k)^kappa / mean_h(...), 0.25, 4.0 ),   kappa = 2 [T]
pbar_k = cohort k's mean pickup over the window hours
every in-window hour gets weight >= 0.25: a missing hour gets weight 0 and wastes capacity the
business owns (constraint 2)
```

**Governance.** Schedule changes are graded neutral (no engagement or caller-ID effect), so they
never trip the harm gate. They ship in the D0 batched approval (they need that approval's cohorts)
and are refreshed at most daily, by auto-apply where `schedule` is allowed and nothing is in flight,
inside a routine approval otherwise, and only if the L1 distance between normalised weight tables
moves by more than 0.1 [T]. Business schedule tables (5.2) are supervisor-owned and left alone. With
no history (`history_days = 0`) no table is proposed until 3 dialling days of our own dials exist.

**Verification.** Analytic expectation `connects_h = capacity_h x sum_k q_{k,h}(w) x p_k(h)` with
`q_{k,h}` proportional to `eligible_{k,h} x w_k(h)` from our lead tracker. **Credible band**: for
each (cohort, hour) cell with at least 30 dials [T] in the 2 segments after the change, the 95%
posterior predictive interval of its connects given its dials, from the shrunk hour-curve posterior
(Beta with the 200-dial prior) times the current DID factor. Verification fails if more than 10% [T]
of those cells fall below their band, or if DID-adjusted workflow pickup falls below the lower 95%
bound of its prediction; then a P1 proposal restores the previous table (risk-reducing, 6.5).

**Composition.** A schedule change changes who connects, so it can move the engagement rate
through the cohort mix with no per-person effect (5.10). The dry run therefore also predicts the
mix-adjusted engagement `E(w) = sum_k connects_k(w) e_k / sum_k connects_k(w)`, with `e_k` from the
effect model, and the guardrail check (5.10) applies to that prediction.

**Per-person timing: cut** (section 1). Sketch kept for the summit conversation: Beta-binomial per
person on four hour bands, prior from the cohort curve, a preference only when the person's posterior
peak beats the cohort's chosen hour by 25%, capped so that no hour's preferred demand exceeds 50% of
its capacity.

### 5.9 Caller-ID health and rotation

**Signal.** DIDs are sampled per dial independently of the lead, so the concurrent pickup of DID d
against the pooled other live DIDs, stratified by (cohort, hour) from `r_did`, is an unbiased health
comparison: `r_d` = Mantel-Haenszel ratio. History classifies DIDs that the default config used;
DIDs in `options.caller_id` never dialled in history start `unknown`. One pickup rate is used in
every computation below: the workflow's history pickup `p` (no history: the first 2 dialling days of
our own dials). The worked numbers use `p = 0.26` (sanity label, dev renewal-like) and `p = 0.25`
(cancel-like); variances use `var(log r_hat) ~ (1 - p_d) / (n_d p_d) + (1 - p) / (n_o p)`,
`p_d = r p`.

**States:** `unknown -> probing -> healthy | unhealthy`; a healthy DID can become `suspect` and then
`degraded` by the fast path (7.3) or `degraded` by the slow path (below).

**Classification of a probed DID: a dial-count SPRT, not a day timeout.** A fixed-width interval
needs about 145 dials at r = 1.10, 240 at r = 0.64 and 415 at r = 1.0 (LCB or UCB against 0.85). The
old "3 dialling days, then unhealthy" timeout therefore dropped healthy DIDs: 415 dials at a 2.8%
probe on renewal-like volume (100 dials a day) is 4.1 days. Instead, per probed DID, Wald's SPRT of
`H_U: r = 0.70` against `H_H: r = 1.0` [T], on dials in event order, each dial using the comparator
pickup `p_s` of its (cohort, hour) stratum:

```
connected:      LLR += ln(1.0 / 0.70)                       = +0.357
not connected:  LLR += ln((1 - p_s) / (1 - 0.70 p_s))        = -0.100 at p_s = 0.26
alpha = P(call healthy | r = 0.70) = 1% [T];  beta = P(call unhealthy | r = 1.0) = 5% [T]
healthy    when LLR >= ln((1 - beta) / alpha) = ln 95  = +4.55
unhealthy  when LLR <= ln(beta / (1 - alpha)) = ln 0.0505 = -2.99
truncation at N_max = 1,000 dials [T]: classify healthy iff r_hat >= 0.85 (the indifference point,
           where either answer is harmless)
```

Expected dials to a decision, `E[N] = boundary / |drift|`, drift `= p_d x 0.357 - (1 - p_d) x 0.100`
at `p = 0.26`:

| True r | Drift per dial | E[N] dials | Decision |
|---|---|---|---|
| 1.60 (fresh probe vs one overloaded incumbent, single-DID workflows) | +0.077 | about 59 | healthy |
| 1.10 (fresh, unloaded, vs lightly loaded peers) | +0.0305 | about 149 | healthy |
| 1.00 | +0.0186 | about 245 | healthy (95%) |
| 0.84 (indifference, worst case) | 0 | about 380 (`A x abs(B) / var`) | either |
| 0.64 | -0.0242 | about 123 | unhealthy |
| 0.36 (flagged) | -0.0574 | about 52 | unhealthy |

**Probe size from the DID harm gate, then from volume** (A). An untested DID has prior
`P(flagged) = 0.2` [T] with relative pickup `r_flag = 0.35` [T] if flagged. With probe share s taken
pro rata from the others, `dd ~ s (r_d - 1)`:

```
one unknown DID:  P(flagged) = 0.2 > 5%, so the probe must be safe even if flagged:
                  s x (1 - 0.35) <= 0.018   ->   s_gate = 2.8%
two unknown DIDs: P(both flagged) = 0.04 <= 5%; the exactly-one case must be safe -> 2.8% each
three or more:    probe at most two per change (P(at least two of three) = 0.104 > 5%)
volume:           s = max(0.5%, min(s_gate, 400 / dials_per_day)) [T]
                  400 dials a day is enough for the r = 1.0 case (245) inside one dialling day
```

Time to classify per workflow (dials per day from the API; workflow labels are sanity checks):

| Workflow shape | Probe share | Probe dials per day | r = 1.10 | r = 1.00 | r = 0.64 |
|---|---|---|---|---|---|
| renewal-like, 3,600 dials/day, `mid_flight` allowed | 2.8% (gate binds) | 101 | 1.5 days | 2.4 days | 1.2 days |
| cancel-like, 4,800 dials/day | 2.8% (gate binds) | 134 | 1.1 days | 1.8 days | 0.9 days |
| KYC-like, 2,000 dials/day, `next_campaign_only` | 2.8% | 19, 38, 56, 56, ... (only new campaigns carry the probe; cumulative 19, 57, 113, 169, 225) | on D4 | on D6 | on D4 |
| public_03 workflow, 54,000 dials/day | 0.74% (volume binds) | 400 | 0.37 days | 0.6 days | 0.3 days |

The KYC-like row is the slow case and is stated as such: classification takes 3 to 5 days after the
approval, so the spread there arrives late, unless the incumbent is a single overloaded DID, in which
case a healthy probe shows r of about 1.6 and classifies in about 59 dials (end of D2 or early D3).

**Slow-path reclassification** (healthy -> degraded). At planning points, for every healthy DID,
the UCB rule on trailing windows of 12, 24 and 48 dialling hours [T]: degraded if the upper 95% bound
of `r_d` is below 0.85 in any window. This covers the band the fast detector's 0.75 floor leaves
open (7.3). Detection time at a one-third share, cancel-like volume, `p = 0.25`: r = 0.75 in about 12
dialling hours (12h window, `SE = 0.060`, UCB 0.844); r = 0.80 in about 4 dialling days (48h window,
`SE = 0.029`, UCB 0.847). A healthy DID at r = 0.90 (load imbalance) crosses with probability about
5e-5 per test. Response: a P3 spread off the DID, which is risk-reducing by definition (6.5), so it
may auto-apply and is exempt from the velocity budget.

**No healthy DID exists** (all unknown, all degraded, or a single DID that the drift detector, 7.5,
marks suspect): never move share onto unknown DIDs beyond the probe size, probe every unknown DID at
`s` (at most two per change), keep the current allocation otherwise, page `NO_HEALTHY_DID`, and show
it on the supervisor view. As soon as a probe classifies healthy, the normal spread runs.

**Spread.** Target = equal shares across healthy DIDs (with equal health, equal split maximises mean
health under any convex load penalty; stated assumption A-7, verified after the fact). Where healthy
DIDs differ, shares are proportional to `r_d` over the healthy set. The spread passes the DID harm
gate with posterior `r_d` values; moving share to lower-load DIDs is normally graded beneficial.
Unhealthy and degraded DIDs get 0; re-admission of a degraded DID is a probe at `s` after 3 days [T].

**Routing and timing.**
- `caller_id` auto-applicable: probes at the first step after bootstrap when nothing is in flight
  (inside the notice rules of the directive, 6.5); spread steps are exposure-increasing and limited
  by the velocity rule (cumulative TV per cohort over a rolling 24h at most `max_shift`, 6.5). The
  part of a spread that does not fit the velocity budget rides the **next routine approval**, which
  has no TV limit, so a two-step spread finishes at the next slot instead of 24h later.
- Approval only and `next_campaign_only` (KYC-like): probes go in the D0 batched approval and reach
  only new campaigns, so we classify from all dials on the probed DIDs regardless of campaign, and
  propose the spread at the first slot after classification. Fully live `len(cadence)` days later;
  the dry run says so.

### 5.10 Guardrail

**Definition** (assumption A-3, ADR 0007): `objective.guardrail.*_max_drop: x` is an **absolute**
drop of x in the guardrail rate (0.02 = 2 points), measured over a rolling 2-day window, against a
concurrent comparator where one exists (holdout, other arms, other DIDs), else against the trailing
3-day baseline adjusted by the model. Stricter than the limit: every proposal's dry run must predict
**no** guardrail drop (point estimate <= 0) and `P(drop > x) <= 2.5%` [T]. The relative reading (C)
is rejected: 2% of a 0.26 pickup is 0.5 points, which cannot be measured at dev volume, so it would
be unenforced.

**Composition effects.** Schedule and caller-ID changes change who connects, so on a KYC-like
workflow (pickup primary, engagement guardrail) the raw engagement rate can fall through the cohort
mix while no person's engagement changes. The guardrail is the raw rate (that is the KPI), so the
design models the mix explicitly:
- **Dry run:** predicted guardrail `E(w) = sum_k connects_k(w) e_k / sum_k connects_k(w)`, with
  `connects_k(w)` from the schedule expectation (5.8) and the DID factors (5.9), and `e_k` from the
  effect model. A schedule change that raises pickup by shifting connects toward low-engagement
  cohorts is caught here, before submit.
- **Monitoring:** the observed change in the raw rate is decomposed into a mix term and a
  within-cohort term, `delta_raw = sum_k (c'_k - c_k) e_k + sum_k c'_k (e'_k - e_k)`, with `c_k` the
  connect shares and `e_k` the cohort rates of the baseline window. The within term is the
  mix-standardised change.

**Monitoring power.** Power at dev volume: a pickup guardrail (p about 0.26, 7,200 dials in 2 days
vs a 50k-dial baseline) has SE 0.0055, so a 2-point drop is a 3.6-SE event; an engagement guardrail
on KYC (930 connected in 2 days) has SE 0.017, only 1.2 SE. That weak case is why engagement-affecting
experiments do not run on pickup-primary workflows, and why the dry-run prediction carries most of
the protection there.

**Breach.** If the lower 97.5% bound of the raw drop exceeds x and the drop is attributable to a
change we made inside the window, that change is rolled back as a P1 (risk-reducing). Attribution:
a concurrent comparison for allocation changes; for schedule and caller-ID changes, the mix term
explains at least half of the drop [T] and the mix shift started within 2 segments of our change.
Unattributable drift raises a `KPI_DRIFT` item (7.5) and triggers the DID and prompt checks, but
changes nothing by itself.

### 5.11 Threshold register

Every `[T]` in this document, with the rule for changing it: tune only on private variants written
by our test harness (random effects, decay shapes, policies, volumes), never on public truth. Values
live in `controller/settings.py` with a docstring citing the section that derives them, and
`settings_hash` is stored on every decision row.

| Threshold | Value | Derived from | Section |
|---|---|---|---|
| alpha_H exploration and promotion / rollback | 2.5% / 10% | grader 1% line; cost asymmetry | 0.1, 5.7 |
| alpha_D | 5% | grader 2% line | 0.1, 5.9 |
| Cohort harm line x_k | 2% default, 1% by directive | Compliance item 4; cost table | 0.1, 5.4 |
| tau_E, tau_H floor | 0.13 logit, 0.06 log | plausible lever sizes (README: +3% to +15%) | 5.3, 5.4 |
| EW half-life | 12 dialling hours | decay over days; about 1.7-day effective window | 5.3 |
| Arm floor, arm ceiling | 2% of cohort, 50% of cohort before a winner | keep every live arm learning; bound exposure | 5.4 |
| Bisection iterations | 12 | lambda resolution 1/4096 | 5.4 |
| Minimum exploitation gain | 0.25% of the lever KPI's daily numerator; exploration and risk-reducing moves exempt | slot economy | 5.4 |
| Harm stop | EW `P(theta < 0) >= 0.95` and expected loss >= 0.5% of scope engagement | fast exit from a clearly bad arm | 5.4 |
| Arm removal | `P(theta < 0) >= 0.8` | risk-reducing, planning points | 5.4 |
| Futility drop | `P(theta > ln 1.02) <= 5%` at a look | free traffic from flat arms | 5.4 |
| Winner W1 | 0.995 population, 0.998 cohort | false-winner table | 5.5 |
| Winner W2, W3 | 0.95; 0.90 and 0.5x | one winner per scope; recency | 5.5 |
| Evidence floor and look schedule | 200 over 2 days, doubling | false-winner bound | 5.5 |
| Targeting cohort minimum | 5% of daily leads | about 200 history dials per hour cell | 5.2 |
| Winner-scope minimum | 15% of daily connected and projected n = 800 look before the freeze | real ramp table | 5.2, 5.5 |
| Holdout grid and SE target | {5, 10, 15, 20}%; 0.075 | one-day decay visibility | 5.6 |
| Decay CUSUM k, h, arming share | 0.5, 4, 10% | ARL table | 5.7 |
| Rollback tolerance | 10% | 5.7 | 5.7 |
| Hour-curve shrinkage | 200 dials per (cell, hour) | history volume per targeting cohort | 5.8 |
| Weight sharpness and clip | kappa 2; [0.25, 4] | dialler ranks by weight; capacity owned by business | 5.8 |
| Schedule refresh threshold | L1 distance 0.1 | slot economy | 5.8 |
| Schedule verification | band cells with >= 30 dials; fail if > 10% below band | 5.8 | 5.8 |
| Grid features | F1 deviance test p < 0.001; F2 = lang if 2 to 5 languages each >= 10% | 5.2 | 5.2 |
| DID prior | P(flagged) 0.2, r_flag 0.35 | harm gate | 5.9 |
| DID SPRT | H_U r = 0.70, H_H r = 1.0, alpha 1%, beta 5%, N_max 1,000 | E[N] table | 5.9 |
| Probe sizing | max(0.5%, min(2.8%, 400 / dials_per_day)) | DID gate; 245-dial r = 1.0 case | 5.9 |
| DID slow path | UCB95 of r_d < 0.85 in a 12, 24 or 48 dialling-hour window | 0.75 to 0.85 band | 5.9 |
| DID re-admission | probe after 3 days | load decay | 5.9 |
| DID fast detector | windows 2, 4, 8 h: z <= -4, r <= 0.75, >= 100 dials; suspect: 1 h window, z <= -2.5 | FPR table | 7.3 |
| DID drain verification | workflow pickup >= 0.9 of the pre-incident 24h at the same hours, 2 dialling hours after the last step | 7.3 | 7.3 |
| Drain precedence | wait for a P1 to P3 holder if its worst case is <= 60 min; chain-first if (k - 1) x tick <= 30 min | 6.2 | 6.2, 7.3 |
| Guardrail | absolute x over 2 days; predicted drop <= 0 and P(drop > x) <= 2.5%; mix term >= half for attribution | A-3 | 5.10 |
| KPI drift CUSUM | k 0.5, h 6 per dialling hour block, armed after 2,000 baseline dials | ARL table | 7.5 |
| Prompt verdict | stage 1 n = 300, UNSAFE at f1 >= 10, SAFE (sequential) at f1 <= 3; pooled n = 1,300, UNSAFE at >= 26 | 7.1 table | 7.1 |
| Gate-evidence run | designated before running; n_e from the n grid maximising P(pass); "tight gate" if that P < 0.95 | 7.1 | 7.1 |
| Fix verification | 300 connected; flag UCB95 < 2 p0; engagement LCB95 >= -5% | 7.1 | 7.1 |
| Flag CUSUM | p1 = 2 p0, h = ln(30 days of calls), >= 5 flags | 7.1 | 7.1 |
| Cross-workflow flag check | ratio >= 1.6 and Poisson z >= 4, workflows with >= 2,000 history connected | F7 | 7.1 |
| Credit pools | R = min(0.8 C0, max(0.3 C0, 2 W)); S = min(0.25 C0, C0 - R); W = 13 K | 7.2 | 7.2 |
| Submit expiry probability | <= 1% | 6.3 | 6.3 |
| Routine slots | slot1 = max(approver open, not_before) or open + min(60, 25% of approver window); slot2 = slot1 + 240 | 6.3 | 6.3 |
| Cap-1 routine spacing | no routine request if one of ours in the last 48h | F2 | 6.4 |
| Supervisor cooldown | 24h | 6.7 | 6.7 |
| Late-decision re-check | 20h after submit | 6.3 | 6.3 |
| Held-patch auto-release | re-check after 6 sim hours; dropped after 2 holds | 6.1 | 6.1 |
| Allocation verification | 2 segments; realised shares within 3 SE; EW P(de < -1%) <= 10% | 6.1 | 6.1 |
| Suspect deferral | P2/P3 submits deferred up to 2 h while a DID is suspect | 6.2 | 6.2 |
| Freeze | last max(1 day, 15%) | 4.2 | 4.2 |

---

## 6. Governance client

### 6.1 Proposal state machine

```
                 planner emits patches -> DRAFT --(no net change)--> DISCARDED
                                            |
                          gates ok (harm gate, guardrail, local policy, timing)
                       approval route       |            auto route
                  +-------------------------+--------------------------+
                  v                                                    v
            SUBMITTING                                      NOTICED (notice_minutes, if any)
   409 dup id -> reconcile by proposal_id                       | hold set -> DISCARDED
   409 stale  -> STALE                                          v
   422        -> FAILED (page)                            AUTO_APPLYING
                  | 201                                    200 -> APPLIED
                  v                                        403 -> FAILED_POLICY (page; re-route to approval)
               PENDING --rejected--> REJECTED              409 stale -> STALE
                  |    --expired---> EXPIRED               409 dup id -> reconcile via config history
                  |    --higher class in flight--> SUPERSEDED (never applied, even if approved later)
                  |    --base changed (supervisor or our P0 drain)--> STALE (never applied)
                  | approved
                  v
               APPROVED --base_version != current--> STALE        (apply never called: no apply_conflict)
                  |     --now > apply_by - tick----> LAPSED_LOCAL (apply never called: no 410)
                  |     --decided > 20h after submit and gates now fail--> ABANDONED (audited, shown)
   (auto route: AUTO_APPLYING 200 -> APPLIED -> VERIFYING as below)
                  | pre-apply checks ok
                  v
               APPLYING --200--> APPLIED --> VERIFYING --> VERIFIED | ROLLBACK (new P1 proposal; checks per kind below)
                        --409 not_approved--> reconcile (already applied)

REJECTED, EXPIRED, STALE, LAPSED_LOCAL, SUPERSEDED, ABANDONED --> REQUEUE:
   a NEW proposal (new proposal_id, fresh data, rebased on the live config, requeued_from = old id)
```

| Transition | Rule |
|---|---|
| DRAFT -> SUBMITTING | Window open (6.3), fatigue below the class budget (6.4), `one_in_flight` free or this proposal's class strictly higher (then the holder becomes SUPERSEDED in the same transaction; the one same-class exception is a P0a bundle replacing a pending P0b approval, 6.2), P2 and P3 only at an exact slot minute (6.3), no hold, `base_version` equal to a fresh `GET /workflows/{id}/config` version. |
| PENDING -> APPROVED | From the `approval_decided` event or a poll; store `apply_by`. |
| APPROVED -> APPLYING | Only if `now <= apply_by - tick` and `base_version == current` (fresh GET). |
| REJECTED, judgement call | P2 and P3: re-queue once at the next routine slot with fresh data; a second rejection of the same patch set drops it and shows "needs discussion". **P0 and P1: resubmit immediately** (the next stop, new id, fresh data, rebased), within `C_policy`; this costs one more request and one more approver latency, and it is never deferred to a routine slot. |
| REJECTED, fatigue, evidence or format reason | A local check is wrong: page (`GOV_INVARIANT`). The patch set is held (`patch.held_until_minute = now + 6 sim hours` [T]); at the first planning point after that it is **automatically re-checked**: `used()` recomputed from `GET /approvals?workflow_id=`, the policy re-read from `GET /workflows/{id}`, evidence re-validated against our verdict store. If every check now passes, the patch set is released into the overlay; if not, it is held again; after 2 holds it is dropped with "needs discussion". No human step is required in a graded run. |
| EXPIRED | Product: a rejection. Re-queue with fresh data, new id, new base; never resubmit the old body. Should not happen (6.3); an expiry pages. |
| STALE | Rebase (6.7); non-conflicting patches return to the overlay. |
| SUPERSEDED | Recorded as "withdrawn by controller"; its approval slot stays used; if approved later it is never applied. |
| APPLIED -> VERIFYING -> VERIFIED or ROLLBACK | Every applied change is verified by a kind-specific check, stored in `proposal.verify`: **prompt fix** 7.1; **DID drain or spread** 7.3; **schedule** 5.8; **allocation changes** (exploration, ramps, removals, promotions) over the next 2 segments [T]: (a) realised shares per cohort and arm within 3 binomial SE of the configured shares (the config took effect; on `next_campaign_only`, counted on new campaigns only); (b) for each arm whose share rose, arm vs control on those 2 segments lies inside the dry run's 90% interval or above it; (c) the EW posterior of the change (new vs previous config, updated with the new data) still has `P(de < -1%) <= 10%` and passes the cohort line. Failure of (b) or (c) is a **harm stop** (5.4): a P1 risk-reducing patch reverting that change's increases. Failure of (a) only pages (`COHORT_MISMATCH`). |

**Proposal ids** are deterministic: `{run6}-{wf}-D{d}T{HHMM}-{class}-{seq}`, `seq` allocated in the
DRAFT transaction. A crashed and restarted run uses the same ids, so `duplicate_proposal_id` is a
reconciliation signal, never a new request.

### 6.2 One change in flight, priority classes, and precedence

The `one_in_flight` index (3.2) holds from SUBMITTING or NOTICED until a terminal state. Any version
bump kills a pending approval at apply (world.py:448-450), **including our own auto-apply**, so an
auto-apply never runs while an approval is in flight unless the rules below supersede it first.

Classes: **P0 incident** > **P1 rollback** (decay, guardrail breach, harm stop, failed
verification) > **P2 promotion** > **P3 routine** (exploration, ramps, probes, spread, schedule).
Inside P0 there is a fixed order: **P0a prompt safety** (hallucination fix: a compliance failure)
before **P0b DID drain** (a pickup loss). The general rule is that a proposal may supersede the
holder only if its class is strictly higher (ADR 0013); the only same-class move is P0a replacing a
pending P0b approval by a **bundle** that keeps the drain, so no incident work is ever dropped.

| Situation | Rule |
|---|---|
| P1 needed while P2 or P3 in flight | Supersede (same transaction) and submit or auto-apply the P1. |
| P2 or P3 needed while anything in flight | Wait for the holder to finish. |
| A DID is `suspect` (7.3) | P2 and P3 submits on that workflow are deferred up to 2 hours [T] until alarm or clear, so a drain does not have to supersede a fresh routine request. |
| P0b drain needed while P1 to P3 in flight, caller_id auto-applicable | If the holder's worst-case remaining time to decision plus apply is at most 60 min [T], wait and drain right after its apply; else supersede it and start the drain chain. |
| P0b drain needed while a **P0a prompt-fix approval** is pending | Never stale or supersede it (same class, higher rank). No drain auto-apply on that workflow while it is pending; the drain chain starts in the step the approval becomes terminal (applied, rejected, expired). The DID's extra exposure is bounded by the remaining latency, at most `lat_hi + 2 x tick`; above 2 hours it pages `INCIDENT_UNRESOLVED` (known weakness, 13.4). |
| P0a prompt fix needed while a **P0b drain chain** is running (auto-apply) | Let `k_rem` = remaining chain steps. If `(k_rem - 1) x tick <= 30 min` [T], finish the chain (one step per tick) and submit the prompt fix in the step of the last chain step, right after it, on the post-drain base: MTTR grows by `(k_rem - 1) x tick`. Otherwise stop the chain and submit **one bundled P0 approval**: prompt fix plus the rest of the drain. |
| Both incidents detected in the same step | Same rule with `k_rem = k = ceil(share / max_shift)`. At `max_shift = 0.2` and a share up to 0.4, k = 2: chain first, MTTR + 15 min. At `max_shift = 0.05`, k = 7: bundle (the chain would add 90 minutes to MTTR, while the bundle drains after one approver latency, mean about 70 + 22 minutes at latency 20 to 120). |
| P0a prompt fix needed while a **P0b drain approval** is pending (approval-only workflow) | Supersede it with one bundled P0 approval (prompt fix plus drain) in the same transaction: the only same-class supersede, and it keeps the drain. |
| Approval-only workflow, both needed at once | One bundled P0 approval. |
| Supervisor edit while anything is pending | The pending approval is already stale (version bumped); mark STALE, rebase. |

Batching: one proposal per workflow carries the whole desired change set (cohorts, schedule, caller
ID, allocations, prompt), one plain-language line per item ("Voice: try V3 on 4% of calls (new
test)"; "Caller ID: move 10% from D1 to D3"). Kestrel: "one request with five things in it".
Auto-applicable operational patches are applied in the same planning step **immediately before** an
approval is submitted, so the approval carries the new base and the lock is never violated (B).

### 6.3 Timing: submit-window search

The approval's outcome, decide minute and expiry are fixed at submit (world.py:461-494):
`decide = next_working(t + L)`, L uniform on the integers `[lat_lo, lat_hi]`, and it expires iff
`decide > t + 60 x TTL`. So the expiry probability of a submit at minute t is exact and computable
from API parameters (F3):

```python
def p_expire(t: int, pol: EffectivePolicy) -> float:
    lo, hi = pol.latency_minutes
    ttl = 60 * pol.ttl_hours                      # expires_in_hours left at the policy TTL (F11)
    bad = sum(1 for L in range(lo, hi + 1)
              if next_working(t + L, pol.approver_hours) > t + ttl)
    return bad / (hi - lo + 1)

def safe(t: int, pol: EffectivePolicy, used: Sequence[int], cls: int) -> bool:
    return (p_expire(t, pol) <= 0.01                                  # [T]
            and fatigue_ok(t, pol, used, cls))                        # rolling count at t, strict

def routine_slots(d: int, pol: EffectivePolicy) -> list[int]:
    """Fixed submit minutes for P2 and P3 on sim day d. Deterministic, so the rolling-24h count
    stays exact: yesterday's slot request stops counting at exactly the same minute today."""
    s1 = d * 1440 + pol.preferred_slot_min                            # 6.3 "Preferred slot"
    if p_expire(s1, pol) > 0.01:                                      # no in-hours slot is safe
        pre_open = [t for t in range(d * 1440, d * 1440 + pol.approver_open_min, pol.tick)
                    if p_expire(t, pol) <= 0.01]
        s1 = max(pre_open) if pre_open else None                      # tier 3: latest safe pre-open
    if s1 is None:
        return []                                                     # alert NO_SAFE_WINDOW
    slots = [s1]
    if pol.routine_budget >= 2 and p_expire(s1 + 240, pol) <= 0.01:   # slot 2 [T]
        slots.append(s1 + 240)
    return slots

def choose_submit(now: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
                  slot1_terminal: bool) -> int | None:
    if cls in (PROMOTION, ROUTINE):
        # ENFORCED: only the exact slot minutes; never "any tick at or after the slot"
        for d in (day(now), day(now) + 1):
            for i, t in enumerate(routine_slots(d, pol)):
                if t < now or (i == 1 and not slot1_terminal):
                    continue
                if pol.not_before_min is not None and t % 1440 < pol.not_before_min:
                    continue                                          # hard directive (Kestrel 11:00)
                if safe(t, pol, used, cls):
                    return t
        return None
    # P0 and P1: any tick in the next 24h; in-hours first, then the latest safe pre-open time
    cands = [t for t in range(now, now + 1440, pol.tick) if safe(t, pol, used, cls)]
    inside = [t for t in cands if in_approver_hours(t, pol)]
    if inside:
        return min(inside, key=lambda t: (expected_apply_minute(t, pol), t))
    outside = [t for t in cands if not in_approver_hours(t, pol)]
    if outside:
        return min(outside, key=lambda t: (expected_apply_minute(t, pol), -t))
    return None                                                       # alert NO_SAFE_WINDOW
```

`choose_submit` has this one signature everywhere (11.2). The step rule (4.2) puts every routine slot
minute in the stop boundaries, so the slot is always hit exactly. `expected_apply_minute(t)` averages
`next_working(t + L)` plus the visibility lag over L, and adds 1440 for `next_campaign_only`
allocation changes whose decision misses the next daily batch. The pre-open slot (Compliance:
"requests sent at night sit in a queue") is used only when no in-hours slot is safe, and then the
latest safe time before opening wins because it minimises queue time.

Worked cases (values read from the API at runtime; these are sanity checks of the formula):

| Policy | Result |
|---|---|
| Approvers 10-19, latency 30-240, TTL 24 (renewal-like) | Every in-hours time has p = 0; same-day decisions for submits up to 14:45. |
| Approvers 10-18, latency 60-480, TTL 24 (KYC-like) | 10:00: p = 0 and the decision lands the same day except `L = 480` (1/421). |
| Approvers 10-18, latency 60-480, **TTL 12** (J2 case) | 10:00: only `L = 480` reaches 18:00 and rolls over: p = 1/421 = 0.24%, allowed. 10:15: p = 16/421 = 3.8%, refused. Pre-open 06:00 to 09:45: p = 0 (pre-open slot, used only if 10:00 is not safe). |
| Approvers 10-19, latency 20-120, TTL 12 (cancel-like) | Safe through 16:45 (p = 0); 17:00 has p = 1/101 = 0.99%, allowed; 17:15 has p = 15.8%, refused. |
| Approvers **14-16**, latency 30-240, **TTL 4** (J2 case) | No in-hours time is safe (14:00: p = 121/211 = 57%). Pre-open: 11:45 has p = 0, 12:00 has p = 1/211 = 0.47%, 12:15 has p = 7.6%. Routine slot of the day: 12:00. |
| Approvers 10-19, latency 30-240, **TTL 4** | In hours: safe through 14:45 (p = 0; `t + L` stays before 19:00), 15:00 has p = 1/211 = 0.47%, 15:15 has 7.6%; pre-open 06:00 to 09:45 is safe. Routine slot 11:00 (p = 0). |

**Preferred slot.** `preferred_slot(day) = max(approver_open, directive.not_before)` for workflows
with a directive, and `approver_open + min(60, 25% of the approver window)` rounded down to the tick
for the `*` default (C's generic reading of "never before 11:00"; approvers 14-16 give 14:30, not a
slot in the last hour). Slot 2 (if the routine budget is 2) is at `slot1 + 240` [T] if slot 1's
proposal is terminal by then, else skipped that day. Fixed minutes keep the rolling-24h count exact under the
simulator's strict inequality (`created_minute > now - 1440`, world.py:477-481): yesterday's 10:00
request stops counting at exactly 10:00 today (B).

**Late decisions.** If an approval is decided more than 20h [T] after submit (possible with TTL 24h
and an evening submit), the harm gate and dry run are re-run on fresh data before applying; if the
change no longer passes, it is ABANDONED (audited and shown), never applied.

**Apply deadline.** Guaranteed by the step rule (4.2); `apply_by - tick` is also a stop boundary.

### 6.4 Fatigue caps, voluntary caps and slot reservation

```
C_policy  = governance.max_requests_per_24h                       (API)
C_dir     = directive.max_requests_per_day (if any)
C_eff     = min(C_policy, C_dir)
reserve   = 1 if C_eff >= 2 else 0                                (F2; B)
routine   = C_eff - reserve                                       (budget for P2 and P3)
if C_eff == 1: a P2 or P3 request only if no request of ours in the last 48h [T]
used(t)   = our requests for wf with created_minute > t - 1440    (every status counts)
may_submit(cls, t):
   P0, P1:   used(t) + 1 <= C_policy          (may exceed C_dir; the view says so)
   P2, P3:   used(t) + 1 <= routine
```

With `C_eff = 1` a routine request on alternate days leaves the single slot free for incidents half
of the time, and still ships the D0 foundation (cohorts, schedule, DID probes), which a `cap - 1`
rule never would. A cap is never exceeded on purpose: an over-cap request is accepted as pending,
burns a slot and is certain to be rejected. `used()` is computed from our intent log and
cross-checked against `GET /approvals?workflow_id=` `created_at` every planning point.

### 6.5 Directives, auto-apply policy and routing

**Directives** (`controller/directives.yaml`, loaded into the `directive` table at start for audit).
These are client agreements from the memos, not simulator data. **Effective policy is always the
stricter of the API policy and the directive**: a directive can only tighten.

**Scope of a directive.** A directive is keyed by `(tenant_id, workflow selector)`. The selector
matches on `workflow_id` or on a case-insensitive keyword in the API's `use_case` string; the most
specific matching directive applies, then the tenant-wide one, then `*`. Who wrote the memo decides
the scope (assumption A-11): Northwind's Compliance head speaks for the tenant, so her conditions are
tenant-wide (and they only tighten); Kestrel's operations supervisor runs **the cancellation line
only**, so her asks apply to Kestrel workflows whose `use_case` contains "cancel". public_03 Kestrel
renewal and KYC workflows therefore get the `*` default, not a 2-per-day cap and an 11:00 rule.

| Scope (tenant, selector) | Directive | Source |
|---|---|---|
| `northwind_life`, all workflows | `customer_facing_auto_apply: none`; `cohort_harm_line: 0.01`; weekly operational digest | memo 02 items 1, 4 and 5 |
| `kestrel_finance`, `use_case ~ "cancel"` | `max_requests_per_day: 2`; `not_before: 11:00` (hard, routine); `notice_minutes: 60` before any non-incident auto-apply (operational or customer-facing), posted inside approver hours; `customer_facing_auto_apply: ramps_and_risk_reducing`; `avoid_prompt_terms: ["verbatim", "word for word"]` | memo 04 |
| `*` (any workflow without a more specific directive, e.g. hidden scenarios and Kestrel's other lines) | `customer_facing_auto_apply: risk_reducing_only` (arm removals, rollbacks, harm stops); `notice_minutes: 60` before customer-facing auto-applies; `cohort_harm_line: 0.02`; preferred slot `approver_open + min(60, 25% of window)`; pre-open submits only when no in-hours time is expiry-safe | design default, ADR 0012 |

Customer-facing dims are `welcome_message`, `voice` and `prompt_version`; caller ID and schedule are
operational, as the Northwind memo itself describes them. Why the `*` default allows risk-reducing
customer-facing auto-apply but not ramps: moving traffic back to the incumbent the client already
runs reduces exposure and delay costs customers, while exposure increases on an unknown client are
exactly what a compliance head would want to see first. Cost: ramps on unknown tenants wait for the
routine slot; the harm gate limits each ramp step anyway (5.4).

**Policy-vs-memo conflict, flagged (F15).** public_03 `nort_cancel_2` permits `welcome_message` and
`voice` auto-apply, while Northwind Compliance requires a named human approval for any
customer-facing change. The directive wins (we do not auto-apply there), the supervisor view shows
"policy permits, client agreement forbids: routed to approval", and the Compliance reply asks
Northwind to align the signed policy with the memo.

**Local policy replica** (`governance/policy.py`, ADR 0013): `normalise` (weights normalised, zero
weights dropped), `changed_dims` exactly as world.py:182-196 (cohort list compared in order, per-dim
distributions compared with the `default` fallback over the union of allocation keys, any schedule
difference including `lead_preferred_hour`), `max_shift` as unweighted TV per cohort per agent dim
over `default` plus every old and new cohort id (world.py:200-209), non-empty rationale. A proposal
auto-applies only if all hold:

1. `auto_apply.enabled` and every changed dim in `allowed_dims`;
2. `max_shift(current, proposed) <= max_shift - 1e-6`;
3. the directive permits it for these dims and this change kind;
4. velocity (B): the cumulative TV of exposure-increasing auto-applies per (cohort, dim) over a
   rolling 24h stays within `max_shift`, so exploration and spreads are never chained around the
   cap; risk-reducing changes are exempt but bounded (below);
5. `one_in_flight` free (or superseded per 6.2);
6. no hold, and the notice period (if any) has elapsed, with the harm gate re-run at notice expiry.

Everything else routes to approval. We **never probe auto-apply by trial and error**: every 403 is
logged and graded. A 403 means the replica is wrong: page, re-route to approval, never retry the body.

**Risk-reducing vs exposure-increasing, defined** (ADR 0025). The label is computed by
`governance/policy.classify_risk(current, proposed, did_state, controls)` on the composed config,
per changed (cohort, dim). It is **never** taken from the emitting module: `PatchRequest` carries no
risk flag (11.2). A change is risk-reducing only if every changed (cohort, dim) is:

| Dim | Risk-reducing iff | Everything else is exposure-increasing, for example |
|---|---|---|
| `welcome_message`, `voice` | every option whose share rises is that cohort's control (the incumbent default option, or the promoted winner for its holdout scope), and no option enters | exploration, ramps, promotions, a new arm |
| `caller_id` | share leaves only DIDs in state `suspect`, `degraded` or `unhealthy`, and every DID whose share rises is `healthy`; no `unknown` or `probing` DID gains | probes, spreads among healthy DIDs, re-admission |
| `schedule` | the new table equals a table that was live on this workflow in the last 7 days (a revert) | any new table |
| `cohorts`, `prompt_version` | never (approval only on every public policy; P0 prompt fixes go by approval) | - |

**Chaining, bounded.** A risk-reducing auto-apply may follow the previous one at the next tick (a
chain) only when it is P0 or P1 (drains, rollbacks, harm stops, failed verifications). Each step
moves at most `max_shift` TV and strictly reduces the share being removed, so a chain on one
(cohort, dim) ends after at most `ceil(share / max_shift)` steps, and it can never be followed by an
exposure-increasing auto-apply on that (cohort, dim) within 24h (the velocity window counts the
chain's TV). Risk-reducing P2 and P3 changes (arm removals, slow-path DID spreads) are exempt from
the velocity budget but are never chained: at most one per (cohort, dim) per planning point.

### 6.6 Impact score, affected leads, dry run

**Affected leads** (integer, as the API requires; B):

```
changed_cells = cells whose effective (cohort -> dim distribution), schedule table or prompt differs
if allocation dims and mid_flight == next_campaign_only:
    affected = daily_batch.size x share_hist(changed_cells)               (next batch only)
else:
    affected = active leads now in changed_cells (lead_dim.state = 0)
             + daily_batch.size x share_hist(changed_cells)               (next 24h)
```

The dry run also reports `leads_hearing_a_different_variant_24h = sum over cells of leads_cell x
TV_cell`, because a 4% probe touches every lead in scope but changes what only 4% of them hear.

**Dry run: stratified posterior replay** (F14; SNIPS cut):

```
Delta_primary = sum over cells c of m_c x sum over options v of (pi_new(v|c) - pi_old(v|c)) x e_rel(v|c)
  m_c      = next-24h connected mix per cell (from the last 3 days of r_engage)
  e_rel    = posterior draws of the cell-level relative effect (5.3, EW fit)
  -> mean, 90% interval, P_harm = P(de < -1%), Q20
caller ID: per-DID r_d posteriors -> predicted dd, P(dd < -2%); an unknown DID ranges over
           r in [r_flag, 1.1]
schedule:  analytic connects-per-hour expectation (5.8), plus the mix-adjusted guardrail
           E(w) = sum_k connects_k(w) e_k / sum_k connects_k(w) (5.10)
guardrail: every dry run reports the guardrail KPI under the new mix, never only the primary
snips:     cut (section 1)
```

```jsonc
{"method": "stratified posterior replay over recorded calls, cells weighted by next-24h mix",
 "window": {"from": "D2T00:00", "to": "D4T00:00", "connected": 1912},
 "primary":   {"kpi": "engagement_rate", "current": 0.553, "proposed": 0.557, "ci90": [0.551, 0.563],
               "p_harm": 0.004, "q20_rel": 0.003},
 "guardrail": {"kpi": "pickup_rate", "current": 0.262, "proposed": 0.262, "max_drop_abs": 0.02,
               "p_breach": 0.0},
 "components": [{"dim": "voice", "cohort": "age60p~3fa2c1", "option": "V3", "share": "0.04->0.10",
                 "evidence_connected": 88, "rel_effect": 0.14, "ci90": [-0.06, 0.34]}],
 "leads_hearing_a_different_variant_24h": 190,
 "impact": {"kpi": "engagement_rate", "q20_rel": 0.003},
 "assumptions": ["no welcome x voice interaction (A-1, not tested)", "hour does not change engagement"]}
```

**Impact score** (B, units, labelled per lever): `impact_score = clip(Q20(expected relative change
of the KPI THE LEVER MOVES over 7 days), 0, 1)`, stored with its label `impact_kpi`:

| Lever | `impact_kpi` | Value |
|---|---|---|
| welcome, voice | `engagement_rate` | Q20 of the relative engagement change from the dry run |
| caller ID (probe, spread, drain) | `pickup_rate` | Q20 of the relative pickup change from the DID posteriors |
| schedule | `pickup_rate` | Q20 of the relative pickup change from the analytic expectation |
| prompt fix | `engagement_rate` | measured deficit of the incident on our calls, `(flag_rate_now - flag_rate_baseline) x (e_unflagged - e_flagged) / e_now` |
| bundle | one score per lever, never summed | the request field gets the score of the highest-class item; the summary lists every label |

0.042 with label `pickup_rate` means "at least about +4.2% pickup on this workflow with 80%
confidence". So a DID spread on an engagement-primary workflow scores its real pickup gain instead of
0, and a prompt fix on a pickup-primary workflow scores the engagement it restores. A pure probe or a
first exploration share scores about 0, which is honest: it buys information. The supervisor queue
sorts by **class first, then impact**, and shows the KPI label next to every score; two KPIs are
compared only for ordering inside a class, never added or averaged (pushback on Product: an incident
with a modest score must still come first).

**Rationale and summary** are deterministic templates filled from decision rows: rule id, gate
values, sample sizes, links to looks and validation runs. No LLM text.

### 6.7 Supervisor edits: rebase, never revert

**Overlay model** (B). Desired config = live config + patches. A patch is owned by one module and
covers one **path**: `("alloc", cohort_key, dim)`, `("prompt",)`, `("hour_weights", cohort_key)` or
`("cohorts",)`. `compose()` deep-copies the live config, applies patches in a fixed order (cohorts,
allocations, schedule, prompt), normalises and validates locally.

On `config_changed` with `actor = supervisor`, version v:

1. Fetch and store the body of v. `sup_paths = paths(diff(body(v-1), body(v)))`. If v-1's body is
   unknown (two edits in one step), diff against the last known body and treat every differing path
   as supervisor-owned.
2. Every patch on a path in `sup_paths`, or depending on a removed cohort, becomes
   `invalidated_by_supervisor`; an experiment on that path becomes `INTERRUPTED_BY_SUPERVISOR` (data
   kept and labelled by version), and the supervisor's choice becomes the new control.
3. Every PENDING or APPROVED proposal with `base_version < v` becomes STALE (never applied).
4. **Cooldown 24h** [T] (a `cooldown` row per path, and `path_owner` set to `supervisor`): no P2 or
   P3 patch may touch a supervisor-owned path; afterwards a proposal
   touching it says so in its summary ("changes the voice your supervisor set on D5 at 15:00"). The
   exception is a prompt incident, which is the "supervisor shipped an unsafe prompt" case, and its
   summary says so plainly.
5. Rebase as intent (A): the supervisor's new values are the control. Cohort overrides that still mix
   in the old control get the new control substituted, except where it is credibly worse in that
   cohort (EW lower bound below -1%), which the proposal lists explicitly for the approver.

Sanity check (public_02): on D5 15:00 the Northwind supervisor sets default voice to V2. The voice
experiment is interrupted, nothing is reverted, other pending work is rebased at the next slot, and
after 24h a voice test may resume with V2 as control. V2 is in fact good for `ta`, so a blind revert
would have been harmful.

---

## 7. Incidents

### 7.1 Prompt safety (hallucination)

**Facts used** (SIMULATOR.md section 5, world.py:519-533): a validation run of n conversations
returns failures distributed binomially with an unknown per-prompt failure rate q; the approver
accepts a prompt change only with a run for that workflow and prompt whose `pass_rate >=
min_validation_pass_rate` (g, from the API). The grader treats any prompt with hallucination above
0.01 as unsafe; under the public mapping that is about `q > 0.014` (sanity check only; the
controller never assumes the mapping).

**Two separate questions** (F5, ADR 0009). "Is this prompt safe?" is **our safety verdict**, with a
bar of our own that does not depend on the client's gate g. "Can this prompt be approved?" needs
**gate evidence**: one run whose `pass_rate >= g`. At g = 0.97 one run answers both; at a stricter g
they come apart, so they are separate procedures.

**The safety verdict** (one procedure for every prompt we judge):

```
stage 1:  n1 = 300 (3 credits).  f1 = failures.
          f1 >= 10 [T]                    -> UNSAFE   (3.3% observed; fixed, independent of g)
          f1 <= 3 [T]                     -> SAFE     (sequential mode only, see below)
stage 2:  n2 = 1,000 (10 credits). F = f1 + f2 over N = 1,300.
          F >= ceil(0.02 N) = 26 [T]      -> UNSAFE   (pooled failure rate at least 2%)
          else                            -> SAFE
modes:    SEQUENTIAL (D0 screen of business defaults): stage 2 only if 4 <= f1 <= 9
          FULL (supervisor prompt changes, never-live fix candidates): stage 2 whenever stage 1 is not UNSAFE
          STAGE1_ONLY (low-credit mode, 7.2): SAFE iff f1 <= 9
cache:    prompt_verdict(wf, prompt) stores the verdict and every run id.
          A single pass at the client gate is never cached as SAFE.
anti cherry-picking: a prompt is never re-validated for the same decision; every run, pass or
          fail, is listed in the proposal evidence.
```

**Gate evidence** (only for a prompt we are about to propose):

```
P_pass(n) = P( Binom(n, q_ref) <= floor((1 - g) n) ),   q_ref = 0.01 [T] (a clearly safe prompt)
tight gate  iff  P_pass(300) < 0.95                      g read from the API; decided before any run
not tight:  the stage-1 run of the verdict is DESIGNATED as the evidence run before it runs
            (g = 0.97: P_pass(300) = P(Poisson(3) <= 9) = 0.999, so this is the normal case)
tight:      one separate evidence run is designated before it runs, at
            n_e = argmax over {50, 100, 200, 300, 500, 1000} of P_pass(n); cost ceil(n_e / 100)
rule:       exactly one designated run per (decision, prompt); only it is ever submitted as
            evidence; if it fails the gate, the prompt is SAFE by our bar but not approvable for
            this decision: move to the next candidate. None left: page CLIENT_GATE_UNREACHABLE,
            keep the incident open, and say on the supervisor view that the client's gate is above
            what a safe prompt passes reliably.
```

**What g >= 0.99 does** (computed at q = 0.0104, a P1-like safe prompt; sanity label). Its expected
pass rate is 0.9896, below 0.99 at every n, so no run size makes it pass reliably:
`P_pass(n)` = 0.59 (n = 50), **0.72 (n = 100)**, 0.66 (200), 0.62 (300), 0.58 (500). The gate is
tight (`P_pass(300) = 0.65 < 0.95` at q_ref = 0.01), `n_e = 100`, and the fix is approvable on the
first candidate about 72% of the time. Our verdict is unaffected: the old rule ("UNSAFE when the run
would fail the client gate") would have called this safe prompt UNSAFE 38% of the time at n = 300
and left the incident with no fix. At g <= 0.98 the gate is not tight (`P_pass(300) >= 0.966`).

Operating characteristics of the verdict (Poisson approximations, computed):

| Prompt failure rate q (sanity label) | Stage-1 UNSAFE | Sequential: final UNSAFE | Full: final UNSAFE | Expected credits (sequential / full) |
|---|---|---|---|---|
| 0.0104 (P1-like, safe) | 0.15% | about 0.3% | about 0.3% | 6.8 / 13.0 |
| 0.014 (grader boundary) | 1.1% | about 3% | about 5% | 9.0 / 12.9 |
| 0.0185 (inside the band below) | 7% | about 35% | about 40% | 10.3 / 12.3 |
| 0.0275 (P4-like, unsafe) | 31% | about 93% | about 96% | 9.5 / 9.9 |
| 0.0545 (P2-like, unsafe) | 96% | about 100% | about 100% | 3.4 / 3.4 |

The client gate alone would let a P4-like prompt through 69% of the time; the verdict lets it
through about 4% (full) to 7% (sequential) of the time, and the live CUSUM below is the backstop.
**The band our bar does not close** (13.4): a prompt whose validation failure rate lies between
about 1.4% and 2% (grader hallucination about 0.01 to 0.017 under the public mapping) passes FULL
about 95% of the time at q = 0.014 and about 60% at q = 0.0185. Moving the pooled bar to 1.4%
(`F >= 19`) would close most of it but would fail a P1-like safe prompt about 8% of the time instead
of 0.3%, which removes the last-known-good fix in 1 incident in 12; we keep 2% and state the band.

**Triggers.**

| Trigger | Action | Cost |
|---|---|---|
| `config_changed` with `prompt_version` in `changed_dims` and actor not us, to a prompt without a cached verdict | FULL verdict on the new prompt at the next step | 3 to 13 credits |
| D0 bootstrap (F7): every workflow's business-default prompt | SEQUENTIAL verdict from the screen pool S (7.2), workflows ordered by history flag rate descending; skipped in low-credit mode | about 6.8 per workflow expected |
| Cross-workflow flag check at D0 (free, F7): history flag rate `r_w` vs the lowest rate `r_min` among workflows with at least 2,000 history connected calls | `r_w / r_min >= 1.6` [T] and Poisson `z >= 4` [T] escalates w to a FULL verdict | free |
| Live flag CUSUM alarm (below) on a prompt with a SAFE verdict | A new FULL verdict whose runs are pooled with every earlier run of that prompt (no run is ever discarded, so this is not cherry-picking); independently, if the live flag rate's lower 99% bound exceeds `2 x p0`, the incident opens on live evidence alone | up to 13 credits |
| D0 screen UNSAFE on a business default | Incident (no LKG exists, so the ranked catalog path runs) | see fix selection |

**Live flag CUSUM** (free backstop, corrected per F6). Per workflow, Bernoulli CUSUM on connected
calls in event order, `p0` = the flag rate under the incumbent prompt (history, then live),
`p1 = 2 p0` [T]:

```
on flag:     S += ln(p1/p0)                    = +0.693
on no flag:  S += ln((1 - p1)/(1 - p0))         = -0.01503 at p0 = 0.0147
S = max(0, S); alarm when S >= h and at least 5 flags since the last reset
h = ln(30 days x connected per day) [T]          ks_cancel-like (1,200/day): ln(36,000) = 10.5
                                                  public_03 workflow (13,500/day): ln(405,000) = 12.9
```

Drift per call at a true flag rate p is `p x 0.693 - (1 - p) x 0.01503`, and the expected delay is
`h / drift`:

| True flag rate (sanity label) | Drift per connected call | Delay (calls) | Delay at 100 connected/hour |
|---|---|---|---|
| 0.0147 (incumbent) | -0.0046 | in-control ARL at least 36,000 calls (30 days) | - |
| 0.0298 (P4-like, about 2x) | +0.0061 | about 1,730 | about 17 dialling hours (1.4 days) |
| 0.0535 (P2-like, about 3.6x) | +0.0229 | about 460 | about 4.6 dialling hours |

So the CUSUM alone is slow for a doubling; the verdict on the prompt change is the primary detector
(MTTR about 2 hours), and the CUSUM catches what slips past it within 1 to 2 days. The CUSUM is reset
after every prompt change with `p0` = the new prompt's historical rate if it has one, else the
pre-change rate.

**Fix selection** (deterministic; the LLM never orders or picks):

```
1. last_known_good = the most recent prompt that was live on this workflow for >= 1 dialling day,
   whose live flag rate was not above its baseline (Poisson test p > 0.05), with a SAFE verdict
   (cached from D0 or obtained now with the SEQUENTIAL procedure).
   Evidence = its designated gate-evidence run (no extra credits if cached and the gate is not tight).
2. only if (1) does not exist: candidates = EVERY prompt in options.prompt_version from the API,
   minus the current one and any with an UNSAFE verdict. The rank is fixed before any validation:
     a. prompts matching the directive's avoid_prompt_terms go last
     b. prompts with live calls on this workflow first, by the stationary posterior mean of their
        engagement intercept pi[p] (5.3), highest first ("best live engagement")
     c. never-live prompts next, in the API's catalog order
   Validate in rank order (FULL verdict, then the designated gate-evidence run if the gate is
   tight) and stop at the first candidate that is SAFE and approvable. Because the rank is fixed
   before any result is seen, this picks exactly the prompt that "validate all, then take the
   top-ranked SAFE one" would pick, and it costs less.
3. none: page NO_SAFE_PROMPT (or CLIENT_GATE_UNREACHABLE), keep the incident open, change nothing
   blindly.
propose: P0 approval, config = CURRENT config (rebased on the supervisor's version) with
   prompt := candidate; evidence.validation_run_id = the designated run; every other run listed in
   the evidence body; summary in plain words, e.g.
   "Revert script to P1 after the 11:30 change: validation 25 failures in 300 (our limit 9);
    P1: 4 in 300 on D0 and 0.6 flags per 100 calls over 14 days."
```

**Why `suggest-fixes` is not called by default.** The contract allows only prompts listed in
`options.prompt_version` in a proposal or a validation run (`422 bad_prompt_version` otherwise), and
their one-line summaries are already free in `GET /workflows/{id}`. So the endpoint can only return a
subset of the candidate set in step 2, for 3 credits, and in the simulator that subset is a random
3 of the other catalog prompts (world.py:534-541), so one candidate can be missing and its order is
noise. Spending credits on it changes no decision (CTO: "the best compute is no compute"). The call
stays implemented behind `CTL_LLM_SUGGEST=1` for production, where an LLM can write new prompt text:
its suggestions then join tier c after the catalog prompts. Either way the LLM only proposes;
constraint 4 holds. This is recorded as a trade-off (12.3).

The known-good prompt comes first because it has days of real engagement data; Kestrel's
"compliance script word for word" fix is safe but disengaging, so validation cannot catch it, only
live engagement can.

**Stated tension with the change grader, and the gate exemption.** P0 prompt fixes are the one
change exempt from the engagement harm gate of 0.1 (they still need the safety verdict, gate
evidence and the guardrail check). When the unsafe prompt has a higher engagement multiplier than
every safe one (a P4-like prompt), the safety fix lowers expected engagement and the grader's change
verdict may label it harmful (sanity check: P4 to P1 is about -4.8%). We apply it anyway:
hallucination in a regulated call is a compliance failure, the grader's incident clock and oracle also
treat such a prompt as unsafe, and the summary tells the approver the engagement cost. The rank in
step 2 (best live engagement first) minimises the drop among safe candidates.

**Post-apply verification.** Verified when 300 connected calls under the fix show a flag rate whose
upper 95% bound is below `2 p0`, and 24h engagement versus the 72h before the incident (matched by
hour) has `LCB95(relative) >= -5%` [T]. Otherwise the incident reopens and the next candidate goes
as a P0 proposal; a page fires.

**Incident MTTR, predicted.** The edit at minute `t_e` is seen at the next stop (delay `D` in
`[0, S]`, S the step in force, 60 in hours). Stage 1 fails in that step, and the P0 approval is
submitted in the same step (LKG cached). The approver decides at `d = submit + L`, L uniform on
`[lat_lo, lat_hi]`. The decision is processed at `ceil_to_tick(d)` and visible after the advance that
ends at `ceil_to_tick(d) + tick` (4.2), and fast-apply steps (S = tick) apply it at that stop, so the
visibility lag is `V = ceil_to_tick(d) - d + tick`, in `[tick, 2 tick - 1]`, mean about 1.5 ticks:

```
MTTR = D + L + V
public_02 shape (edit 11:30, stop 12:00, latency 20 to 120, tick 15): D = 30, V in [15, 29]
   min 30 + 20 + 25 = 75 min (L = 20: decision 12:20, processed 12:30, visible 12:45)
   max 30 + 120 + 15 = 165 min (decision 14:00 exactly; any L from 106 to 120 lands on 14:15)
   mean about 30 + 70 + 22 = 122 min
general, latency 20 to 120: D in [0, 60] -> 35 to 209 min
```

A judgement-call rejection (probability `base_reject_rate`) is resubmitted at the next stop, not at a
routine slot (6.1), and adds about `V + L` more (mean about 92 minutes at latency 20 to 120). If a
drain chain runs first (6.2), add `(k - 1) x tick`.

### 7.2 Credits

Ordered pools, filled once at start in this order and persisted in `credit_envelope`; every spend
names its pool in `credit_ledger`.

```
C0 = credits_remaining at start (API)
K  = largest prompt catalog among the workflows (API; 5 in the public scenarios)
W  = worst incident = 13 (FULL verdict on the new prompt) + 13 x (K - 1) (FULL verdicts on every
     candidate, 7.1) + (K if the gate is tight, one 1-credit evidence run per candidate)
     K = 5, gate not tight: W = 65
1. R  incident reserve = min(0.8 C0, max(0.3 C0, 2 W)) [T]
      shared across tenants; spent only after a detector fired: the FULL verdict on a supervisor's
      new prompt (that verdict IS the detector, so R pays it), CUSUM-triggered verdicts, LKG verdicts
      made during an incident, fix candidates, evidence runs
2. S  D0 screen pool = min(0.25 C0, C0 - R)
      split across tenants by workflow count (the C15 envelope); unspent screen credit returns to U
3. U  unallocated = C0 - R - S; tops R back up to W after an incident
mode, re-evaluated before every incident spend:
   NORMAL if R + U >= W
   LOW    otherwise: every verdict is STAGE1_ONLY (3 credits; SAFE iff f1 <= 9), W_low = 3 K (+ K if
          tight) = 15; D0 screens are skipped except for workflows the free cross-workflow flag check
          escalates. LOW holds from the start whenever 0.8 C0 < W, that is C0 < 82 at K = 5.
402 = page; stop all non-incident spend; the DID path needs no credits
```

| C0 | R | S | U | Mode at start | What it covers |
|---|---|---|---|---|---|
| 50 | 40 | 10 | 0 | LOW | about 2.7 low-mode incidents; no D0 screens |
| 100 | 80 | 20 | 0 | NORMAL | one worst incident, then LOW; screens for about 3 workflows at 6.8 expected |
| 200 (public_01/02) | 130 | 50 | 20 | NORMAL | two worst incidents; screens for every dev workflow |
| 400 (public_03) | 130 | 100 | 170 | NORMAL | two worst incidents plus U; 12 screens at 6.8 = 82 |

Expected spend on public_02 (sanity check): D0 screens about 3 x 6.8 = 20 from S, the P2-like
incident 3 from R (stage 1 fails; LKG cached, gate not tight): about 23 of 200. Every spend is an
intent first, so a crash between the call and its response is reconciled from the credit balance
(8.3).

### 7.3 Caller ID degraded mid-run (no event exists)

**Detector** (C's multi-window rule on A's stratified ratio). Per live DID d, at every stop, for the
trailing 1, 2, 4 and 8 dialling hours (exact, because `r_did` is keyed by tick): `r_d` =
Mantel-Haenszel pickup ratio of d vs the pooled other live DIDs, stratified by (cohort, hour), and
`z = log r_d / SE`.

```
alarm   if, in the 2, 4 or 8h window: dials_d >= 100 [T]  and  z <= -4.0 [T]  and  r_d <= 0.75 [T]
suspect if z <= -2.5 [T] in the 1h window (dials_d >= 50)   -> tick-sized steps until alarm or clear,
                                                               P2/P3 submits deferred (6.2)
```

The ratio floor 0.75 excludes natural load imbalance: under the stated load assumption a DID at 0.5
share is about 10% less healthy than one at 0.25 (ratio about 0.90). The 0.75 to 0.85 band is
covered by the slow path (5.9), and a DID with no comparator by the KPI drift detector (7.5).

False alarms: `P(z <= -4) = 3.2e-5` per test. Tests per day = about 14 stops x 3 alarm windows x
live DIDs (plus tick stops while suspect). Dev (3 workflows, about 9 DIDs): 378 tests, at most 0.012
per day, 0.12 per 10-day run (Bonferroni bound; overlapping windows make it lower). public_03 (about
42 DIDs): at most 0.056 per day. At 300 workflows we move to `z <= -4.5` (3.4e-6 per test, about 0.13
per day across the fleet). A false suspect costs only tick steps and a deferred routine slot. A false
alarm drains a healthy DID: graded about neutral at apply (equal-health DIDs), and costs
concentration until the DID is re-probed after 3 days.

**Detection time, computed with one pickup rate** (5.9: `p = 0.25`, cancel-like, 400 dials/hour,
flag to r = 0.36 at 10:00, window opens 09:00). Windows that straddle the flag mix pre-flag dials at
r = 1 with post-flag dials at r = 0.36:

| Share s at the flag | 11:00, 1h window (all post-flag) | 2h window at 11:30 | 2h window at 11:45 | Alarm |
|---|---|---|---|---|
| 0.333 | z = -3.46: suspect | z = -3.72 | z = -4.34 | 11:45 (1.75 h) |
| 0.386 | z = -3.66: suspect | z = -3.93 | z = -4.56 | 11:45 (1.75 h) |
| 0.486 | z = -3.96: suspect | z = -4.18 | - | 11:30 (1.5 h) |

With the 2h window fully post-flag (12:00) a one-third share gives `SE = 0.209`, `z = -4.89`. A mild
flag to r = 0.74 at a one-third share gives `z = -2.86` in the 4h window and `z = -4.05` in the 8h
window: alarm about 8 dialling hours after it starts.

**Response** (F8). Where `caller_id` is auto-applicable: a chain of P0 risk-reducing auto-applies
(6.5), one per tick, each moving at most `max_shift` TV off the degraded DID onto healthy DIDs toward
equal shares, each with its own proposal id and decision row and each passing the DID harm gate
(they grade beneficial). Steps needed: `k = ceil(share / max_shift)`: 2 steps at `max_shift = 0.2`
for a share up to 0.4, 7 steps (1h45m) at `max_shift = 0.05` for a one-third share. Precedence with a
prompt incident follows 6.2 (same-class P0 order; chain first only if `(k - 1) x tick <= 30 min`,
else bundle). Where auto-apply is not allowed: a P0 approval at the next expiry-safe time; under
`next_campaign_only`, campaigns already running stay locked on the flagged DID until their cadence
ends, which the business owns, and the view says so. A faster drain than one `max_shift` step per
tick does not exist inside the policy, so the levers on the flag-day share are detection time and
the share the DID carries before the flag (an earlier spread, 5.9).

**Flag-day share of the flagged DID, as a formula** (the grader's per-day share is
`did_dials / dials` over the whole dialling day, including the hours before the flag):

```
day_share = ( s x (t_pre + t_alarm) + tick_h x sum_{j=1..k-1} (s - j m) ) / H
  s       share of the flagged DID at the flag        m      max_shift per chain step
  t_pre   dialling hours from window open to the flag  t_alarm hours from flag to alarm
  k       ceil(s / m)                                  tick_h tick / 60; H dialling hours per day
  each chain step applies at a stop and its residual share (s - j m) is dialled for one tick
```

| s | m | t_alarm | Flag-day share (t_pre = 1, H = 12, tick 15) |
|---|---|---|---|
| 0.333 | 0.2 | 1.75 | (0.916 + 0.033) / 12 = **0.079** |
| 0.386 | 0.2 | 1.75 | (1.062 + 0.047) / 12 = **0.092** |
| 0.486 | 0.2 | 1.5 | (1.215 + 0.093) / 12 = **0.109** |
| 0.333 | 0.05 | 1.75 | (0.916 + 0.237) / 12 = **0.096** |
| 0.486 | 0.05 | 1.5 | (1.215 + 0.531) / 12 = **0.146** |

So "under 0.10 on the flag day" is not a property the controller can guarantee: it depends on `s`,
which depends on how far the spread got before the flag. The tests therefore assert the formula
(10.1): measured flag-day share `<= day_share(s_measured, m, t_alarm = 2 h) + 0.01` (2 h is a typical alarm time, not a promise: the alarm tick is random, 75 to 150 min measured over 7 seeds; the incident must open within 3 h) (sampling slack:
binomial SE of the share at 4,800 dials is about 0.004), and 0 on every later day.

**Verification.** Two dialling hours after the last step: the degraded DID has no new-version dials
(or only locked campaigns), and workflow pickup versus the pre-incident 24h at the same hours is at
least 0.9 of its level [T]; otherwise re-run the detector on the remaining DIDs and page.

### 7.4 Predicted public_02 timeline (sanity check only)

The controller does not know these events are coming. Times assume the step rule, mid-range
approver latency, and the SPRT expectations of 5.9 (classification times are random).

| Sim time | Workflow | World | Controller action | Expected grading |
|---|---|---|---|---|
| before D0 | all | 14 history days, 1 chunk | ingest; hour curves; cohort grids; DID states (unknown: renewal D3, D4; kyc D2, D3; cancel D3); flag baselines; D0 prompt screens (SEQUENTIAL on P1 x 3, about 20 credits from S) | - |
| D0 00:00 | nw_renewal | | auto-apply probes D3, D4 at 2.8% each (TV 0.056; the Northwind directive restricts only customer-facing dims) | neutral (fresh DIDs, small share) |
| D0 10:00 | nw_renewal | approvers open | approval 1: cohorts + hour weights + W2, W3, V2, V3, V4 at **1.9%** each (Northwind 1% cohort line, 5.4) | neutral |
| D0 10:00 | nw_kyc | | approval 1: cohorts + hour weights + D2, D3 probes at 2.8% (reach the D1 batch) | neutral or beneficial |
| D0 10:00 to 11:00 | ks_cancel | Kestrel notice | probe D3 at 2.8%: notice posted 10:00 (inside approver hours), auto-applied 11:00 just before approval 1 in the same planning step; approval 1: cohorts + hour weights + 5 challengers at 3.8% each (Kestrel line 2%) | neutral |
| D1 about 12:30 to 13:30 | ks_cancel | | D3 classified healthy after about 149 probe dials (about 112 on D0, 37 on D1); the D1 11:00 routine approval decides by 13:00 and is applied first; spread step 1 noticed at 12:30, auto-applied 13:30: TV 0.2 (velocity cap), shares D1 0.386, D2 0.386, D3 0.228 | beneficial (DID) |
| D1 about 15:00 | nw_renewal | | D3 and D4 classified healthy after about 149 dials each (101 on D0, 48 on D1); the D1 10:00 routine approval is applied by about 14:15; spread to quarters, TV 0.444, by auto-apply (the D0 00:00 probes are outside the rolling 24h, so the 0.5 velocity budget is free) | beneficial (DID) |
| D1 to D2 | renewal, cancel | | ramps from EW posteriors in the routine slots (renewal 10:00, cancel 11:00) | neutral or beneficial |
| D2 10:00 | ks_cancel | D2 flagged (hidden), share 0.386 | 11:00 suspect (1h window z = -3.66), tick steps, the 11:00 routine slot is deferred to D3; 11:45 alarm (2h window z = -4.56); drain 0.386 -> 0.186 at 11:45 -> 0 at 12:00, toward D1 0.5, D3 0.5 | beneficial x2; flag-day share 0.092, then 0 |
| D2 to D3 | nw_kyc | | D2 and D3 probes classified: against the single overloaded D1 a healthy probe shows r of about 1.6 and needs about 59 dials (end of D2 or early D3); spread approval at the D3 10:00 slot, decided 11:00 to 18:00, reaches the D4 batch, fully live D6. If r is only about 1.1, classification lands on D4 and everything moves two days | beneficial (DID) |
| D3 11:00 | ks_cancel | | routine approval (ramps) | - |
| D3 11:30 | ks_cancel | supervisor ships P2 | the 11:00 routine approval becomes STALE (version bump) | - |
| D3 12:00 | ks_cancel | | stop: FULL verdict on P2, stage 1 fails (f1 >= 10 with probability 96%), 3 credits from R; LKG P1 SAFE from D0, its designated run is the evidence (g = 0.97, not tight); P0 approval in the same step (requests in the rolling 24h: 2, within Kestrel's 2 and the policy's 4) | - |
| D3 12:45 to 14:15 | ks_cancel | decision visible | applied at the first stop after visibility: **MTTR 75 to 165 min, mean about 122** | beneficial |
| D3 about 15:00 | ks_cancel | | fix verified after 300 connected under P1 (about 3 dialling hours) | - |
| D4 11:00 | ks_cancel | | re-queued routine approval, fresh data, rebased on the P1 version | neutral or beneficial |
| D5 15:00 | nw_renewal | supervisor sets default voice V2 | seen at the 16:00 stop; voice experiment interrupted, V2 is the control; pending proposal STALE; cooldown to D6 15:00; voice test resumes at the D7 10:00 slot | neutral or beneficial |
| D8.5 to D10 | all | | freeze: safety, rollbacks, DID and promotions already declared | - |

Expected shape: nw_kyc pickup clearly up from about D4 (DID spread); renewal and cancel pickup up
from D1; engagement lift modest at dev volume (population looks from about D4, 5.5); target zero
harmful changes, zero 403, lapse, expiry or apply conflict; one hallucination incident fixed in 75
to 165 minutes; about 23 credits.

### 7.5 KPI drift detector (README section 2 item 1)

Comparator-free, per workflow and per KPI, against the history baseline adjusted for hour, attempt,
cohort mix and our own changes. It is the only detector for a sole-DID workflow, for all DIDs failing
together, and for drift with no DID or prompt cause.

```
block b = one dialling hour, closed at the first stop after the hour ends
pickup:      O_b = connects in b
             E_b = sum over b's dials of p_hist(cell, hour, attempt bucket) (history r_pickup, shrunk
                   as in 5.8) x the DID factor our DID model predicts for that dial's DID, relative to
                   the history DID mix (so our own spreads and drains do not read as drift)
engagement:  O_b = engaged in b
             E_b = sum over b's connects of the effect model's prediction for its cell, prompt and
                   arms (so our own arms and promotions do not read as drift)
z_b = (O_b - E_b) / sqrt(E_b (1 - rate_bar))
downward Page CUSUM: S_b = max(0, S_{b-1} - z_b - k),  k = 0.5, alarm at S_b >= h = 6 [T]
an upward CUSUM with the same k and h feeds the view ("doing better than expected"), never an action
armed after 2,000 baseline dials (pickup) or 1,000 baseline connects (engagement); no history: the
first 2 dialling days of our own data are the baseline
```

**ARL0 and false alarms** (Siegmund's approximation, `b = h + 1.166 = 7.166`):
`ARL0 = (e^{2kb} - 1 - 2kb) / (2k^2) = (e^{7.166} - 8.166) / 0.5 = about 2,570 blocks`, about 214
dialling days at 12 blocks a day. Expected false alarms: dev, 3 workflows x 2 KPIs x 10 days = 60
KPI-days: 0.28 per run; public_03, 12 x 2 x 5 = 120 KPI-days: 0.56 per run; at 300 workflows about
2.8 a day across the fleet, which is why a drift alarm is a digest item, never a page or a change.

**Delay**, `ARL1 = (e^{-2 Delta b} + 2 Delta b - 1) / (2 Delta^2)`, `Delta = delta - k`, `delta` = the
shift in SE units per block:

| Case (sanity labels) | Per-block expectation and SE | delta | Delay |
|---|---|---|---|
| cancel-like pickup, -10% | E = 100 connects, SE 8.66 | 1.15 | about 10 dialling hours |
| cancel-like pickup, -20% (e.g. a one-third DID falling to 0.36) | as above | 2.31 | about 3.8 dialling hours (the DID detector is faster, 1.75 h) |
| cancel-like engagement, -10% | E = 47 engaged of 100 connects, SE 4.99 | 0.94 | about 14 dialling hours |
| KYC-like pickup, -10% (single DID) | E = 46 connects, SE 5.95 | 0.77 | about 20 dialling hours (2 days) |
| public_03 workflow pickup, -10% | E = 1,125 connects, SE 29 | 3.87 | about 2 dialling hours |

**Response to an alarm.** Always a `KPI_DRIFT` digest item with the decomposition (mix term vs
within-cohort term, 5.10). Pickup drift: every DID of the workflow is re-tested; if no comparator
exists (sole DID, or every DID drifting together), the DID becomes `suspect` and probes start on
unknown DIDs at size `s` (5.9), which create the comparator the fast and slow paths need; with no
healthy DID, the 5.9 "no healthy DID" rule applies. Engagement drift: the flag CUSUM is checked; if
its statistic is above `h/2`, a FULL verdict on the incumbent is paid from R (it is a detector
firing); otherwise nothing is spent. A drift alarm never changes config by itself.

---

## 8. Reliability

### 8.1 Idempotency keys

| Call | Key | Retry semantics |
|---|---|---|
| chunk ingest | `chunk_no` (PK of `ingested_chunk`, same transaction as facts and rollups) | Re-fetch and skip if present; a partial attempt rolled back, so it cannot duplicate. |
| `POST /sim/advance` | `adv:<from>-><to>` | **Never blind-retried.** On timeout, `GET /sim/state` (it waits on the simulator lock) and resolve as in 8.3. |
| `POST /approvals` | deterministic `proposal_id` | Retry with the same id. `409 duplicate_proposal_id` means the first attempt created a record (ids are consumed only when a record is created): look it up in `GET /approvals?workflow_id=`. |
| `PUT /workflows/{id}/config` | deterministic `proposal_id` | Consumed only on success, so retry with the same id; `409 duplicate` means applied, confirm in `config/history`. Never retry a 403. |
| `POST /workflows/{id}/apply` | `approval_id` | Retry safe: an applied approval returns `409 not_approved` (status applied); it cannot double-apply. |
| `POST /validation/runs`, `/llm/suggest-fixes` | intent row | No idempotency or list endpoint; resolved through the credit balance (8.3). |

### 8.2 Write-ahead intent protocol

```python
def execute(kind: str, key: str, payload: dict, decision: Decision) -> Any:
    with tx():                                    # T1
        insert decision row (inputs, rule, outcome='intended')
        insert intent(kind, key, payload, status='started', decision_id)
        apply the "before" transition (e.g. proposal DRAFT -> SUBMITTING) + audit row
    maybe_crash(kind, "after_t1")
    try:
        result = http(kind, payload)               # the side effect
    except (Timeout, ConnectError):
        return reconcile_one(intent)               # never blind retry
    maybe_crash(kind, "after_http")
    with tx():                                    # T2
        intent.status = 'done', result
        domain transition (proposal, experiment, incident, credit ledger) + audit row
        decision.outcome = result summary
```

Every decision is in the store before its action (constraint 6). A crash between T1 and the call,
between the call and T2, or inside T2 leaves a `started` intent that is resolved deterministically
before any new side effect.

### 8.3 Reconciliation on restart

| Intent kind | Lookup | Resolution |
|---|---|---|
| advance(from, to) | `GET /sim/state` | `sim_minute == from`: never ran, mark aborted, re-plan (planning is deterministic, so the same step). `sim_minute == min(to, end)`: done, ingest new chunks. Anything else: page (impossible by contract). |
| submit_approval(pid) | `GET /approvals?workflow_id=` by `proposal_id` | Found: store `approval_id`, move to its status. Not found: re-check every SUBMITTING precondition (window, fatigue, base, lock); all still hold: resend the same id and payload; else abort and re-plan. |
| auto_apply(pid) | `config/history` by `proposal_id` | Found: APPLIED with that version; store our body. Not found: re-check and resend or abort. |
| apply(aid) | `GET /approvals/{aid}` | `applied`: done (version from history by `approval_id`). `approved` and `now <= apply_by - tick` and base unchanged: re-apply. Otherwise LAPSED_LOCAL or STALE. |
| validation | `credits_remaining` vs ledger | Fell by the cost: the run happened but its id and result are lost: record `lost_validation_run` and re-run once if still needed (the lost result was never seen, so this is not cherry-picking; it is listed in the evidence). Did not fall: re-run. |
| suggest_fixes | credits delta | Same as validation. |

Then the sweep: every non-terminal proposal against `GET /approvals` and `config/history`; every
history version newer than our mirror is recorded (its event also arrives in chunks); any
controller-actor version or applied approval with no intent of ours pages as "unknown writer".

### 8.4 Simulator errors and timeouts

| Response | Handling |
|---|---|
| 5xx, timeout, connection refused on GET | Exponential backoff 0.5 s to 30 s with jitter; page after 5 minutes, keep retrying, do nothing that needs the simulator. |
| Timeout on advance | Reconcile (8.3). Read timeout `max(120 s, 20 x measured wall s per sim hour x S/60)`. |
| Timeout or 5xx on other POSTs | Reconcile by key, then retry. |
| 400 bad_minutes | Bug (we read `tick` from the API): page, stop advancing. |
| 402 out_of_credits | Page; stop non-incident spend; DID path continues. |
| 403 auto_apply_not_permitted | Replica bug: FAILED_POLICY, page, disable auto-apply for that workflow for the rest of the run, re-route to approval. |
| 404 | Reconcile; an unknown approval after a restart means a new world (8.5). |
| 409 stale_base_version | STALE, rebase. |
| 409 duplicate_proposal_id, not_approved | Reconcile (8.1). |
| 409 finished | Final ingest and reconcile, exit 0. |
| 410 approval_lapsed | Should be impossible (we check `apply_by`): page, re-queue. |
| 422 | Bug: FAILED, page, decision row records the problems. |

### 8.5 Run identity: schema per run, resume only when consistent (F9)

```
fingerprint = sha256(scenario name, end_minute, tick_minutes, sorted (workflow_id, tenant_id,
              governance, options) from the API, history chunk 0 metadata (events, last_event_id))
candidates  = ctl.run_registry rows with this fingerprint and status != done, newest first
resume a candidate only if ALL hold against the live simulator:
   our clock.sim_minute <= state.sim_minute
   our max ingested chunk < state.chunks and our max event_id <= state.last_event_id
   every approval_id we hold is returned by GET /approvals with the same proposal_id
   every controller version we recorded appears in config/history with the same proposal_id
   for every workflow, current_version >= our highest recorded version
otherwise: create schema run_<fp12>_<n+1>, register it, raise a "new world" alert, keep old data
```

A re-run of the same scenario, or a restarted simulator, has the same fingerprint but fails the
checks (its clock or ids are behind ours), so it gets a fresh schema. A controller restart against
the same live world passes them.

### 8.6 Determinism

No wall clock and no unseeded randomness in decisions; posterior draws are seeded by
`hash(run, workflow, decision_id, fit_kind)`; iteration orders are sorted; floats are rounded to 1e-9
before comparison with gates; proposal ids are deterministic. Approval outcomes are seeded by
`proposal_id` in the simulator (world.py:465), and dial randomness depends on the request sequence,
so a crashed and restarted run issues the same calls at the same simulated minutes as a clean run,
**provided the restart issues no extra simulator call**. That holds for every intent kind except one
case: validation and suggest-fixes calls lost at `after_http`. Validation ids are sequential and
global across workflows, and the id seeds the result (`rng = _stable_rng(seed, "val", rid)`,
world.py:548-552), so one lost run shifts the id, and therefore the outcome, of **every** later
validation run in the scenario, not just one. Those matrix cells are checked by invariants, not
equality (10.2).

### 8.7 Wall-clock budget per public_03 sim day (estimate; M1 measures)

| Phase | Per sim day | Basis |
|---|---|---|
| Simulator advance | about 5 s on the dev laptop, assume 15 s on the grading box | measured: `python -m sim baseline` on public_03, 63.1 s for 12 sim days including history (docs/measurements/simulator-baseline.md); HTTP overhead re-measured in M1 (F17) |
| Chunk fetch + COPY (about 370 MB) | 4 to 8 s, overlapped with the transform | loopback HTTP, one text column |
| SQL transform + rollups (about 770k to 840k events) | 15 to 25 s | jsonb parse, est. 40k to 80k events/s per backend |
| Snapshots, detect, decide (12 workflows x about 14 steps) | 10 to 20 s | 50 to 100 ms per workflow-step for detectors and gates |
| Model fits at planning points | 5 to 25 s | 12 workflows x about 3 planning points x 2 fits x 100 to 300 ms, plus draws (5.3) |
| Governance HTTP | under 1 s | a handful of calls per step |
| **Total** | **about 45 to 95 s** | target under 2 minutes; SRE bar 10 minutes |

History bootstrap (about 5.9M events, 2.3 GB) is a one-time cost, estimated 2 to 3 minutes,
pipelined. Controller RSS target under 1 GB; it streams chunks and never holds one in memory.

---

## 9. Observability, audit and the supervisor view

### 9.1 Loop health (`loop_step` table, `GET /ops`)

| Metric | Definition | Target |
|---|---|---|
| `ingest_events_per_s` | events / transform wall seconds, per chunk and per sim day | measured in M1 |
| `ingest_lag_sim_min` | `clock.sim_minute - max(until_minute)` at decision time | always 0 (decisions block otherwise) |
| `ingest_lag_wall_ms` | advance return to last chunk committed | under 10 s per step at public_03 |
| `decision_lag_wall_ms` | advance return to end of the govern phase | under 15 s per step |
| `detection_lag_sim_min` | incident confirmed minute minus first-evidence hour | reported per incident |
| `wall_s_per_sim_day` | sum of step wall time per sim day | under 120 s at public_03; SRE bar 600 s |
| `decisions_per_sim_hour` | decision rows by module and kind, including "no action" | reported |
| `credits_remaining`, `credit_burn_24h`, `reserve_headroom` | per tenant and global | above reserve |
| `requests_used_24h` | per workflow vs `C_eff` and reserve | at most `C_eff` |
| governance invariants | 403s, lapses, expiries, apply conflicts, orphan calls, cohort mismatches | all 0 |
| resources | controller RSS, `pg_database_size`, WAL size | RSS under 1 GB |
| liveness | heartbeat age, restarts in the last hour | heartbeat under 60 s |

### 9.2 Alerts

| Pages (actionable now; message says where to look) | Morning digest (can wait) |
|---|---|
| `INGEST_STALLED`: chunks listed but not ingested for 5 wall minutes (last `ingested_chunk`, transform error) | approver rejections and expiries (handled automatically) |
| `APPLY_AT_RISK`: an `APPROVED` or `APPLYING` proposal within one step of `apply_by`, or at an overnight jump (proposal id, failed pre-check) | harm-gate blocks and "did not act" decisions |
| `GOV_INVARIANT`: any 403, 410, 422, fatigue or evidence rejection, apply conflict (local `policy_check` output vs `GET /workflows/{id}`) | decay CUSUM alarms that cleared; futility drops; arm removals |
| `CREDITS_BELOW_RESERVE` or any 402, or a switch to LOW credit mode (burn table by pool) | `KPI_DRIFT` items (7.5) with their mix decomposition |
| `INCIDENT_UNRESOLVED`: an incident without a submitted fix within 2 sim hours, or unresolved after 6; a DID drain waiting more than 2 hours behind a P0a approval | supervisor edits and interrupted experiments (information) |
| `NO_SAFE_PROMPT`, `CLIENT_GATE_UNREACHABLE` (7.1) | retries that succeeded; orphan calls |
| `NO_HEALTHY_DID` (5.9), `NO_SAFE_WINDOW` (no expiry-safe submit time in 24h, 6.3) | held patches auto-released (6.1) |
| `NEW_WORLD`, `UNKNOWN_WRITER`, `COHORT_MISMATCH`, `CRASH_LOOP` (more than 3 restarts in 10 minutes) | weekly operational digest (Northwind) |

**Never alert** on single-call anomalies, routine submissions or auto-applied schedule refreshes.
Every alert row carries `runbook = docs/RUNBOOK.md#<code>` (9.5).

### 9.3 Supervisor view (`GET /supervisor/{tenant}`, JSON; the HTML page is cut)

One screen per tenant, one section per workflow, answering Kestrel Ops' four questions plus health:
1. **Needs your decision**: pending approvals sorted by class, then impact score (with its KPI
   label); each with its one-line items, diff, affected leads, dry-run headline (primary and
   guardrail, worst case), time to expiry and our expected decision window.
2. **Changed today**: every config version (ours approved, ours auto-applied, supervisor) with actor,
   time, summary, approval id, the verification result (6.1) and a link to the decision record.
   Notes such as "3rd request today, above your 2/day preference, because of incident INC-4" and
   "policy permits auto-apply, client agreement forbids it: routed to approval".
3. **Being tested**: arms and shares per cohort in words ("V3 for over-60s: 25% of calls, probably
   better, not yet proven"), the next gate in words.
4. **Winning**: declared winners with effect and 95% interval, holdout size, decay monitor state.
5. **Health**: open and closed incidents with timelines, DID table (status, ratio, SPRT progress),
   flag rate vs baseline, drift monitors, credits and credit mode.
6. **Upcoming**: announced customer-facing auto-applies with ETA and countdown, next routine slot.
   `POST /supervisor/holds` pauses non-incident changes on a workflow. The api container connects as
   role `ctl_api`, which can INSERT and UPDATE `hold` and nothing else; the controller reads `hold`
   at every step.
7. **Where your default does not reach**: cohorts whose own allocations override `default`, and the
   business cohorts we never redefine (5.2).
8. **Weekly digest** (Northwind): operational changes (caller ID, schedule) with reasons, from
   `decision` rows.

Pickup and engagement are always shown side by side, never blended.

### 9.4 The audit queries (C12)

"Why did workflow X change at T, who approved it, and what happened after?"

```sql
WITH v AS (SELECT cv.* FROM config_version cv JOIN workflow w USING (wf)
           WHERE w.workflow_id = :wf AND cv.applied_minute <= :t
           ORDER BY cv.applied_minute DESC LIMIT 1)
SELECT v.version, v.applied_minute, v.actor, v.changed_dims,
       p.proposal_id, p.class, p.change_summary, p.rationale, p.impact_score, p.dry_run, p.verify,
       a.approval_id, a.created_minute, a.decided_minute, a.status, a.approver_name,
       d.rule_id, d.inputs, d.harm_report          -- the statistics at decision time
FROM v LEFT JOIN proposal p ON p.proposal_id = v.proposal_id
       LEFT JOIN approval_mirror a ON a.approval_id = v.approval_id
       LEFT JOIN decision d ON d.decision_id = p.decision_id;
-- what happened after: everything the decision caused, in order (verification, harm stops, rollbacks)
WITH RECURSIVE chain AS (
  SELECT * FROM audit_log WHERE subject_type = 'proposal' AND subject_id = :proposal_id
  UNION ALL SELECT a.* FROM audit_log a JOIN chain c ON a.caused_by = c.audit_id)
SELECT sim_minute, actor, action, summary FROM chain ORDER BY audit_id;
```

A supervisor version has `proposal_id` NULL; the chain then starts from its `config_version` audit
row and shows our reaction (stale marks, interrupts, rebase, cooldown).

"Why did this customer hear this variant on this day?" (IRDAI, within 24 hours). Option codes are
per-dimension smallints, so every join is on the `(dim, option)` pair. Welcome, voice and caller ID
come from the version the call was dialled with (`c.config_version`, which on `next_campaign_only`
is the campaign's locked version); the prompt always comes from the version **live at the call's
minute**, because prompts apply immediately even on locked campaigns. `allocation_provenance` is
materialised for every cohort, so a cohort without its own override finds its inherited `default`
rows directly (`inherited = true`).

```sql
WITH k AS (
  SELECT c.*, cc.cohort_key,
         (SELECT max(cv.version) FROM config_version cv
           WHERE cv.wf = c.wf AND cv.applied_minute <= c.minute) AS live_version
  FROM call_fact c
  JOIN lead_dim l USING (lead_sk)
  JOIN config_cohort cc ON cc.wf = c.wf AND cc.version = c.config_version
                       AND cc.cohort_code = c.cohort_code
  WHERE l.lead_id = :lead_id AND c.sim_day = :day)
SELECT k.minute, k.attempt_no, k.cohort_key, d.dim, d.option, d.version,
       ap.share, ap.source, ap.inherited, ap.experiment_id, ap.proposal_id, ap.approval_id,
       ap.decision_id
FROM k
CROSS JOIN LATERAL (VALUES ('welcome_message', k.w,      k.config_version, k.cohort_key),
                           ('voice',           k.v,      k.config_version, k.cohort_key),
                           ('caller_id',       k.did,    k.config_version, k.cohort_key),
                           ('prompt_version',  k.prompt, k.live_version,   'default'))
     AS d(dim, option, version, scope_key)
LEFT JOIN allocation_provenance ap
       ON ap.wf = k.wf AND ap.version = d.version AND ap.cohort_key = d.scope_key
      AND ap.dim = d.dim AND ap.option = d.option
ORDER BY k.minute, d.dim;
```

A NULL provenance row is itself a finding (the call heard an option our record says was not live)
and raises `COHORT_MISMATCH`. Both queries are wrapped as `python -m controller.audit why-changed
--workflow X --at "D3 14:00"` and `why-heard --lead <id> --day 3`, and as `/explain/change` and
`/explain/call`. Output reads like: "V3 at 10%: arm of `exp_0007`, proposed by
`a1b2c3-nw_renewal-D2T1000-3-0004`, approved `apr_00012` at D2 11:40, randomised per call." Gap
stated for Compliance: the API never returns the approver's identity, only `approval_id` and
`decided_at`; `approval_mirror.approver_name` is a nullable column for the client's own system.

### 9.5 Runbooks (`docs/RUNBOOK.md`, one anchor per alert code) and the local-run guide

| Code | What it means | Look first | Do |
|---|---|---|---|
| `INGEST_STALLED` | A listed chunk is not committed after 5 wall minutes | `ingested_chunk` max vs `GET /events/chunks`; last `loop_step.phase_ms`; Postgres log for the transform error | If the count check failed, compare chunk meta `events` with `stage_raw` rows; set `CTL_INGEST=python` and restart; the chunk re-ingests exactly once |
| `APPLY_AT_RISK` | An approved proposal may lapse | `proposal` row, `approval_mirror.apply_by_minute`, the failed pre-check in the decision row | If the base changed: it is STALE, nothing to do. If a step skipped past `apply_by - tick`: a bug in `next_stop`; the proposal goes LAPSED_LOCAL and re-queues by itself; file the `loop_step` rows with the bug |
| `GOV_INVARIANT` | The local policy replica disagreed with the simulator | the 403 or rejection reasons vs `policy_check` output and `GET /workflows/{id}` | Auto-apply for that workflow is already disabled; diff the policy block against `workflow.spec`; add the case to the differential test |
| `CREDITS_BELOW_RESERVE` | R fell below W or a 402 arrived | `credit_ledger` by pool and purpose | Normal if an incident is running; otherwise find the non-incident spender in the ledger |
| `INCIDENT_UNRESOLVED` | No fix submitted in 2 h or not resolved in 6 h | `incident` row, its verdicts, the in-flight proposal | Check for `NO_SAFE_PROMPT` or `CLIENT_GATE_UNREACHABLE`; check whether the fix waits on fatigue (the 24h window in `requests_used_24h`) |
| `NO_SAFE_PROMPT` | Every catalog candidate is UNSAFE | `prompt_verdict` rows for the workflow | A human must supply a prompt; the controller changes nothing blindly |
| `CLIENT_GATE_UNREACHABLE` | Safe prompts cannot pass g reliably | `min_validation_pass_rate`, the designated runs | Ask the client to review g; never re-run to fish for a pass |
| `NO_HEALTHY_DID` | No caller ID can take traffic | `did_state` rows, SPRT progress | Probes are running; provision a new caller ID if every option is degraded |
| `NO_SAFE_WINDOW` | No expiry-safe submit time in 24h | `EffectivePolicy` latency, TTL and approver hours | Usually TTL shorter than the latency; ask the client to extend TTL |
| `NEW_WORLD` | The simulator restarted or a scenario re-ran | `ctl.run_registry`, the failed consistency check | Expected after a re-run; otherwise check `SIM_URL` |
| `UNKNOWN_WRITER` | A controller version or approval with no intent of ours | `config/history` vs `intent` | A second controller is running: check the advisory lock holder |
| `COHORT_MISMATCH` | A call's `cohort_id` or option disagrees with our resolution | the call, `config_cohort`, `allocation_provenance` | Our config mirror is wrong: compare `config_version.config` with `GET /workflows/{id}/config` |
| `CRASH_LOOP` | More than 3 restarts in 10 minutes | the controller log tail of the last run, `intent` rows in `started` | Reconciliation failing on one intent: inspect it; exit code 3 means configuration |

**`docs/RUNNING.md`** (M7 deliverable, for the engineer who is paged): prerequisites (Docker with
Compose v2, or `CTL_NO_DOCKER=1` with a local Postgres 16); running public_01 against a local
simulator in four commands; connecting with `psql` to the run schema (`ctl.run_registry` names it);
the audit CLI (`why-changed`, `why-heard`, `replay-decision`); fault injection (`CTL_FAULT`); where
logs and `loop_step` metrics live; running the unit, contract and E2E suites; and how to add a
threshold (settings docstring, register row, private-variant test).

### 9.6 Decision replay (`python -m controller.audit replay-decision <decision_id>`)

Every decision row stores `snapshot_ids`, `seed`, `settings_hash` and `code_version`; every posterior
snapshot stores its `inputs` (the aggregated count arrays, segment weights and prior, as npz) next
to the mean and covariance. Replay loads them, refits the model (or reuses the stored posterior when
`--no-refit`), redraws with the stored seed, recomputes the gate, rule or bisection, and prints the
stored and recomputed values side by side (equal after rounding to 1e-9, or a diff). If
`settings_hash` or `code_version` differ from the running code, it says so before printing. This is
the tool for the live review: "explain this decision from your audit store".

### 9.7 Change a constraint: what reads it and what happens

Every parameter is read from the API by `world/registry.py` into `EffectivePolicy`; the column below
names the formula and module that consume it.

| Parameter (API) | Read by | Behaviour at the changed value |
|---|---|---|
| `max_requests_per_24h` = 1 | `C_eff`, `reserve`, `routine` (6.4, `governance/timing.py`) | reserve 0, routine at most once per 48h; D0 foundation still ships; an incident that arrives while our routine request is in the 24h window waits up to 24h for the slot (13.4) |
| `proposal_ttl_hours` = 4 | `p_expire` (6.3, `world/clock.py`) | approvers 10-19, latency 30-240: in-hours submits safe through 14:45, routine slot 11:00 unchanged; approvers 14-16: pre-open 12:00 is the slot |
| `apply_window_hours` = 1 | `s_watch_max` (4.2, `loop/stepper.py`) | `S_watch_max = 15 x floor(46 / 15) = 45`, in-hours step while pending 30 instead of 60; about 8 more advances per approver day; MTTR unchanged (fast-apply steps) |
| `tick_minutes` = 30 | `next_stop`, `p_expire`, chain steps, `r_did` keying | `S_watch_max(120) = 30 x floor(91 / 30) = 90`, step 60 unchanged; visibility lag V in [30, 59] instead of [15, 29], so MTTR mean +22 min; drain chain steps every 30 min, so the `(k - 1) x tick <= 30` rule bundles any chain longer than 2 steps; the day-share formula uses `tick_h = 0.5` |
| `auto_apply.max_shift` = 0 | `auto_apply_permitted` (6.5, `governance/policy.py`) | every welcome, voice or caller-ID change has TV > 0, so none auto-applies; schedule-only auto-applies still allowed if listed; drains become P0 approvals; probes ride the D0 approval |
| `auto_apply.enabled` = false | same | everything by approval; DID drain is a P0 approval (bundled with a prompt fix if both); schedule refresh inside routine approvals |
| credits = 50 | pools (7.2, `incidents/credits.py`) | R = 40 < W = 65: LOW mode from the start; no D0 screens; stage-1-only verdicts, worst incident 15 credits |
| `approver_hours` = 14-16 (TTL 24, latency 30-240) | `p_expire`, `preferred_slot`, decision horizon (4.2) | slot 14:30; most decisions land the next day at 14:00 or later; overnight jumps only from 16:15; apply window unchanged |
| `history_days` = 0 | bootstrap (C3), `HourCurves`, `DidHealth`, flag CUSUM, drift baseline | no schedule change for 3 dialling days; grid from the first day's leads at D0 end, F2 only (no hour-curve test); default-config DIDs classified against each other live, others probed; flag CUSUM and drift detector armed after their baseline counts; cross-workflow flag check skipped |
| `mid_flight` = `next_campaign_only` | `affected_leads`, `expected_apply_minute`, verification (6.1, 6.6) | allocation changes reach only new campaigns; probes classify from new campaigns only (5.9 KYC row); verification counts new campaigns only |
| `min_validation_pass_rate` = 0.99 | gate-evidence rule (7.1, `incidents/safety.py`) | tight gate: separate designated evidence run at n = 100, about 72% pass for a safe prompt; otherwise next candidate, then `CLIENT_GATE_UNREACHABLE` |

---

## 10. Testing strategy

End to end first: tests start the real simulator as a subprocess (allowed for tests, never for
controller code) and drive the real controller over HTTP against a real Postgres. Tests may read
the simulator's run directory and run the grader; the controller never does. Custom scenarios are
YAML written by the test harness, never read by the controller.

### 10.1 Suites

| Layer | Test | Asserts |
|---|---|---|
| E2E floor | `test_public01_full_run` | Finishes; grader: 0 harmful, 0 `auto_apply_rejected`, 0 lapsed, 0 expired, 0 apply conflicts, peak requests per 24h at most `C_eff`; every simulator config version maps to exactly one of our proposals or a supervisor edit, with decision and audit rows. |
| E2E floor | `test_public02_incidents` | Same, plus hallucination MTTR under 4h; flagged-DID share on the flag day at most `day_share(s_measured, max_shift, t_alarm = 2 h) + 0.01` (7.3 formula; `s_measured` = the DID's share of dials in the hour before the flag, from our store) and 0 on every later day; the supervisor voice edit is never reverted without an approval; credits above reserve. |
| E2E policy cases (B, reduced) | Only the J2 attack cases below plus the 9.7 rows (tick 30, apply window 1h, `max_shift` 0, auto-apply off, credits 50, no history); generated fuzzing beyond them is cut (section 1) | Governance invariants as above; at least one routine approval applied per workflow whenever an expiry-safe window exists. |
| E2E custom (J2 attacks) | (a) KYC-like, TTL 12h, latency 60-480; (b) approvers 14-16, TTL 4h, latency 30-240; (c) cap 1, auto-apply off; (d) `max_shift` 0.05 and a DID flagged at a one-third share; (e) prompt incident pending when a DID is flagged; (f) supervisor ships a P4-like prompt; (g) strong winner that decays after 3 days at high volume; (h) pre-flagged unknown DID at 0.3; (i) unsafe business-default prompt; (j) winners only among the last-listed options with sign-flipping cohort effects; (k) approver rejects 60% | (a), (b) no expiry, at least one approval applied per day; (c) foundation approval ships on D0; (d) flag-day share within the 7.3 formula bound (for s = 0.333: at most 0.096 + 0.01), 0 the day after; (e) prompt-fix approval never stale or superseded, the drain starts when it is terminal, MTTR under 4h; (f) MTTR finite in at least 90% of 20 seeds; (g) no ramp or promotion graded harmful, decay share under 10% by onset + 2 days; (h) no change with dd below -2%; (i) fix proposed with a FULL verdict, never a strict-script prompt for Kestrel-like directives; (j) engagement gap captured above 0 and no harmful change; (k) re-queue with fresh data, fatigue never exceeded. |
| Differential (B) | `policy_check` vs a live simulator | 2,000 random (current, proposed) pairs on a scratch world: local verdict and `changed_dims` equal the simulator's 100%. Test-only: it is allowed to trigger 403s on a scratch world. |
| Contract (C) | `test_sim_contract` | Lazy lapse; approval id consumed on POST, auto-apply id only on success; decision visible after its tick; chunk listing during idle; strict-inequality fatigue window; `next_working` behaviour. Fails first if the graders' copy differs. |
| Ingest | `test_ingest_counts_public03_day` | After D0, fact and lead counts plus other events equal `state.last_event_id` minus history; rollups equal a from-scratch recomputation over `call_fact`; SQL path and Python fallback produce identical rollups on a hand-written NDJSON fixture. |
| Determinism | `test_determinism` | Two clean public_01 runs produce identical proposal tables and config histories. |
| Unit (pure) | stats, policy, timing, lifecycle, cohorts | The tables in this document are fixtures: harm-gate sizes and the per-cohort cost (5.4), effective boundaries and union bounds per scope (5.5), `classify_risk` cases (6.5), `routine_slots` and `choose_submit` never returning a non-slot minute for P2 or P3 (6.3), the day-share formula (7.3), credit pools at C0 = 50, 100, 200, 400 (7.2), gate-evidence `n_e` at g = 0.97, 0.98, 0.99 (7.1), holdout sizes (5.6), verdict probabilities (7.1, seeded binomial simulation), CUSUM ARLs (seeded simulation), `p_expire` worked cases (6.3), `S_watch_max`, `changed_dims` and `max_shift` edge cases (cohort order, normalised weights, `lead_preferred_hour`), lifecycle transition table. |
| Statistics (M5) | `test_false_winner_rate` | Seeded simulation of the exact production code path (allocator with its population and cohort gates, probability matching, looks, W1 to W6) on synthetic null worlds: every arm effect 0 in every cohort, dev-like (950 connected/day) and load-like (13,500/day) volumes, 10 days, a reduced 24-cell grid so one replication fits in about 0.1 s of model time; 4,000 replications per volume on 4 cores (about 10 minutes, marked slow). Measured separately for population and cohort scopes, under the real adaptive (unequal) allocation. Pass: the upper 95% bound of the false-winner rate is at most 1% per hypothesis in each scope; otherwise that scope's W1 is raised [T]. |
| Statistics (M4) | `test_did_sprt` | Seeded simulation of the SPRT on binomial dials: E[N] within 10% of the 5.9 table at r = 1.10, 1.00, 0.64, 0.36; P(healthy at r = 0.70) <= 1%; P(unhealthy at r = 1.0) <= 5%. |
| Statistics (M4) | `test_drift_cusum` | Seeded simulation: ARL0 at least 2,000 blocks at k 0.5, h 6; delays within 20% of the 7.5 table. |
| Load | `scripts/load_test.sh` | public_03: wall per sim day, events/s, peak RSS (`docker stats`), DB size; writes `reports/public_03_load.md`. |

### 10.2 Crash-recovery demonstration (C13)

1. **Fault injection.** `CTL_FAULT=<kind>:<n>:<phase>` calls `os._exit(137)` (same effect as
   SIGKILL) the n-th time the process reaches the point. Kinds: advance, submit_approval,
   auto_apply, apply, validation, suggest_fixes, chunk_transform, audit_write. Phases: `before_t1`,
   `after_t1`, `after_http`, `in_t2`.
2. **Crash-equivalence matrix** (B). For public_02, one clean run; then for each (kind, phase) and
   n in {1, 3}, a run with one injected crash, restarted as `run.sh` would. For every cell except
   two, assert the simulator's `config/history`, `GET /approvals` (ids, statuses), credits used and
   the grade report are **identical** to the clean run. The two exceptions are `validation` and
   `suggest_fixes` at `after_http`: a lost validation run consumed a validation id, so every later
   validation outcome differs (8.6); a lost suggest-fixes call (only with `CTL_LLM_SUGGEST=1`)
   costs its 3 credits twice, so the credit balance differs. Those cells assert **invariants** instead: at most one extra
   run per lost run, `lost_validation_run` recorded, every lost and re-run id listed in the
   evidence, no prompt proposed without a SAFE verdict and a designated passing run, 0 lapses, 0
   conflicts, 0 duplicate applies, no pending approval unresolved, credits above the incident
   reserve, and the incident (if any) still fixed within 4h.
3. **Random kill.** `scripts/kill_test.sh public_02`: SIGKILL the controller container at uniform
   random wall intervals of 10 to 60 s, 20 times, the `run.sh` loop restarting it. Assert the
   invariants (0 lapses, 0 conflicts, 0 duplicate applies, no pending approval left unresolved,
   ingested events equal `last_event_id`) and that the run completes. One kill run on public_03 shows
   recovery mid-COPY at load.
4. **Evidence:** `reports/crash_recovery.md` with the matrix (pass or fail per cell), the kill log
   (kill times, open intents found at each restart and how each was reconciled) and the grade diffs.

---

## 11. Module breakdown

### 11.1 Package layout

```
controller/
  __main__.py              entry: settings, lock, run resolution, reconcile, loop; exit codes 0 / 1 / 3
  settings.py              env settings and every [T] threshold with its derivation docstring
  directives.yaml          client agreements (6.5)
  types.py                 frozen dataclasses: the M0 contract (below)
  simclient/client.py      SimClient; errors.py: ApiError subclasses per (status, code)
  store/db.py              pool, tx(), advisory lock, schema per run, migrations runner
  store/migrations/*.sql   DDL of section 3
  store/repos.py           typed read and write helpers per table group
  ingest/loader.py         stream chunk -> COPY stage_raw (thread)
  ingest/transform.sql     set-based transform and rollups (3.1)
  ingest/python_parse.py   fallback parser behind the same interface
  ingest/pipeline.py       Ingestor: ordering, exactly-once, gap filling, metrics
  world/registry.py        WorkflowRegistry: specs, EffectivePolicy (API AND directives)
  world/configs.py         ConfigMirror: bodies, supervisor-edit detection
  world/clock.py           next_working, p_expire, submit_candidates, s_watch_max
  world/fingerprint.py     fingerprint, consistency checks, run resolution
  cohorts/cells.py         cell catalog
  cohorts/grid.py          build_grid(history, business_cohorts) -> CohortSet   (regrid cut)
  cohorts/cohortset.py     CohortSet: cohort_of(cell), key(), to_config(), membership(version)
  stats/glm.py             fit_ridge_logit(design, counts, weights, prior) -> Posterior
  stats/draws.py           seeded_draws(posterior, seed, n)
  stats/sequential.py      BernoulliCusum, PageCusum (serialisable state)
  stats/ratios.py          mantel_haenszel, wilson, poisson_tail, binom_tail
  stats/boundaries.py      effective_boundary(I, tau, p), union_bound(looks)
  snapshot.py              WorkflowSnapshot from rollups (numpy)
  learning/engagement.py   EngagementModel.fit(ws, kind) ; cohort_effects(draws, cohort_set)
  learning/harm.py         harm_gate(cur, new, draws, pop) -> HarmReport ; did_gate(...)
  learning/allocator.py    plan_allocations(ws, draws_ew, constraints) -> list[PatchRequest]
  learning/winners.py      looks_due(ws) ; declare(ws, stat, ew) ; holdout_size(N_k, e)
  learning/decay.py        DecayMonitor.update(segment) ; rollback_plan(ws, ew, t_a)
  learning/did.py          DidHealth.update(ws) ; classify() ; plan_caller_ids(ws)
  learning/schedule.py     HourCurves.fit(ws) ; plan_hour_weights(ws)
  learning/guardrail.py    GuardrailMonitor.check(ws) -> list[Breach]
  incidents/detectors.py   DidDetector, PromptTrigger, FlagCusum, DriftDetector (7.5)
  incidents/safety.py      SafetyVerdict (sequential and full), fix selection
  incidents/manager.py     IncidentManager: state machine, verification
  incidents/credits.py     CreditBudget.authorize(tenant, purpose, amount) ; ledger
  planner/overlay.py       Patch, Overlay.compose, rebase_on_supervisor_edit, path ownership
  governance/policy.py     normalise, changed_dims, max_shift, classify_risk, auto_apply_permitted
  governance/timing.py     choose_submit, fatigue, slots
  governance/builder.py    build_request: summary, diff, affected leads, dry run, impact, rationale
  governance/lifecycle.py  ProposalLifecycle: table-driven transitions, supersede, pre-apply checks
  governance/release.py    ReleaseTrain: classes, precedence (6.2), batching, notice
  reliability/intents.py   IntentExecutor, Reconciler ; faults.py: crash injection
  loop/stepper.py          next_stop (pure)
  loop/orchestrator.py     run_step, resume
  audit/writer.py          record(tx, ...) ; provenance(tx, version, ...)
  audit/explain.py         why_changed, why_heard, weekly_digest ; __main__ CLI
  audit/replay.py          replay_decision(decision_id) -> ReplayReport (9.6)
  obs/metrics.py, obs/alerts.py   alert rules; every alert carries its runbook anchor
  api/app.py               FastAPI JSON views + holds (role ctl_api); no HTML page
docs/RUNNING.md, docs/RUNBOOK.md  local run and debug guide; one runbook anchor per alert code
tests/{e2e,unit,fuzz,contract}  scripts/{load_test.sh,kill_test.sh,crash_matrix.sh}
compose.yml compose.linux.yml compose.desktop.yml Dockerfile run.sh
```

Dependency order (each layer uses only layers above it): types, settings, store -> simclient,
reliability, audit/writer -> world, ingest -> cohorts, stats (pure) -> snapshot -> learning,
incidents -> planner, governance -> loop -> api, obs, audit/explain.

### 11.2 Public interfaces (the contracts teams build against)

Every class and function below is in `types.py` or the named module; every type used in a signature
is defined here. Learning and incident code is pure over a snapshot plus persisted monitor state;
only `IntentExecutor` performs side effects.

```python
# ---------------------------------------------------------------- types.py: value types
Cohort = str                      # cohort_key, e.g. "age60p_ta~3fa2c1"; "default" for the default
Dim = Literal["welcome_message", "voice", "caller_id", "prompt_version", "schedule", "cohorts"]

@dataclass(frozen=True)
class Config:                     # normalised simulator config body (weights normalised, zeros dropped)
    cohorts: tuple[CohortDef, ...]                 # in order; first match wins
    allocations: Mapping[Cohort, Mapping[Dim, Mapping[str, float]]]
    prompt_version: str
    hour_weights: Mapping[Cohort, Mapping[int, float]]
    lead_preferred_hour: Mapping[str, int]
    def dist(self, cohort: Cohort, dim: Dim) -> Mapping[str, float]: ...   # with default fallback
    def body(self) -> dict: ...                    # exact JSON for the API
    def hash(self) -> str: ...

@dataclass(frozen=True)
class EffectivePolicy:            # API policy AND directives, the stricter of each (6.5)
    tick: int; approver_hours: tuple[int, int]; approver_open_min: int
    latency_minutes: tuple[int, int]; ttl_hours: float; apply_window_min: int
    c_policy: int; c_eff: int; reserve: int; routine_budget: int
    auto_enabled: bool; allowed_dims: frozenset[str]; max_shift: float
    customer_facing_auto: str          # none | risk_reducing_only | ramps_and_risk_reducing
    not_before_min: int | None; preferred_slot_min: int; notice_minutes: int
    cohort_harm_line: float            # x_k: 0.02 default, 0.01 by directive
    mid_flight: str; min_validation_pass_rate: float; avoid_prompt_terms: tuple[str, ...]

@dataclass(frozen=True)
class PatchRequest:               # what learning and incidents emit; no risk flag (computed, 6.5)
    wf: str; owner: str; path: tuple[str, ...]; value: Any
    cls: int                           # 0 incident, 1 rollback, 2 promotion, 3 routine
    p0_rank: int | None                # 0 = P0a prompt safety, 1 = P0b DID drain; None if cls > 0
    evidence: dict; decision_id: int

@dataclass(frozen=True)
class Composition:                # live config + patches, ready for the gates
    wf: str; base_version: int; live: Config; proposed: Config
    patches: tuple[Patch, ...]; changed_dims: tuple[str, ...]
    tv_by_cohort_dim: Mapping[tuple[Cohort, Dim], float]
    risk: Mapping[tuple[Cohort, Dim], Literal["reducing", "increasing"]]   # classify_risk
    cls: int; p0_rank: int | None

@dataclass(frozen=True)
class Draws:                      # seeded posterior draws of one fit
    kind: Literal["stationary", "ew"]; seed: int; snapshot_id: int
    params: np.ndarray                 # (n_draws, n_params), n_draws = 4,000
    index: ParamIndex                  # maps (arm, feature, level) and cell intercepts to columns
    def cell_effects(self, dim: Dim, arm: str) -> np.ndarray: ...  # (n_draws, n_cells) log ratios

@dataclass(frozen=True)
class HarmReport:
    de_mean: float; p_de_below_1pct: float
    de_k: Mapping[Cohort, tuple[float, float]]     # cohort -> (mean, P(de_k < -x_k))
    dd_mean: float; p_dd_below_2pct: float
    guardrail_kpi: str; guardrail_drop_mean: float; p_guardrail_breach: float
    passes: bool; failed: tuple[str, ...]; alpha_used: float

@dataclass(frozen=True)
class Finding:                    # any detector output
    wf: str; detector: str; kind: str  # e.g. ("did", "alarm"), ("drift", "pickup_down")
    scope: Cohort | str; stat: float; threshold: float
    first_evidence_minute: int; evidence: dict

@dataclass(frozen=True)
class Verdict:
    wf: str; prompt: str; verdict: Literal["SAFE", "UNSAFE"]
    mode: Literal["sequential", "full", "stage1_only"]
    runs: tuple[str, ...]; pooled_n: int; pooled_failures: int
    gate_run: str | None               # the designated evidence run, if it passed g
    approvable: bool; credits: float

@dataclass(frozen=True)
class Winner:
    exp_id: str; wf: str; dim: Dim; arm: str; scope: Cohort | Literal["population"]
    look_no: int; n: int; p_pos: float; p_best: float; ew_p_pos: float; harm: HarmReport

@dataclass(frozen=True)
class Decision:                   # written before acting (8.2)
    wf: str | None; module: str; kind: str; rule_id: str
    inputs: dict; outcome: dict; harm_report: HarmReport | None
    snapshot_ids: tuple[int, ...]; seed: int; settings_hash: str; code_version: str
    parent_decision_id: int | None = None

@dataclass(frozen=True)
class WorkflowSnapshot:
    spec: WorkflowSpec; policy: EffectivePolicy; live: Config; live_version: int
    cohorts: CohortSet; engage: EngageArrays; pickup: PickupArrays; did: DidArrays
    flags: FlagSeries; experiments: tuple[Experiment, ...]; did_state: Mapping[str, DidState]
    in_flight: Proposal | None; used_24h: tuple[int, ...]; now: int

# ---------------------------------------------------------------- simclient/client.py
class SimClient:
    def state(self) -> SimState: ...
    def advance(self, minutes: int, timeout_s: float) -> AdvanceResult: ...   # no internal retry
    def list_chunks(self, kind: str, after: int) -> list[ChunkMeta]: ...
    def stream_chunk(self, n: int) -> Iterator[bytes]: ...
    def workflows(self) -> list[WorkflowSpec]: ...
    def config(self, wf: str) -> ConfigSnapshot: ...
    def config_history(self, wf: str) -> list[VersionMeta]: ...
    def request_approval(self, body: ApprovalRequest) -> ApprovalPublic: ...
    def approvals(self, wf: str | None = None, status: str | None = None) -> list[ApprovalPublic]: ...
    def approval(self, aid: str) -> ApprovalPublic: ...
    def apply(self, wf: str, aid: str) -> ApplyResult: ...
    def auto_apply(self, wf: str, body: AutoApplyRequest) -> ApplyResult: ...
    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun: ...
    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]: ...   # used only if CTL_LLM_SUGGEST=1

# ---------------------------------------------------------------- ingest, world, cohorts
class Ingestor(Protocol):
    def ingest(self, chunks: list[ChunkMeta]) -> IngestReport: ...   # exactly-once, in order
class ConfigMirror:
    def on_config_changed(self, tx, event: ConfigEvent) -> ConfigChange: ...  # fetch, store, diff paths
    def body(self, wf: str, version: int) -> Config | None: ...
    def live(self, wf: str) -> tuple[int, Config]: ...
class CohortSet:
    version: int
    def cohort_of(self, cell: int) -> Cohort: ...          # first match, business cohorts first
    def to_config(self) -> tuple[CohortDef, ...]: ...
    def membership(self, config_version: int) -> Mapping[Cohort, frozenset[int]]: ...
    def owner(self, cohort: Cohort) -> Literal["ours", "supervisor"]: ...
    def winner_scope_eligible(self, cohort: Cohort, ws: WorkflowSnapshot) -> bool: ...   # 5.2

# ---------------------------------------------------------------- learning (pure)
class EngagementModel:
    def fit(self, ws: WorkflowSnapshot, kind: Literal["stationary", "ew"]) -> Posterior: ...
    def draws(self, post: Posterior, seed: int, n: int = 4000) -> Draws: ...
def harm_gate(cur: Config, new: Config, draws_ew: Draws, did_post: DidPosterior,
              guard: GuardrailForecast, pop: ActivePopulation, pol: EffectivePolicy,
              alpha_h: float, alpha_d: float) -> HarmReport: ...     # population, cohort, DID, guardrail
def plan_allocations(ws: WorkflowSnapshot, draws_ew: Draws) -> list[PatchRequest]: ...
def declare_winners(ws: WorkflowSnapshot, stat: Draws, ew: Draws) -> list[Winner]: ...
class DecayMonitor:               # state in decay_monitor
    def update(self, ws: WorkflowSnapshot, segment: int) -> list[Finding]: ...
    def rollback_plan(self, ws: WorkflowSnapshot, ew: Draws, t_apply: int) -> list[PatchRequest]: ...
class DidHealth:                  # state in did_state
    def update(self, ws: WorkflowSnapshot) -> list[Finding]: ...      # SPRT, slow path
    def classify(self, ws: WorkflowSnapshot) -> Mapping[str, DidState]: ...
    def plan_caller_ids(self, ws: WorkflowSnapshot) -> list[PatchRequest]: ...   # probes, spread, drain
class HourCurves:
    def fit(self, ws: WorkflowSnapshot) -> HourCurveSet: ...
    def plan_hour_weights(self, ws: WorkflowSnapshot, curves: HourCurveSet) -> list[PatchRequest]: ...
class GuardrailMonitor:
    def predict(self, ws: WorkflowSnapshot, cur: Config, new: Config) -> GuardrailForecast: ...  # mix-adjusted
    def check(self, ws: WorkflowSnapshot) -> list[Finding]: ...       # breach with attribution

# ---------------------------------------------------------------- incidents
def detect(ws: WorkflowSnapshot, state: DetectorState) -> list[Finding]: ...   # DID, prompt, flag, drift
class SafetyVerdict:
    def run(self, wf: str, prompt: str, mode: Literal["sequential", "full", "stage1_only"],
            for_proposal: bool) -> Verdict: ...          # for_proposal: designate the gate run
class IncidentManager:            # state in incident
    def on_findings(self, ws: WorkflowSnapshot, findings: list[Finding]) -> list[PatchRequest]: ...
    def verify(self, ws: WorkflowSnapshot) -> list[Finding]: ...
class CreditBudget:               # state in credit_envelope, credit_ledger
    def mode(self) -> Literal["normal", "low"]: ...
    def authorize(self, tenant: str, pool: Literal["R", "S", "U"], purpose: str, amount: float) -> bool: ...
    def record(self, tx, intent_id: int, pool: str, cost: float, remaining_after: float) -> None: ...

# ---------------------------------------------------------------- planner and governance
class Overlay:
    def add(self, req: PatchRequest) -> Patch: ...
    def compose(self, wf: str, live: Config, classes: set[int]) -> Composition: ...
    def rebase_on_supervisor_edit(self, wf: str, old: Config | None, new: Config, v: int) -> RebaseReport: ...
def changed_dims(old: Config, new: Config) -> list[str]: ...
def max_shift(old: Config, new: Config) -> float: ...
def classify_risk(cur: Config, new: Config, did_state: Mapping[str, DidState],
                  controls: Mapping[tuple[Cohort, Dim], str], history: Sequence[Config]
                  ) -> Mapping[tuple[Cohort, Dim], Literal["reducing", "increasing"]]: ...
def auto_apply_permitted(pol: EffectivePolicy, comp: Composition, velocity: VelocityLedger,
                         holds: Sequence[Hold], now: int) -> tuple[bool, list[str]]: ...
def routine_slots(d: int, pol: EffectivePolicy) -> list[int]: ...
def choose_submit(now: int, pol: EffectivePolicy, used: Sequence[int], cls: int,
                  slot1_terminal: bool) -> int | None: ...    # the one signature (6.3)
class ProposalLifecycle:
    def tick(self, now: int) -> None: ...                 # apply visible approvals, resolve terminals
    def submit(self, comp: Composition, now: int) -> Proposal: ...
    def supersede(self, holder: Proposal, by: Composition) -> Proposal: ...   # one transaction
    def verify(self, now: int) -> None: ...               # VERIFYING checks per kind (6.1)
class ReleaseTrain:
    def run(self, now: int) -> None: ...                  # P0a, P0b, P1, auto, routine slot

# ---------------------------------------------------------------- loop, reliability, audit
class Orchestrator:
    def resume(self) -> ResumeReport: ...                 # lock, run resolution, reconcile, bootstrap
    def run_step(self) -> StepReport: ...                 # one PLAN..OPS pass (4.1)
    def run(self) -> int: ...                             # exit code 0 / 1 / 3
class IntentExecutor:
    def execute(self, kind: str, key: str, payload: dict, decision: Decision) -> Any: ...
class Reconciler:
    def run(self) -> ReconcileReport: ...
def record(tx, *, wf: str | None, actor: str, action: str, subject: tuple[str, str],
           summary: str, details: dict, caused_by: int | None = None) -> int: ...
def replay_decision(decision_id: int, refit: bool = True) -> ReplayReport: ...
```

### 11.3 run.sh and Docker Compose (F10, from C)

```bash
#!/usr/bin/env bash
# Requires Docker with Compose v2. Starts Postgres and the controller in containers and runs until
# the scenario at SIM_URL finishes. Restarts the controller after a crash; state lives in Postgres.
# No Docker: CTL_NO_DOCKER=1 PG_DSN=postgresql://... ./run.sh <SIM_URL>  (local venv, Postgres 16)
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
if [ "${CTL_NO_DOCKER:-0}" = "1" ]; then exec scripts/run_local.sh "$SIM_URL"; fi
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-formi_ctl}"
case "$(uname -s)" in
  Linux) FILES="-f compose.yml -f compose.linux.yml";   CTL_SIM_URL="$SIM_URL" ;;   # host network
  *)     FILES="-f compose.yml -f compose.desktop.yml"
         CTL_SIM_URL="$(printf '%s' "$SIM_URL" | sed -E 's#//(127\.0\.0\.1|localhost)#//host.docker.internal#')" ;;
esac
export CTL_SIM_URL
docker compose $FILES build --quiet
docker compose $FILES up -d --wait db                       # healthcheck: pg_isready
restarts=0
while true; do
  set +e; docker compose $FILES run --rm controller; code=$?; set -e
  [ "$code" -eq 0 ] && break                                # scenario finished
  [ "$code" -eq 3 ] && { echo "fatal configuration error, not restarting" >&2; exit 3; }
  restarts=$((restarts + 1))
  [ "$restarts" -ge "${CTL_MAX_RESTARTS:-20}" ] && { echo "controller failed $restarts times" >&2; exit "$code"; }
  sleep 2
done
docker compose $FILES up -d api                             # leave read-only views up
```

- **Reaching the simulator.** The simulator binds 127.0.0.1, which a bridge-networked container on
  Linux cannot reach (`host.docker.internal` resolves to the docker0 gateway, not loopback). On Linux
  the controller uses `network_mode: host` and `SIM_URL` unchanged; Postgres publishes
  `127.0.0.1:${CTL_PG_PORT}`. On Docker Desktop the controller stays on the bridge and the URL is
  rewritten. A remote `SIM_URL` works in both.
- **Port collision.** `run.sh` picks `CTL_PG_PORT` as the first port in 55432 to 55451 that is free
  (a bash `/dev/tcp/127.0.0.1/<port>` probe that fails to connect), unless the variable is set, and
  passes it to compose and to the controller's DSN; none free: exit 3 with a message.
- **Network at build time** (assumption A-12). `docker compose build` pulls `postgres:16` and
  `python:3.11-slim` and installs wheels. To need no PyPI access, the wheels are vendored in
  `vendor/wheels/` and installed with `pip install --no-index`; the two base images still need a
  registry or a local cache, which `run.sh` checks first (`docker image inspect`) and reports
  clearly if missing.
- **Health polling.** The controller polls `GET {SIM_URL}/health` every 2 s for up to
  `CTL_SIM_WAIT` (default 900 s; the simulator generates history before it listens), then
  `GET /sim/state`. Never answers: exit 3 (no restart loop).
- **Resources.** db `mem_limit` 3 GB, controller 1 GB, api 256 MB.
- **Data volume.** Named volume `pgdata`, never wiped by `run.sh`; runs are separated by schema.

---

## 12. Stakeholder positions

### 12.1 Positions

| Memo | Accept (where it shows) | Push back or decline | Wrong claim against the contract |
|---|---|---|---|
| 01 Sales | One lift figure per client per KPI, from our store (supervisor view, reports). Batched requests so we do not look hesitant (6.2). | One blended score (constraint 3, Product). "Winning by lunch, live by evening": a morning of nw_renewal has about 300 connected calls in total, of which a new variant gets about 12; a 10% effect needs 800 to 1,600 calls on the variant (power 0.52 to 0.93 at our 99.5% bar, 5.5). Rolling out "persuasive" everywhere: it fails our safety verdict (7.1) and is not offered on KYC. | "Kestrel turned on auto-apply for everything": only schedule, caller_id, welcome and voice, at `max_shift` 0.2; never prompt or cohorts. "Northwind is fine with KYC auto-apply": KYC auto-apply is disabled in policy, and Northwind's own Compliance requires human approval. |
| 02 Compliance (Northwind) | No customer-facing auto-apply on Northwind (directive, 6.5), even on `nort_cancel_2` where policy allows it: conflict flagged (F15). Validation evidence on every prompt change, with a stricter internal bar (7.1). Per-call "why heard" (9.4). Approver hours respected (6.3). Weekly operational digest (9.3). "Slow and right" is the default, and item 4 ("worse for a cohort, even briefly") is a per-cohort harm line at 1% for Northwind, stricter than the 2% default (0.1, 5.4), at the cost of half-size first exploration. | "Named human approval": the API exposes only approval id and time; we store those and ask Formi to expose identity. Requests are queued before opening only when no in-hours time is expiry-safe (6.3, pre-open slot), and the view says why. | "Timing and caller-ID rotation are covered by the policy we signed": true for renewal, **false for KYC**, where auto-apply is off and every change goes to approval. |
| 03 SRE | Kill-safety with a crash-equivalence matrix and a random kill loop (10.2); idempotency keys everywhere (8.1); Postgres only (no Redis, ADR 0001); actionable pages only (9.2); loop health at `/ops`; measured public_03 numbers and a 10x table (13.2). | No Prometheus stack yet (C14 partial). | None. |
| 04 Kestrel Ops | Her caps as ours on the cancellation line she runs (directive scoped by use case, 6.5; Kestrel's other workflows get the default): 2 a day, never before 11:00; one request with many items; one screen; 60-minute notice before auto-applies; her edits never silently undone (rebase, cooldown, 6.7); no "word for word" script (LKG first, `avoid_prompt_terms`, engagement watch). | A third request only for an incident, still within the policy's 4, and the screen says why. There is no notification channel, so "tell me before" is the Upcoming list. | None false; her own prompt edit is exactly the incident class we catch, and we fix it through her approver with evidence. |
| 05 Product | Separate KPIs; one cohort object with versioned identity (5.2); impact score with units (6.6); expired = rejected, re-queued from fresh data (6.1); evidence only from calls that ran (5.3); minimum cohort sizes (5% of leads to target, 15% of calls to declare a cohort winner) and windows chosen with their cost on the real ramp (5.2, 5.5); winners stay in their workflow. | Queue ranked by class first, then impact. P0 list: caller-ID health yes, supervisor view yes (minimal), per-person call time no (too few calls per person; per-cohort timing captures most of the archetype signal). | "Everything on the wishlist is P0": not with this data. |
| 06 CTO | Outcomes a compliance head can sign (harm gate, holdout, audit); credits only where they change a decision (about 23 of 200 predicted on public_02); scale numbers and a 10x story; the cut list written down. | - | "Credits are cheap here" is only partly true: 200 credits, and a careless retry loop on validation would burn an incident's budget; we keep an ordered reserve of 130 (two worst-case incidents) and do not spend 3 credits on suggest-fixes when it can only return catalog prompts we already rank (7.1). |

### 12.2 Replies (each 120 words or fewer, ready to paste)

**To Nikhil (Sales).** Nikhil, for the summit you will get one lift figure per client per KPI,
pickup and engagement shown separately, taken from our audit store. No blended score: Product and our
client contracts keep the KPIs separate. "Winning by lunch" is not real at pilot volume: a morning has
about 300 connected calls, a new variant gets a dozen of them, and a 10% effect needs 800 to 1,600
calls on the variant. At production volume winners ship in one to two days. Kestrel's auto-apply
covers timing, caller ID, welcome and voice in small steps, never prompts; Northwind KYC has no
auto-apply. The persuasive prompt fails our safety validation far more often than today's, so it
will not ship.

**To Meera (Northwind Compliance).** Meera, we accept all five conditions. No customer-facing change
(opening line, voice, script) goes live on Northwind without your approvers, including nort_cancel_2,
whose signed policy allows welcome and voice auto-apply; please align it with your memo. Script
changes carry validation evidence against a stricter bar than your 0.97 gate. Every change must show,
before it ships, that no customer cohort is likely to lose more than 1% engagement. One correction:
the policy covers timing and caller-ID rotation on renewal, not KYC, where every change comes to you.
We answer "why did this customer hear this" per call, from our own records. The approval system
gives us an approval id and time, not the approver's name. Weekly summaries start now.

**To Kabir (SRE).** Kabir, accepted as written. State lives only in Postgres; no Redis and nothing
new to operate. Every side effect is written ahead as an intent and reconciled on restart. The crash
test kills the controller at every side-effect boundary plus 20 random SIGKILLs, and the grade report
must match a clean run. Pages only for what needs a human now (stuck ingest, an approval about to
lapse, a policy-check mismatch, credits below the incident reserve), each linking a runbook; the rest
goes to a morning digest. Loop health is at /ops. You will get measured public_03 numbers and the
10x breakdown: the per-chunk lead join and the serial transform break first, and we partition by
workflow and tenant.

**To Anita (Kestrel Ops).** Anita, on your cancellation line your rules are ours: at most two
requests a day, never before 11:00, each batching everything we want to change. We never undo your
edits silently. If one of your prompt changes starts hallucinating, we send one request with the
validation evidence and the prompt we suggest returning to; only then would we use a third request,
and your screen will say why. Timing, caller-ID and voice adjustments your policy allows appear on
your screen at least an hour before they happen, and you can hold them. We will not push a
word-for-word script: fixes go back to a prompt your callers already responded to. One screen:
tested, winning, needs you, changed today.

**To Tara (Product).** Tara, we hold every definition: separate KPIs, one versioned cohort object,
expired treated as rejected and rebuilt from fresh data, evidence only from calls that happened. A
cohort needs 5% of leads for its own targeting and 15% of calls for its own declared winner; smaller
cohorts benefit through allocation. A winner needs 99.5% posterior confidence (99.8% for a cohort),
200 calls per arm over two days, and recent data that agrees. Dropping to 95% lifts early decisions
from 52% to 87% but multiplies false winners eighteenfold. The impact score is the cautious lift on
the KPI the change moves. The queue sorts by urgency first, then impact. Caller-ID health and the
supervisor view ship; per-person call time does not.

**To Aryamann (CTO).** Aryamann, the bet: every change passes the grader's population harm test plus
a stricter per-cohort one, so we learn slowly rather than hurt a cohort. Caller-ID health and cohort
call timing carry pickup; welcome and voice learn through harm-limited exploration, with a holdout to
catch decay. Credits go only where they change a decision: prompt screens, a reserve for two
worst-case incidents, no LLM call that cannot change the fix. I said no to blended scores, to
auto-apply where clients asked for humans, to per-person timing and to prompt A/B tests. Kestrel's
auto-apply, Northwind KYC auto-apply and KYC caller-ID rotation claims were false. At 10x the lead
join and serial transform break first; partitioning by tenant fixes both.

### 12.3 Trade-offs and rejected alternatives (SUBMISSION section 12)

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Fixed split (50/50, or 10% per challenger) | Standard, simple to explain | 10% on each of 5 challengers fails the harm gate on D0, and one x0.88 arm makes the launch about -1.2%, graded harmful. Instead: harm-gated probability matching (5.4). |
| Plain Thompson sampling | The textbook bandit | Random per decision (constraint 4), moves traffic every step (each move is a governance action), no bound on per-change harm. Instead: Thompson's allocation rule as a seeded, deterministic expectation (ADR 0004). |
| One challenger per dimension at workflow level, options in listed order (proposal B's queue) | Fewer comparisons, simple | Sign-flipping cohort effects average to null at workflow level, and late-listed options are tested last. Instead: factorial model with arm x feature terms, all arms at once (5.3). |
| One W1 threshold (0.99) for every scope | Simpler | 1.28% per population and 2.8% per cohort null hypothesis under unequal sizes. Instead: 0.995 and 0.998 per scope (5.5). |
| Relative guardrail reading (2% of the rate) | Literal reading of `max_drop` | 2% of a 0.26 pickup is 0.5 points, unmeasurable at dev volume, so unenforced. Instead: absolute, with "no predicted drop" for proposals (5.10). |
| SNIPS off-policy replay as the dry run | Unbiased estimate | High variance at dev volume and a large build; cut. Instead: stratified posterior replay (6.6). |
| Population-only harm gate | Mirrors the grader exactly | Lets a cohort fall below -1% while the population is neutral (Compliance item 4, CTO). Instead: plus a per-cohort line, 2% default, 1% by directive (0.1). |
| Day-based DID classification timeout | Simple | Drops healthy DIDs at dev volume (415 dials needed at r = 1.0). Instead: dial-count SPRT (5.9). |
| `suggest-fixes` on every incident | The C8 wording | Can return only catalog prompts we already have, a random 3 of them, for 3 credits. Instead: the full catalog in a fixed statistical rank; LLM behind a flag for production (7.1). |
| Pooling prompt verdicts across workflows (one verdict per prompt id) | Would save credits: hallucination looks global per prompt id in the simulator | The API never says prompts are shared across workflows, so relying on it is tuning to the simulator; and approval evidence must be a run for the same workflow anyway. Instead: per-workflow verdicts. |
| Re-running validation until one run passes | The approver accepts any single passing run | Cherry-picking. Instead: one designated evidence run per decision, chosen before it runs (7.1). |
| A DID drain superseding a pending P0 prompt fix | Faster drain | Burns an approval slot and a latency and breaks the strictly-higher-class rule. Instead: same-class P0 order, wait or bundle (6.2). |
| Always finishing the drain chain before the prompt fix | Simple ordering | Adds up to 90 minutes of MTTR at `max_shift` 0.05. Instead: chain first only if it ends within 30 minutes, else bundle (6.2). |
| Chained exposure-increasing auto-applies | Faster ramps and spreads | Walks around `max_shift`, the client's signed limit. Instead: velocity rule; only risk-reducing P0 and P1 chain (6.5). |
| Per-person call time (`lead_preferred_hour`) | Product's summit headline | 2 to 3 dials per person per campaign, and a wrong guess costs x0.25 elsewhere. Cut (section 1). |
| Prompt A/B outside incidents | The prompt is a large lever | No concurrent control; a switchback needs about 30 dev days. Cut (section 1). |
| SQLite, Redis, LangGraph | Less infrastructure, or a ready-made state machine | ADR 0001: node-local state, weaker durability, duplicated checkpoint state. |

---

## 13. Top risks and the scale envelope

### 13.1 Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | Dev volume makes winners slow, so engagement lift is small | Lower outcome score | Factorial design, cohort effects via interactions, probability matching inside the harm gate, exploitation paying for exploration; pickup levers (DID, schedule) carry dev-scale outcomes; at load the same rules converge in 1 to 2 days. |
| 2 | The harm prior (`tau_H` 0.06, mean 0) is wrong for a hidden scenario | Exploration graded harmful | `tau_H = max(0.06, tau_EB)` widens with observed dispersion; exploration starts at no more than 3.8% per arm (1.9% under a 1% cohort line); every increment is gated on fresh EW posteriors; residual risk per exploration change at most 2.5% under the prior. |
| 3 | Hidden policies differ (auto-apply off, cap 1, TTL 4h, long latency, narrow approver hours, stricter validation gate) | Expiries, lapses, wasted slots, unapprovable fixes | Every parameter from the API; `p_expire` search over the whole day; fixed routine slots; reserve 0 at cap 1; the J2 and 9.7 policy cases as E2E tests; tight-gate evidence rule (7.1); no safe window in 24h raises an alert instead of guessing. |
| 4 | Our own auto-apply or a supervisor edit stales a pending approval | apply_conflict | `one_in_flight` index; precedence table (6.2); pre-apply base check; STALE never calls apply. |
| 5 | Decay at dev volume detected late | Decay share metric | EW gates block ramps and promotions early; CUSUM armed from 10% share; rollback timed to the predicted apply time; residual one to two days at dev volume, stated. |
| 6 | Probe onto a pre-flagged DID | Harmful change | Probes at most 2.8% from the DID gate (less at volume), at most two unknowns per change; worst case -1.8% (neutral); SPRT classifies a flagged DID in about 52 dials. |
| 7 | Effects on feature combinations, or welcome x voice interactions, the additive model misses | Missed lift or hidden cohort harm | All three features in the model; per-cohort harm line; holdout and decay monitor see a smaller-than-predicted effect after promotion. The LR test and regrid are cut, so this risk is accepted and stated (13.4). |
| 8 | Credits exhausted mid-incident | Constraint 7 failure | Ordered pools: reserve `R = min(0.8 C0, max(0.3 C0, 2 W))`, `W = 13 K`, then the D0 screen pool `min(0.25 C0, C0 - R)`; LOW mode (stage-1-only verdicts, no screens) whenever R plus U is below W; no re-runs to fish for a pass; intents reconciled through the balance (7.2). |
| 9 | The SQL transform is slower than estimated | SRE target | M1 measures before learning lands; Python parser behind the same interface; P-way split designed (13.2). |
| 10 | The ridge-logit allocator overruns M5 | Unshipped core | Fallback behind the same `plan_allocations` interface: per-cohort Beta-binomial posteriors (no interactions) with the same harm gate and probability matching; the M5 acceptance tests are identical for both. |
| 11 | A safety fix for a P4-like prompt is graded harmful | One harmful verdict | Accepted and disclosed (7.1): compliance first; pick the safe candidate with the best live engagement. |
| 12 | Directives keyed by tenant and use case do not match hidden workflows | Too strict or too loose | The `*` default is conservative and can only tighten the API policy; scope by use-case keyword is stated (A-11). |
| 13 | Docker networking or image build fails on the grading box | Floor failure | Host network on Linux, URL rewrite on Desktop, health polling, exit code 3, no-Docker fallback (11.3). |
| 14 | History hour curves biased by eligibility | Weak schedule | Attempt-bucket stratification, shrinkage, clip to [0.25, 4], post-change checks; schedule changes cannot be graded harmful. |
| 15 | Guardrail definition ambiguous | Rubric disagreement | Absolute reading plus a stricter "no predicted drop" proposal rule, both stated (A-3). |

### 13.2 What breaks first at 10x (8.4M events per sim day) and how to partition

| Order | What breaks | At 1x (public_03) | At 10x | Fix |
|---|---|---|---|---|
| 1 | Lead join and fact indexes outgrow RAM | `lead_dim` 2.3M rows, hot | 23M leads, about 3.8 GB with index; random I/O in the per-chunk join | Partition `lead_dim` by campaign day and join only campaigns within cadence days (about 3 days); drop `call_fact` partitions after the audit horizon (archive elsewhere). |
| 2 | Serial SQL transform | est. 15 to 25 s per day | est. 150 to 250 s per day plus simulator time | P-way transform: the loader splits lines by `hash(workflow_id) % P` into P staging partitions (byte search, no JSON parse); P backends transform concurrently; a chunk is complete when all `(chunk_no, part)` markers exist. |
| 3 | Serial per-workflow decide | 12 workflows x 14 steps x about 75 ms = 13 s per day | 120 workflows: about 2 minutes per day | Process pool by `hash(tenant) % W`: snapshots are pure reads, patches and proposals are per workflow, isolated by `one_in_flight`. |
| 4 | Disk | about 1 GB DB + 4.1 GB simulator chunks | about 10 GB DB | Retention, partition drops, bigger volume. |
| 5 | Credits and approver attention | per-workflow caps suffice | many tenants share ops time | C15 proper: per-tenant budgets and a fair queue for approval slots. |

In production there is no `advance`: events arrive as a stream. The same design partitions by
tenant: each worker owns a set of tenants through a lease row (advisory lock per partition), consumes
that partition's stream into its own staging and runs detect, decide and govern only for its
workflows. Governance and audit tables are already keyed by workflow, so nothing is shared across
partitions except the read-only views and the credit pool.

### 13.3 Assumptions referenced above (go into SUBMISSION section 1)

- **A-1** No welcome x voice interaction. Stated, **not tested** (the LR test is cut, 5.3).
- **A-2** Arm log-multipliers a priori `N(0, 0.06^2)` at population scope and sd 0.12 at cohort
  scope for the harm gate, widened by observed dispersion.
- **A-3** Guardrail `max_drop` is an absolute drop, measured over a rolling 2-day window against a
  concurrent comparator where one exists; proposals must predict no drop, including through the
  cohort mix (5.10).
- **A-4** Directives (Kestrel's 2 a day after 11:00, Northwind's no customer-facing auto-apply and
  1% cohort line) are client agreements honoured above policy; workflows without one get the `*`
  default.
- **A-5** (amended) Auto-applies chain, one `max_shift` step per tick, only when every step is
  risk-reducing and P0 or P1 (drains, rollbacks, harm stops, failed verifications); a chain ends
  after at most `ceil(share / max_shift)` steps. Exposure-increasing auto-applies never exceed
  `max_shift` cumulative TV per (cohort, dim) per 24h.
- **A-6** Untested caller IDs: `P(flagged) = 0.2`, flagged relative pickup 0.35.
- **A-7** DID health falls with recent load (equal split is the target among equally healthy DIDs).
- **A-8** The grading box runs Docker; `run.sh` says so and offers a no-Docker path.
- **A-9** Hour of day, caller ID and schedule do not change engagement given connect (the effect
  model has no hour terms; checked by adding them in a diagnostic fit). They change who connects,
  which is modelled (5.10).
- **A-10** The approver's identity is not available from the API (`approver_name` stays NULL).
- **A-11** Directive scope follows the memo's author: Northwind Compliance is tenant-wide; Kestrel
  Ops covers only Kestrel workflows whose `use_case` contains "cancel" (6.5).
- **A-12** The grading box can pull `postgres:16` and `python:3.11-slim` (or has them cached);
  Python wheels are vendored, so no PyPI access is needed at build time (11.3).
- **A-13** The grader's wall-clock timeout is at least 2 hours per scenario. Our estimate: a dev
  scenario in a few minutes; public_03 about 3 minutes of history bootstrap plus 5 x 1 to 1.5
  minutes per sim day (8.7), measured in M1.
- **A-14** Some port in 127.0.0.1:55432 to 55451 is free for Postgres on Linux host networking;
  `run.sh` picks the first free one (11.3).
- **A-15** Approvers work every day: `approver_hours` carries no weekday, and the simulator's
  `next_working` ignores weekends (world.py:579). If a hidden scenario had weekends, `p_expire` would
  under-estimate expiry; the contract test (`test_sim_contract`) checks this first.
- **A-16** "Risk-reducing" is the formal definition of 6.5, computed from the composed config, and
  only risk-reducing changes are exempt from the velocity budget; `customer_facing_auto_apply:
  risk_reducing_only` uses the same definition.
- **A-17** Cohorts, allocations and schedules in a business default are the business's: kept,
  supervisor-owned, never redefined (5.2).
- **A-18** The per-cohort harm line is our standard, not the grader's; the grader judges the
  population only.

### 13.4 Known weaknesses (SUBMISSION section 13; separate from the risks above)

Risks are things that might go wrong; these are things we know are weak in the design as built.

1. **Engagement learning is slow at dev volume.** Population winners are possible from about D4,
   cohort winners only for cohorts of about half the traffic and only from about D6, and the
   Northwind 1% cohort line delays both by about 1.5 days (5.5). Most dev-scale lift comes from
   pickup levers.
2. **A validation band our bar does not close.** A prompt with a validation failure rate between
   about 1.4% and 2% passes our FULL verdict 60% to 95% of the time (7.1). The live flag CUSUM is
   the backstop and needs about 1.4 days for a doubling.
3. **A strict client gate makes safe fixes unreliable.** At g = 0.99 a safe prompt with a 1.04%
   failure rate is approvable on one designated run about 72% of the time; otherwise the incident
   moves to the next candidate or stays open (`CLIENT_GATE_UNREACHABLE`).
4. **A DID drain can wait behind a pending prompt fix** for up to `lat_hi + 2 x tick`, 8.5 hours at a
   KYC-like latency of 480 minutes, because a P0a approval is never staled (6.2).
5. **The flag-day share of a flagged DID is not bounded below 0.10.** It is 0.079 to 0.109 at
   `max_shift` 0.2 and up to 0.146 at 0.05, depending on the share the DID carried before the flag
   (7.3). No faster drain exists inside the policy.
6. **KYC-like spreads arrive late.** Probes reach only new campaigns, so a fresh DID at r = 1.1 is
   classified on about D4 and fully live about three days after its approval (5.9).
7. **At a fatigue cap of 1, an incident can wait up to 24 hours** for the slot our routine request
   holds (6.4, 9.7).
8. **No interaction test and no regrid.** A welcome x voice interaction or off-grid heterogeneity
   is learned only through main effects and existing cohorts (A-1).
9. **The drift detector is slow at dev volume** (10 to 20 dialling hours for a 10% drop) and is
   alert-only (7.5).
10. **Decay at dev volume is seen 1.5 to 2 days after onset** (5.7).
11. **Hour curves come from history biased by eligibility**; per-person timing is cut (5.8).
12. **A safety fix away from a P4-like prompt may be graded harmful**; accepted and disclosed (7.1).
13. **Approver identity is unavailable**, so "named human approval" is an approval id and a time.
14. **The supervisor view is JSON only, with no auth**; holds are the only write.
15. **Several numbers are estimates until measured**: wall time per sim day (8.7), fit time (5.3),
    the transform rate (3.1). M1 and M5 replace them.

### 13.5 If this went to production at Formi next month: the first two weeks, in order

1. **Shadow mode on three real workflows** (days 1 to 3): consume the production event stream into
   the same Postgres schema, run detect and decide, write every decision and the would-be proposal
   to the audit store, submit nothing. Compare dry-run predictions and harm-gate probabilities with
   what the business default then did.
2. **Replace `advance` with the stream** (days 2 to 5): the per-tenant partition and lease design of
   13.2, ingest lag and decision lag on `/ops`, and the intent protocol against the real governance
   API (it has no `advance`, so reconciliation keys change).
3. **Differential test of the policy replica** against the production governance service on staging
   (days 3 to 5); any disagreement blocks auto-apply.
4. **Re-derive every `[T]` on private variants at real volumes** with Product (days 4 to 6); review
   the threshold register and the per-cohort harm lines with each client's compliance contact.
5. **Turn on caller-ID health for one tenant** that allows caller-ID auto-apply (days 6 to 8), with SRE
   on call, the runbooks live and paging routed.
6. **Ask Formi platform for approver identity** in the approval API and wire `approver_name`
   (days 6 to 10); until then the Compliance gap stays in the weekly digest.
7. **Routine approvals on one engagement-primary workflow** with a willing client (days 8 to 10),
   reviewed daily with its supervisor.
8. **Prometheus exporter and dashboards** from `loop_step` (days 8 to 10), replacing `/ops` polling.
9. **Game day** (days 11 and 12): kill the controller and fail over Postgres in staging under load;
   compare with the crash matrix.
10. **Week-2 review** (days 13 and 14): shadow-mode calibration, false alarms per detector, credits per
    tenant, and the go or no-go for the next tenants.

---

## 14. Decision records to write

Each is `docs/decisions/NNNN-title.md` with context, decision, alternatives and consequences, written
when its milestone starts, so the reasoning can be defended in the live review.

| ADR | Title | Decision in one line |
|---|---|---|
| 0002 | Harm gate mirrors the grader, plus a cohort line | Every change except a P0 prompt fix must pass `P(de < -1%) <= 2.5%` (10% for rollbacks), `P(de_k < -x_k) <= 2.5%` per cohort (x = 2% default, 1% by directive) and `P(dd < -2%) <= 5%` on our own recency-weighted posterior. |
| 0003 | Engagement effect model | Factorial ridge-logit with arm x feature terms over lang, age and tier (about 300 to 450 parameters), Laplace posterior, stationary and EW fits, history variants used where the business mixed them; no LLM and no per-cohort experiments. |
| 0004 | Probability matching with seeded draws | Allocation targets are P(best) from 4,000 seeded draws, moved toward by the largest harm-safe step per cohort; exploration and risk-reducing moves exempt from the minimum-gain rule; chosen over fixed splits, Thompson sampling and successive elimination. |
| 0005 | Winner rule and false-winner control | Stationary P(theta > 0) >= 0.995 at population scope and 0.998 at cohort scope, P(best) >= 0.95, recency check on the EW fit, 200 calls over 2 days, information-doubling looks under unequal arm sizes; union bound at most 0.54% (population) and 0.47% (cohort) per null hypothesis; cohort winners only for cohorts of at least 15% of calls. |
| 0006 | Holdout, decay monitor and rollback timing | Holdout sized from a one-day SE target, Page CUSUM from 10% share, rollback when the predicted state at apply time passes a 10% harm tolerance. |
| 0007 | Guardrail definition | Absolute drop of `max_drop` points over 2 days against a concurrent comparator; proposals must predict no drop, including through the cohort mix; relative reading rejected as unmeasurable. |
| 0008 | Caller-ID probing, classification, spread and drains | Probes at most 2.8% from the DID gate and sized from volume, dial-count SPRT classification, equal split over healthy DIDs, multi-window MH fast detector with a 1h suspect window, slow-path reclassification below 0.85, per-tick risk-reducing drain chains. |
| 0009 | Prompt safety verdict and gate evidence | Two-stage verdict with a fixed bar (stage 1 UNSAFE at 10 of 300, pooled 1,300 at 2%), separate from one designated gate-evidence run sized from g; D0 screen of defaults; LKG first, then every catalog prompt in a fixed statistical rank; suggest-fixes not called by default. |
| 0010 | Submit timing and the apply guarantee | Submit times chosen by an exact expiry-probability search over the whole day (at most 1%), `expires_in_hours` at the policy TTL, step bound `S_watch_max - tick`, re-check after 20h. |
| 0011 | Fatigue reserve and fixed slots | `reserve = 1` only when `C_eff >= 2`; routine on alternate days at cap 1; P0 and P1 may exceed the directive cap but never the policy cap; P2 and P3 only at fixed slot minutes, enforced in `choose_submit`. |
| 0012 | Directives layer | Effective policy is the stricter of API policy and client agreements, scoped by tenant and use case; `*` default allows only risk-reducing customer-facing auto-apply; nort_cancel_2 conflict raised with the client. |
| 0013 | One change in flight and priority classes | `one_in_flight` partial unique index; P0 > P1 > P2 > P3 with P0a prompt safety before P0b DID drain; supersede only by a strictly higher class, except a P0a bundle replacing a pending P0b approval; a pending P0a approval is never staled; short drain chains finish first, long ones are bundled. |
| 0014 | Local policy replica | Re-implement `changed_dims` and `max_shift` exactly, differential-test against a live simulator, never probe auto-apply, treat any 403 as a bug. |
| 0015 | Supervisor edits: overlay and cooldown | Patches own paths; a supervisor edit invalidates owned paths, interrupts experiments, becomes the control, and starts a 24h cooldown; business cohorts are supervisor-owned from the start; never a silent revert. |
| 0016 | Ingest as Postgres ELT | COPY raw lines and transform set-based in one transaction per chunk with a count check; history calls go to rollups only; Python parser kept as fallback. |
| 0017 | Run identity | Advisory lock on the SIM_URL key first; schema per run; resume only after fingerprint and consistency checks against the live simulator. |
| 0018 | Crash safety protocol | Write-ahead intents with deterministic ids, reconciliation by kind, credit-balance resolution for validation and LLM calls, crash-equivalence as the acceptance test (invariants for the validation cells). |
| 0019 | run.sh and networking | Compose with host networking on Linux and a URL rewrite on Desktop, free-port selection, vendored wheels, restart loop, exit code 3 for fatal configuration, no-Docker fallback. |
| 0020 | Impact score definition | Q20 of the expected relative change, over 7 days, of the KPI the lever moves, stored with its KPI label; queue ranked by class first. |
| 0021 | Credit budget | Ordered pools: reserve `min(0.8 C0, max(0.3 C0, 2 W))` with `W = 13 K`, then D0 screens `min(0.25 C0, C0 - R)`, then unallocated; LOW mode (stage-1-only verdicts, no screens) when the reserve cannot cover one worst incident; 402 as an incident. |
| 0022 | Cohort grid | Business cohorts first and untouched, then F1 x F2 cells from history with a 5% targeting minimum and a 15% winner-scope minimum, versioned identity used as the simulator cohort id; regrid cut. |
| 0023 | Scope cuts | Per-person timing, prompt A/B, cross-workflow promotion, interaction LR test, regrid, policy fuzzing beyond the attack cases, SNIPS and the HTML page cut; C10, C11, C14 and C15 minimal; 16.0 hours with the numbers that justify each. |
| 0024 | KPI drift detector | Hour-, attempt-, mix- and own-change-adjusted O/E per dialling hour, downward Page CUSUM k 0.5, h 6 (ARL0 about 214 dialling days), alert-only, the comparator of last resort for caller IDs. |
| 0025 | Risk-reducing changes and chaining | A formal per-dim definition computed from the composed config (never from the emitter); only risk-reducing P0 and P1 changes chain, bounded by the share removed; exposure-increasing auto-applies stay within `max_shift` per 24h. |
| 0026 | Decision replay | Store posterior inputs, seeds, settings hash and code version on every decision; `replay-decision` recomputes any gate from the audit store. |

---

## 15. Implementation plan

Every acceptance criterion below is an end-to-end check that starts the real simulator
(`python -m sim serve`) as a subprocess, runs the real controller through `run.sh` against a real
Postgres, and asserts on the grader output and our store, unless it is marked "unit". Each milestone
ends with its ADRs written and a commit per coherent step (no squash).

### 15.1 Milestones

| M | Name | Modules | Acceptance (E2E against the real simulator) | Est. hours |
|---|---|---|---|---|
| M0 | Contracts | `types.py` (11.2), `store/migrations` (section 3), `settings.py` with every `[T]` of 5.11, `directives.yaml` (scoped, 6.5), `SimClient` interface | Migrations apply on a fresh Postgres from compose; `test_sim_contract` passes against a live simulator (lazy lapse, id consumption rules, decision visibility after the tick, strict fatigue window, `next_working` without weekends, validation ids sequential). | 1.0 |
| M1 | Platform and ingest | `simclient`, `store/db` (SIM_URL advisory lock, schema per run), `world/fingerprint`, `ingest/*` (ELT and Python fallback), `compose*.yml`, `Dockerfile`, `run.sh` (free port, vendored wheels), `scripts/load_test.sh` | (1) `./run.sh` with a no-op loop that advances one day at a time finishes public_01 with exit 0, on Linux (host network) and on Docker Desktop. (2) public_03: history and D0 ingested; ingested events equal `state.last_event_id`; rollups (including tick-keyed `r_did`) equal a from-scratch recomputation; SQL and Python paths give identical rollups on a fixture; wall time per sim day, events/s and RSS recorded in `reports/public_03_load.md`, replacing every estimate in 8.7 (target under 120 s per day, bar 600 s). (3) Restarting the simulator or re-running the scenario creates a new schema; restarting only the controller resumes the same one. (4) A kill mid-COPY resumes with exact counts. | 3.0 |
| M2 | Loop and reliability | `loop/stepper`, `loop/orchestrator`, `reliability/intents`, `reliability/faults`, `audit/writer`, `obs/metrics` | public_01 and public_02 complete with zero config changes; every step obeys `S_watch_max - tick` while a synthetic pending marker is set and no overnight jump happens before `close + tick` or with a local `APPROVED` proposal; crash-equivalence matrix for the `advance` and `chunk_transform` kinds equals the clean run; `test_determinism` passes; an interrupted bootstrap reruns. | 2.0 |
| M3 | Governance core | `governance/policy` (incl. `classify_risk`), `timing` (fixed slots), `lifecycle` (VERIFYING, auto-release), `release` (P0a/P0b order), `builder`; `world/registry` (EffectivePolicy, scoped directives); `planner/overlay` | With a probe-only learner (DID probes and schedule from history only): public_01, public_02, the J2 cases and the 9.7 policy rows have 0 `auto_apply_rejected`, 0 lapsed, 0 expired, 0 apply conflicts, peak requests at most `C_eff`; custom cases (a) TTL 12h with latency 60-480, (b) approvers 14-16 with TTL 4h, (c) cap 1 each get at least one approval applied per day; every P2 or P3 submit lands on a slot minute; the public_02 supervisor voice edit marks our pending proposal STALE and is never reverted; differential `policy_check` test agrees on 2,000 pairs; crash matrix for `submit_approval`, `apply` and `auto_apply` equals the clean run. | 2.5 |
| M4 | Pickup levers | `cohorts/*` (business cohorts first), `learning/schedule`, `learning/did` (SPRT, slow path), `incidents/detectors` (DID, drift), DID drain in `incidents/manager` | public_01: nw_kyc pickup above baseline, renewal and cancel pickup above baseline, 0 harmful; public_02: flagged-DID flag-day share at most the 7.3 formula bound for the measured pre-flag share plus 0.01, and 0 after; custom (d) `max_shift` 0.05 drain within its formula bound; custom (h) pre-flagged unknown DID at 0.3 produces no change with dd below -2%; `test_did_sprt` and `test_drift_cusum` pass; a sole-DID custom world with a pickup drop raises `KPI_DRIFT` and starts probes. | 2.0 |
| M5 | Engagement learning | `stats/*`, `snapshot`, `learning/engagement`, `harm` (population and cohort lines), `allocator`, `winners` (per-scope W1, persisted looks), `decay`, `guardrail` (mix-adjusted) | public_01 and public_02: 0 harmful, engagement gap captured above 0 on every engagement-primary workflow; custom (g) decaying strong winner: no ramp or promotion graded harmful, decay share under 10% by onset + 2 days; custom (j) winners only among last-listed options with sign-flipping cohort effects: gap captured above 0, 0 harmful, no cohort's predicted `de_k` below `-x_k` at any applied change; `test_false_winner_rate` upper bound at most 1% per hypothesis in each scope under adaptive allocation; unit fixtures from 5.4 to 5.6 pass; fit time measured. | 2.5 |
| M6 | Prompt safety and credits | `incidents/safety` (verdict, designated gate run), prompt trigger and flag CUSUM in `incidents/detectors`, ranked fix selection and verification in `incidents/manager`, `incidents/credits` (ordered pools, LOW mode) | public_02: hallucination MTTR under 4h (predicted 75 to 165 min, mean about 122), credits used at most 40; custom (e) prompt fix pending when a DID is flagged: the prompt-fix approval is never stale or superseded, MTTR under 4h; custom (f) P4-like supervisor prompt: MTTR finite in at least 18 of 20 seeds; custom (i) unsafe default: fix proposed with a FULL verdict and a designated run; a g = 0.99 world: no safe prompt declared UNSAFE by the verdict, evidence run at n = 100; a credits-50 world runs in LOW mode without a 402; crash at `validation:after_http` records `lost_validation_run`, costs at most one extra run, and passes the 10.2 invariants. | 1.5 |
| M7 | Surface and deliverables | `audit/explain`, `audit/replay` and CLI, `api/app` (JSON, `ctl_api` role), `obs/alerts`, `docs/RUNNING.md`, `docs/RUNBOOK.md`, reports, `SUBMISSION.md` | On a finished public_02 run, `why-changed --workflow ks_cancel --at "D3 14:00"` returns the version, proposal, approval id, decision inputs and the verification that followed; `why-heard` returns a lead's calls with per-dim provenance, including inherited default rows and the live prompt; `replay-decision` reproduces 20 sampled decisions exactly; `ctl_api` cannot write outside `hold`; `scripts/kill_test.sh public_02` completes with all invariants; every alert code has a runbook anchor; `reports/public_01.md`, `public_02.md`, `public_03_load.md` and `crash_recovery.md` exist; SUBMISSION has every heading (15.4). | 1.5 |
| | **Total** | | | **16.0** |

### 15.2 Parallel build

```
day 1    M0 (one engineer, short; it is the contract)
           |
           +-- Eng 1: M1 platform and ingest ------> M2 loop and reliability ------+
           +-- Eng 2: M3 pure parts (policy, risk, timing, lifecycle, overlay,      |
           |          builder) unit-tested on fixtures; differential test needs M0 -+--> M3 E2E
           +-- Eng 3: stats + M5 pure parts (glm, draws, boundaries, harm gate,    |
           |          allocator, winners, decay) on synthetic snapshots            |
           +-- Eng 4: M6 pure parts (verdict, gate evidence, flag CUSUM, credits)  |
           |          and the M4 statistics (SPRT, drift CUSUM)                    |
           +-- Eng 5: M7 api, explain and replay against the schema and a fixture DB
                                                                                    v
         integration (critical path): M2 -> M3 E2E -> M4 E2E -> M5 E2E -> M7
                                      M6 E2E needs only M3 E2E, runs beside M4 and M5
```

The contracts between tracks are the schema (section 3) and the dataclasses and interfaces (11.2).
Learning and incident modules emit `PatchRequest`s and never call the simulator for config changes,
so tracks 3 and 4 can be finished before governance is wired in.

### 15.3 If behind schedule, cut in this order

1. The weekly digest and the drift detector's upward CUSUM (the view loses "better than expected").
2. The slow-path DID reclassification (the fast detector and the drift detector remain).
3. The ridge-logit allocator falls back to per-cohort Beta-binomial posteriors behind the same
   interface (risk 10), keeping the population and cohort harm gates, EW recency, winner rule and
   holdout.
4. Never cut: M1 to M3 invariants, the harm gate, DID drain, prompt safety verdict, crash safety,
   audit store, replay.

### 15.4 Map from this document to SUBMISSION.md

| SUBMISSION heading | Source sections here |
|---|---|
| 0. TL;DR | 0.4 (results pasted after M7) |
| 1. Assumptions | 13.3 (A-1 to A-18) |
| 2. Scope decisions | 1 (table, principle, hours), 15.3 |
| 3. Stakeholders | 12.1, 12.2 |
| 4. Architecture | 2, 3, 8.5 |
| 5. The learning loop: cohorts | 5.2 |
| 5. Experiment design | 5.3, 5.4 |
| 5. Deciding a winner | 5.5 |
| 5. Decay and rollback | 5.6, 5.7 |
| 5. Call time and caller ID | 5.8, 5.9, 7.3 |
| 5. Guardrail KPI | 5.10 |
| 6. Governance | 6.1 to 6.7 |
| 7. Incidents | 7.1 to 7.5 |
| 8. Scale | 8.7, 13.2, `reports/public_03_load.md` |
| 9. Reliability | 8.1 to 8.6, 10.2, `reports/crash_recovery.md` |
| 10. Audit and observability | 9.1, 9.2, 9.4 to 9.6 (a real `why-changed` output pasted after M7) |
| 11. Results | `reports/public_01.md`, `reports/public_02.md`, compared with 7.4 |
| 12. Trade-offs and rejected alternatives | 12.3 |
| 13. Known weaknesses | 13.4 |
| 14. If this went to production at Formi next month | 13.5 |
| Live review preparation (not a heading) | 9.6 replay, 9.7 change-a-constraint table, 16 |


---

## 16. Critique items not applied

Every item of the round-3 critique was applied (map in 0.5) except the parts below, each rejected or
replaced for the reason given.

- **Item 5, "a faster drain".** Not applied: inside a `max_shift` policy no drain is faster than one
  step per tick, and an approval is slower than a two-step chain. Applied instead: a 1h suspect
  window (alarm 15 to 30 minutes earlier), the spread remainder riding the next routine approval
  (lower pre-flag share), and test bounds derived from the day-share formula. The "under 0.10" target
  is dropped rather than met, because it fails for pre-flag shares above about 0.39 (7.3, 13.4).
- **Item 8, "run verdicts on all candidates".** Replaced: the rank is fixed before any validation,
  so validating in rank order and stopping at the first SAFE, approvable prompt chooses exactly what
  "validate all, then take the top-ranked SAFE one" would, at lower cost (7.1). The deterministic
  rule, the full catalog as candidate set and skipping the LLM call were applied as proposed.
- **Item 11, cutting "the sequential D0 screen mode".** Not cut: it is one parameter of the same
  verdict procedure, so cutting it saves no build time; replacing it with FULL would cost about 6
  more credits per workflow (73 more on public_03), and dropping the screen would lose F7. The other
  suggested cuts (interaction LR test, regrid, fuzzing beyond the J2 cases, C10 to minimal) were
  applied, and the HTML page and SNIPS were cut as well.
- **Item 16, "monitoring compares mix-standardised rates".** Applied for attribution and for the
  view, not as the breach statistic: the guardrail is the raw rate (it is the KPI the business and
  the rubric read), so a breach is still a drop in the raw rate; the mix-standardised (within-cohort)
  term decides whether our schedule or caller-ID change caused it (5.10).
- **Item 1, scope of the fix.** Applied beyond what was asked rather than rejected: the critique
  fixed the cohort scope only; the population W1 was also raised, from 0.99 to 0.995, because under
  unequal sizes 0.99 gives 1.28% per population hypothesis, above the 1% acceptance line of
  `test_false_winner_rate`. Cost: power at the n = 800 look falls from 0.63 to 0.52 (5.5).

---

## 17. Lead review (2026-11-06): accepted with these overrides

The design above is accepted as the build target. Where this section conflicts with an earlier
section, this section wins.

1. **LLM suggest-fixes is called once per hallucination incident** (overrides 7.1 "not called by
   default"). README section 6 describes C8 as "detect, LLM suggest, validate, propose". The call
   costs 3 credits from the incident reserve. Its suggestions are added to the candidate set; the
   fixed statistical rank (last known good first, then catalog order by live engagement) still
   decides, so the LLM only proposes (constraint 4). If credits are in LOW mode, the call is skipped
   and the skip is recorded as a decision.
2. **Simple engagement model first.** M5 implements the `EngagementModel` interface twice: first a
   per-cohort Beta-binomial (log-ratio posterior per arm and cohort, same harm gates, EW recency,
   winner rule and holdout), then the ridge-logit of 5.3. The ridge-logit becomes the default only if
   it beats the Beta-binomial on the private scenario variants (gap captured, harmful count) with
   no harmful change. Otherwise the Beta-binomial ships and the ridge-logit is documented as the
   next step. Both stay behind one setting.
3. **Private scenario variants are built early (M0b).** `scripts/variants/make_variants.py` writes
   scenario files under `scenarios/private/` (gitignored is wrong: commit them, they are test
   fixtures) by changing seeds, effect signs and sizes, incident times and types, and governance
   policies (the J2 attack cases a to j). Only tests and harness scripts read them; controller code
   never does.
4. **Parallel build rules.** M0 owns `controller/types.py`, `controller/settings.py`,
   `controller/store/migrations/`, the dependency list and the package skeleton. Parallel tracks add
   only files they own, and report any contract change they need instead of editing shared files;
   the lead integrates contract changes.
