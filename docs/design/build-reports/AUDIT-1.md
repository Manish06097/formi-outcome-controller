# Independent audit of the controller at HEAD `29b5ba5` (read-only)

**Verdict: I found nothing disqualifying.** The main risks are that harm, guardrail and incident work is not wired into the live loop yet (the I3 to I5 steps still to come), and the way `run.sh` depends on the grader's box.

**Scope.** I read the committed state (`git archive HEAD` into the scratchpad, plus `git grep HEAD`). The uncommitted working-tree edits, including the new `controller/loop/pickup.py`, were not audited. I made no edits or commits, did not run the simulator and ran no tests. I read the public scenario YAML only to get its ids and option names, so I could grep the controller for them.

## Findings

**1. Caller-ID drains can only be created at routine slot minutes.**
- **Where:** `controller/loop/governance.py:378`. Severity: hidden-scenario risk (incident time-to-fix).
- **Evidence:** DETECT updates caller-ID (DID) states every step, but DECIDE skips any workflow where `is_planning_point(wf, now)` is false. `plan_caller_ids` is the only code that turns a fast-path `degraded` DID into a P0 drain patch, and it runs only inside DECIDE. So an alarm at 14:00 produces no drain until the next slot, often the next day around approver open. `plan_facts` only adds a submit stop for urgent patches that already exist, so it cannot help.
- **Fix:** when DETECT changes a DID state to degraded, unhealthy or suspect, run `plan_caller_ids` for that workflow in the same step, or treat "urgent finding exists" as a planning point.

**2. No harm gate, no re-gate and no verifier runs in the live path.**
- **Where:** `controller/loop/governance.py:94` (the `LifecycleDeps` built without `regate` or `verifiers`), `controller/governance/lifecycle.py:436` (`regate` defaults to None), `controller/governance/lifecycle.py:1007`. Severity: constraint risk (constraint 5, and the "harmful changes" score).
- **Evidence:** `git grep did_gate\|did_posterior` finds no caller outside `learning/did.py`. Because `regate` is None:
  - late approvals and notice expiries are never re-gated;
  - VERIFYING proposals stay VERIFYING forever, so a bad apply is never rolled back.
- This matches the open I2 problem: one harmful change on public_02.
- **Fix:** wire the `did_posterior` → `did_gate` call before an overlay add or compose, pass `regate` and the kind verifiers into `LifecycleDeps`, and add an end-to-end check that gives 0 harmful changes on the private set.

**3. The guardrail is not enforced on what the live learner ships.**
- **Where:** `controller/loop/governance.py:201` (`BuildContext` never sets `guardrail`), `controller/learning/guardrail.py`. Severity: constraint risk (constraint 3).
- **Evidence:** `learning/guardrail.py` is imported only by `types.py`.
- The probe learner proposes schedule (hour-weight) tables built from pickup curves and caller-ID splits, on every workflow, with no check against the workflow's `primary` or `guardrail`.
- On a workflow whose primary is pickup and whose guardrail is engagement, a schedule shift that hurts engagement goes out unchecked.
- **Fix:** before DECIDE adds a patch, run the guardrail forecast (5.10) for the workflow's guardrail KPI and drop or hold patches that are predicted to breach it. Set `BuildContext.guardrail` so the request text states it.

**4. Incidents and credit spending are not wired, and the code that will be wired bypasses the intent log.**
- **Where:** `controller/incidents/safety.py:150` and `controller/incidents/manager.py:267`. Severity: constraint risk (constraints 7 and 8 once I5 lands) and hidden-scenario risk now.
- **Evidence, part 1:** nothing outside `controller/incidents/` imports it. At HEAD a supervisor shipping a bad prompt gets no detection, no verdict and no fix. That makes 0 credits spent: constraint 7 holds only trivially.
- **Evidence, part 2:**
  - `SafetyVerdictRunner._validate` calls `self.client.validate` directly.
  - The manager calls `llm.suggest_fixes` directly.
  - Both keep runs in memory ("persisted by the caller").
  - A crash between the HTTP call and persisting loses the run and its gate-evidence designation. The retry then spends credits again and could designate a second evidence run, which is fishing for a pass.
