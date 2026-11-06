#!/usr/bin/env bash
# scripts/run_local.sh <SIM_URL>   (what `CTL_NO_DOCKER=1 ./run.sh <SIM_URL>` runs)
#
# The controller without Docker: a local Python 3.11 with the dependencies of
# requirements-controller.txt (the repo's .venv when present) and a Postgres 16 at PG_DSN.
# Same restart loop and exit codes as run.sh (ADR 0019).
set -euo pipefail
SIM_URL="${1:?usage: scripts/run_local.sh <SIM_URL>}"
cd "$(dirname "$0")/.."
: "${PG_DSN:?set PG_DSN, e.g. postgresql://postgres:ctl@127.0.0.1:5432/postgres}"
PY="${CTL_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
  || { echo "[run_local] Python 3.11+ required ($PY)" >&2; exit 3; }
"$PY" -c 'import psycopg, psycopg_pool, httpx, numpy, yaml' 2>/dev/null \
  || { echo "[run_local] missing dependencies: $PY -m pip install -r requirements-controller.txt" >&2; exit 3; }
export CTL_SIM_URL="$SIM_URL" PG_DSN
if [ -z "${CTL_CODE_VERSION:-}" ]; then
  CTL_CODE_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
fi
export CTL_CODE_VERSION
restarts=0
while true; do
  set +e
  "$PY" -m controller
  code=$?
  set -e
  [ "$code" -eq 0 ] && exit 0
  if [ "$code" -eq 3 ]; then
    echo "[run_local] fatal configuration error, not restarting" >&2
    exit 3
  fi
  restarts=$((restarts + 1))
  if [ "$restarts" -ge "${CTL_MAX_RESTARTS:-20}" ]; then
    echo "[run_local] controller failed $restarts times" >&2
    exit "$code"
  fi
  echo "[run_local] controller exited $code; restart $restarts" >&2
  sleep 2
done
