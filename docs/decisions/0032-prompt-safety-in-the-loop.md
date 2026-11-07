# 0032. Prompt safety in the loop

Status: accepted (2026-11-07), integration step I5 (milestone M6)

## Context
T4 wrote the statistics (two-stage verdict, designated gate-evidence run, flag CUSUM, ranked fix
selection, ordered credit pools) and the incident state machine, but nothing ran them. At HEAD
(AUDIT-1 finding 4) a supervisor who shipped an unsafe prompt got no detection, no verdict and no
fix (public_02: `mttr_minutes: null`, 0 credits used), and the code that would be wired called the
simulator directly: `SafetyVerdictRunner` called `client.validate`, the manager called
`llm.suggest_fixes`, and runs lived in memory "persisted by the caller". A crash between the HTTP
call and the persist would lose the run and its evidence designation, and the retry would spend
credits again and could designate a second evidence run, which is fishing for a pass.

## Decision
1. **Every validation and suggest-fixes call is one intent** (`validation`, `suggest_fixes`) with
   `credits_before` and `cost`. `IntentValidationClient` and `IntentSuggestClient`
   (`loop/incidents_hooks.py`) are the only code that calls them. The key is deterministic
   (`val:<wf>:<prompt>:<purpose>:<stage>:<minute>:<decision key>`, `sug:<incident>`), so a restarted
   step asking again gets the stored answer and spends nothing.
2. **The result transaction commits the whole spend.** The `done` hook writes the `validation_run`
   row (with its pool, cost and whether it is the designated gate-evidence run), the `credit_ledger`
   debit and the `credit_envelope` spend together. The `lost` hook (the balance fell by the cost
   but the answer is gone) commits the debit and a `LOST_VALIDATION_RUN` digest item. A lost
   validation is paid once more under a second key (`:r2`), so a crash after the HTTP call costs at
   most one extra run; a lost suggest-fixes call is not repeated (the LLM only adds candidates and
   the catalog rank already covers them). An in-memory budget that already counted a replayed
   intent refunds it (`OrderedCreditBudget.refund`), so memory and store agree after a restart.
3. **Designation before the run.** The runner decides whether a run is the decision's evidence
   before calling (`designated` in the request), and the flag is stored with the run. A restart
   rebuilds `designated` from `validation_run`, so there is never a second designated run for one
   (decision, prompt).
4. **Where it runs.** `PromptSafety` is owned by `GovernanceHooks` and kept in its own module; the
   edit to `loop/governance.py` is six call sites. REFRESH records a supervisor prompt change in
   `prompt_trigger` (it is one transaction and cannot make an HTTP call). DETECT updates the flag
   CUSUM (`flag_monitor`), judges triggered prompts outside any long transaction and persists
   verdicts and incidents. DECIDE asks every open or blocked prompt incident for its P0a fix at
   every stop (not only at routine slots) and adds it to the overlay with its decision row in the
   same transaction as the incident state. VERIFY runs the post-apply check and links the incident
   to the proposal that carries its evidence run. At start: the ordered pools (`credit_envelope`,
   filled once) and the D0 screens of the business defaults from pool S.
5. **The fix goes through the release train unchanged.** It is a P0a patch, so it composes first,
   is submitted at the next stop (the fast-apply step rule applies it at the first stop after the
   decision is visible), cannot be auto-applied under the default policy, and is rebased on a
   supervisor edit instead of becoming stale. A DID drain (P0b) in the same workflow queues behind
   it by p0_rank without superseding it.
6. **Alert codes.** `INCIDENT_FIX_FAILED` (page) and `LOST_VALIDATION_RUN` (digest) are added with
   runbook anchors; the manager's `CREDITS_LOW` is the existing `CREDITS_BELOW_RESERVE`.
7. **Migration 0020** adds the run metadata columns and `prompt_trigger`. Migrations 0021 to 0024
   are unused.

## Alternatives
- Persist runs "by the caller" after the manager returns: loses a run on a crash between call and
  persist, which is exactly the fishing risk. Rejected.
- Repeat a lost validation under the same key: the simulator has no idempotency key, so this would
  be a blind retry. Rejected; the second key makes the repeat explicit and audited.
- Judge a supervisor prompt inside REFRESH: holds a transaction open across a 300 to 1,300
  conversation HTTP call. Rejected for the trigger table.
- Compare crashed and clean runs exactly after a lost validation: not possible. The repeated run
  has a new id and the id seeds the outcome, so the world diverges. The crash cells assert the
  structure instead (one lost intent, at most one repeat, the ledger equals the simulator's
  balance, no incident left open or blocked).

## Consequences
- Credits are never spent outside an intent, and the ledger always equals the balance (the tests
  assert it after every crash cell).
- A prompt fix to a safer prompt that engages worse is labelled "harmful" by the grader (7.1: P0
  fixes are exempt from the engagement gate). The post-apply engagement check can also reject a
  safe fix whose engagement fell more than 5%, which sends the next candidate (see the I5 report,
  Decisions for the lead).