- **Fix:** give the runner a `ValidationClient` adapter that goes through `IntentExecutor.execute("validation", ...)` with `credits_before` and `cost`. Commit the ledger debit, the `validation_run` row and the designation in T2 (or in the `lost` hook). Do the same for `suggest_fixes`. Add both kinds to the crash matrix.

**5. `run.sh` needs network access at build time.**
- **Where:** `run.sh:63-68` and the `pip install` in the Dockerfile. Severity: hidden-scenario risk (grader box).
- **Evidence:**
  - The base images `postgres:16` and `python:3.11-slim` are pulled when they are not cached.
  - `vendor/wheels/*.whl` is in `.gitignore`, so a fresh clone has only `.gitkeep`, and the build falls back to installing from PyPI.
  - The requirements are unpinned `>=` ranges (`numpy>=1.26` and others), so builds made on different days can get different numpy versions. numpy's `Generator` streams are not guaranteed stable across versions, so "deterministic" draws could change.
  - `pytest`, `fastapi` and `uvicorn` are installed in the runtime image as well.
- **Fix:** pin exact versions with hashes in a lock file and pin the base image digests. Either commit the x86_64 wheels or say plainly in `run.sh` that the box needs Docker Hub and PyPI access. Use a runtime-only requirements file for the image.

**6. A missing Compose v2 is fatal, with no fallback.**
- **Where:** `run.sh:31`. Severity: hidden-scenario risk.
- **Evidence:**
  - Without Compose v2, `run.sh` prints `docker compose v2 not available` and exits 3.
  - On Ubuntu or Debian with `docker.io` the compose plugin is a separate package, so this is a realistic setup.
  - Compose v2 older than 2.1.1 lacks `up --wait`, so it dies at "Postgres did not become healthy" with exit 1.
  - `CTL_NO_DOCKER` needs a Postgres 16 already running, which a bare box does not have.
- **Fix:** add a plain `docker` path that needs only the docker CLI: `docker network create`, `docker run postgres:16` polled with `pg_isready` through `docker exec`, `docker build`, then a `docker run --network host` loop. Use it when `docker compose version` fails, and also try `docker-compose` v1 without `--wait`.

**7. Client directives are keyed to the public tenant ids.**
- **Where:** `controller/directives.yaml:44` and `:52`; matching in `controller/world/directives.py`. Severity: hidden-scenario risk.
- **Evidence:** `tenant: northwind_life` and `tenant: kestrel_finance` (plus `use_case_contains: cancel`) carry the memo agreements:
  - no customer-facing auto-apply for Northwind and a 1% cohort harm line;
  - 2 requests a day and not before 11:00 for Kestrel.
- If a hidden scenario renames the tenants, every rule silently falls back to the defaults, with no alert. Directives can only tighten, so this cannot break the API policy, but it can break the memo promises.
- **Fix:** at start, raise an alert and write an audit row for each directive whose tenant or selector matches no workflow from `/workflows`. Mention in the ADR that the memos name these tenants.

**8. The slow-path caller-ID degradation has no minimum dial count.**
- **Where:** `controller/learning/did.py:423`. Severity: constraint risk (constraint 5, minor).
- **Evidence:**
  - The only guard is `est.dials > 0 and est.comp_dials > 0 and ucb < 0.85`.
  - By my rough calculation, a DID with about 35 dials and 0 connects against a large comparator would already cross the UCB line.
  - The fast path requires 100 dials (`did_fast_min_dials`) and the suspect path 50.
- **Fix:** add a `did_slow_min_dials` threshold, for example 100, derived like the others.

**9. Held patches are auto-released without re-checking anything.**
- **Where:** `controller/loop/governance.py:292`, `release_held(now, lambda p: True)`. Severity: minor.
- **Evidence:** the design (`held_patch_release_min`, 6.1) says a held patch set is re-checked after 6 hours. The predicate always says yes, so patches return to the open set without a gate re-check.
- **Fix:** pass a real predicate (gate plus guardrail) once item 2 lands.

