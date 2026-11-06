# Outcome-First Agent Loop - Submission

**Name:** TBD · **Date:** TBD · **Hours actually spent (be honest):** TBD · **Language / storage:** Python 3.11 / Postgres 16

Reading key: `§5.4` means section 5.4 of [docs/design/DESIGN.md](docs/design/DESIGN.md) (the full
design; section 17 there overrides earlier sections). ADRs are in [docs/decisions/](docs/decisions/).
Every `[T]` threshold lives in `controller/settings.py` with its derivation, and may be tuned only on the
56 private variants in `scenarios/private/`, never on public-scenario truth. Measured numbers cite
the build report they come from (M0, T1 to T5, I1, I2 in
[docs/design/build-reports/](docs/design/build-reports/)); "I2" is the integration step that put
governance into the real loop.

---

## 0. TL;DR

1. A Python controller with Postgres as its only state, started by `./run.sh <SIM_URL>`. It ingests
   every event exactly once, learns per cohort, and changes config only through governance. Every
   change passes one harm gate that mirrors the grader's per-change judgement (population) plus a
   stricter per-cohort line of my own.
2. Pickup comes from caller-ID health (SPRT probes, spread, a flagged-DID drain in about 2 hours) and
   per-cohort call times. Engagement comes from harm-gated welcome x voice learning with a holdout
   and decay rollback. Hallucination incidents are fixed through one evidence-backed approval.
3. public_01: [[RESULT: ALL primary-KPI lift, harmful count and engagement gap captured, from reports/public_01.md]].
   public_02: [[RESULT: ALL primary-KPI lift, harmful count, hallucination MTTR and flagged-DID day share, from reports/public_02.md]].
4. Governance: [[RESULT: auto_apply_rejected / lapsed / expired / apply conflicts summed over public_01 and public_02, from reports/public_01.md and reports/public_02.md]];
   credits used on public_02 [[RESULT: credits used, from reports/public_02.md]] of 200.
5. Deliberately not built: per-person call time, prompt A/B tests outside incidents, a SNIPS dry
   run, an HTML supervisor page, a welcome x voice interaction test, cross-tenant approver fairness.

---

## 1. Assumptions

1. **Guardrail `max_drop` is an absolute drop** (0.02 = 2 points), over a rolling 2-day window,
   against a concurrent comparator where one exists. Proposals must predict no drop at all,
   including drops caused by a change in who connects ([ADR 0007](docs/decisions/0007-guardrail-definition.md)).
   The relative reading (2% of a 0.26 pickup = 0.5 points) cannot be measured at dev volume, so it
   would be unenforced.
2. **No welcome x voice interaction.** The simulator samples them independently per dial and I
   assume their effects multiply. Stated, not tested (the likelihood-ratio test is cut).
3. **Arm effects a priori `N(0, 0.06^2)` on the log scale at population scope, sd 0.12 at cohort
   scope**, widened (never narrowed) by the dispersion actually observed.
4. **Client memos are agreements that can only tighten the API policy.** Effective policy = the
   stricter of the two ([ADR 0012](docs/decisions/0012-directives-layer.md)).
5. **A memo's scope follows its author.** Northwind Compliance speaks for the whole tenant. Kestrel's
   operations supervisor runs the cancellation line only, so her asks apply to Kestrel workflows
   whose `use_case` contains "cancel".
6. **Where policy allows customer-facing auto-apply but the client memo forbids it, I do not use it**
   (public_03 `nort_cancel_2`), and I flag the conflict.
7. **Kestrel's "two a day, never before 11:00" is my cap** on her line. An incident may use a third
   request, still inside the policy's 4.
8. **Auto-applies are not chained to walk around `max_shift`.** Exposure-increasing auto-applies
   stay within `max_shift` of cumulative movement per (cohort, dim) per 24h. Only risk-reducing
   incident and rollback steps chain, one per tick, bounded by the share being removed
   ([ADR 0025](docs/decisions/0025-risk-reducing-changes-and-chaining.md)).
9. **An untested caller ID is flagged with probability 0.2**, at a relative pickup of 0.35 if so.
   This sizes probes.
10. **Caller-ID health falls with recent load**, so an equal split over healthy DIDs is the target.
11. **Hour of day, caller ID and schedule do not change engagement given a connect.** They change
    who connects, which the guardrail forecast models.
12. **The business owns whether, when and how many to call.** I never touch cadence, dialling
    window or dials per hour, and every in-window hour keeps an hour weight of at least 0.25 (a
    weight of 0 would waste capacity the business owns).
13. **Cohorts, allocations and schedules in a business default belong to the business**: kept
    first, verbatim, never redefined.
14. **The per-cohort harm line is my standard, not the grader's.** The grader judges the population.
15. **"Unsafe prompt" is judged from validation failures, not from a known mapping.** My bar is a
    pooled failure rate of 2% over 1,300 conversations, independent of the client's gate. Verdicts
    are per workflow, not pooled across workflows that share a prompt id.
16. **Only what ran counts as evidence.** An arm is judged from the `variant` recorded on each call,
    not from the config version (they differ on `next_campaign_only` workflows).
17. **The API never names the approver**, so "named human approval" is an approval id and a time.
18. **Approvers work every day.** `approver_hours` has no weekday and the simulator's
    `next_working` ignores weekends; a contract test checks this first.
19. **A restarted simulator is a new world.** Resume happens only if the store passes consistency
    checks against the live simulator (§8.5).
20. **Grading box:** Docker with Compose v2 (a no-Docker path exists), able to pull `postgres:16`
    and `python:3.11-slim`, a free port in 55432 to 55451, and a wall-clock limit of at least 2
    hours per scenario.

---

## 2. Scope decisions

