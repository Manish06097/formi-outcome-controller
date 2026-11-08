# Does the stricter caller-ID gate (ADR 0035) cost pickup? Seven seeds, A/B

Measured 2026-11-08 on this Mac, no Docker (`CTL_NO_DOCKER=1 ./run.sh` against a private Postgres 17.6, port 55479). Question: ADR 0031 said enforcing the caller-ID (DID) line of the harm gate on every change cost about 2% pickup; ADR 0035 enforces it anyway. Does it cost pickup now, and how much?

## What was compared

There is **no `CTL_` setting** that turns the DID line off: ADR 0035 removed the exemption from the code (`_adds_unproven_did`, `_lever_lines_pass`) and `controller/settings.py` has no toggle for it. So the A/B is between two code states, not between two settings:

- **A, "gate" (default):** HEAD of `controller` (includes ADR 0035, commit 07ef72c). No controller code changed after 07ef72c, so HEAD is the code the final reports were measured on.
- **B, "nogate" (the old behaviour):** the same HEAD in a throwaway `git worktree` (`/tmp/final_wt`, deleted afterwards) with only `controller/learning/did.py` and `controller/loop/engagement.py` restored to their state at `07ef72c~1`. That is exactly the ADR 0031 behaviour: the release train holds a caller-ID patch only when it raises an unproven DID, and the learner cuts on its own draws. The repository's branch was not modified.

Scenario: the `p4_like_supervisor_p01` family (public_01 with a supervisor prompt edit at D3 11:30 on `ks_cancel`; the closest private family with caller-ID decisions on three workflows that also exists as 20 ready-made seeds). Only the seed differs between members (`seed: 6001` ... `6007`, files `scenarios/private/p4_like_supervisor_p01_s600N.yaml`); both arms use the same scenario file and the same no-op baseline run (`runs/base_p4_like_supervisor_p01_s600N`). Seeds 6001 to 6007 were chosen in advance (the first seven of the family); none was dropped. Each run: `./run.sh` to the end of the scenario (exit 0 every time, 127 to 156 s, two runs in parallel on the machine), graded with `python -m sim grade`.

## Result: pickup change against the no-op baseline, workflow-weighted `ALL` row

| seed | gate (A) | no gate (B) | A - B (points) | A pickup | B pickup |
|---|---|---|---|---|---|
| 6001 | +5.2% | +6.2% | -1.0 | 0.262 | 0.264 |
| 6002 | +8.0% | +7.7% | +0.3 | 0.268 | 0.267 |
| 6003 | +4.8% | +6.0% | -1.2 | 0.262 | 0.265 |
| 6004 | +7.2% | +7.2% | 0.0 | 0.268 | 0.268 |
| 6005 | +6.8% | +5.8% | +1.0 | 0.269 | 0.267 |
| 6006 | +6.1% | +5.9% | +0.2 | 0.264 | 0.264 |
| 6007 | +7.0% | +7.0% | 0.0 | 0.268 | 0.268 |
| **mean** | **+6.44%** | **+6.54%** | **-0.10** | 0.266 | 0.266 |
| sd over seeds | 1.14 | 0.75 | 0.76 (paired) | 0.003 | 0.002 |
| min / max | +4.8 / +8.0 | +5.8 / +7.7 | -1.2 / +1.0 | | |

Standard error of the paired difference: 0.29 points (n = 7), so a rough 95% interval for the mean cost is about -0.8 to +0.6 points (t with 6 degrees of freedom, 2.45 x 0.29). Seeds 6004 and 6007 give identical outcomes in both arms (the DID line never binds there); the largest per-seed gaps (-1.2 on 6003, +1.0 on 6005) go in both directions.

Other rows, A - B means over the same seven pairs (points of change against baseline): conclusion rate +0.07 (A +6.90%, B +6.83%); engagement +0.14 (A -0.19%, B -0.33%); per workflow pickup `ks_cancel` -0.04, `nw_renewal` -0.23, `nw_kyc` -0.11 (the `nw_kyc` pair differences range from -4.4 to +4.3 with sd 2.5, so single-workflow pickup is very noisy at this volume).

Incident MTTR (the bad prompt of D3 11:30, minutes to the approved fix) per seed 6001 to 6007: A 75, 120, 165, 120, 90, 150, 150; B 75, 120, 165, 120, 75, 150, 150 (identical except seed 6005, 90 against 75). Safety rows, both arms, all 14 runs: 0 lapsed approvals, 0 expired, 0 apply conflicts, 0 rejected auto-applies. Each run has exactly one change the grader calls harmful: the `ks_cancel` prompt change that fixes the unsafe supervisor prompt (`dims=['prompt_version']`, engageΔ = -0.037 in all 14 runs, i.e. the safe prompt is less engaging), identical in A and B. This is the disclosed weakness 9 in `docs/LIVE_REVIEW.md`, not an effect of the DID gate.

## Reading

- On seven seeds, the stricter caller-ID gate costs **no measurable pickup**: mean -0.10 points (A below B), well inside the seed-to-seed spread (sd 1.1 for A) and the paired standard error (0.29). The ADR 0031 estimate of about 2% is not reproduced; if the true cost were 2 points it would have shown in every seed, and the worst pair is -1.2.
- This does not prove the cost is zero. With seven pairs a cost under about 0.8 points cannot be excluded, and the family has one design (a prompt incident on `ks_cancel`; caller IDs are healthy on all three workflows). A scenario with a flagged or unknown DID on a pickup-primary workflow would stress the gate harder (`did_flag_d1_p01`, `preflagged_unknown_did_p01`); I did not run those A/B pairs.
- Single-run numbers in the final reports (`reports/public_01.md`, `reports/public_02.md`) are single samples from this noise: ADR 0035 should not be blamed for, or credited with, differences of 1 point between two reports.

## Reproduce

```bash
git worktree add --detach /tmp/final_wt HEAD && (cd /tmp/final_wt && git checkout 07ef72c~1 -- controller/learning/did.py controller/loop/engagement.py && ln -s "$OLDPWD/.venv" .venv)
export CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55479
# per seed N and arm: serve scenarios/private/p4_like_supervisor_p01_sN.yaml on a fresh port, run ./run.sh from the repo (A) or from /tmp/final_wt (B),
# then: python -m sim grade --run <run dir> --baseline runs/base_p4_like_supervisor_p01_sN --out <report>
```
Two runs of one seed must not overlap in time: the run schema is keyed by the scenario fingerprint, and a second controller on the same fingerprint attaches to the first one's schema. (I hit exactly this once and discarded that batch; every number above is from the clean second batch.)
