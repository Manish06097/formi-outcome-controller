# Runbook: one entry per alert code

Every alert row carries `runbook = docs/RUNBOOK.md#<CODE>` (DESIGN.md 9.2, 9.5). The codes and
their severities live in `controller/obs/alerts.py`; `tests/obs/test_alerts.py` fails if a code
has no entry here or an entry has no code.

**Pages** need someone now: each says where to look first and what to do. **Digest** items are
handled automatically and appear in the morning digest for information. Nothing pages on a
single call, a routine submission or an auto-applied schedule refresh.

How to look at things (details in [RUNNING.md](RUNNING.md)):
- `psql "$PG_DSN"`, then `SELECT schema_name FROM ctl.run_registry ORDER BY created_wall DESC;`
  and `SET search_path TO run_<fp12>_<n>;`
- `GET /ops` on the API for loop health, open alerts and credits.
- `python -m controller.audit why-changed --workflow <wf> --at "D3 14:00"` and the other audit
  commands.

Open alerts: `SELECT alert_id, code, wf, message FROM alert WHERE closed_wall IS NULL;`. Condition
alerts close themselves when the condition clears. Event alerts stay open until you close them
after acting: `UPDATE alert SET closed_wall = now() WHERE alert_id = <id>;`.

---

## Pages

<a id="INGEST_STALLED"></a>
### INGEST_STALLED

**Means:** a chunk the simulator lists is not committed after 5 wall minutes. Every decision waits
for ingest (ingest lag must be 0), so the controller is effectively paused.

**Look first:** `SELECT max(chunk_no), max(ingested_wall) FROM ingested_chunk;` against
`GET /events/chunks`; the last `loop_step.phase_ms`; the Postgres log for the transform error.

**Do:** if the count check failed, compare the chunk meta `events` with
`SELECT count(*) FROM stage_raw WHERE chunk_no = <n>;`. To fall back to the Python parser, set
`CTL_INGEST=python` and restart; the chunk re-ingests exactly once (the `ingested_chunk` guard).

<a id="APPLY_AT_RISK"></a>
### APPLY_AT_RISK

**Means:** an `approved` or `applying` proposal has its `apply_by` within the next step. Missing it
turns the approval into a lapse, which the grader counts.

**Look first:** the `proposal` row, `approval_mirror.apply_by_minute`, and the failed pre-check in
the proposal's decision row (`/explain/decision/<id>`).

**Do:** if the base version changed, the proposal is STALE and there is nothing to do. If a step
skipped past `apply_by - tick`, that is a bug in `loop/stepper.next_stop`: the proposal goes
LAPSED_LOCAL and re-queues by itself; file the `loop_step` rows with the bug.

<a id="GOV_INVARIANT"></a>
### GOV_INVARIANT

**Means:** the simulator disagreed with our local policy replica: a 403, 410, 422, a fatigue or
evidence rejection, or an apply conflict. By construction this never happens, so it is a bug.

**Look first:** the 403 or rejection reasons against the `policy_check` output recorded in the
decision row, and `GET /workflows/{id}` against `workflow.spec`.

**Do:** auto-apply for that workflow is already disabled. Diff the governance block against
`workflow.spec`, add the case to the differential policy test, fix the replica, then close the
alert.

<a id="CREDITS_BELOW_RESERVE"></a>
### CREDITS_BELOW_RESERVE

**Means:** the incident reserve R fell below one worst incident W (13 credits per catalog
prompt), the pools switched to LOW mode, or a 402 arrived.

**Look first:** `SELECT pool, purpose, sum(cost) FROM credit_ledger GROUP BY 1, 2 ORDER BY 1;` and
`SELECT * FROM credit_envelope;`.

**Do:** normal while an incident is running. Otherwise find the non-incident spender in the ledger.
LOW mode means stage-1-only verdicts and no screens; that is the designed degradation, not an
outage.

<a id="INCIDENT_UNRESOLVED"></a>
### INCIDENT_UNRESOLVED

**Means:** an incident has no submitted fix within 2 sim hours, is unresolved after 6, or a DID
drain has waited more than 2 hours behind a prompt-safety approval.

**Look first:** the `incident` row, its `prompt_verdict` and `validation_run` rows, and the
in-flight proposal (`/supervisor/<tenant>` "needs your decision").

**Do:** check for an open `NO_SAFE_PROMPT` or `CLIENT_GATE_UNREACHABLE`; check whether the fix
waits on fatigue (`requests_used_24h` in `/ops`). If the approver is simply slow, the alert clears
when the fix applies.

<a id="NO_SAFE_PROMPT"></a>
### NO_SAFE_PROMPT

**Means:** every catalog prompt for the workflow has an UNSAFE verdict.

**Look first:** `SELECT * FROM prompt_verdict WHERE wf = <wf>;`

**Do:** a human must supply a new prompt. The controller changes nothing blindly and never picks
a prompt by re-running validation until one passes.

