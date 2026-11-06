# 0001. Stack: Python service, Postgres store, no LLM framework

Status: accepted (2026-11-06)

## Context
- The controller must ingest about 850k events per simulated day (public_03), decide, and act
  through governance, on a 4 vCPU / 8 GB Linux box, started by `./run.sh <SIM_URL>` with no manual
  steps. Docker is explicitly allowed.
- It must survive `kill -9` at any point: no double-apply, no lost pending approval, no lost
  learning. Every decision must be queryable from our own store, including per-call answers to
  "why did this customer hear this variant" (Compliance memo, IRDAI).
- Platform/SRE already runs Postgres and Redis and does not want new stateful infrastructure, and
  nodes get recycled in production, so state on a node's local disk is not durable there.
- Every dependency has to be defended.

## Decision
- **Python 3.11**, one long-running controller process.
- **Postgres 16** as the only stateful component: ingest cursor, per-call facts, cohort and variant
  aggregates, experiment state, proposal lifecycle, audit log. Each loop step commits its state
  change and its audit record in one transaction.
- **Libraries:** `httpx` (HTTP client), `numpy` (statistics), `psycopg` 3 (Postgres, COPY for bulk
  ingest), `fastapi` + `uvicorn` (read-only supervisor and ops views). All but psycopg are already
  in `requirements.txt`.
- **Packaging:** Docker Compose (Postgres + controller), launched by `run.sh`.
- **No LangChain, LangGraph or other LLM framework**, and no external LLM calls. Constraint 4
  forbids an LLM from choosing winners. The only LLM in scope is the simulator's
  `/llm/suggest-fixes` endpoint, one HTTP call. Proposal text is templated, so it is deterministic
  and auditable.

## Alternatives considered
- **SQLite (WAL):** zero infrastructure and fast enough for public_03. Rejected as the primary store
  because it is node-local state (lost on node recycle in production), single-writer, and does not
  match what SRE operates. It remains a reasonable fallback if the grading box cannot run Docker.
- **Redis as a queue or state store:** not needed; one process pulls chunks in order, so there is
  no queue to manage, and Redis persistence is weaker than Postgres for an audit record.
- **LangGraph state machines for the proposal lifecycle:** its checkpointing would duplicate state
  that must live in the same transaction as our audit log anyway.
- **Go or Rust for ingest speed:** the bottleneck is likely the simulator's own Python loop, and a
  second language costs reviewers and owners more than it saves. To be revisited only if
  measurements show the ingest path in Python cannot keep a public_03 day under 10 minutes.

## Consequences
- `run.sh` depends on Docker being available on the grading box (stated in `run.sh`).
- The controller container must reach the simulator on the host (`SIM_URL`), handled in the
  compose and run.sh design.
- Crash tests are realistic: killing the controller leaves Postgres running, like production.
