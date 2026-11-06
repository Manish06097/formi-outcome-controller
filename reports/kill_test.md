# Kill test: scenarios/public_01_dev.yaml

Produced by `scripts/kill_test.sh` on 2026-11-06 (measured on this Mac, Docker Desktop).
Faults: `chunk_copy:1:mid,advance:20:after_http,advance:40:after_t1,chunk_transform:30:in_t2`; random SIGKILLs: 4 every 3-10 s; run.sh restarts: 8.

```
kill.log:[kill_test] kill 1: SIGKILL container e5e93633d889 at 21:35:45
kill.log:[kill_test] kill 2: SIGKILL container f04d76596344 at 21:35:53
kill.log:[kill_test] kill 3: SIGKILL container 2a32a277c96a at 21:36:03
kill.log:[kill_test] kill 4: SIGKILL container d6118e20bb75 at 21:36:06
kill.log:[kill_test] crashed run done: run_214eb8b3e18e_6 after 8 restarts
run_crashed.log:[run.sh] controller exited 137; restart 1
run_crashed.log:[run.sh] controller exited 137; restart 2
run_crashed.log:[run.sh] controller exited 137; restart 3
run_crashed.log:[run.sh] controller exited 137; restart 4
run_crashed.log:[run.sh] controller exited 137; restart 5
run_crashed.log:[run.sh] controller exited 137; restart 6
run_crashed.log:[run.sh] controller exited 137; restart 7
run_crashed.log:[run.sh] controller exited 137; restart 8
```

Clean schema `run_214eb8b3e18e_5`, crashed schema `run_214eb8b3e18e_6`.

- Advances: 121 clean, 121 crashed, same stops in the same order.
- Ingested events 297,003 in 113 chunks vs /sim/state last_event_id 297,003 and 113 chunks: exact.
- Intents left started: 0.
- Faults fired and intents reconciled (audit log): CTL_FAULT chunk_copy:1:mid; CTL_FAULT advance:20:after_http; advance reconciled: done; advance reconciled: done; CTL_FAULT chunk_transform:30:in_t2; CTL_FAULT advance:40:after_t1; advance reconciled: not_run

| Table | Clean rows | Crashed rows | Content |
|---|---|---|---|
| `cell` | 144 | 144 | identical |
| `campaign` | 66 | 66 | identical |
| `lead_dim` | 72,600 | 72,600 | identical |
| `call_fact` | 83,200 | 83,200 | identical |
| `config_event` | 0 | 0 | identical |
| `approval_event` | 0 | 0 | identical |
| `event_other` | 0 | 0 | identical |
| `config_version` | 3 | 3 | identical |
| `config_cohort` | 3 | 3 | identical |
| `r_engage` | 11,356 | 11,356 | identical |
| `r_pickup` | 113,205 | 113,205 | identical |
| `r_did` | 5,039 | 5,039 | identical |
| `r_flags` | 740 | 740 | identical |
| `workflow` | 3 | 3 | identical |
| `allocation_provenance` | 14 | 14 | identical |
| `ingested_chunk` | 113 | 113 | identical |

**PASS**
