#!/usr/bin/env bash
# ./run.sh <SIM_URL>
#
# Starts Postgres and the controller in Docker containers and runs until the scenario at SIM_URL
# finishes (DESIGN.md 11.3, ADR 0019). State lives only in Postgres (a named volume, never wiped
# here), so the controller is restarted after a crash and resumes; runs are separated by schema.
#
# WHAT THE BOX NEEDS
#   - Docker: a daemon this user can reach (root, the docker group, or passwordless sudo).
#     Docker Compose is NOT required. Order tried: `docker compose` (v2), `docker-compose` (v1),
#     plain `docker run`. Only the docker CLI and daemon are needed by the last one.
#   - Network, on the first run only:
#       registry-1.docker.io  the two base images, pinned by digest (postgres:16, python:3.11-slim)
#       pypi.org and files.pythonhosted.org  the locked wheels of requirements-runtime.lock
#     With no PyPI: run scripts/vendor_wheels.sh on a machine that has it and copy vendor/wheels/
#     here, or set PIP_INDEX_URL to a mirror. Images already cached are never pulled again.
#   - Ports on 127.0.0.1: the first free one of 55432..55451 for Postgres. Linux: the controller
#     uses the host network, so SIM_URL may be 127.0.0.1.
#   - Memory limits: Postgres 3 GB, controller 2 GB; both are stopped when this script exits.
#
# EXIT CODES
#   0  the scenario finished. The last line says DEGRADED if it was only advanced to its end
#      because the controller could not run at all (limp mode, see below).
#   3  fatal: the simulator never answered, so nothing can finish the scenario.
#   other  the scenario did not finish; the controller's last exit code.
#
# WHAT IT DOES ON FAILURE (an unfinished scenario is the worst outcome)
#   - A crash restarts the controller. The restart budget (CTL_MAX_RESTARTS, 30) counts restarts
#     with no progress in the store and resets whenever the run moves on.
#   - A hook that keeps crashing is put in safe mode by the controller itself (audit row and a
#     SAFE_MODE page; controller/loop/safemode.py): the loop keeps advancing without it.
#   - If the budget runs out, the configuration is fatal, or Docker cannot run the controller at
#     all, limp mode advances the simulator to the end with no decisions (CTL_LIMP=0 disables).
#
# No Docker: CTL_NO_DOCKER=1 PG_DSN=postgresql://... ./run.sh <SIM_URL>  (local venv, Postgres 16)
#
# Optional environment: COMPOSE_PROJECT_NAME (formi_ctl), CTL_PG_PORT, CTL_MAX_RESTARTS,
# CTL_KEEP_UP (1: leave Postgres and the read-only API up after the run, on CTL_API_PORT 8780),
# CTL_DOCKER_BACKEND (auto | compose2 | compose1 | plain), CTL_NET_MODE (auto | host | desktop),
# PIP_INDEX_URL, and every other CTL_* setting, which is passed into the controller's container.
set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"

if [ "${CTL_NO_DOCKER:-0}" = "1" ]; then
  exec scripts/run_local.sh "$SIM_URL"
fi

log() { printf '[run.sh] %s\n' "$*" >&2; }

# Consumed here, never passed into the controller's container.
OWN_VARS=" CTL_SIM_URL CTL_PG_HOST CTL_PG_PORT CTL_PG_PASSWORD CTL_PG_IMAGE CTL_MAX_RESTARTS \
CTL_KEEP_UP CTL_DOCKER_BACKEND CTL_NET_MODE CTL_API_PORT CTL_API_PG_PASSWORD CTL_LIMP_SIM_WAIT \
CTL_RESTART_SLEEP_S CTL_PYTHON CTL_NO_DOCKER "

PROJECT="${COMPOSE_PROJECT_NAME:-formi_ctl}"
export COMPOSE_PROJECT_NAME="$PROJECT"
IMAGE="$PROJECT-controller:latest"
export CTL_PG_PASSWORD="${CTL_PG_PASSWORD:-ctl}"
export CTL_API_PG_PASSWORD="${CTL_API_PG_PASSWORD:-ctl_api}"
export CTL_API_PORT="${CTL_API_PORT:-8780}"
PY_PIN="$(sed -n 's/^ARG PYTHON_BASE=//p' Dockerfile)"
PG_PIN="$(sed -nE 's/.*CTL_PG_IMAGE:-([^}]+)\}.*/\1/p' compose.yml)"
PG_IMAGE="${CTL_PG_IMAGE:-$PG_PIN}"
# One controller container per simulator; a second run.sh against the same URL replaces an orphan.
CTL_NAME="${PROJECT}_ctl_$(printf '%s' "${SIM_URL%/}" | cksum | cut -d' ' -f1)"
PLAIN_DB="${PROJECT}_plain_db"
PLAIN_API="${PROJECT}_plain_api"

