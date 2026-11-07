# Running, debugging and extending the controller locally

For the engineer who is paged, or who joins the project. DESIGN.md 9.5 lists what this guide must
cover; the design itself is `docs/design/DESIGN.md`, the alert runbook is `docs/RUNBOOK.md`.

Status of the pieces this guide mentions (so nothing here surprises you):

| Piece | Where | State |
|---|---|---|
| Store, migrations, settings, simulator client | `controller/store`, `controller/settings.py`, `controller/simclient` | built (M0) |
| Audit CLI, decision replay, supervisor and ops API, alert rules | `controller/audit`, `controller/api`, `controller/obs/alerts.py` | built (M7, T5) |
| Private scenario variants and the matrix runner | `scenarios/private/`, `scripts/variants/` | built (M0b) |
| `run.sh` with Docker Compose, the loop, ingest, crash safety | `run.sh`, `compose*.yml`, `controller/loop`, `controller/ingest`, `controller/reliability` | built (M1, M2) |
| Governance in the loop: proposals and overlay in Postgres, lifecycle, release train, supervisor-edit rebase | `controller/governance`, `controller/planner`, `controller/loop/governance.py` | built (M3, step I2, ADR 0029) |
| Learners | `controller/learning`, `controller/incidents` | `CTL_LEARNER=probe` runs caller-ID probes and spreads, per-cohort call times from history, engagement learning (welcome and voice experiments, winners, decay rollback, the harm gate on every change) and incident response (prompt fixes, caller-ID drain) |

## 1. Prerequisites

