# Design proposal B: governance, compliance and safety first

Author: Designer B (governance). Status: proposal for the lead engineer. Stack per
`docs/decisions/0001-stack.md` (Python 3.11, Postgres 16, httpx, numpy, psycopg 3, FastAPI,
Docker Compose, no LLM framework).

**Thesis.** A regulated client has to be able to sign off on every change, and the controller
must never break a constraint. Lift that has to be explained away is worth less than a smaller
lift a compliance head can approve. So this design spends most of its effort on the proposal
lifecycle, timing, batching, rebasing, honest proposal contents, the audit store and crash
safety. The learning loop is complete but deliberately simple: one challenger per dimension, a
ramp with a hard limit on how much harm it can do, a deterministic winner rule, and a holdout
for decay.

All governance parameters (tick, approver hours, latency, TTL, apply window, caps, allowed dims,
max_shift, mid_flight, options, objective) are read from `GET /sim/state` and
`GET /workflows/{id}` at startup and re-read every simulated day. When this document quotes
public-scenario values it is only to show that a formula gives the right answer. None of them
are constants in code.

---

## 1. Scope

### 1.1 Selection principle

1. **First, whatever the floor and the 8 constraints need.** If governance hygiene, audit or
   crash safety breaks, the run is disqualified or penalised whatever the lift, so these are
   built properly: C1, C9, C12, C13.
2. **Second, levers whose benefit is large and that can be checked with data we can actually
   collect.** Caller-ID health (C5) is the largest pickup lever (about +38% on a single-DID KYC
   workflow) and can be measured within a day. Incident response (C8) is graded on time to fix.
3. **Third, the learning loop at the sample size it can really support.** About 950 connected
   calls a day on nw_renewal supports roughly one confirmed winner per dimension per scenario.
   We build that properly (C6, C7) instead of a bandit that cannot be explained.
4. **Cut what thin data cannot support** (per-person timing), and anything whose only purpose
   is a demo (blended scores).

### 1.2 Scope table

| # | Capability | Status | Why | Cost to finish |
|---|---|---|---|---|
| C1 | Event ingestion | **Built** | Floor. Each chunk is ingested in one transaction, idempotent by chunk number, and streamed into COPY. | n/a |
| C2 | Shared cohort object | **Built** | Experiments, detectors, dry runs and the supervisor view all reference one registry keyed by a hash of the cohort's definition. | n/a |
| C3 | Bootstrapping from history | **Built** (baselines only) | Per-DID pickup, per-segment hour curves, flag-rate baseline, segment sizes for affected-lead counts. History ran on the default config, so it tells us nothing about variants. | n/a |
| C4 | Call-time optimisation | **Partial**: per-cohort `hour_weights` only | Per-person preferred hour is cut. A returning person has at most 8 past attempts spread across 12 hours, so a per-person archetype posterior is barely different from the cohort prior (see 5.7). | About 1 day: Bayesian archetype posterior per returning person, behind the 200k entry cap. |
| C5 | Caller-ID health | **Built** | Concurrent per-DID comparison, harm-limited probes of unknown DIDs, equal spread, quarantine. | n/a |
| C6 | Welcome and voice experiments | **Built, simple** | Factorial across the two dims, one challenger per dim, harm-limited ramp, Pocock-Bonferroni winner rule. Only on engagement-primary workflows (see 5.2). | Segment-specific carve-outs are partial (screening only). About 1 day for confirmatory cohort experiments. |
| C7 | Promotion, decay, rollback | **Built** | 10% holdout after every promotion. Rollback when the holdout beats the promoted variant. | n/a |
| C8 | Hallucination incidents | **Built** | Triggered by a prompt change, with a free flag-rate detector as backup, and paid confirmation. The fix is the last known-good prompt; the LLM is a fallback. Verified after apply. | n/a |
| C9 | Governance client | **Built, deepest** | State machine, release trains, slot reservation, timing, apply deadlines, supersede, rebase, notice period. | n/a |
| C10 | Impact, affected leads, dry run | **Built** | Stratified replay over our own recorded calls. The impact score is a conservative expected relative lift, so it has units. | n/a |
| C11 | Supervisor view | **Built, minimal** | One JSON API and one server-rendered HTML page per tenant. | Auth, an action button for holds (the endpoint exists). |
| C12 | Decision and audit store | **Built** | Decisions, transitions, provenance per config version, and per-call "why did they hear this". | n/a |
| C13 | Crash recovery demonstrated | **Built** | Write-ahead intent log, reconciliation, fault injection at every intent phase, a crash-equivalence test. | n/a |
| C14 | Observability of the loop | **Partial** | A metrics table, `/ops` endpoints and alert rules. No Prometheus or Grafana (SRE asked for no new stateful infra). | Half a day: a `/metrics` exporter. |
| C15 | Fairness across tenants | **Partial** | Per-tenant credit envelopes with a shared incident reserve. Approver attention is already per workflow under the policy caps. | About 1 day: weighted fair queueing of releases across workflows that share one human approver (the API does not model shared approvers). |
| (extra) | Prompt optimisation outside incidents | **Cut** | There is one prompt per workflow, so prompts cannot be A/B tested on concurrent traffic. A +3% effect needs about 17,400 connected calls per arm (5.2). | n/a |
| (extra) | Cross-workflow promotion | **Cut** | Product memo: later. | n/a |

---

## 2. Architecture

### 2.1 Diagram

```
                       Simulator (HTTP only)
   /sim/advance  /events/chunks  /workflows  /approvals  /config  /validation  /llm
        ^              |              |           ^   |        ^         ^
        |              v              v           |   v        |         |
+-------+--------------------------------------------------------------------------+
|  controller process (single, deterministic, no wall-clock in decisions)          |
|                                                                                  |
|  loop.py  ---- step rule ----> intents.execute(ADVANCE)                          |
|     |                                                                            |
|     v                                                                            |
|  ingest/  (chunk fetch -> parse -> COPY staging -> facts + aggregates, 1 tx/chunk)|
|     |                                                                            |
|     v                                                                            |
|  world/   (policies, effective policy = policy AND directives, config mirror,    |
|            approval mirror, clock helpers)                                       |
|     |                                                                            |
|     +--> incidents/  (DID detector, prompt-change trigger, flag-rate detector,   |
|     |                 drift, credit budget)                                      |
|     +--> learning/   (experiments, caller-ID, call-time, decay monitors)         |
|     |         |  both emit Patches (desired state)                               |
|     v         v                                                                  |
|  planner/  (overlay of patches on live config, rebase, compose)                  |
|     |                                                                            |
|     v                                                                            |
|  governance/ (policy_check, timing, lifecycle state machine, release trains,     |
|               package builder: summary, diff, dry run, affected leads, impact)   |
|     |                                                                            |
|     v                                                                            |
|  reliability/intents.py  (write-ahead intent + decision row, then HTTP call,     |
|                           then result row; reconciler on restart)                |
+--------------------------------------+-------------------------------------------+
                                       |  psycopg3 (COPY, transactions)
                                       v
                     +-------------------------------------+
                     |  Postgres 16 (only stateful thing)  |
                     |  raw facts | aggregates | decisions |
                     |  proposals | intents | audit        |
                     +-------------------------------------+
                                       ^
                                       | read-only
                     +-------------------------------------+
                     | api/ FastAPI (uvicorn, same image)  |
                     | /supervisor  /audit  /ops           |
                     +-------------------------------------+
```

### 2.2 Components

| Component | Job | State it owns |
|---|---|---|
| `simclient` | Typed HTTP client. Maps errors to exceptions, retries only idempotent GETs, sets timeouts. No business logic. | none |
| `ingest` | Pulls chunks in order, parses NDJSON, COPYs into facts, updates aggregates and lead state, mirrors `config_changed` and `approval_decided`. | `chunks`, `calls`, `leads`, `campaigns`, `agg_*` |
| `cohorts` | Canonicalises a `where` clause, computes `cohort_uid`, maps features to a cell code (0..251), computes first-match membership for a config version. | `cohorts`, `cohort_cells`, `effective_membership` |
| `stats` | Pure functions: proportions, CIs, z, Pocock constant, binomial tail, harm-limited ramp. | none |
| `world` | Workflow specs, effective policy, config mirror, approval mirror, clock helpers (`next_working`, safe-submit windows). | `workflows`, `config_versions`, `approvals_mirror` |
| `incidents` | Detectors, incident state machine, credit budget. | `incidents`, `validation_runs`, `credit_ledger` |
| `learning` | Experiments (welcome, voice), caller-ID module, call-time module, decay monitors. Emits patches. | `experiments`, `experiment_looks`, `did_status` |
| `planner` | Overlays patches on the live config. Handles rebase and invalidation when a supervisor edits. | `patches` |
| `governance` | Local policy check, timing, release trains, lifecycle, package builder. | `proposals`, `proposal_transitions` |
| `reliability` | Write-ahead intent log, executor, reconciler. | `intents` |
| `api` | Read-only supervisor, audit and ops views, plus one write: supervisor holds. | `holds` |

### 2.3 Where state lives and what happens on restart

Everything lives in Postgres. The controller process holds only caches it can rebuild from
Postgres in seconds: current policies, aggregates for open experiments, and the cohort map. On
start:

1. `GET /sim/state` and `GET /workflows`. Compute the world fingerprint (section 8.6). If it
   does not match the stored world, start a new namespace. Never mix two worlds.
2. Run the **reconciler** over every intent in status `pending` (section 8.3).
3. Resume ingestion from `max(chunk_no) where status = 'done'`. Any partially ingested chunk was
   rolled back by its transaction, so it is simply ingested again.
4. Re-read open proposals and poll `GET /approvals?workflow_id=` for each workflow that has one.
5. Re-enter the loop at step 1. Decisions are a pure function of the store plus API answers, so
   the controller makes the same decisions it would have made without the crash.

---

## 3. Data model

All tables have `world_id smallint` as the leading key column (omitted below for brevity).
Codes: `workflow_no smallint`. Option codes are smallints indexed into the workflow's option
list (from the API). `cell smallint` = lang(7) x tier(3) x age(4) x value(3), so 252 cells.

### 3.1 Raw facts (append-only)

