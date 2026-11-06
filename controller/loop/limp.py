"""Limp mode: advance the simulator to the end of the scenario with no decisions at all.

Owner: track R (grader-box hardening, ADR 0019). The last layer under the restart loop and safe
mode: run.sh uses it only when nothing else can finish the scenario (a crash loop in the core
loop, a fatal configuration, or Docker unable to run the controller), because an unfinished
scenario is the worst outcome. The config stays exactly as it is, nothing is proposed, approved
proposals are left to lapse. It is a clock, not a controller.

It imports only the standard library at module level, so it also runs from the box's own Python
with no dependencies: `python3 -I controller/loop/limp.py <SIM_URL>`. Inside the image
`CTL_LIMP=1 python -m controller` runs it too, and then it also tries to leave an audit row and a
LIMP_MODE page in the newest active run schema (best effort; the store may be why we are here).

Exit codes: 0 the scenario is finished; 3 the simulator does not answer; 1 it answers but will not
finish (an advance it refuses for a reason other than `finished`).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("controller.limp")

MAX_STEP_MIN = 1440
"""Advance at most one simulated day per call, so a slow day cannot hit the read timeout twice."""
ADVANCE_TIMEOUT_S = 1800.0
READ_TIMEOUT_S = 30.0
EXIT_DONE, EXIT_STUCK, EXIT_UNREACHABLE = 0, 1, 3


def _request(url: str, body: dict[str, Any] | None = None, timeout: float = READ_TIMEOUT_S) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (the given SIM_URL)
        return json.loads(r.read().decode())


def _state(base: str, wait_s: float) -> dict[str, Any] | None:
    """GET /sim/state, retrying for up to wait_s (the simulator builds history before it listens)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return _request(base + "/sim/state")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if time.monotonic() >= deadline:
                log.error("simulator at %s does not answer: %s", base, e)
                return None
            time.sleep(2.0)


def next_step(state: dict[str, Any]) -> int:
    """Whole ticks up to one day, never past the end. Reads tick and end from the API."""
    tick = int(state["tick_minutes"])
    left = int(state["end_minute"]) - int(state["sim_minute"])
    return max(tick, min(left, MAX_STEP_MIN) // tick * tick)


def advance_to_end(base: str, wait_s: float = 30.0) -> int:
    """Advance until the simulator says finished. A lost response is harmless here: limp mode never
    hands back to the normal loop, so the next read of the clock is all it needs."""
    base = base.rstrip("/")
    state = _state(base, wait_s)
    if state is None:
        return EXIT_UNREACHABLE
    log.warning("limp mode from minute %s to %s", state.get("sim_minute"), state.get("end_minute"))
    while not state.get("finished"):
        try:
            _request(base + "/sim/advance", {"minutes": next_step(state)}, ADVANCE_TIMEOUT_S)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                log.error("advance refused: %s %s", e.code, e.read()[:300])
                return EXIT_STUCK
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("advance failed (%s); reading the clock", e)
        next_state = _state(base, max(wait_s, 300.0))
        if next_state is None:
            return EXIT_UNREACHABLE
        state = next_state
    log.warning("limp mode: scenario finished at minute %s", state.get("sim_minute"))
    return EXIT_DONE


def _record_in_store(minute_note: str) -> None:
    """Best effort: an audit row and a LIMP_MODE page in the newest active run. Never raises."""
    try:
        import psycopg

        from controller.audit.writer import record
        from controller.obs.alerts import raise_alert
        from controller.settings import EnvSettings

        dsn = EnvSettings.from_env(os.environ).dsn
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            row = conn.execute("SELECT schema_name FROM ctl.run_registry WHERE status <> 'done' "
                               "ORDER BY created_wall DESC LIMIT 1").fetchone()
            if row is None:
                return
            conn.execute(f'SET search_path TO "{row[0]}", public')
            record(conn, wf=None, actor="controller", action="limp_mode", subject=("run", row[0]),
                   summary="limp mode: advancing to the end with no decisions",
                   details={"note": minute_note})
            raise_alert(conn, "LIMP_MODE", None, "run",
                        "the controller could not run; the scenario is being advanced to its "
                        "end with no decisions")
    except Exception as e:  # the store may be the reason we are here
        log.warning("could not record limp mode in the store: %s", e)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:] if argv is None else argv
    base = args[0] if args else (os.environ.get("CTL_SIM_URL") or os.environ.get("SIM_URL") or "")
    if not base:
        log.error("no simulator URL (argument, CTL_SIM_URL or SIM_URL)")
        return EXIT_UNREACHABLE
    _record_in_store("entered from run.sh's restart loop")
    return advance_to_end(base, float(os.environ.get("CTL_SIM_WAIT", "900")))


if __name__ == "__main__":
    sys.exit(main())
