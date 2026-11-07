#!/usr/bin/env bash
# Closest honest reproduction of the grader box that a Mac with Docker Desktop allows.
#
# A fresh `git clone` of this repo (no .venv, no vendored wheels, nothing cached in the tree) is
# mounted into a Linux container that has the docker CLI and NO compose plugin. Inside it a
# simulator runs and `./run.sh` runs against it with the host network (the VM's loopback), the
# plain-docker backend, and sibling containers built and started through the mounted socket.
#
#   scripts/linux_box/run.sh [scenario yaml, default scenarios/public_01_dev.yaml]
#
# What this is NOT: a bare Linux machine. Docker Desktop's VM provides the kernel and daemon, the
# architecture is the Mac's (arm64 on Apple silicon, not x86_64), and the daemon is Docker Desktop's
# (so run.sh is told CTL_NET_MODE=host instead of detecting it). The Compose v2 plugin path on Linux
# and docker-compose v1 are not exercised here.
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
scenario="${1:-scenarios/public_01_dev.yaml}"
work="$(mktemp -d /tmp/formi_box.XXXXXX)"
git clone -q --branch "$(git -C "$here" rev-parse --abbrev-ref HEAD)" "$here" "$work/clone"
docker build -q -t formi_box "$here/scripts/linux_box" >/dev/null
cat > "$work/inner.sh" <<INNER
set -euo pipefail
git config --global --add safe.directory '*'
python -m venv /venv && /venv/bin/pip install -q -r requirements.txt
/venv/bin/python -m sim serve --scenario $scenario --run-dir /work/clone/runs/box --port 8741 --force > /work/sim.log 2>&1 &
export CTL_NET_MODE=host COMPOSE_PROJECT_NAME=formi_box
./run.sh http://127.0.0.1:8741 2>&1 | tee /work/run_sh.log
INNER
docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$work:/work" -w /work/clone formi_box bash /work/inner.sh
echo "run directory for grading: $work/clone/runs/box"
