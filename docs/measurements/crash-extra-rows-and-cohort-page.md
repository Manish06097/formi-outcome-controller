# Crash extra rows and the COHORT_MISMATCH page (public_02, final code)

Two open items from `reports/crash_recovery.md` and `reports/public_02.md`, answered with SQL on the
existing run schemas of the Postgres on port 55479. No controller code, `sim/`, `runs/` or scenario
file was read or changed, and no new run was made (the schemas were still there).

Schemas used: `run_4bdc1ff90f7d_5` (clean kill-test run), `run_4bdc1ff90f7d_6` (crashed run, 31
controller deaths), `run_4bdc1ff90f7d_4` (the public_02 final run of the report). The clean schemas 4
and 5 have the same row counts (686 / 530 / 504 / 120).

## Question 1: are the extra rows of the crashed run repeats?

Extra rows in the crashed schema (6) against the clean one (5): `decision` 712 vs 686 (+26),
`audit_log` 536 vs 530 (+6), `patch` 521 vs 504 (+17), `posterior_snapshot` 131 vs 120 (+11).

### Verdict

**Not all of them are exact repeats, but none is a state defect.** Of the 26 extra decision rows:

| Class | Rows | What it is |
|---|---|---|
| Exact repeat | 23 | Same minute, workflow, module, kind, rule, seed, inputs, outcome and harm report as a row of the clean run, only identity columns differ (`decision_id`, patch ids, snapshot ids) |
| Repeat, outcome differs by idempotence | 1 | `allocation` at minute 660, ks_cancel: the redo has `new_experiments: []` where the first attempt registered `ks_cancel:welcome_message:v1` and `ks_cancel:voice:v1`. Same seed, lambdas, expected gain, patches. The experiments already existed |
| Reconcile record | 1 | `advance` at 9720, `status: not_run`, `via: reconcile`: the intent was open at the kill and the simulator had not advanced; the advance was then redone as a normal row |
| Redo continued from partial progress | 1 | `harm_gate` at 3540, nw_renewal: the second gate covers only `[welcome_message, voice]`, the first covered `[welcome_message, voice, caller_id]` |

Net of those, three clean-run rows have a different-content partner in the crashed run instead of a
copy (they count 0 in the +26): the `advance` rows of minute 3480 and 5130 (`via: response` in the
clean run, `via: reconcile` in the crashed run, because the kill came after the simulator advanced and
before the result was written), and the `release 6.2/submitted` row of minute 3540 (see below).

23 + 1 + 1 + 1 = 26. The 23 exact repeats: 17 `patch` rows of the schedule planner at minute 660
(8 for ks_cancel, 9 for nw_renewal), 4 `allocation` (minute 2100 once, minute 3540 three times for
ks_cancel, which was redone 4 times), 1 `release` (`6.2/defer_suspect`, 3540) and 1 `detector`
(`7.3/suspect`, 3720).

### The one place the redo is not a copy (reported, not fixed)

Minute 3540, nw_renewal. The clean run does one pass: harm gate over the combined change (decision
145, dims welcome_message, voice, caller_id), auto-apply of `4bdc1f-nw_renewal-D2T1100-3-0007`
(caller_id only), submit of `...-0008` (welcome_message and voice) to approval, then the release row
with `auto_refused: []`.

The crashed run was killed after `0007` was applied and before `0008` was submitted (decisions 168 to
169, then a redo 170 to 173). The redo resumes from the partial state: `0007` is already live, so the
harm gate (decision 171) is evaluated on the remaining change only, with another `gate_key` and without
`caller_id` in `changed_dims`, and the release row (173) records why `0008` was routed to approval
(`auto_refused: [policy: dims not permitted..., directive: no customer-facing auto-apply, notice
required..., no chain...]`), which the clean pass never had to compute.

Both runs end identical on this step: `proposal` `...-0008` has the same route, class, state
(`rollback` later), submitted minute 3540, decided 3581, applied 3600, and the audit rows of the two
runs line up (intent started/done for `0007`, `0008`, `apr_00008`). So this is an audit-trail
difference (two gate records for one logical step, the second narrower), not a double apply and not a
lost or changed approval. I do not call it a defect, but it is the one case where "redo = copy" is
false, so the sentence in `crash_recovery.md` should say "a redo resumes from the partial state, so a
step that was part-applied is re-evaluated on what remains".

