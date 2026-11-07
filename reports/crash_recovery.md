# Kill test: scenarios/public_01_dev.yaml

Produced by `scripts/kill_test.sh` on 2026-11-07 (measured on this Mac, no Docker: run_local.sh against a local Postgres).
Faults: `chunk_copy:1:mid,advance:20:after_http,advance:40:after_t1,chunk_transform:30:in_t2`; random SIGKILLs: 8 every 3-12 s; run.sh restarts: 11.

```
kill.log:[kill_test] kill 1: SIGKILL pid 57768 at 21:37:32
kill.log:[kill_test] kill 2: SIGKILL pid 57854 at 21:37:43
kill.log:[kill_test] kill 3: SIGKILL pid 58087 at 21:37:55
kill.log:[kill_test] kill 4: SIGKILL pid 58249 at 21:38:06
kill.log:[kill_test] kill 5: SIGKILL pid 58425 at 21:38:13
kill.log:[kill_test] kill 6: SIGKILL pid 58526 at 21:38:22
kill.log:[kill_test] kill 7: SIGKILL pid 58638 at 21:38:33
kill.log:[kill_test] kill 8: SIGKILL pid 58795 at 21:38:42
kill.log:[kill_test] crashed run done: run_51cc279ffa02_12 after 11 restarts
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 2
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
run_crashed.log:[run_local] controller exited 137; restart 1
```

Clean schema `run_51cc279ffa02_11`, crashed schema `run_51cc279ffa02_12`.

- Advances: 131 clean, 131 crashed, same stops in the same order.
- Ingested events 297,055 in 123 chunks vs /sim/state last_event_id 297,055 and 123 chunks: exact.
- Intents left started: 0.
- Faults fired and intents reconciled (audit log): CTL_FAULT chunk_copy:1:mid; CTL_FAULT advance:20:after_http; advance reconciled: done; CTL_FAULT chunk_transform:30:in_t2

| Table | Clean rows | Crashed rows | Content |
|---|---|---|---|
| `cell` | 144 | 144 | identical |
| `campaign` | 66 | 66 | identical |
| `lead_dim` | 72,600 | 72,600 | identical |
| `call_fact` | 83,200 | 83,200 | identical |
| `config_event` | 27 | 27 | identical |
| `approval_event` | 25 | 25 | identical |
| `event_other` | 0 | 0 | identical |
| `config_version` | 30 | 30 | identical |
| `config_cohort` | 220 | 220 | identical |
| `r_engage` | 19,678 | 19,678 | identical |
| `r_pickup` | 112,920 | 112,920 | identical |
| `r_did` | 22,721 | 22,721 | identical |
| `r_flags` | 740 | 740 | identical |
| `workflow` | 3 | 3 | identical |
| `allocation_provenance` | 2,108 | 2,108 | identical |
| `ingested_chunk` | 123 | 123 | identical |

**PASS**

## Grade report of the crashed run against the clean run

`sim grade` output is byte-identical for the clean and the crashed run.
