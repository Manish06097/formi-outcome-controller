# Design proposal A: outcomes and statistics first

Status: proposal (designer A-outcomes). Stack fixed by ADR 0001: Python 3.11, Postgres 16, httpx,
numpy, psycopg 3, FastAPI, Docker Compose, no LLM framework.

This proposal optimises for what the hidden-scenario grader measures: lift on each workflow's
`primary` KPI with the `guardrail` respected, **zero harmful changes**, fast incident fixes, fast
moves off decaying variants, and clean governance. It goes deepest on the learning loop. Every
number in it is computed from API-visible volumes or from stated design assumptions. Public truth
(UNDERSTANDING.md section 7) is used only to sanity-check that the design would find what it
should, and is labelled "sanity check" wherever it appears.

## 0. The one idea that shapes everything

The grader judges every controller change **at apply time** against hidden truth over the active-lead
population: **harmful** if expected engagement drops by more than 1% or expected caller-ID health
drops by more than 2% (grader.py:70-72, world.py:634-655). It judges increments: each change is
compared with the config it replaced.

So the controller treats "how much could this change hurt, given what we know right now" as a hard
gate on every change, and computes it the same way the grader does, from its own posterior:

```
harm gate (engagement):  P( de(new vs current) < -1% | data ) <= alpha_H
harm gate (caller ID):   P( dd(new vs current) < -2% | data ) <= alpha_D
  de = E_new / E_cur - 1, expected engagement averaged over OUR tracked active leads
  dd = H_new / H_cur - 1, expected relative caller-ID pickup over the same leads
```

with `alpha_H = 2.5%` for exploration and promotion, `10%` for rollbacks (keeping a decaying
variant also costs money every hour), and `alpha_D = 5%` for caller-ID probes. Exploration,
promotion, rollback, DID probing and supervisor-intent rebases are all the same computation with a
different allocation on the right-hand side. The consequence is a **harm-budgeted ramp**: unproven
arms enter small, and each later change may grow them only as far as the evidence gathered since the
last change allows. At dev volumes (about 950 connected calls a day) this makes learning slow, and
the proposal says so with numbers (section 5.4). At production volumes (about 13.5k a day per
workflow) the same rule converges in one to two days.

---

## 1. Scope

| # | Capability | Status | Why | Cost to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | **Built** | Floor requirement and the base of every decision. Chunk-number cursor, one transaction per chunk, COPY. | - |
| C2 | Lead joins and one shared cohort object | **Built** | The harm gate, the experiments, schedule and supervisor view all need the same cohort ids. Versioned identity. | - |
| C3 | Bootstrap from history | **Built (narrow)** | Baselines only: hour curves, cell mix, DID baseline pickup, flag-rate baseline, active leads entering D0. History has no variant data, so no variant priors come from it. | - |
| C4 | Call-time optimisation | **Partial** | Per-cohort `hour_weights` built: free at apply time (schedule changes are graded neutral) and a real pickup lever. Per-person `lead_preferred_hour` **cut**: one or two observations per person, x0.25 penalty outside the hour, 200k cap. | Per-person: about 1.5 days (Beta-binomial per person on hour bands, cap by per-hour demand, A/B on returning persons). |
| C5 | Caller-ID health | **Built** | Largest pickup lever (pickup-primary workflows), detectable within an hour, auto-applicable on most policies. | - |
| C6 | Welcome x voice experimentation | **Built** | The engagement lever. Factorial, Bayesian ridge-logit model with feature interactions, harm-gated allocator. | - |
| C7 | Promotion, decay, rollback | **Built** | Holdout sized by computation, Page CUSUM on winner-vs-holdout, rollback timed by the harm gate. | - |
| C8 | Hallucination incident response | **Built** | Detection on every supervisor prompt edit (validation) plus a live Bernoulli CUSUM, last-known-good fix, LLM only when no known-good prompt exists. | - |
| C9 | Governance client | **Built** | Floor-critical: state machine, timing, fatigue reserve, expiry-safe windows, stale rebase, local policy replica. | - |
| C10 | Impact score, affected leads, dry run | **Built** for allocation and caller-ID (posterior plus off-policy replay); **Partial** for schedule (analytic expectation, no dialler twin). | Twin: about 1 day. |
| C11 | Supervisor view | **Partial** | JSON API plus one server-rendered page per tenant. No auth, no notifications. | About 1 day for auth and email digest. |
| C12 | Audit store | **Built** | Floor requirement and the live-review question. | - |
| C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable, floor-adjacent. Fault-injection points plus random SIGKILL. | - |
| C14 | Loop observability | **Partial** | Metrics table, `/ops` endpoint, alert rules evaluated in-process. No pager integration. | Half a day to export to Prometheus. |
| C15 | Fair allocation across tenants | **Partial** | Per-tenant credit envelopes with a safety-only shared pool. Approver attention is per workflow in this API, so there is nothing to share across tenants. | Cross-tenant approver pools: not meaningful until approvers are shared. |
| - | Prompt experimentation (not a listed C) | **Cut** | One prompt per workflow, so prompts can only be compared over time. A +3% engagement difference needs about 14,400 connected calls per arm (fixed design, alpha 0.05, power 0.8 at e = 0.55), 15 days of a dev workflow, plus an approval and a validation run per switch. Not decidable in a scenario. | Switchback design with approval per switch: 2 days, low value. |

**Selection principle.** Build, in order: (1) what the floor and hard constraints require (C1, C9,
C12, C13); (2) what moves the graded outcomes per unit of evidence available at dev volumes (C5,
C6, C7, C8, per-cohort C4); (3) what makes those trustworthy to a supervisor (C2, C10, minimal C11).
Cut what cannot reach statistical significance inside a scenario (prompt A/B, per-person timing) and
what the API gives no lever for (cross-tenant approver fairness). Every cut is listed with the
computation that justifies it.

---

## 2. Architecture

```
                                  Simulator (HTTP only)
   +------------------------------------------------------------------------------+
   | /sim/advance  /events/chunks/{n}  /workflows*  /approvals*  /workflows/*/apply |
   | /workflows/*/config (PUT auto-apply)  /validation/runs  /llm/suggest-fixes     |
   +-------^---------------------^----------------------^-----------------^--------+
           | advance (journaled) | stream NDJSON        | proposals/apply | credits
           |                     |                      |                 |
   +-------+---------------------+----------------------+-----------------+--------+
   |                           CONTROLLER PROCESS (one)                            |
   |                                                                               |
   |  loop.Orchestrator  (one step = plan -> advance -> ingest -> refresh ->       |
   |        |              detect -> govern/apply -> decide -> act -> audit)      |
   |        v                                                                      |
   |  loop.Stepper ---- picks advance size from pending work and clocks            |
   |  journal.Intent ---- writes intent row BEFORE every non-idempotent call       |
   |                                                                               |
   |  ingest.ChunkIngestor -> parser -> aggregator -> LeadTracker                  |
   |        | (one DB transaction per chunk: raw calls, aggregates, cursor)        |
   |        v                                                                      |
   |  world.ConfigMirror  world.WorkflowRegistry  cohorts.CohortSet (versioned)    |
   |        v                                                                      |
   |  stats.*  (ridge logit + Laplace, Bernoulli CUSUM, Page CUSUM, MH ratio,      |
   |            SNIPS replay, seeded posterior draws)                              |
   |        v                                                                      |
   |  learning.EngagementModel  learning.Allocator  learning.HarmGate              |
   |  learning.DidHealth  learning.Schedule  learning.Decay  learning.Guardrail    |
   |  incidents.Hallucination  incidents.DidIncident  incidents.CreditBudget      |
   |        v                                                                      |
   |  planner.DesiredChangeSet (priority merge per workflow) -> planner.Rebase     |
   |        v                                                                      |
   |  governance.Router (auto vs approval, local policy replica)                   |
   |  governance.Timing  governance.Builder (summary, diff, dry run, impact)       |
   |  governance.Lifecycle (state machine)  governance.Reconcile                   |
   |        |                                                                      |
   |  audit.Writer  (same transaction as every state change)                       |
   +--------+--------------------------------------------------------+-------------+
            |                                                        |
            v                                                        v
   +---------------------------+                       +----------------------------+
   | Postgres 16 (only state)  |<----- read only ------| FastAPI (same container,   |
   | world-namespaced tables:  |                       | separate thread pool):     |
   | journal, chunks, leads,   |                       | /supervisor/{tenant}       |
   | calls, aggregates, cohorts|                       | /explain/change /explain/  |
   | configs, proposals, audit,|                       | call  /ops/health /ops/    |
   | incidents, credits, metric|                       | metrics                    |
   +---------------------------+                       +----------------------------+
```

**Components.** One controller process, single-threaded control loop plus one download thread
that prefetches chunk k+1 while chunk k is parsed. The FastAPI app runs in the same container on a
read-only connection pool. Docker Compose brings up Postgres and the controller; `run.sh <SIM_URL>`
runs `docker compose up` and waits for the controller to exit after `409 finished`.

**Where state lives.** Only in Postgres, namespaced by `world_id`. In-memory structures (cell
arrays per lead number, current posteriors, the CUSUM statistics) are caches rebuilt from Postgres
on start. CUSUM statistics are persisted after every step because they are path-dependent.

**What happens on restart** (also section 8):
1. Compute the world fingerprint (section 3). Same fingerprint: resume in that namespace.
   Different: the simulator restarted, so start a new namespace and raise an alert.
2. Resolve the step journal: if the last step is `advancing`, `GET /sim/state` (it takes the sim's
   lock, so it waits for any in-flight advance) and compare `sim_minute` with the journaled
   `from_minute` and `to_minute`.
3. Ingest every chunk with number above the cursor (`GET /events/chunks?kind=events&after=n`).
4. Resolve open intents by kind (approval submit, auto-apply, apply, validation, LLM).
5. Reconcile every non-terminal proposal against `GET /approvals?workflow_id=` and
   `GET /workflows/{id}/config/history`.
