# 0017. Run identity: lock first, schema per run, resume only when consistent

Status: accepted (2026-11-06), milestone M1

## Context
- The simulator keeps its world in memory. A restarted simulator, or a re-run of the same
  scenario, is a new world, but it has the same scenario name, the same workflows and, because the
  seed is the same, the same history. `/sim/state` has no run id (F9).
- A restarted controller must resume the same world exactly where it stopped (constraint 8);
  data of an old world must never leak into decisions about a new one, and must stay for audit.
- During a deploy two controllers may briefly run against one SIM_URL.

## Decision
1. **Single owner first.** `pg_try_advisory_lock(hashtext('formi_ctl:' || normalised SIM_URL))`
   on a dedicated connection, before anything reads or creates schemas; a second controller polls
   every 5 s instead of acting. The heartbeat thread checks every 10 s that the lock is still held
   and exits the process if it is not (another controller may now own the world).
2. **Fingerprint** = sha256 of what the API says the world is: scenario name, end minute, tick,
   every workflow's id, tenant, governance and options, and history chunk 0's metadata (events,
   last event id). It leaves out everything that changes while the world runs.
3. **Schema per run** `run_<fp12>_<n>` registered in `ctl.run_registry`. Candidates are the rows
   with this fingerprint whose status is not `done`, newest first. A candidate is resumed only if
   every check holds against the live simulator (`world/fingerprint.py: check_consistency`):
   our clock is not ahead of the simulator; our highest chunk number and event id are not ahead;
   our newest ingested chunk has the same event count and last event id as the simulator's chunk
   with that number; every approval id we hold is listed with the same proposal id; every
   controller-actor version we recorded is in the config history with the same proposal id; no
   workflow's history is behind our highest recorded version.
4. **Otherwise a fresh schema** `n + 1`, created and registered in one transaction with every
   migration applied; rejected candidates are marked `abandoned` and kept; a `NEW_WORLD` digest
   alert names them and why they failed.
5. **Proposal ids use the fingerprint's first 6 characters**, not the schema name, so two runs of
   one world issue identical ids (the simulator's id namespace is per world anyway).

## Alternatives
- **Fingerprint alone.** Collides on every re-run and every simulator restart (the J2 finding).
- **A run id we invent and store in the simulator.** There is nowhere to store it: the API has no
  free-form metadata.
- **One schema with a run column.** Every 8M-row table and index carries the column, old runs
  cannot be dropped with one statement, and a bug in one query mixes worlds.
- **Resume the newest candidate without checks.** A restarted simulator at minute 0 would be
  driven with our clock at, say, D3; advances are relative, so the worlds silently diverge.

## Consequences
- Tested end to end (`tests/e2e/platform/test_run_identity.py`): a controller stopped twice
  resumes one schema with contiguous advances; restarting the simulator gives schema `_2`, marks
  `_1` abandoned and raises `NEW_WORLD`.
- A controller that crashes before its first advance against a fresh simulator resumes that
  schema after a simulator restart too: the two worlds are identical up to that point (same seed,
  same history), so this is correct, not a collision.
- The lock key is a 32-bit `hashtext`; two different SIM_URLs could collide in theory and would
  then serialise, which is safe (ADR 0027).
