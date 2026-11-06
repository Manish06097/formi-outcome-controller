"""Process entry: `python -m controller`.

Owner: track T1 (milestone M2). Implements DESIGN.md 11.1 (entry), 11.3 (exit codes for run.sh).

Reads settings from the environment (`CTL_SIM_URL` or `SIM_URL`, `CTL_PG_*` or `PG_DSN`, see
`controller/settings.py`), then runs the Orchestrator until the scenario finishes. Exit codes:
0 finished, 1 transient failure (run.sh restarts the process; the restart sequence makes that
safe), 3 fatal configuration error (run.sh stops).
"""
from __future__ import annotations

import logging
import sys

EXIT_FATAL = 3


def main(argv: list[str] | None = None) -> int:
    try:  # a missing dependency is a deployment error: restarting cannot fix it
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
    return Orchestrator(settings).run()


if __name__ == "__main__":
    sys.exit(main())
