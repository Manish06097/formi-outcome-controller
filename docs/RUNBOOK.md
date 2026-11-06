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

<a id="DID_DEGRADED"></a>
### DID_DEGRADED

**Means:** the fast DID detector (DESIGN.md 7.3) found a caller ID whose pickup, against the other
live caller IDs in the same cohort and hour, fell to a ratio of 0.75 or less with z of -4 or less
over a 2, 4 or 8 dialling-hour window. A `did_degraded` incident is open and a P0b drain is
running: one `max_shift` auto-apply per tick where caller ID auto-applies, otherwise one P0
approval at the next expiry-safe minute.

**Look first:** `SELECT * FROM incident WHERE kind = 'did_degraded' ORDER BY opened_minute;`
(evidence holds the alarm window, the share at the alarm and every drain step), then
`SELECT * FROM did_state WHERE wf = <wf>;` and the proposals of class 0 for the workflow.

**Do:** nothing while the drain runs. If the caller ID is known to be fine (a carrier glitch that
cleared), it is re-admitted as a probe after 3 days on its own; ask the client before forcing it.

<a id="DID_DRAIN_UNVERIFIED"></a>
### DID_DRAIN_UNVERIFIED

**Means:** two dialling hours after the last drain step, the drained caller ID still has dials on
the new version, or workflow pickup is below 0.9 of the pre-incident 24 hours at the same hours.
The incident went back to `draining` and the DID detector re-checks the remaining caller IDs.

**Look first:** the incident's `verification` evidence (ratio, dials after the drain, whether
locked `next_campaign_only` campaigns explain the dials), then the KPI drift monitor of the
workflow (`drift_monitor`).

**Do:** if every caller ID is degrading together, expect a `KPI_DRIFT` item and probes of the
unknown caller IDs; if none is healthy, `NO_HEALTHY_DID` follows.

<a id="DID_CHANGE_UNVERIFIED"></a>
### DID_CHANGE_UNVERIFIED

**Means:** two dialling hours after a caller-ID change (a probe, a spread or a drain step), a DID the
change left at 0 still has dials, or workflow pickup is credibly below 0.9 of the 24 hours before at
the same hours. Nothing is rolled back: the DID detectors run every step and act on what they find.

**Look first:** the proposal's `verify` column (the reasons carry the ratio and the dials), the
workflow's `did_state` rows and `drift_monitor`.

**Do:** if a DID is the cause, expect `DID_DEGRADED` or `KPI_DRIFT` to follow. If neither does, the
fall was probably not caused by caller ID; compare with the workflow's hour mix.

<a id="SCHEDULE_UNVERIFIED"></a>
### SCHEDULE_UNVERIFIED

**Means:** after an hour table went live, more than 10% of its (cohort, hour) cells with at least 30
dials fell below the 95% predictive band of the curves it was built from, with the workflow's level
already allowed for. So the hour pattern the table relies on did not hold. A P1 proposal restores the
previous table, and no further table is proposed for this workflow in this run.

**Look first:** the proposal's `verify` column and the `5.8/schedule_restore` decision row (it lists
the cells below their band).

**Do:** nothing while the P1 is in flight. Ask whether the client changed its dialling or lead mix.

<a id="UNKNOWN_WORKFLOW"></a>
### UNKNOWN_WORKFLOW

**Means:** `GET /workflows` lists a workflow id that was not there when this run started. The
controller keeps running the workflows it knows and does not touch the new one, because it has no
catalog entry, history or baseline for it.

**Look first:** `GET /workflows` on the simulator or platform, and the audit row
`workflow_ignored`.

**Do:** tell the operator. The new workflow is picked up by the next run. A controller restart sees a
different scenario fingerprint and starts a new run schema, so do not restart just to pick it up.

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