DOCKER=(docker)
COMPOSE=()
BACKEND=""
NET=""
DB_CID=""
child=""
stop_on_exit=0

# ---------------------------------------------------------------- last resort: no controller at all
# Advance the simulator to the end from this box's own Python (standard library only). Used when
# Docker cannot run the controller. No store exists here, so the log is the only record.
limp_on_host() {
  [ "${CTL_LIMP:-1}" = "1" ] || return 1
  local py
  for py in "${CTL_PYTHON:-}" python3 python; do
    if [ -n "$py" ] && command -v "$py" >/dev/null 2>&1; then
      log "no controller could run: advancing the scenario to its end with $py (limp mode)"
      "$py" -I controller/loop/limp.py "$SIM_URL" && return 0
      return 1
    fi
  done
  log "no python on this box for limp mode"
  return 1
}

# give_up <exit code> <message>: Docker could not run the controller.
give_up() {
  log "$2"
  if limp_on_host; then
    log "RESULT scenario finished DEGRADED: limp mode, no controller ran ($2)"
    exit 0
  fi
  exit "$1"
}

# ---------------------------------------------------------------- docker, backend, network
find_docker() {
  command -v docker >/dev/null 2>&1 || give_up 3 "docker not found (or use CTL_NO_DOCKER=1 PG_DSN=...)"
  if docker info >/dev/null 2>&1; then return; fi
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    log "using sudo for docker"
    DOCKER=(sudo -n -E docker)
    return
  fi
  give_up 3 "the docker daemon is not reachable by this user"
}

pick_net_mode() {
  NET="${CTL_NET_MODE:-auto}"
  if [ "$NET" = "auto" ]; then
    NET=desktop
    if [ "$(uname -s)" = "Linux" ] && \
       ! "${DOCKER[@]}" info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qi 'docker desktop'; then
      NET=host
    fi
  fi
  case "$NET" in
    host) CTL_SIM_URL="$SIM_URL" ;;                  # host network: loopback is the host's
    desktop) CTL_SIM_URL="$(printf '%s' "$SIM_URL" | sed -E 's#//(127\.0\.0\.1|localhost)([:/]|$)#//host.docker.internal\2#')" ;;
    *) give_up 3 "CTL_NET_MODE must be auto, host or desktop, not $NET" ;;
  esac
  export CTL_SIM_URL
}

compose() {
  local overlay=compose.desktop.yml
  [ "$NET" = "host" ] && overlay=compose.linux.yml
  "${COMPOSE[@]}" -f compose.yml -f "$overlay" "$@"
}

pick_backend() {
  local want="${CTL_DOCKER_BACKEND:-auto}"
  case "$want" in auto|compose2|compose1|plain) ;; *) give_up 3 "bad CTL_DOCKER_BACKEND $want" ;; esac
  if { [ "$want" = auto ] || [ "$want" = compose2 ]; } && "${DOCKER[@]}" compose version >/dev/null 2>&1; then
    COMPOSE=("${DOCKER[@]}" compose); BACKEND=compose2; return
  fi
  if { [ "$want" = auto ] || [ "$want" = compose1 ]; } && command -v docker-compose >/dev/null 2>&1; then
    if [ "${DOCKER[0]}" = sudo ]; then COMPOSE=(sudo -n -E docker-compose); else COMPOSE=(docker-compose); fi
    if compose config -q >/dev/null 2>&1; then BACKEND=compose1; return; fi
    log "docker-compose $("${COMPOSE[@]}" version --short 2>/dev/null || echo '?') cannot read compose.yml"
  fi
  [ "$want" = auto ] || [ "$want" = plain ] || log "backend $want is not available"
  COMPOSE=(); BACKEND=plain
}

# ---------------------------------------------------------------- Postgres port
port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

