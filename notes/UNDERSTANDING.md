# Understanding the assignment

Formi "Outcome-First Agent Loop", senior engineer take-home. Repo cloned from
`github.com/AryamannNingombam/sde-assignment-2026` (one `init` commit).

This file was built by reading every file in the repo, then cross-checked by four independent
analysts (governance, outcomes/grader, stakeholders, systems), one skeptic per analyst trying to
refute each claim against the code, and a completeness critic. Line references point at the code
that backs each statement. Nothing here has been executed yet: running the simulator still needs
permission.

> Rule that governs everything below: our **controller** may only know what the HTTP API returns.
> Reading `runs/`, importing `sim`, or parsing scenario YAML is disqualifying (README.md:84-87).
> The hidden truth listed in section 7 is for sanity-checking only and must never be coded in.

---

## 1. What we are building, in one paragraph

An always-on **controller** service that drives a closed-loop simulator over HTTP. For every
workflow it ingests call events, detects problems on populations (pickup or engagement drift, a
caller ID going bad, hallucination spikes), runs statistically sound experiments on welcome message,
voice, caller ID, call timing and prompt, packages changes as proposals with evidence, routes them
through governance (approval or policy-permitted auto-apply), applies what is approved, keeps
measuring, rolls back what decays, and records every decision in its own queryable audit store.
`./run.sh <SIM_URL>` must start it and run to scenario end with no manual steps.

## 2. How we are graded

| Weight | What | Notes |
|---|---|---|
| 25% | Outcomes on **2 hidden scenarios** | Lift vs no-op baseline on the primary KPI, guardrail respected, harmful changes, incident time-to-fix, decay rollback, governance hygiene. No credit for lift that breaks a constraint. |
| 25% | Design and reasoning | Statistical soundness, cohort sizing, false positives, scale envelope. |
| 20% | Scope and stakeholder judgement | Choosing, saying no, catching the memos' false claims. |
| 20% | Engineering | Correctness, crash safety, idempotency, load throughput, tests, ownable code. |
| 10% | Communication | Can a new engineer run, debug and extend it from the docs alone. |

Then a 60-minute review: explain a hidden-scenario decision **live from our audit store**, a
constraint gets changed and we say what breaks, and they ask about what we cut.

**Floor (mandatory):** public_01 and public_02 run end to end with no manual steps, an audit trail
(C12), none of the 8 constraints broken, `SUBMISSION.md` complete.

**The 8 non-negotiable constraints** (README.md section 5): governance boundary absolute and
supervisor edits never silently overwritten; business owns whether/when/how many to call (cadence,
window, capacity); pickup and engagement never blended, optimise `primary` and respect `guardrail`;
winner selection statistical and deterministic (LLM proposes, never decides); act on populations,
threshold-based; every decision auditable from our own store; credits finite (running out
mid-incident is a failure); crash safety (kill anywhere, resume, never double-apply, never lose a
pending approval, never forget what was learned).

**Deliverables:** controller + `run.sh`; `SUBMISSION.md` from the template (keep every heading);
`reports/` with grade output for public_01 and public_02 plus public_03 load numbers; crash-recovery
evidence (script or log); honest small-commit history; optional video of 5 minutes or less.

## 3. The world model: which lever moves which KPI

Pickup per dial = `min(p0 x hour_profile[archetype, hour] x DID_health, 0.95)` (world.py:845).
Engagement given connect = `min(e0 x welcome_mults x voice_mults x prompt_engage_mult x (1 - 0.5 x halluc), 0.95)` (world.py:849-857).

| Lever | Moves | How it really works | Size (public truth) |
|---|---|---|---|
| **caller_id** allocation | pickup only | Health = `base / (1 + (load/l0)^2)`, `l0 = 30 x dials_per_hour`, load decays with tau = 2 days (world.py:244-245, 666-668). Steady state load/l0 is about `0.8 x share` for a 12h window. One DID 0.61, 50/50 pair 0.862, 3 DIDs 0.934, 4 DIDs 0.962. | nw_kyc on one DID: health 0.69 to 0.95 with three DIDs, **about +38% pickup on its primary KPI**. Renewal and cancel: +8 to +12%. |
| **schedule.hour_weights** (per cohort) | pickup only (who is dialled, not how many) | Hidden archetypes morning/lunch/evening/flat, strongly predicted by age band (60+ about 59% morning, 18-30 about 52% evening) (world.py:724-733). Gumbel top-k weighted selection. | Up to +60% for a person dialled at their peak. |
| **schedule.lead_preferred_hour** (per person) | pickup only | x6 in the preferred hour, x0.25 elsewhere, cap 200k entries (world.py:800). | Thin data per person; mostly helps returning people. |
| **welcome_message, voice** allocations (per cohort) | engagement only | Hidden multiplicative effects, often cohort-specific and sign-flipping, some decay. | +3% to +15% per lever. |
| **prompt_version** (one per workflow) | engagement and hallucination | `engage_mult x (1 - 0.5 x halluc)`. | P3 +3.1% vs P1. P4 +5% but unsafe. |
| **cohorts** | defines the targeting unit | Up to 20, first match wins, AND across features, OR within a list. | Needed for any per-cohort allocation or schedule. |

