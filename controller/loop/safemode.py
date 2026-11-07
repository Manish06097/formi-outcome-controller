"""Safe mode: a hook that keeps crashing must not end the run unfinished.

Owner: track R (grader-box hardening, ADR 0019, AUDIT-1 finding 10). The orchestrator calls every
`LoopHooks` method through `HookGuard.run`. The simulator only moves on POST /sim/advance, so a
deterministic exception in one hook would otherwise crash every restart at the same step and leave
the scenario part-finished, which scores worse than any bad decision.

A hook point moves through three states, kept in the run schema so restarts remember them:

  normal    an exception crashes the process (exit 1): a restart rebuilds everything from
            Postgres and cures anything transient. `hook_attempt` counts the attempts at this
            (point, step).
  guarded   entered after `safe_mode_crashes` exceptions at one step. The hook still runs at each
            step, but an exception no longer ends the process: it is recorded (`hook_skipped`
            audit row), the hook's in-memory state is rebuilt the way a restart would (the
            orchestrator's `recover`), and the loop moves on without it.
  disabled  entered when the process itself dies in the hook: `safe_mode_hard_crashes` deaths at
            one step with no exception (out of memory, a native crash), or two watchdog firings
            (a hang). Nothing can catch those, so the hook is not called again in this run.

Every entry writes a decision row, an audit row and a SAFE_MODE page. Simulator and database
outages are not the hook's fault: they end the process as usual and do not count.

What this costs: a `normal` hook adds two small autocommit statements per call on a dedicated
connection, so the counter survives the rollback of whatever the hook was doing.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Protocol, TypeVar

import psycopg

from controller.audit.writer import record, write_decision
from controller.obs.alerts import raise_alert
from controller.reliability.faults import parse_hook_faults
from controller.settings import EnvSettings
from controller.simclient.errors import ServerError, SimTransportError
from controller.store.db import Database
from controller.types import Decision

log = logging.getLogger(__name__)

T = TypeVar("T")
TRANSIENT = (SimTransportError, ServerError, psycopg.OperationalError, psycopg.InterfaceError)
"""Outages, not bugs: these end the process and never count against a hook."""
WATCHDOG = "watchdog"
WATCHDOG_DISABLE_AFTER = 2
"""A hang costs a full watchdog period each time, so two at one step are enough."""
EXIT_RESTART = 1


class Decisions(Protocol):
    def __call__(self, *, wf: str | None, module: str, kind: str, rule_id: str,
                 inputs: dict[str, Any], outcome: dict[str, Any] | None = None,
                 sim_minute: int | None = None) -> Decision: ...


class HookGuard:
    def __init__(self, db: Database, schema: str, env: EnvSettings, decision: Decisions):
        self.db = db
        self.schema = schema
        self.env = env
        self.decision = decision
        self.faults = parse_hook_faults(env.hook_fault)
        self.states: dict[str, str] = {}
        self.needs_recovery = False
        self._conn: psycopg.Connection[Any] | None = None
        self._current: tuple[str, int] | None = None
        self._started = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def open(self, minute: int) -> None:
        """Load the states and judge the attempts a dead process left behind."""
        self._connect()
        conn = self._c()
        self.states = dict(conn.execute("SELECT point, state FROM hook_guard").fetchall())
        rows = conn.execute("SELECT point, step_no, started, raised, last_error FROM hook_attempt"
                            ).fetchall()
        for point, step_no, started, raised, last_error in rows:
            hard = started - raised
            if last_error == WATCHDOG and raised >= WATCHDOG_DISABLE_AFTER:
                self._enter(point, "disabled", step_no, minute,
                            f"hung {raised} times at step {step_no}")
            elif hard >= self.env.safe_mode_hard_crashes:
                self._enter(point, "disabled", step_no, minute,
                            f"the process died {hard} times inside it at step {step_no}")
        if self.env.stage_watchdog_s > 0 and self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch, name="stage-watchdog",
                                              daemon=True)
            self._watchdog.start()

    def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connect(self) -> None:
        self._conn = psycopg.connect(self.db.dsn, autocommit=True,
                                     options=f"-c search_path={self.schema},public",
                                     application_name="formi-controller-guard")

    def _c(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._connect()
        assert self._conn is not None
        return self._conn

    # ------------------------------------------------------------------ the guard
    def state(self, point: str) -> str:
        return self.states.get(point, "normal")

    def run(self, point: str, step_no: int, minute: int, fn: Callable[[], T], default: T) -> T:
        """Call `fn` under the guard; `default` is what the loop continues with if it is skipped."""
        state = self.state(point)
        if state == "disabled":
            self._count_skip(point, minute)
            return default
        self._begin(point, step_no)
        try:
            self._inject(point, minute)
            result = fn()
        except TRANSIENT:
            self._forget(point, step_no)
            raise
        except Exception as e:
            raised = self._note_raise(point, step_no, e)
            if state == "normal" and raised < self.env.safe_mode_crashes:
                self._end_current()
                raise
            log.exception("hook %s failed at step %d (minute %d); continuing without it", point,
                          step_no, minute)
            if state == "normal":
                self._enter(point, "guarded", step_no, minute,
                            f"{raised} exceptions at step {step_no}: {_describe(e)}")
            self._record_skip(point, step_no, minute, e)
            self._forget(point, step_no)
            self.needs_recovery = True
            return default
        self._forget(point, step_no)
        return result

    # ------------------------------------------------------------------ attempts
    def _begin(self, point: str, step_no: int) -> None:
        self._c().execute(
            """INSERT INTO hook_attempt (point, step_no, started) VALUES (%s, %s, 1)
               ON CONFLICT (point, step_no) DO UPDATE SET started = hook_attempt.started + 1""",
            (point, step_no))
        with self._lock:
            self._current = (point, step_no)
            self._started = time.monotonic()

    def _end_current(self) -> None:
        with self._lock:
            self._current = None

    def _forget(self, point: str, step_no: int) -> None:
        self._end_current()
        self._c().execute("DELETE FROM hook_attempt WHERE point = %s AND step_no = %s",
                          (point, step_no))

    def _note_raise(self, point: str, step_no: int, e: Exception) -> int:
        r = self._c().execute(
            """UPDATE hook_attempt SET raised = raised + 1, last_error = %s
               WHERE point = %s AND step_no = %s RETURNING raised""",
            (_describe(e), point, step_no)).fetchone()
        return int(r[0]) if r else 1

    # ------------------------------------------------------------------ records
    def _enter(self, point: str, state: str, step_no: int, minute: int, reason: str) -> None:
        self.states[point] = state
        log.critical("SAFE MODE: hook %s is now %s: %s", point, state, reason)
        conn = self._c()
        with conn.transaction():
            did = write_decision(conn, self.decision(
                wf=None, module="loop.safemode", kind="safe_mode",
                rule_id="hook_crashes_at_one_step",
                inputs={"point": point, "step_no": step_no, "reason": reason,
                        "safe_mode_crashes": self.env.safe_mode_crashes,
                        "safe_mode_hard_crashes": self.env.safe_mode_hard_crashes},
                outcome={"state": state}, sim_minute=minute))
            conn.execute(
                """INSERT INTO hook_guard (point, state, since_step, since_minute, reason)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (point) DO UPDATE SET state = excluded.state,
                       since_step = excluded.since_step, since_minute = excluded.since_minute,
                       reason = excluded.reason""",
                (point, state, step_no, minute, reason))
            record(conn, wf=None, actor="controller", action="safe_mode_entered",
                   subject=("hook", point), summary=f"hook {point} is now {state}: {reason}",
                   details={"state": state, "step_no": step_no, "reason": reason},
                   caused_by=did, sim_minute=minute)
            raise_alert(conn, "SAFE_MODE", None, point,
                        f"hook {point} is {state} since minute {minute}: {reason}",
                        sim_minute=minute)
            conn.execute("DELETE FROM hook_attempt WHERE point = %s", (point,))

    def _record_skip(self, point: str, step_no: int, minute: int, e: Exception) -> None:
        conn = self._c()
        with conn.transaction():
            conn.execute("UPDATE hook_guard SET skips = skips + 1, last_skip_minute = %s "
                         "WHERE point = %s", (minute, point))
            record(conn, wf=None, actor="controller", action="hook_skipped",
                   subject=("hook", point), summary=f"hook {point} skipped at minute {minute}",
                   details={"step_no": step_no, "error": _describe(e)}, sim_minute=minute)

    def _count_skip(self, point: str, minute: int) -> None:
        self._c().execute("UPDATE hook_guard SET skips = skips + 1, last_skip_minute = %s "
                          "WHERE point = %s", (minute, point))

    # ------------------------------------------------------------------ test faults
    def _inject(self, point: str, minute: int) -> None:
        for f in self.faults:
            if f.point == point and minute >= f.from_minute:
                f.fire()

    def inject_core(self, minute: int) -> None:
        """The `core` fault point: a failure outside every hook (reaches limp mode)."""
        self._inject("core", minute)

    # ------------------------------------------------------------------ hang watchdog
    def _watch(self) -> None:
        limit = self.env.stage_watchdog_s
        while not self._stop.wait(min(limit / 4, 15.0)):
            with self._lock:
                cur, started = self._current, self._started
            if cur is not None and time.monotonic() - started > limit:
                self._fire_watchdog(cur)

    def _fire_watchdog(self, cur: tuple[str, int]) -> None:
        """The hook has been running for longer than any stage may: leave a record and die, so a
        restart can judge it. Runs on its own connection: the main one is in use."""
        point, step_no = cur
        log.critical("hook %s has run for more than %.0f s at step %d: exiting", point,
                     self.env.stage_watchdog_s, step_no)
        faulthandler.dump_traceback(file=sys.stderr)
        try:
            with psycopg.connect(self.db.dsn, autocommit=True, connect_timeout=10,
                                 options=f"-c search_path={self.schema},public") as conn:
                conn.execute("""UPDATE hook_attempt SET raised = raised + 1, last_error = %s
                                WHERE point = %s AND step_no = %s""", (WATCHDOG, point, step_no))
        finally:
            for h in logging.getLogger().handlers:
                h.flush()
            os._exit(EXIT_RESTART)


def _describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:500]


__all__ = ["HookGuard", "TRANSIENT"]
