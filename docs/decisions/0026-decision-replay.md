# 0026. Decision replay

Status: accepted (2026-11-06), milestone M7 (track T5)

## Context
The live review asks us to "explain a hidden-scenario decision live from the audit store"
(README, UNDERSTANDING.md 2). Showing the stored numbers is not enough: a reviewer can fairly ask
whether those numbers are what the code computes from the evidence it had, or just what it
happened to write down. Constraint 4 (winners chosen by deterministic statistics) and constraint 6
(every decision auditable from our own store) both need the answer to be checkable.

DESIGN.md 9.6 sets the shape: every decision stores `snapshot_ids`, `seed`, `settings_hash` and
`code_version`; every posterior snapshot stores its inputs next to its fit; replay refits,
redraws with the stored seed, recomputes the gate and prints stored and recomputed values side by
side. Three questions were left open, and the build order made them sharper: replay is built in
track T5, before the learning, incident and governance tracks that make the decisions exist.

1. Where does the recomputation of each rule live?
2. How exactly are snapshot inputs encoded and hashed, so replay can read what writers wrote?
3. What counts as "equal", and what does replay do with what it cannot recompute?

## Decision

**1. Replay owns loading and comparison; each track owns the recomputation of its own rules.**
`controller/audit/replay.py` loads the decision row and its snapshots, decodes them, checks their
integrity, finds the recomputer registered for the decision's `(module, rule_id)`, calls it with a
`ReplayContext`, and compares. A track makes its decisions replayable by registering a recomputer
next to the code that made the decision:

```python
@register_recomputer("learning.harm", "harm_gate")
def _replay(ctx: ReplayContext) -> dict:
    mean, cov = ctx.fit("ew", refit=fit_from_inputs)
    ...
    return {"outcome": {...}, "harm_report": {...}}
```

Recomputers are discovered by importing every module of `controller.learning`,
`controller.incidents`, `controller.governance` and `controller.planner` once. The recomputer
calls the same functions the live code path calls; it must not reimplement them, or replay would
test a copy.

**2. A fixed snapshot codec, defined in replay.** `encode_inputs` writes the input arrays as an
npz without pickle; `encode_array` writes `mean` and `cov` as `.npy`; `inputs_hash` is
`hash_inputs(arrays)`, a SHA-256 over each array's name, dtype, shape and raw bytes in sorted name
order. The hash is over the arrays, not the npz bytes, because zip headers carry timestamps, so
the same inputs encoded twice would hash differently.

**3. Equal means equal within `Thresholds.rounding` (1e-9), on what the recomputer returns.**
Numbers are compared with `math.isclose(rel_tol = abs_tol = 1e-9)`, recursively through mappings
and lists. Only the stored columns `outcome` and `harm_report` are compared, and inside them only
the keys the recomputer returns, so it can leave out fields that are not a function of the stored
inputs (ids, wall times). Everything else is a listed diff, never a silent pass:
- no recomputer registered for the rule (the report is `equal = False`, with the modules that
  failed to import);
- stored inputs that do not match `inputs_hash` (the store was altered, or written with another
  codec);
- with refit (the default), a refitted mean or covariance that differs from the stored fit (a
  non-deterministic fit);
- a recomputer that raises, or returns a key that is not a stored column.

`--no-refit` reuses the stored mean and covariance and recomputes only the draws and the gate,
which isolates the fit from the decision rule when a diff appears. If the decision's
`settings_hash` or `code_version` differ from the running ones, replay still runs but says so
first: a threshold override (`CTL_T_*`) or a newer build may legitimately change the result.

`replay-sample --n 20` picks decisions deterministically (a seeded hash of the id) among those
with a recomputer, which is the M7 acceptance check ("reproduces 20 sampled decisions exactly").

## Alternatives
- **Replay reimplements every gate itself.** Rejected: it would be a second implementation that
  can agree with itself while the live code drifts, and it couples T5 to the internals of three
  other tracks. Registration keeps one implementation per rule.
- **Store only the outcome and trust it.** Rejected: an outcome cannot be checked, and the review
  question is precisely whether it follows from the evidence.
- **Store the raw posterior draws instead of inputs plus seed.** 4,000 draws times hundreds of
  parameters per decision is megabytes per decision at public_03 volumes, and it would let us
  show the gate but not the fit. Inputs (aggregated counts, KBs) plus the seed reproduce both.
- **Hash the npz bytes.** Rejected for the timestamp reason above.
- **Exact equality.** Rejected: summing in a different order (a numpy upgrade, a BLAS change) moves
  the last bits. 1e-9 is the same rounding the design applies before gate comparisons (8.6), so a
  difference larger than that could change a decision and must be shown.
- **Compare every stored key.** Rejected: it forces recomputers to reproduce bookkeeping fields;
  instead they return what is a function of the inputs and everything they return is checked.

## Consequences
- Each track that writes decisions must register a recomputer for every rule it wants replayable
  and must write snapshots with this codec (listed as a contract item for T1, T3, T4 and T2).
  Until a track does, its decisions replay as "no recomputer registered", visibly.
- A replay difference is a finding to investigate (non-determinism, a changed threshold, a
  changed store), and the report says which kind.
- Replay needs the stored inputs: decisions made without a snapshot (pure rules over scalar
  inputs) must put everything the rule reads into `decision.inputs`.
- The cost is storage for snapshot inputs (aggregated counts, small) and one import pass over the
  learning packages on the first replay.