6. Rebuild caches, resume the loop.

---

## 3. Data model

All tables carry `world_id` (8-byte hash) as the first key column. **World fingerprint** =
sha256 of `/sim/state.scenario` + `end_minute` + the sorted `/workflows` bodies without
`current_version` + the history chunk metadata list (chunk, events, first/last event id). Raw
facts are append-only; aggregates are derived and could be rebuilt from raw post-D0 facts.

### 3.1 Tables

| Table | Key | Key columns | Raw / derived | Rows at public_03 |
|---|---|---|---|---|
| `world` | world_id | fingerprint, sim_url, scenario, tick_minutes, end_minute, created_at | meta | 1 |
| `workflow` | (world, wf_code smallint) | workflow_id, tenant_id, public_spec jsonb, objective, governance jsonb, options jsonb | raw (API) | 12 |
| `step` | (world, step_no) | from_min, to_min, state {planned, advancing, advanced, ingested, decided, done}, wall_ms per phase | journal | ~80 |
| `intent` | (world, intent_id) | kind {advance, submit_approval, auto_apply, apply, validation, llm}, request jsonb, state {intended, done, failed, unknown}, response jsonb, step_no | journal | ~500 |
| `chunk` | (world, chunk_no) | kind, events, first_event_id, last_event_id, until_min, applied_at | cursor | ~45 |
| `lead` | (world, wf_code, lead_no int) | campaign_idx, person_no, lang, tier, age, value (smallint codes), cell_id, status {active, concluded, unreachable}, attempts, last_attempt_min, created_min | raw + state | 2.3M |
| `person` | (world, wf_code, person_no) | first_seen_min, n_campaigns | raw | ~1.6M |
| `campaign` | (world, wf_code, campaign_idx) | campaign_id, day, created_min, config_version_at_creation, leads | raw | ~150 |
| `call_fact` (partitioned by sim day) | (world, event_id) | wf_code, lead_no, person_no, minute, hour, attempt_no, camp_day_idx, config_version, cohort_code, welcome, voice, caller_id, prompt (smallint codes), connected, engaged, disposition, duration_s, turns, flag, adherence | raw, **post-D0 only** | 3.2M (5 days x 648k) |
| `agg_pickup` | (world, wf, day, cell, hour, attempt_bucket, caller_id) | dials, connected | derived | ~450k |
| `agg_engage` | (world, wf, day, cell, prompt, welcome, voice) | connected, engaged | derived | ~35k |
| `agg_flags` | (world, wf, day, hour, prompt) | connected, flags | derived | ~2k |
| `config_version` | (world, wf, version) | actor, changed_dims, config jsonb (nullable if unrecoverable), applied_min, proposal_id, approval_id, observed_via {event, own_apply, fetch} | raw | <500 |
| `cohort_set` | (world, wf, set_version) | def_hash, features_used, built_from {history, regrid, supervisor} | derived | <50 |
| `cohort` | (world, wf, set_version, cohort_id) | where jsonb, order_idx, def_hash, cell_ids int[] | derived | <1k |
| `experiment` | (world, wf, exp_id) | dim, arms, control_arm, cohort_set_version, state, started_version, started_min | derived | <100 |
| `posterior_snapshot` | (world, wf, decision_id) | model inputs hash, params mean, cov (bytea), arm x cohort summaries jsonb | derived | <5k |
| `decision` | (world, decision_id) | wf, step_no, kind, rule, inputs jsonb, outcome, harm_report jsonb, created_min | audit | <5k |
| `proposal` | (world, proposal_id) | wf, route {approval, auto}, base_version, proposed_config jsonb, request jsonb (full body), state, approval_id, apply_by, reasons, applied_version, supersedes, decision_id | audit + state | <500 |
| `incident` | (world, incident_id) | wf, type {hallucination, did_degraded, kpi_drift, decay}, state, detected_min, evidence jsonb, fix_proposal_id, resolved_min, credits_spent | state | <50 |
| `validation_run` | (world, run_id) | wf, prompt, n, failures, pass_rate, purpose, intent_id | raw | <50 |
| `credit_ledger` | (world, entry_id) | tenant, wf, purpose, amount, balance_after, intent_id | audit | <100 |
| `audit_event` | (world, audit_id) | sim_min, wf, actor {controller, supervisor, approver, sim}, kind, refs jsonb, summary text, detail jsonb | audit | <20k |
| `loop_metric` | (world, step_no, name) | value | ops | ~5k |

Indexes beyond primary keys: `call_fact (wf_code, lead_no)` for "why did this customer hear this";
`lead (wf_code, status) WHERE status='active'` for active-population queries;
`proposal (wf, state)`; `audit_event (wf, sim_min)`; `config_version (wf, applied_min)`.

### 3.2 Raw versus aggregated

- **History** (pre-D0): leads and campaigns stored raw (needed to join features for leads still
  dialled on D0 and D1); calls go **only into aggregates**. That saves inserting about 4.5M rows at
  public_03 and loses nothing: history ran on the default config, so no per-call question about it
  is ours to answer.
- **Post-D0** calls are stored raw because Compliance needs per-call answers and the off-policy
  dry run needs per-call propensities.
- Aggregates are upserted in the same transaction as the chunk's raw rows and the cursor update.

### 3.3 Sizes at public_03 scale

Volumes: 12 workflows x 54k dials/day = 648k calls/day, 192k leads/day, about 840k events/day;
history 7 days, about 5.9M events.

| Object | Rows | Approx size |
|---|---|---|
| lead | 12 days x 192k = 2.3M | 210 MB heap + 50 MB PK + 20 MB partial index |
| person | ~1.6M | 100 MB |
| call_fact | 5 x 648k = 3.24M | 275 MB heap + 70 MB PK + 70 MB lead index |
| agg_pickup | ~450k | 40 MB |
| everything else | <100k | <20 MB |
| **Total** | | **about 0.9 GB on disk**, Postgres `shared_buffers` 512 MB, controller RSS target < 1 GB |

---

## 4. The control loop

### 4.1 Per-step sequence

```
loop:
  s = Stepper.next_minutes(state)                       # 4.2
  journal.step_begin(from=sim_minute, to=sim_minute+s)  # committed before the call
  r = sim.advance(s)                                    # never blindly retried (8.3)
  journal.step_advanced(r)
  Ingestor.ingest(r.new_chunks)                         # one tx per chunk: raw, aggregates, leads, cursor
  Refresh:
     for wf with config_changed events: GET config, store body as config_version
     for approval_decided events and every pending proposal: update lifecycle
  UpdateStats:
     detectors every step (CUSUMs, DID ratios): O(new events)
     models (engagement GLM, hour curves) only at decision points or every 4 dialling hours
  Detect -> incidents / alarms (each writes a decision row)
  Govern:
     for approved proposals: pre-apply checks (base_version current, apply_by not passed,
     harm gate re-check) -> apply or mark stale/abandoned
  Decide:
     per workflow: DesiredChangeSet = merge by priority
         1 safety (hallucination fix, DID drain)
         2 supervisor-intent rebase
         3 rollback (decay, guardrail breach)
         4 caller-ID spread / probe
         5 promotion and exploration allocation
         6 schedule
     Router: auto-apply if the whole set is auto-permitted and nothing is pending, else approval
     Timing: submit now, or defer to the next safe window
  Act: journal intent -> HTTP call -> record response (validation runs before the proposals they back)
  Audit + metrics committed with each state change
  if r.finished: final reconciliation, export, exit
```

Each decision row is committed before its HTTP action; the HTTP response and the resulting state
transition are committed together afterwards. The simulator is frozen between advances, so every
pre-check in Govern and Decide is race-free: nothing can change the config between our
`GET config` and our `apply`.

### 4.2 Advance step-size rule

All inputs come from the API (`tick_minutes` from `/sim/state`, windows and governance from
`/workflows`).

```
def next_minutes(st):
    t = st.tick
    cands = []
    if any_workflow_in_dialling_window(st.now) or approver_hours_open_any(st.now):
        cands.append(60)
    else:
        # overnight: jump to the earliest of next dialling-window start, next approver-hours start,
        # next daily batch time, next planned action, scenario end
        cands.append(minutes_until(next_interesting_minute(st)))
    if st.any_proposal_in({PENDING, APPROVED}):
        W = min(apply_window_hours of those workflows) * 60
        cands.append(min(60, W - 2 * t))          # 90 with W=120, t=15: decision seen and applied in time
    if st.open_incident or st.suspect_did or st.suspect_prompt:
        cands.append(t)                             # 15 min while an incident is live
    if st.ramp_dwell_active:                        # auto-apply chain waiting for evidence
        cands.append(2 * t)
    cands.append(minutes_until(st.next_planned_action))   # land exactly on 10:00, 11:00 etc.
    cands.append(st.end_minute - st.now)
    s = max(t, floor_to_multiple(min(cands), t))
    return s
```

Why 60 in-hours: a DID flag at a 33% share needs about 100 dials on the DID to cross the detection
threshold (section 7.2), which is about 1 hour at dev volumes. Smaller steps do not detect sooner and
cost round-trips. The apply-window bound: a decision at minute d becomes visible at the end of the
tick that contains d, and we act in the same iteration, so `s <= W - 2t` leaves one tick of margin.
At public_03 this gives about 15 advances per simulated day.

**End-game freeze.** No new exploration in the last `max(1 day, 15% of scenario)`; only safety,
rollback, DID drain and already-declared promotions.

---

## 5. Learning loop

### 5.1 Volumes the design must work with

Read at runtime as `dials_per_hour x window length` and observed connect rates. Public numbers for
sanity:

