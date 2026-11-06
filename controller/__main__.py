"""Process entry: `python -m controller`.

Owner: track T1 (milestone M2). Implements DESIGN.md 11.1 (entry), 11.3 (exit codes for run.sh).

Reads settings from the environment (`CTL_SIM_URL` or `SIM_URL`, `CTL_PG_*` or `PG_DSN`, see
`controller/settings.py`), then runs the Orchestrator until the scenario finishes. Exit codes:
0 finished, 1 transient failure (run.sh restarts the process; the restart sequence makes that
safe), 3 fatal configuration error (run.sh stops), 143 asked to stop (SIGTERM).

`CTL_LIMP=1` runs limp mode instead: advance to the end with no decisions (`loop/limp.py`, ADR 0019).
It comes first, with nothing else imported, so it works when the rest of the package cannot load.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType

EXIT_FATAL = 3
EXIT_TERM = 143


def _on_term(signum: int, frame: FrameType | None) -> None:
    """As PID 1 of its container, Python ignores SIGTERM unless it has a handler, and `docker stop`
    would wait out its grace period. Stopping here is as safe as a crash (write-ahead intents)."""
    raise SystemExit(EXIT_TERM)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _on_term)
    if os.environ.get("CTL_LIMP") == "1":
        from controller.loop.limp import main as limp_main
        return limp_main([])
    try:  # a missing dependency is a deployment error: restarting cannot fix it
        from controller.loop.governance import GovernanceHooks
        from controller.loop.orchestrator import Orchestrator
        from controller.settings import load_settings
    except ImportError as e:
        logging.basicConfig(level=logging.INFO)
        logging.critical("cannot import the controller: %s", e)
        return EXIT_FATAL
    try:
        settings = load_settings()
    except ValueError as e:
        logging.basicConfig(level=logging.INFO)
        logging.critical("invalid settings: %s", e)
        return EXIT_FATAL
    logging.basicConfig(level=getattr(logging, settings.env.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return Orchestrator(settings, GovernanceHooks(settings.env.learner)).run()


if __name__ == "__main__":
    sys.exit(main())