| Table | Key columns | PK / indexes | Notes |
|---|---|---|---|
| `chunks` | chunk_no, kind, first_event_id, last_event_id, until_minute, events, status, ingested_wall | PK chunk_no | The ingest cursor. A row is written in the same tx as the chunk's facts. |
| `calls` | workflow_no, event_id bigint, sim_minute int, lead_no int, person_no int, cell, attempt_no, campaign_day_index, config_version smallint, cohort_code smallint, welcome, voice, caller_id, prompt (smallint codes), connected, engaged, flag (bool), disposition smallint, adherence real | PK (workflow_no, event_id); idx (workflow_no, lead_no); range-partitioned by sim day | Raw facts. This answers the IRDAI "why did this customer hear this". |
| `leads` | workflow_no, lead_no, person_no, campaign_no, cell, enrolled_minute, state (active/concluded/exhausted), attempts, last_attempt_minute | PK (workflow_no, lead_no); idx (workflow_no, state, cell) | Updated per chunk. `exhausted` is derived from cadence length (read from the API). |
| `campaigns` | workflow_no, campaign_no, created_minute, leads, config_version_at_creation | PK (workflow_no, campaign_no) | Needed for `next_campaign_only` locking. |
| `config_versions` | workflow_no, version, actor, applied_minute, changed_dims text[], proposal_id, approval_id, config jsonb NULL, body_source enum(ours, fetched, unknown) | PK (workflow_no, version) | The API has no history bodies. We store our own bodies, and fetch the body of supervisor versions as soon as we see the event. |
| `approvals_mirror` | approval_id, proposal_id UNIQUE, workflow_no, status, created_at, expires_at, decided_at, apply_by, reasons jsonb, last_polled_minute | PK approval_id | Reconciled against `GET /approvals`. |
| `validation_runs` | run_id, workflow_no, prompt, n, failures, pass_rate, purpose, incident_id, intent_id | PK run_id | Every run is kept, pass or fail (the anti-cherry-pick record). |

### 3.2 Aggregates (updated in the chunk transaction, so exactly once)

| Table | Grain | Measures | Used by |
|---|---|---|---|
| `agg_engage` | workflow, day, cell, config_version, welcome, voice, prompt | connected, engaged, flags | experiments, dry runs, decay |
| `agg_pickup` | workflow, day, hour, cell, caller_id, attempt_bucket (1, 2-3, 4+) | dials, connected | DID detector, call time, dry runs |
| `agg_hourly` | workflow, day, hour, prompt | dials, connected, engaged, flags | flag-rate detector, drift, guardrail |

Cohort and segment statistics are always **sums over cells**. A cohort is a set of cells, so
every module computes cohort numbers the same way from the same rows.

### 3.3 Control and audit

| Table | Key columns | Purpose |
|---|---|---|
| `intents` | intent_id, kind, idem_key UNIQUE, workflow_no, payload jsonb, status (pending/done/failed/aborted), decision_id, result jsonb, created_minute, created_wall | Write-ahead log of every side-effecting call. |
| `decisions` | decision_id, sim_minute, workflow_no, module, kind, rule_id, rule_version, inputs jsonb (the stat snapshot), outcome jsonb, parent_decision_id | Every decision, **including decisions not to act** ("held release: fatigue", "did not ramp: LCB -9%"). |
| `proposals` | proposal_id, workflow_no, path (approval/auto), priority_class, state, base_version, proposed_config, diff, change_summary, rationale, affected_leads, dry_run, impact_score, evidence, approval_id, submit_minute, apply_by, applied_version, superseded_by, rebased_from, notice_posted_minute | Our superset of the simulator's approval. |
| `proposal_transitions` | proposal_id, seq, from_state, to_state, sim_minute, reason, decision_id | PK (proposal_id, seq). The state machine's history. |
| `patches` | patch_id, workflow_no, owner (module, object id), path, value jsonb, state, proposal_id, decision_id | The desired-state overlay (section 6.8). |
| `allocation_provenance` | workflow_no, version, cohort_uid, dim, option, share, source (default/champion/arm/holdout/supervisor/probe), experiment_id, proposal_id, approval_id | Per config version: why each option had its share. |
| `cohorts`, `cohort_cells` | cohort_uid, workflow_no, definition jsonb (canonical), def_hash, label, kind (segment/config), created_by | The shared cohort object. Immutable: a new definition gets a new uid. |
| `effective_membership` | workflow_no, version, cell, cohort_uid | First-match result per config version, 252 rows per version. |
| `experiments`, `experiment_looks` | experiment_id, workflow, cohort_uid, dim, champion, challenger, stage, share, state; looks: n/x per arm, z, lcb, ucb, gate result, decision_id | Learning state and evidence trail. |
| `did_status` | workflow, caller_id, state (unknown/probing/healthy/quarantined), evidence | Caller-ID module state. |
| `incidents` | incident_id, workflow, kind, state, opened_minute, detector, evidence, fix_proposal_id, verified_minute, closed_minute | Incident state machine. |
| `credit_ledger` | seq, sim_minute, kind, cost, remaining_after (from API), tenant, incident_id | Reconciled against `credits_remaining`. |
| `holds` | workflow_no, set_by, reason, from_minute, until_minute | Supervisor "pause non-incident changes". |
| `alerts`, `metrics` | see section 9 | Loop health. |
| `directives` | tenant_id, scope, directive jsonb, source (memo), version | Client agreements outside the API contract (section 6.3). |

### 3.4 Raw versus aggregated, and sizes at public_03 scale

public_03: 12 workflows, 54k dials/day each (4,500 dials/h x 12 h), 16k leads/day each, 7
history days plus 5 scenario days.

| Table | Rows | Bytes/row (incl. tuple header) | Heap | Indexes | Total |
|---|---|---|---|---|---|
| calls | 12 x 54k x 12 days = 7.8M | about 80 | 620 MB | PK about 250 MB, lead idx about 250 MB | **about 1.1 GB** |
| leads | 12 x 16k x 12 = 2.3M | about 70 | 160 MB | about 120 MB | about 280 MB |
| agg_pickup | about 15k per workflow-day (12 h x observed cells x DIDs x 3 buckets) -> 2.2M | about 60 | 130 MB | 60 MB | about 190 MB |
| agg_engage | about 2k per workflow-day -> 290k | about 60 | 17 MB | 10 MB | about 30 MB |
| agg_hourly | 12 x 12 x 12 x prompts -> about 2k | | | | negligible |
| decisions, proposals, intents, transitions | tens of thousands | about 1 KB (jsonb) | | | under 100 MB |

That is under 2 GB on disk. Postgres gets `shared_buffers = 1GB`, the controller stays under
about 600 MB (it streams chunks line by line and never holds a chunk in memory), and the
simulator gets the rest of the 8 GB box. Detectors and experiments read only aggregates.
Raw `calls` is read only by audit queries and by the lead-state update.

---

## 4. The control loop

### 4.1 Per-step sequence

```
on start: reconcile()                                  # section 8.3

loop until sim finished:
  1. PLAN STEP      S = step_size(now)                 # 4.2
  2. ADVANCE        intents.execute(ADVANCE, from=now, minutes=S)   # never blind-retried
  3. INGEST         for chunk in GET /events/chunks?kind=events&after=cursor (ascending):
                       one tx: COPY facts, update agg_*, leads, campaigns,
                               mirror config_changed / approval_decided, chunks row
  4. SYNC           GET /sim/state (minute, credits) -> credit_ledger reconcile
                    for each workflow with a new config_changed: GET /workflows/{id}/config,
                      store the body if version == current (else body_source = unknown)
                    for each workflow with an open proposal: GET /approvals?workflow_id=
  5. LIFECYCLE      governance.lifecycle.tick(): apply visible approvals (deadline and
                    base checks first), resolve rejected / expired / stale / superseded,
                    return their still-wanted patches to the overlay
  6. DETECT         incidents.detect(): DID, prompt-change trigger, flag rate, drift,
                    post-apply guardrail and verification monitors, decay monitors
  7. DECIDE         incidents.respond() (may spend credits, priority over learning)
                    learning.update(): harm stops any time; ramp and winner looks only at
                    the workflow's planning point (once per routine release slot)
                    -> patches (desired state), each with a decision row
  8. GOVERN         planner.compose() + governance.release():
                      a) incident proposals for any workflow (immediate if window allows)
                      b) auto-apply operational patches where allowed, no approval in
                         flight, notice satisfied
                      c) routine approval release if this is the workflow's slot
  9. AUDIT          (not a separate step) every decision row and intent is committed
                    before its HTTP call; results committed after (section 8.2)
```

Order matters. Lifecycle runs before detection, so an approval that is already visible gets
applied before an incident fix is built on top of it. Incidents run before routine work, so a
routine release always goes out on a base that already includes the incident fix.

### 4.2 Advance step-size rule

**Why the step must be bounded.** A decision taken at minute `d` is processed at the first tick
start `T >= d`, and becomes visible only when an advance ends at or after `T + tick`. The
approval lapses after `d + W`, where `W = apply_window_hours x 60`. In the worst case
`d = T - tick + 1`, so the tick-aligned minutes at which we can still apply are
`T + tick, T + 2 tick, ..., T + floor((W - tick + 1)/tick) x tick - tick`. A poll grid of step
`S` is certain to hit one of them iff

```
S <= S_watch_max = tick * floor((W - tick + 1) / tick)       # W=120, tick=15 -> 105 min
```

**Rule.**

```
def step_size(now):
    tick = state.tick_minutes
    S = BASE_STEP                                  # 60, a constant multiple of tick
    if any open proposal could be decided before now + S:
        S = min(S, S_watch_max - tick)             # one tick of margin, 90 at public values
        if that proposal is incident class and its decision window is open
           (now >= created + latency_min):
            S = tick                               # fast-apply: cuts expected MTTR by ~30 min
    if any open incident has a chained remediation step pending:
        S = tick
    B = next_boundary(now)   # min over workflows of: dialling window open/close,
                             # approver open/close, daily batch time, planned release slot,
                             # notice expiry, apply_by - tick of any approved proposal,
                             # scenario end
    S = min(S, B - now)
    if nothing dials, nothing can be decided, and no release or notice is due before B:
        S = B - now          # overnight jump, e.g. 21:00 -> 08:00 is one advance
    return max(tick, floor_to_tick(S))
```

An overnight jump is safe because approvers decide only inside `approver_hours`. Before any
jump, step 5 has already applied every visible approval. With `BASE_STEP = 60`, worst-case
apply latency after a decision is `60 + 14 = 74` minutes, inside the 120-minute window with 46
minutes to spare. There are about 13 advances per simulated day (about 11 hourly steps plus the
night jump), more on incident days.

**Freshness (decision lag).** Data for minute `m` is used by minute `ceil_to_step(m) + ingest
wall time`, so at most 60 simulated minutes plus seconds of wall time. Incidents switch to
`tick` steps.

---

## 5. Learning loop (simple, complete)

### 5.1 Cohorts: one shared object with a versioned identity

```
canonical(where) = json.dumps({feat: sorted(vals) for feat, vals in sorted(where.items())},
                              separators=(",", ":"))
cohort_uid       = "coh_" + slug(where)[:24] + "_" + sha1(workflow_id + canonical)[:6]
cells(cohort)    = { c in 0..251 : features(c) satisfy where }        # AND across, OR within
```