<a id="CLIENT_GATE_UNREACHABLE"></a>
### CLIENT_GATE_UNREACHABLE

**Means:** prompts we judge SAFE cannot pass the client's validation gate reliably
(`min_validation_pass_rate` too tight for the evidence run size).

**Look first:** `min_validation_pass_rate` in `workflow.spec`, and the designated evidence runs in
`validation_run`.

**Do:** ask the client to review the gate. Never re-run validation to fish for a pass.

<a id="NO_HEALTHY_DID"></a>
### NO_HEALTHY_DID

**Means:** no caller ID of the workflow can take traffic (every one is unhealthy or degraded).

**Look first:** `SELECT * FROM did_state WHERE wf = <wf>;` and the SPRT progress.

**Do:** probes keep running. If every option is degraded, the client must provision a new caller
ID; the controller cannot create one.

<a id="NO_SAFE_WINDOW"></a>
### NO_SAFE_WINDOW

**Means:** there is no expiry-safe submit minute in the next 24 hours, so approvals cannot be
requested without risking an expiry.

**Look first:** the `EffectivePolicy` latency, TTL and approver hours (decision rows of
`governance.timing`; `workflow.spec` governance block).

**Do:** usually the TTL is shorter than the approver latency. Ask the client to extend the TTL or
the approver hours.

<a id="NEW_WORLD"></a>
### NEW_WORLD

**Means:** the simulator restarted or the scenario re-ran: the world fingerprint matched no
resumable run, so a new run schema was created.

**Look first:** `SELECT * FROM ctl.run_registry ORDER BY created_wall DESC;` and the failed
consistency check in the controller log.

**Do:** expected after a re-run. Otherwise check `SIM_URL`: the controller may be pointed at a
different simulator than intended.

<a id="UNKNOWN_WRITER"></a>
### UNKNOWN_WRITER

**Means:** a config version or approval exists that no intent of ours explains.

**Look first:** `GET /workflows/{id}/config/history` against the `intent` table;
`why-changed` on that version shows the finding.

**Do:** a second controller is probably running against the same simulator. Check who holds the
advisory lock (`SELECT * FROM pg_locks WHERE locktype = 'advisory';`) and stop the extra process.

<a id="COHORT_MISMATCH"></a>
### COHORT_MISMATCH

**Means:** a call's `cohort_id` or option disagrees with our recorded resolution: `why-heard`
found no provenance row for an option the customer actually heard.

**Look first:** the call (`why-heard --lead <id> --day <d>`), `config_cohort` and
`allocation_provenance` for that version.

**Do:** our config mirror is wrong. Compare `config_version.config` with
`GET /workflows/{id}/config` and fix the mirror before anything else is proposed on the workflow.

<a id="SAFE_MODE"></a>
### SAFE_MODE

**Means:** one hook of the loop crashed repeatedly at one step (or the process died in it, or it
hung). The run keeps advancing without that hook, because an unfinished scenario is the worst
outcome. If the hook is `decide` or `detect`, nothing new is being learned; if `lifecycle`,
approvals are not being applied.

**Look first:** `SELECT point, state, since_minute, reason, skips FROM hook_guard;` and
`SELECT * FROM audit_log WHERE action IN ('safe_mode_entered', 'hook_skipped') ORDER BY audit_id;`.

**Do:** fix the bug named in `reason`. A guarded hook keeps being tried at every step, so one that
only failed on one step's data has recovered if its `hook_skipped` rows stopped. A disabled hook
stays off for this run. Safe mode is per run schema: a new run starts clean.

<a id="LIMP_MODE"></a>
### LIMP_MODE

**Means:** the core loop crash-looped, the configuration is fatal, or Docker could not run the
controller. `controller/loop/limp.py` advanced the simulator to its end with no decisions, so the
scenario finished but nothing was optimised after that point. run.sh's last line says DEGRADED.

**Look first:** the run.sh log before the `last resort` line: the last exit code and traceback.

**Do:** fix the cause, then run the scenario again on a fresh simulator.

<a id="DIRECTIVE_UNMATCHED"></a>
### DIRECTIVE_UNMATCHED

**Means:** a rule in `controller/directives.yaml` names a tenant or use case that no workflow from
`GET /workflows` has. The workflows that rule was written for fall back to the defaults, so a memo
promise may not be applied.

**Look first:** the alert text (it lists the rule, its memo and the live tenants) and
`SELECT * FROM audit_log WHERE action = 'directive_unmatched';`.

**Do:** if the tenant was renamed, update the rule. The defaults only ever tighten the API policy,
so nothing unsafe happened, but tell the client which promise is not in force.

<a id="CRASH_LOOP"></a>
### CRASH_LOOP

**Means:** the controller restarted more than 3 times in 10 minutes.

**Look first:** the log tail of the last run, and `SELECT * FROM intent WHERE status = 'started';`.

