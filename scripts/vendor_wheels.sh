#!/usr/bin/env bash
# scripts/vendor_wheels.sh [arch]
#
# Downloads every wheel the controller image needs into vendor/wheels/, for the Docker server's
# architecture (or the one given: amd64 | arm64), so `docker compose build` installs with
# `pip install --no-index` and needs no PyPI access (DESIGN.md 11.3, assumption A-12, ADR 0019).
# Wheels are build inputs, not source: they are not committed (.gitignore). Run this once on a
# machine with network access before building on one without.
set -euo pipefail
cd "$(dirname "$0")/.."
arch="${1:-$(docker version -f '{{.Server.Arch}}' 2>/dev/null || uname -m)}"
case "$arch" in
  amd64|x86_64) m=x86_64 ;;
  arm64|aarch64) m=aarch64 ;;
  *) echo "unsupported architecture $arch" >&2; exit 2 ;;
esac
mkdir -p vendor/wheels
python3 -m pip download --only-binary=:all: --python-version 3.11 --implementation cp \
  --platform "manylinux2014_$m" --platform "manylinux_2_17_$m" --platform "manylinux_2_28_$m" \
  -r requirements.txt -r requirements-controller.txt -d vendor/wheels
echo "wheels in vendor/wheels: $(ls vendor/wheels | grep -c '\.whl$')"
