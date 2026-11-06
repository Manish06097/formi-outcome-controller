#!/usr/bin/env bash
# Run the simulator baseline and the grader for a list of private scenario variants
# (DESIGN.md 17 override 3, 10.1). Test tooling: it reads scenarios/private/*.yaml and runs/;
# the controller never does.
#
# Usage:
#   scripts/variants/run_matrix.sh [options] [variant ...]
#
#   variant           a name from scenarios/private/ (with or without .yaml); default: every
#                     variant except the 20-seed p4_like family (pass --all to include it)
#   --controller CMD  run CMD <SIM_URL> against a served simulator, then grade that run
#                     against the baseline (for example: --controller ./run.sh)
#                     Without it the matrix only runs the baseline and grades the baseline
#                     against itself, which proves the variant loads, runs and grades.
#   --port N          simulator port for --controller runs (default 8715, track T5)
#   --out DIR         where run dirs and reports go (default runs/private)
#   --all             include the p4_like_supervisor_p01_s* seed family
#   --python PATH     interpreter (default .venv/bin/python, else python3)
#
# Output: DIR/<variant>/baseline, DIR/<variant>/run (with --controller), DIR/<variant>/grade.md
# and grade.json, and DIR/summary.tsv with one row per variant:
#   variant  baseline_s  controller_exit  controller_s  grade
# Exit status: 0 when every baseline, controller run and grade succeeded, 1 otherwise.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

CONTROLLER=""
PORT=8715
OUT="runs/private"
ALL=0
PY=""
VARIANTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --controller) CONTROLLER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --all) ALL=1; shift ;;
    --python) PY="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) VARIANTS+=("${1%.yaml}"); shift ;;
  esac
done
if [[ -z "$PY" ]]; then
  if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; else PY=python3; fi
fi
if [[ ${#VARIANTS[@]} -eq 0 ]]; then
  for f in scenarios/private/*.yaml; do
    n="$(basename "$f" .yaml)"
    if [[ $ALL -eq 0 && "$n" == p4_like_supervisor_p01_s* ]]; then continue; fi
    VARIANTS+=("$n")
  done
fi

mkdir -p "$OUT"
SUMMARY="$OUT/summary.tsv"
printf 'variant\tbaseline_s\tcontroller_exit\tcontroller_s\tgrade\n' > "$SUMMARY"
FAILED=0
SIM_PID=""

stop_sim() {
  if [[ -n "$SIM_PID" ]] && kill -0 "$SIM_PID" 2>/dev/null; then
    kill "$SIM_PID" 2>/dev/null
    wait "$SIM_PID" 2>/dev/null
  fi
  SIM_PID=""
}
trap stop_sim EXIT INT TERM

wait_healthy() {  # poll GET /health until it answers (history generation takes a few seconds)
  local url="$1" i
  for i in $(seq 1 600); do
    if "$PY" -c "import httpx,sys; sys.exit(0 if httpx.get('$url/health', timeout=2).status_code == 200 else 1)" 2>/dev/null; then
      return 0
    fi
    if [[ -n "$SIM_PID" ]] && ! kill -0 "$SIM_PID" 2>/dev/null; then return 1; fi
    sleep 0.5
  done
  return 1
}

now() { "$PY" -c 'import time; print(f"{time.monotonic():.3f}")'; }
elapsed() { "$PY" -c "print(f'{$2 - $1:.1f}')"; }

for v in "${VARIANTS[@]}"; do
  scen="scenarios/private/$v.yaml"
  dir="$OUT/$v"
  if [[ ! -f "$scen" ]]; then
    echo "no such variant: $v" >&2
    printf '%s\t-\t-\t-\tmissing\n' "$v" >> "$SUMMARY"
    FAILED=1
    continue
  fi
  mkdir -p "$dir"
  echo "== $v"
  t0="$(now)"
  if ! "$PY" -m sim baseline --scenario "$scen" --run-dir "$dir/baseline" > "$dir/baseline.log" 2>&1; then
    echo "   baseline FAILED (see $dir/baseline.log)"
    printf '%s\tFAIL\t-\t-\t-\n' "$v" >> "$SUMMARY"
    FAILED=1
    continue
  fi
  base_s="$(elapsed "$t0" "$(now)")"
  echo "   baseline ${base_s}s: $(tail -1 "$dir/baseline.log")"

  run_dir="$dir/baseline"
  ctl_exit="-"
  ctl_s="-"
  if [[ -n "$CONTROLLER" ]]; then
    url="http://127.0.0.1:$PORT"
    "$PY" -m sim serve --scenario "$scen" --run-dir "$dir/run" --port "$PORT" --force \
      > "$dir/sim.log" 2>&1 &
    SIM_PID=$!
    if ! wait_healthy "$url"; then
      echo "   simulator did not become healthy (see $dir/sim.log)"
      stop_sim
      printf '%s\t%s\tsim-down\t-\t-\n' "$v" "$base_s" >> "$SUMMARY"
      FAILED=1
      continue
    fi
    t1="$(now)"
    $CONTROLLER "$url" > "$dir/controller.log" 2>&1
    ctl_exit=$?
    ctl_s="$(elapsed "$t1" "$(now)")"
    stop_sim
    echo "   controller exit $ctl_exit after ${ctl_s}s (log $dir/controller.log)"
    [[ $ctl_exit -eq 0 ]] || FAILED=1
    run_dir="$dir/run"
  fi

  if "$PY" -m sim grade --run "$run_dir" --baseline "$dir/baseline" --out "$dir/grade.md" \
       > /dev/null 2> "$dir/grade.err" \
     && "$PY" -m sim grade --run "$run_dir" --baseline "$dir/baseline" --json \
       > "$dir/grade.json" 2>> "$dir/grade.err"; then
    grade="$dir/grade.md"
    echo "   graded: $grade"
  else
    grade="FAIL"
    FAILED=1
    echo "   grade FAILED (see $dir/grade.err)"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$v" "$base_s" "$ctl_exit" "$ctl_s" "$grade" >> "$SUMMARY"
done

echo
column -t -s $'\t' "$SUMMARY" 2>/dev/null || cat "$SUMMARY"
exit $FAILED
