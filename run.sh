#!/usr/bin/env bash
# ./run.sh <SIM_URL>
#
# Starts Postgres and the controller in containers and runs until the scenario at SIM_URL
# finishes (DESIGN.md 11.3, ADR 0019). Requires Docker with Compose v2. State lives only in
# Postgres (named volume pgdata, never wiped here), so the controller is restarted after a crash
# and resumes; runs are separated by schema, not by volume.
#
# Exit codes: 0 the scenario finished; 3 fatal configuration error (not restarted); otherwise the
# controller's last exit code after CTL_MAX_RESTARTS restarts.
#
# No Docker: CTL_NO_DOCKER=1 PG_DSN=postgresql://... ./run.sh <SIM_URL>  (local venv, Postgres 16)
#
# Optional environment: COMPOSE_PROJECT_NAME (default formi_ctl), CTL_PG_PORT (default: the first
# free port in 55432..55451, or the port of this project's running db), CTL_MAX_RESTARTS (20),
# CTL_START_API (1: leave the read-only API up after the run when it is built), and every CTL_*
# knob that compose.yml passes through.
set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"

if [ "${CTL_NO_DOCKER:-0}" = "1" ]; then
  exec scripts/run_local.sh "$SIM_URL"
fi

log() { printf '[run.sh] %s\n' "$*" >&2; }
die() { log "$1"; exit "${2:-3}"; }

command -v docker >/dev/null 2>&1 || die "docker not found (or use CTL_NO_DOCKER=1 PG_DSN=...)"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
docker info >/dev/null 2>&1 || die "the docker daemon is not reachable"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-formi_ctl}"
case "$(uname -s)" in
  Linux)
    FILES=(-f compose.yml -f compose.linux.yml)
    CTL_SIM_URL="$SIM_URL" ;;                        # host network: loopback is the host's
  *)
    FILES=(-f compose.yml -f compose.desktop.yml)
    CTL_SIM_URL="$(printf '%s' "$SIM_URL" | sed -E 's#//(127\.0\.0\.1|localhost)([:/]|$)#//host.docker.internal\2#')" ;;
esac
export CTL_SIM_URL
compose() { docker compose "${FILES[@]}" "$@"; }

# ---------------------------------------------------------------- Postgres port
port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -z "${CTL_PG_PORT:-}" ]; then
  running="$(compose port db 5432 2>/dev/null | sed -E 's/.*:([0-9]+)$/\1/' || true)"
  if [ -n "$running" ]; then
    CTL_PG_PORT="$running"                           # our own db is already up: reuse it
  else
    for p in $(seq 55432 55451); do
      if port_free "$p"; then CTL_PG_PORT="$p"; break; fi
    done
  fi
fi
[ -n "${CTL_PG_PORT:-}" ] || die "no free port in 55432..55451 for Postgres (set CTL_PG_PORT)"
export CTL_PG_PORT
log "project $COMPOSE_PROJECT_NAME, Postgres on 127.0.0.1:$CTL_PG_PORT, simulator $CTL_SIM_URL"

# ---------------------------------------------------------------- images
for img in postgres:16 python:3.11-slim; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    log "pulling $img"
    docker pull -q "$img" >/dev/null || die "base image $img is neither cached nor pullable"
  fi
done
CTL_CODE_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
export CTL_CODE_VERSION
compose build --quiet controller || die "image build failed" 1
compose up -d --wait db || die "Postgres did not become healthy" 1

# ---------------------------------------------------------------- controller, restarted after a crash
restarts=0
while true; do
  set +e
  compose run --rm -T controller
  code=$?
  set -e
  if [ "$code" -eq 0 ]; then
    log "scenario finished"
    break
  fi
  if [ "$code" -eq 3 ]; then
    die "fatal configuration error, not restarting" 3
  fi
  restarts=$((restarts + 1))
  if [ "$restarts" -ge "${CTL_MAX_RESTARTS:-20}" ]; then
    die "controller failed $restarts times (last exit $code)" "$code"
  fi
  log "controller exited $code; restart $restarts"
  sleep 2
done

# ---------------------------------------------------------------- leave the read-only views up
if [ "${CTL_START_API:-1}" = "1" ] && grep -q "FastAPI(" controller/api/app.py 2>/dev/null; then
  compose --profile api up -d api >/dev/null 2>&1 || log "read-only API did not start (not fatal)"
fi
exit 0
