# 0015. Supervisor edits: overlay and cooldown

Status: accepted (2026-11-06), milestone M3 (track T2)

## Context
- Supervisor edits bypass governance, bump the version and arrive as `config_changed` with actor
  `supervisor`. A proposal built from our old desired config would silently revert them, which the
  hard rules forbid.
- public_02: the Northwind supervisor sets the default voice to V2 at D5 15:00, in the middle of a
  voice experiment. V2 is in fact good for `ta`, so a blind revert would be harmful.
- The simulator re-normalises every distribution when it applies a supervisor patch (deep merge,
  then `normalise_config`), so distributions the supervisor did not touch can change in their last
  bits.

## Decision
- **Overlay model.** Desired config = live config + patches. A patch is owned by one module and
  covers one path: `("alloc", cohort, dim)`, `("hour_weights", cohort)`, `("prompt",)` or
  `("cohorts",)`. `compose` copies the live config, applies patches in a fixed order (cohorts,
  allocations, schedule, prompt), drops overrides of cohorts that no longer exist, validates
  locally and labels the result (changed dims, TV, risk).
- **Ownership.** A newer patch from the same module replaces an open one on the path; another
  module's patch is refused unless its class is strictly more urgent. Business cohorts are
  supervisor-owned from the start: a cohorts patch that changes or removes one is skipped.
- **Rebase on a supervisor edit** (`rebase_on_supervisor_edit`):
  1. diff the previous known body against the new one into supervisor paths, comparing override
     distributions with a 1e-9 tolerance (re-normalisation drift is not an edit);
  2. invalidate every patch on those paths or on a removed cohort; report the experiments behind
     them as interrupted (data kept, labelled by version);
  3. report in-flight proposals with an older base; `ProposalLifecycle.mark_stale` makes them
     STALE (never applied) and returns their other patches to the overlay;
  4. start a 24h [T] cooldown per path and record the supervisor as owner: no P2/P3 patch touches
     the path meanwhile, and a later proposal touching it says so in its summary ("changes the
     voice your supervisor set on D5 15:00"); incidents (P0, P1) are not blocked;
  5. rebase as intent: other cohorts' overrides that still give share to the old control get the
     supervisor's new control instead, except where it is credibly worse there (a callback to the
     engagement model's EW lower bound), which is kept and listed for the approver.
- State lives in the `patch`, `path_owner` and `cooldown` tables; `Overlay.rows()` and
  `Overlay.restore()` convert both ways.

## Alternatives
- *Re-propose from our desired config.* Silent revert.
- *Drop all pending work on any supervisor edit.* Loses unrelated work (the caller-ID spread in the
  D5 test survives).
- *No cooldown.* The next routine slot would test against the supervisor's choice within hours,
  which reads as overruling her.

## Consequences
- Unit test: the D5 edit invalidates only the default-voice patch, interrupts its experiment,
  rebases the senior override from V1 to V2, keeps the young override where V2 is flagged worse,
  and the next composition never reverts V2. E2E test on public_02: our pending voice approval
  goes STALE, zero `apply_conflict`, V2 still live two days later.
- Two edits inside one step diff against the last known body, so both are treated as
  supervisor-owned (design 6.7 step 1).