**Do:** reconciliation is usually failing on one intent: inspect it. Exit code 3 means a fatal
configuration error (bad settings, no database), which restarting will not fix.

<a id="SIM_UNREACHABLE"></a>
### SIM_UNREACHABLE

**Means:** idempotent GETs to the simulator have been retrying for 5 minutes. The controller keeps
retrying with backoff; nothing is written while the simulator is unreachable.

**Look first:** `curl $SIM_URL/health`; the simulator process and its log.

**Do:** restart the simulator only if it is truly dead, knowing a restarted simulator is a new
world (`NEW_WORLD`). A slow simulator under load recovers on its own.

<a id="CLOCK_MISMATCH"></a>
### CLOCK_MISMATCH

**Means:** after reconciling every open intent at start, our `clock.sim_minute` still differs from
`/sim/state`. The advance protocol commits the clock with the advance result, so this means the
store contradicts the contract. The controller adopted the simulator's minute (an advance is
relative and cannot be undone) and carried on.

**Look first:** the newest `intent` rows of kind `advance` and their status; `audit_log` rows with
`action = 'intent_reconciled'`; `loop_step` around the last minute we recorded.

**Do:** check whether something other than this controller advanced the simulator (a second
process, a manual `POST /sim/advance`). If not, treat it as a bug in the advance protocol and keep
the store for the post-mortem.

<a id="INTENT_UNKNOWN"></a>
### INTENT_UNKNOWN

**Means:** reconciliation could not tell whether a side effect happened: the simulator's answer
matched neither "it ran" nor "it never ran" (for example a credit balance that moved by a
different amount than the call costs). The intent is marked `unknown` and is never retried.

**Look first:** the intent row (`SELECT * FROM intent WHERE idem_key = '<subject>'`), its
`request` and `response`, and the simulator's current state for that kind (approvals listing,
config history, `/sim/state` credits).

**Do:** decide by hand whether the effect happened, then mark the owning proposal or validation
run accordingly. Look for a second writer to the simulator.

---

## Digest (handled automatically)

<a id="APPROVAL_REJECTED"></a>
### APPROVAL_REJECTED

An approver rejected a request. The still-wanted patches re-queue as a new proposal with fresh
data (DESIGN.md 6.1). Read the reasons in `approval_mirror.reasons`; repeated rejections of the
same change are worth raising with the client.

<a id="APPROVAL_EXPIRED"></a>
### APPROVAL_EXPIRED

A request expired. Treated as a rejection and re-queued. Expiries should not happen (the submit
search avoids them); several in a day point at `NO_SAFE_WINDOW` conditions.

<a id="HARM_GATE_BLOCK"></a>
### HARM_GATE_BLOCK

The harm gate blocked a change. Information only: the decision row holds the harm report.

<a id="DID_NOT_ACT"></a>
### DID_NOT_ACT

A module decided not to act, with its reason in the decision row (`kind = 'no_action'`).

<a id="DECAY_ALARM_CLEARED"></a>
### DECAY_ALARM_CLEARED

A decay CUSUM alarm cleared without a rollback. Information only.

<a id="FUTILITY_DROP"></a>
### FUTILITY_DROP

An arm was dropped for futility. Information only.

<a id="ARM_REMOVED"></a>
### ARM_REMOVED

An arm was removed from an experiment. Information only.

<a id="KPI_DRIFT"></a>
### KPI_DRIFT

A KPI drift monitor alarmed (DESIGN.md 7.5); the item carries its mix decomposition. Alert-only:
if it repeats on a single-DID workflow, check `did_state` and whether probes started.

<a id="SUPERVISOR_EDIT"></a>
### SUPERVISOR_EDIT

A client supervisor edited a config. `why-changed` at that minute shows our reaction (stale marks,
interrupted experiments, rebase, cooldown). We never revert it silently.

<a id="EXPERIMENT_INTERRUPTED"></a>
### EXPERIMENT_INTERRUPTED

A supervisor edit interrupted an experiment; its arms were frozen and the supervisor's value became
the control.

<a id="RETRY_SUCCEEDED"></a>
### RETRY_SUCCEEDED

A retried idempotent request succeeded. Information only; many of them point at an overloaded
simulator.

<a id="ORPHAN_CALLS"></a>
### ORPHAN_CALLS

Calls arrived whose lead was never seen; they are kept in `call_fact` but left out of cell-keyed
rollups. A few at the start are expected; a steady stream means a missing history chunk.

<a id="HOLD_AUTO_RELEASED"></a>
### HOLD_AUTO_RELEASED

A held patch set was auto-released after its re-check (DESIGN.md 6.1). Not a supervisor hold:
those are released only by `POST /supervisor/holds/{id}/release`.

<a id="WEEKLY_DIGEST"></a>
### WEEKLY_DIGEST

The weekly operational digest is ready (`GET /supervisor/<tenant>/digest` or
`python -m controller.audit weekly-digest --tenant <tenant>`).
