#!/usr/bin/env bash
# scripts/load_test.sh [scenario.yaml]     (default scenarios/public_03_load.yaml)
#
# Load test of DESIGN.md 10.1 and 15.1 M1 (2): serves the scenario on a real simulator, runs
# ./run.sh against it (Postgres and the controller in containers), samples the memory of the
# controller and Postgres containers (docker stats) and of the simulator process every 2 s, then
# writes reports/public_03_load.md with wall time per sim day, events per second, peak RSS and
# DB size, plus the correctness checks (ingested events equal /sim/state last_event_id; rollups
# equal a from-scratch recomputation). Tests and scripts may start the simulator; the controller
# never does.
#
# Environment: CTL_LOAD_SIM_PORT (8711), COMPOSE_PROJECT_NAME (formi_t1), CTL_PG_PORT (55441),
# CTL_LOAD_REPORT (reports/public_03_load.md), CTL_LOAD_LABEL (free text for the report header).
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:-scenarios/public_03_load.yaml}"
PORT="${CTL_LOAD_SIM_PORT:-8711}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-formi_t1}"
export CTL_PG_PORT="${CTL_PG_PORT:-55441}"
OUT="${CTL_LOAD_REPORT:-reports/public_03_load.md}"
PY="${CTL_PYTHON:-.venv/bin/python}"
# Not under TMPDIR: docker CLI plugins put a unix socket there, and a long TMPDIR breaks it.
WORK="${CTL_LOAD_WORK:-$(mktemp -d /tmp/formi_load.XXXXXX)}"
mkdir -p "$WORK"
echo "[load_test] work dir $WORK" >&2

sim_pid=""
sampler_pid=""
cleanup() {
  [ -n "$sampler_pid" ] && kill "$sampler_pid" 2>/dev/null || true
  [ -n "$sim_pid" ] && kill "$sim_pid" 2>/dev/null || true
}
trap cleanup EXIT

t0=$(date +%s)
"$PY" -m sim serve --scenario "$SCENARIO" --run-dir "$WORK/sim" --port "$PORT" --force \
  > "$WORK/sim.log" 2>&1 &
sim_pid=$!
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
  kill -0 "$sim_pid" 2>/dev/null || { cat "$WORK/sim.log" >&2; exit 1; }
  sleep 1
done
t_ready=$(date +%s)
echo "[load_test] simulator ready after $((t_ready - t0)) s (history generation)" >&2

# No Docker (CTL_NO_DOCKER=1 PG_DSN=...): run.sh runs scripts/run_local.sh; memory is the RSS of the
# controller process and the sum of the RSS of all processes of the Postgres cluster at CTL_PG_PORT
# (an upper bound: shared buffers count once per backend that touched them).
NO_DOCKER="${CTL_NO_DOCKER:-0}"
sample_local() {
  now=$(date +%s)
  rp="$(pgrep -f "run_local.sh http://127.0.0.1:$PORT" | head -1 || true)"
  cp="$([ -n "$rp" ] && pgrep -P "$rp" -f controller | head -1 || true)"
  [ -n "$cp" ] && printf '%s\tlocal-controller-1\t%s KiB\t-\n' "$now" "$(ps -o rss= -p "$cp" | tr -d ' ')" >> "$WORK/stats.tsv"
  pm="$(head -1 "${PGDATA_DIR:-/opt/homebrew/var/postgresql@17}/postmaster.pid" 2>/dev/null || true)"
  if [ -n "$pm" ]; then
    kb="$({ echo "$pm"; pgrep -P "$pm"; } | xargs -I{} ps -o rss= -p {} 2>/dev/null | awk '{s+=$1} END {print s+0}')"
    printf '%s\tlocal-db-1\t%s KiB\t-\n' "$now" "$kb" >> "$WORK/stats.tsv"
  fi
}

sample() {
  while true; do
    now=$(date +%s)
    if [ "$NO_DOCKER" = 1 ]; then sample_local; else
    docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' \
      $(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME") \
      2>/dev/null | sed "s/^/$now\t/" >> "$WORK/stats.tsv" || true
    fi
    printf '%s\tsimulator\t%s KiB\t-\n' "$now" "$(ps -o rss= -p "$sim_pid" | tr -d ' ')" \
      >> "$WORK/stats.tsv"
    sleep 2
  done
}
sample &
sampler_pid=$!

t_run=$(date +%s)
./run.sh "http://127.0.0.1:$PORT" > "$WORK/run.log" 2>&1
t_done=$(date +%s)
curl -sf "http://127.0.0.1:$PORT/sim/state" > "$WORK/state.json"
kill "$sampler_pid" 2>/dev/null || true
sampler_pid=""
echo "[load_test] run.sh finished in $((t_done - t_run)) s" >&2

"$PY" scripts/load_report.py --dsn "host=127.0.0.1 port=$CTL_PG_PORT user=postgres password=ctl dbname=postgres" \
  --state "$WORK/state.json" --stats "$WORK/stats.tsv" --scenario "$SCENARIO" \
  --sim-ready-s "$((t_ready - t0))" --run-s "$((t_done - t_run))" \
  --label "${CTL_LOAD_LABEL:-measured on this Mac}" --out "$OUT"
echo "[load_test] wrote $OUT (logs in $WORK)" >&2
