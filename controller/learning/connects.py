"""Who connects under a config: the expectation the guardrail forecast needs (DESIGN.md 5.8, 5.10).

Owner: integration step I4 (milestone M5). ADR 0031.

`MixGuardrailMonitor.predict` (T3) fails closed on a schedule or caller-ID change unless the caller
says how such a change moves dials and connects. This module is that caller, for every workflow:
a pickup-primary workflow with an engagement guardrail is exactly where a schedule change can
raise pickup by shifting connects toward low-engagement cells (5.10).

The expectation is the formula of 5.8, per cell c and window hour h, for a config `cfg`:

    dials_c(h)    = capacity(h) x q_c(h)
    q_c(h)        = eligible_c x w_c(h) / sum_c' eligible_c' x w_c'(h)     (the dialler ranks
                    eligible leads by weight times noise, and capacity binds)
    connects_c(h) = dials_c(h) x p_c(h) x F_cfg(c) / F_live(c)
    F_cfg(c)      = sum_d share_d(cohort of c) x r_d                       (the DID factor)

- capacity(h) is `dials_per_hour` from the API; w is the hour table of the cell's first-match
  cohort under cfg (weight 1 without a table, 0 for an hour a table leaves out).
- eligible_c is the cell's active leads (or, without tracked leads, its share of dials).
- p_c(h) is the cell's pickup at that hour from `r_pickup`, shrunk toward the workflow's pooled
  pickup at that hour with the hour-curve prior (200 dials, 5.8). It already contains the DID mix
  and health the calls were made under, which is why only the ratio of DID factors enters.
- r_d is the persisted estimate of the DID's relative pickup, 1 where none exists (unknown DIDs
  are judged by the caller-ID line of the gate, not here). A DID's load effect is not modelled:
  moving share off a loaded DID helps more than this says, so the forecast errs against the move.

Pure: a function of one `WorkflowSnapshot`.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np

from controller.learning.schedule import window_hours
from controller.settings import Thresholds
from controller.snapshot import cohort_of_cells
from controller.types import Config, WorkflowSnapshot


class ConnectModel:
    """Expected dials, connects and pickup per cell under any config of one workflow."""

    def __init__(self, ws: WorkflowSnapshot, t: Thresholds):
        self.ws = ws
        self.hours = window_hours(ws)
        pk = ws.pickup
        hour_pos = {h: i for i, h in enumerate(self.hours)}
        in_win = np.isin(pk.hour, self.hours) if len(pk) else np.zeros(0, dtype=bool)
        cells = {int(c) for c in pk.cell[in_win]}
        if ws.active is not None:
            cells |= {int(c) for c in ws.active.cells}
        self.cells = sorted(cells)
        cell_pos = {c: i for i, c in enumerate(self.cells)}
        n = np.zeros((len(self.cells), len(self.hours)))
        y = np.zeros_like(n)
        for c, h, d, k in zip(pk.cell[in_win].tolist(), pk.hour[in_win].tolist(),
                              pk.dials[in_win].tolist(), pk.connected[in_win].tolist()):
            n[cell_pos[int(c)], hour_pos[int(h)]] += d
            y[cell_pos[int(c)], hour_pos[int(h)]] += k
        tot_n, tot_y = n.sum(axis=0), y.sum(axis=0)
        overall = tot_y.sum() / tot_n.sum() if tot_n.sum() > 0 else 0.0
        pooled = np.where(tot_n > 0, tot_y / np.maximum(tot_n, 1.0), overall)
        m = t.hour_curve_prior
        self.p = (y + m * pooled[None, :]) / (n + m)
        self.eligible = self._eligible(cell_pos, n)
        self.capacity = float(ws.spec.dials_per_hour)
        self.r = {d: (float(s.r_hat) if s.r_hat is not None else 1.0)
                  for d, s in ws.did_state.items()}
        self._f_live = self._did_factor(ws.live)
        self._memo: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _eligible(self, cell_pos: Mapping[int, int], dials: np.ndarray) -> np.ndarray:
        ws = self.ws
        out = np.zeros(len(self.cells))
        if ws.active is not None and float(np.sum(ws.active.leads)) > 0:
            for c, v in zip(ws.active.cells.tolist(), ws.active.leads.tolist()):
                out[cell_pos[int(c)]] = float(v)
            return out
        return dials.sum(axis=1)

    def _did_factor(self, cfg: Config) -> np.ndarray:
        coh = cohort_of_cells(cfg, self.ws.cells, self.cells)
        memo: dict[str, float] = {}
        for k in set(coh):
            memo[k] = sum(float(p) * self.r.get(d, 1.0) for d, p in cfg.dist(k, "caller_id").items())
        return np.array([memo[k] for k in coh]) if coh else np.zeros(0)

    def _weights(self, cfg: Config) -> np.ndarray:
        coh = cohort_of_cells(cfg, self.ws.cells, self.cells)
        tables: dict[str, np.ndarray] = {}
        for k in set(coh):
            table = cfg.hour_table(k)
            tables[k] = np.ones(len(self.hours)) if table is None else np.array(
                [float(table.get(h, 0.0)) for h in self.hours])
        return np.stack([tables[k] for k in coh]) if coh else np.zeros((0, len(self.hours)))

    def _expect(self, cfg: Config) -> tuple[np.ndarray, np.ndarray]:
        key = cfg.hash()
        if key not in self._memo:
            q = self.eligible[:, None] * self._weights(cfg)
            tot = q.sum(axis=0, keepdims=True)
            q = np.divide(q, tot, out=np.zeros_like(q), where=tot > 0)
            dials = self.capacity * q
            ratio = np.divide(self._did_factor(cfg), self._f_live,
                              out=np.ones(len(self.cells)), where=self._f_live > 0)
            connects = dials * self.p * ratio[:, None]
            self._memo[key] = (dials.sum(axis=1), connects.sum(axis=1))
        return self._memo[key]

    def factor_against(self, cfg: Config) -> float:
        """The live caller-ID mix's mean DID factor against `cfg`'s, weighted by eligible leads."""
        f = self._did_factor(cfg)
        w = self.eligible
        if len(w) == 0 or w.sum() <= 0:
            return 1.0
        a, b = float((self._f_live * w).sum()), float((f * w).sum())
        return a / b if b > 0 else 1.0

    def dials(self, cfg: Config) -> dict[int, float]:
        d, _ = self._expect(cfg)
        return {c: float(v) for c, v in zip(self.cells, d)}

    def connects(self, cfg: Config) -> dict[int, float]:
        _, k = self._expect(cfg)
        return {c: float(v) for c, v in zip(self.cells, k)}

    def pickup(self, cfg: Config) -> dict[int, float]:
        d, k = self._expect(cfg)
        return {c: float(a / b) if b > 0 else 0.0 for c, a, b in zip(self.cells, k, d)}


__all__ = ["ConnectModel"]