- **Immutable.** A changed definition is a new uid. The old one stays in the registry for audit.
- **The same uid everywhere.** It is the `id` we put in the simulator config (so calls come back
  with `cohort_id = cohort_uid`), the key of experiment and detector rows, and the label in the
  supervisor view.
- **Effective membership** for config version v is computed by the one function `first_match
  (cohort list of v, cell)` and stored in `effective_membership`. Ingest checks that our computed
  cohort equals the simulator's `cohort_id` on every call. A mismatch raises a page alert,
  because it means our model of the config is wrong.
- Cohorts that supervisors create are registered with `created_by = supervisor`.
- **Segments** (for analysis only) are cohort objects of `kind = segment`: one per feature value
  whose share of connected calls in history is at least 10%. At most 7 + 3 + 4 + 3 = 17. They are
  pre-registered when the workflow is bootstrapped, which is what makes multiple-comparison
  control possible.
- **Size floor.** A config cohort is created only if its expected connected calls per day are at
  least `n_min_day`, where `n_min_day` lets a 50/50 test detect a 15% lift within 5 days:
  `n_per_arm = (2.8 + 0.84)^2 x 2pq / (0.15 e)^2`, which at e = 0.55 is 13.25 x 0.495 / 0.0068 =
  about 960 per arm, so `n_min_day = 2 x 960 / 5 = 385` connected/day. On nw_renewal (about 936
  connected/day) that allows at most 2 experiment-bearing cohorts, which is why experiments run
  at workflow level (5.3) and cohorts are used mainly for scheduling. At public_03 volumes (about
  13.5k connected/day) it allows up to 35, which the 20-cohort cap then bounds.

### 5.2 What is experimented on, and what is not

| Workflow primary | Levers we move | Why |
|---|---|---|
| engagement_rate | welcome_message, voice (experiments); caller_id and schedule (pickup is the guardrail, and these are operational) | Welcome and voice act after the call connects. |
| pickup_rate | caller_id, schedule only | A welcome message or voice is heard after pickup, so it cannot move the primary KPI but can hurt the engagement guardrail. That is negative expected value for the objective, so we do not experiment there. |
| any | prompt_version: incidents only | One prompt per workflow means no concurrent control. A +3% engagement effect at e = 0.5 needs `7.84 x 2 x 0.25 / 0.015^2 = 17,400` connected calls per arm before and after, about 15 days of ks_cancel each. A before/after test is also confounded by batch and DID-load cycles. |

### 5.3 Experiment design

- **Unit:** (workflow, cohort, dim). By default the cohort is `default`, which covers every lead
  because cohorts we create for scheduling inherit allocations from `default`.
- **Factorial:** one welcome experiment and one voice experiment run at the same time on the
  same traffic. The simulator samples the dims independently per dial, so each call contributes
  to both. **Stated assumption:** no welcome x voice interaction. We check it at the winner look
  with a 2x2 interaction z-test and report it, but do not act on it.
- **One challenger per dim at a time.** Challengers are queued in the API's option order (the
  order is deterministic, and we have no prior about which option is best). The LLM is never
  used for welcome or voice.
- **What counts as evidence:** only calls whose recorded `variant` and `config_version` show
  that the arm actually ran (Product memo). If an arm is rejected, it never ran and has no rows.
  On `next_campaign_only` workflows, the `config_version` on a call is the campaign's locked
  version, which is the version that actually ran.
- **Concurrency:** challenger and champion are compared only on calls dialled while both were
  live in the same config lineage. Randomisation is per dial, so this comparison has no hour or
  attempt confounding.

### 5.4 Allocation: the harm-limited ramp

Parameters, all in `controller/config.py` with reasons: `rho = 0.01` (worst-case expected harm
of one change, as a share of workflow engagement; it matches the size of change a supervisor
would notice), `L = 0.15` (largest single-lever engagement loss we treat as plausible),
ladder `{s_probe, 0.10, 0.25, 0.50}`, `HOLDOUT = 0.10`.

```
k              = number of arms in this change whose share increases
rho_arm        = rho / k
s_probe        = rho_arm / L                 # prior LCB is -L; k=2 -> 0.0333, k=1 -> 0.0667
LCB95, UCB95   = one-sided 95% bounds on the relative effect (challenger/champion - 1)

next_share(s) = max{ s' in ladder, s' > s : (s' - s) * max(0, -LCB95) <= rho_arm }
                (stay at s if no s' qualifies)
```

The same formula gives the first probe (when there is no data, LCB = -L) and every later ramp.
Any applied change therefore has a worst-case expected engagement loss of at most `rho`, at 95%
confidence once we have data. That is the number the approver sees in the dry run.

Exit rules, checked at every look:

| Rule | Condition | Action |
|---|---|---|
| Harm stop (any time, not just at looks) | `share x max(0, -LCB95) > rho_arm` at the current share, i.e. it is plausibly costing more than budget | Drop the arm (risk-reducing change, priority class `rollback`). |
| Futility | `UCB95 < delta_min = +3%` | Drop. It cannot be a worthwhile winner. |
| Stalled probe | still at `s_probe` after `T_probe = 3` days | Retire as inconclusive, so the next challenger can start. |
| Winner | at share 0.5, `z >= z_star` and guardrail check passes (5.6) | Promote to `1 - HOLDOUT`. |
| End of scenario freeze | no new probes in the last 3 days; no ramps in the last 1.5 days | Promotions, rollbacks and incidents continue until the end. |

**What the ramp costs at public volumes** (nw_renewal, about 936 connected/day, e about 0.55,
pq about 0.2475, two dims probing so k = 2 and `s_probe = 3.3%`, about 31 connected/day on the
challenger):

- At the probe, `se_rel` is about `sqrt(0.2475 / (31 d)) / 0.55 = 0.1625 / sqrt(d)`.
- The ramp from 3.3% to 10% needs `LCB >= -0.005/0.0667 = -7.5%`.

| True effect | Probe to 10% | 10% to 25% to 50% | Winner at 50/50 (z >= 2.8) | Promoted on about |
|---|---|---|---|---|
| +15% | d >= 1.4 days | next slot each (the gate needs LCB >= -3.3% and -2%, already true) | z = 0.15/(0.0587/sqrt(d)) = 2.56 sqrt(d), about 1.2 days | D5 |
| +10% | d >= 2.3 days | 1 to 2 slots | 1.70 sqrt(d), about 2.7 days | D7 to D8 |
| 0 | never (needs 12.7 days) | | | retired at D3 |
| -12% | never | | | futility drop after about 3 days (UCB = -12 + 1.645 x 16.25/sqrt(3) = +3.4%, just failing; dropped at the D3 to D4 look) |

At the README envelope (13.5k connected/day per workflow) the probe arm gets 450 calls a day, and
a +10% challenger passes every gate within one day. **The harm limit is cheap at scale and slow
at dev volume.** We accept that trade-off openly because of Compliance's "even briefly" (memo
02, item 4).

### 5.5 Winner rule: computed power and false-winner rate

- **Test:** a two-proportion z-test on engagement (engaged / connected), challenger against
  champion, on all concurrent data from the start of the arm. One-sided.
- **Looks:** only at the workflow's planning points (at most one a day per experiment), and at
  most K = 10 looks.
- **Multiple comparisons:** two dims are tested at once, so Bonferroni gives one-sided
  `alpha = 0.025 / 2 = 0.0125` per comparison. The Pocock constant for K = 10 at one-sided 0.0125
  (equivalent to two-sided 0.025) interpolates to **z_star = 2.8**. The tabulated values are
  2.555 at two-sided 0.05 and 3.001 at two-sided 0.01.
- **False-winner rate:** under the null, at most 1.25% per comparison, at most 2.5% per workflow
  per concurrent pair, and under 5% per workflow over a 10-day run with at most two pairs in
  sequence. Across 3 workflows, expected false winners are at most 0.15 per scenario.
- **Power at 50/50**, nw_renewal: `se_rel(d) = sqrt(0.2475 x 2 / (468 d)) / 0.55 =
  0.0587/sqrt(d)`. The minimum detectable effect at 80% power is `(2.8 + 0.84) x 0.0587 /
  sqrt(d)`, which is **15.1% at 2 days, 12.3% at 3, 10.7% at 4, 9.6% at 5.** For ks_cancel at
  about 864 connected/day: 15.7%, 12.8%, 11.1%, 9.9%. For a public_03 workflow: 5.7% after 1 day.
- **Segment screening (partial):** at each winner look we also compute the effect in each of the
  pre-registered segments. A segment with `z <= -3.0` (one-sided p = 0.00135; times 17 segments
  is a 2.3% family-wise false flag per look) becomes a **carve-out**: the promotion proposal
  creates that segment as a config cohort pinned to the champion, so a winner overall never
  harms a cohort we can see. A segment with `z >= 3.0` while the overall test fails is listed as
  a hypothesis in the supervisor view. A confirmatory cohort experiment on fresh data is the cut
  part (cost about 1 day).

### 5.6 Guardrail

- **Definition (an assumption we state):** `*_max_drop: 0.02` is an **absolute** drop of 2
  points in the guardrail rate. A relative reading (0.56 points of pickup) could not be measured
  at these volumes.
- **Before promotion:** require `LCB95(guardrail_challenger - guardrail_champion) >= -0.02` on
  concurrent data. Pickup per arm at 50/50 on nw_renewal: 1,800 dials/day/arm, so `se = sqrt(0.26
  x 0.74 x 2 / 1800) = 0.0146` per day; LCB is -0.024 after 1 day and -0.017 after 2, so it
  passes on day 2 if there is no true effect.
- **After every applied change** (any dim): a post-change monitor compares the guardrail over the
  next 24h and 72h with the 72h before, matched by hour of day. It alerts if the point estimate
  is at most -0.02 and z is at most -2.33. For a randomised arm, the comparison is per arm. Any
  alert opens a `guardrail_breach` decision that proposes the rollback of that change.

### 5.7 Promotion, holdout, decay and rollback

```
PROMOTE:   challenger -> 1 - HOLDOUT (0.90), old champion -> HOLDOUT (0.10)
MONITOR:   every planning point, trailing 48h window, concurrent promoted vs holdout
ROLLBACK if either:
  R1  z(promoted - holdout) <= -2.33                    # promoted is now clearly worse
  R2  z(lift_now - lift_at_promotion) <= -2.33  AND  lift_now point estimate <= 0
ROLLBACK = holdout option -> 1.0, decayed option -> 0 (never re-explored in this scenario)
```

