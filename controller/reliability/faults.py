"""Crash injection for the crash-recovery demonstration.

Owner: track T1 (milestone M2). Implements DESIGN.md 10.2 step 1.

`CTL_FAULT=<kind>:<n>:<phase>[,<kind>:<n>:<phase>...]` makes the process call `os._exit(137)`
(the same effect as SIGKILL: no cleanup, no commit, no flush of anything unwritten) the n-th time
it reaches the point `(kind, phase)`. Code marks its side-effect boundaries with
`maybe_crash(kind, phase)`.

Points (kind: phases):
- advance, submit_approval, auto_apply, apply, validation, suggest_fixes:
  before_t1 | after_t1 | after_http | in_t2 (the write-ahead intent protocol, 8.2)
- chunk_copy: mid (half of the chunk's bytes are inside an open COPY)
- chunk_transform: before_t1 | in_t2 (statements done, COMMIT not sent) | after_t2 (committed)
- audit_write: in_t2 (an audit row is inside an uncommitted transaction)
- bootstrap: mid (between bootstrap stages)

A fault fires once per run: before exiting, the injector calls `on_fire` (the orchestrator
writes an audit row in its own committed transaction), and a restarted process asks
`already_fired` and stays disarmed for that spec. Without both callbacks the fault fires once per
process. The n-th count is per process.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

EXIT_CODE = 137
KNOWN_KINDS = frozenset({"advance", "submit_approval", "auto_apply", "apply", "validation",
                         "suggest_fixes", "chunk_transform", "chunk_copy", "audit_write",
                         "bootstrap"})


@dataclass(frozen=True)
class FaultSpec:
    kind: str
    n: int
    phase: str

    def text(self) -> str:
        return f"{self.kind}:{self.n}:{self.phase}"


def parse_faults(raw: str | None) -> tuple[FaultSpec, ...]:
    """Parse `CTL_FAULT`. Raises ValueError on a malformed entry (a configuration error)."""
    if not raw:
        return ()
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3 or not bits[1].isdigit() or int(bits[1]) < 1:
            raise ValueError(f"CTL_FAULT entry {part!r} must be <kind>:<n>:<phase> with n >= 1")
        if bits[0] not in KNOWN_KINDS:
            raise ValueError(f"CTL_FAULT kind {bits[0]!r} is not one of {sorted(KNOWN_KINDS)}")
        out.append(FaultSpec(bits[0], int(bits[1]), bits[2]))
    return tuple(out)


class FaultInjector:
    def __init__(self, specs: tuple[FaultSpec, ...] = (),
                 on_fire: Callable[[FaultSpec], None] | None = None,
                 already_fired: Callable[[FaultSpec], bool] | None = None,
                 exit_fn: Callable[[int], None] = os._exit):
        self._armed = [s for s in specs if not (already_fired and already_fired(s))]
        self._counts: dict[tuple[str, str], int] = {}
        self._on_fire = on_fire
        self._exit = exit_fn
        self._lock = threading.RLock()
        self._firing = False

    @property
    def armed(self) -> tuple[FaultSpec, ...]:
        return tuple(self._armed)

    def hit(self, kind: str, phase: str) -> None:
        if not self._armed or self._firing:
            return
        with self._lock:
            if self._firing:      # a boundary reached while recording a firing fault
                return
            key = (kind, phase)
            self._counts[key] = c = self._counts.get(key, 0) + 1
            spec = next((s for s in self._armed if (s.kind, s.phase) == key and s.n == c), None)
            if spec is None:
                return
            self._armed.remove(spec)
            self._firing = True
            if self._on_fire is not None:
                try:
                    self._on_fire(spec)
                except Exception:  # the crash must happen even if recording it fails
                    log.exception("could not record fault %s", spec.text())
            log.critical("CTL_FAULT %s: exiting with %d", spec.text(), EXIT_CODE)
            for h in logging.getLogger().handlers:
                h.flush()
            sys.stderr.flush()
            self._exit(EXIT_CODE)


_injector = FaultInjector()


def install(injector: FaultInjector) -> None:
    global _injector
    _injector = injector


def maybe_crash(kind: str, phase: str) -> None:
    """Mark a side-effect boundary; exits the process if an armed fault matches."""
    _injector.hit(kind, phase)