Things that do **not** matter for any graded KPI: hour of day, caller ID and schedule have no
effect on engagement; `value_band` only drives "interested", and interested and not-interested both
count as engaged (world.py:736, 869, 881-882).

## 4. Governance mechanics and the traps in them

**Lifecycle:** `pending -> approved | rejected | expired`, then `approved -> applied | lapsed`.

- **The outcome is decided at request time** (world.py:461-494). All rejection reasons, the
  judgement-call coin flip, the decision time and the expiry time are fixed when we POST. A later
  validation run or a fatigue window rolling off cannot rescue a request. Reasons are revealed only
  once it is decided.
- **Fatigue is a rolling 24h count of every request record**, whatever its status, including
  rejected and expired ones (world.py:477-481). Caps: renewal 3, KYC 2, cancel 4. A request over the
  cap is accepted as pending and rejected later, still burning a slot. Synchronous 4xx errors create
  no record.
- **Expiry is decided at submit:** it expires if `next_working(now + latency) > now + TTL`. Renewal
  and KYC (24h TTL) never expire. **ks_cancel (12h TTL, approvers 10-19):** a request sent from
  19:00 to 21:59 always expires, and from about 16:45 to 19:00 it may expire. The naive example
  requests at 20:00, so every ks_cancel request it sends expires.
- **The apply window is only 2h, counted from the hidden decide time**, and a decision becomes
  visible only at the end of the tick after it. Safe rule: advance step of at most 105 minutes while
  anything is pending in approver hours, and always poll and apply before an overnight jump.
  Every possible `apply_by` falls at or before 20:59.
- **Lapse is lazy:** an approval past `apply_by` still shows `approved` forever, and turns `lapsed`
  (410, counted by the grader) only when we try to apply it. Check `apply_by` before applying.
- **base_version is checked twice**, at request and at apply. **Any version bump in between kills
  the approval** (409 plus a logged `apply_conflict`). That includes **our own** auto-apply on the
  same workflow. So: one change in flight per workflow, and no auto-applies while an approval is
  pending.
- **proposal_id** is one global namespace across workflows and both paths. On approvals it is
  consumed even if the request will be rejected. On auto-apply it is consumed only on success, so a
  403 can be retried with the same id. Applying an already-applied approval returns 409, so a retried
  apply cannot double-apply.
- **Auto-apply** needs every changed dim in `allowed_dims` and every cohort's
  welcome/voice/caller_id total-variation shift at most `max_shift`. TV is unweighted, so a tiny
  cohort counts as much as the whole population. **No public policy allows `cohorts` or
  `prompt_version` to auto-apply**, and hour_weights may only reference cohorts already defined, so a
  cohort-specific schedule needs a cohort approval first. Every 403 is logged and counted against
  governance hygiene, so re-implement the checks locally and never probe by trial and error. The
  auto-apply path also skips validation evidence, fatigue and approver hours.
- **changed_dims quirks:** cohort comparison is order-sensitive; weights are normalised (so
  `{D1:2,D2:2}` equals `{D1:.5,D2:.5}`); any change to `lead_preferred_hour` counts as a `schedule`
  change.
- **mid_flight `next_campaign_only` (KYC):** cohorts and allocations, caller_id included, are locked
  to the version live when each 08:00 campaign was created. Approvals land 10:00-18:00, so an
  approved KYC allocation change first reaches the next morning's batch and is fully live about 3
  days later (cadence has 3 days). Prompt and schedule apply at once.
- **Validation:** expected pass rate = `1 - 0.9 x halluc - 0.005`; the gate is 0.97. P1, P3 and P5
  always pass. **P4 "persuasive" passes about 69% of runs** even though the grader treats any prompt
  with halluc above 0.01 as unsafe. P2 passes about 23% at n=50. The approver accepts any single
  passing run, so re-running until one passes is possible, but it is cherry-picking and we should
  say we refuse to do it.