- Python 3.11 and [uv](https://docs.astral.sh/uv/).
- Docker (a daemon the user can reach). Compose is optional: `run.sh` tries `docker compose` v2,
  then `docker-compose` v1 (no `--wait`), then plain `docker run`, which needs only the docker CLI
  (`CTL_DOCKER_BACKEND=auto|compose2|compose1|plain` forces one). Or `CTL_NO_DOCKER=1` with a local
  Postgres 16.
- Network on the first run only: `registry-1.docker.io` (the two base images, pinned by digest) and
  `pypi.org` plus `files.pythonhosted.org` (the exact, hash-checked wheels of
  `requirements-runtime.lock`). Without PyPI, run `scripts/vendor_wheels.sh` elsewhere and copy
  `vendor/wheels/`, or set `PIP_INDEX_URL` to a mirror. Cached images and wheels are not fetched
  again. If even that fails, `run.sh` advances the simulator to its end with no decisions (limp
  mode, last line says DEGRADED) rather than leave the scenario unfinished.
- About 8 GB of RAM for public_03; public_01 and public_02 fit in 1 GB.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-controller.txt
```

## 2. Run public_01 against a local simulator (four commands)

```bash
.venv/bin/python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine_public_01 --force &
./run.sh http://127.0.0.1:8700
.venv/bin/python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base_public_01
mkdir -p reports && .venv/bin/python -m sim grade --run runs/mine_public_01 --baseline runs/base_public_01 --out reports/public_01.md
```

`run.sh` starts Postgres and the controller in containers, waits for the simulator's `/health`
(it builds history first; up to `CTL_SIM_WAIT`, default 900 s), runs until the scenario finishes
and restarts the controller after a crash (state lives only in Postgres). Exit code 3 means a
fatal configuration error and is not retried. A crash restarts the controller; the restart budget
(`CTL_MAX_RESTARTS`, 30) counts restarts with no progress and resets when the run moves on. A hook
that keeps crashing at one step is put in safe mode by the controller (the loop keeps advancing
without it; `SAFE_MODE` alert and audit rows, see `docs/RUNBOOK.md`). When the run ends `run.sh`
stops Postgres (data stays in its volume); `CTL_KEEP_UP=1` leaves Postgres and the read-only API up.
`SIGTERM` or `SIGINT` to `run.sh` stops the controller container too. Without Docker:

```bash
CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=5432 user=postgres password=... dbname=postgres" ./run.sh http://127.0.0.1:8700
```

`make grade` names things by the scenario file: `runs/mine_<file name>` and `reports/<file name>.md`
(`reports/public_01_dev.md`). `reports/public_01.md` and `reports/public_02.md` are the README's names:
the same grade tables plus a reading of the numbers and the exact commands used.

If the Docker daemon hangs (`docker ps` never returns), do not wait on it: use `CTL_NO_DOCKER=1`
with any Postgres 16 or newer. Two runs of one scenario make the same decisions (decision seeds use
the scenario fingerprint, not the per-run schema counter), so a run is reproducible and a crashed
run can be compared with a clean one.

Every setting is an environment variable read by `controller/settings.py` (`EnvSettings`):
`CTL_SIM_URL`, `PG_DSN` or `CTL_PG_HOST/PORT/USER/PASSWORD/DB`, `CTL_API_PORT` (8780),
`CTL_INGEST` (`sql` or `python`), `CTL_ENGAGEMENT_MODEL`, `CTL_LLM_SUGGEST`, `CTL_FAULT`,
`CTL_LEARNER` (`probe`, the default; `probe_only`: no engagement learning; or `none`: governance runs but nothing proposes changes),
`CTL_CODE_VERSION`. Thresholds are overridden with `CTL_T_<NAME>` (only for private-variant
experiments; an override changes `settings_hash`, which every decision row stores).

## 3. Find and open the run's data

One Postgres schema per run, `run_<fp12>_<n>` (DESIGN.md 8.5). The registry names them:

```bash
psql "host=127.0.0.1 port=55432 user=postgres password=ctl dbname=postgres"
```
```sql
SELECT schema_name, status, last_sim_minute, created_wall FROM ctl.run_registry ORDER BY created_wall DESC;
SET search_path TO run_ab12cd34ef56_1;          -- the newest active one
SELECT * FROM clock;                            -- where the controller is in sim time
SELECT step_no, from_minute, to_minute, phase_ms FROM loop_step ORDER BY step_no DESC LIMIT 10;
SELECT code, wf, message, runbook FROM alert WHERE closed_wall IS NULL;
```

Minutes are simulated minutes since D0 00:00 (negative in history); `D3 14:00` is
`3 * 1440 + 14 * 60 = 5160`. Option codes (`call_fact.w`, `v`, `did`, `prompt`) index the
workflow's option lists in `workflow.spec -> 'options'`.

## 4. The audit CLI

Answers the live-review questions from our own store (read-only connections):

```bash
python -m controller.audit why-changed --workflow ks_cancel --at "D3 14:00"
python -m controller.audit why-heard --lead <lead_id> --day 3
python -m controller.audit replay-decision <decision_id> [--no-refit]
python -m controller.audit replay-sample --n 20
python -m controller.audit weekly-digest --tenant northwind_life
```

- `why-changed`: the version live at that minute, its proposal (summary, rationale, impact score,
  affected leads), the approval id and decide time (the API never names the approver), the
  decision with its harm report, and everything the change caused afterwards (the `audit_log`
  chain through `caused_by`). For a supervisor edit, the chain shows our reaction.
- `why-heard`: every call to the lead that day, with welcome, voice and caller ID explained from
  the version the call was dialled with and the prompt from the version live at the call's
  minute; inherited default rows are marked; a call with no provenance row is reported as
  `COHORT_MISMATCH`, never dropped.
- `replay-decision`: recomputes the decision from the stored inputs and prints stored and
  recomputed values side by side; it warns first if the decision's `settings_hash` or
  `code_version` differ from the running ones. Exit 1 if anything differs (ADR 0026).
- `why-changed` lists "what happened after" from `proposal_transition`, the audit rows about the
  proposal or its version, decisions that name the proposal and alerts that name it; for a
  supervisor version it lists the rebase, stale marks, interrupted experiments and the incident it
  opened. `audit_log.caused_by` is a decision id, not an audit id, and intent rows (the crash-safety
  plumbing) are left out.
- `why-heard` needs `allocation_provenance`, which every version writes when it is recorded
  (ours in the same transaction as the apply). A call it cannot explain is `COHORT_MISMATCH`.
- `replay-sample` replays only decisions with a registered recomputer: `learning.harm/harm_gate`,
  `learning.allocator/allocation` and `harm_stop`, `learning.decay/rollback`,
  `learning.winners/looks`. The random sample is mostly harm gates; to cover every kind pick ids per
  `(module, kind)` and run `replay-decision` on each (`docs/design/build-reports/I6.md` has the query).
  A warning that `code_version` differs only means HEAD moved since the run started.
- Every command takes `--json`, `--schema run_...` (default: `$CTL_SCHEMA`, else the newest
  active run) and `--dsn` (default: the controller's settings). Exit 2 means not found or a bad
  argument.

## 5. The supervisor and ops API

```bash
CTL_API_PG_DSN="host=127.0.0.1 port=55432 user=ctl_api password=$CTL_API_PG_PASSWORD dbname=postgres" \
  uvicorn --factory controller.api.app:create_app --port 8780
curl localhost:8780/supervisor/northwind_life      # needs-your-decision, changed today, tested, winning, health, upcoming
curl "localhost:8780/explain/change?workflow=ks_cancel&at=D3%2014:00"
curl "localhost:8780/explain/call?lead=<lead_id>&day=3"
curl localhost:8780/explain/decision/17
curl localhost:8780/ops                            # loop health, lags, heartbeat, credits, alerts
curl -X POST localhost:8780/supervisor/holds -H 'content-type: application/json' \
  -d '{"tenant_id":"northwind_life","workflow_id":"nw_renewal","set_by":"Asha","reason":"board review","until":"D4 12:00"}'
curl -X POST localhost:8780/supervisor/holds/1/release -H 'content-type: application/json' -d '{"released_by":"Asha"}'
```

The API connects as role `ctl_api` (created by the migrations; give it a login once with
`ALTER ROLE ctl_api LOGIN PASSWORD '...'`). It can read the run schema and INSERT or UPDATE
`hold`, nothing else, and it refuses to serve (HTTP 503 with the reason) if its role could write
more, for example when started with the superuser DSN. `CTL_API_ALLOW_OWNER=1` lifts that refusal
for local debugging only. A hold pauses every non-incident change on the workflow; the controller
reads `hold` at every step.

## 6. Debugging

- **Logs.** The controller logs to stdout (`docker compose logs controller`), at `CTL_LOG_LEVEL`.
- **Loop metrics** are rows in `loop_step` (`phase_ms` per phase, chunks, events, orphan calls,
  decisions, RSS, DB size) and the `GET /ops` view of them. Liveness is the `heartbeat` row (every
  10 s).
- **An alert fired**: open `docs/RUNBOOK.md#<CODE>`; each entry says what to look at first.
- **"Why did it do that?"**: `why-changed`, then `replay-decision` on the decision it names.
- **Crash safety.** `CTL_FAULT=<kind>:<n>:<phase>` makes the controller exit as if killed the n-th
  time it reaches a side-effect boundary (kinds: advance, submit_approval, auto_apply, apply,
  validation, suggest_fixes, chunk_transform, audit_write; phases: before_t1, after_t1,
  after_http, in_t2). Restart it the way `run.sh` does and check that nothing was applied twice:
  `SELECT * FROM intent WHERE status = 'started';` must be empty after reconciliation.

## 7. Tests

```bash
.venv/bin/python -m pytest -q tests/unit tests/obs            # pure: statistics, policy, alerts
.venv/bin/python -m pytest -q tests/contract                  # the simulator contract and the DDL
.venv/bin/python -m pytest -q tests/audit                     # explain, replay, API, ctl_api limits
.venv/bin/python -m pytest -q tests/variants                  # private variants reproducible and loadable
.venv/bin/python -m pytest -q -m e2e                          # real simulator over HTTP
```

The `ctl_api` tests (`tests/audit/test_api.py`, `test_hold_audit.py`) log in as the read-only role
with `CTL_TEST_PG_DSN`'s host and port and `dbname=postgres` when the DSN names none, and set that
role's password on the cluster (`ALTER ROLE ctl_api LOGIN PASSWORD ...`, cluster-wide, so use a
throwaway cluster). Tests that need Postgres use `CTL_TEST_PG_DSN` when set, else start a throwaway `postgres:16`
container on a random port and remove it at the end (`tests/support/pg.py`). Tests that need a
simulator start `python -m sim serve` as a subprocess (`tests/support/sim_process.py`).