[[RESULT: confirm each status against the I3 to I6 integration reports; at the I2 report C6, C7, C8, C10's dry run and verification were built as pure modules but not yet wired into the loop]]

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Built | The floor, and SRE's 10-minute bar. COPY of raw lines plus one set-based SQL transform per chunk, exactly once by chunk number, Python parser as fallback. Measured 32 s per public_03 sim day with a no-op loop. | P-way transform split by workflow hash, for 10x (§13.2). |
| C2 | One shared cohort object | Built (small) | Every detector, experiment, dry run and view row carries the same versioned `cohort_key`, which is also the id written into the simulator config. | Regrid when traffic shifts (cut). |
| C3 | Bootstrap from history | Built (narrow) | Baselines only: hour curves, cell mix, per-DID pickup, flag-rate baseline, active leads. History ran one config, so it has no variant information. | - |
| C4 | Call-time optimisation | Partial | Per-cohort hour weights built: pickup lever, graded neutral, cheap. Per-person `lead_preferred_hour` cut: 2 to 3 dials per person per campaign, and a wrong guess costs x0.25 in every other hour. | About 1.5 days: Beta-binomial per returning person on hour bands, cap management, A/B on returning persons. |
| C5 | Caller-ID health | Built | Largest pickup lever on single-DID workflows, auto-applicable on most policies, and an incident class with no event. Includes the KPI drift detector for workflows with no comparator. | - |
| C6 | Welcome x voice experimentation | Built | The only engagement lever with per-cohort targeting. Factorial, harm-gated probability matching, per-scope winner rule. | Interaction test and regrid (cut, see below). |
| C7 | Promotion, decay, rollback | Built | Decay rollback is graded. Holdout, Page CUSUM, recency-weighted gates, rollback timed to the predicted apply time. | - |
| C8 | Hallucination incident response | Built | Graded on time to fix and a compliance failure if missed. Two-stage verdict, live flag CUSUM, one LLM suggest call, last-known-good fix, post-apply verification. | - |
| C9 | Governance client | Built | Every outcome passes through it, and hygiene is graded on hidden policies. | - |
| C10 | Impact, affected leads, dry run | Partial | Impact labelled with the KPI its lever moves, integer affected leads, stratified posterior replay. SNIPS cut (high variance at dev volume). | Half a day for SNIPS with variance control. |
| C11 | Supervisor view | Partial | JSON API per tenant (8 sections) and a holds endpoint. HTML page cut. | About 1 day: HTML page, auth, email digest. |
| C12 | Audit store | Built | The floor and the live-review question: `why-changed`, `why-heard`, `replay-decision`. | - |
| C13 | Crash recovery, demonstrated | Built | SRE non-negotiable: write-ahead intents, fault injection at every side-effect boundary, crash-equivalence matrix, random kill loop. | - |
| C14 | Loop observability | Partial | `loop_step` table, `/ops` JSON, alert rules with a runbook anchor for every code. No metrics exporter. | Half a day for a Prometheus exporter. |
| C15 | Fair allocation across tenants | Partial (minimal) | Ordered credit pools with per-tenant screen envelopes. Approver attention is per workflow in this API, so there is nothing to share yet. | About 1 day once approvers are shared across workflows. |
| - | Prompt A/B outside incidents | Cut | One prompt per workflow means no concurrent control. A +3% effect needs about 14,400 connected calls per arm; a switchback needs about 30 days of a dev workflow. | Switchback design, about 2 days, low value. |
| - | Interaction test, regrid, policy fuzzing beyond the attack cases | Cut | Each refines a built capability rather than adding one. Their failure modes are stated in section 13. | About 1 day together. |
| - | Cross-workflow promotion of winners | Cut | Product: "a later problem". | - |

**The principle.** (1) Build to production grade what the floor and the hard constraints need:
C1, C9, C12, C13. (2) Then levers in order of expected outcome per unit of evidence at the
**smallest** volume we must handle (500 to 1,200 connected calls per workflow-day): caller ID and
per-cohort call time first (pickup, measurable in hours), then the hallucination fix (graded on time
to fix), then welcome and voice (they need days, so they get the most careful statistics). (3) Make
those trustworthy to a supervisor in minimal form: C2, C10, C11. Cut what cannot reach significance
inside a scenario (prompt A/B, per-person timing), what refines rather than adds, and what the API
gives no lever for. Detail: §1 and §15.3.

---

## 3. Stakeholders

Full positions: §12.1. Evidence for "wrong" claims is the governance block that
`GET /workflows/{id}` returns, which is what the controller reads.

### 01 Sales (Nikhil Rao, VP Sales)

- **Accepted:** one lift figure per client per KPI, from our audit store; batching (one request per
  workflow carries every item), so we do not look hesitant.
- **Pushed back:** a blended score (Product and the contract keep KPIs separate). "Winning by lunch,
  live by evening": a new variant gets about a dozen of a morning's 300 connected calls, and a 10%
  effect needs 800 to 1,600. "Persuasive everywhere": my safety verdict rejects a prompt like it 94%
  to 97% of the time (T4 simulation), and it is not offered on KYC.
- **Wrong:** "Kestrel turned on auto-apply for everything": ks_cancel allows only `schedule,
  caller_id, welcome_message, voice` at `max_shift: 0.2`, never prompts or cohorts, and public_03's
  `kest_kyc_1` has auto-apply off. "Northwind is fine with KYC auto-apply": `nw_kyc` has
  `auto_apply: {enabled: false, allowed_dims: []}`, and Northwind Compliance requires human approval.
- **Reply:**
  > Nikhil, for the summit you will get one lift figure per client per KPI, pickup and engagement
  > shown separately, taken from our audit store. No blended score: Product and our client contracts
  > keep the KPIs separate. "Winning by lunch" is not real at pilot volume: a morning has about 300
  > connected calls, a new variant gets a dozen of them, and a 10% effect needs 800 to 1,600 calls on
  > the variant. At production volume winners ship in one to two days. Kestrel's auto-apply covers
  > timing, caller ID, welcome and voice in small steps, never prompts; Northwind KYC has no
  > auto-apply. The persuasive prompt fails our safety validation far more often than today's, so it
  > will not ship.

### 02 Northwind Compliance (Meera Iyer)

- **Accepted, all five conditions:** no customer-facing auto-apply anywhere on Northwind (a
  directive, section 6); validation evidence on every script change against a stricter bar than
  their 0.97 gate; per-call "why did this customer hear this" (`why-heard`, section 10); requests
  timed inside approver hours; a weekly digest. "Worse for a cohort, even briefly" became a 1%
  per-cohort harm line for Northwind (2% elsewhere), at a cost: half-size first exploration and
  winner looks about 1.5 days later.
- **Pushed back:** "named human approval" cannot be met as written: the API returns an approval id
  and a decide time, never a name. We store those and ask the platform to expose identity.
- **Wrong:** "timing and caller-ID rotation are covered by the policy we signed" is true for
  `nw_renewal` (`allowed_dims: [schedule, caller_id]`) and **false for `nw_kyc`** (auto-apply
  disabled). And public_03 `nort_cancel_2` *permits* welcome and voice auto-apply, contradicting
  condition 1: I follow the memo and ask them to align the policy.
- **Reply:**
  > Meera, we accept all five conditions. No customer-facing change (opening line, voice, script)
  > goes live on Northwind without your approvers, including nort_cancel_2, whose signed policy
  > allows welcome and voice auto-apply; please align it with your memo. Script changes carry
  > validation evidence against a stricter bar than your 0.97 gate. Every change must show, before it
  > ships, that no customer cohort is likely to lose more than 1% engagement. One correction: the
  > policy covers timing and caller-ID rotation on renewal, not KYC, where every change comes to
  > you. We answer "why did this customer hear this" per call, from our own records. The approval
  > system gives us an approval id and time, not the approver's name.

### 03 Platform & SRE (Kabir Menon)

- **Accepted, all of it:** a crash-equivalence matrix and a random kill loop, idempotency keys on
  every call (section 9); Postgres as the only stateful component, no Redis
  ([ADR 0001](docs/decisions/0001-stack.md)); pages only for what needs a human now, each with a
  runbook (section 10); loop health at `GET /ops`; measured public_03 numbers and a 10x table
  (section 8).
- **Pushed back:** only on timing: no Prometheus exporter yet (C14 partial).
- **Wrong against the contract:** nothing found.
- **Reply:**
  > Kabir, accepted as written. State lives only in Postgres; no Redis and nothing new to operate.
  > Every side effect is written ahead as an intent and reconciled on restart. The crash test kills
  > the controller at every side-effect boundary plus random SIGKILLs, and the result must match a
  > clean run. Pages only for what needs a human now (stuck ingest, an approval about to lapse, a
  > policy-check mismatch, credits below the incident reserve), each linking a runbook; the rest
  > goes to a morning digest. Loop health is at /ops. Ingesting and simulating one public_03 day
  > takes 32 s on a laptop; at 10x the lead join and the serial transform break first, and we
  > partition by tenant.

### 04 Kestrel Operations (Anita Desai)

- **Accepted, on her cancellation line:** at most 2 requests a day, never before 11:00; one request
  with every item; one screen (needs you, changed today, being tested, winning); a 60-minute notice
  inside approver hours before any non-incident auto-apply; her edits never silently undone
  (rebase, 24-hour cooldown); no "word for word" fix (last known good first, prompts whose summary
  says "verbatim" or "word for word" ranked last).
- **Pushed back:** a third request in a day, only for an incident, within the policy's 4, and the
  screen says why. The API has no notification channel, so "tell me before" is the notice period
  and the Upcoming list.
- **Wrong:** nothing false; her caps are just stricter than the policy (4 a day, approvers from
  10:00). Her own mid-day prompt edit is the incident class public_02 contains (D3 11:30), fixed
  through her approver with evidence. The "word for word" fix matches the catalogue's `P5`
  ("Strict: near-verbatim compliance script"): validation cannot catch it, only live engagement can.
- **Reply:**
  > Anita, on your cancellation line your rules are ours: at most two requests a day, never before
  > 11:00, each batching everything we want to change. We never undo your edits silently. If one of
  > your prompt changes starts hallucinating, we send one request with the validation evidence and
  > the prompt we suggest returning to; only then would we use a third request, and your screen will
  > say why. Timing, caller-ID and voice adjustments your policy allows appear on your screen at
  > least an hour before they happen, and you can hold them. We will not push a word-for-word
  > script: fixes go back to a prompt your callers already responded to. One screen: tested,
  > winning, needs you, changed today.

### 05 Product (Tara Kulkarni)

- **Accepted:** separate KPIs; one versioned cohort object; expired = rejected, rebuilt with a new
  id, base and fresh data; evidence only from calls that ran; winners stay in their workflow. Her
  open questions answered with costs: targeting cohort 5% of daily leads, winner scope 15% of
  daily connected calls, 200 calls per arm over 2 days, threshold 0.995 (0.998 per cohort), with
  the power and false-winner table in section 5.
- **Pushed back:** the queue ranks by priority class first, then impact (an incident must come
  first even with a modest score). Of her three P0s: caller-ID health yes, supervisor view yes
  (JSON), per-person call time no.
- **Wrong:** "everything on the wishlist is P0" cannot hold with this data. And ranking by impact
  only means something if the score has units: mine is the 20th percentile of the relative change
  of the KPI the lever moves, labelled with that KPI, never summed across KPIs
  ([ADR 0020](docs/decisions/0020-impact-score-definition.md)).
- **Reply:**
  > Tara, we hold every definition: separate KPIs, one versioned cohort object, expired treated as
  > rejected and rebuilt from fresh data, evidence only from calls that happened. A cohort needs 5%
  > of leads for its own targeting and 15% of calls for its own declared winner; smaller cohorts
  > benefit through allocation. A winner needs 99.5% posterior confidence (99.8% for a cohort), 200
  > calls per arm over two days, and recent data that agrees. Dropping to 95% lifts early decisions
  > from 52% to 87% but multiplies false winners eighteenfold. The impact score is the cautious lift
  > on the KPI the change moves. The queue sorts by urgency first, then impact. Caller-ID health and
  > the supervisor view ship; per-person call time does not.

### 06 CTO (Aryamann)

- **Accepted:** outcomes a compliance head can sign (harm gate, holdout, replayable decisions);
  credits only where they change a decision; an honest scale envelope; the cut list (section 2).
- **Pushed back:** none.
- **Wrong:** "credits are cheap here" is only partly true: 200 (400 on public_03), a full safety
  verdict costs 13 and a worst-case incident 68, so I hold a reserve of 136 for two worst-case
  incidents (section 7). He is right that other memos contain false claims; they are listed above.
- **Reply:**
  > Aryamann, the bet: every change passes the grader's population harm test plus a stricter
  > per-cohort one, so we learn slowly rather than hurt a cohort. Caller-ID health and cohort call
  > timing carry pickup; welcome and voice learn through harm-limited exploration, with a holdout to
  > catch decay. Credits go only where they change a decision: prompt screens, one LLM suggestion
  > call per incident, and a reserve for two worst-case incidents. I said no to blended scores, to
  > auto-apply where clients asked for humans, to per-person timing and to prompt A/B tests. The
  > Kestrel auto-apply, Northwind KYC auto-apply and KYC caller-ID rotation claims were false. At
  > 10x the lead join and serial transform break first; partitioning by tenant fixes both.

---

## 4. Architecture

```
            host: 4 vCPU / 8 GB Linux (grading box)
 +------------------------------+   HTTP only   +----------------------------------------------+
 | simulator (python -m sim)    |<------------->| controller container (one process)           |
 | /sim/advance /events/chunks  |               | main thread, one step:                       |
 | /workflows* /approvals*      |               |  PLAN -> ADVANCE -> INGEST -> REFRESH ->     |
 | /apply /config /validation   |               |  LIFECYCLE -> DETECT -> DECIDE -> GOVERN ->  |
 | /llm/suggest-fixes           |               |  VERIFY -> OPS                               |
 +------------------------------+               | loader thread: stream chunk k+1 into COPY    |
                                                | heartbeat thread: 10 s row                   |
                                                +----------------------+-----------------------+
                                                                       | psycopg 3
                                                                       v
 +---------------------------------------------------------------------------------------------+
 | Postgres 16 container: the ONLY stateful component                                          |
 |  ctl.run_registry          one schema per run: run_<fingerprint12>_<n>                      |
 |  ingest | facts | rollups | learned state | governance | intents | audit | ops               |
 +---------------------------------------------------------------------------------------------+
                                                                       ^ role ctl_api: SELECT,
                                                                       | write only `hold`
                                                +----------------------+-----------------------+
                                                | api container (FastAPI): /supervisor/{tenant}|
                                                | /explain/change /explain/call /ops /holds    |
                                                +----------------------------------------------+
```

**From events in to config changes out.**

```
chunks --COPY--> stage_raw --one SQL txn per chunk--> call_fact, lead_dim, rollups (r_engage,
         r_pickup, r_did, r_flags), ingested_chunk(chunk_no)          [exactly once by chunk_no]
rollups -> snapshot per workflow (numpy)
   -> incidents.detect (DID, prompt trigger, flag CUSUM, KPI drift, guardrail, decay)
   -> learning (engagement model, allocator, winners, DID health, schedule)
        each emits a PatchRequest(path, value, class, evidence) after its decision row is written
   -> planner.Overlay: live config + patches by path (rebase on supervisor edits, ownership)
   -> governance: harm gate re-check -> router (auto or approval) -> submit timing -> lifecycle
   -> IntentExecutor: write-ahead intent -> HTTP -> result, each transition in the same txn
```

Learning and incident modules never call the simulator to change config. They emit patches, and
governance is the only path to a change.

**State and where it lives.** Only in Postgres. Facts and rollups; cohorts by version; experiments,
arms, winner looks, holdouts, decay and DID state, CUSUMs; incidents, verdicts, validation runs,
credit ledger; patches, path owners, cooldowns, proposals and their transitions, the approval
mirror; intents; decisions, audit log, allocation provenance per (version, cohort, dim, option);
`loop_step` metrics and alerts. The process holds only caches rebuilt from Postgres on start.
"One change in flight per workflow" is a partial unique index (`one_in_flight`), not a convention.

**What happens on restart** (§2, [ADR 0017](docs/decisions/0017-run-identity.md),
[ADR 0018](docs/decisions/0018-crash-safety-protocol.md),
[ADR 0029](docs/decisions/0029-governance-in-the-loop.md)):

1. Take an advisory lock keyed on the simulator URL (a second controller waits instead of acting).
2. Resolve the run schema by fingerprint, and resume it only if it is consistent with the live
   simulator (our clock and ids not ahead of it, every approval and version we hold is there).
   Otherwise create a new schema and raise `NEW_WORLD`.
3. Reconcile every intent left `started` before any new side effect (section 9).
4. Ingest every chunk above the cursor; resend or abort any proposal left in DRAFT.
5. Sweep proposals against `GET /approvals` and `config/history`.
6. Resume the interrupted step from its first unfinished stage (`loop_stage` markers), so a crash
   inside GOVERN neither skips nor repeats the earlier stages.

---

## 5. The learning loop

Volumes it must work with (read from the API at runtime): nw_renewal about 950 connected calls a
day, nw_kyc about 465, ks_cancel about 1,200, each public_03 workflow about 13,500. At dev volume a
+10% effect inside a 50% cohort needs about 5 days at 50/50. Power, not cleverness, is the limit.

**The one gate behind every change** (§0.1, [ADR 0002](docs/decisions/0002-harm-gate.md)). The
grader judges each change at apply time: harmful if expected engagement falls more than 1% or
expected caller-ID health falls more than 2%. So every change I make (exploration, ramp, promotion,
rollback, probe, drain, rebase) must pass, on my own recency-weighted posterior:

```
P(de  < -1%)  <= 2.5%   population engagement (10% for rollbacks, which only move back to control)
P(de_k < -x_k) <= 2.5%  every cohort k; x_k = 2% default, 1% for Northwind (my line, not the grader's)
P(dd  < -2%)  <= 5%     relative caller-ID pickup
```

The one stated exemption is a P0 prompt fix: safety comes first even if it costs engagement (section 7).

### Cohorts

- **Grid at D0, from history only** ([ADR 0022](docs/decisions/0022-cohort-grid.md)). Every rollup
  is keyed by cell (an observed language, tier, age, value combination), so a cohort's statistics
  are sums of rows. F1 = the feature whose hour-of-day pickup curves differ most (deviance test,
  p < 0.001); F2 = language when there are 2 to 5 languages with at least 10% each. F1 x F2 cells
  under 5% of daily leads merge with the closest hour curve, then into an `other` bucket; cap 20.
  Business cohorts come first, verbatim, never redefined.
- **Two minimums, two jobs.** A *targeting* cohort needs 5% of daily leads (about 200 history dials
  per window hour for its own hour curve). A *winner scope* needs 15% of daily connected calls and a
  projected 800-call look before the freeze: a 50% cohort at nw_renewal volume gets there around
  D6, a 5% cohort would need 20+ days. Smaller cohorts benefit only through allocation.
- **Identity and no fragmentation.** `cohort_key = slug + "~" + sha1(definition)[:6]` (for example
  `age60p_ta~3fa2c1`) is also the simulator's cohort id; a new definition is a new key. Exploration
  lives in `allocations.default`; a cohort gets an override only when its allocation diverges.

### Experiment design

- **Only on engagement-primary workflows.** On a pickup-primary workflow welcome and voice cannot
  move the primary, can only hurt the guardrail, and would burn the slots caller-ID changes need.
- **Factorial, all arms at once.** Welcome, voice and caller ID are sampled independently per dial,
  so every call informs both dimensions (roughly double the power of one dimension at a time), and
  options listed last are tested as early as the first. No interaction terms (assumption 2).
- **Cohort-specific effects without splitting traffic.** Two models behind one interface (§17.2): a
  per-cohort Beta-binomial (default) and a ridge-logit with arm x feature terms over language, age
  and tier, so a voice that is x1.22 for one age band and x0.88 for another does not average to
  null. The ridge-logit ships only if it beats the Beta-binomial on the private variants with no
  harmful change. [[RESULT: which engagement model shipped and the gap-captured comparison, from the I4 report and run_matrix.sh output]]
  Fit time at public_03 scale: 0.04 s and 0.14 s.
- **Allocation: harm-gated probability matching** ([ADR 0004](docs/decisions/0004-probability-matching.md)).
  Each arm's target share in a cohort is proportional to P(it is best), from 4,000 draws seeded by
  the decision id (deterministic, replayable). The step toward the target is the largest that
  passes the gate. Floor 2% per live arm, ceiling 50% before a winner.
- **Exploration vs exploitation falls out of the gate.** Untested arms are scored at prior mean 0,
  so `1.96 x 0.06 x sqrt(sum s_i^2) <= 0.01` gives 3.8% per arm for 5 challengers (1.9% under
  Northwind's 1% cohort line). Moving traffic onto a confidently good arm buys headroom for more
  exploration. A harm stop runs every step: `P(theta < 0) >= 0.95` with a material expected loss
  sends that arm's traffic back to control.

### Deciding a winner

- **Posterior with a fixed rule** ([ADR 0005](docs/decisions/0005-winner-rule.md)). Looks only on an
  information-doubling schedule (200, 400, 800, 1,600 arm calls in scope, first look after at least
  2 days), persisted so a crash does not add looks. Declared when all hold: W1 stationary
  `P(theta > 0) >= 0.995` (population) or `0.998` (cohort); W2 `P(best) >= 0.95`; W3 the
  recency-weighted posterior agrees (`P >= 0.90`, mean at least half the stationary one); W4 no
  decay alarm; W5 the promotion itself passes the harm gate; W6 no predicted guardrail drop.
- **Multiple comparisons.** Per-scope thresholds come from the effective boundaries under unequal
  arm and control sizes. Union bound over 4 looks: **0.54% per population hypothesis, 0.47% per
  cohort hypothesis** (a single 0.99 threshold would give 1.28% and 2.8%). Per workflow, under a
  global null, at most 0.074 expected false winners at dev volume and 0.22 in the worst grid.
- **Measured false-winner rate** (T3, `tests/unit/test_false_winner_rate.py`, 300 null runs x 15
  hypotheses of the production code path, adaptive allocation): **full rule 0 of 1,500 population,
  2 of 3,000 cohort (0.07%)**. W1 alone: 0.73% population (above the 0.54% bound, because at higher
  volume first looks come later and doubling continues past 4 looks), 0.20% cohort. A `look_max`
  setting can cap it at 4. [[RESULT: look_max value chosen on the private variants, from the I4 report]]
- **Cost, for Product.** For a true +10% effect, P(declared at the 800-call look) is 0.52 at 0.995
  and 0.87 at 0.95, but 0.95 multiplies the null bound by about 18. At dev volume a population
  winner is possible from about D4, a 50%-cohort winner from about D6.

### Decay and rollback

- **Promotion keeps a holdout** sized so the one-day standard error of winner vs holdout is at most
  0.075: 20% at nw_renewal volume, 5% at public_03 volume. It costs `h x theta` (about 2% of the
  scope's engagement) and buys decay visibility and a concurrent guardrail comparator.
- **Monitor:** a one-sided Page CUSUM (k 0.5, h 4) per arm with at least 10% share and per promoted
  winner, on half-day segments, reference taken at the 200-call evidence floor and retaken at
  promotion ([ADR 0006](docs/decisions/0006-holdout-decay-rollback.md)). In-control ARL about 336
  segments (T3 corrected my design's 168), about 6% false alarms per monitored arm per 10-day run.
- **Rollback timing.** The grader counts a decay from onset and grades a rollback at onset as
  harmful (the variant is still at full strength). So an alarm only freezes increases; the rollback
  fires when the effect *predicted at the expected apply time* (local trend over 4 segments) is
  below control with `P(harm) <= 10%`. Recency-weighted gates (half-life 12 dialling hours) stop
  ramps and promotions on stale evidence before the CUSUM alarms.
- **How fast:** at dev volume 1.5 to 2 days after onset; at public_03 volume within one segment.
  Measured (T3, 4 decay shapes x 4 seeds, 343 graded changes): no ramp or promotion of the decaying
  arm graded harmful, none of 34 promotions and 46 rollbacks harmful, 2 harmful changes overall
  (0.6%, mild cuts on posterior noise). [[RESULT: decay share by onset + 2 days on the decaying-winner private variants, from the I4 report]]

### Call time and caller ID

- **Call time: per cohort, from history.** Pickup by (cell, hour, attempt bucket), averaged over a
  fixed attempt mix (later hours over-represent hard-to-reach leads), shrunk toward the workflow
  curve with 200 dials of prior. Weights `(p_k(h) / pbar_k)^2` clipped to [0.25, 4] (comparative
  advantage, because the dialler ranks eligible leads within an hour by weight). Shipped in the D0
  approval, refreshed at most daily. Per person: cut.
- **Caller ID: concurrent comparisons, per workflow** ([ADR 0008](docs/decisions/0008-caller-id-probing-classification-spread-drains.md)).
  DIDs are drawn per dial independently of the lead, so a DID's pickup against the pooled other
  live DIDs, stratified by (cohort, hour), is an unbiased health ratio. History classifies the DIDs
  the default used; the rest start `unknown`.
- **Exploring caller IDs:** probe share from the DID harm gate (a flagged probe must still be
  harmless: `s x 0.65 <= 1.8%`, so at most 2.8%, two unknown DIDs per change) and from volume.
  Each probe is classified by a dial-count SPRT (r = 0.70 vs 1.0), not a day timeout, which dropped
  healthy DIDs at dev volume. Measured dials to a decision: 248 / 148 / 133 / 50 at r = 1.0 / 1.1 /
  0.64 / 0.36. Healthy DIDs share load equally; a slow path catches drift into the 0.75 to 0.85
  band; a degraded DID is re-probed after 3 days.
- **Open from I2:** with the probe-only learner, one re-admission probe on public_02 was graded
  harmful (caller-ID health -2.1%), because the DID harm gate was not yet wired. [[RESULT: harmful caller-ID changes on public_02 after I3 wires the DID gate, from the I3 report]]

### Guardrail KPI

- **Definition:** assumption 1 (absolute points, rolling 2 days, concurrent comparator).
- **Before submit:** every dry run forecasts the guardrail under the new mix. Schedule and caller-ID
  changes change *who* connects, so on a KYC-like workflow (pickup primary, engagement guardrail)
  the raw engagement rate can fall with no one's engagement changing. The forecast
  `E(w) = sum_k connects_k(w) e_k / sum_k connects_k(w)` catches that. A proposal needs a predicted
  drop of at most 0 and `P(drop > max_drop) <= 2.5%`.
- **After apply:** if the lower 97.5% bound of the observed drop exceeds `max_drop` and the drop is
  attributable to our change (a concurrent comparison for allocations; for schedule and caller-ID
  changes, the mix term explains at least half), a P1 rollback follows. Unattributable drift raises
  a `KPI_DRIFT` digest item and changes nothing.
- **Power, honestly:** a 2-point pickup drop is a 3.6-SE event at dev volume, but an engagement
  guardrail on KYC is only 1.2 SE. That is why engagement experiments do not run on pickup-primary
  workflows, and why the forecast carries most of the protection there.

---

## 6. Governance

Measured so far (I2, probe-only learner, `scripts/m3_run.py` plus `python -m sim grade`), across
public_01, public_02 and the attack variants (a) TTL 12h with latency 60-480, (b) approvers 14-16
with TTL 4h, (c) a cap of 1 request a day, (d) `max_shift` 0.05: **0 auto-apply rejections, 0 lapses,
0 expiries, 0 apply conflicts; peak requests never above the effective cap.**
[[RESULT: the same four counts and peak requests per 24h with all learners on, from reports/public_01.md, reports/public_02.md and the I6 report]]

### Proposal lifecycle

```
 planner patches -> DRAFT --no net change--> DISCARDED
                      | gates ok: harm gate, guardrail, local policy replica, timing
          +-----------+----------------------------------+
          | approval route                               | auto-apply route
          v                                              v
      SUBMITTING --409 stale--> STALE                 NOTICED (notice period) --hold--> DISCARDED
          | 201                                          v
          v                                           AUTO_APPLYING --200--> APPLIED -> VERIFYING
      PENDING --rejected--> REJECTED                                --403--> FAILED_POLICY (page)
          |   --expired---> EXPIRED
          |   --higher class needs the slot--> SUPERSEDED (never applied, even if approved)
          |   --base version changed--------> STALE      (never applied)
          | approved
          v
      APPROVED --base changed-----------> STALE         (apply never called: no apply_conflict)
          |    --now > apply_by - tick---> LAPSED_LOCAL  (apply never called: no 410)
          |    --decided >20h after submit, gates now fail--> ABANDONED
          v
      APPLYING --200--> APPLIED --> VERIFYING --> VERIFIED | ROLLBACK (a new P1 proposal)

 REJECTED, EXPIRED, STALE, LAPSED_LOCAL, SUPERSEDED, ABANDONED --> REQUEUE:
   a NEW proposal: new id, fresh data, rebased on the live config, requeued_from = old id
```

- **Re-proposal:** a judgement-call rejection of routine work is re-queued once at the next slot,
  then dropped as "needs discussion". An incident or rollback is resubmitted at the next stop. A
  rejection for fatigue, evidence or format means my local check is wrong: it pages and the patch
  set is held, then re-checked automatically.
- **Proposal ids are deterministic** (`{run}-{wf}-D{d}T{HHMM}-{class}-{seq}`), so a duplicate-id
  409 after a restart is a reconciliation signal, never a second request.
- **One change in flight per workflow,** enforced by the database index, because any version bump
  (including my own auto-apply) kills a pending approval at apply. Priority classes: P0 incident
  (P0a prompt safety before P0b DID drain) > P1 rollback > P2 promotion > P3 routine. Only a
  strictly higher class may supersede, in the same transaction
  ([ADR 0013](docs/decisions/0013-one-change-in-flight-and-priority-classes.md)).

### Batching and timing

- **Batching:** one proposal per workflow carries every item, one plain-language line each
  ("Voice: try V3 on 4% of calls (new test)"). Operational auto-applies run in the same step
  *immediately before* the approval is submitted, so the approval carries the new base.
- **Expiry is computable at submit.** The decide minute is `next_working(t + L)` with L uniform on
  the latency range, fixed at request time. So `p_expire(t)` is exact from API parameters, and I
  submit only where it is at most 1%, searching the whole day, including before approvers open
  when no in-hours time is safe ([ADR 0010](docs/decisions/0010-submit-timing-and-apply-guarantee.md)).
  Examples: ks_cancel (10-19, latency 20-120, TTL 12h) is safe through 17:00, refused from 17:15;
  approvers 14-16 with TTL 4h have no safe in-hours time, so the slot is 12:00 (p = 0.47%).
- **Fixed routine slots.** P2 and P3 submit only at exact slot minutes (approver open + min(60 min,
  25% of the window), or Kestrel's 11:00; a second slot 4 hours later if the budget allows), so the
  simulator's strict rolling-24h fatigue count stays exact.
- **Fatigue:** `C_eff = min(policy cap, client cap)`; one slot is reserved for incidents when
  `C_eff >= 2`; at a cap of 1 routine work goes at most once every 48 hours
  ([ADR 0011](docs/decisions/0011-fatigue-reserve-and-fixed-slots.md)). A cap is never exceeded on
  purpose: an over-cap request is accepted, burns a slot, and is certain to be rejected.
- **Apply window:** a decision is visible only at the end of the tick after it, and the window is 2
  hours from the hidden decide time. The step rule bounds every advance while anything may be
  decided: `S_watch_max = tick x floor((W - tick + 1) / tick)` = 105 minutes for W = 120, tick 15;
  I use 90, and tick-sized steps once an incident fix could be decided. No overnight jump while a
  proposal of mine is approved but not applied.

### When I auto-apply, and when I voluntarily don't

Auto-apply only when all hold: the policy allows every changed dim; my exact replica of the
simulator's `changed_dims` and `max_shift` says it fits (with a 1e-6 margin); the client directive
permits it; the 24-hour velocity budget holds; nothing is in flight; no hold; the notice period has
passed and the harm gate still passes ([ADR 0014](docs/decisions/0014-local-policy-replica.md)).
I never probe auto-apply by trial and error: every 403 is logged and graded. The replica agrees with
the simulator on 2,100 generated bodies, bit for bit, including a float trap found in M0 (the
simulator re-normalises weights, and about 23% of normalised weight sets drift in the last digit,
which would read as a changed dim and a 403).

| Scope | Directive (only tightens the API policy) |
|---|---|
| Northwind, all workflows | No customer-facing auto-apply (welcome, voice, prompt). Cohort harm line 1%. Weekly digest. |
| Kestrel, cancellation line | 2 requests a day, never before 11:00; 60-minute notice inside approver hours before any non-incident auto-apply; customer-facing auto-apply only for ramps and risk-reducing moves; avoid "verbatim" prompts. |
| Everything else (`*`, so hidden scenarios) | Customer-facing auto-apply only for risk-reducing moves (arm removals, rollbacks, harm stops); 60-minute notice; cohort line 2%. |

"Risk-reducing" is computed from the composed config, never declared by the module that emitted the
patch: every option whose share rises is the control, or every DID that gains is healthy and the one
losing is degraded. Exploration and ramps on an unknown client go by approval, because exposure
increases are exactly what a compliance head wants to see first.

### Impact score, affected leads, dry run, and how honest they are

- **Affected leads:** active leads in cells whose effective distribution, schedule or prompt
  changes, plus the next 24 hours of new leads in those cells (next batch only on
  `next_campaign_only`). Honest but blunt: a 4% probe "affects" every lead in scope. So the dry run
  also reports `leads_hearing_a_different_variant_24h`. (I2 found every request had been sending 0;
  fixed, for example 3,698 leads for one caller-ID change.)
- **Dry run:** a stratified posterior replay. For each cell, next-24h connected mix x change in
  allocation x posterior draws of that cell's effect, giving mean, 90% interval, P(harm) and the
  guardrail under the new mix. It is model-based, not an off-policy estimate (SNIPS is cut), and it
  says so in its `method` field and lists its assumptions.
- **Impact score:** the 20th percentile of the 7-day relative change of the KPI *the lever moves*,
  clipped to [0, 1], stored with its KPI label. A probe or a first exploration scores about 0,
  honestly: it buys information. Two KPIs are never summed.
- [[RESULT: whether the dry-run replay and impact scores are wired with real posteriors, from the I3 to I5 reports]]

### Supervisor edits mid-experiment

Desired config = live config + my patches, each owning a path (`alloc/<cohort>/<dim>`, `prompt`,
`hour_weights/<cohort>`, `cohorts`) ([ADR 0015](docs/decisions/0015-supervisor-edits-overlay-and-cooldown.md)).
On a supervisor version: every pending or approved proposal on an older base becomes STALE and is
never applied; my patches on the paths they touched are invalidated; experiments on them are
interrupted (data kept, labelled by version); their value becomes the new control; a 24-hour
cooldown blocks routine work on those paths; afterwards any proposal touching them says so. The only
exception is a prompt incident, which is the "supervisor shipped an unsafe prompt" case, and its
summary says so plainly. Measured on public_02 (I2): after the D3 prompt edit our pending ks_cancel
request went STALE and was never applied; after the D5 voice edit no later version of ours touched
voice. That matters: the edit set V2, which the scenario's hidden truth makes good for Tamil speakers, so a blind revert
would have been harmful.

---

## 7. Incidents

### Detectors, thresholds and false-positive rates

| Detector | Threshold `[T]` | Expected false positives | Detection speed |
|---|---|---|---|
| Flagged caller ID (no event exists) | Pickup ratio vs pooled other DIDs, stratified by (cohort, hour). Alarm in a 2, 4 or 8h window: >= 100 dials, z <= -4, ratio <= 0.75. Suspect: 1h window, z <= -2.5 (tick-sized steps, routine submits deferred) | <= 0.012 alarms a day at dev volume, 0.12 per 10-day run (Bonferroni); measured at most 2 in 150 null days, suspects 0.2% to 1.2% | 1.5 to 1.75 h for a flag to r = 0.36 at a one-third share; measured median 1.0 to 2.25 h |
| Prompt change by someone else | FULL safety verdict on the new prompt at the next stop | P1-like safe prompt called UNSAFE about 0.3% (measured 0.2%) | Next stop, at most 60 min |
| Live flag-rate CUSUM (backstop) | Bernoulli CUSUM, p1 = 2 p0, h = ln(30 days of calls), >= 5 flags | In-control ARL at least 30 days of calls | About 460 calls for a 3.6x jump, about 1,730 for a doubling (1.4 days) |
| Unsafe business default (D0) | Sequential verdict on every default prompt; free cross-workflow flag-rate check (ratio >= 1.6 and z >= 4) escalates | As above | At D0 |
| KPI drift (sole DID, everything at once) | Hour-, attempt-, mix- and own-change-adjusted observed/expected per dialling hour, downward Page CUSUM k 0.5, h 6 | ARL0 about 2,570 blocks (214 dialling days), 0.28 false alarms per dev run; measured within 15% | 10 dialling hours for a -10% pickup drift; alert-only, never changes config |

**The safety verdict** ([ADR 0009](docs/decisions/0009-prompt-safety-verdict-and-gate-evidence.md)):
stage 1 runs 300 conversations (3 credits): 10 or more failures is UNSAFE; stage 2 adds 1,000 and
calls UNSAFE at a pooled 2%. It is separate from the client's gate (0.97), which would let a P4-like
prompt through about 69% of the time; my verdict lets it through 3% to 6% (measured UNSAFE 94%
sequential, 97% full). Approval evidence is one run designated *before* it runs; I never re-run
to fish for a pass. There is a band I do not close: failure rates of about 1.4% to 2% pass 60% to
95% of the time. Closing it would fail a safe prompt 8% of the time and remove the last-known-good
fix in 1 incident in 12.

### How I pick a fix

1. **Last known good**: the latest prompt live for at least a dialling day on this workflow, with
   no excess flags and a SAFE verdict (usually cached from the D0 screen, so no extra credits).
2. Otherwise **every catalogue prompt in a fixed rank**, decided before any validation: live
   prompts by measured engagement first, never-live prompts next, prompts matching the client's
   avoid-terms (Kestrel's "word for word") last. One `suggest-fixes` call per incident (3 credits,
   §17.1) adds candidates but never reorders: the LLM proposes, statistics decide.
3. None safe and approvable: page `NO_SAFE_PROMPT` or `CLIENT_GATE_UNREACHABLE` and change nothing
   blindly.

The fix is one P0 approval, rebased on the supervisor's version, exempt from the engagement harm
gate (a fix away from a P4-like prompt may be graded harmful; I accept that and the summary states
the engagement cost).

**Caller-ID drain:** where caller ID may auto-apply, a chain of risk-reducing auto-applies, one per
tick, each at most `max_shift`, onto healthy DIDs: 2 steps at `max_shift` 0.2, 7 to 8 at 0.05.
Otherwise one P0 approval. If both incidents come at once: finish the chain first only if it ends
within 30 minutes, else bundle the drain into the prompt-fix approval; a pending prompt-fix approval
is never staled by a drain.

### Credits

| C0 | Incident reserve R | D0 screens S | Unallocated | Mode |
|---|---|---|---|---|
| 50 | 40 | 10 | 0 | LOW: stage-1-only verdicts, no screens |
| 200 (public_01/02) | 136 | 50 | 14 | NORMAL: two worst-case incidents |
| 400 (public_03) | 136 | 100 | 164 | NORMAL |

`R = min(0.8 C0, max(0.3 C0, 2W))`, with W = 68, the worst incident at 5 catalogue prompts (a FULL
verdict on the new prompt and on every candidate, plus the suggest-fixes call)
([ADR 0021](docs/decisions/0021-credit-budget.md)). The DID path needs no credits. A 402 pages and
stops all non-incident spend. Predicted spend on public_02: about 20 for D0 screens, 3 for the
stage-1 verdict on the bad prompt, 3 for suggest-fixes: about 26 of 200.
[[RESULT: measured credits used on public_02, by pool, from the I5 report or credit_ledger]]

### Verifying a fix worked

- **Prompt fix:** sequential check on calls under the fix: passes when the flag rate's upper 95%
  bound is below 2 p0 and engagement vs the 72 hours before the incident has a lower 95% bound of at
  least -5%; fails as soon as either bound shows the fix is bad (the incident reopens and the next
  candidate goes as a new P0); otherwise waits up to 24 hours. (A one-shot check at 300 calls would
  reopen good fixes about half the time.)
- **Drain:** two dialling hours after the last step, the drained DID has no new dials and workflow
  pickup is at least 0.9 of the pre-incident 24 hours at the same hours; else the detector re-runs on
  the remaining DIDs and pages.

### public_02 timeline

**Predicted, to be replaced by the measured run.** [[RESULT: measured public_02 timeline from why-changed and the audit_log, from the I6 report]]

| Sim time | Workflow | World | Controller (predicted) |
|---|---|---|---|
| before D0 | all | 14 history days | Ingest, hour curves, cohort grids, DID states, flag baselines, D0 prompt screens (about 20 credits) |
| D0 00:00 to 11:00 | all | | Caller-ID probes at 2.8%; first approvals per workflow (cohorts, hour weights, probes, welcome and voice arms at 3.8%, or 1.9% on Northwind) |
| D1 | renewal, cancel | | Probes classified healthy after about 150 dials; spreads (beneficial on caller-ID health) |
| D2 10:00 | ks_cancel | D2 flagged (hidden), share about 0.39 | 11:00 suspect; 11:45 alarm; drain in two auto-apply steps by 12:00. Flag-day share about 0.092, then 0 |
| D3 11:30 | ks_cancel | Supervisor ships a new prompt | Our pending routine request goes STALE |
| D3 12:00 | ks_cancel | | FULL verdict, stage 1 fails (96% likely), 3 credits; suggest-fixes, 3 credits; last-known-good P1 with its D0 run as evidence; P0 approval in the same step |
| D3 12:45 to 14:15 | ks_cancel | Approver decides | Applied at the first stop after visibility: **MTTR 75 to 165 min, mean about 122** |
| D3 about 15:00 onward | ks_cancel | | Fix verified on calls under P1 |
| D5 15:00 | nw_renewal | Supervisor sets default voice V2 | Seen at the 16:00 stop; voice experiment interrupted; pending proposal STALE; 24h cooldown; never reverted |
| last 1.5 days | all | | Freeze: no new arms or probes; safety, rollbacks, drains continue |

---

## 8. Scale

**Measured so far, public_03 with a no-op decide stage** (T1, `scripts/load_test.sh`, Apple Silicon
Mac, Docker Desktop with 11 CPUs and 8 GB; [reports/public_03_load.md](reports/public_03_load.md)):

| Metric | Value |
|---|---|
| Events | 9,737,591 in 93 chunks, ingested count equal to `/sim/state`; rollups equal a from-scratch recomputation (0 mismatches) |
| Wall time per sim day | **32 s** mean, 34 s worst (simulator advance 5.8 s, ingest about 26 s) |
| Ingest rate | 26k to 37k events/s per transform transaction (design estimate was 40k to 80k) |
| History bootstrap | 5.5M events, 2.35 GB of NDJSON in 152 s |
| Peak memory | controller 104 MB, Postgres 1.75 GB, simulator 306 MB |
| Run schema size | 1.04 GB (`lead_dim` 591 MB, `call_fact` 84 MB per day) |

With the full loop: [[RESULT: wall time per sim day, ingest events/s, end-to-end decision lag per step (advance return to end of GOVERN) and peak controller and Postgres memory on public_03 with all learners on, from reports/public_03_load.md re-run after I6]].
Grading box note: [[RESULT: the same numbers on a 4 vCPU / 8 GB Linux box if one is available, else state that only the Mac was measured]].
SRE's bar is 600 s per sim day; the no-op loop leaves about 85 s of a 120 s internal target for the
decision stages. Cost I already know: `load_snapshot` runs per workflow every step, and the
allocation plan costs 1.3 to 1.6 s per workflow per planning point at public_03 scale.

**What breaks first at 10x** (8.4M events per sim day, §13.2):

| Order | What breaks | Why | Fix |
|---|---|---|---|
| 1 | The per-chunk lead join and lead-state updates | Already the measured bottleneck: ingest slows as `lead_dim` grows; at 10x that is 23M leads, about 3.8 GB with indexes, random I/O | Partition `lead_dim` by campaign day and join only campaigns inside the cadence window; drop old `call_fact` partitions after the audit horizon |
| 2 | Serial SQL transform | About 26 s a day now, about 4 to 5 minutes at 10x | Loader splits lines by `hash(workflow_id) % P` into P staging partitions (byte search, no JSON parse); P backends transform concurrently |
| 3 | Serial per-workflow decide | 120 workflows x about 15 steps | Process pool by `hash(tenant) % W`: snapshots are pure reads, and proposals are already isolated per workflow by `one_in_flight` |
| 4 | Disk | about 10 GB of database | Retention and partition drops |
| 5 | Credits and approver attention | Many tenants share an ops team | C15 properly: per-tenant budgets and a fair queue for approval slots |

**Partitioning across workflows and tenants.** In production there is no `advance`; events arrive
as a stream. Each worker owns a set of tenants through a lease row (an advisory lock per partition),
consumes that partition's stream into its own staging, and runs detect, decide and govern only for
its workflows. Governance and audit tables are already keyed by workflow, so nothing is shared
across partitions except read-only views and the credit pool.

---

## 9. Reliability

### Method

- **Write-ahead intents** (§8.2): transaction 1 writes the decision row, the intent row and the
  "before" transition (for example DRAFT to SUBMITTING); then the HTTP call; transaction 2 writes the
  result and the domain transition. A crash anywhere leaves a `started` intent that is resolved
  before any new side effect.
- **Idempotency keys:**

| Call | Key | On a crash or timeout |
|---|---|---|
| chunk ingest | `chunk_no`, committed with the facts | Partial attempt rolled back; re-fetch, skip if present |
| `POST /sim/advance` | `adv:<from>-><to>` | **Never blind-retried** (advance is not idempotent): read `/sim/state`; at `from` it never ran, at `to` it did |
| `POST /approvals` | deterministic `proposal_id` | Look it up in `GET /approvals`; found: adopt it; not found and preconditions still hold: resend the same id and body |
| auto-apply `PUT /config` | deterministic `proposal_id` | Look it up in `config/history`; ids are consumed only on success |
| `POST /apply` | `approval_id` | Retry is safe: an applied approval returns 409, it cannot apply twice; re-check `apply_by` and base first |
| validation, suggest-fixes | intent row | No idempotency in the API: resolved from the credit balance (§8.3) |

- **Determinism** (§8.6): no wall clock or unseeded randomness in decisions, seeds derived from
  decision ids, sorted iteration, deterministic proposal ids. So a restarted run issues the same
  calls at the same simulated minutes as a clean run, and the test can demand equality.
- **Fault injection:** `CTL_FAULT=<kind>:<n>:<phase>` exits as if killed the n-th time a boundary is
  reached. Kinds: advance, submit_approval, auto_apply, apply, validation, suggest_fixes,
  chunk_copy, chunk_transform, bootstrap, audit_write. Phases: before_t1, after_t1, after_http, in_t2.
- **Crash-equivalence matrix:** for public_02, one clean run, then one run per (kind, phase, n), each
  restarted as `run.sh` does. Assert the simulator's config history, approvals, credits and grade are
  identical to the clean run.
- **Random kill loop:** `scripts/kill_test.sh`: SIGKILL the controller container at random
  intervals, `run.sh` restarts it; assert 0 lapses, 0 conflicts, 0 duplicate applies, no pending
  approval left unresolved, ingested events equal `last_event_id`.

### Results

Measured so far: the platform kill test on public_01 (4 injected faults including mid-COPY and
inside a transform, plus 4 random SIGKILLs, 8 restarts): same 121 advances in the same order, all
297,003 events in 113 chunks, 16 compared tables identical ([reports/kill_test.md](reports/kill_test.md)).
T1 added 17 crash points and I2 added the 18 governance cells (submit, apply, auto-apply at every
phase); all equal the clean run.

Final: [[RESULT: crash-equivalence matrix on public_02 with all learners on, pass or fail per cell, from reports/crash_recovery.md]];
[[RESULT: kill_test.sh public_02 with 20 random SIGKILLs: restarts, invariants, grade diff against the clean run, from reports/crash_recovery.md]].

Bugs this method found, each reproduced end to end and re-run after the fix:

- **A crash changed the simulated world.** Postgres `jsonb` re-orders object keys; the resent
  request listed caller-ID weights in another order; the simulator draws each dial in the order
  options arrive, so every later dial changed. Request bodies are now stored as `json` (exact text).
- **A restart mid-step skipped the rest of that minute's work**, and re-running the whole step was
  also wrong. Fixed with per-stage markers.
- **A fault firing while another was armed hung the process mid-COPY** (found by the kill test). The
  load test separately found that deleting staged rows left 0.6 GB of dead rows; the stage is now
  truncated after each batch.

### What state is lost

- **A validation or suggest-fixes call killed after the HTTP call:** its id and result are lost
  (credits are spent). Recorded as `lost_validation_run` and re-run once if still needed; listed in
  the evidence, so it is not cherry-picking. Validation ids are global and seed the outcome, so
  every later validation in the scenario differs from the clean run: those cells assert invariants,
  not equality.
- **The governance step's per-workflow summary decision row** for the workflow in progress at the
  kill. The side effect's own decision row is always written before its call.
- Nothing else: learned state, CUSUMs, looks, pending approvals and the ingest cursor are committed
  every step.

### Simulator 4xx and 5xx

| Response | Behaviour |
|---|---|
| 5xx, timeout, refused (GET) | Backoff 0.5 s to 30 s with jitter; page after 5 minutes; do nothing that needs the simulator |
| Timeout on advance or another POST | Reconcile by key (above), never blind-retry |
| 400 `bad_minutes`, 422 | A bug in my code: page, stop advancing or mark the proposal FAILED |
| 402 | Page, stop non-incident spend; the DID path continues |
| 403 on auto-apply | My replica is wrong: page, disable auto-apply for that workflow, re-route to approval, never retry the body |
| 409 stale / duplicate / not_approved | STALE and rebase / reconcile / already applied |
| 410 lapsed | Should be impossible (I check `apply_by - tick`): page, re-queue |
| 409 finished | Final ingest and reconcile, exit 0 |

---

## 10. Audit & observability

**"Why did workflow X change at time T, who approved it, and what happened after?"**

```bash
python -m controller.audit why-changed --workflow ks_cancel --at "D3 14:00"
```

It runs this query against the run schema (§9.4), then follows the `caused_by` chain:

```sql
WITH v AS (SELECT cv.* FROM config_version cv JOIN workflow w USING (wf)
           WHERE w.workflow_id = :wf AND cv.applied_minute <= :t
           ORDER BY cv.applied_minute DESC LIMIT 1)
SELECT v.version, v.applied_minute, v.actor, v.changed_dims,
       p.proposal_id, p.class, p.change_summary, p.rationale, p.impact_score, p.dry_run, p.verify,
       a.approval_id, a.created_minute, a.decided_minute, a.status,
       d.rule_id, d.inputs, d.harm_report              -- the statistics at decision time
FROM v LEFT JOIN proposal p ON p.proposal_id = v.proposal_id
       LEFT JOIN approval_mirror a ON a.approval_id = v.approval_id
       LEFT JOIN decision d ON d.decision_id = p.decision_id;

WITH RECURSIVE chain AS (            -- what happened after: verification, harm stops, rollbacks
  SELECT * FROM audit_log WHERE subject_type = 'proposal' AND subject_id = :proposal_id
  UNION ALL SELECT a.* FROM audit_log a JOIN chain c ON a.caused_by = c.audit_id)
SELECT sim_minute, actor, action, summary FROM chain ORDER BY audit_id;
```

Output on the finished public_02 run:

```
[[RESULT: verbatim output of why-changed --workflow ks_cancel --at "D3 14:00" on the final public_02 run, from the I6 report]]
```

"Who approved it": the approval id and decide time. The API never returns the approver's name; the
column exists for the client's own system to fill. Related commands:

```bash
python -m controller.audit why-heard --lead <lead_id> --day 3       # IRDAI: per call, per dim, with provenance
python -m controller.audit replay-decision <decision_id>            # recompute a gate from stored inputs and seed
python -m controller.audit weekly-digest --tenant northwind_life    # Compliance's weekly summary
```

`why-heard` takes welcome, voice and caller ID from the version the call was dialled with (on
`next_campaign_only`, the campaign's locked version) and the prompt from the version live at the
call's minute; a call with no provenance row is reported, never dropped. `replay-decision` loads
the stored posterior inputs, seed, settings hash and code version, recomputes, and prints stored and
recomputed values side by side ([ADR 0026](docs/decisions/0026-decision-replay.md)).
[[RESULT: replay-sample --n 20 result on the final public_02 run (how many decisions replayed exactly), from the I6 report]]

**Loop health** is `GET /ops`, from the `loop_step` table: ingest lag (simulated and wall), decision
lag, wall time per sim day, decisions per sim hour by module (including "no action"), credits and
burn by pool, requests used per 24h vs the cap, governance invariants (403s, lapses, expiries,
conflicts, orphan calls, cohort mismatches), RSS and database size, heartbeat age.

| I page on (each links `docs/RUNBOOK.md#<code>`) | I do not page on (morning digest) |
|---|---|
| `INGEST_STALLED`: a listed chunk not committed for 5 wall minutes | Approver rejections and expiries (handled automatically) |
| `APPLY_AT_RISK`: an approved proposal within one step of `apply_by` | Harm-gate blocks and "did not act" decisions |
| `GOV_INVARIANT`: any 403, 410, 422, fatigue or evidence rejection, apply conflict | Decay alarms that cleared, futility drops, arm removals |
| `CREDITS_BELOW_RESERVE`, any 402, a switch to LOW mode | `KPI_DRIFT` (0.28 false alarms per dev run; 2.8 a day at 300 workflows) |
| `INCIDENT_UNRESOLVED`: no fix submitted in 2 sim hours or unresolved after 6 | Supervisor edits and interrupted experiments |
| `NO_SAFE_PROMPT`, `CLIENT_GATE_UNREACHABLE`, `NO_HEALTHY_DID`, `NO_SAFE_WINDOW` | Retries that succeeded, held patches released |
| `NEW_WORLD`, `UNKNOWN_WRITER`, `COHORT_MISMATCH`, `CRASH_LOOP` | Fatigue waits on a cap-1 workflow (I2 found these were paging falsely) |

Never: single-call anomalies, routine submissions, auto-applied schedule refreshes.

---

## 11. Results

What I predicted before running (§7.4), so the results can be judged against it: 0 harmful changes,
0 rejected auto-applies, lapses, expiries or apply conflicts; nw_kyc pickup clearly up from about
D4 and renewal and cancel pickup up from D1; modest engagement lift at dev volume; one hallucination
incident fixed in 75 to 165 minutes; about 26 credits.

### public_01

```
[[RESULT: summary table pasted from reports/public_01.md]]
```

### public_02

```
[[RESULT: summary table pasted from reports/public_02.md]]
```

### Interpretation

- **Where it helped:** [[RESULT: per-workflow primary lift and which lever produced it (caller ID, call time, welcome/voice), from reports/public_01.md, reports/public_02.md and why-changed]]
- **Where it was harmful, and why:** [[RESULT: every change graded harmful, with its decision row and cause, from the grade reports and the audit store]]
- **Incidents:** [[RESULT: hallucination MTTR, flagged-DID day share on the flag day and after, decay share, from reports/public_02.md]]
- **Against the prediction:** [[RESULT: where the measured run differed from the 7.4 timeline and why]]
- **What I would change:** [[RESULT: lead to write after reading the results]]

For reference, the I2 interim run with only the probe learner (caller-ID probes and spreads, hour
weights) gave public_01 ALL pickup +7.5% with 0 harmful in 16 changes, and public_02 ALL pickup
+20.1% with 1 harmful caller-ID probe in 22 changes and ALL engagement -1.6% (no incident response
yet, so the unsafe prompt stayed live).

---

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Fixed split (50/50, or 10% per challenger) | Standard, easy to explain | 10% on each of 5 challengers fails the harm gate on D0, and one x0.88 arm makes the launch about -1.2%, graded harmful. Instead: harm-gated probability matching. |
| Plain Thompson sampling | The textbook bandit | Random per decision (winners must be deterministic), moves traffic every step (each move is a governance action), no bound on per-change harm. Instead: Thompson's allocation rule as a seeded, deterministic expectation. |
| One challenger per dimension at workflow level, in listed order | Fewer comparisons, simple | Sign-flipping cohort effects average to null at workflow level, and late-listed options are tested last. Instead: factorial, all arms at once, cohort effects from feature terms. |
| One winner threshold (0.99) for every scope | Simpler | 1.28% false winners per population hypothesis and 2.8% per cohort under unequal sizes. Instead: 0.995 and 0.998. |
| Population-only harm gate | Mirrors the grader exactly | Lets a cohort fall below -1% while the population looks neutral (Compliance, CTO). Instead: plus a per-cohort line, 2% default, 1% for Northwind. |
| Relative guardrail reading | Literal reading of `max_drop` | 0.5 points of pickup is unmeasurable at dev volume, so unenforced. Instead: absolute, plus "no predicted drop". |
| Day-based caller-ID classification timeout | Simple | Dropped healthy DIDs at dev volume (415 dials needed at r = 1.0). Instead: dial-count SPRT. |
| Never calling `suggest-fixes` | It can only return catalogue prompts whose summaries are already free | README C8 describes "LLM suggest". Instead: one call per incident (3 credits from the reserve); suggestions join the candidate list, a fixed statistical rank decides. |
| Re-running validation until one run passes | The approver accepts any single passing run | Cherry-picking. Instead: one designated evidence run per decision, chosen before it runs. |
| Pooling prompt verdicts across workflows | Saves credits | The API never says prompts are shared; relying on it is tuning to the simulator. Instead: per-workflow verdicts. |
| Chained exposure-increasing auto-applies | Faster ramps and spreads | Walks around `max_shift`, the client's signed limit. Instead: a 24h velocity budget; only risk-reducing incident steps chain. |
| SNIPS off-policy replay as the dry run | Unbiased | High variance at dev volume and a large build. Instead: stratified posterior replay, labelled as model-based. |
| SQLite, Redis, LangGraph | Less infrastructure, or a ready-made state machine | Node-local state, weaker durability, checkpoint state duplicated outside the audit transaction ([ADR 0001](docs/decisions/0001-stack.md)). |
| `jsonb` for stored request bodies | Postgres default | Re-orders keys, which changed the simulated world after a crash. Instead: `json`. |

---

## 13. Known weaknesses

[[RESULT: re-check every item against the I3 to I6 reports; delete the ones that were fixed, keep the rest]]

1. **Engagement learning is slow at dev volume.** Population winners are possible from about D4,
   cohort winners only for cohorts of about half the traffic and only from about D6; Northwind's 1%
   cohort line delays both by about 1.5 days. Most dev-scale lift comes from pickup levers.
2. **A validation band my bar does not close:** prompts failing 1.4% to 2% of validation
   conversations pass my verdict 60% to 95% of the time. The live flag CUSUM is the backstop and
   needs about 1.4 days for a doubling.
3. **A strict client gate makes safe fixes unreliable:** at g = 0.99 a safe prompt is approvable on
   one designated run about 72% of the time; otherwise the next candidate, or the incident stays open.
4. **A DID drain can wait behind a pending prompt fix** for up to the approver latency plus two
   ticks (8.5 hours at a KYC-like 480-minute latency), because a P0a approval is never staled.
5. **The flagged DID's flag-day share is not bounded below 0.10:** 0.079 to 0.109 at `max_shift` 0.2,
   up to 0.146 at 0.05, depending on the share it carried before the flag. No faster drain exists
   inside the policy.
6. **KYC-like spreads arrive late:** probes reach only new campaigns, so a fresh DID is classified
   around D4 and fully live about three days after its approval.
7. **At a cap of 1 request a day, an incident can wait up to 24 hours** for the slot my routine
   request holds.
8. **No interaction test and no regrid** (assumption 2): a welcome x voice interaction or off-grid
   heterogeneity is learned only through main effects and existing cohorts.
9. **The winner threshold alone exceeds its bound at load volume** (0.73% vs 0.54%) unless
   `look_max` caps looks; the full rule measured 0% (population) and 0.07% (cohort).
10. **Decay at dev volume is seen 1.5 to 2 days after onset,** and null arms get about 6% false
    decay alarms per 10-day run (the rollbacks they caused were neutral in tests).
11. **The KPI drift detector is slow at dev volume** (10 to 20 dialling hours for a 10% drop) and
    alert-only; its mix-versus-within decomposition was not computed at the T4 report.
12. **Hour curves come from history biased by eligibility;** per-person timing is cut. Schedule
    verification lacks the DID-adjusted workflow pickup bound.
13. **A safety fix away from a P4-like prompt may be graded harmful;** accepted and disclosed.
14. **"Named human approval" is an approval id and a time;** the supervisor view is JSON only, with
    no authentication; holds are its only write.
15. **Ingest is slower than designed** (26k to 37k events/s vs 40k to 80k) and slows as `lead_dim`
    grows; it is the first thing to break at 10x.
16. **Untested on the target platform:** Linux host networking in `compose.linux.yml` and the
    vendored-wheel build were never run end to end on Linux; every number here is from a Mac.
17. **At I2, verification and the late-decision harm re-check were not wired** (applied proposals
    stayed VERIFYING, the re-check always passed), and the probe learner produced one harmful
    re-admission probe on public_02.

---

## 14. If this went to production at Formi next month

In order (§13.5):

1. **Shadow mode on three real workflows** (days 1 to 3): consume the production event stream into
   the same schema, run detect and decide, write every decision and would-be proposal to the audit
   store, submit nothing. Compare dry-run predictions and harm-gate probabilities with what the
   business default then did.
2. **Replace `advance` with the stream** (days 2 to 5): the per-tenant partition and lease design of
   section 8, ingest and decision lag on `/ops`, and the intent protocol against the real governance
   API (no `advance`, so reconciliation keys change).
3. **Differential test of the policy replica** against the production governance service in staging
   (days 3 to 5); any disagreement blocks auto-apply.
4. **Re-derive every `[T]` threshold at real volumes** with Product (days 4 to 6), and review the
   per-cohort harm lines with each client's compliance contact.
5. **Turn on caller-ID health for one tenant** whose policy allows caller-ID auto-apply (days 6 to
   8), with SRE on call, runbooks live and paging routed.
6. **Ask the platform team for approver identity** in the approval API and wire it (days 6 to 10);
   until then the gap stays in Northwind's weekly digest.
7. **Routine approvals on one engagement-primary workflow** with a willing client (days 8 to 10),
   reviewed daily with its supervisor.
8. **Prometheus exporter and dashboards** from `loop_step` (days 8 to 10), replacing `/ops` polling.
9. **Game day** (days 11 and 12): kill the controller and fail over Postgres in staging under load;
   compare with the crash matrix.
10. **Week-2 review** (days 13 and 14): shadow-mode calibration, false alarms per detector, credits
    per tenant, and the go or no-go for the next tenants.
