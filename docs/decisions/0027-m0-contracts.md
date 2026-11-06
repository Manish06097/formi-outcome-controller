# 0027. M0 contracts: config wire form, schema additions, settings and directives

Status: accepted (2026-11-06), milestone M0

## Context
M0 fixes the contracts the parallel tracks build against: `controller/types.py`,
`controller/settings.py`, the DDL in `controller/store/migrations/`, the directives layer and the
`SimClient`. DESIGN.md sections 3, 5.11, 6.5, 11.1 and 11.2 specify most of it. Where they left a
choice open, or where building the contract against the real simulator showed something the
design did not anticipate, the choice is recorded here.

## Decisions

### 1. `Config` is the simulator's stored form, and `body()` is computed so it round-trips
The simulator re-normalises every body it receives (`float(w) / float(sum)`). Measured on 100k
random weight vectors, about 23% of normalised distributions do not sum to exactly 1.0 in
floating point, so sending a stored distribution back unchanged changes its last bits. The
simulator then reports that dim in `changed_dims`: a spurious change on an approval, and a 403
on an auto-apply when the dim is not in `allowed_dims`. `test_sim_contract` reproduces this end
to end.

- `Config.from_body(raw)` normalises exactly like the simulator (bit for bit, differential test)
  and keeps the raw weights for the wire. `Config.from_stored(api_config)` takes the API's
  stored config verbatim (re-normalising it would drift).
- `body()` sends, per distribution, the raw weights it was built from, or a searched preimage
  (scaled copies within 64 ulps of 1) that the simulator normalises back to the stored floats.
  The search succeeds for about 99.5% of drifting vectors.
- `as_stored()` returns exactly what the simulator will store for `body()`. The policy replica
  (T2) must compare the live config with `proposed.as_stored()`, so the remaining 0.5% can never
  surprise it: it predicts the extra changed dim and routes to approval instead of a 403.
- `changed_dims()` lives on `Config` because "equal" for governance means "the simulator sees no
  change", which is not structural equality (an override equal to the default is no change).
  `governance/policy.changed_dims` should delegate to it.

Alternatives: round every weight to a fixed number of decimals (does not make the sum exactly
1.0, so it does not remove the drift); store raw weights only (configs fetched from the API have
none).

### 2. Interfaces of 11.2 are `Protocol`s in `types.py`
The concrete classes stay in their tracks' modules; the protocols give every track a typed
contract without importing another track's code. `WorkflowSnapshot.cohorts` is typed with the
`CohortSet` protocol.

### 3. Migrations: one flat directory, a scope header, checksums
Files are `NNNN_name.sql` with first line `-- scope: ctl` or `-- scope: run`. Run-scope files are
applied into each `run_<fp12>_<n>` schema with `search_path` set to it, so the DDL is unqualified.
Each schema records applied files in its own `schema_migrations`; a changed checksum is refused,
so later contract changes are new files and a resumed run schema never silently diverges.
`call_fact` gets a default partition plus `ensure_call_fact_partitions(first, last)`, because day
partitions depend on `end_minute` from the API; `create_run_schema(sim_days=...)` calls it.

Additions to section 3.2, each needed by a type or a sentence elsewhere in the design:
`flag_monitor` (the flag CUSUM of 7.1 must persist; no table held it), `heartbeat` (the 10 s row
of section 2), `drift_monitor.up_cusum` (7.5 view), `prompt_verdict.mode` and `gate_run`
(`Verdict`), `validation_run.sim_minute`, `patch.p0_rank` and `patch.evidence` (`Patch`),
`alert.sim_minute`, `loop_step.wall`, a unique index on `proposal.approval_id`, partial indexes
for open intents and open alerts, an index on `audit_log.caused_by` for the 9.4 chain query, and
CHECK constraints where the design enumerates values (proposal states equal
`types.PROPOSAL_STATES`). Rollups keyed by cell exclude orphan calls (cell unknown); they stay in
`call_fact` and are counted in `loop_step.orphan_calls`.

The `ctl_api` role is created by the ctl migration and granted per run schema, and both steps
are skipped with a notice when the connecting role lacks CREATEROLE (no-Docker mode against a
managed Postgres), so only the API fails to start in that case.

### 4. Settings: one field per threshold, with metadata
Each threshold is a dataclass field whose metadata holds its section, derivation and whether it
is a tunable `[T]` or a contract/grader constant; `threshold_register()` generates the 5.11 table
from code. `settings_hash` covers all thresholds plus the two environment flags that change
decisions (`CTL_ENGAGEMENT_MODEL`, `CTL_LLM_SUGGEST`), never connection details. `CTL_T_<NAME>`
overrides exist for the private-variant harness and change the hash. `CTL_LLM_SUGGEST` defaults
to on because section 17.1 overrides 7.1 (one call per hallucination incident).

### 5. Directives: layered resolution
`defaults` < tenant-wide < use-case keyword < workflow id, key by key, later scope overriding
earlier. A tenant-specific rule beats a `*` rule with the same selector. Overriding is allowed in
both directions between directive scopes (Kestrel's cancel line allows ramps where the `*`
default does not); "only tighten" applies against the API policy, in `world/registry.py`. The `*`
cohort harm line and preferred-slot offsets come from settings, so each number has one home.
The loader is `controller/world/directives.py`.

### 6. Dependencies
`psycopg[binary,pool]`: the pool extra is psycopg's own `psycopg_pool`, the same project ADR 0001
accepted; a hand-rolled pool would be new code to own for no gain.

## Consequences
- Tracks build configs with `Config.from_body` or the `with_*` builders and send `body()`; they
  never send `stored()`. T2's replica compares against `as_stored()`.
- Contract changes after M0 are new migration files and new fields, merged by the lead.
- Tests that need Postgres start a throwaway `postgres:16` container (or use `CTL_TEST_PG_DSN`),
  because a fixed port can be taken by an unrelated container on a developer machine (55432 was).