| Workflow (public_01/02) | Primary | Dials/day | Connected/day | Base rate |
|---|---|---|---|---|
| nw_renewal | engagement | 3,600 | ~950 | engagement ~0.555 |
| nw_kyc | pickup | 2,000 | ~465 | pickup ~0.23 |
| ks_cancel | engagement | 4,800 | ~1,200 (800 after DID flag) | engagement ~0.50 |
| each public_03 workflow | mixed | 54,000 | ~13,500 | |

### 5.2 Cohorts: a shared object with versioned identity

**Cells.** A cell is one combination of the features used by the grid. Every lead gets a `cell_id`
at ingest (from its `lead` event, or from history for leads carried into D0).

**Grid construction at D0** (from history only, per workflow):
1. F1 = the feature whose levels have the most different hour-of-day pickup curves in history
   (between-level chi-square of `connected` by hour, normalised by its degrees of freedom). This
   is the schedule driver.
2. F2 = `lang` if the workflow has 2 to 5 languages each with at least 10% of leads, else the
   remaining feature with the most balanced mass. Rationale: welcome and voice are language-bound
   (accent, script), and lang is the most likely carrier of welcome/voice heterogeneity before any
   variant data exists. This is a stated prior, not a learned fact.
3. Cells of F1 x F2 with under **5% of daily leads** are merged with their F1-neighbour within the
   same F2 level, then with an `other` bucket per F1 level. Cap 20 (the API limit); if still above
   20, F2 levels are merged by size.
4. Each cell becomes one cohort, ordered most-specific first (merged buckets last), so first-match
   evaluation is unambiguous.

**Minimum cohort size 5% of daily leads, and why.** A cohort only earns its own allocation if it
can accumulate 200 connected calls on an arm within the scenario. At nw_renewal volumes a 5% cohort
has about 48 connected calls a day; at a 50/50 split that is 24 per arm per day, so 200 takes about
8 days, which is the outer edge of a scenario. Smaller cohorts never reach a decision; they exist
only as merged buckets.

**Identity.** `cohort_key = "{workflow}:{slug}@{hash8(where, order)}"`, plus `set_version`. The
simulator's `cohort_id` on call events is our slug; the `config_version` on the event maps it to a
set version. Detection, experiments, the supervisor view and the dry run all go through
`CohortSet.cohort_of(cell_id)`, so "seniors in Tamil" means the same thing everywhere (Product memo).
Changing the grid is a new set version proposed through approval (cohorts never auto-apply on any
public policy), triggered only if the engagement model finds heterogeneity on a feature outside the
grid that is worth more than 1% expected population engagement (a regrid proposal).

**Allocation layering.** Uniform exploration lives in `allocations.default`. A cohort gets its own
override for a dimension only when its allocation diverges (soft exploitation or a promotion).
Consequences: a supervisor's edit to `allocations.default` reaches every cohort without an override,
as the supervisor expects; and our diffs stay small. Cohorts inherit `default` for dimensions they
omit.

**Supervisor edits to `cohorts`** are adopted as a new set version built from their definitions;
experiments whose cohorts disappeared are marked `interrupted` (data kept, decisions paused) and
re-planned on the new set.

### 5.3 Experiment design: welcome x voice, factorial

**Scope by objective.** Welcome/voice experiments run only on workflows whose `primary` is
engagement. On pickup-primary workflows these dimensions cannot move the primary KPI, they put the
guardrail at risk, and they would consume the scarce approval slots that caller-ID and schedule
changes need. Read from `objective.primary`.

**Factorial.** The simulator samples welcome, voice and caller ID independently per dial from the
lead's cohort distributions, so any product allocation is a full factorial: every call informs both
dimensions. Under no interaction (stated assumption A-1), this doubles effective sample size
compared with testing one dimension at a time. Allocations are always product-form anyway (the API
cannot express a joint welcome-voice distribution), so with full promotion the chosen combination is
served purely.

**Model** (per workflow, refit at decision points; deterministic):

```
For connected call i in cell c(i), day d(i), prompt p(i), welcome a(i), voice b(i):
logit P(engaged_i) = beta[c] + gamma[d] + pi[p]
                   + theta_W[a] + sum_f delta_W[a, f, level_f(i)]
                   + theta_V[b] + sum_f delta_V[b, f, level_f(i)]
control arms (incumbent W, incumbent V): theta = delta = 0
f ranges over lang, age_band, city_tier (all three, not only the grid features)
priors: theta, delta ~ N(0, tau_E^2), tau_E = 0.13 on the logit scale
        (about 0.06 on the log-probability scale at p = 0.55); beta, gamma, pi ~ N(0, 2^2)
fit: ridge IRLS on aggregated counts (cell x day x prompt x welcome x voice), Laplace posterior
size: <= 84 cells + 10 days + 5 prompts + 5 arms x 15 levels ~ 175 params, < 10 ms per fit
```

Data come only from `call_fact`/`agg_engage` rows with the variant the call actually carried
(Product: "only what actually happened counts"). On `next_campaign_only` workflows the event's
`variant` field, not the locked `config_version`, decides the arm. Day and prompt intercepts absorb
supervisor prompt edits and day-level shocks so they do not leak into arm effects.

**Cohort-level effects** for arm a in cohort k: average over the cohort's active leads of the
predicted engagement ratio arm vs control, computed from 4,000 posterior draws with seed
`hash(world, workflow, decision_id)`. Same seed, same data, same answer: winner selection is
deterministic.

**Interaction check** (assumption A-1 is tested, not just assumed): refit with welcome x voice
terms, `(k_W-1)(k_V-1)` extra parameters (6 for 3 welcomes x 4 voices). Likelihood-ratio test at
alpha 0.01 (chi-square critical 16.8 at 6 df). If significant, the supervisor view flags it and
promotions use the combination-level posterior.

### 5.4 Allocation: harm-budgeted probability matching

**Why not a fixed split.** A fixed 50/50 or uniform split of untested arms fails the harm gate on
day 0 (an arm at 20% with multiplier 0.90 is -2%, graded harmful). **Why not plain Thompson
sampling.** It is randomised per decision (constraint 4 wants deterministic winners), it moves
traffic every step (each move is a governance action, rationed to 2 to 4 a day), and nothing bounds
its per-change harm. **Why not successive elimination alone.** Needs per-arm confidence bounds that
take most of a dev scenario to separate.

**Chosen allocator.** At each governance opportunity, per cohort k and dimension:

```
target_k   = probability matching: share_a ∝ P(a is best in k | data)   (from the seeded draws)
             floors: every live arm >= 2% while still undecided; control >= holdout h_k
             ceiling: an arm not yet declared winner <= 50% of its cohort
proposal   = current + lambda * (target - current), lambda in [0, 1]
lambda*    = largest lambda passing ALL of:
               harm gate:  P(de < -1%) <= 2.5%    (Monte Carlo over the same 4,000 draws)
               TV per cohort <= max_shift          (only when routed to auto-apply)
               guardrail dry run: predicted guardrail drop <= 0 (point) and P(drop > limit) <= 2.5%
             found by bisection (12 iterations)
skip the change if the expected primary gain of lambda* is < 0.25% of the cohort's daily primary numerator
```

Untested arms are evaluated under the prior, so the harm gate alone sizes the first exploration.
With a prior log-multiplier sd `tau_H` per arm and mean 0 (deliberately pessimistic), the
population-level change is `de ≈ sum_i s_i theta_i` with sd `tau_H * sqrt(sum s_i^2)`, so

```
P(de < -1%) <= 2.5%   <=>   1.96 * tau_H * sqrt(sum s_i^2) <= 0.01
tau_H = 0.06:  sqrt(sum s_i^2) <= 0.085
  5 challenger arms (2 welcome + 3 voice, e.g. 3 welcome and 4 voice options): s = 3.8% each, 19% total exposure
  3 arms: 4.9% each;  2 arms: 6.0% each;  1 arm: 8.5%
```

`tau_H = max(0.06, tau_EB)` where `tau_EB` is the empirical-Bayes dispersion of arm effects
estimated from day 1 onward: more dispersion than assumed makes the gate more cautious, never less.
Uniform exploration across cohorts is optimal here: harm and information are both proportional to
traffic, so concentrating exploration in one cohort buys nothing.

**Exploitation pays for exploration.** Because the gate is on the sum, a change that also moves
traffic onto an arm with a confidently positive posterior gets a positive expected `de` that offsets
the variance of new exploration in the same change. And removing an arm whose posterior turned
negative counts as a gain. The bisection handles both automatically.

**Ramp speed at dev volumes** (nw_renewal, 950 connected/day, 5 arms, 2 discretionary approvals a
day): day 0 exposure 3.8% per arm gives about 36 connected per arm per day. Posterior sd of a
population-level arm effect after n arm calls is `1 / sqrt(1/tau^2 + n/0.82)`: 0.056 after 36,
0.047 after 150, 0.035 after 500. Each later change may add about 4% to 6% per arm while posteriors
are this wide, more for arms that look good. Expected trajectory: 4% on D0, 10% to 15% on D1, 20%
to 30% for promising arms by D2 and D3, bad arms back to 0 as soon as their posterior mean turns
negative with `P(theta < 0) >= 0.8` (removals are risk-reducing).

**Ramp speed at load** (13.5k connected/day): 3.8% gives 510 calls per arm per day, posterior sd
0.034 after one day. Arms separate in one to two days.

**Approval budget.** Discretionary approvals per workflow per 24h =
`min(policy max_requests_per_24h, tenant preference) - reserve`, reserve = 1 slot (2 if the cap is
6 or more). Ramps for dimensions in `allowed_dims` go through auto-apply (chained only as section
6.4 allows).

### 5.5 Deciding a winner

**Rule.** Arm a is declared the winner in cohort k when all of:

1. `P(theta_{a,k} > 0 | data) >= 0.99` (cohort-level effect vs control, from the model);
2. `P(a is best in k) >= 0.95`;
3. at least 200 connected calls on arm a among leads in cohort k, spread over at least 2 calendar
   days (a single bad afternoon cannot produce a winner: populations, not anecdotes);
