# Crash recovery on public_02 (final code)

Final run on HEAD of `controller` (2026-11-08), scenario `scenarios/public_02_incidents.yaml` (the incident scenario: DID flag, supervisor prompt edit, 24 approvals, so the kills land among live proposals and incidents).

**Summary: 27 random `kill -9` of the controller process plus 4 deterministic `CTL_FAULT` exits = 31 controller deaths in one run (31 restarts by `run.sh`), against the 20 that DESIGN 10.2 promised. The crashed run finished with the same final state as the uninterrupted run: PASS.** The script scheduled 30 random kills (every 2 to 5 s); 3 of them found no controller process running (it was between restarts), so 27 landed. Command:

```bash
export CTL_NO_DOCKER=1 PG_DSN="host=127.0.0.1 port=55479 user=postgres password=ctl dbname=postgres" CTL_PG_PORT=55479
CTL_KILL_SIM_PORT=8763 CTL_KILLS=30 CTL_KILL_MIN_S=2 CTL_KILL_MAX_S=5 CTL_KILL_BASELINE=runs/base_public_02_incidents \
    CTL_KILL_REPORT=reports/crash_recovery.md scripts/kill_test.sh scenarios/public_02_incidents.yaml
```

Equivalence checks, in order of strength:
1. `scripts/compare_runs.py` (below): the same 184 advances in the same order, every event ingested exactly once, 16 fact, rollup, catalog and config tables identical row for row, 0 intents left `started`. PASS.
2. `sim grade` output of the crashed run is byte-identical to the clean run's (below).
3. The crash-equivalence machinery of the e2e tests (`tests/e2e/platform/helpers.py` `dump_schema` and `diff_dumps`) over the whole run schema (52 tables, 366,208 rows), run after the fact: the 15 domain tables are identical, and so are `proposal` (38 rows, every column except the decision id and the evidence/verify JSON, apart from the `rationale` text, which embeds decision numbers), `approval_mirror` (24 rows), `config_version`, `config_cohort` and `alert` count (26). The remaining differences are expected and are not state: wall-clock and identity columns (`decision_id`, `audit_id`, `intent_id` numbering, `loop_step` chunk counts per step, `posterior_snapshot`) and **extra rows in the crashed run**: `decision` 712 against 686, `audit_log` 536 against 530 (the fault-injection and restart rows), `intent` 247 against 246, `patch` 521 against 504, `posterior_snapshot` 131 against 120. A killed step is redone from the intent log, so some decisions are made twice (the redo is written again with a new id); the applied configuration and the approvals do not change. I did not prove that each of the 26 extra decision rows is an exact repeat of a killed one; the evidence is that every table that defines state (config versions, proposals, approvals, alerts, credit ledger totals) and the grader's verdict match.


Produced by `scripts/kill_test.sh` on 2026-11-08 (measured on this Mac, no Docker: run_local.sh against a local Postgres).
Faults: `chunk_copy:1:mid,advance:20:after_http,advance:40:after_t1,chunk_transform:30:in_t2`; random SIGKILLs: 30 every 2-5 s; run.sh restarts: 31.