R2 never rolls back while the promoted variant is still estimated better than the holdout.
Rolling back at decay onset, when the variant is still at full strength, would itself be a
harmful change.

**Power.** On nw_renewal the holdout gets about 94 connected/day, so in 48h `se_rel =
sqrt(0.2475 x (1/188 + 1/1684)) / 0.55 = 6.9%`. R1 fires at 50% for a promoted variant 16%
worse than the holdout. R2 detects a fall of 22 points of lift (for example +15% to -7%) at
`z = 0.22 / sqrt(0.069^2 + 0.04^2) = 2.75`, so it fires within about 2 days of the lift crossing
zero. A 10% holdout costs 10% of the promoted lift (1.5% of engagement for a +15% winner). We
pay that for the ability to see decay at all.

**Call time (C4, partial).** From history, per age-band segment, pickup by hour on **first
attempts only**. Retries over-represent hard-to-reach leads in later hours. About 14 days x
3,600 dials gives about 1,000 dials per (age band, hour) cell, so `se` is about 0.014 at p = 0.3
(5% relative). We create age-band cohorts only if a chi-square test of each band's hour curve
against the pooled curve has p < 0.001. Then:

```
w(c, h) = clip( (p_hat(c,h) / mean_h p_hat(c,h)) ^ 2 , 0.2, 5.0 )   for every h in window
```

Every in-window hour gets a positive weight. A missing hour would get weight 0 and waste the
capacity the business owns. The first schedule ships with the cohort definitions in one
approval (cohorts are never auto-applicable). Later tweaks auto-apply where policy allows.
Evaluation is before/after with hour x attempt stratification. It is weaker than a randomised
test, and we say so. Rollback is the guardrail monitor.

**Why per-person timing is cut.** A morning-archetype person picks up about 0.48 at 10:00 and
0.18 at 19:00. After 3 attempts with 1 connect, the likelihood ratio between morning and evening
archetypes is at most about 2.7, too weak to justify the x0.25 penalty in every other hour.

### 5.8 Caller ID

- **Status per DID:** `unknown -> probing -> healthy | quarantined`. History gives `healthy` or
  `quarantined` for DIDs in the default config. DIDs that history never used start `unknown`.
- **Probe:** each unknown DID gets 5% (at most 2 unknowns per change, 10% in total), taken from
  the most loaded healthy DID. Worst case disclosed in the dry run: a probe onto a DID at
  relative health r costs `0.05 x (1 - r)` of expected caller health, which is -1.8% at r = 0.64
  and -3.5% at r = 0.3.
- **Classification:** ratio `R = pickup(DID) / pickup(other DIDs)` on concurrent dials in the
  same hours. `healthy` if `LCB95(R) >= 0.85`. `quarantined` if `UCB95(R) < 0.80`. At 5% of
  3,600 dials/day (180 dials) and pickup 0.26 vs 0.17, `z = 3.2` after one day.
- **Target:** equal shares across healthy DIDs. This minimises the maximum per-DID load under any
  convex load penalty. That is an assumption about the mechanism, not knowledge of it.
- **Moving to target:** routine moves are limited by a **velocity rule**: the cumulative TV shift
  per (cohort, dim) over a rolling 24h is at most `max_shift`, so we never chain auto-applies to
  get around the cap. Incident moves are exempt (6.6).
- **Detector (incident):** hourly, over the trailing 2h. Alert if `z <= -3.5` and the drop in R
  is at least 20%. The z threshold gives a per-evaluation false-positive rate of 0.00023. At 12
  evaluations x 3 DIDs that bounds false alerts at 0.008 per workflow-day, about 0.5 over all of
  public_03. The 20% practical threshold excludes the natural load imbalance (a DID at 0.5 share
  has about 10% lower health than one at 0.25).

---

## 6. Governance client

### 6.1 Proposal state machine

```
                         planner emits patches
                                  |
                                  v
                              +--------+   hold / no slot / outside window / lock busy
                              | QUEUED |<-------------------------------------------+
                              +--------+                                            |
                 approval path   |        | auto path (operational, allowed,       |
                                 v        v  no approval in flight, notice done)    |
                         +------------+  +----------------+                         |
                         | SUBMITTING |  | AUTO_APPLYING  |--403--> AUTO_REFUSED    |
                         +------------+  +----------------+         (bug, page)     |
              409 stale  |    |  2xx          |     |   409 stale                   |
              +----------+    v               v     +----------> STALE ---rebase----+
              |         +---------+      AUTO_APPLIED --> MONITORING                |
              |         | PENDING |-----------------------------+                   |
              |         +---------+                             |                   |
              |  rejected |  | approved   | expired             | higher priority   |
              |           v  v            v                     v proposal sent     |
              |    REJECTED  APPROVED    EXPIRED           SUPERSEDED               |
              |      |        |   |         |              (never applied, even if  |
              |      |        |   |         |               approved later)         |
              |      |   base  |  now > apply_by - margin                           |
              |      | changed |   v                                                |
              |      |    |   |  LAPSED_LOCAL (we never call apply, so no 410)      |
              |      |    v   v                                                     |
              |      |  STALE  APPLYING --2xx--> APPLIED --> MONITORING             |
              |      |    |        |                                                |
              |      |    |        +--409 not_approved--> reconcile (already applied)|
              +------+----+---- re-queue with FRESH data and a NEW proposal_id -----+
```

Rules attached to the transitions:

| Transition | Rule |
|---|---|
| QUEUED -> SUBMITTING | Every check passes locally first: the window is open (6.4), the fatigue count is below the routine budget (6.3), no proposal is in flight for the workflow (or this one is higher priority, in which case the other becomes SUPERSEDED), there is no hold, and `base_version == mirrored current version`, confirmed by a fresh `GET /workflows/{id}/config`. |
| PENDING -> APPROVED | Seen through the `approval_decided` event or a poll. Store `apply_by`. |
| APPROVED -> APPLYING | Only if `now <= apply_by - tick` and `base_version == current version` (fresh GET). Otherwise LAPSED_LOCAL or STALE. **Neither calls the API, so the simulator logs neither `approval_lapsed` nor `apply_conflict`.** |
| APPROVED, decided more than 20h after submit | Re-run the dry run before applying. If the evidence no longer supports the change, SUPERSEDE it. |
| REJECTED | Classify the reasons. "Judgement call" means re-queue once at the next routine slot with fresh data; a second rejection of the same patch set drops it and shows "needs discussion" in the supervisor view. Fatigue, evidence or format reasons mean our local check is wrong: page alert, and the patch is not re-proposed until the alert is acknowledged. |
| EXPIRED | Treated as a rejection (Product memo). Re-queue with fresh data, a new id and a new base. Never resubmit the stale body. |
| STALE | Rebase (6.8). The non-conflicting patches go back to QUEUED. |
| SUPERSEDED | Its approval, if it is ever approved, is never applied. It is recorded as "withdrawn by controller" and its approval slot counts as used. |

**One change in flight per workflow.** A workflow lock is held from SUBMITTING until the
proposal reaches a terminal state. Auto-applies need the lock too. An auto-apply while an
approval is pending would make our own approval stale. The lock is a row in `proposals`
(`state in (SUBMITTING, PENDING, APPROVED, APPLYING)`), so it survives crashes.

**Priority classes:** `incident > rollback (harm stop, decay, guardrail) > promotion > routine
(probe, ramp, schedule, rotation)`. Only `incident` and `rollback` may supersede an in-flight
proposal.

**Proposal ids** are deterministic, `{world6}-{workflow}-{D}T{HHMM}-{class}-{seq}`. A crashed and
restarted run therefore uses the same ids, and `duplicate_proposal_id` can be used for
reconciliation.

### 6.2 Batching: release trains