- **Supervisor edits** bypass governance, bump the version and emit `config_changed`
  (actor `supervisor`). A re-proposal built from our old desired config would silently revert them,
  so re-proposals must be rebased on the new config.
- The API never names the approver, only `approval_id` and `decided_at`.
- The public `/workflows/{id}` exposes the full governance block (including `base_reject_rate` and
  `min_validation_pass_rate`), options, objective, mid_flight and the prompt catalog **summaries**.
  It hides halluc, engage_mult, effects, base rates and DID health.

## 5. How the grader judges changes

- Every controller change is judged **at apply time** against hidden truth, on a sample of active
  leads (world.py:634-655, grader.py:67-73):
  **harmful** if expected engagement falls more than 1% **or** expected DID health falls more than
  2%; beneficial if either rises by more than that. Harmful is tested first.
- Schedule-only and caller-ID-only changes have zero engagement delta. Schedule changes are always
  "neutral" even when they raise pickup.
- **Probing a bad DID can be graded harmful:** 10% onto a 0.55-health DID is -3.6% DID health. A
  5% probe offset by spreading over a healthy third DID in the same change grades beneficial.
- **Exploration arms are applied changes too.** A 20% arm on a 0.90 variant is about -2%, so
  harmful. Cohort-targeted arms dilute over the whole population.
- **engagement_gap_captured** = our expected engagement vs the business default, divided by the
  oracle gap. A supervisor prompt edit resets the default's prompt to theirs. The oracle keeps
  crediting a decaying variant at full strength until it has been dialled, so never trying W3 caps
  the captured gap near 50% on nw_renewal.
- **Incident MTTR** counts only supervisor prompt edits to a prompt with halluc above 0.01, until the
  first later version with a safe prompt. A `did_flagged` incident emits **no event**; we must detect
  it from pickup by caller ID. The grader reports the flagged DID's daily share.
- **Decay metric:** share of in-population dials still on the decaying variant per day, counted from
  decay **onset** (when the multiplier starts falling, not when it falls below baseline). The decay
  clock starts at the first dial ever made with that variant, in any cohort, at any share. Rolling
  back exactly at onset is itself graded harmful because the variant is still at full strength then.
- The grader does **not** check guardrails. The rubric does, and the guardrail definition (2 points
  absolute or 2% relative, over what window) is ambiguous. We must state ours.

## 6. Events, scale and crash safety facts

- Time moves only on `POST /sim/advance` (multiples of `tick_minutes`, read it from `/sim/state`).
  **Advance is not idempotent:** a client timeout plus blind retry skips simulated time.
- Chunks are written at 250k events or at the end of every advance. Numbering is global across
  `history` and `events`; `event_id` is global and strictly increasing; chunks stay on disk and can be
  re-fetched, so ingestion can be idempotent by chunk number. Chunk downloads do not take the sim's
  global lock, so we can fetch chunk k while the next advance runs.
- Event `ts` is not monotonic within a chunk (approval_decided uses the decide minute; call ts is
  randomised per tick). Order by `event_id`.
- Leads from the last 2 history days keep being dialled on D0 and D1. Their features exist only in
  history chunks, so we must ingest history to join features.
- Volumes: public_01/02 about 13.7k events/day and one history chunk. **public_03 about 840k
  events/day (648k dials), 7 history days of about 5.9M events (about 2.7 GB NDJSON), about 380 MB per
  sim day.** SRE wants one sim day ingested and decided in under 10 wall-clock minutes on 4 vCPU /
  8 GB, and the simulator's own single-threaded Python loop shares that box.
- Reconciliation data is thin: `GET /approvals` has no proposed_config; config history has no past
  config bodies. We must persist proposal payloads and configs ourselves.
- If the **simulator** restarts it is a new world (ids restart, no run id in `/sim/state`), so we
  should fingerprint the world and namespace our store by it.
- Connected calls per day (default config): nw_renewal about 900, nw_kyc about 500, ks_cancel about
  1,200, each public_03 workflow about 13.5k.

## 7. Public-scenario truth (sanity checks only, never coded in)