```
kill.log:[kill_test] kill 1: SIGKILL pid 49360 at 14:02:02
kill.log:[kill_test] kill 2: SIGKILL pid 49379 at 14:02:05
kill.log:[kill_test] kill 3: SIGKILL pid 49424 at 14:02:08
kill.log:[kill_test] kill 4: SIGKILL pid 49467 at 14:02:14
kill.log:[kill_test] kill 5: SIGKILL pid 49513 at 14:02:17
kill.log:[kill_test] kill 6: SIGKILL pid 49555 at 14:02:21
kill.log:[kill_test] kill 7: SIGKILL pid 49602 at 14:02:24
kill.log:[kill_test] kill 8: no controller process running at 14:02:26
kill.log:[kill_test] kill 9: SIGKILL pid 49657 at 14:02:31
kill.log:[kill_test] kill 10: SIGKILL pid 49730 at 14:02:36
kill.log:[kill_test] kill 11: no controller process running at 14:02:38
kill.log:[kill_test] kill 12: SIGKILL pid 49795 at 14:02:42
kill.log:[kill_test] kill 13: SIGKILL pid 49861 at 14:02:47
kill.log:[kill_test] kill 14: SIGKILL pid 49921 at 14:02:52
kill.log:[kill_test] kill 15: SIGKILL pid 50076 at 14:02:57
kill.log:[kill_test] kill 16: SIGKILL pid 50132 at 14:03:02
kill.log:[kill_test] kill 17: SIGKILL pid 50183 at 14:03:06
kill.log:[kill_test] kill 18: SIGKILL pid 50232 at 14:03:11
kill.log:[kill_test] kill 19: SIGKILL pid 50291 at 14:03:16
kill.log:[kill_test] kill 20: SIGKILL pid 50342 at 14:03:21
kill.log:[kill_test] kill 21: SIGKILL pid 50391 at 14:03:26
kill.log:[kill_test] kill 22: SIGKILL pid 50451 at 14:03:30
kill.log:[kill_test] kill 23: no controller process running at 14:03:32
kill.log:[kill_test] kill 24: SIGKILL pid 50508 at 14:03:37
kill.log:[kill_test] kill 25: SIGKILL pid 50567 at 14:03:41
kill.log:[kill_test] kill 26: SIGKILL pid 50622 at 14:03:45
kill.log:[kill_test] kill 27: SIGKILL pid 50673 at 14:03:48
kill.log:[kill_test] kill 28: SIGKILL pid 50710 at 14:03:52
kill.log:[kill_test] kill 29: SIGKILL pid 50859 at 14:03:56
kill.log:[kill_test] kill 30: SIGKILL pid 50915 at 14:04:00
kill.log:[kill_test] crashed run done: run_4bdc1ff90f7d_6 after 31 restarts
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 3
run_crashed.log:[run_local] controller exited 137; restart 4
run_crashed.log:[run_local] controller exited 137; restart 5
run_crashed.log:[run_local] controller exited 137; restart 6
run_crashed.log:[run_local] controller exited 137; restart 7
run_crashed.log:[run_local] controller exited 137; restart 8
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 3
run_crashed.log:[run_local] controller exited 137; restart 4
run_crashed.log:[run_local] controller exited 137; restart 5
run_crashed.log:[run_local] controller exited 137; restart 6
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 3
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
```

Clean schema `run_4bdc1ff90f7d_5`, crashed schema `run_4bdc1ff90f7d_6`.

- Advances: 184 clean, 184 crashed, same stops in the same order.
- Ingested events 324,338 in 174 chunks vs /sim/state last_event_id 324,338 and 174 chunks: exact.
- Intents left started: 0.
- Faults fired and intents reconciled (audit log): CTL_FAULT chunk_copy:1:mid; advance reconciled: done; CTL_FAULT advance:20:after_http; advance reconciled: done; CTL_FAULT chunk_transform:30:in_t2; CTL_FAULT advance:40:after_t1; advance reconciled: not_run

| Table | Clean rows | Crashed rows | Content |
|---|---|---|---|
| `cell` | 144 | 144 | identical |
| `campaign` | 72 | 72 | identical |
| `lead_dim` | 79,200 | 79,200 | identical |
| `call_fact` | 104,000 | 104,000 | identical |
| `config_event` | 34 | 34 | identical |
| `approval_event` | 24 | 24 | identical |
| `event_other` | 0 | 0 | identical |
| `config_version` | 37 | 37 | identical |
| `config_cohort` | 318 | 318 | identical |
| `r_engage` | 22,270 | 22,270 | identical |
| `r_pickup` | 123,330 | 123,330 | identical |
| `r_did` | 28,954 | 28,954 | identical |
| `r_flags` | 810 | 810 | identical |
| `workflow` | 3 | 3 | identical |
| `allocation_provenance` | 3,087 | 3,087 | identical |
| `ingested_chunk` | 174 | 174 | identical |

**PASS**

## Grade report of the crashed run against the clean run

`sim grade` output is byte-identical for the clean and the crashed run.
