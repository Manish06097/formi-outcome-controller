# 0029. Governance in the real loop: Postgres store, intent-bound transitions, stage markers

Status: accepted (2026-11-06), integration step I2 (milestone M3)

## Context
Track T2 built governance as pure modules (overlay, lifecycle, release train, policy replica,
timing, builder) and proved them against a live simulator through a test harness with an
in-memory proposal store and an executor that called the simulator directly. Track T1 built the
loop, the write-ahead intent executor and the reconciler, with no-op governance hooks. Step I2
puts governance into the real loop. Three things the harness never had to face decide the
design:

1. A crash can land between any two statements. The loop must resume into exactly the state a
   clean run holds at that point, or the world diverges (DESIGN.md 8.2, 10.2; constraint 8).
2. The simulator's randomness depends on what we send, down to the order of keys in a body
   (found in this step, see below).
3. The loop runs its stages in a fixed order at each stop (4.1). A crash inside one stage must not
   make a later restart run an earlier stage again on a world a later stage already changed.

## Decision

### 1. Proposals and the overlay live in Postgres; one ambient transaction at a time
`governance/store_pg.py` implements the lifecycle's `ProposalStore` port over `proposal`,
`proposal_transition` and `approval_mirror`, and persists the overlay (`patch`, `path_owner`,
`cooldown`). `TxScope` binds one transaction at a time: every store call joins the bound
transaction or opens a short one. The overlay stays an in-memory working set (the planner reads
it many times per step) and is flushed, by identity diff, at the end of every scoped transaction,
so an overlay change commits with the proposal transition that caused it. `one_in_flight` is the
partial unique index of 3.2; the store turns its violation into `OneInFlightViolation`.

### 2. Every proposal transition commits with its intent row
`IntentExecutor.execute` takes a per-call `before(tx)`; the registry's `done`, `failed` and
`aborted` hooks (`governance/effects.py`) bind the scope to the executor's or the reconciler's
transaction. So:

| Moment | Transaction | Proposal |
|---|---|---|
| T1 | decision row, intent row, `before` | DRAFT -> SUBMITTING, DRAFT or NOTICED -> AUTO_APPLYING, APPROVED -> APPLYING |
| T2 | intent done, `on_effect_done` | -> PENDING (mirror), -> APPLIED (config mirror records our body as `ours`) |
| failed | intent failed, `on_effect_failed` | -> STALE, FAILED, FAILED_POLICY, LAPSED_LOCAL, with the overlay change |
| reconcile | `done` or `aborted` -> `on_effect_not_run` | found: as T2; not found: SUBMITTING/AUTO_APPLYING -> DRAFT, APPLYING -> APPROVED |

The lifecycle calls the same `on_effect_*` methods after `execute` returns; they are no-ops when
the state already moved, so the test executor (no hooks) and the production one give the same
transitions. New transitions: `submitting|auto_applying --not_sent--> draft`,
`draft --abort--> discarded`, `applying --not_applied--> approved`.

**A DRAFT is a request that never reached the simulator.** `ProposalLifecycle.recover` (restart
step 5 and the start of every LIFECYCLE) resends it with the same id and the same stored body when
every SUBMITTING precondition still holds (8.3: "all still hold: resend the same id and
payload"), otherwise aborts it and returns its patches to the overlay. It also resolves anything a
crash left in a call state without an open intent, by the same lookups as the reconciler.

### 3. Bodies we send are stored as `json`, not `jsonb` (migration 0015)
The first crash cell (`submit_approval:1:before_t1`) diverged from the clean run: the resent
request was byte-different because `jsonb` re-orders object keys, and the simulator keeps the
body's key order, which decides which caller ID each dial draws. One re-ordered distribution
changed every later dial of the world (a probe SPRT decided at D0 18:15 in the clean run and at
D1 19:15 in the crashed one). `proposal.request`, `patch.value` and `patch.evidence` are `json`,
which keeps the exact text; anything rebuilt from them after a restart is identical.

### 4. A restart finishes the interrupted step from the stage that did not complete
`loop_stage (step_no, stage)` marks each completed stage (migration 0014). If the last advance
committed but its `loop_step` row is missing, the restart runs that step's remaining stages at
the same minute before planning the next advance. Without it, a crash in GOVERN skipped the
remaining workflows' work at that minute (a routine request is only ever made at its exact slot
minute). Re-running only the unfinished stages matters: DETECT re-run after GOVERN auto-applied a
probe would see the probe already live and start its SPRT a step early. The interrupted stage
itself runs again and is idempotent at a given minute.

### 5. The velocity ledger is derived from proposals (migration 0013)
A proposal stores the (cohort, dim, TV, increasing) of its composition at draft time and its
`applied_minute`; the ledger of 6.5 rule 4 is every applied auto-apply in the window. Nothing to
rebuild on start, nothing to lose between the apply and a separate ledger write, and the T2
gap (a noticed auto-apply applied later was never recorded) is gone.

### 6. Fatigue waits are not NO_SAFE_WINDOW
At cap 1 a rejected routine request leaves no slot for 48h; `choose_submit` returns None and the
release train used to page NO_SAFE_WINDOW (seen on the cap-1 variant). `no_submit_reason`
pages only when no expiry-safe minute exists; a fatigue wait is recorded as `fatigue_wait`.

### 7. The probe-only learner
`learning/probe.py` (DESIGN.md 15.1 M3) runs T4's `DidHealthModel` (DID states every step in
DETECT, persisted in `did_state`; probes and spreads at planning points) and T4's grid and
`HourCurvesModel` fitted on history only; the grid's cohort list is proposed only together with
an hour table. Planning points are the routine slot minutes, which `plan_facts` puts in the stop
boundaries. `CTL_LEARNER=probe` is the default; `none` keeps the M1/M2 no-op loop for the
platform tests.

## Alternatives
- **Separate transactions for the proposal and its intent, reconciled by a sweep.** Works, but
  leaves windows where the proposal and the intent log disagree, each needing its own recovery
  rule. Binding them is fewer rules and matches 8.2 literally.
- **Abort and re-plan with a new proposal id after a crash before the call.** Simpler, but the
  new id and seq change every later id, and 8.3 asks for a resend of the same id and payload.
- **Canonical key order in `Config.body()`.** Would make the order independent of where a
  config came from, but changes the float sums the simulator normalises with and so M0's exact
  preimage search; storing the exact text is local and complete.
- **Re-run the whole interrupted step.** Not idempotent (DETECT after GOVERN, above).
- **Persist the velocity ledger as its own table.** Needs a second write in the apply's T2 and a
  rebuild rule; the proposal already knows everything the ledger needs.

## Consequences
- The crash matrix for `submit_approval`, `apply` and `auto_apply` (4 phases at the first
  occurrence, 2 at the third, 18 cells) leaves the simulator (approvals, config versions) and the
  store (proposals, patches, DID states, approval mirror, stage markers, facts and rollups)
  identical to the clean run. Decision ids differ, because reconciliation writes its own
  decision rows; rationale text quotes them, so the comparison masks the numbers.
- The release train's per-workflow summary row is written when that workflow is done; every side
  effect also has its own decision row written in T1, before the call.
- A stage hook must be idempotent at a given minute. That is now a rule for every later track.
- `did_state` real columns round floats to float4 between steps; every step reads the stored
  values, so a crashed and a clean run see the same numbers.
- Verification of applied changes is not wired (no kind verifier exists yet): applied proposals
  stay VERIFYING, which holds nothing in flight.