**10. The restart cap counts every restart, and a repeating bug ends the run unfinished.**
- **Where:** `run.sh:75-91`. Severity: hidden-scenario risk.
- **Evidence:**
  - `restarts` is never reset, so 20 restarts in total ends the run, even when they are spread over a long scenario.
  - A deterministic exception in any hook makes every restart crash at the same step.
  - The simulator only moves on `POST /sim/advance`, so the scenario is left part-finished and graded that way.
- **Fix:** reset the counter whenever `loop_step` makes progress. After N crashes at the same step, start in a safe mode that keeps the advance and the governance lifecycle running but skips the hook that failed, and record that in the audit store.

**11. If `run.sh` itself is killed, its container keeps running.**
- **Where:** `run.sh:78`. Severity: minor (crash-test semantics).
- **Evidence:**
  - There is no `trap` on `run.sh`, so a SIGTERM or SIGKILL to `run.sh` leaves the `compose run` container running.
  - A second `run.sh` starts a new container that waits on the advisory lock. That is safe: no double-apply.
  - When the orphan finishes, the waiting one finds only a `done` schema. `run_candidates` excludes done runs, so it creates a fresh schema and re-ingests the whole run before exiting 0.
- **Fix:** `trap 'compose stop controller' TERM INT`, and make `resolve_run` exit 0 at once when the simulator reports `finished` and a done schema with the same fingerprint exists.

**12. The run fingerprint includes the governance block.**
- **Where:** `controller/world/fingerprint.py:48`. Severity: minor.
- **Evidence:**
  - The simulator I checked never changes governance mid-run, so this is safe today.
  - But `_reread_policies` exists because the design (9.7) expects the policy can change.
  - If it did, a restart would compute a new fingerprint and give the run a new id prefix. It would then abandon the old schema, which loses pending approvals and learned state (constraint 8).
- **Fix:** leave policy fields out of the fingerprint and keep the scenario, end minute, tick, workflow ids, tenants, options and history chunk 0.

**13. A workflow appearing mid-run would cause a crash loop.**
- **Where:** `controller/loop/governance.py:304`. Severity: minor.
- **Evidence:** `registry.load(rt.client.workflows())` runs every step, but `rt.catalog` was registered only at start. Any new id would raise a `KeyError` in `catalog.code` on every restart.
- **Fix:** ignore ids that are not in the catalog, and raise an alert.

**14. Small hard-codings.** Severity: minor.
- `controller/loop/governance.py:60`: `SCHEDULE_REVERT_LOOKBACK_MIN = 7*1440` duplicates `Thresholds.schedule_revert_lookback_min`, so an override of the threshold is ignored. Read `self.t` instead.
- `controller/types.py:58`: `FEATURE_NAMES = ("lang","city_tier","age_band","value_band")` comes from the documented schema. That is acceptable, since hidden scenarios share the schema, but an unknown feature is ignored silently. Log any extra keys seen on `lead` events.

**15. Operational notes on `run.sh` and the API.** Severity: minor.
- **GETs retry forever:** `get_retry_budget_s = 0.0` (`controller/settings.py:442`). If the simulator dies, the controller retries forever and `run.sh` never exits. Consider a large finite budget, for example 30 minutes, and exit 1.
- **Containers stay up after exit 0:** `run.sh:98` leaves Postgres (3 GB limit plus 1 GB shm) and the API on 127.0.0.1:8780 running.
- **Holds are not in the audit log:** `POST /supervisor/holds` (`controller/api/app.py:188`) is unauthenticated, though bound to loopback. It writes holds that change decisions, but no `audit_log` row is written.