### Private scenario variants

`scenarios/private/` holds deterministic variants of the public scenarios (new seeds, flipped
effects, decaying winners, caller-ID flags, unsafe prompts, two incidents at once, the governance
attack cases and the constraint rows of DESIGN.md 9.7). Each file's header says what it tests and
what passing looks like. Only tests and scripts read them; the controller never does.

```bash
.venv/bin/python scripts/variants/make_variants.py --verify     # regenerate, load and baseline all
scripts/variants/run_matrix.sh                                   # baseline + grade every variant
scripts/variants/run_matrix.sh --controller ./run.sh gov_c_cap1_p01 two_incidents_p02
```

`run_matrix.sh --controller CMD` serves the simulator for each variant on port 8715, runs
`CMD <SIM_URL>`, grades the run against the baseline and writes `runs/private/summary.tsv`.

To run the controller process itself (what run.sh runs inside its container) against one
scenario with a throwaway database and grade it, without Docker:

```bash
.venv/bin/python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base_public_01_dev
.venv/bin/python scripts/m3_run.py scenarios/public_01_dev.yaml --out runs/m3/public_01 \
    --baseline runs/base_public_01_dev --port 8720 \
    --dsn "host=127.0.0.1 port=55470 user=postgres password=ctl dbname=postgres" [--keep-db]
```

