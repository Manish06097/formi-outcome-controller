# 0019. run.sh and networking

Status: accepted (2026-11-06), milestone M1; amended by track R (2026-11-07), see Amendment

## Context
- `./run.sh <SIM_URL>` must start everything and run to scenario end with no manual steps on a
  bare 4 vCPU / 8 GB Linux grading box; Docker is allowed. The simulator binds 127.0.0.1.
- A bridge-networked container on Linux cannot reach the host's loopback
  (`host.docker.internal` resolves to the docker0 gateway, not 127.0.0.1, F10). Docker Desktop
  (this Mac) forwards `host.docker.internal` to the host's loopback but has no usable host
  networking.
- The grading box may lack PyPI access (assumption A-12), and port 55432 may be taken (it is on
  this Mac, by an unrelated container).
- The controller must restart after a crash; state lives only in Postgres.

## Decision
1. **Compose with two overlays.** `compose.yml` defines Postgres 16 (settings of DESIGN.md 3.3,
   named volume `pgdata`, healthcheck, 3 GB limit), the controller (1 GB) and an optional
   read-only API (profile `api`, 256 MB). On Linux `compose.linux.yml` puts the controller on the
   host network and SIM_URL is used unchanged; Postgres is reached on its published loopback
   port. Elsewhere `compose.desktop.yml` keeps the bridge, and run.sh rewrites a loopback
   SIM_URL to `host.docker.internal`. A remote SIM_URL works in both.
2. **Port.** `CTL_PG_PORT` if set; else the port of this compose project's running db (so a
   re-run reuses its own database instead of colliding with it); else the first free port in
   55432..55451 (a bash `/dev/tcp` probe); none free: exit 3.
3. **Restart loop.** `docker compose run --rm -T controller` in a loop: exit 0 ends the run;
   exit 3 (fatal configuration: simulator never healthy, bad settings, a changed migration, a
   missing dependency) stops without restarting; anything else restarts after 2 s, at most
   `CTL_MAX_RESTARTS` (20) times. The controller waits up to `CTL_SIM_WAIT` (900 s) for
   `/health`, because the simulator generates its history before it listens (35 s for public_03
   on this Mac).
4. **The image contains only `controller/`** (`.dockerignore` whitelists it): the simulator package
   is absent, so the image itself shows the controller cannot import `sim`.
5. **Vendored wheels are a build script, not committed files.** `scripts/vendor_wheels.sh`
   downloads the manylinux wheels for the Docker server's architecture into `vendor/wheels/`; the
   Dockerfile installs with `pip install --no-index` when wheels are present and from PyPI
   otherwise. Wheels are binary, architecture-specific (this Mac builds arm64 images, the grading
   box is likely x86_64) and about 60 MB; committing them would bloat history and still be wrong
   for one of the two architectures. The two base images still need a registry or a local cache;
   run.sh checks them first (`docker image inspect`, then `docker pull`) and says which is missing.
6. **No-Docker fallback**: `CTL_NO_DOCKER=1 PG_DSN=... ./run.sh <SIM_URL>` runs
   `scripts/run_local.sh`: the same restart loop and exit codes with a local Python 3.11.
7. **Test isolation**: every knob is an environment variable (`COMPOSE_PROJECT_NAME`,
   `CTL_PG_PORT`, `CTL_FAULT` and the other `CTL_*` settings, passed through only when set).

## Alternatives
- **Host networking everywhere.** Not available on Docker Desktop for the bridge-to-host case we
  need; the overlay keeps one compose definition.
- **Run the controller on the host, only Postgres in Docker.** Needs a matching Python and
  dependencies on the grading box; kept only as the explicit no-Docker fallback.
- **Commit `vendor/wheels/`.** Rejected above (size, architecture).
- **compose `restart: on-failure`.** Cannot distinguish a fatal configuration error from a crash
  and hides the restart count from run.sh.

