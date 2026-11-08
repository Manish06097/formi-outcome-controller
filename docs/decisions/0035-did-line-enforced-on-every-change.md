# 0035. The caller-ID line of the harm gate holds every non-P0 change

Status: accepted (2026-11-08), supersedes the last paragraph of "Decision" in ADR 0031
("The caller-ID line is recorded everywhere and enforced only on unproven DIDs").

## Context
On public_02 `nw_renewal` v8 (proposal `4bdc1f-nw_renewal-D2T1100-3-0007`, class P3, auto-applied)
raised D4 from 2.8% to 21.6% and carried the package text "Harm gate FAILED ... P(dd < -2%) = 0.0622
... failed ['did']". It went out. Two gates judged the DID line of that change, on different draws:
- the pickup learner's own gate (`DidHealthModel.gate`, ADR 0030) cut the full step (P(dd < -2%)
  0.082) to the largest step whose P is at most 0.05, found by bisection on draws seeded by
  `gate_seed`. It reads 0.050 by construction;
- the release train's composed gate (`engagement.gate`, ADR 0031) judged the cut change on draws
  seeded by `engagement_plan.seed_for(ws, "gate", "did")` and read 0.062. It recorded the failure
  and, by ADR 0031, held a caller-ID patch only when the change raised the share of a DID that was
  unknown, probing, degraded, unhealthy or suspect. D4 had been cleared by the SPRT, so nothing held.

ADR 0031 gave a reason for that exemption: the posterior of a cleared DID is wide, P(dd < -2%) was
about 0.48 for an ordinary spread, and enforcing the line cost about 2% pickup on public_01 with the
I2 learner. That is no longer true. Since ADR 0030 the learner cuts every change to the line before
it reaches the overlay, and on public_02 the composed gate read P(dd < -2%) of 0.000 to 0.005 on
every other applied caller-ID change; v8 was the only one near the line. So the exemption covered no
change it was written for, and let through the one the learner cut to the edge.

Two things made v8 possible. (1) The learner chose its cut by maximising along a line on one set of
draws, so on any other draws an edge change reads high by selection alone (winner's curse). (2) The
release train did not enforce what the gate recorded. ADR 0002 and CLAUDE.md say every change except
a P0 prompt fix passes the gate.

## Decision
1. **The release train holds the caller-ID patches of every non-P0 change whose composed gate fails
   the DID line** (`did` or `did_unmodeled`), whichever DIDs gain share. The re-check of a late
   decision, of a notice expiry and of a held patch use the same verdict (`v.passes`); the unproven
   DID test (`_adds_unproven_did`, `_lever_lines_pass`) is removed.
2. **The learner cuts on the draws that judge the change.** `gate_seed` equals
   `engagement_plan.seed_for(ws, "gate", "did")` and the posterior has `n_draws` draws, as in the
   composed gate. For a single-scope change at the same minute the two gates then compute the same
   number, so a change cut to the line passes at release. This is the rule the allocator already
   follows (ADR 0031: "the draws the planner of this class used").
3. **Exemptions are unchanged and explicit:** P0 incident work (drains are risk-reducing by
   construction, ADR 0008 and 0025) is judged, recorded and never held; rollbacks keep their 10% tail
   on the engagement line (it does not touch the DID line). There is no exemption for a probe on a
   never-measured DID: the probe size comes out of this same line (ADR 0008 point 2).
4. The package text says FAILED only for a change that was held; a change that ships passes every
   line it is judged on.

## Alternatives
- **Keep the exemption and change the text to "exempt".** Honest, but it leaves a documented
  exception to the hard rule that cost nothing to remove, and it let a 0.062 change out.
- **Only share the draws, keep the exemption.** Removes the disagreement but not the unenforced
  line: a future emitter that skips the learner's cut, or a multi-scope composition, would again
  ship on a recorded failure. The e2e test `tests/e2e/test_gate_did_live.py` removes the learner's
  cut and shows the release train refusing alone.
- **Add a safety margin to the learner's cut** (for example cut to 0.04). It is a threshold tune,
  which the lead ruled out, and shared draws make it unnecessary for single-scope changes.

## Consequences
- A multi-scope composition (cohort overrides) is judged by the lead-weighted population health, the
  learner by each scope; they can still disagree, then the patch is held, re-checked after
  `held_patch_release_min`, and dropped after `held_patch_max_holds` holds. The learner plans a
  fresh, smaller step at the next planning point. The cost is a delayed spread, not a harmful one.
- The measured effect on pickup is in `docs/design/build-reports/GATE-DID.md`.