It writes `grade.md`, `controller.log` and `summary.json` (exit codes, wall time, the grader's
governance block) under `--out`. `--env CTL_FAULT=apply:1:after_http` injects a crash.
Baseline wall times are in `docs/measurements/private-variants-baseline.md`.

### Crash and load evidence without Docker

```bash
export CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55479
CTL_KILL_SIM_PORT=8763 CTL_KILLS=8 CTL_KILL_BASELINE=runs/base_public_01_dev \
    CTL_KILL_REPORT=reports/crash_recovery.md scripts/kill_test.sh scenarios/public_01_dev.yaml
CTL_LOAD_SIM_PORT=8764 PGDATA_DIR=/path/to/pgdata scripts/load_test.sh scenarios/public_03_load.yaml
```

`kill_test.sh` kills the controller process with `kill -9` (no containers), compares the crashed
run with a clean one table by table and by the `sim grade` output, and exits 1 on any difference.
`load_test.sh` samples process memory (`PGDATA_DIR` locates `postmaster.pid`; Postgres memory is the
sum of its processes' RSS, an upper bound because shared buffers count once per backend).

## 8. Extending

- **Add a threshold.** Add a field to `Thresholds` in `controller/settings.py` with `T(...)`
  (tunable) or `C(...)` (contract), naming its DESIGN.md section and its derivation in the
  docstring; the 5.11 register is generated from these (`threshold_register()`). Read it from
  settings, never inline. Tune it only on a private variant: add one to `make_variants.py` that
  exercises the threshold, regenerate, and add a test that runs it.
- **Add a private variant.** Append a `Variant(...)` to `VARIANTS` in
  `scripts/variants/make_variants.py` with what it tests and what passing looks like, run the
  script with `--verify`, and commit the YAML with the script change
  (`tests/variants/test_variants.py` fails if they drift).
- **Make a new decision replayable.** Next to the code that makes the decision, register a
  recomputer:
  ```python
  from controller.audit.replay import ReplayContext, register_recomputer

  @register_recomputer("learning.harm", "harm_gate")
  def _replay(ctx: ReplayContext) -> dict:
      mean, cov = ctx.fit("ew", refit=fit_from_inputs)        # or the stored fit with --no-refit
      ...                                                      # redraw with ctx.seed, recompute
      return {"outcome": {...}, "harm_report": {...}}
  ```
  Store the snapshot with `replay.encode_inputs`, `replay.encode_array` and `replay.hash_inputs`
  so replay can read it back.
- **Add an alert.** Add an `AlertCode` to `controller/obs/alerts.py` (page or digest, condition or
  event) and an anchored entry to `docs/RUNBOOK.md`; the alert test fails until both exist.
  Condition alerts get a rule in `RULES`; event alerts are raised with `raise_alert` by the module
  that sees the event.
- **Add a supervisor view section.** Add a function to `controller/api/views.py` and a key in
  `supervisor_view`; keep pickup and engagement as separate fields, never one blended number.