own_db_port() {
  local out=""
  if [ "$BACKEND" = plain ]; then
    out="$("${DOCKER[@]}" port "$PLAIN_DB" 5432/tcp 2>/dev/null | head -1 || true)"
  else
    out="$(compose port db 5432 2>/dev/null | head -1 || true)"
  fi
  printf '%s' "$out" | sed -nE 's/.*:([0-9]+)$/\1/p'
}

pick_pg_port() {
  [ -n "${CTL_PG_PORT:-}" ] && [ "${PG_PORT_AUTO:-0}" = 0 ] && return 0
  PG_PORT_AUTO=1
  CTL_PG_PORT="$(own_db_port)"                       # our own db is already up: reuse it
  if [ -z "$CTL_PG_PORT" ]; then
    local p
    for p in $(seq 55432 55451); do
      if port_free "$p"; then CTL_PG_PORT="$p"; break; fi
    done
  fi
  [ -n "$CTL_PG_PORT" ] || give_up 3 "no free port in 55432..55451 for Postgres (set CTL_PG_PORT)"
}

# ---------------------------------------------------------------- images
have_image() { "${DOCKER[@]}" image inspect "$1" >/dev/null 2>&1; }

pull_image() {
  local i
  for i in 1 2 3; do
    "${DOCKER[@]}" pull -q "$1" >/dev/null 2>&1 && return 0
    [ "$i" -lt 3 ] && sleep 5
  done
  return 1
}

# usable_image <pinned ref>: prints the pinned ref when it is cached or pullable; else the same tag
# with a warning (a registry that dropped the digest must not stop the run; the Python packages
# stay locked either way).
usable_image() {
  local pin="$1" tag="${1%%@*}"
  if have_image "$pin"; then printf '%s' "$pin"; return 0; fi
  log "pulling $pin"
  if pull_image "$pin"; then printf '%s' "$pin"; return 0; fi
  log "cannot pull the pinned digest of $tag; trying the tag"
  if have_image "$tag" || pull_image "$tag"; then printf '%s' "$tag"; return 0; fi
  return 1
}

build_image() {
  local py_image logf
  py_image="$(usable_image "$PY_PIN")" \
    || give_up 1 "cannot get base image $PY_PIN: this box needs registry-1.docker.io or a cached copy"
  PG_IMAGE="$(usable_image "$PG_IMAGE")" \
    || give_up 1 "cannot get base image $PG_IMAGE: this box needs registry-1.docker.io or a cached copy"
  export CTL_PG_IMAGE="$PG_IMAGE"
  CTL_CODE_VERSION="${CTL_CODE_VERSION:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
  export CTL_CODE_VERSION
  # `docker build`, not `compose build`: recent Compose refuses to build without a recent buildx,
  # which distribution packages often lack. The plain builder needs neither.
  local args=(build -t "$IMAGE" --build-arg "PYTHON_BASE=$py_image"
              --build-arg "CODE_VERSION=$CTL_CODE_VERSION")
  [ -n "${PIP_INDEX_URL:-}" ] && args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
  logf="$(mktemp)"
  log "building $IMAGE"
  if ! "${DOCKER[@]}" "${args[@]}" . >"$logf" 2>&1; then
    log "image build failed on the default network; retrying on the host network"
    tail -5 "$logf" >&2 || true
    if ! "${DOCKER[@]}" "${args[@]}" --network host . >"$logf" 2>&1; then
      tail -30 "$logf" >&2 || true
      rm -f "$logf"
      give_up 1 "image build failed: this box needs pypi.org and files.pythonhosted.org, a mirror in PIP_INDEX_URL, or scripts/vendor_wheels.sh"
    fi
  fi
  rm -f "$logf"
}

# ---------------------------------------------------------------- Postgres
PG_SETTINGS=(-c shared_buffers=1GB -c effective_cache_size=3GB -c work_mem=64MB
             -c maintenance_work_mem=256MB -c max_wal_size=4GB -c checkpoint_timeout=15min
             -c wal_compression=on -c jit=off -c synchronous_commit=on)

start_db_plain() {
  # Its own volume: a second Postgres must never open the compose volume's data directory.
  "${DOCKER[@]}" rm -f "$PLAIN_DB" >/dev/null 2>&1 || true
  "${DOCKER[@]}" run -d --name "$PLAIN_DB" --label "formi.project=$PROJECT" \
    -e "POSTGRES_PASSWORD=$CTL_PG_PASSWORD" -p "127.0.0.1:$CTL_PG_PORT:5432" \
    -v "${PROJECT}_plain_pgdata:/var/lib/postgresql/data" --shm-size 1g --memory 3g \
    --stop-timeout 60 "$PG_IMAGE" postgres "${PG_SETTINGS[@]}" >/dev/null
  DB_CID="$PLAIN_DB"
}

