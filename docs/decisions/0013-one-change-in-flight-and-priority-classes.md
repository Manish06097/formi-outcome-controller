# 0013. One change in flight and priority classes

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- Any version bump between request and apply kills a pending approval at apply time (409 plus a
  logged `apply_conflict`), **including our own auto-apply** on the same workflow.
- Incidents must not wait behind routine work, and two incident kinds can collide: a
  hallucinating prompt (a compliance failure, fixed only by approval) and a flagged caller ID (a
  pickup loss, drained by auto-apply where the policy allows it, else by approval).
- A pending prompt-fix approval that gets staled costs a fresh approver latency and a fatigue slot
  (F4).

## Decision
- **One change in flight per workflow**, as the `one_in_flight` partial unique index (states
  submitting, pending, approved, applying, noticed, auto_applying). `InMemoryProposalStore`
  enforces the same invariant; the only way past it is `supersede`, which ends the holder and
  inserts the new proposal in one store step.
- **Classes:** P0 incident > P1 rollback > P2 promotion > P3 routine, and inside P0 a fixed order
  P0a prompt safety before P0b DID drain (`release.p0_order`).
- **Supersede only by a strictly higher class**, and only while no HTTP call is outstanding
  (pending, approved, noticed). The one same-class move: a P0a prompt fix replaces a **pending**
  P0b drain approval by a **bundle that keeps the drain** (`can_supersede(...,
  bundle_keeps_drain=True)`); the release returns the drain's patches to the overlay before
  composing the bundle, so no incident work is dropped.
- `release.precedence` is the 6.2 table, one branch per row:
  P1 over P2/P3 supersedes; P2/P3 wait; routine submits wait up to 2h [T] while a DID is suspect;
  a P0b drain waits for a holder whose worst-case decide-plus-apply is within 60 min [T], else
  supersedes; a pending P0a is never staled by a drain; a P0a facing a running drain chain finishes
  the chain first if `(k_rem - 1) x tick <= 30 min` [T], else stops it and bundles.
- **Batching:** one proposal per workflow carries the change set. Urgent compositions carry only
  classes at least as urgent (so a P1 stays risk-reducing and never waits for a slot); routine
  compositions at a slot carry everything. Auto-applicable operational patches (caller ID, hour
  tables) are auto-applied immediately before the approval, so the approval carries the new base.
- A SUPERSEDED proposal is never polled or applied, even if approved later; its slot stays used.

## Alternatives
- *Several approvals in flight per workflow.* Any apply stales the others: guaranteed conflicts.
- *Supersede within the same class by recency.* A drain could stale a pending prompt fix (F4).
- *Always bundle P0a and P0b.* A two-step drain chain (max_shift 0.2, share up to 0.4) finishes in
  15 min, faster than one approver latency; bundling it would delay the drain by about 90 min.

## Consequences
- A drain behind a pending P0a waits up to `lat_hi + 2 x tick`; above 2h it pages (13.4).
- The e2e tests show the operational part auto-applied at the slot and the approval's
  `base_version` equal to that new version, with zero `apply_conflict`.
- `Proposal` has no `p0_rank` field; the builder writes it into the request evidence and
  `lifecycle.p0_rank_of` reads it back (contract change requested).