4. the promotion change passes the harm gate at 2.5%.

**What the threshold costs** (computed: true effect +10%, theta = 0.0953, tau = 0.06, arm and
control equal size, e = 0.55, so `SE^2 ≈ 1.64 / n`). "Median n" is the per-arm sample at which the
expected posterior just crosses the threshold (50% power); "null p/look" is the frequentist chance
that an arm with zero effect crosses at that sample; "null ever" multiplies by 3 for repeated looks
(the shrinkage makes early looks much stricter, so crossing is dominated by late looks).

| P(theta > 0) threshold | Median n per arm (+10%) | Days at nw_renewal, 50/50 in a 50% cohort | Null p/look | Null ever (approx) |
|---|---|---|---|---|
| 0.95 | 774 | 3.3 | 1.9% | 5.7% |
| 0.975 | 1,005 | 4.2 | 0.9% | 2.7% |
| **0.99 (chosen)** | **1,311** | **5.5** | **0.35%** | **~1.0%** |
| 0.995 | 1,546 | 6.5 | 0.17% | 0.5% |

For a +15% effect the chosen threshold needs about 620 per arm; for +22% about 300. For comparison,
the fixed-sample two-proportion test at alpha 0.05 two-sided and power 0.8 needs 1,262 per arm for
+10% at e = 0.55 and 1,562 at e = 0.50.

**False-winner rate, stated.**
- Bayesian false discovery rate among declared winners <= 1% by construction (each declared winner
  has posterior probability >= 0.99 of a real positive effect), if the prior is right.
- Frequentist: about 1% per (arm, cohort) hypothesis that is truly null. Hypotheses are correlated
  through the additive feature model; the effective count is about arms x feature levels, 5 x 7 = 35
  for a renewal-like workflow, so even if every arm were null we would expect about 0.35 null
  winners per workflow-run. Multiple-comparison control is therefore (a) the strict per-hypothesis
  threshold, (b) the extra `P(best) >= 0.95` condition, and (c) the harm gate.
- What a false winner costs: a null winner is graded **neutral** (de ≈ 0), costs one approval slot
  and nothing else. A **negative** winner is what matters, and the harm gate requires
  `P(de < -1%) <= 2.5%` on the promotion itself. A promotion of 50% of a 50% cohort onto an arm
  with true multiplier 0.97 is de = -0.75%, still neutral. Harm needs `share x |theta| > 1%`.

**Product's question (minimum cohort size and window, and the cost).** Minimum cohort 5% of daily
leads; minimum evidence 200 connected per arm over 2 days; window = all data since the arm went live
(stationary estimate) for winner decisions, and an exponentially weighted window with a 12
dialling-hour half-life for decay. Moving the threshold from 0.99 to 0.95 makes decisions about
40% faster and roughly sextuples null winners.

### 5.6 Promotion with holdout

A promotion sets the cohort's allocation to `1 - h_k` on the winner and `h_k` on the control, and
drops the other arms of that dimension in that cohort. Holdout size:

```
h_k = smallest of {5%, 10%, 15%, 20%} such that the one-day SE of log(winner/holdout)
      sqrt( (1-e)/(e * N_k * h) + (1-e)/(e * N_k * (1-h)) ) <= 0.075,   capped at 20%
N_k = connected calls per day in the promoted cohorts
```

| Case | N_k | h | One-day SE |
|---|---|---|---|
| population-wide winner, nw_renewal | 950 | 20% | 0.0725 |
| 50% cohort, nw_renewal | 475 | 20% (cap) | 0.103 |
| population-wide winner, public_03 | 13,500 | 5% | 0.035 |

Holdout cost: `h x theta`, for example 20% x 10% = 2% of the cohort's engagement while the winner is
good. It buys decay detection (5.7) and a concurrent guardrail comparison (5.10). A holdout is
never removed late in a scenario: the remaining gain is too small to pay for losing decay
visibility.

### 5.7 Decay detection and rollback timing

Facts that shape it: decay starts some time after a variant's first exposure, measured from its
first dial anywhere at any share, and declines gradually; rolling back at onset is graded harmful
because the variant is still at full strength (UNDERSTANDING section 5).

**Monitor** (per promoted or ramped arm with at least 15% share somewhere):
- Per half-day segment (split of the dialling window into two halves) compute
  `z_j = (log RR_j - theta_ref) / SE_j`, where `RR_j` is the winner-vs-control ratio in that
  segment (Mantel-Haenszel across cells) and `theta_ref` is the posterior mean at promotion.
- One-sided Page CUSUM for a decrease: `S_j = max(0, S_{j-1} - z_j - k)`, k = 0.5, alarm at h = 4.
  Standard table values: in-control ARL about 168 segments (84 dialling days, about 12% chance of a
  false alarm per monitored arm in a 10-day run); out-of-control ARL about 8.4 segments at a 1 SE
  shift and 3.3 at 2 SE.
- A false alarm only freezes ramp-ups for that arm and starts the rollback evaluation below; it never
  rolls anything back by itself.

**Rollback decision** (evaluated every step once the alarm is on, and at every governance
opportunity regardless):

```
use the exponentially weighted posterior (half-life 12 dialling hours) of the arm's current effect
predict to the expected apply time t_a (now + expected approval latency inside approver hours,
  or now for auto-apply), with a local linear trend fitted to the last 4 segments
rollback (move the arm's share to the control or the next-best arm) when
   E[de_rollback(t_a)] > 0  and  P(de_rollback(t_a) < -1%) <= 10%
partial rollback (largest share cut passing the 10% gate) when only part of the share qualifies
```

The 10% tolerance (vs 2.5% elsewhere) is deliberate: leaving a decaying variant live costs
engagement every hour, the rollback target is a known baseline, and the grader's decay metric
counts every day the variant stays. The prediction to `t_a` matters because an approval takes 0.5
to 4 hours: we propose when the variant is expected to have crossed below the control by the time
it is applied, not when it crosses.

**Detection lag at dev volumes** (sanity check with a variant falling about 0.27 in log-multiplier
over 2 days, nw_renewal, 20% holdout, segment SE about 0.10): the drift is about 0.07 per segment,
so the CUSUM reaches h = 4 after about 3 to 4 segments, 1.5 to 2 days after onset, by which time the
variant is near or below the control and the rollback is graded beneficial. At public_03 volumes
(segment SE about 0.03) the alarm comes within 1 segment. Residual risk, stated: at dev volumes a
fast decay costs one to two days of exposure.

Not done: staggering exploration so decay clocks start only when we can exploit them. That is
tuning to a simulator mechanism, and a real decaying effect (novelty wearing off) would not care.

### 5.8 Call time (per cohort)

**From history:** pickup by (cell, hour, attempt bucket {1, 2-3, 4+}). The history ran on a flat
schedule, but later hours over-represent leads that failed earlier (hard to reach). Stratifying by
attempt bucket and averaging over a fixed attempt mix removes most of that selection; the remaining
bias is stated. Curves are shrunk toward the workflow's pooled curve with a prior strength of 200
dials per (cell, hour).

**Weights:** comparative advantage, because the dialler ranks eligible leads within an hour by
weight times Gumbel noise and capacity is binding:

```
w_k(h) = clip( (p_k(h) / pbar_k)^kappa / mean_h(...), 0.25, 4.0 ),  kappa = 2
pbar_k = cohort k's mean pickup across the window hours
every in-window hour gets weight >= 0.25 (no zero weights: zero wastes capacity the business owns)
```

A cohort whose pickup at hour h is high relative to its own average wins the hour against cohorts
that would pick up just as well later.

**Governance.** Schedule changes are graded neutral at apply time (no engagement or caller-ID
effect), so they never trip the harm gate. They go in the D0 batched approval (they need the
cohorts that the same approval creates) and are refreshed at most daily, by auto-apply where
`schedule` is allowed, inside an approval batch otherwise.

**Dry run and verification.** Analytic expectation: connects per hour =
`capacity_h x sum_k q_k,h(w) x p_k(h)`, with `q_k,h ∝ eligible_k,h x w_k(h)`, the eligible pool
reconstructed from our own lead tracker. After a change we check per-(cohort, hour) pickup stays
within its credible band (the model's assumption) and that overall pickup, adjusted for caller-ID
mix, did not drop.

**Per-person timing: cut**, with the reason above. Its design sketch stays in the doc for the
summit conversation: Beta-binomial per person on four hour bands, prior from the cohort curve, a
preference only when the person's posterior peak beats the cohort's chosen hour by 25%, capped so
that no hour's preferred demand exceeds 50% of its capacity.

### 5.9 Caller-ID health and rotation

**Signal.** DID assignment is randomised per dial within a cohort, so the concurrent pickup of DID
d against the other live DIDs, in the same cohorts and hours, is an unbiased health comparison:
`r_d` = Mantel-Haenszel pickup ratio of d vs the pooled other DIDs, stratified by (cohort, hour).
We also track each DID's dials in the last 24 hours as a load index.

**States per DID:** `unknown -> probing -> healthy | unhealthy`, plus `degraded` for a previously
healthy DID that drops (section 7.2).

```
healthy    if lower 95% bound of r_d >= 0.85
unhealthy  if upper 95% bound of r_d <  0.85
probing    otherwise (at most 3 dialling days, then treated as unhealthy and dropped)
```

**Probe size from the harm gate.** An untested DID has prior `P(flagged) = 0.2` with relative
pickup `r_flag = 0.35` if flagged (design assumptions, A-6). The grader's `dd` is evaluated with
current loads, so a fresh healthy DID helps at apply time. With probes of share s on m new DIDs,
`dd ≈ sum s_d (r_d - 1)`:

```
one new DID:   P(flagged) = 0.2 > 5%, so it must be safe even if flagged:  s x 0.65 <= 0.018  ->  s <= 2.8%
two new DIDs:  P(both flagged) = 0.04 <= 5%, exactly-one case must be safe -> s <= 2.8% each
```

So probes are 2.8% per untested DID. Enough, because DID effects are large: at nw_renewal volumes a
2.8% probe gets about 100 dials a day, and a flagged DID at r = 0.64 is classified unhealthy after
about 200 dials (upper bound 0.83), a fresh healthy one healthy after about 100.

**Spreading (load-aware).** Target = equal shares across healthy DIDs. Rationale: if health falls
with load, an equal split maximises mean health among equally healthy DIDs. Where healthy DIDs differ,
shares are proportional to `r_d` over the healthy set. A simple load-response fit
`log r_d = a_d - c x share_d` is estimated from share changes over time; when `c` is credibly
positive we include it in the dry run; we do not need it to choose the target. The spread passes the
DID harm gate with posterior `r_d` values (it is normally graded beneficial: share moves to lower
load).

**Routing and timing.**
- Policy allows `caller_id` auto-apply: probe at D0 00:00 (before any approval is in flight), spread
  as soon as the probes are classified, outside any pending-approval window. Steps larger than
  `max_shift` ride along with the next approval instead of chaining, except for incident drains.
- Approval only and `next_campaign_only` (KYC-like): the probe goes in the D0 batched approval and
  only reaches new campaigns, so we classify from all dials on the probed DIDs regardless of
  campaign, and propose the spread at the first approver window after classification. Fully live
  `len(cadence)` days later; the proposal states this in its dry run.

### 5.10 Guardrail

**Definition** (assumption A-3; the contract does not say): `*_max_drop: x` means an **absolute**
drop of x in the guardrail rate (0.02 = 2 points), measured against a concurrent comparator where
one exists (holdout, other arms, other DIDs), else against the trailing 3-day baseline adjusted by the
model. We also require every proposal's dry run to predict **no** guardrail drop at all (point
estimate <= 0) and `P(drop > x) <= 2.5%`, which is stricter than the stated limit.