At a workflow's routine slot, the planner collects every QUEUED patch whose evidence gate has
passed and composes **one** approval. The `change_summary` lists one line per item, in plain
language ("Voice: try V2 on 3% of calls (new test)"; "Caller ID: move 10% from D1 to D3 and
D4"). This is Kestrel's "one request with five things in it". Operational patches that may be
auto-applied are applied **in the same planning step, immediately before** the approval is
submitted. The approval then carries the new `base_version`, and the single-change lock is never
violated. Between releases, operational patches wait unless an incident needs them.

### 6.3 Fatigue caps, voluntary caps and slot reservation

```
C_policy   = governance.max_requests_per_24h                       (API)
C_vol      = directive.max_requests_per_day (if any)                (directives)
C_eff      = min(C_policy, C_vol)
reserve    = 1 if C_eff >= 2 else 0
routine    = C_eff - reserve
used(now)  = count(our approval requests for wf with created_minute > now - 1440)  # same strict
                                                                                    # inequality as the sim,
                                                                                    # every status counts
may_submit(class) = used(now) + 1 <= (C_eff if class in {incident, rollback} else routine)
```

Incidents may also use policy headroom above `C_vol` (never above `C_policy`), and the
supervisor view says so explicitly ("3rd request today, above your 2/day preference, because of
incident INC-4"). A safety fix outranks a preference. A cap is never exceeded on purpose,
because an over-cap request still burns a slot and is certain to be rejected.

**Directives** (`controller/directives.yaml`, client agreements from the memos, not simulator
data). Effective policy is always the **stricter** of the API policy and the directive:

| Tenant scope | Directive | Source |
|---|---|---|
| `northwind_life` (all workflows) | `customer_facing_auto_apply: false`; weekly operational digest | memo 02 items 1 and 5 |
| `kestrel_finance` (all workflows, the conservative reading) | `max_requests_per_day: 2`, `earliest_submit: 11:00`, `notice_minutes: 60` before any non-incident auto-apply | memo 04 |
| `*` (unknown tenants in hidden scenarios) | `customer_facing_auto_apply: false`, submissions only inside approver hours, reserve 1 slot | design default |

Customer-facing dims are `welcome_message`, `voice` and `prompt_version`. `cohorts` never
auto-applies under any public policy anyway. Caller ID and schedule are operational, as the
Northwind memo itself describes them.

### 6.4 Timing against approver hours, latency, TTL and the apply window

Approval outcome, decision time and expiry are all fixed when we submit (`world.py:461-494`).
Decision time is `next_working(t + latency)`. Expiry happens if that is later than
`t + ttl`. So the window is computed per workflow from API parameters:

```
open, close   = approver_hours (h)          lat_min, lat_max = approver_latency_minutes
earliest      = max(open, directive.earliest_submit)
expiry_safe(t)   :=  next_working(t + lat_max) <= t + 60 * ttl_hours       # must hold
same_day(t)      :=  t + lat_max < close                                    # preferred
latest_same_day  =  floor_to_tick(close - lat_max - 1)
submit window    =  [earliest, latest_same_day]  if non-empty
                    else {earliest} if expiry_safe(earliest)   # decision almost surely same day
                    else none: never submit routine (alert: policy makes approvals impossible)
```

Sanity check with the public policies (values read from the API at runtime):

| Workflow | C_eff / routine | Submit window | Notes |
|---|---|---|---|
| nw_renewal (10-19, lat 30-240, TTL 24) | 3 / 2 | 10:00 to 14:45 | 15:00 + 240 = 19:00 rolls to the next morning. TTL 24 means it never expires. |
| nw_kyc (10-18, lat 60-480, TTL 24) | 2 / 1 | 10:00 only | 10:00 + 480 = 18:00 rolls over with probability 1/421. Same-day decision means the change is live for the next 08:00 campaign under `next_campaign_only`. |
| ks_cancel (10-19, lat 20-120, TTL 12, Kestrel 2/day after 11:00) | 2 / 1 | 11:00 to 16:45 | 17:00 + 120 = 19:00 rolls to 10:00, past 17:00 + 12h, so it **expires**. 16:45 is the last safe submit. |

**Routine slots.** Slots are placed at fixed times of day, so the rolling-24h count is
predictable. Slot 1 is `earliest`. Slot 2 (if `routine >= 2`) is the first planning point after
slot 1's proposal is terminal, no later than `latest_same_day`. Using the same minutes every day
keeps `used()` exact: yesterday's 10:00 request leaves the window at exactly 10:00, because the
simulator's inequality is strict.

**Apply deadline.** Covered by the step rule (4.2). `apply_by - tick` is also a boundary in
`next_boundary`, so we never step past it.

**`expires_in_hours`** is left at the policy TTL. A shorter TTL would turn "decided tomorrow at
10:00" into a logged expiry. Freshness is instead enforced by the more-than-20h re-check rule.

**Kestrel notice.** A non-incident auto-apply on a Kestrel workflow is posted to the supervisor
view's "Upcoming" list with an ETA. It applies `notice_minutes` later, unless a hold was set
through `POST /supervisor/holds`. Approvals need no notice, because the request itself is the
notice.

### 6.5 Auto-apply policy

`policy_check.py` re-implements the simulator's contract: normalisation (weights normalised,
zero weights dropped), `changed_dims` (cohort list compared in order, per-dim distributions
compared with the `default` fallback, any `schedule` difference), and `max_shift` (unweighted TV
per cohort per agent dim, over `default` plus old and new cohort ids). It is unit-tested and
differentially tested against a live simulator in tests (10.1). A proposal is auto-applied only
if all of these hold:

1. `auto_apply.enabled`, and every changed dim is in `allowed_dims`.
2. `max_shift(current, proposed) <= max_shift - 1e-6`.
3. The directive does not forbid it (customer-facing dims are never auto-applied under the
   defaults).
4. The velocity rule (5.8) holds, unless the change is incident class and risk-reducing.
5. No approval is in flight for the workflow (the lock).
6. There is no hold, and the notice period (if any) has elapsed.

Anything else goes to the approval path. We **never probe auto-apply by trial and error**,
because every 403 is logged by the simulator and graded. A 403 means our checker is wrong: page,
and fall back to the approval path.

**When we voluntarily do not auto-apply even though policy allows it:**

- Customer-facing dims for Northwind. For example, public_03 `nort_cancel_2` permits welcome and
  voice auto-apply, but Compliance requires a named human approval.
- Customer-facing dims for unknown tenants (default directive).
- Any routine change that would push the 24h cumulative shift past `max_shift`.
- Any change while an approval is in flight, other than an incident.

### 6.6 Incidents and the in-flight rule

- **DID incident on a workflow that allows caller_id auto-apply:** if an approval is in flight
  and its worst-case remaining time to decision plus apply is more than 60 minutes, it is
  SUPERSEDED, then the risk-reducing chain runs: an auto-apply of at most `max_shift` per tick,
  moving share off the quarantined DID onto healthy DIDs only. Every step is logged and shown.
  This is the **only** time we chain auto-applies, and we state it as an assumption.
- **DID incident on an approval-only workflow (KYC):** an incident-class approval, using the
  reserved slot, at the next safe submit time. Under `next_campaign_only` existing campaigns stay
  on the flagged DID until their cadence ends. Cadence is owned by the business, and we say so in
  the view.
- **Prompt incident:** always an approval with evidence. It supersedes any routine approval in
  flight.

### 6.7 Impact score, affected leads, dry run

**Affected leads** (integer, as the API requires):

```
changed_cells = cells whose effective (cohort -> dim distribution) or schedule table or prompt
                differs between current and proposed (prompt and default-schedule: all cells)
if dim in allocations and mid_flight == next_campaign_only:
    affected = expected leads of the next campaign in changed_cells
             = daily_batch.size * share_hist(changed_cells)
else:
    affected = active leads now in changed_cells (from `leads`, state = active)
             + daily_batch.size * share_hist(changed_cells)       # next batch, 24h horizon
```

The dry run also reports `leads_hearing_a_different_variant_24h = sum over cells of leads_cell x
TV_cell`, because a 3% probe touches every lead in scope but changes what only 3% of them hear.

**Dry run** (a JSON object, built from our own recorded calls):

```jsonc
{
  "method": "stratified replay, last 7 days of recorded calls, cells weighted by next-24h dial mix",
  "window": {"from": "D2T00:00", "to": "D8T00:00", "dials": 25140, "connected": 6555},
  "primary":   {"kpi": "engagement_rate", "current": 0.553, "proposed": 0.557, "ci90": [0.551, 0.563]},
  "guardrail": {"kpi": "pickup_rate", "current": 0.262, "proposed": 0.262, "max_drop_abs": 0.02,
                "p_breach": 0.00},
  "components": [
    {"patch": "pat_0041", "dim": "voice", "cohort": "default", "option": "V2", "share": "0.033->0.10",
     "evidence_connected": 88, "rel_effect": 0.14, "ci90": [-0.06, 0.34], "worst_case_rel_change": -0.004},
    {"patch": "pat_0042", "dim": "welcome_message", "option": "W3", "share": "0->0.033",
     "evidence_connected": 0, "unobserved": true, "prior_range": [-0.15, 0.15], "worst_case_rel_change": -0.005}
  ],
  "worst_case_rel_change_primary": -0.009,
  "leads_hearing_a_different_variant_24h": 190,
  "assumptions": ["no welcome x voice interaction", "hour of day does not change engagement",
                  "DID health is a function of recent load"]
}
```

The estimator for an allocation change is
`Delta = sum over cells m_c x sum over v (pi_new(v|c) - pi_old(v|c)) x e_hat_rel(v)`, where
`m_c` is the next-24h connected mix and `e_hat_rel` comes from the concurrent experiment
estimate (point and CI). An unobserved option has point 0 and range `[-L, +L]`. Caller ID uses
the per-DID pickup ratio, with an unknown DID ranging over `[0.3, 1.1]`. Schedule replays
per-(cohort, hour) pickup, with dial shares from the selection approximation
`share(c|h) ~ elig(c,h) w(c,h) / sum over c of elig(c,h) w(c,h)`.

**Impact score** (in [0, 1], with units, so ranking by it means something):

```
impact_score = clip( Q20( expected relative change in the workflow's PRIMARY KPI over 7 days ), 0, 1 )
```

`Q20` is the 20th percentile of the dry-run interval (a conservative estimate). 0.042 means "we
expect at least about +4.2% on this workflow's primary KPI, with 80% confidence". A pure probe
scores about 0, which is honest: it buys information, not impact. For a hallucination incident,
the score is the measured engagement deficit of flagged calls, `(flag_rate_now -
flag_rate_baseline) x (e_unflagged - e_flagged) / e_now`, measured on our own calls. It does
**not** use the detector's hidden error rates. The supervisor queue sorts by priority class
first, then impact score. We push back on Product here: an incident with a modest score must
still come first.

**Rationale** is a deterministic template filled from the decision rows: rule id, gate values,
sample sizes, and links to `experiment_looks` and `validation_runs`. There is no LLM text.

### 6.8 Supervisor edits: rebase, never revert

**Overlay model.** The desired config is `live config + patches`. A patch is owned by one module
and covers one **path**: `("alloc", cohort_uid, dim)`, `("prompt",)`, `("hour_weights",
cohort_uid)` or `("cohorts",)`. `compose()` deep-copies the live config and applies the patches
in a fixed order (cohorts, allocations, schedule, prompt). It then normalises and validates
locally.

**On `config_changed(actor = supervisor, version v)`:**

1. Fetch the body of v. Compute `sup_paths = paths(diff(snapshot(v-1), body(v)))`. If v-1's body
   is unknown (two supervisor edits in one advance), diff against the last known body. Every
   differing path then counts as supervisor-owned (the conservative choice).
2. Every patch whose path is in `sup_paths`, or that depends on a cohort the supervisor removed,
   becomes `invalidated_by_supervisor`. Its owner is notified. An experiment on that path goes to
   `INTERRUPTED_BY_SUPERVISOR`: its data is kept and labelled by version, and the supervisor's
   choice becomes the new champion.
3. Every proposal in PENDING or APPROVED with `base_version < v` becomes STALE and is never
   applied. Its remaining patches go back to QUEUED, rebased on v at the next slot.
4. **Cooldown:** for `24h` no routine patch may touch a supervisor-owned path. After that, a
   proposal touching it must say so in its summary ("changes the voice your supervisor set on D5
   at 15:00"). The exception is a prompt incident, which is exactly the "the supervisor shipped
   an unsafe prompt" case, and its summary says it plainly.

Public_02 check: on D5 at 15:00 the Northwind supervisor sets the default voice to V2. Our voice
experiment on `("alloc", default, voice)` is interrupted, nothing is reverted, the other patches
are re-proposed at D6 10:00, and after 24h a new voice test may start with **V2 as champion**.
V2 is in fact good for `ta`, so blindly reverting would have been harmful.

### 6.9 Credits and request slots reserved for incidents

- **Request slots:** 1 per workflow per rolling 24h (6.3).
- **Credits:** let `C0 = credits_remaining` at start. Floor = `max(0.25 C0, 2 x 20)`, spent only
  on confirmed incidents. Per incident there is a cap of 20 credits (worst case: 4 + 4 + 3 + 2 x 4
  = 19). Each tenant's envelope is `0.75 C0 x workflows_tenant / workflows_total`. A tenant can
  borrow from the floor only for an incident whose detector has already fired (C15, partial).
- Results are cached per (workflow, prompt): a prompt validated once is never re-validated for the
  same purpose.

---

## 7. Incidents

### 7.1 Detectors

| Detector | Signal | Threshold | Expected false positives | Cost |
|---|---|---|---|---|
| **Prompt-change trigger** | `config_changed` with `prompt_version` in `changed_dims`, any actor other than us | Run `POST /validation/runs n=400` on the new prompt. Incident if `pass_rate < min_validation_pass_rate`, i.e. at least 13 failures out of 400 at 0.97. | A prompt with true failure rate 0.0104 has `P(X >= 13)`, Poisson(4.16), about **0.04%**. | 4 credits per prompt change, cached. |
| **Flag-rate detector** (free, backup) | Flags on the trailing N connected calls, `N = max(400, 1h of connected)`; baseline `p0` = upper 90% bound of the history flag rate | k = smallest count with binomial `P(X >= k | N, p0) <= 0.001`. At p0 = 0.0147, N = 400: **k = 16**. | 0.04% per evaluation. Windows overlap 75% at about 100 connected/h, so about 3 effective tests a day, about 0.1% per workflow-day. A firing only buys a 4-credit validation. | free |
| **DID detector** | 5.8 | `z <= -3.5` and at least a 20% relative drop | at most 0.008 per workflow-day | free |
| **Drift** (alert only) | Pickup and engagement over the trailing 24h vs the same hours of the previous 72h | `z <= -3` and at least a 10% relative drop | 0.13% per evaluation | free |

Power against a bad prompt (sanity values only: P2 has expected validation failure rate 0.0545
and expected flag rate 0.0535). Validation at n = 400 has a mean of 21.8 failures, so `P(<= 12)
= Phi((12.5 - 21.8)/4.54) = 2%`, i.e. **98% detection** in one run. The flag detector at N =
400 has a mean of 21.4, so `P(>= 16)` is about **90%**, reached about 2 to 3 hours after the
edit at 100 connected/h. **No change is ever triggered by one call:** both signals are
population tests.

### 7.2 Fix selection

```
candidates =
  1. last_known_good: the most recent prompt that was live on this workflow for >= 1 day with flag
     rate not above baseline (we have its real-traffic engagement)
  2. only if (1) does not exist or fails validation: POST /llm/suggest-fixes (3 credits),
     candidates in returned order, excluding any prompt with a failed run on record
for each candidate (at most 3): validate n=400 once (4 credits; never re-run a failed candidate)
  accept if pass_rate >= gate          for last_known_good (it has a live safety record)
  accept if failures <= 6 (pass >= 0.985) for never-live candidates (stricter internal bar)
first accepted candidate -> incident-class approval, evidence.validation_run_id, all runs listed
```

The stricter bar for prompts that have never been live rejects a "persuasive" prompt whose
expected failure rate is 0.0275 with probability `1 - P(Poisson(11) <= 6) = 92%`. It does this
by measurement, without reading summaries. The approver would accept one passing run at 0.97,
and P4 passes that about 69% of the time. We do not exploit that. A prompt that is safe but
disengaging (the "word for word compliance script", Kestrel memo) cannot be caught by
validation. It is caught by post-apply verification, and it is the reason the known-good prompt
comes first.

### 7.3 Verification after apply

| Check | Window | Pass | Fail action |
|---|---|---|---|
| Flags back to baseline | next 400 connected calls | flags < k (same test) | reopen; next candidate |
| Engagement not worse than before the incident | next 24h vs the 72h before the incident, matched by hour | `LCB95(rel) >= -5%` | rollback-class proposal to the next candidate; alert "fix underperforming" |
| DID fix | next 2h | workflow pickup back within 10% of the pre-incident 24h at the same hours | escalate |

### 7.4 Predicted public_02 timeline (sanity check against public truth, not tuning)

| Sim time | Controller action | API calls / credits |
|---|---|---|
| D0 00:00 | Ingest 14 history days, bootstrap baselines, register segments, DID status from history (renewal D1, D2 healthy; D3, D4 unknown; kyc D1 healthy, D2, D3 unknown; cancel D1, D2 healthy, D3 unknown). | 0 credits |
| D0 09:00 | nw_renewal auto-apply: probe D3, D4 at 5% each (TV 0.10 <= 0.5). ks_cancel: probe D3 at 5% posted with 60-min notice at 08:00, auto-applied 09:00 (TV 0.05 <= 0.2). | 2 PUTs |
| D0 10:00 | nw_renewal approval #1: age-band cohorts (if the chi-square test passes) + hour weights + W2 probe 3.3% + V2 probe 3.3%. nw_kyc approval #1: cohorts + hour weights + D2, D3 probes 5% each (no welcome or voice: pickup primary). | 2 approvals |
| D0 11:00 | ks_cancel approval #1: cohorts + hour weights + W2 and V2 probes. | 1 approval |
| D1 09:00 | renewal D3, D4 healthy, rebalance to 0.25 each (TV 0.40, one auto step). cancel D3 healthy, step toward 1/3 (TV capped at 0.2 by velocity). | auto |
| D2 09:00 | cancel reaches 1/3 each. | auto |
| **D2 10:00** | D2 flagged (no event). | |
| D2 11:00 | DID detector on hour 10: about 133 dials on D2, `z` about -4.7. Incident opened. Chain: 11:00 D2 0.333 -> 0.133, 11:15 -> 0 (risk-reducing, at most 0.2 per tick). Routine release deferred to 11:30 on the new base. | 2 PUTs |
| D2 summary | Predicted flagged-DID share for D2: (1 h x 0.333 + 1 h x 0.333 + 0.25 h x 0.133) / 12 h = **0.058**; D3 onward: **0.0** | |
| **D3 11:30** | Kestrel supervisor ships P2. | |
| D3 12:00 | `config_changed` seen. Validation of P2, n = 400: fails (98%). Incident. Candidate P1 (last known good) validated n = 400: passes (99.96%). Incident approval submitted. The 11:00 routine approval, if still pending, is SUPERSEDED. The request count is 2 in 24h, within Anita's 2. | 8 credits |
| D3 12:20 to 14:00 | Decision (latency 20 to 120); approved with p = 0.95. Fast-apply steps of 15 min. | |
| D3 about 13:30 | Apply. **Predicted MTTR: 75 to 165 min, mean about 123.** With the 5% rejection risk and one retry, expected about 130 min. | 1 apply |
| D3 to D4 | Verification: flags below k within about 4h, engagement within 5% of pre-incident. Incident closed. | 0 |
| **D5 15:00** | Northwind supervisor sets default voice to V2. Voice experiment interrupted; any renewal approval pending since 14:30 becomes STALE and is not applied (no `apply_conflict`). Rebased release at D6 10:00 without the voice path. New voice test with V2 as champion no earlier than D6 15:00. | 0 |
| D7 onward | Freeze rules start at D7 (no new probes) and D8.5 (no ramps). | |

Expected governance numbers for public_02: about 2 approvals per workflow-day at most, 0
auto-apply 403s, 0 apply conflicts, 0 lapses, 0 expiries, peak requests per 24h at most C_eff
everywhere. Credits: about 8 to 16 out of 200.

---

## 8. Reliability

### 8.1 Idempotency keys

| Call | Key | Retry semantics |
|---|---|---|
| `POST /sim/advance` | `advance:{from_minute}` | **Never retried blindly.** On timeout, `GET /sim/state`. The simulator lock makes that call wait until the advance finishes, then compare `sim_minute`. |
| `POST /approvals` | `proposal_id` (deterministic) | Retry with the same id. A `409 duplicate_proposal_id` means it was created, so look it up in `GET /approvals?workflow_id=`. The id is consumed only when the approval record is created, so a duplicate always means a record exists. |
| `PUT /workflows/{id}/config` | `proposal_id` | Consumed only on success. Retry with the same id. A 409 duplicate means it was applied: confirm via `config/history`. A 403 is never retried. |
| `POST /workflows/{id}/apply` | `approval_id` | Retry. `409 not_approved` with status `applied` means it was done before. It cannot double-apply. |
| `POST /validation/runs`, `/llm/suggest-fixes` | `intent_id` | Not idempotent and no lookup API. See 8.3. |
| Chunk ingest | `chunk_no` | One transaction per chunk, with `chunks.status = done` in the same transaction. Re-ingesting is a no-op. |

### 8.2 Write-ahead intent protocol

```
def execute(kind, key, payload, decision):
    with tx:                                           # T1
        insert decision row (inputs, rule, outcome = 'intended')
        insert intent(kind, key, payload, status='pending', decision_id)
    try:
        result = http(kind, payload)                   # side effect
    except Timeout / ConnectionError:
        return reconcile_one(intent)                   # never blind retry
    with tx:                                           # T2
        update intent set status='done', result=...
        apply domain transition (proposal state, experiment stage, incident, credit ledger)
        update decision outcome
```

Every decision is in the store **before** the action (constraint 6). A crash between T1 and the
HTTP call, between the call and T2, or inside T2, leaves a `pending` intent that is resolved
deterministically on restart.

### 8.3 Reconciliation on restart

| Pending intent kind | Lookup | Resolution |
|---|---|---|
| advance(from, minutes) | `GET /sim/state` | If `sim_minute == from`, it never ran: mark aborted, and the loop re-plans (the same step, since planning is deterministic). If `sim_minute == min(from + minutes, end)`, mark done and ingest new chunks. Anything else is impossible: page. |
| approval_request(pid) | `GET /approvals?workflow_id=` matched by `proposal_id` | If found, store `approval_id` and move to PENDING / APPROVED / ... as listed. If not found, it was never created: re-check every SUBMITTING precondition (window, fatigue, base, lock). If they all still hold, resend **the same pid and payload**; otherwise abort and re-plan. |
| auto_apply(pid) | `GET /workflows/{id}/config/history` matched by `proposal_id` | If found, it was applied: store the version and body (ours). If not, re-check and resend or abort. |
| apply(aid) | `GET /approvals/{aid}` | If `applied`, done (version from config history by `approval_id`). If `approved` and now <= apply_by - tick and base unchanged, re-apply. Otherwise LAPSED_LOCAL or STALE. |
| validation(n, wf, prompt) | `credits_remaining` vs ledger | If credits fell by the cost, the run happened but its id is lost: record `lost_validation_run`, and re-run once if the incident still needs evidence. The lost result was never seen, so this is not cherry-picking. It is listed in the evidence. If credits did not fall, re-run. |
| suggest_fixes | credits delta | Same as validation. |

Then the full sweep: every proposal in a non-terminal state is compared with `GET /approvals` and
`config/history`. Every `config/history` version newer than our mirror is ingested (its
`config_changed` event will also arrive through chunks). Any `applied` approval or
controller-actor version we have no intent for is a page alert ("unknown writer").

### 8.4 Simulator error handling

| Response | Handling |
|---|---|
| 2xx | normal |
| 400 bad_minutes | bug (we read the tick from the API): page, stop advancing |
| 402 out_of_credits | incident module degraded: no validation means no prompt fix can be proposed. Page. The DID path is unaffected. |
| 403 auto_apply_not_permitted | checker bug: page, re-route to the approval path, never retry |
| 404 | reconcile (unknown approval or chunk) |
| 409 stale_base_version | rebase (6.8) |
| 409 duplicate_proposal_id | reconcile (8.1) |
| 409 not_approved | reconcile with GET |
| 409 finished | exit 0 after the final ingest |
| 410 approval_lapsed | should be impossible (we check apply_by). Page. |
| 422 | bug: page, drop the proposal, decision row records the problems |
| 5xx, timeout, connection refused | GETs: exponential backoff (0.5 s to 30 s, jitter, max 10 tries). POSTs: reconcile through lookups as above. If the simulator stays down for more than 5 minutes: page, keep retrying, and do nothing that needs it. |

Timeouts: 30 s for GETs. For advance, `max(120 s, 20 x the measured wall seconds per simulated
hour x S/60)`.

### 8.5 Determinism (what makes the crash test strong)

There is no wall clock and no randomness in decisions. Iteration order is sorted, ids are
deterministic, and floats are rounded before they are compared to gates. Given the same API
answers, the controller issues the same calls at the same simulated minutes. Approval outcomes
are seeded by `proposal_id`, so a crashed run can reproduce the clean run's side effects
exactly.

### 8.6 World fingerprint

`world_id = sha1(scenario name, end_minute, tick, sorted workflow specs from the API, the first
16 lines of history chunk 0)`. If the simulator restarts, that is a new world: a new namespace,
an alert, and the old data is kept.

---

## 9. Observability and the supervisor view

### 9.1 Loop metrics (table `metrics`, served at `/ops`)

| Metric | Definition |
|---|---|
| `ingest_lag_sim_min` | `sim_minute - until_minute of the last done chunk` |
| `ingest_events_per_s` | per chunk, in wall time |
| `wall_s_per_sim_day` | the SRE target is under 600 s on public_03 |
| `decision_lag_sim_min` | event ts to the decision row that used it, p50 and p95 |
| `decisions_per_sim_hour` | by module and kind, including no-action |
| `proposals_by_state` | per workflow |
| `request_slots_used_24h` | per workflow vs C_eff and the reserve |
| `credits_remaining`, `credit_burn_24h`, `credit_floor_headroom` | |
| `open_incidents`, `mttr_min` | |
| `cohort_mismatch_count` | must be 0 |

### 9.2 Alerts

| Pages (actionable now, with what to look at) | Does not page (morning digest) |
|---|---|
| Intent pending after 2 reconcile passes ("see `/ops/intents?status=pending`") | Judgement-call rejections |
| Any 403, 410, 422, or a fatigue or evidence rejection ("local checker diverged from the policy: `policy_check` vs `GET /workflows/{id}`") | Futility drops, stalled probes |
| Ingest stalled for 2 steps, or `wall_s_per_sim_day > 600` | Drift alerts with no attributable cause |
| Credits at the floor while an incident is open ("incident INC-n cannot be validated") | Guardrail warnings under the threshold |
| Cohort mismatch, an unknown writer, or a world fingerprint change | Supervisor edits (these are information) |
| An open incident with no fix submitted within its window | Experiments interrupted by a supervisor |

### 9.3 Supervisor view (one screen per tenant, `GET /supervisor/{tenant}` as HTML and JSON)

1. **Needs your decision:** pending approvals, sorted by priority class then impact score. Each
   shows the one-line items, the diff, affected leads, the dry-run headline (primary and
   guardrail, worst case), time to expiry, and our expected decision window.
2. **Changed today:** each config version with actor, time, summary, approval id, and a link to
   the decision record. Supervisor edits are listed alongside ours.
3. **Being tested:** experiments with arm shares, stage, connected counts, effect and CI, and the
   next gate in words ("ramps to 10% if the lower bound stays above -7.5%").
4. **Winning:** promoted variants with holdout lift and decay status.
5. **Health:** incidents (open and closed with timeline), DID status, flag rate vs baseline.
6. **Upcoming:** planned auto-applies with ETA and the notice countdown, and the next routine
   slot. `POST /supervisor/holds` pauses non-incident changes on a workflow.
7. **Weekly digest** (Northwind): operational changes (caller ID, schedule) with the reason for
   each, generated from `decisions`.

### 9.4 The audit queries (C12)

"Why did workflow X change at T, who approved it, and what happened after?"

```sql
SELECT cv.version, cv.applied_minute, cv.actor, cv.changed_dims,
       p.proposal_id, p.change_summary, p.rationale, p.impact_score, p.dry_run,
       a.approval_id, a.created_at, a.decided_at, a.status,
       d.rule_id, d.inputs                      -- the stat snapshot at decision time
FROM config_versions cv
LEFT JOIN proposals p        USING (proposal_id)
LEFT JOIN approvals_mirror a ON a.approval_id = cv.approval_id
LEFT JOIN decisions d        ON d.decision_id = p.decision_id
WHERE cv.workflow_no = wf('nw_renewal') AND cv.applied_minute <= sim('D3 14:00')
ORDER BY cv.version DESC LIMIT 1;
-- what happened after: post-change monitors and later decisions on the same patches
SELECT * FROM decisions WHERE parent_decision_id = :decision_id ORDER BY sim_minute;
```

"Why did this customer hear this variant on this day?" (IRDAI, under 24h):

```sql
SELECT c.sim_minute, c.attempt_no, opt(c.welcome), opt(c.voice), opt(c.caller_id), opt(c.prompt),
       c.config_version, cohort_uid_of(c.cohort_code),
       ap.dim, ap.share, ap.source, ap.experiment_id, ap.proposal_id, ap.approval_id
FROM calls c
JOIN allocation_provenance ap
  ON ap.workflow_no = c.workflow_no AND ap.version = c.config_version
 AND ap.cohort_uid = cohort_uid_of(c.cohort_code)
 AND ap.option IN (opt(c.welcome), opt(c.voice), opt(c.caller_id))
WHERE c.workflow_no = wf('nw_renewal') AND c.lead_no = 12345 AND day(c.sim_minute) = 3;
```

The output reads like: "V2 at 10%: arm B of `exp_0007` (stage RAMP), proposed by
`w3fa1-nw_renewal-D2T1000-routine-1`, approved `apr_00012` at D2 11:40, randomised per call."
Both are exposed as `formi-audit why-changed` and `formi-audit why-heard`. Missing:
**approver identity**, which the API never returns. We store `approval_id` and `decided_at`,
and `approver_name` is a nullable column for the client's own system.

---

## 10. Testing strategy (end to end first)

### 10.1 Test pyramid

| Layer | What | How |
|---|---|---|
| **E2E, governance invariants** (primary) | public_01 and public_02 full runs | Start the simulator as a subprocess, run `run.sh`, then assert from the simulator log and our store: 0 auto-apply 403s, 0 apply conflicts, 0 lapses, 0 expiries, peak requests per 24h at most C_eff per workflow, every controller version has a proposal and decision row, every supervisor edit is never followed by a version that reverts its paths within the cooldown, and every prompt change has a passing validation on record. |
| **E2E, policy fuzz** | Generated mini-scenarios (the test writes YAML for the simulator; the controller never reads it) with randomised approver hours, latency, TTL (6 to 24h), caps (1 to 4), allowed dims, max_shift, mid_flight, tick 15 or 30 | The same invariants. This is our protection against hidden policies we have never seen. |
| **E2E, incident** | public_02 | DID share from D3 onward is 0, P2 MTTR under 4h, credits at most 30, V2 edit not reverted. |
| **Differential** | `policy_check` vs the live simulator | Tests only: 2,000 random config pairs, compare the local verdict with `PUT` on a scratch world. Must agree 100%. |
| **Unit** | `stats` (z, CI, Pocock lookup, binomial tail via `math.lgamma`, ramp), `timing` (`next_working`, windows, `used()`), `cohorts` (canonical form, first match) | pytest, table-driven, with the hand-computed numbers in this document as fixtures. |

### 10.2 Crash-recovery demonstration (C13)

1. **Fault injection.** `CONTROLLER_CRASH_AT=<kind>:<n>:<phase>` makes the process call
   `os._exit(137)` at the n-th intent of that kind. Phases: `before_t1`, `after_t1`,
   `after_http`, `in_t2`. Kinds: advance, approval_request, auto_apply, apply, validation, plus a
   crash inside a chunk ingest transaction.
2. **Crash-equivalence matrix.** For public_02, run once clean. Then, for each (kind, phase) and
   for n in {1, 3}, run the same scenario with one injected crash, then restart. Assert that the
   simulator's `config/history`, `GET /approvals`, credits used and the grade report are
   **identical** to the clean run. The only allowed difference is the `validation/after_http`
   case, which may cost one extra run and must record `lost_validation_run`.
3. **Random kill.** `scripts/kill_loop.sh` sends `SIGKILL` at uniformly random wall times 20
   times during a public_02 run, restarting each time. Assert the same invariants, plus "the run
   completes".
4. **Evidence:** `reports/crash_recovery.md` with the matrix (pass or fail per cell), the kill-loop
   log, and the diff of the grade reports.

---

## 11. Module breakdown for implementation

### 11.1 Package layout

```
controller/
  __main__.py            entry: python -m controller --sim-url ...
  config.py              Settings (env), learning and governance parameters with docstrings
  directives.yaml        client agreements (6.3)
  simclient/
    client.py            SimClient
    errors.py            ApiError subclasses per (status, code)
  store/
    schema.sql           DDL (versioned migrations)
    db.py                pool, tx(), copy_rows()
    repos.py             typed repositories per table
  cohorts/
    cells.py             encode(features) -> cell, decode
    registry.py          CohortRegistry
  stats/
    proportions.py       two_prop_z, rel_effect_ci, binom_tail, pocock_z
    ramp.py              next_share, harm_stop, futility
    detectors.py         window_count_threshold, ratio_test
  ingest/
    chunks.py            ChunkIngestor
    leadstate.py         lead state updates
    aggregates.py        SQL upserts from staging
  world/
    model.py             WorldModel (policies, effective policy, mirrors)
    clock.py             next_working, submit_window, latest_same_day, S_watch_max
  learning/
    experiments.py       ExperimentManager
    callerid.py          CallerIdManager
    calltime.py          CallTimeManager
    decay.py             DecayMonitor
  incidents/
    manager.py           IncidentManager (state machine)
    hallucination.py     prompt trigger, flag detector, fix selection
    did.py               DID incident runbook
    credits.py           CreditBudget
  planner/
    overlay.py           Patch, Overlay.compose, rebase_on_supervisor_edit
  governance/
    policy_check.py      changed_dims, max_shift, auto_apply_permitted
    timing.py            SlotPlanner, fatigue counter
    package.py           build_package (summary, diff, dry_run, affected, impact, rationale)
    lifecycle.py         ProposalLifecycle (state machine, lock)
    release.py           ReleaseTrain
  reliability/
    intents.py           IntentExecutor, Reconciler
    faults.py            crash injection
  loop.py                ControlLoop, step_size
  api/
    app.py               FastAPI: /supervisor, /audit, /ops, /supervisor/holds
    templates/           one HTML page
tests/ e2e/ unit/ fuzz/ scripts/
```

### 11.2 Public interfaces

```python
# simclient
class SimClient:
    def state(self) -> SimState: ...
    def advance(self, minutes: int, timeout_s: float) -> AdvanceResult: ...     # raises Timeout
    def chunks(self, kind: str, after: int) -> list[ChunkMeta]: ...
    def stream_chunk(self, n: int) -> Iterator[bytes]: ...                       # NDJSON lines
    def workflows(self) -> list[WorkflowSpec]: ...
    def config(self, wf: str) -> ConfigSnapshot: ...
    def config_history(self, wf: str) -> list[VersionMeta]: ...
    def request_approval(self, body: ApprovalRequest) -> ApprovalPublic: ...
    def approvals(self, wf: str | None = None, status: str | None = None) -> list[ApprovalPublic]: ...
    def approval(self, aid: str) -> ApprovalPublic: ...
    def apply(self, wf: str, aid: str) -> ApplyResult: ...
    def auto_apply(self, wf: str, body: AutoApplyRequest) -> ApplyResult: ...
    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun: ...
    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]: ...

# cohorts
class CohortRegistry:
    def uid(self, wf: str, where: dict[str, list[str]]) -> str: ...
    def register(self, wf: str, where: dict, kind: str, created_by: str) -> Cohort: ...
    def cells(self, uid: str) -> frozenset[int]: ...
    def membership(self, wf: str, version: int, cohorts: list[dict]) -> dict[int, str]: ...

# stats (pure, numpy and math only)
def two_prop_z(x1: int, n1: int, x0: int, n0: int) -> float: ...
def rel_effect_bounds(x1, n1, x0, n0, conf: float) -> tuple[float, float, float]: ...
def next_share(cur: float, lcb: float, ladder: Sequence[float], rho_arm: float) -> float: ...
def binom_upper_threshold(n: int, p0: float, alpha: float) -> int: ...
def pocock_z(k_looks: int, alpha_one_sided: float) -> float: ...           # lookup table

# world
class WorldModel:
    def policy(self, wf: str) -> EffectivePolicy: ...       # API policy AND directives
    def current_version(self, wf: str) -> int: ...
    def config_body(self, wf: str, version: int) -> dict | None: ...
def submit_window(policy: EffectivePolicy, day: int) -> tuple[int, int] | None: ...
def s_watch_max(tick: int, apply_window_min: int) -> int: ...

# learning and incidents emit patches and decisions, never call the API directly
class ExperimentManager:
    def on_ingest(self, wf: str) -> None: ...
    def harm_checks(self, wf: str, now: int) -> list[PatchRequest]: ...
    def planning_look(self, wf: str, now: int) -> list[PatchRequest]: ...
    def on_applied(self, wf: str, version: int) -> None: ...
    def on_interrupted(self, wf: str, path: Path, version: int) -> None: ...
class IncidentManager:
    def detect(self, now: int) -> list[Incident]: ...
    def respond(self, now: int) -> list[PatchRequest]: ...        # may spend credits via intents
    def verify(self, now: int) -> None: ...

# planner and governance
class Overlay:
    def add(self, req: PatchRequest) -> Patch: ...
    def compose(self, wf: str, live: dict, classes: set[str]) -> Composition: ...
    def rebase_on_supervisor_edit(self, wf: str, old: dict | None, new: dict, v: int) -> RebaseReport: ...
def auto_apply_permitted(policy: EffectivePolicy, cur: dict, new: dict) -> tuple[bool, list[str]]: ...
class ProposalLifecycle:
    def tick(self, now: int) -> None: ...                 # apply visible approvals, resolve terminals
    def submit(self, comp: Composition, cls: str, now: int) -> Proposal: ...
    def lock_holder(self, wf: str) -> Proposal | None: ...
class ReleaseTrain:
    def run(self, now: int) -> None: ...                  # incidents, auto, routine slot

# reliability
class IntentExecutor:
    def execute(self, kind: str, key: str, payload: dict, decision: Decision) -> Any: ...
class Reconciler:
    def run(self) -> ReconcileReport: ...
```

### 11.3 Dependency order and parallel tracks

```
Track A (day 1, parallel): store/schema + db      simclient          cohorts         stats
Track B (needs A):         ingest                  world (+clock)     governance.policy_check
Track C (needs B):         reliability.intents     governance.timing/package/lifecycle
                           learning.* (needs stats, cohorts, aggregates)     incidents.*
Track D (needs C):         planner.overlay -> governance.release -> loop
Track E (needs store):     api (read-only views can start from schema + fixtures)
Tests: unit with each module; e2e harness starts at Track B (ingest-only run), grows with D.
```

Contracts between tracks are the table schemas in section 3 and the dataclasses above. Learning
and incident modules never call the simulator for config changes. They only emit
`PatchRequest`s, so governance is the only path to a change. That is easy to review and easy to
test.

---

## 12. Stakeholder positions (brief)

| Memo | Accept | Push back | Wrong claim |
|---|---|---|---|
| 01 Sales | Batched requests, so we do not look hesitant. A lift figure per workflow and KPI. | No blended score (constraint 3, Product). No "winning by lunch": a morning (4h) of nw_renewal has about 310 connected calls, so a 50/50 test there can only detect effects of about 29% (`2.8 x sqrt(0.2475 x 2/155)/0.55`). No P4 rollout: its validation failure rate is about 2.6x P1's, and it fails our bar 92% of the time. | "Kestrel turned on auto-apply for everything": false, only schedule, caller_id, welcome and voice, at max_shift 0.2, and never prompt or cohorts. "Northwind fine with auto-apply on KYC": false, KYC auto-apply is disabled, and their Compliance requires human approval. |
| 02 Compliance | Customer-facing changes only through named approval (directive, even where policy would allow auto-apply, e.g. `nort_cancel_2`). Validation evidence on prompts. Per-call "why heard" within 24h (9.4). Approver hours respected. Weekly digest. | "Named human": the API exposes no approver identity, so that has to come from your approval system. | "Timing and caller-ID rotation covered by the policy we signed": true for renewal, **false for KYC**, where auto-apply is off, so every change there goes to approval. |
| 03 SRE | Kill-safety with a crash-equivalence test, idempotency everywhere, only actionable pages, Postgres only (no Redis needed), loop health at `/ops`. | No Prometheus stack yet (C14 partial). | none. The 10-minute day is a deliverable, measured, not assumed. |
| 04 Kestrel Ops | Never undo her edits silently (rebase, cooldown). 2 a day, never before 11:00 (directive). One request with many items. One screen. 60-minute notice before auto-applies. No "word for word" prompt: last-known-good first, plus post-apply engagement verification. | An incident may use a 3rd request (still within the policy's 4), and the view says why. | Her own P2 edit is the hallucination incident. It is fixed through an approval with evidence, not a silent revert. |
| 05 Product | Separate KPIs, one cohort object, a computed cohort size floor (385 connected/day) and window, every proposal with impact, diff, affected leads and dry run, expired treated as rejected and re-queued fresh, only arms that ran count as evidence, winners stay in their workflow. | The queue is ranked by priority class first, then impact. Per-person call time is not P0: data too thin (5.7). | "All three are P0": not with this data. Caller ID is the biggest pickup lever and is built; per-cohort timing is partial. |
| 06 CTO | Explainable over larger, harm-limited exploration, credits spent only where they change a decision (8 to 16 of 200 predicted), choices written down. | none | "Credits are cheap here": partly true. 200 credits is about 10 worst-case incidents, so we keep a floor. |

---

## 13. Top risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The harm-limited ramp is too slow at dev volume and we capture little lift on hidden scenarios | Lower outcome score | The factorial design doubles the traffic each call contributes. Ladder, rho and L are explicit parameters with the cost table in 5.4. At the README envelope the ramp costs hours. We accept the trade-off openly as the governance position. |
| The local policy checker diverges from the simulator (403 is graded) | Governance hygiene | The checker re-implements the contract, a 2,000-pair differential test runs against a live simulator, a 403 pages and falls back to approval, and we never probe by trial. |
| A hidden policy shape we never tested (TTL 6h, cap 1, approver hours that make same-day impossible) | Expiries, lapses, wasted slots | Every window is computed from API parameters with `expiry_safe` as a hard precondition. A policy fuzz test covers random shapes. If no safe window exists, routine is suspended with an alert rather than guessing. |
| Supervisor edits racing our approvals | apply_conflict | Fresh base check before every apply; STALE never calls apply; the overlay rebases and invalidates supervisor-owned paths. |
| Two supervisor edits in one advance hide an intermediate body | Imperfect audit of that version | Treat every differing path as supervisor-owned. `body_source = unknown` is visible in the audit, and calls still record what was heard. |
| A crash between a validation POST and recording it | Lost credits, a missing evidence id | Detected through the credit-ledger delta, recorded as `lost_validation_run`, one re-run. The crash-equivalence matrix covers the case. |
| A false DID or hallucination alarm triggers a harmful change | Harmful change | Two-stage detection: a free population detector, then paid confirmation for prompts; for DIDs, a z threshold plus a practical-size threshold. Computed false-positive rates are 0.04% to 0.8% per workflow-day. |
| Ingest or the simulator too slow on public_03 | SRE target missed | Streaming COPY, aggregates in SQL, overnight jumps. Estimate: about 850k events/day at about 100k lines/s parse is under 15 s/day for ingest. The simulator's own per-event loop is the likely bottleneck, which we measure, not assume. At 10x: shard ingest by workflow across processes (aggregates are commutative), partition `calls` by day and drop raw rows older than the audit horizon down to the audit columns, and shard the governance loop by tenant (no cross-workflow state except the credit pool). |
| Directives keyed by tenant do not match hidden tenants | Too strict or too loose | The default directive for unknown tenants is the conservative one. Directives can only tighten the API policy, never loosen it. |
| Decay detection is underpowered (a 10% holdout gives a 6.9% se over 48h) | A decayed variant lingers | R2 compares the lift now with the lift at promotion (z of about 2.75 for a 22-point fall). Rollback only when the holdout is better, so a late rollback costs little, and an early one would itself be harmful. |
