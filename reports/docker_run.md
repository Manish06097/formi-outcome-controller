# Docker run of public_01 on the final code (2026-11-08)

Command: simulator started with `python -m sim serve --scenario scenarios/public_01_dev.yaml --port 8700 --force`,
then `./run.sh http://127.0.0.1:8700` with no CTL_NO_DOCKER, on Docker Desktop (arm64, Mac) after a forced
restart of the stuck daemon. Postgres 16 and the controller ran in containers.

- Result: `RESULT scenario finished`, exit 0, wall 2 min 43 s, no DEGRADED line.
- Grade (`sim grade` against `runs/base_public_01_dev`): conclusion +8.5%, pickup +5.5%, engagement +2.0%,
  gap captured 7%; 28 changes applied (21 neutral, 7 beneficial), 0 apply conflicts, 0 lapsed, 0 expired.
  This is the same as the no-Docker run of the same code.
- Not measured: Postgres container memory against the 3 GB limit (the containers were stopped when the run
  ended before a sample was taken), x86 Linux, a real Linux daemon, `compose.linux.yml`, public_02 and public_03.