## Consequences
- Verified on this Mac (Docker Desktop 28, Compose 2.34): `./run.sh http://127.0.0.1:8711` against
  public_01 exits 0 with zero config changes; the load test and kill test run through it. The
  Linux host-network path is written but not run here (no Linux box in this environment).
- The controller is started with `docker compose run`, so its container name is generated; tools
  find it by the compose labels (`com.docker.compose.service=controller`).
- `TMPDIR` must stay short when the docker CLI runs (its plugin socket lives there); the scripts
  keep their work directories elsewhere.

## Amendment (track R, grader-box hardening, AUDIT-1 findings 5, 6, 7, 10, 11, 12, 15)

This replaces points 3 and 5 above and adds the rest. The original text is kept for the reasoning.

- **Reproducible build (finding 5).** `requirements-runtime.txt` lists only what the controller
  imports (no pytest); `requirements-runtime.lock` pins every package by exact version and hash,
  and the Dockerfile installs it with `--require-hashes --no-deps --only-binary=:all:`. numpy is
  pinned exactly, so `Generator` streams cannot change between builds
  (`tests/unit/test_numpy_stream.py` pins a known stream). Both base images are pinned by digest
  (`Dockerfile` ARG, `compose.yml`); if a registry no longer serves the digest, `run.sh` warns and
  uses the tag. `requirements.txt` is the simulator's and is untouched. `run.sh` states in its header
  what the box needs: the registry, and PyPI (or a mirror, or `vendor/wheels/`) on the first run.
- **Backends (finding 6).** `docker compose` v2, `docker-compose` v1, then plain `docker run`
  (own network-free layout: Postgres container with a published loopback port, polled with
  `pg_isready` through `docker exec`; the controller on `--network host` or `host.docker.internal`).
  Nothing uses `--wait`. The image is built with `docker build`, not `compose build`, so a missing
  buildx does not matter. A compose backend that fails to start Postgres falls back to plain.
- **Failure ladder (finding 10).** (1) A crash restarts the controller; the budget counts restarts
  with no progress in `ctl.run_registry` and resets on progress. (2) A hook that raises
  `safe_mode_crashes` times at one step is guarded: skipped with an audit row and a SAFE_MODE page
  while the advance and the governance lifecycle continue (`controller/loop/safemode.py`, table
  `hook_guard`, migration 0030). A process death or hang in a hook disables it. (3) If the budget runs
  out anyway, the configuration is fatal, or Docker cannot run the controller, limp mode
  (`controller/loop/limp.py`, standard library only) advances the simulator to its end. The last line
  says DEGRADED. An unfinished scenario is the worst outcome, so the order is: finish with
  decisions, finish without some hooks, finish with none.
- **Exit and signals (finding 11).** A trap stops the controller container on SIGTERM/SIGINT.
  `resolve_run` returns an already-done schema at once when the simulator is finished and the
  store is consistent with it, so a second `run.sh` is a fast no-op. One controller container name
  per simulator URL; an orphan is replaced.
- **Fingerprint (finding 12).** The governance block is out of the run fingerprint (a policy change
  between restarts resumes the same schema).
- **Bounded waits and cleanup (finding 15).** GET retries stop after 30 minutes (exit 1; the next
  start waits `CTL_SIM_WAIT` for `/health` and exits 3 if the simulator is gone). Postgres and the
  API are stopped on exit unless `CTL_KEEP_UP=1`; the data stays in the volume. Holds written through
  `POST /supervisor/holds` write an audit row (migration 0031).
- **Directives (finding 7).** A directive that matches no workflow raises a DIRECTIVE_UNMATCHED alert
  and an audit row at start. The memos name the Northwind and Kestrel tenants, so a renamed tenant
  would otherwise silently lose the memo promise.

### What was verified, and where
See `docs/design/build-reports/R.md` for the commands. The Linux host-network path was exercised
only through a Linux container with the docker CLI and the Docker Desktop socket; that is not a
bare Linux box and the report says what it does and does not show.