Minimal reproduction: kill -9 the controller on public_02 after the `auto_apply` intent of a
two-proposal plan is done and before the `submit_approval` intent starts (audit rows 169 and 172 here;
sim minute 3540, nw_renewal). There is no `CTL_FAULT` kind for that point (the kinds are chunk,
advance and transform phases), so the repro is timing based (`scripts/kill_test.sh` with
`CTL_KILLS` of about 30 hit it once). A deterministic reproduction would need a fault kind between the
two lifecycle steps; not added, as asked.

### Why `patch`, `posterior_snapshot` and `audit_log` have extra rows

- `patch` +17: all 17 are `learning.schedule` patches of the two D0T1100 proposals (ks_cancel 8,
  nw_renewal 9), all `state = applied`, same wf/owner/path/value/class/proposal as the original, no
  clean-only rows. The planner step of minute 660 was redone and re-inserted its patches (fresh
  `patch_id`, same content). These are the 17 exact-repeat `patch` decisions above. Config versions are
  identical (37 vs 37), so the duplicate ledger rows do not change what is live; it is a ledger that
  carries a redo as a second copy rather than upserting. Worth knowing for anyone who counts patches.
- `posterior_snapshot` +11: every decision attempt writes its own snapshot pair. Owning decisions with
  more snapshots than the clean run: allocation at 660 ks_cancel (+2), at 2100 (+2), at 3540 ks_cancel
  (+6, three extra attempts), harm_gate at 3540 nw_renewal (+1). In all 11, `seed`, `mean`, `cov` and
  `summary` are byte-equal to a clean-run snapshot (multiset diff: 11 excess, 0 clean-only). The
  `inputs` blob differs in 25 snapshots only in `experiments[].arm_rows[].decision_id` (an identity
  column), plus the 660 redo seeing the already registered experiments (same cause as the allocation
  row above).
- `audit_log` +6: `fault_injected` 4 (the four `CTL_FAULT` exits), `intent_reconciled` 3,
  `intent_started` +1 (the `not_run` advance), `intent_done` -2 (two advances were finished by the
  reconciler instead of the controller). 4 + 3 + 1 - 2 = 6.
- `intent` 247 vs 246: the `not_run` advance (intent 185, status `aborted`).
- `experiment` and `experiment_arm`: identical but for `decision_id` (385 vs 411, 518 vs 545).

### Queries and method

Identity columns were removed before comparing. In `decision`, `inputs`, `outcome` and `harm_report`
(JSONB) every key ending `_id` or `_ids` was dropped, the patch id lists (`patch_ids`, `held`,
`released`, `dropped`, `substitutions[*][0]`) were replaced by the patch's content
(wf, owner, path, value, class), then the two schemas were compared as multisets of
`(sim_minute, wf, module, kind, rule_id, inputs, outcome, harm_report, seed, settings_hash,
code_version)`. A python script did the normalisation (psycopg, read only); the counting is plain SQL:

```sql
select 'clean', count(*) from run_4bdc1ff90f7d_5.decision union all
select 'crashed', count(*) from run_4bdc1ff90f7d_6.decision;

-- extra rows of the audit log by kind of event
select a.actor, a.action, a.c - coalesce(b.c, 0) as delta
from (select actor, action, count(*) c from run_4bdc1ff90f7d_6.audit_log group by 1,2) a
left join (select actor, action, count(*) c from run_4bdc1ff90f7d_5.audit_log group by 1,2) b
  using (actor, action) where a.c <> coalesce(b.c, 0);

-- the part-applied step, side by side
select decision_id, wf, kind, rule_id, snapshot_ids, left(inputs::text, 260)
from run_4bdc1ff90f7d_6.decision where sim_minute = 3540 order by decision_id;
```

The first pass without patch-id normalisation showed 216 crashed-only rows; the offset of 17 in every
patch id list is the 17 extra `patch` rows, which is why the id-aware pass shrinks it to 29 excess rows
and 3 clean-only rows (net +26).

## Question 2: the COHORT_MISMATCH page

### Verdict

**Sampling noise, not an allocation mismatch.** The page is the expected one false alarm of the
verification step, not a signal.

### What the check does

