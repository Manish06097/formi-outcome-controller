"""The image's inputs stay pinned and the two ways of starting Postgres stay equal (AUDIT-1 finding 5,
ADR 0019). No Docker needed: these read the files a grader's build reads.
"""
from __future__ import annotations

import re

import yaml

from tests.e2e.platform.helpers import REPO

DIGEST = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")


def _lock() -> dict[str, list[str]]:
    """{requirement line: its hashes} of the lock file."""
    out: dict[str, list[str]] = {}
    current = ""
    for raw in (REPO / "requirements-runtime.lock").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            out[current].append(line.split("=", 1)[1].rstrip(" \\"))
        else:
            current = line.rstrip(" \\")
            out[current] = []
    return out


def _direct_requirements() -> list[str]:
    names = []
    for raw in (REPO / "requirements-runtime.txt").read_text().splitlines():
        line = raw.split("#")[0].strip()
        if line:
            names.append(re.split(r"[\[<>=!~ ]", line, maxsplit=1)[0].lower())
    return names


def test_every_locked_package_is_exact_and_hashed():
    lock = _lock()
    assert len(lock) > 10
    for req, hashes in lock.items():
        assert re.fullmatch(r"[A-Za-z0-9._-]+(\[[a-z,]+\])?==[\w.]+", req), req
        assert hashes and all(h.startswith("sha256:") for h in hashes), req


def test_the_lock_covers_the_runtime_list_and_has_no_test_tools():
    names = {re.split(r"[\[=]", r, maxsplit=1)[0].lower() for r in _lock()}
    assert set(_direct_requirements()) <= names
    assert not names & {"pytest", "pluggy", "iniconfig", "pygments"}


def test_numpy_is_pinned_exactly_in_the_source_list_too():
    source = (REPO / "requirements-runtime.txt").read_text()
    pinned = re.search(r"^numpy==([\w.]+)$", source, re.M)
    assert pinned, "a numpy bump must be deliberate (controller/stats/draws.py)"
    assert f"numpy=={pinned.group(1)}" in _lock()


def test_the_lock_was_compiled_from_the_source_list():
    header = (REPO / "requirements-runtime.lock").read_text()
    assert "requirements-runtime.txt" in header.splitlines()[1]


def test_the_image_installs_only_the_lock_and_never_the_simulator():
    docker = (REPO / "Dockerfile").read_text()
    assert "--require-hashes" in docker and "--no-deps" in docker and "--only-binary=:all:" in docker
    assert "requirements-runtime.lock" in docker
    assert "requirements.txt" not in docker and "requirements-controller.txt" not in docker
    ignore = [ln.strip() for ln in (REPO / ".dockerignore").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
    assert ignore[0] == "*" and "!requirements-runtime.lock" in ignore
    assert not any(line.startswith("!sim") or line == "!requirements.txt" for line in ignore)


def test_base_images_are_pinned_by_digest_in_one_place_each():
    python_base = re.search(r"^ARG PYTHON_BASE=(\S+)$", (REPO / "Dockerfile").read_text(), re.M)
    assert python_base and DIGEST.match(python_base.group(1))
    pg = re.search(r"CTL_PG_IMAGE:-([^}]+)\}", (REPO / "compose.yml").read_text())
    assert pg and DIGEST.match(pg.group(1))
    assert pg.group(1).startswith("postgres:16@") and python_base.group(1).startswith("python:3.11-")
    assert "sha256:" not in (REPO / "run.sh").read_text(), "run.sh reads the pins, never repeats them"


def _run_sh_pg_settings() -> list[str]:
    m = re.search(r"PG_SETTINGS=\((.*?)\)", (REPO / "run.sh").read_text(), re.S)
    assert m
    return m.group(1).split()


def test_plain_docker_postgres_has_the_compose_settings():
    db = yaml.safe_load((REPO / "compose.yml").read_text())["services"]["db"]
    assert db["command"][0] == "postgres"
    assert _run_sh_pg_settings() == db["command"][1:]
    run_sh = (REPO / "run.sh").read_text()
    assert f"--shm-size {db['shm_size']}" in run_sh
    assert f"--memory {db['mem_limit']}" in run_sh
    ctl = yaml.safe_load((REPO / "compose.yml").read_text())["services"]["controller"]
    assert f"--memory {ctl['mem_limit']}" in run_sh


def test_the_compose_api_entrypoint_is_the_real_factory():
    api = yaml.safe_load((REPO / "compose.yml").read_text())["services"]["api"]
    assert "--factory" in api["entrypoint"] and "controller.api.app:create_app" in api["entrypoint"]
    assert "def create_app(" in (REPO / "controller" / "api" / "app.py").read_text()