**Monitoring.** Rolling 2-day window. Power at dev volumes: pickup guardrail (e ≈ 0.26, 7,200 dials
in 2 days vs a 50k-dial baseline) has SE 0.0055, so a 2-point drop is a 3.6 SE event; engagement
guardrail on KYC (930 connected in 2 days) has SE 0.017, only 1.2 SE. That weak case is why we do
not run engagement-affecting experiments on pickup-primary workflows.

**Breach response.** If the lower 97.5% bound of the drop exceeds x, attributable to a change we
made inside the window (by concurrent comparison, or by timing for schedule changes), we roll that
change back with rollback priority. Unattributable drift raises a supervisor-view alert
(`kpi_drift`) and triggers the DID and prompt checks, but changes nothing on its own.

---

## 6. Governance client

### 6.1 Proposal state machine

```
                       +--------------------------- REQUEUE (new proposal, fresh data, supersedes old) <------+
                       |                                                                                       |
 DRAFT --gates ok--> READY --route=approval--> SUBMITTING --201--> PENDING --approved--> APPROVED             |
   |                   |                          |                  |   \--rejected--> REJECTED --retriable--+
   |                   |                          |                  \-----expired----> EXPIRED  -------------+
   |                   |                          |--409 dup id--> reconcile by proposal_id -> PENDING/...     |
   |                   |                          |--409 stale---> STALE --rebase-------------------------------+
   |                   |                          \--4xx other--> FAILED (alert; bug)                          |
   |                   |                                                                                       |
   |                   +--route=auto--> AUTO_APPLYING --200--> APPLIED                                         |
   |                                       |--403--> FAILED_POLICY (alert: local policy replica wrong) --> re-route to approval
   |                                       |--409 stale--> STALE --rebase--------------------------------------+
   |                                       \--409 dup id--> reconcile via config history -> APPLIED
   |
 APPROVED --pre-apply checks--> APPLYING --200--> APPLIED --> VERIFYING --> VERIFIED | ROLLED_BACK (new proposal)
     |  base_version != current ------> STALE (never call apply: no logged apply_conflict) --rebase--> REQUEUE
     |  now > apply_by ----------------> LAPSED_LOCAL (never call apply: no 410) ---------------------> REQUEUE
     \  harm gate now fails ----------> ABANDONED (approved but evidence changed; audited, shown to supervisor)
```

Retriable rejections: `approver declined (judgement call)` (re-propose once with fresh data, next
window), fatigue (should never happen; it means our counter was wrong, so alert). Non-retriable:
missing fields, validation evidence (bugs, alert). EXPIRED follows Product's rule: a rejection, re-queued
with fresh data, never resubmitted as is.

### 6.2 Batching

One proposal in flight per workflow, containing the whole desired change set: cohorts, schedule,
caller ID, allocations, prompt. "Five things in one request" (Kestrel Ops). Auto-apply is used only
when the entire set is auto-permitted and no approval is pending on that workflow, because any
version bump makes a pending approval stale. Priority when work collides:

1. Safety auto-applies (DID drain) **preempt** a pending approval. The approval becomes stale; when
   it is decided we do not apply it (no `apply_conflict`), and its still-wanted content is rebased
   into the next proposal.
2. A safety fix through approval (prompt) may be submitted while another approval is pending only
   if the other one is already stale (for example, the supervisor edit that caused the incident
   bumped the version). Otherwise it waits for the pending one to resolve, at most one apply window.

### 6.3 Timing

All from `/workflows/{id}.governance` and tenant preferences:

```
earliest_submit(day) = max(approver_hours.start, tenant_pref.not_before)       # Kestrel: 11:00
expiry-safe(t) iff for every latency L in [lat_lo, lat_hi]:
        next_working(t + L) <= t + min(TTL, expires_in_hours)
latest_submit(day)   = approver_hours.end - lat_hi    when TTL < time to next day's opening + lat_hi
                       (12h TTL, approvers 10-19, latency <= 120 min: 17:00)
fatigue(t)           = our own requests for this workflow with created in (t - 24h, t]   (from the intent
                       log, cross-checked against GET /approvals created_at)
submit allowed iff  fatigue(t) + 1 <= cap - reserve   (reserve waived for safety fixes)
                and earliest_submit <= t <= latest_submit and expiry-safe(t)
planned slots: earliest_submit, then earliest_submit + 4h if the first has been applied
```

`expires_in_hours` is set to the smallest value that is still expiry-safe for the latency range, so
a proposal whose evidence goes stale overnight cannot linger. Apply happens on the first step after
the decision is visible (step bound in 4.2).

### 6.4 Auto-apply policy

The router applies a **local replica** of the simulator's policy checks before any `PUT`: changed
dimensions computed exactly like `changed_dims` (cohort list order-sensitive, weights normalised,
any `lead_preferred_hour` change counts as `schedule`), every changed dimension in `allowed_dims`,
per-cohort total-variation shift <= `max_shift` over default plus every old and new cohort, non-empty
rationale. A 403 therefore means our replica is wrong: alert, re-route to approval, never probe.

Voluntary restrictions on top of policy:
- **Customer-facing dimensions** (welcome, voice, prompt) are never auto-applied for a tenant whose
  preferences say so (Northwind, from the Compliance memo). Default for unknown tenants:
  customer-facing auto-apply only for harm-gated exploration ramps and rollbacks; **promotions always
  go to a human**.
- **Chaining.** Consecutive auto-applies on the same dimension are allowed only in the risk-reducing
  direction (DID drain, rollback), each justified by evidence gathered after the previous one was
  live (dwell >= 1 dialling hour). Ramps toward new exposure: at most one auto-apply per dimension
  per cohort per day. We do not chain `max_shift`-sized steps to get around `max_shift`.
- **Announce ahead** (Kestrel: "tell me before anything changes"): customer-facing auto-applies are
  scheduled at least one hour ahead during approver hours and shown on the supervisor view as
  upcoming, with the reason.

### 6.5 Impact score, affected leads, dry run

**Impact score** (Product: the queue is ranked by it, so it must mean the same thing everywhere):

```
G   = expected extra primary-KPI successes per day over the next 3 days (posterior mean;
      engaged calls for engagement-primary, connects for pickup-primary)
G0  = 5% of the workflow's current daily primary numerator
impact = clip(G / G0, 0, 1) x (1 - P_harm)
safety fixes (hallucination, DID drain) = max(0.9, formula)
```

So 1.0 means "about a 5% primary lift with no meaningful harm risk"; exploration typically scores
0.05 to 0.2, and the dry run shows the components.

**Affected leads** = active leads now in cohorts whose effective distribution changes, plus the
expected leads of the next daily batch in those cohorts (batch size x cohort share). For
`next_campaign_only` and agent-facing dimensions, only the batch term counts, and the summary says
so.

**Dry run** (non-empty object, deterministic, stored with the proposal):
- `posterior`: predicted primary and guardrail KPI per affected cohort under current vs proposed,
  with 95% credible intervals, plus `P_harm`.