`verify_allocation` (DESIGN 6.1) runs once per applied allocation proposal after the next 2 whole
segments. For every cohort with at least 100 connected calls under the applied version, for the
`welcome_message` and `voice` dims, for every option with a configured share, it compares the realised
share among connected calls with the configured one and pages when it is more than 3 binomial SE off.
Reproduced from `r_engage`, `config_cohort` (cell to cohort) and `allocation_provenance` (configured
share), segments from the apply minute and the workflow's dialling window.

### Numbers

| Quantity | Value |
|---|---|
| Allocation proposals verified in the run | 17 (7 ks_cancel, 10 nw_renewal) |
| Of those with a cohort of at least 100 connected calls in the window (so tested) | 11 |
| (cohort, dim, option) tests | **334** (27 in the page's proposal) |
| Calls per tested cohort | 105 to 268 connected, mean about 150 |
| Expected false pages, normal approximation (334 x 0.0027) | 0.90 |
| Expected false pages, exact binomial per test | **1.00**, so P(at least one page in the run) about 63% |
| Pages observed | 1 |

The page's cell reproduces exactly: ks_cancel v12 (`4bdc1f-ks_cancel-D7T1100-3-0012`, applied 10920),
segments 15 and 16, cohort `age1830_hi~5396af`, voice V2: 19 of 154 connected calls = 0.123 against a
configured 0.0621, SE 0.0194, z = 3.15.

Why the SE is shaky there: 154 x 0.0621 = 9.6 expected V2 calls, observed 19. The exact one-sided
binomial tail P(X >= 19) is 0.0034, so the cell is about a 1 in 300 draw, and with 334 tests one
such draw per run is normal. The 3 SE rule on a small share is a little looser than the normal
approximation says (mean exact false rate 0.0037 per test for shares under 0.1, against 0.0027 for
shares of 0.3 and up).

### No sign of a real mismatch

- **Same window, other cohorts and options.** The 3 other `max |z|` values over 2.5 in the run are
  2.89, 2.82 and 2.79 (and one -2.58), all on other proposals and cohorts, in both directions. In the
  page's window, the other 7 ks_cancel cohorts have V2 shares 0.052, 0.074, 0.085, 0.056, 0.058, 0.050
  against configured 0.067, 0.061, 0.085, 0.048, 0.062, 0.056. `age3145_hi~0c6c13` has no V2 in its
  configuration and got 0 V2 among 205 connected calls: the allocation is applied exactly.
- **Dial level, whole run.** All 1,205 groups (wf, version, cohort, dim, option) with at least 100
  dials in either workflow: mean z -0.006, sd 0.980 (1.00 for a faithful allocation), 49 groups beyond 2
  SE against 54.8 expected, 4 beyond 3 SE against about 3.3 expected. The allocation is calibrated.
- **The cell itself, dial level.** Over the whole life of v12 (699 dials) V2 got 58 dials, z = 2.29;
  the 646-dial window gives 56 (0.087 against 0.062, z = 2.6). The extra connected V2 calls (19 of 56,
  pickup 0.34 against 0.23 for the rest) are a second draw of noise on top, not a bias: voice does not
  move pickup in this run (ks_cancel pickup by voice 0.257, 0.246, 0.248, 0.249 over 4,410 to 23,655
  dials each).
- **Next window.** The same cell under v13 (same configured V2 share 0.0621), segments 17 and 18:
  9 of 190 = 0.047, z = -0.84. Across all v13: 11 of 267 = 0.041.
- **Concurrent change or memo.** No supervisor edit touches ks_cancel after minute 5010 (supervisor
  events: ks_cancel v7 at 5010, nw_renewal v14 at 8100); the `directive` table is empty. The only other
  ks_cancel change near the window is the controller's own v13 (voice, approved, applied 12300, inside
  segment 16): those calls carry `config_version` 13 and are not counted in the v12 test. No incident
  alert in that window. The page and the digest alerts around it are controller-side (harm gate blocks,
  futility drop).

### Side finding (not the question)

Dial level, ks_cancel v9, cohort `age60p_hi~7b12d6`, voice V3: 22 of 368 dials (0.060) against a
configured 0.131, z = -4.07. In the next version with the same share (v10) the same option got 62 of
451 (0.137). One 4 SE group in 1,205 has probability about 0.08 under the null, so I read it as an
unlucky draw too, but it is the one group in the run that is hard to wave off, and the
verifier did not see it (that window had fewer than 100 connected calls per cohort, so it was not
tested). Not investigated further.