**public_01 (8 days):** nw_renewal: W2 for `hi` x1.10, V3 x1.22 for 60+ but x0.88 for 18-30, W3
x1.15 decaying to 0.93 after 3 days, D4 already spam-flagged (0.55). nw_kyc: W2 x1.08, V2 x1.12
for `en`, single DID (the big pickup lever). ks_cancel: V4 x1.15 for ages 18-45, W2 x1.06 for tier
1, W3 x0.90.

**public_02 (10 days)**, signs flip: nw_kyc W2 is now x0.92; nw_renewal V4 x0.90, V2 x1.14 for `ta`,
W3 x1.18 decaying faster. Incidents: ks_cancel D2 flagged at D2 10:00 (health about 0.30); Kestrel
supervisor ships P2 (halluc 0.055) at D3 11:30; Northwind supervisor sets default voice to V2 at
D5 15:00 mid-experiment (V2 is actually good for `ta`, so blindly reverting it would be harmful).

**public_03 (5 days, 12 workflows):** four tenants, each with a renewal, KYC and cancel workflow,
truth mirroring public_01. Its point is throughput.

Lesson for hidden scenarios: effects flip between scenarios and may include cross-workflow `*`
effects, so no priors carried over from public truth.

## 8. Statistical power is the real limit

- nw_renewal yields about 970 connected calls a day. A +10% lift inside a 50% cohort, split 50/50,
  needs about 1,260 connected calls per arm (alpha 0.05, power 0.8), about 5 days. The same effect
  diluted over the whole population needs about 10 days, longer than the scenario.
- So each workflow affords maybe one or two confirmed winners. Cohort design is compression: there
  are 252 feature cells against a 20-cohort cap.
- Welcome, voice and caller_id are sampled independently per dial and the engine has no interaction
  terms, so a factorial design (test welcome and voice on the same traffic) roughly doubles power.
  We should state the no-interaction assumption.
- History (14 days) ran entirely on the default config: it gives baselines and per-feature hour
  curves, but no variant information. Hour curves are biased by eligibility (later hours
  over-represent hard-to-reach leads).
- Before/after comparisons are biased (08:00 batch effect, DID load cycling, attempt-number
  selection). Prefer concurrent comparisons within the same hour and attempt stratum.

## 9. Stakeholder memos vs the actual contract

| Memo | Claim or ask | Verdict |
|---|---|---|
| 01 Sales | Kestrel "turned on auto-apply for everything" | **False.** Only schedule, caller_id, welcome, voice, with max_shift 0.2. Never prompt or cohorts. Kestrel KYC in public_03 has it off. |
| 01 Sales | Northwind fine with auto-apply on KYC | **False.** KYC auto-apply is disabled everywhere, and Northwind's own Compliance requires human approval. |
| 01 Sales | One blended score per client | **Rejected.** Constraint 5.3 and Product forbid it; primary KPIs differ (KYC is pickup). |
| 01 Sales | Winning by lunch, live for everyone by evening | **Unsafe.** A morning has too few calls for significance; winners decay; effects reverse across cohorts. |
| 01 Sales | Roll out "persuasive" (P4) everywhere | **Rejected.** halluc 0.025, about 4x P1; not offered on KYC; grader counts it unsafe. |
| 01 Sales | Don't look hesitant with frequent requests | Partly aligned with Ops; the answer is batching, not skipping approval. |
| 02 Compliance | Timing and caller-ID rotation covered by policy | **Partial.** True for Northwind renewal; false for Northwind KYC (auto-apply off). |
| 02 Compliance | Nothing customer-facing changes without human approval | **Conflicts with policy** in public_03: nort_cancel_2 lets welcome and voice auto-apply. We should honour the memo (voluntarily not auto-apply customer-facing dims there). |
| 02 Compliance | Approvers 10-19, KYC 10-18 | **True.** |
| 02 Compliance | Script changes need validation evidence | True for prompts (enforced). Welcome/voice need approval but no validation. |
| 02 Compliance | IRDAI: why did this customer hear this, within 24h | True and only answerable from our store: call events carry config_version, cohort_id and variant. |
| 02 Compliance | "Named human approval" | API returns no approver identity. Gap to state. |
| 02 Compliance | Weekly summary of operational changes | Feasible as a per-tenant digest from the audit store. |
| 03 SRE | Kill-safe, idempotent, actionable alerts only | Non-negotiable and supported by the API. |
| 03 SRE | Prefer existing Postgres/Redis, no new stateful infra | Preference. Grading box is bare, so `run.sh` must bring everything up itself. |
| 03 SRE | One public_03 day in under 10 wall-clock minutes; what breaks at 10x | Not enforced by the sim, but a required deliverable. |
| 04 Kestrel Ops | Two requests a day, never before 11:00 | **Preference stricter than policy** (4/day, approvers from 10:00). Honouring it also keeps 2 slots free for incidents. |
| 04 Kestrel Ops | "I change prompts myself; don't undo me" | True, and her own P2 edit **is** the public_02 hallucination incident. We fix it through an approval with evidence, never a silent revert. |
| 04 Kestrel Ops | "Fix" that read the compliance script word for word | That is **P5** (engage_mult 0.86). Suggest-fixes can offer it; P5 as a fix for P2 grades harmful. |
| 04 Kestrel Ops | Tell me before anything changes on a running campaign; one screen | No notification API. The supervisor view is the only channel. ks_cancel is mid_flight allowed. |
| 05 Product | Separate KPIs; one shared cohort object | True; cohort_id is on every call event. Give cohorts a versioned identity (hash of definition). |
| 05 Product | Expired = rejection, re-queue with fresh data | Matches the contract. Only the 12h-TTL cancel workflows can expire under shipped policy. |
| 05 Product | Impact score must mean something | Not enforced by the sim (any number in [0,1] passes). We make it honest anyway. |
| 05 Product | Only what actually happened counts as evidence | Enforceable from call events. On KYC the locked config_version differs from the prompt actually heard. |
| 05 Product | Per-person call time, supervisor view, caller-ID health all P0 | Caller-ID health is the biggest pickup lever. Per-person timing is the weakest (thin data; per-cohort hour weights capture most of it). |
| 06 CTO | Some memo claims are false; trust the contract | **Confirmed** (see above). "Credits are cheap here" is only partly true: 200 credits (400 on load), suggest-fixes 3, validation up to 20. |