- `replay`: self-normalised importance-weighted estimate over the last 2 days of our own logged calls,
  weights = proposed propensity / logged propensity per dimension (propensities recovered from
  the stored config version and the call's cohort). Only for arms with logged support; arms without
  support are labelled "no support, prior only".
- `schedule`: the analytic connects-per-hour expectation (5.8).
- `caller_id`: per-DID `r_d` posteriors and the predicted `dd`.

### 6.6 Supervisor edits

On a `config_changed` event with `actor: supervisor`:
1. Fetch and store the new config body; write an audit event; show it on the supervisor view.
2. Every pending or approved proposal on that workflow is now stale: never applied, re-queued.
3. Freeze decisions on the changed dimensions for that workflow until the rebase below resolves.
4. Interpret the edit as intent and **rebase** on it: the supervisor's new values become the
   control. Examples:
   - Default voice set to V2 (public_02 shape): our voice exploration in `default` was replaced by
     V2 at 100%. We do not revert it. The next proposal (rebased on their version) may resume the
     voice test with V2 as the control: "Your edit made V2 the voice for everyone and ended our voice
     test. We propose to keep V2 as the baseline and resume testing V3 and V4 at 3.8% each." Cohort
     overrides that still mix in the old control get V2 substituted, except where V2's posterior is
     credibly worse than the old control in that cohort (lower bound below -1%), which the
     proposal lists explicitly for the approver.
   - Prompt changed: if the new prompt has no passing validation on record, the hallucination path
     (section 7.1) starts; if it is safe, we leave it alone and only re-baseline the engagement
     model's prompt intercept.
5. If a supervisor edit and one of our versions happen inside the same step, the intermediate body
   cannot be fetched (the API returns only the current config); we record the gap.

---

## 7. Incidents

### 7.1 Hallucination

**Detector 1, on prompt changes (instant).** Any `config_changed` that changes `prompt_version` to
a prompt with no passing validation on record triggers one validation run on the new prompt,
n = 300 (3 credits). Incident if `pass_rate < min_validation_pass_rate` (the client's own gate).
At a 0.97 gate, failing means at least 10 failures in 300:
- a safe prompt with a 1% failure rate fails with probability P(Poisson(3) >= 10) = 0.1%
  (false-incident rate);
- a prompt with a 5.45% failure rate (sanity check: the public P2 level) is caught with
  probability 1 - P(Poisson(16.35) <= 9) ≈ 96%.

This is a population measure (300 conversations), not a single call.

**Detector 2, live flags (backstop, free).** Bernoulli CUSUM per workflow on connected calls in
event order. `p0` = the workflow's flag rate under its incumbent prompt (history first, then live),
`p1 = 3 p0`:

```
on flag:     S += ln(p1/p0)                       = 1.099
on no flag:  S += ln((1-p1)/(1-p0))               (= -0.030 at p0 = 0.0147)
S = max(0, S); alarm when S >= h_cusum and at least 5 flags since the last reset
h_cusum = ln(30 days x connected per day)   -> in-control ARL >= 30 days of calls (Lorden bound)
          nw_renewal: ln(28,500) = 10.3;  public_03 workflow: ln(405,000) = 12.9
```

Detection delay at a 3.6x flag-rate jump: about 10.3 / 0.030 ≈ 340 connected calls (about 5 hours
at 67 connected per hour). It catches unsafe prompts that slip past one validation run (a
"persuasive"-type prompt with a 3% failure rate passes a 0.97 gate about half the time; its flag
rate doubles, so the CUSUM fires within about half a day) and verifies fixes after apply.

**Fix selection.** LLM proposes, statistics decide:

```
candidates = [last-known-good prompt]            # the incumbent before the incident: it has
                                                 # days of live flag-rate and engagement data
if no last-known-good exists (the unsafe prompt is the business default, or LKG is itself suspect):
     candidates += /llm/suggest-fixes             # 3 credits
     # catalogue summaries are free from /workflows, so the LLM call is spent only when it can
     # change the decision
rank: candidates with live evidence first, by live engagement posterior;
      a candidate with no live engagement evidence is accepted only if nothing else exists, and
      never if its summary reads as a verbatim/strict script (Kestrel's "nobody stayed on the
      call" memo); then it ships with a 24h engagement watch and a 1% harm gate on live data
validate the chosen candidate once, n = 300 (3 credits): a prompt with a 1% failure rate fails
  the 0.97 gate with probability 0.1%. Never re-run the same prompt for the same decision
  (no cherry-picking); a failed validation moves to the next candidate.
propose: config = CURRENT config (rebased on the supervisor's version) with prompt := candidate,
  evidence.validation_run_id, impact >= 0.9, summary "Revert script to <P> after hallucination
  spike: validation 0.946 (n=300) vs gate 0.97; live flags 3.4x baseline". Uses the reserved slot.
```

The fix is an approval request to the supervisor's approver, never a silent revert (Kestrel Ops:
"don't undo my changes behind my back"; the evidence is the reason).

**Post-apply verification.** Reset the CUSUM with `p0` = the fix prompt's historical flag rate.
Verified when 300 connected calls show a flag rate whose upper 95% bound is under `2 p0` and
engagement is within its credible band. Otherwise escalate: next candidate, page.

**Credit budget.** Worst case per hallucination incident: 3 (detect) + 3 (LLM) + 3 x 3 (validate up
to three candidates) = 15 credits; typical with a known-good prompt: 6.

```
per-tenant envelope  = total_credits x (tenant workflows / all workflows)
safety reserve       = 2 incidents x 15 credits x workflows, capped at 80% of the envelope
discretionary        = envelope - reserve - spent      (we have almost no discretionary uses)
a tenant may borrow from the global pool only for an open safety incident
402 is treated as an incident: page, stop all non-safety spend
```

public_01/02: 200 credits, 3 workflows, reserve 90; expected spend about 6 to 12. public_03: 400
credits, 12 workflows, reserve 320.

### 7.2 Caller ID degraded mid-run (no event exists)

Detector per live DID on the concurrent ratio `r_d` (5.9), over a sliding 2-hour window:

```
alarm if  dials on d in window >= 100  and  upper 99.5% bound of r_d < 0.80
```

False-alarm rate: with 100 dials at pickup 0.25, SE(log r) ≈ 0.18, so a healthy DID must show
`log r_hat < -0.69`, a 3.8 SE event, p ≈ 7e-5 per window; over 6 windows a day x 3 DIDs x 10 days
that is about 1.3% per scenario. Detection: a DID falling to about 0.38 of its peers at a 33% share
of 400 dials/hour crosses after one hour (upper bound about 0.74).

**Response:** drain by auto-apply where `caller_id` is allowed, in steps of at most `max_shift`, each
step after one more dialling hour of evidence (chaining is allowed in the risk-reducing direction).
Where auto-apply is not allowed, an approval with safety priority. Share moves to healthy DIDs
pro rata. Graded beneficial (moving share off a degraded DID raises expected health).

### 7.3 Predicted public_02 timeline

Sanity-check only: the controller does not know these events are coming; times assume the step
rule above and mid-range approver latency.

| Sim time | Workflow | World | Controller action | Expected grading |
|---|---|---|---|---|
| before D0 | all | history (14 days, 1 chunk) | ingest; hour curves; cohort grids (renewal lang x age, 12 cells after merging); DID baselines; flag baselines | - |
| D0 00:00 | nw_renewal, ks_cancel | | auto-apply DID probes: renewal D3, D4 at 2.8% each; cancel D3 at 2.8% | neutral (fresh DIDs, small share) |
| D0 10:00 | nw_renewal | approvers open | approval #1: cohorts + hour weights + factorial exploration (W2, W3, V2, V3, V4 at 3.8%) | neutral |
| D0 10:00 | nw_kyc | | approval #1: cohorts + hour weights + DID probes D2, D3 at 2.8% (reaches the D1 batch) | neutral or beneficial (DID) |
| D0 11:00 | ks_cancel | Kestrel preference | approval #1: cohorts + hour weights + exploration | neutral |
| D0 10:30-14:00 | all | decisions | apply on the next step | - |
| D0 21:00 | renewal, cancel | probes classified healthy | auto-apply spread toward equal shares (cancel: first 0.2 TV step; remainder rides on D1's approval) | beneficial (DID) |
| D1 10:00-15:00 | renewal, cancel | | ramps from posteriors (W3 likely rising) | neutral or beneficial |
| D2 10:00 | nw_kyc | | DID spread approval (probes classified from D1 campaign dials) -> live in the D3 batch, fully by D5 | beneficial (DID) |
| D2 10:00 | ks_cancel | D2 flagged (hidden) | 11:00 step: D2 alarm (about 130 dials on D2 in the hour). Auto-apply drain 0.33 -> 0.13; 12:00 drain to 0. Pending approval, if any, abandoned and rebased | beneficial x2; D2 day share about 0.07, then 0 |
| D2-D3 | nw_renewal | W3 decay onset (2 days after first exposure) | CUSUM accumulates; ramps frozen at the alarm | - |
| D3 11:30 | ks_cancel | supervisor ships P2 | 11:45 step: validate P2 n=300 (3 cr) -> fails gate; validate P1 n=300 (3 cr) -> passes; fix approval rebased on the supervisor version, reserved slot | - |
| D3 12:05-13:45 | ks_cancel | approver decides | apply on the next 15-minute step; **MTTR about 50 to 150 min** | beneficial |
| D3 evening | ks_cancel | | post-apply verification passes after about 300 connected | - |
| D3-D4 | nw_renewal | W3 below control | rollback when `P(de_rollback < -1%) <= 10%` at the predicted apply time | beneficial |
| D5 15:00 | nw_renewal | supervisor sets default voice V2 | freeze voice decisions; rebase proposal (V2 as control, resume V3/V4 test) at the next slot, D5 15:00-17:00 or D6 10:00 | neutral or beneficial |
| D9 | all | | end-game freeze since about D8.5: safety and rollbacks only | - |

Expected outcome shape on public_02: nw_kyc pickup clearly up (DID spread live from D3); renewal
and cancel pickup up (spread); engagement lift small on renewal and cancel (dev volumes, harm-gated
ramps); zero harmful changes as the target, with the residual risk quantified in 5.4 and 5.9; one
hallucination incident fixed in about 1.5 hours; no lapsed, expired or 403s.

---

## 8. Reliability

### 8.1 Idempotency keys

| Action | Key | Retry semantics |
|---|---|---|
| chunk ingest | `chunk_no` (PK in `chunk`) | Re-fetch and skip if applied. Raw rows keyed by `event_id`, so even a partial earlier attempt cannot duplicate (it rolled back). |
| approval submit | `proposal_id = "{world8}-{wf}-{seq:05d}"`, allocated and committed with the READY row | Retry with the same id. `409 duplicate_proposal_id` means the first attempt landed: reconcile with `GET /approvals?workflow_id=` matched on `proposal_id`. |
| auto-apply | same id scheme | A 403 does not consume the id (we do not retry 403 anyway). `409 duplicate` means it landed: confirm in config history by `proposal_id`. |
| apply | `approval_id` | Retry safe: already applied returns 409 `not_approved` with status applied; reconcile from config history. |
| advance | step journal (from, to) | **Never blindly retried** (8.3). |
| validation, LLM | intent row | Not idempotent and no list endpoint. See 8.3. |

### 8.2 Intent log and reconciliation

Every non-idempotent or state-changing call is preceded by a committed `intent` row
(`state = intended`, full request). After the response: `done` with the response, in the same
transaction as the resulting proposal or step transition. On restart, every `intended` intent is
resolved:

| Kind | Resolution |
|---|---|
| advance | `GET /sim/state`: `sim_minute == from` means not applied (re-issue); `== to` (or end) means applied, discover chunks with `GET /events/chunks?after=cursor`. |
| submit_approval | find by `proposal_id` in `GET /approvals?workflow_id=`; found -> PENDING with its id; not found -> resubmit with the same id. |
| auto_apply | find by `proposal_id` in config history; found -> APPLIED; not found -> re-evaluate from scratch (state may have moved). |
| apply | `GET /approvals/{id}`: applied -> APPLIED (version from config history); approved -> re-run pre-apply checks. |
| validation | compare `credits_remaining` with the ledger. Unchanged: never ran, re-run. Decreased: the run happened but its id and pass rate are lost. We never cite evidence we have not seen, so we re-run and record the lost credits. |
| llm | same credit check; suggestions are re-requested only if still needed. |

Reconciliation also runs every step in a cheap form: `GET /approvals?workflow_id=&status=pending`
for each workflow with an in-flight proposal.

### 8.3 Simulator errors and timeouts

| Response | Handling |
|---|---|
| advance timeout / connection error | Do not retry. `GET /sim/state` (blocks on the sim lock until the advance finishes), then resolve as in 8.2. Advance timeout = 900 s (public_03 hour-steps take seconds; this is headroom). |
| 5xx / timeout on GET | Retry with exponential backoff (0.5 s to 30 s, jitter), unbounded with an alert after 2 minutes. |
| 5xx / timeout on approval submit, auto-apply, apply | Retry with the same key (8.1). |
| 5xx / timeout on validation or LLM | No blind retry; resolve via credits (8.2). |
| 400 bad_minutes, 422 invalid_config / no_change / missing_fields | Bug: alert, mark the proposal FAILED, keep running. |
| 403 auto_apply_not_permitted | Local policy replica disagrees: alert, re-route to approval, never retry the same body. |
| 409 stale_base_version | Rebase on the current config, new proposal superseding the old. |
| 409 finished | Final reconciliation and export, exit 0. |
| 410 approval_lapsed | Should not happen (pre-apply `apply_by` check); re-queue. |
| 402 out_of_credits | Incident: page, stop non-safety spend. |
| 404 | Reconcile (unknown approval after a world change means the simulator restarted: fingerprint check). |

---

## 9. Observability and supervisor view

### 9.1 Loop health (`/ops/health`, `/ops/metrics`, `loop_metric` table)

Per step: wall ms per phase (advance, download, parse, load, decide, act), events ingested,
events/s, ingest lag (sim minutes between the newest ingested event and `sim_minute`, should be 0
after ingest), decision lag (sim minutes from the first evidence of a detected condition to our
action), decisions per sim hour, proposals by state, credits spent and reserve headroom per tenant,
Postgres size, controller RSS.

**Alerts that page** (each message says what to look at): controller made no step progress for 10
wall minutes; the same chunk failed to ingest 3 times; an approved proposal is within one step of
`apply_by` and not applied; any 403 from auto-apply (policy replica drift); credits below the safety
reserve, or any 402; a hallucination incident open more than 2 sim hours without a submitted fix;
world fingerprint mismatch on restart.

**Alerts that do not page** (morning digest): approver rejections and expiries (handled
automatically), harm-gate blocks, CUSUM false alarms that were cleared, retries that succeeded.

### 9.2 Supervisor view (`/supervisor/{tenant}`, JSON plus one HTML page)

One page per tenant, one section per workflow, answering Kestrel Ops' four questions:
- **What is being tested**: per experiment, arms and current shares per cohort, in words ("V3 for
  over-60s: 25% of calls, probably better, not yet proven").
- **What is winning**: declared winners with effect and 95% interval, holdout size, decay monitor
  state.
- **What we want from you**: pending proposals ranked by impact score with summary, diff, affected
  leads, dry run and deadline (`expires_at`); upcoming announced auto-applies.
- **What changed today**: timeline from `audit_event`: who (controller, supervisor, approval id),
  what, why (decision id), and what happened after (KPI movement since).
- Pickup and engagement shown side by side per cohort, never blended. Open incidents at the top.

**Explain endpoints** (backing the live review and IRDAI requests):
- `/explain/change?workflow=&at=`: the config version live at that time, the proposal and decision
  that created it, the approval id and decided time, the posterior snapshot, and KPI before and after.
- `/explain/call?lead_id=&date=`: each call's variant, config version, cohort, the allocation that
  produced it, and the decision and approval behind that allocation.

The approver's name is not available from the API; we record `approval_id`, `decided_at` and the
tenant. Stated gap for Compliance.

---

## 10. Testing strategy

**End-to-end first** (pytest starts the real simulator as a subprocess per test; controller code
never imports `sim`). Assertions read the grader output, which is test-only.

| Test | Scenario | Asserts |
|---|---|---|
| floor_public_01 | public_01 | completes; 0 harmful; 0 lapsed; 0 expired; 0 auto_apply_rejected; apply_conflicts 0; every applied version has a decision and audit row |
| floor_public_02 | public_02 | same, plus hallucination MTTR < 4 h; flagged DID daily share < 0.10 on the flag day and 0 after; supervisor edits never reverted without an approval |
| custom: unsafe default prompt | test YAML (written by the test) | LLM path runs; fix validated; never P5-like |
| custom: approver rejects 60% | test YAML | re-queue with fresh data; fatigue never exceeded |
| custom: 6h TTL, latency up to 8h | test YAML | no expiries (timing calculator) |
| custom: auto-apply disabled everywhere | test YAML | everything routes to approval; 0 403s |
| custom: decaying strong winner, high volume | test YAML | decay share after onset < 10% by onset + 2 days; no harmful rollback |
| custom: pre-flagged DID at 0.3 | test YAML | probe size keeps dd >= -2%; DID dropped |
| load_public_03 | public_03 | one sim day ingested and decided in < 10 wall minutes; RSS < 1 GB; ingest events/s reported |

Custom test scenarios are written by the test harness, which is allowed to create scenarios; the
controller still only sees HTTP.

**Unit tests** only for pure code: posterior and harm-gate numbers against hand-computed values (the
tables in this document are test fixtures), CUSUM ARL by seeded simulation, local policy replica
(`changed_dims`, `max_shift`) against a table of cases, timing calculator, rebase logic.

**Crash-recovery demonstration** (`scripts/crash_test.sh`, output in `reports/crash/`):
1. Reference: clean run of public_02, export the normalised audit trail and the sim config history.
2. Deterministic faults: env `CRASH_AT=<point>:<occurrence>` calls `os._exit(137)` at named points:
   after intent commit before each HTTP kind, after HTTP before response commit, mid-chunk
   transaction, between approval decided and apply. One run per point, restart until done.
3. Random faults: 10 SIGKILLs at random wall times over one run.
4. Pass criteria: grader shows 0 lapsed, 0 duplicate applies, 0 apply_conflicts; every sim config
   version maps to exactly one of our proposals or a supervisor edit; no pending approval left
   unresolved; posteriors at the end equal the reference within float tolerance. Because decisions
   are deterministic and step boundaries come from the journal, crash runs reproduce the reference
   audit trail exactly, except after a lost validation response (8.2), which the report lists.

---

## 11. Module breakdown

```
controller/
  main.py                 run(sim_url) -> exit code
  settings.py             Settings (env), TenantPrefs (prefs.yaml: stakeholder agreements, keyed by tenant_id)
  types.py                shared dataclasses: Config, Allocation, CohortSet, Proposal, Decision, Posterior, HarmReport
  simclient/client.py     SimClient: state, advance, list_chunks, stream_chunk, workflows, workflow, config,
                          config_history, auto_apply, request_approval, get_approval, list_approvals, apply,
                          suggest_fixes, validate; raises SimError(status, code, detail); retry classes per method
  store/db.py             pool, tx() context, migrations/*.sql
  store/repos/*.py        one repository per table group
  journal/intent.py       IntentJournal.begin(kind, req) -> id; complete(id, resp); open_intents()
  journal/step.py         StepJournal.begin(from, to); advanced(result); last_open()
  world/fingerprint.py    compute(client) -> str
  world/registry.py       WorkflowRegistry: specs, governance, objective, options (all from API)
  world/configs.py        ConfigMirror: current(wf), body(wf, version), on_config_changed(evt)
  ingest/fetch.py         ChunkFetcher (prefetch thread, streaming)
  ingest/parse.py         parse_chunk(bytes) -> ColumnBatch (numpy columns, dictionary codes)
  ingest/aggregate.py     apply(batch, tx): COPY raw, upsert aggregates
  ingest/leads.py         LeadTracker: active population, cell ids, eligibility reconstruction
  cohorts/grid.py         build_grid(history_aggs, spec) -> CohortSet; regrid_candidate(model) -> CohortSet | None
  cohorts/cohortset.py    CohortSet: cell_of(features), cohort_of(cell), key(), to_config()
  stats/glm.py            fit_ridge_logit(design, counts, prior) -> Posterior (mean, cov)
  stats/draws.py          seeded_draws(posterior, seed, n) -> ndarray
  stats/sequential.py     BernoulliCusum, PageCusum (state serialisable)
  stats/ratios.py         mantel_haenszel(...), wilson(...), beta_binomial(...)
  stats/ope.py            snips(logged, propensity_old, propensity_new) -> (est, ci)
  learning/engagement.py  EngagementModel.fit(wf) ; arm_effects(cohort_set) -> per-cohort draws
  learning/harm.py        harm_gate(cur_cfg, new_cfg, draws, active_pop) -> HarmReport ; did_gate(...)
  learning/allocator.py   plan_allocations(wf, state, draws, constraints) -> AllocationPlan
  learning/winners.py     declare(wf, draws, counts) -> list[Winner] ; holdout_size(...)
  learning/decay.py       DecayMonitor.update(segment) ; rollback_plan(...)
  learning/did.py         DidHealth.update() ; classify() ; plan_caller_ids(...)
  learning/schedule.py    HourCurves.fit(history+live) ; plan_hour_weights(...)
  learning/guardrail.py   GuardrailMonitor.check(wf) -> list[Breach]
  incidents/hallucination.py  HallucinationResponder (state machine: detect, validate, fix, verify)
  incidents/did_incident.py   DidIncidentResponder
  incidents/credits.py    CreditBudget.authorize(tenant, purpose, amount) -> bool ; ledger
  governance/policy.py    changed_dims(old, new) ; max_shift(old, new) ; auto_permitted(wf, old, new) -> (bool, reasons)
  governance/timing.py    submit_window(wf, now) ; expiry_safe(wf, t) ; fatigue(wf, t)
  governance/builder.py   build_request(change_set, dry_run, ...) -> dict (summary, diff, affected, impact, rationale)
  governance/lifecycle.py transition(proposal, event) -> proposal (pure, table-driven)
  governance/router.py    route(change_set) -> Route
  governance/reconcile.py reconcile_all(client, store)
  planner/desired.py      DesiredChangeSet.merge(items by priority)
  planner/rebase.py       rebase(current_cfg, desired_delta, supervisor_edits) -> new_cfg
  loop/stepper.py         next_minutes(state) -> int
  loop/orchestrator.py    run_step() ; resume()
  audit/writer.py         record(tx, kind, wf, refs, summary, detail)
  audit/explain.py        why_changed(wf, at) ; why_heard(lead_id, date)
  api/app.py              FastAPI: /supervisor/{tenant}, /explain/*, /ops/*
  obs/metrics.py, obs/alerts.py
```

**Dependency order** (each layer depends only on layers above it):
1. `types`, `settings`, `store` (schema first: it is the contract between teams)
2. `simclient`, `journal`, `audit/writer`
3. `world`, `ingest`
4. `cohorts`, `stats` (pure; no I/O)
5. `learning`, `incidents`
6. `governance`, `planner`
7. `loop`
8. `api`, `obs`, `audit/explain`

**Parallel build plan** (contracts fixed on day 1: `schema.sql`, `types.py`, the `SimClient`
interface):

| Engineer | Modules | Can start | Tested by |
|---|---|---|---|
| 1 | simclient, journal, loop, reliability | day 1 | crash test, E2E floor |
| 2 | store, ingest, world | day 1 | load test, golden counts vs grader truth (test only) |
| 3 | stats | day 1 (pure) | unit fixtures from this doc |
| 4 | learning (engagement, allocator, harm, winners, decay) | after stats interfaces | custom scenarios |
| 5 | learning (did, schedule, guardrail), incidents | after stats interfaces | public_02, custom scenarios |
| 6 | governance, planner | day 1 (policy, timing, lifecycle are pure) | unit tables, E2E |
| 7 | audit/explain, api, obs | after schema | E2E plus manual review |

---

## 12. Stakeholder positions

| Memo | Accept | Push back | Wrong claim (against the contract) |
|---|---|---|---|
| 01 Sales | A per-client lift number per KPI, from our audit store. | One blended score (Product and constraint 3 forbid it). "Winning by lunch, live by evening": a morning (09:00 to 12:00) has about 250 to 300 connected calls, roughly a tenth of the 2,500 a 10% effect needs. P4 rollout: unsafe, refused. | "Kestrel turned on auto-apply for everything": only schedule, caller_id, welcome, voice, at max_shift 0.2; never prompt or cohorts. "Northwind fine with KYC auto-apply": KYC auto-apply is disabled in policy, and Northwind's Compliance requires human approval. |
| 02 Compliance (Northwind) | Everything: no customer-facing auto-apply on Northwind (tenant pref), validation evidence on scripts, per-call explain endpoint, weekly digest of operational changes from the audit store, "slow and right" is our default (harm gate). | Named approver: the API exposes only approval id and time; we store those and ask Formi to add identity. | "Call timing and caller-ID rotation are covered by the policy we signed": true for renewal, **false for KYC** (auto-apply off), where those changes go through approval. |
| 03 SRE | Kill-safe, idempotent, Postgres only, no Redis, actionable alerts only, loop health without logs, one public_03 day under 10 minutes measured. | Nothing material. | None; "prefer existing infra" is satisfied, and Compose brings Postgres up on the grading box. |
| 04 Kestrel Ops | Two requests a day, none before 11:00, as our cap (stricter than policy's 4); one request with all changes; one screen; her prompt edits respected; strict-script "fixes" refused (fix ranking needs live engagement evidence). | A third request in a day only for a safety fix (hallucination), stated up front. "Tell me before": there is no notification channel, so customer-facing auto-applies are announced on the view at least an hour ahead. | None false; note her own prompt edit is the kind of change our incident path catches, and we fix it through her approver with evidence, never silently. |
| 05 Product | Separate KPIs; one cohort object with versioned identity; impact score with a fixed meaning (1.0 ≈ 5% primary lift, harm-discounted); expired = rejected and re-queued with fresh data; evidence only from calls that happened; minimum cohort and window chosen with costs (5.5). | P0 list: caller-ID health yes, supervisor view yes (minimal), per-person call time **no** (thin data, x0.25 penalty risk; per-cohort timing captures most of it). | None false. |
| 06 CTO | Outcomes a compliance head can sign; no compute without a decision behind it (LLM only without a known-good prompt; about 6 to 12 credits used); scale numbers and 10x story; written choices. | - | "Credits are cheap here" is only partly true: 200 credits in public scenarios, and one careless retry loop on validation would burn a hallucination fix's budget. |

---

## 13. Top risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Dev-scale volumes make winners slow, so engagement lift is small. | Factorial design, soft exploitation by probability matching inside the harm gate, exploitation paying for exploration, honest reporting of expected lift. At public_03 volumes the same rules converge in 1 to 2 days. |
| 2 | The harm prior (`tau_H = 0.06`, mean 0) is wrong for hidden scenarios, so exploration is graded harmful. | `tau_H = max(0.06, tau_EB)` widens with observed dispersion; exploration starts at 3.8% per arm; every later increment is gated on fresh posteriors. Residual risk per exploration change <= 2.5% under the prior. |
| 3 | Hidden policies differ (auto-apply off, caps of 1, 6h TTL, long latencies). | Every parameter read from the API; local policy replica and timing calculator are generic and covered by custom-scenario E2E tests. |
| 4 | Our own auto-apply or a supervisor edit stales a pending approval. | One change in flight per workflow; auto-apply only when nothing is pending, except safety drains, which abandon (never apply) the stale approval. |
| 5 | Decay at dev volume is detected late. | Computed holdout (up to 20%), Page CUSUM, rollback timed to the predicted apply time with a 10% harm tolerance. Residual: one to two days of exposure at dev scale, stated. |
| 6 | DID probes graded harmful if a fresh DID is pre-flagged. | 2.8% probe from the DID harm gate; classification within about a day; equal-split spread only to healthy DIDs. |
| 7 | Model misspecification: interactions or multi-feature effects. | Interaction LR test; all three features in the effect model, not just the grid; regrid proposal when off-grid heterogeneity is worth more than 1%. |
| 8 | Credits exhausted mid-incident. | Per-tenant envelopes with a safety reserve for 2 worst-case incidents per workflow; LLM only when it can change the decision; never re-run a validation to fish for a pass. |
| 9 | Throughput at public_03 or 10x. | One transaction and COPY per chunk, history calls aggregated not stored, prefetch thread. At 10x the first thing to break is single-process JSON parsing (about 100k events/s per core, about 85 s per 10x sim day): shard the controller by workflow (workflows share nothing but credit envelopes), partition `call_fact` by day with 30-day raw retention, and the simulator itself becomes the bottleneck. |
| 10 | Crash between a credit-spending call and its response. | Intent log plus credit-ledger reconciliation; never cite unseen evidence; the cost is bounded to one extra 3-credit run, listed in the crash report. |
| 11 | History hour curves biased by eligibility, so schedule weights mislead. | Attempt-bucket stratification, shrinkage, weights clipped to [0.25, 4], post-change per-(cohort, hour) checks; schedule changes cannot be graded harmful. |
| 12 | Guardrail definition ambiguous. | We enforce the stricter reading at proposal time (no predicted drop at all) and an absolute 2-point limit for rollback, both stated as assumptions. |

### Assumptions referenced above

- **A-1** No welcome x voice interaction (tested with an LR test, 5.3).
- **A-2** Arm log-multipliers a priori `N(0, 0.06^2)` for the harm gate, widened by observed dispersion.
- **A-3** Guardrail `max_drop` is absolute, rolling 2-day window; proposals must predict no drop.
- **A-4** Kestrel's "2 a day, after 11:00" and Northwind's "no customer-facing auto-apply" are tenant
  preferences we honour above policy (in `prefs.yaml`, keyed by tenant id; unknown tenants get the
  default in 6.4).
- **A-5** Chaining auto-applies only in the risk-reducing direction.
- **A-6** Untested caller IDs: `P(flagged) = 0.2`, flagged relative pickup 0.35.
- **A-7** The grading box runs Docker; `run.sh` says so.
