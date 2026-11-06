#!/usr/bin/env bash
# scripts/run_local.sh <SIM_URL>   (what `CTL_NO_DOCKER=1 ./run.sh <SIM_URL>` runs)
#
# The controller without Docker: a local Python 3.11 with the dependencies of
# requirements-runtime.lock (the repo's .venv when present) and a Postgres 16 at PG_DSN.
# Same restart loop, progress rule, limp mode and exit codes as run.sh (ADR 0019).
set -euo pipefail
SIM_URL="${1:?usage: scripts/run_local.sh <SIM_URL>}"
cd "$(dirname "$0")/.."
: "${PG_DSN:?set PG_DSN, e.g. postgresql://postgres:ctl@127.0.0.1:5432/postgres}"
PY="${CTL_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi
log() { printf '[run_local] %s\n' "$*" >&2; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
  || { log "Python 3.11+ required ($PY)"; exit 3; }
"$PY" -c 'import psycopg, psycopg_pool, httpx, numpy, yaml' 2>/dev/null \
  || { log "missing dependencies: $PY -m pip install --require-hashes -r requirements-runtime.lock"; exit 3; }
export CTL_SIM_URL="$SIM_URL" PG_DSN
if [ -z "${CTL_CODE_VERSION:-}" ]; then
  CTL_CODE_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
fi
export CTL_CODE_VERSION

progress_mark() {
  "$PY" -c '
import os, psycopg
with psycopg.connect(os.environ["PG_DSN"], connect_timeout=5) as c:
    print(c.execute("SELECT schema_name, status, last_sim_minute FROM ctl.run_registry "
                    "ORDER BY created_wall DESC LIMIT 1").fetchone())' 2>/dev/null || true
}

limp() {
  [ "${CTL_LIMP:-1}" = "1" ] || return 1
  log "last resort: advancing the scenario to its end with no controller logic (limp mode)"
  CTL_LIMP=1 CTL_SIM_WAIT="${CTL_LIMP_SIM_WAIT:-30}" "$PY" -m controller
}

restarts=0
last_mark="$(progress_mark)"
while true; do
  set +e
  "$PY" -m controller
  code=$?
  set -e
  [ "$code" -eq 0 ] && exit 0
  if [ "$code" -eq 3 ]; then
    log "fatal configuration error, not restarting"
    if limp; then log "RESULT scenario finished DEGRADED (limp mode)"; exit 0; fi
    exit 3
  fi
  mark="$(progress_mark)"
  if [ -n "$mark" ] && [ "$mark" != "$last_mark" ]; then
    restarts=0
    last_mark="$mark"
  fi
  restarts=$((restarts + 1))
  if [ "$restarts" -ge "${CTL_MAX_RESTARTS:-30}" ]; then
    log "controller failed $restarts times with no progress (last exit $code)"
    if limp; then log "RESULT scenario finished DEGRADED (limp mode)"; exit 0; fi
    exit "$code"
  fi
  log "controller exited $code; restart $restarts"
  sleep "${CTL_RESTART_SLEEP_S:-2}"
done