## Run.sh on a bare Linux 4 vCPU / 8 GB box
- **Docker access:** it needs a docker daemon the user can reach (root or the docker group), plus Compose v2 2.1.1 or newer.
- **Network:** it needs Docker Hub and PyPI access unless the images and wheels are already present (items 5 and 6).
- **Linux networking:** the controller runs with `network_mode: host` and reaches the simulator at whatever `SIM_URL` says, including 127.0.0.1. It reaches Postgres on 127.0.0.1 at the first free port in 55432..55451. The environment override from `compose.linux.yml` merges by key, which is correct.
- **Ports:** 55432..55451 (one is used) and 8780 (post-run API, failure is not fatal).
- **Memory:** limits are db 3 GB, controller 1 GB and API 256 MB. The I1 load test measured 94 MB controller RSS and 306 MB simulator RSS at public_03 scale, without the learner. That fits in 8 GB.
- **Startup:** the 900 s simulator wait covers history building. When git is missing the code version falls back to `dev`.
- **Without Compose v2:** exit 3, `[run.sh] docker compose v2 not available`, and nothing started.

## Checked and found clean
- **No `sim` import:** `git grep` finds no `import sim` or `from sim` in `controller/`, `run.sh` or `scripts/run_local.sh`. The `importlib` in `audit/replay.py` loads only `controller.*` packages. The image copies only `controller/` and the requirements (`.dockerignore` is `*` plus allow-list), so `sim` cannot be imported inside the container.
- **No disk reads of run data:** no access to `runs/`, chunk files, `scenarios/`, `os.listdir` or `glob` in runtime code. The only file reads are the controller's own files: `transform.sql`, `migrations/*.sql` and `directives.yaml`. Chunks are streamed from `GET /events/chunks/{n}` straight into a Postgres COPY.
- **No hidden-truth field names:** `base_pickup`, `base_engage`, `did_base_health`, `engage_mult`, `archetype` and `effects` do not appear in runtime code. `halluc` appears only as the API's `qa.hallucination_flag` and in comments.
- **No public ids or option names:** none of the workflow ids (`nw_renewal`, `nw_kyc`, `ks_cancel`) or option ids (W1, V1, D1, P1 and the rest) appear in runtime code. Tenant ids appear only in `directives.yaml` (item 7) and in docstrings.
- **Parameters come from the API:** tick (`state.tick_minutes`), approver hours, latency range, TTL, apply window, request cap, gate pass rate, auto-apply dims and `max_shift`, dialling window, daily batch hour and options are all parsed in `types.py` with no defaults hiding public values. The policy is re-read every step and changes are audited. The "15 / 105 / 120" values appear only in comments.
- **Constraint 1, governance:** every mutating call (`advance`, `request_approval`, `auto_apply`, `apply`, `validate`, `suggest_fixes`) exists only in `reliability/intents.py`, apart from item 4.
  - Routing is in `ReleaseTrain._route`: the local `check_auto_apply`, then the notice path, a chain step, the operational part first, or an approval at `choose_submit`'s minute. `_tick_noticed` re-checks `sim_verdict` against the fresh policy.
  - Supervisor edits trigger `rebase_on_supervisor_edit` plus `mark_stale` inside REFRESH's transaction, and `pre_apply_event` refuses `base_version != current`. The `one_in_flight` unique index is enforced by the database.
- **Constraint 2, business owns when to call:** no code changes `dialling_window`, `dials_per_hour` or `cadence`. Hour weights are floored at 0.25 on every in-window hour.
- **Constraint 4, LLM never decides:** the LLM only adds candidates in `safety.py` and the manager, which are not wired. Winners in `learning/winners.py` are deterministic statistics, also not wired yet.
- **Constraint 6, audit before acting:** T1 commits the decision row, intent row, state transition and audit row before the HTTP call, and T2 commits the outcome. DECIDE writes the decision row in the same transaction as the overlay add. The rebase decision is written in REFRESH's transaction.
- **Constraint 8, crash safety:**
  - Every non-idempotent call is reconciled by its kind's lookup: clock, approvals listing, config history, approval status, or credit balance.
  - The advance is never retried blindly.
  - The `loop_stage` markers let a restart finish an interrupted step.
  - The run schema is resolved by fingerprint plus consistency checks under an advisory lock.
  - DID states, the overlay and proposals are in Postgres, and grids are rebuilt deterministically from history.

Nothing was saved apart from the HEAD snapshot I read, at `/private/tmp/claude-501/-Users-recro-assignment/d38adfaf-3817-48c0-8db8-64b10d82c35f/scratchpad/head`.