start_db_compose() {
  # No --wait anywhere: Compose before 2.1.1 and v1 do not have it, and the poll below is the same.
  compose up -d db >/dev/null || return 1
  DB_CID="$(compose ps -q db)"
  [ -n "$DB_CID" ]
}

wait_db() {
  local deadline=$((SECONDS + ${CTL_DB_WAIT_S:-300}))
  until "${DOCKER[@]}" exec "$DB_CID" pg_isready -q -U postgres -h 127.0.0.1 >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      "${DOCKER[@]}" logs --tail 30 "$DB_CID" >&2 2>&1 || true
      return 1
    fi
    sleep 1
  done
}

start_db() {
  pick_pg_port
  export CTL_PG_PORT
  log "backend $BACKEND, network $NET, project $PROJECT, Postgres on 127.0.0.1:$CTL_PG_PORT, simulator $CTL_SIM_URL"
  if [ "$BACKEND" != plain ]; then
    if start_db_compose && wait_db; then return; fi
    log "$BACKEND could not start Postgres; falling back to plain docker"
    compose stop db >/dev/null 2>&1 || true
    COMPOSE=(); BACKEND=plain
    pick_pg_port
    export CTL_PG_PORT
    log "backend plain, Postgres on 127.0.0.1:$CTL_PG_PORT"
  fi
  start_db_plain || give_up 1 "docker could not start Postgres"
  wait_db || give_up 1 "Postgres did not become ready"
}

stop_db() {
  if [ "$BACKEND" = plain ]; then
    "${DOCKER[@]}" stop -t 60 "$PLAIN_DB" >/dev/null 2>&1 || true
    "${DOCKER[@]}" rm -f "$PLAIN_DB" "$PLAIN_API" >/dev/null 2>&1 || true
  elif [ -n "$BACKEND" ]; then
    compose --profile api stop -t 60 >/dev/null 2>&1 || compose stop -t 60 >/dev/null 2>&1 || true
  fi
}

# Written when the run is over: the data stays in the volume and the next run starts it again.
cleanup() {
  local code=$?
  trap - EXIT
  if [ "$stop_on_exit" = 1 ] && [ "${CTL_KEEP_UP:-0}" != 1 ]; then
    if [ -n "$("${DOCKER[@]}" ps -q --filter "name=^${PROJECT}_ctl_" 2>/dev/null)" ]; then
      log "another controller of project $PROJECT is running: leaving Postgres up"
    else
      log "stopping Postgres (data stays in its volume; CTL_KEEP_UP=1 leaves it up)"
      stop_db
    fi
  fi
  exit "$code"
}

# ---------------------------------------------------------------- the controller container
ENV_ARGS=()
collect_env() {
  local name
  ENV_ARGS=(-e "CTL_CODE_VERSION=$CTL_CODE_VERSION")
  for name in $(compgen -e | grep '^CTL_' || true); do
    case "$OWN_VARS" in *" $name "*) continue ;; esac
    ENV_ARGS+=(-e "$name=${!name}")
  done
}

remove_orphan() { "${DOCKER[@]}" rm -f "$CTL_NAME" >/dev/null 2>&1 || true; }

# run_controller [extra -e args]: foreground from the loop's view, in the background of this shell
# so a signal reaches the trap at once instead of after the child exits.
run_controller() {
  local rc
  if [ "$BACKEND" = plain ]; then
    local net_args=()
    if [ "$NET" = host ]; then
      net_args=(--network host -e CTL_PG_HOST=127.0.0.1)
    else
      net_args=(--add-host host.docker.internal:host-gateway -e CTL_PG_HOST=host.docker.internal)
    fi
    "${DOCKER[@]}" run --rm --name "$CTL_NAME" --label "formi.project=$PROJECT" --memory 2g \
      "${net_args[@]}" -e "CTL_PG_PORT=$CTL_PG_PORT" -e "CTL_PG_PASSWORD=$CTL_PG_PASSWORD" \
      -e "CTL_SIM_URL=$CTL_SIM_URL" "${ENV_ARGS[@]}" "$@" "$IMAGE" &
  else
    compose run --rm -T --no-deps --name "$CTL_NAME" "${ENV_ARGS[@]}" "$@" controller &
  fi
  child=$!
  set +e
  wait "$child"
  rc=$?
  set -e
  child=""
  return "$rc"
}

