# 0025. Risk-reducing changes and chaining

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- `max_shift` caps one auto-apply's TV per cohort and dim, but nothing stops a controller from
  chaining many capped auto-applies. Doing that for exploration would defeat the client's cap.
- Some changes must move faster than one approval: draining a flagged caller ID (F8), rolling back
  a decaying winner, a harm stop. With `max_shift` 0.05 a 0.35 drain needs 7 steps.
- Directives allow customer-facing auto-apply only for risk-reducing changes (ADR 0012), so the
  label must be precise and must not be self-declared by the module that wants the change.

## Decision
- **The label is computed, never declared.** `PatchRequest` carries no risk flag;
  `policy.classify_risk(cur, new, did_state, controls, history)` labels every changed (cohort,
  dim) of the composed config:
  - welcome, voice: reducing iff every option whose share rises is that cohort's control (the
    incumbent default or the promoted winner in its scope) and no option enters;
  - caller ID: reducing iff share leaves only DIDs in suspect, degraded or unhealthy and every DID
    whose share rises is healthy (no unknown or probing DID gains);
  - schedule: reducing iff the new table equals a table live on the workflow in the last 7 days [T];
  - cohorts, prompt_version: always increasing (approval only by design, whatever the API allows).
  A composition is risk-reducing only if every label is reducing.
- **Velocity.** Exposure-increasing auto-applies on a (cohort, dim) may total at most `max_shift`
  TV over a rolling 24h [T]; the ledger counts every auto-apply step, risk-reducing chain steps
  included, so a chain can never be followed by an exposure increase on that pair within 24h.
- **Chaining.** Only P0 and P1 risk-reducing changes may chain: the release train applies a change
  larger than `max_shift` as straight-line steps `live + lambda x (target - live)` with
  `lambda = (max_shift - 1e-6) / max TV`, one per tick. TV is linear along that line, so every
  pair stays within the cap, every step is itself risk-reducing, and the chain ends after
  `ceil(TV / (max_shift - 1e-6))` steps. Steps carry no patches, so the patches stay open until the
  step that reaches the target. Risk-reducing P2/P3 changes are exempt from the velocity budget but
  never chain (refused if the same pair moved at the previous tick).

## Alternatives
- *Trust the emitter's flag.* One buggy module could auto-apply exploration as "risk-reducing".
- *No chaining.* A 0.35 drain at max_shift 0.05 would need an approval (one approver latency plus
  a slot) or 7 hours of graded exposure (F8).
- *Chain anything risk-reducing.* Slow-path spreads and arm removals are not urgent; letting them
  chain would multiply auto-applies for no outcome gain.

## Consequences
- With `max_shift` 0.2 and a share up to 0.4, a drain takes 2 ticks; with 0.05 and 0.35, 8 ticks
  (the 1e-6 margin adds one step), which is why the release bundles a long chain into the
  prompt-fix approval when both incidents are open (ADR 0013).
- Supervisor-facing text says "move 10% from D1 to D3" for each step, with the remaining steps in
  the release decision.
