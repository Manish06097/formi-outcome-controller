#!/usr/bin/env bash
# scripts/kill_test.sh [scenario.yaml]     (default scenarios/public_01_dev.yaml)
#
# Crash-recovery demonstration of DESIGN.md 10.2 and 15.1 M1 (4), through ./run.sh and Docker:
#  1. a clean run of the scenario on a fresh simulator;
#  2. the same scenario on another fresh simulator, where the controller is killed
#     - deterministically, by CTL_FAULT (os._exit(137), the effect of SIGKILL): in the middle of
#       a history chunk's COPY, right after an advance took effect in the simulator but before its
#       result was recorded, after an advance intent was written but before the call, and inside
#       a chunk transform before COMMIT;
#     - and at random wall times by `docker kill -s KILL` on the controller container
#       (CTL_KILLS times, every CTL_KILL_MIN_S..CTL_KILL_MAX_S seconds);
#     and run.sh restarts it every time;
#  3. scripts/compare_runs.py: the crashed run must have the same advances, exact event counts
#     and identical facts and rollups as the clean run.
# Writes reports/kill_test.md (CTL_KILL_REPORT). Exit 0 on PASS.
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:-scenarios/public_01_dev.yaml}"
PORT="${CTL_KILL_SIM_PORT:-8711}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-formi_t1}"
export CTL_PG_PORT="${CTL_PG_PORT:-55441}"
export CTL_MAX_RESTARTS="${CTL_MAX_RESTARTS:-40}"
export CTL_KEEP_UP=1   # compare_runs.py reads the store after run.sh exits
KILLS="${CTL_KILLS:-4}"
KMIN="${CTL_KILL_MIN_S:-3}"
KMAX="${CTL_KILL_MAX_S:-10}"
FAULTS="${CTL_KILL_FAULTS:-chunk_copy:1:mid,advance:20:after_http,advance:40:after_t1,chunk_transform:30:in_t2}"
OUT="${CTL_KILL_REPORT:-reports/kill_test.md}"
PY="${CTL_PYTHON:-.venv/bin/python}"
DSN="host=127.0.0.1 port=$CTL_PG_PORT user=postgres password=ctl dbname=postgres"
# Not under TMPDIR: docker CLI plugins put a unix socket there, and a long TMPDIR breaks it.
WORK="${CTL_KILL_WORK:-$(mktemp -d /tmp/formi_kill.XXXXXX)}"
mkdir -p "$WORK"
log() { printf '[kill_test] %s\n' "$*" | tee -a "$WORK/kill.log" >&2; }

sim_pid=""
killer_pid=""
cleanup() {
  [ -n "$killer_pid" ] && kill "$killer_pid" 2>/dev/null || true
  [ -n "$sim_pid" ] && kill "$sim_pid" 2>/dev/null || true
}
trap cleanup EXIT

start_sim() {
  "$PY" -m sim serve --scenario "$SCENARIO" --run-dir "$WORK/$1" --port "$PORT" --force \
    > "$WORK/$1.log" 2>&1 &
  sim_pid=$!
  until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
    kill -0 "$sim_pid" 2>/dev/null || { cat "$WORK/$1.log" >&2; exit 1; }
    sleep 1
  done
}
stop_sim() { kill "$sim_pid" 2>/dev/null || true; wait "$sim_pid" 2>/dev/null || true; sim_pid=""; }
latest_done() {
  "$PY" - "$DSN" <<'EOF'
import sys, psycopg
with psycopg.connect(sys.argv[1]) as c:
    print(c.execute("SELECT schema_name FROM ctl.run_registry WHERE status = 'done' "
                    "ORDER BY created_wall DESC LIMIT 1").fetchone()[0])
EOF
}

log "clean run of $SCENARIO (work dir $WORK)"
start_sim clean
CTL_FAULT= ./run.sh "http://127.0.0.1:$PORT" > "$WORK/run_clean.log" 2>&1
clean_schema="$(latest_done)"
stop_sim
log "clean run done: $clean_schema"

log "crashed run: CTL_FAULT=$FAULTS plus $KILLS random docker kills every ${KMIN}-${KMAX} s"
start_sim crashed
killer() {
  for i in $(seq 1 "$KILLS"); do
    sleep $((KMIN + RANDOM % (KMAX - KMIN + 1)))
    c="$(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
         --filter "label=com.docker.compose.service=controller" | head -1)"
    if [ -n "$c" ]; then
      docker kill -s KILL "$c" >/dev/null 2>&1 && log "kill $i: SIGKILL container $c at $(date +%T)"
    else
      log "kill $i: no controller container running at $(date +%T)"
    fi
  done
}
killer &
killer_pid=$!
CTL_FAULT="$FAULTS" ./run.sh "http://127.0.0.1:$PORT" > "$WORK/run_crashed.log" 2>&1
kill "$killer_pid" 2>/dev/null || true
killer_pid=""
curl -sf "http://127.0.0.1:$PORT/sim/state" > "$WORK/state.json"
crashed_schema="$(latest_done)"
stop_sim
restarts="$(grep -c 'controller exited' "$WORK/run_crashed.log" || true)"
log "crashed run done: $crashed_schema after $restarts restarts"

{
  echo "# Kill test: $SCENARIO"
  echo
  echo "Produced by \`scripts/kill_test.sh\` on $(date +%F) (measured on this Mac, Docker Desktop)."
  echo "Faults: \`$FAULTS\`; random SIGKILLs: $KILLS every ${KMIN}-${KMAX} s; run.sh restarts: $restarts."
  echo
  echo '```'
  grep -E 'kill |restart|exited' "$WORK/kill.log" "$WORK/run_crashed.log" | sed 's#^.*/##' || true
  echo '```'
  echo
} > "$OUT"
"$PY" scripts/compare_runs.py --dsn "$DSN" --clean "$clean_schema" --crashed "$crashed_schema" \
  --state "$WORK/state.json" >> "$OUT"
status=$?
cat "$OUT"
exit $status