on_signal() {
  trap - TERM INT
  log "signal $1: stopping the controller"
  "${DOCKER[@]}" stop -t 10 "$CTL_NAME" >/dev/null 2>&1 || true
  if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; fi
  exit $((128 + $1))
}

# What the store says the run has reached; empty when it cannot be read (counts as no progress).
progress_mark() {
  "${DOCKER[@]}" exec "$DB_CID" psql -U postgres -tAc \
    "SELECT schema_name, status, last_sim_minute FROM ctl.run_registry ORDER BY created_wall DESC LIMIT 1" \
    2>/dev/null || true
}

limp_in_container() {
  [ "${CTL_LIMP:-1}" = "1" ] || return 1
  log "last resort: advancing the scenario to its end with no controller logic (limp mode)"
  run_controller -e CTL_LIMP=1 -e "CTL_SIM_WAIT=${CTL_LIMP_SIM_WAIT:-30}"
}

degraded_or_exit() {
  if limp_in_container; then
    log "RESULT scenario finished DEGRADED: limp mode ($1)"
    exit 0
  fi
  exit "$2"
}

controller_loop() {
  local restarts=0 code=0 mark last_mark
  last_mark="$(progress_mark)"
  while true; do
    code=0
    run_controller || code=$?
    if [ "$code" -eq 0 ]; then
      log "RESULT scenario finished"
      return 0
    fi
    if [ "$code" -eq 3 ]; then
      log "fatal configuration error, not restarting"
      degraded_or_exit "fatal configuration error" 3
    fi
    mark="$(progress_mark)"
    if [ -n "$mark" ] && [ "$mark" != "$last_mark" ]; then
      restarts=0                                     # the run moved on: this is not a crash loop
      last_mark="$mark"
    fi
    restarts=$((restarts + 1))
    if [ "$restarts" -ge "${CTL_MAX_RESTARTS:-30}" ]; then
      log "controller failed $restarts times with no progress (last exit $code)"
      degraded_or_exit "crash loop, last exit $code" "$code"
    fi
    log "controller exited $code; restart $restarts"
    sleep "${CTL_RESTART_SLEEP_S:-2}"
  done
}

# ---------------------------------------------------------------- leave the read-only views up
keep_views_up() {
  "${DOCKER[@]}" exec "$DB_CID" psql -q -U postgres \
    -c "ALTER ROLE ctl_api LOGIN PASSWORD '$CTL_API_PG_PASSWORD'" >/dev/null 2>&1 \
    || log "could not enable role ctl_api"
  if [ "$BACKEND" = plain ]; then
    local net_args=(--add-host host.docker.internal:host-gateway -e CTL_PG_HOST=host.docker.internal
                    -p "127.0.0.1:$CTL_API_PORT:8780")
    [ "$NET" = host ] && net_args=(--network host -e CTL_PG_HOST=127.0.0.1)
    "${DOCKER[@]}" rm -f "$PLAIN_API" >/dev/null 2>&1 || true
    "${DOCKER[@]}" run -d --name "$PLAIN_API" --label "formi.project=$PROJECT" --memory 256m \
      "${net_args[@]}" -e "CTL_PG_PORT=$CTL_PG_PORT" -e "CTL_API_PG_PASSWORD=$CTL_API_PG_PASSWORD" \
      --entrypoint python "$IMAGE" -m uvicorn --factory controller.api.app:create_app \
      --host 127.0.0.1 --port "$CTL_API_PORT" >/dev/null 2>&1 \
      || log "read-only API did not start (not fatal)"
  else
    compose --profile api up -d --no-deps api >/dev/null 2>&1 \
      || log "read-only API did not start (not fatal)"
  fi
  log "left up: Postgres on 127.0.0.1:$CTL_PG_PORT, read-only API on 127.0.0.1:$CTL_API_PORT"
}

# ---------------------------------------------------------------- main
trap cleanup EXIT
trap 'on_signal 15' TERM
trap 'on_signal 2' INT

find_docker
pick_net_mode
pick_backend
build_image
stop_on_exit=1
start_db
remove_orphan
collect_env
controller_loop
if [ "${CTL_KEEP_UP:-0}" = 1 ]; then keep_views_up; fi
exit 0
