# 0019. run.sh and networking

Status: accepted (2026-11-06), milestone M1

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
