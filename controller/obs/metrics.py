"""Loop health: phase timings, the `loop_step` row, the heartbeat thread and the 9.1 metrics.

Owner: track T1 (milestone M2). Implements DESIGN.md 9.1 and the 10 s heartbeat of section 2.

Wall-clock values live only in ops columns (`loop_step.phase_ms`, `rss_mb`, `db_bytes`, `wall`,
`heartbeat`); decisions never read them (DESIGN.md 8.6).
"""
from __future__ import annotations

import logging
import os
import resource
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from controller.store import repos
from controller.store.db import AdvisoryLock, Database
from controller.types import Tx

log = logging.getLogger(__name__)


class PhaseTimer:
    """Wall milliseconds per named phase of one step (`loop_step.phase_ms`)."""

    def __init__(self) -> None:
        self.ms: dict[str, int] = {}
        self._t0 = time.monotonic()

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        t = time.monotonic()
        try:
            yield
        finally:
            self.ms[name] = self.ms.get(name, 0) + int((time.monotonic() - t) * 1000)

    def total_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


def rss_mb() -> int:
    """Current resident set size of this process in MB (Linux /proc; elsewhere the peak)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // (1 << 20)
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024)


def peak_rss_mb() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024)


class Heartbeat:
    """Writes the heartbeat row every `interval_s` and checks the advisory lock is still ours.

    Losing the lock (its connection died) means another controller may now own this world, so
    the process stops acting at once: `on_lost_lock` (default: exit 1, run.sh restarts and the
    new process waits for the lock)."""

    def __init__(self, db: Database, lock: AdvisoryLock | None, interval_s: float,
                 step_no: Callable[[], int | None],
                 on_lost_lock: Callable[[], None] | None = None):
        self.db = db
        self.lock = lock
        self.interval_s = interval_s
        self.step_no = step_no
        self.on_lost_lock = on_lost_lock or (lambda: os._exit(1))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)

    def start(self) -> "Heartbeat":
        self.beat()
        self._thread.start()
        return self

    def beat(self) -> None:
        with self.db.tx() as tx:
            repos.beat(tx, os.getpid(), self.step_no())

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                if self.lock is not None and not self.lock.held():
                    log.critical("advisory lock lost: stopping")
                    self.on_lost_lock()
                    return
                self.beat()
            except Exception:
                log.exception("heartbeat failed")

    def stop(self) -> None:
        self._stop.set()


def step_summary(conn: Tx) -> dict[str, Any]:
    """The 9.1 throughput metrics over the whole run, from `loop_step` and `ingested_chunk`."""
    ing = conn.execute(
        """SELECT kind, count(*), coalesce(sum(events), 0), coalesce(sum(txn_ms), 0),
                  coalesce(sum(bytes), 0)
           FROM ingested_chunk GROUP BY kind ORDER BY kind""").fetchall()
    steps = conn.execute(
        """SELECT (to_minute - 1) / 1440 AS day, count(*),
                  sum(coalesce((SELECT sum(value::int) FROM jsonb_each_text(phase_ms)), 0)),
                  sum(events), max(rss_mb), max(db_bytes)
           FROM loop_step GROUP BY 1 ORDER BY 1""").fetchall()
    return {
        "ingest": [{"kind": k, "chunks": n, "events": e, "txn_ms": ms, "bytes": b,
                    "events_per_s": round(e / ms * 1000) if ms else None}
                   for k, n, e, ms, b in ing],
        "days": [{"day": d, "steps": n, "wall_s": round((ms or 0) / 1000, 1), "events": ev,
                  "rss_mb": rss, "db_bytes": dbb} for d, n, ms, ev, rss, dbb in steps],
    }