### Would two consecutive failing windows have suppressed it, and at what cost?

**Yes for this page.** The next window of the same cell was at z = -0.84, so the second window would
not have failed and no page would have been raised. The false-page rate of a test goes from about
3.4e-3 to 1.2e-5 (about 0.003 expected pages in the run instead of 1.0).

Cost of the rule (power computed from the exact binomial for n = 154 connected calls, configured share
0.0621, 3 SE threshold):

| True share | Page in one window | Page on two consecutive windows |
|---|---|---|
| 1.5x configured | 13% | 1.6% |
| 2x configured | 55% | 30% |
| 3x configured | 99% | 97% |

- Detection delay: a window is 2 segments, one dialling day, so the second window moves the page by
  1,440 simulated minutes (this page: from minute 12420 to about 13860). During that day about 986
  connected calls (4,000 dials) of the workflow are made under the allocation that is possibly wrong,
  in a cohort that gets about 150 of them.
- Power: for a moderate mismatch (2x on a 6% option) the rule roughly halves the chance to catch it in
  the window pair, and only a gross mismatch (3x or more, or a wrong cohort mapping) is still caught
  almost surely. A mismatch from a broken cohort mapping would be large and would persist, so it would
  still page, one day later.
- A better trade than "two consecutive failing windows": pool the two windows (one 4-segment test).
  Pooled n = 308 catches a 2x mismatch with probability 0.88 (against 0.55 for one window) and, being
  a single test, keeps the false rate near 3e-3 per test. The cost is the same one day of delay. I
  recommend either that or keeping the single window and treating a lone 3 to 3.5 SE page as expected
  (as the RUNBOOK already says), looking only for repeats on the same cohort and option. The 3 SE
  line on a single window is the right size for a page that costs a human minutes and whose miss costs a
  day of a mildly wrong 6% option; I did not change any threshold.

### Queries used

Allocation checks per proposal (the controller's own rule, rebuilt in SQL): segment of the apply
minute from the dialling window (`2 * day + (minute-of-day >= start*60 + (end-start)*30)`), the two
following segments, `r_engage` rows of that version joined to `config_cohort.effective_cells`,
configured shares from `allocation_provenance`:

```sql
set search_path = run_4bdc1ff90f7d_4;
-- vp: proposals of kind allocation that were verified or rolled back, with s0 = segment of applied_minute
-- obs: connected calls per proposal, cohort, dim, option in segments s0+1, s0+2 under the applied version
select vp.proposal_id, cc.cohort_id k, 'voice' dim, e.v opt, sum(e.connected) n
from vp join r_engage e on e.wf = vp.wf and e.config_version = vp.v and e.seg in (vp.s0+1, vp.s0+2)
join config_cohort cc on cc.wf = vp.wf and cc.version = vp.v and e.cell = any(cc.effective_cells)
group by 1,2,3,4;   -- same for welcome_message with e.w
-- tst: join allocation_provenance (cohort_key, dim, option, share), keep cohorts with total >= 100,
-- z = (n/total - share) / sqrt(share*(1-share)/total)
```

Dial-level calibration: `call_fact` grouped by `(wf, config_version, cohort_code, v or w)` joined to
`config_cohort` and `allocation_provenance`, same z. Pickup by voice:
`select v, count(*), avg(connected::int) from call_fact where wf = 1 group by v`. Exact false rates:
python, sum over k of `Binomial(n, p).pmf(k)` where `|k/n - p| > 3 * sqrt(p(1-p)/n)`.

## Unresolved

- The `COHORT_MISMATCH` source (2) text in the RUNBOOK says "one such line at 3 to 3.5 SE ... is
  expected by chance"; the numbers above (1.00 expected per run on public_02) back that up and could be
  quoted there. Not edited.
- The `crash_recovery.md` sentence "I did not prove that each of the 26 extra decision rows is an exact
  repeat" is now answered (23 exact, 3 explained); not edited, left to the lead.
- The v9 voice V3 group (z = -4.07) is a loose end.
- The kill between `auto_apply` and `submit_approval` has no deterministic fault kind.
