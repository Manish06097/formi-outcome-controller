# 0033 Decision seeds do not depend on the per-run schema counter

Status: accepted (step I6)

## Context
The probe learner draws posterior samples with `derive_seed(run_id, workflow, minute, purpose, kind)`.
`run_id` was the run schema name, `run_<fingerprint12>_<n>`, where `n` counts runs of the same
scenario fingerprint in one database. Two clean runs of public_01 therefore made different
decisions (28 and 34 config versions in the first pair compared), and the kill test could never
show a crashed run equal to a clean one, because the two always lived in different schemas.

## Decision
Seeds use the fingerprint part of the schema name only (`_seed_run_id` in `controller/loop/governance.py`).
The seed is still stored on every decision, so `replay-decision` is unaffected.

## Alternatives
- Compare the crashed run with a clean run of the same schema (drop the schema, re-run): needs two
  runs sharing one schema, which the run registry deliberately prevents.
- Leave seeds random per run and compare outcomes statistically: a crash bug would hide inside the
  noise; byte-identical tables are a far stronger check.
- Fixed global seed: would make different worlds share randomness.

## Consequences
- Same world, same code, same inputs: same decisions. A re-run of a hidden scenario reproduces
  itself, which helps the live review.
- Outcomes on one world no longer vary between runs; variance across worlds remains. Public_01
  moved from +11.1% to +7.4% conclusion-rate lift only because the seed changed, a reminder that one
  sample of the randomness is not a point estimate.
- Decision ids and patch ids (identity values) still differ after a crash; `compare_runs.py`
  ignores `allocation_provenance.decision_id` for that reason.
