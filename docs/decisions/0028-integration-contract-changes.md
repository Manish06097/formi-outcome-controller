# 0028. Integration contract changes (step I1)

Status: accepted (2026-11-06), integration step I1

## Context
During the parallel build (DESIGN.md 17.4) only M0 could edit the shared contract:
`controller/types.py`, `controller/settings.py` and the migrations. When a track needed a field,
column or setting it did not have, it built a local workaround inside its own module and listed
the change it wanted under "Contract changes requested" in its build report
(`docs/design/build-reports/`). Those workarounds work in isolation, but each one is a second,
private version of a shared fact: a subclass the rest of the code does not know about, a value
smuggled inside a JSON blob, a constant that should be a threshold. Wiring the tracks together
on top of them would spread the private versions further.

Step I1 merges every requested change into the shared files, removes every workaround, and closes
three convention gaps the reports left open. It changes no decision rule and no threshold value;
the full test suite passes after each commit (658 tests before, 676 after).

## Decision
One change per request. "Why" is the reason the shared contract is the right home.

### Shared types (`controller/types.py`)
| Change | Replaces | Why |
|---|---|---|
| `EffectivePolicy` gains `pre_open_submits`, `api_auto_enabled`, `api_allowed_dims` (pre-directive API view; None resolves to the effective value), `use_case`, `directive_scopes`; `approver_close_min` and `ttl_min` are properties | `world.registry.ExtendedPolicy` and `ext()` (T2) | Every governance rule reads limits from the policy; a subclass plus a getter with fallbacks meant a policy built elsewhere (stepper, tests) silently answered with defaults. Derived values are properties so they can never disagree with the API fields. |
| `Proposal.p0_rank` | `evidence["p0_rank"]` and `lifecycle.p0_rank_of` (T2) | Supersede and release precedence (ADR 0013) branch on it; a decision input must be a typed field and a column, not a key in the evidence we send to approvers. |
| `Substitution` moves into types; `RebaseReport.substitutions` | `overlay.RebaseOutcome` (T2) | The supervisor-edit handler (T1) and the audit trail consume the rebase report; they should not need to know a planner subclass. |
| `Overlay` and `ProposalLifecycle` protocols carry the keyword extensions (`compose(base_version, paths)`, `submit(package, check)`, `supersede(...)`, `auto_apply`, `mark_stale`, `policy_reread`) | protocol narrower than the implementation (T2) | The orchestrator is typed against the protocols; it must be able to call what the lifecycle actually offers. |
| `WorkflowSnapshot` gains `cells`, `active`, `looks`, `decay`, `holdouts`, `changes`, `end_minute`, `run_id`, `snapshot_id` and a `window` property; `LookRecord`, `HoldoutState`, `ChangeRecord` move into types; the snapshot is `eq=False` | `snapshot.LearningSnapshot`, `getattr(ws, "cells", {})` in five learning modules (T3), `end_minute` passed to constructors (T4) | One snapshot type for every learning and incident module. `getattr` with a default hid a missing field as "no data", which for the harm gate means averaging over the wrong population. `eq=False` keeps the identity semantics the subclass had (the snapshot holds numpy arrays). |
| `FlagMonitorState.last_sim_hour` (renamed from `last_event_id`) | the hour stored in `last_event_id` (T4) | `r_flags` is hourly, so the hour is the unit of progress; a field named after event ids invites someone to compare it with one. |
| `PatchRequest.decision_id: int \| None = None`, with the rule below | `decision_id=0` placeholders (T4, T3) | See "decision_id rule". |
| `Hold.released_by` | release suffix appended to `reason` (T5) | Who released a hold is a fact the supervisor view and the audit read; parsing it back out of free text is fragile. |
| `FaultKind` adds `chunk_copy`, `bootstrap`; `FaultPhase` adds `after_t2`, `mid`; `faults.KNOWN_KINDS` derives from `FaultKind` | two hand-kept lists (T1) | The crash points the kill test uses are part of the contract; one list cannot drift. |

### Settings (`controller/settings.py`)
| Change | Replaces | Why |
|---|---|---|
| `Thresholds.apply_time_trend_p = 0.90` | `decay.TREND_SIGNIFICANCE` (T3) | A `[T]` threshold must be in the register, enter `settings_hash` and be overridable for the private-variant harness. |
| `Thresholds.look_max = 0` (no cap) and `winners.look_due` honours it | none (T3 asked for the knob) | T3 measured W1 alone at 0.73% false winners when volume pushes the first look past 200 calls; 4 restores the 0.54% union bound. Default 0 keeps today's behaviour; choosing 4 is a tuning decision for the private variants, not for I1. |
| `Thresholds.winner_scope_ramp` | `cohortset.WINNER_SCOPE_RAMP` (T4) | Same reason as above; its doc says it is derived from the harm gate's ramp and must be re-derived if the gate changes. |
| `decay_cusum_h` doc: one-sided in-control ARL about 336 segments, not 168 | wrong number in the register (T3) | The register is what we defend in review; 168 is the two-sided value. |
| `EnvSettings.stop_after_steps`, `synthetic_markers` (parsed and validated), `prefetch_chunks` | `loop.orchestrator.LoopOptions.from_env` reading `os.environ` a second time (T1) | One parser for the environment; a malformed marker is a settings error (exit 3) like any other. None of them enters `settings_hash` (they do not change decisions). |

