# Project guide: Formi outcome-first controller

Senior engineer take-home. Read `README.md`, `docs/SIMULATOR.md` and `notes/UNDERSTANDING.md` first.
`notes/UNDERSTANDING.md` is the verified map of the simulator's mechanics, traps and grader.

## Operating model
- The lead engineer (main session) acts as the candidate: owns every design decision, writes it down,
  and delegates implementation, review and verification to subagents.
- Every non-trivial decision gets a short record in `docs/decisions/NNNN-title.md`
  (context, decision, alternatives, consequences). The candidate must be able to defend each one
  in a 60-minute live review without AI help, so write the reasoning, not just the outcome.
- Small, honest commits that show how the thinking progressed. Never squash.

## Hard rules (disqualifying or constraint-breaking if violated)
- The controller talks to the simulator **only over HTTP**. It must never import `sim`, read
  `runs/`, read chunk files from disk, or parse `scenarios/*.yaml`. Tests may start the simulator
  as a subprocess; controller code may not.
- Never modify `sim/` (graders run their own copy).
- Every config change goes through `/approvals` or a policy-permitted auto-apply. Never overwrite a
  supervisor edit silently: rebase on it.
- Pickup and engagement are never blended. Optimise each workflow's `primary`, respect its `guardrail`.
- Winners are chosen by deterministic statistics. The LLM endpoint may only propose candidates.
- No change is triggered by a single call. Thresholds on populations only.
- Every decision is written to our own audit store before or atomically with acting on it.
- Credits are finite: reserve budget for incidents.
- Crash safety: killing the controller at any point and restarting must never double-apply, lose a
  pending approval, or forget learned state.
- Read every parameter (tick, hours, TTL, caps, windows, options) from the API. Never hard-code
  public-scenario values or tune to public truth.

## Engineering conventions
- Python 3.11, type hints, small modules with one clear job each.
- Prefer end-to-end tests that run the real simulator over HTTP over unit tests for product
  behaviour. Unit tests are for pure statistics and policy-check code.
- When fixing a bug: reproduce it end to end first, fix, then re-run the same reproduction.
- No em dashes in docs or comments.

## Lessons learned
(Add an entry here whenever the user corrects a mistake.)
