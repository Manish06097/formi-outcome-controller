"""A SimClient-shaped fake for validation runs and suggest-fixes (T4 pure tests).

It mirrors the contract of SIMULATOR.md 5: ceil(n/100) credits per run, n in [50, 2000], ids
sequential and global, failures binomial at a hidden per-prompt failure rate, 402 when out of
credits, suggest-fixes costs 3 and returns up to 3 other catalog prompts. The failure rates are
test inputs, never read by controller code.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np

from controller.simclient.errors import OutOfCredits
from controller.types import Suggestion, ValidationRun


class FakeSim:
    def __init__(self, q: Mapping[str, float], credits: float = 200.0, seed: int = 0,
                 catalog: Mapping[str, Sequence[str]] | None = None):
        self.q = dict(q)
        self.credits = float(credits)
        self.rng = np.random.default_rng(seed)
        self.next_id = 1
        self.calls: list[tuple] = []
        self.catalog = {k: list(v) for k, v in (catalog or {}).items()}

    def _charge(self, cost: float) -> None:
        if self.credits < cost:
            raise OutOfCredits(402, "out_of_credits", "out of credits", "POST", "/validation/runs")
        self.credits -= cost

    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun:
        assert 50 <= n <= 2000
        self._charge(math.ceil(n / 100))
        f = int(self.rng.binomial(n, self.q[prompt]))
        rid = f"val_{self.next_id:05d}"
        self.next_id += 1
        self.calls.append(("validate", wf, prompt, n, f))
        return ValidationRun(rid, wf, prompt, n, f, 1 - f / n, "2027-01-04T10:00:00+05:30",
                             self.credits)

    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]:
        self._charge(3)
        self.calls.append(("suggest", wf, problem))
        others = list(self.catalog.get(wf, ()))
        pick = self.rng.permutation(len(others))[:3] if others else []
        return [Suggestion(others[i], f"suggested {others[i]}") for i in pick]
