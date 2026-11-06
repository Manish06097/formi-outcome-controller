"""The draws under the locked numpy, bit for bit (AUDIT-1 finding 5, ADR 0019). numpy's Generator
methods may change between versions (NEP 19), and every decision's replay depends on them. If this
fails after a numpy bump, the bump changed the streams: treat it as a change of behaviour, then
update `STREAM_DIGEST` in controller/stats/draws.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from controller.stats.draws import STREAM_DIGEST, stream_digest

REPO = Path(__file__).resolve().parents[2]


def test_this_environment_runs_the_locked_numpy():
    locked = re.search(r"^numpy==([\w.]+)", (REPO / "requirements-runtime.lock").read_text(), re.M)
    assert locked and np.__version__ == locked.group(1), (
        "install with: uv pip install -r requirements.txt -r requirements-controller.txt "
        "-c requirements-runtime.lock")


def test_the_stream_is_the_golden_one():
    assert stream_digest() == STREAM_DIGEST