## 10. Traps checklist (things that silently cost points)

1. Our own auto-apply makes our own pending approval stale. One change in flight per workflow.
2. Fatigue slots: keep headroom for incident fixes (prompt fixes always need an approval).
3. Large advance steps make approvals lapse. Step at most about 105 min while anything is pending.
4. ks_cancel requests after about 16:45 can expire, after 19:00 always do.
5. Every 403 auto-apply is logged. Validate policy locally first.
6. Re-proposals must be rebased on supervisor edits, never revert them.
7. Missing hours in an hour_weights table get weight 0 and waste capacity, which arguably breaks
   "business owns capacity". Give every in-window hour a positive weight.
8. DID probes and exploration arms are graded at apply time. Keep them small or offset them.
9. Rolling back a decaying variant at onset is graded harmful; you need a holdout to see decay.
10. `did_flagged` has no event. Detect per-DID pickup drops concurrently against healthy DIDs.
11. Never use P4 or cherry-pick validation runs. Don't pick P5 as a fix.
12. suggest-fixes only returns other catalog prompts whose summaries are already free in
    `/workflows`. A validation run on the incumbent prompt (2 credits) is the cheaper detector.
13. Advance is not idempotent. Record intent before calling, reconcile with `/sim/state` after.
14. Late changes still get graded but barely move outcomes. Freeze experiments near the end.
15. Read every parameter (tick, hours, TTL, caps, windows) from the API, never hard-code.
16. `make grade` writes `reports/public_01_dev.md`, the README asks for `reports/public_01.md`.

## 11. Ambiguities to state as assumptions in SUBMISSION.md

- Guardrail definition: absolute vs relative 0.02, reference period, window.
- Whether chaining several max_shift-sized auto-applies in a row is acceptable (we will say no).
- Whether customer-facing auto-apply is used where policy allows but the client memo forbids (no).
- Treating the Kestrel supervisor's "2 a day, after 11:00" as our own cap.
- Grading box: Docker availability, network access, wall-clock timeout.
- No interaction effects between welcome and voice.
- Approver identity is not available from the API.

## 12. Open decisions for us

- **Tech stack** (proposed: Python + numpy + httpx, Postgres via Docker Compose, FastAPI read-only
  supervisor view; SQLite fallback behind a storage interface). Not yet confirmed.
- **Scope**: which 5 or so capabilities to do properly. A first take, for discussion: C1 ingestion,
  C2 shared cohorts, C5 caller-ID health, C6+C7 experiments with decay and rollback, C8 incident
  response, C9 governance client, C12 audit store, C13 crash recovery; minimal C11 and C14; cut C4
  per-person timing, C15 and most of C3.
- Create our own private git repo (the clone's origin is the author's) and commit in small steps.