### Migrations (new files only; 0001 to 0008 are untouched)
- `0009_proposal_p0_rank`: `proposal.p0_rank` with `CHECK ((class = 0) = (p0_rank IS NOT NULL))`.
- `0010_flag_monitor_sim_hour`: rename `flag_monitor.last_event_id` to `last_sim_hour`, type int.
- `0011_config_event_refreshed`: `config_event.refreshed_minute`; REFRESH stamps it in its own
  transaction and selects `refreshed_minute IS NULL`, instead of looking for a
  `config_refreshed` audit row. Backfilled from those audit rows, so a resumed pre-0011 run does
  not refresh a version twice. The stamp is the clock minute; resume refreshes before the next
  advance, so a crashed run writes the same value as a clean one (the crash-equivalence and
  determinism tests compare `config_event`).
- `0012_alert_dedupe_hold_release`: `alert.dedupe_key` plus the unique index `alert_open_key` on
  `(code, coalesce(wf, -1), dedupe_key) WHERE closed_wall IS NULL`; `hold.released_by`. Old
  prefixed messages and release suffixes are split into the new columns.
- `tests/contract/test_migrations_i1.py` builds a schema with 0001 to 0008, fills the old
  encodings, and checks that `migrate_run_schema` (what `Database.open` runs on resume) upgrades
  it in place without losing a row.

### Conventions closed
- **decision_id rule.** Pure modules cannot write decision rows, so they emit
  `PatchRequest(decision_id=None)`. The caller that ran the module writes the Decision and stamps
  the request with `dataclasses.replace(req, decision_id=...)` in the same transaction, before
  `Overlay.add`. `Overlay.add` raises if `decision_id` is None. This makes "every decision is in
  the audit store before or with the action" (CLAUDE.md) a check at the overlay boundary rather
  than a habit, and `0` can no longer pose as a real id.
- **Segment formula.** T3 asked ingest to compute
  `seg = 2 * sim_day + (minute_of_day >= window_start * 60 + (window_end - window_start) * 30)`.
  Ingest computes `minute_of_day >= (win_lo + win_hi) / 2` in minutes, which is the same number
  (both window ends are whole hours, so the integer division is exact), on the dial's TICK START
  because the simulator randomises the call timestamp inside the tick. `snapshot.segment_of` is
  the reference and its docstring now states the tick-start rule. A new ingest test checks
  `call_fact.seg` and `r_engage.seg` against `segment_of(floor_to(minute, tick))` on windows
  9-21, 9-20 and 7-20, through both parsers, including a call at the last minute of the tick
  before the half-window. No code change was needed: the two rules already agreed.
- **Freeze.** `world.clock.freeze_start(end, min_days, frac)` is the one formula; the DID prober
  and the cohort-scope projection both read `ws.end_minute` through it (rounded up, so
  `now >= start` equals the real-valued rule for whole minutes).
- **Credit-spending intents.** `validation` and `suggest_fixes` requests must carry
  `credits_before` and `cost` (the reconciler's only evidence after a crash). `KindHandler.required`
  declares them and `IntentExecutor.execute` refuses a request without them before writing
  anything.
- **Audit subject of a config version.** `audit/writer.config_version_subject(wf, v)` returns
  `"<wf>@v<v>"` (T5's convention, which `why_changed` starts its chain from). The loop's
  `config_refreshed` row used `"<wf>:<v>"`, so `why-changed` could not find its anchor; it now
  uses the shared function.
- **One alert writer.** `store.repos.raise_alert` wrote its own severity and a lower-case anchor
  (`docs/RUNBOOK.md#new_world`), which does not exist in the runbook, and raised two codes the
  catalogue did not know (`CLOCK_MISMATCH`, `INTENT_UNKNOWN`). Reproduced end to end:
  `test_run_identity` on the pre-I1 code stores NEW_WORLD as a digest with a dead link. It is
  removed; every alert goes through `obs.alerts.raise_alert`, which takes severity and anchor
  from the catalogue. The two codes join the catalogue as event pages with runbook sections.
  NEW_WORLD becomes a page, as DESIGN.md 9.2 lists it. `INTENT_UNKNOWN` is keyed by the intent's
  idempotency key, so a second unknown intent is no longer swallowed by the first one's open row.

## Alternatives considered
- **Keep the local extensions and wire on top of them.** Cheapest today, but every consumer
  would have to know about the subclass or the JSON key, and the M0 contract would no longer
  describe the system.
- **`decision_id` required and the emitters take it as a parameter.** Pure modules would need a
  decision id before the decision exists (they produce the decision's content), which is
  circular; the allocator's `decision_id=0` parameter showed the problem.
- **Keep `config_refreshed` audit rows as the REFRESH marker.** It works, but it makes an audit
  row load-bearing for control flow and couples REFRESH to the audit subject format, which is
  exactly what broke when the subject convention was aligned.
- **Rewrite the applied migrations instead of adding 0009 to 0012.** Faster to read, but a schema
  created by an earlier build would be refused on resume (the checksum check) or, worse, differ
  silently. New files are the only safe path.
- **Set `look_max = 4` now.** That would change winner behaviour, which is a tuning decision to
  make on the private variants (`scripts/variants/run_matrix.sh`), not part of a contract merge.

## Consequences
- `controller/types.py` and `settings.py` again describe the whole system; the build reports'
  "Contract changes requested" sections are closed.
- A request with no decision id now fails loudly at `Overlay.add`. The orchestrator wiring (step
  I2) must write the decision and stamp the request before adding it; tests that build requests
  by hand pass an id.
- Adding thresholds changed `settings_hash` for every run, so `replay-decision` on decisions
  recorded before I1 reports a settings mismatch (expected; there are none outside tests).
- Duplications noticed and left for later: `stats/sequential.py` has two SPRTs (`Sprt`,
  `BernoulliRatioSprt`) and two Page CUSUMs (`PageCusum`, `DriftPageCusum`) from T3 and T4; the
  `ProposalStore` protocol lives in `governance/lifecycle.py`, not in types (it has one
  implementation, in memory; the Postgres one is I2's).
