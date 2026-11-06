"""Run the real simulator as a subprocess for end-to-end and contract tests.

    with SimProcess(scenario_yaml, tmp_path, port=8700) as sim:
        client = SimClient(sim.url)

The scenario is copied into the run's temp directory first, optionally with overrides (for
example fewer history days for speed), so the test never edits files under scenarios/.
"""
from __future__ import annotations

import copy
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO = Path(__file__).resolve().parents[2]


def write_scenario(src: str | Path, dest_dir: Path, overrides: dict[str, Any] | None = None) -> Path:
    """Copy a scenario YAML into dest_dir, applying top-level key overrides."""
    data = yaml.safe_load(Path(REPO / src if not Path(src).is_absolute() else src).read_text())
    data = copy.deepcopy(data)
    for k, v in (overrides or {}).items():
        data[k] = v
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / Path(src).name
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out


class SimProcess:
    def __init__(self, scenario: str | Path, workdir: Path, port: int = 8700,
                 overrides: dict[str, Any] | None = None, ready_timeout_s: float = 120.0):
        self.scenario = write_scenario(scenario, workdir / "scenario", overrides)
        self.run_dir = workdir / "run"
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.ready_timeout_s = ready_timeout_s
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_path = workdir / "sim.log"

    def start(self) -> "SimProcess":
        self._ensure_port_free()
        log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sim", "serve", "--scenario", str(self.scenario),
             "--run-dir", str(self.run_dir), "--port", str(self.port), "--force"],
            cwd=REPO, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        deadline = time.monotonic() + self.ready_timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"simulator exited early:\n{self.log_path.read_text()}")
            try:
                if httpx.get(f"{self.url}/health", timeout=1.0).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"simulator not healthy after {self.ready_timeout_s}s")

    def _ensure_port_free(self) -> None:
        try:
            httpx.get(f"{self.url}/health", timeout=0.5)
        except httpx.HTTPError:
            return
        raise RuntimeError(f"port {self.port} already serves something; stop it first")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        self.proc = None

    def __enter__(self) -> "SimProcess":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
