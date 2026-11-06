#!/usr/bin/env bash
# Make the no-op baselines for a list of scenarios in parallel (test tooling, step I4).
#   scripts/variants/baselines.sh [-j N] name=path.yaml ...
# Writes runs/base_<name> (skipped when it already exists) and runs/base_<name>.log.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
J=6
if [[ "${1:-}" == "-j" ]]; then J="$2"; shift 2; fi
PY="${PY:-.venv/bin/python}"
one() {
  local n="${1%%=*}" f="${1#*=}"
  if [[ -f "runs/base_$n/.done" ]]; then echo "$n cached"; return 0; fi
  rm -rf "runs/base_$n"
  if "$PY" -m sim baseline --scenario "$f" --run-dir "runs/base_$n" > "runs/base_$n.log" 2>&1; then
    touch "runs/base_$n/.done"; echo "$n ok"
  else
    echo "$n FAILED"
  fi
}
export -f one
export PY
mkdir -p runs
printf '%s\n' "$@" | xargs -P "$J" -I{} bash -c 'one "$@"' _ {}
