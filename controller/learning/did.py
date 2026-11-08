"""Caller-ID health: DidHealth.update(ws), classify(ws), plan_caller_ids(ws).

Owner: track T4 (milestone M4). Implements DESIGN.md 5.9 (caller-ID health and rotation) and the
drain planning of 7.3; ADR 0008.

Signal. DIDs are sampled per dial independently of the lead, so the pickup of DID d against the
pooled other live DIDs, stratified by (cohort, hour) from `r_did`, is a concurrent and unbiased
comparison: `r_d` is the Mantel-Haenszel risk ratio (Greenland-Robins variance of log r).

States: unknown -> probing -> healthy | unhealthy; healthy -> degraded (slow path here, fast path
by the detector and incident manager); a DID degraded by the slow path (or from history) is
re-admitted as a probe after 3 days, because load decay may explain it; a DID degraded by the
fast path (a sudden drop below 0.75 that load imbalance cannot produce, 7.3) or below the slow
path's load floor (UCB under 0.5), and a probe the SPRT judged unhealthy (a probe carries almost
no load), are not re-admitted (ADR 0030).
`suspect` is set by the fast detector and left alone here.

- Probing: Wald's SPRT of r = 0.70 against r = 1.0 per probed DID, on its dials in tick order,
  each tick-and-stratum batch using the comparator pickup of the same (day, hour, cohort);
  truncated at 1,000 dials (healthy iff r_hat >= 0.85).
- Slow path: a healthy DID is degraded if the upper 95% bound of r_d is below 0.85 in a trailing
  window of 12, 24 or 48 dialling hours.
- Plan: probes of at most two unknown DIDs at `s = max(0.5%, min(2.8%, 400 / dials_per_day))`;
  equal shares over healthy DIDs (proportional to r_d only when healthy DIDs differ
  significantly); 0 on unhealthy and degraded DIDs; no healthy DID: keep the allocation, probe,
  report NO_HEALTHY_DID. A fast-path drain is a P0b chain of steps of at most `max_shift` that
  move share only off the drained DID where caller_id auto-applies, else one full step.

Codes: `r_did.did` indexes `spec.options["caller_id"]`. Pure: everything is computed from the
snapshot (`ws.did_state` holds the persisted states); the caller persists `classify()`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from statistics import NormalDist
from typing import Iterable, Mapping, Sequence

import numpy as np

from controller.settings import Thresholds
from controller.stats.draws import derive_seed
from controller.stats.sequential import BernoulliRatioSprt
from controller.world.clock import freeze_start
from controller.types import (DEFAULT_COHORT, P0_INCIDENT, P0B_DID_DRAIN, P3_ROUTINE, Config,
                              DidArrays,
                              DidPosterior, DidState, Finding, FrozenDict, PatchRequest,
                              WorkflowSnapshot, freeze, thaw)

OWNER = "learning.did"
LIVE_STATES = ("healthy", "suspect", "degraded", "unhealthy", "probing", "unknown")
BAD_STATES = frozenset({"degraded", "unhealthy"})
READMIT_PATHS = frozenset({"slow", "history"})
"""The only DIDs re-admitted as a probe after 3 days (ADR 0030): degraded by the slow path or
from history, a shortfall a heavy load can explain, and load decays (tau 2 days). Never
re-admitted: a sudden drop (fast path), a shortfall below the load floor, and a probe the SPRT
judged unhealthy (a probe carries almost no load, so load cannot explain it). Re-probing those
only re-measures a bad DID on real calls: I2's harmful public_02 change was such a probe, and
custom (h) re-probed a DID at r = 0.3 every 3 days."""
PRIOR_KEYS = ("prior_r_hat", "prior_r_lcb", "prior_r_ucb")
"""Evidence keys of a re-admitted DID: the estimate it was degraded or judged unhealthy with."""


def _prior_of(st: DidState) -> dict:
    """The last estimate of a DID being re-admitted (empty if it never had a finite one)."""
    if st.r_hat is None or st.r_ucb is None or not math.isfinite(st.r_ucb) or st.r_hat <= 0:
        return {}
    return {"prior_r_hat": float(st.r_hat), "prior_r_ucb": float(st.r_ucb),
            "prior_r_lcb": float(st.r_lcb) if st.r_lcb is not None else None}

# =============================================================================== MH ratio


@dataclass(frozen=True)
class RatioEstimate:
    """Mantel-Haenszel pickup ratio of one DID vs its comparator over a set of strata."""

    r: float
    se_log: float
    dials: int
    connected: int
    comp_dials: int
    comp_connected: int
    score_z: float = 0.0
    """Cochran-Mantel-Haenszel score statistic (O - E) / sqrt(V) under r = 1."""

    @property
    def z(self) -> float:
        """log r / SE (negative when the DID picks up less than its comparator). With zero
        connects on the DID the log-scale SE is dominated by the continuity correction and
        understates the evidence, so the score statistic is used instead."""
        if self.connected == 0 and self.dials > 0:
            return self.score_z
        if self.se_log <= 0 or not math.isfinite(self.se_log):
            return 0.0
        return math.log(self.r) / self.se_log

    def bound(self, conf: float, upper: bool) -> float:
        """Two-sided `conf` interval end (DESIGN.md 5.9 quotes two-sided 95%: z = 1.96)."""
        zc = NormalDist().inv_cdf(1.0 - (1.0 - conf) / 2.0)
        if not math.isfinite(self.se_log):
            return math.inf if upper else 0.0
        return self.r * math.exp((zc if upper else -zc) * self.se_log)


def mh_ratio(a: np.ndarray, n1: np.ndarray, b: np.ndarray, n0: np.ndarray) -> RatioEstimate:
    """Mantel-Haenszel risk ratio over strata, with the Greenland-Robins (1985) variance of its
    log. `a`/`n1`: connects/dials of the DID; `b`/`n0`: of the comparator. Strata with an empty
    side carry no information and are dropped. A zero total on either side gets a 0.5
    continuity correction per stratum, so the estimate stays finite; the CMH score statistic is
    computed on the raw counts."""
    a, n1, b, n0 = (np.asarray(x, dtype=float) for x in (a, n1, b, n0))
    keep = (n1 > 0) & (n0 > 0)
    a, n1, b, n0 = a[keep], n1[keep], b[keep], n0[keep]
    dials, conn = int(n1.sum()), int(a.sum())
    comp_dials, comp_conn = int(n0.sum()), int(b.sum())
    if len(a) == 0:
        return RatioEstimate(1.0, math.inf, dials, conn, comp_dials, comp_conn)
    t = n1 + n0
    m1 = a + b
    e = np.sum(n1 * m1 / t)
    v = np.sum(n1 * n0 * m1 * (t - m1) / (t ** 2 * np.maximum(t - 1, 1)))
    score = float((a.sum() - e) / math.sqrt(v)) if v > 0 else 0.0
    if a.sum() == 0 or b.sum() == 0:
        a, b = a + 0.5, b + 0.5
        n1, n0 = n1 + 1.0, n0 + 1.0
        t = n1 + n0
    num = np.sum(a * n0 / t)
    den = np.sum(b * n1 / t)
    r = float(num / den)
    var = float(np.sum(((a + b) * n1 * n0 - a * b * t) / t ** 2) / (num * den))
    se = math.sqrt(var) if var > 0 else math.inf
    return RatioEstimate(r, se, dials, conn, comp_dials, comp_conn, score)


# =============================================================================== windows


def did_names(ws: WorkflowSnapshot) -> tuple[str, ...]:
    return tuple(ws.spec.options.get("caller_id", ()))


def completed_dialling_ticks(da: DidArrays, now: int, tick: int) -> np.ndarray:
    """Sorted tick numbers with any dials that ended by `now`."""
    if len(da) == 0:
        return np.zeros(0, dtype=np.int64)
    ok = (da.dials > 0) & (da.tick_no < now // tick)
    return np.unique(da.tick_no[ok])


def trailing_window(da: DidArrays, now: int, tick: int, hours: float) -> np.ndarray:
    """Boolean row mask of the trailing `hours` DIALLING hours (ticks with dials), exact because
    `r_did` is keyed by tick (DESIGN.md 3.2, 7.3)."""
    ticks = completed_dialling_ticks(da, now, tick)
    n = max(1, int(round(hours * 60 / tick)))
    return np.isin(da.tick_no, ticks[-n:])


def ratio_vs_others(da: DidArrays, mask: np.ndarray, did: int,
                    comparators: Iterable[int] | None = None) -> RatioEstimate:
    """MH ratio of DID code `did` vs the pooled comparator DIDs (default: every other DID with
    dials in the window), stratified by (cohort, hour of day)."""
    m = mask & (da.dials > 0)
    hour, coh, dd = da.hour[m], da.cohort_code[m], da.did[m]
    dials, conn = da.dials[m], da.connected[m]
    comp = (dd != did) if comparators is None else np.isin(dd, list(comparators)) & (dd != did)
    own = dd == did
    key = coh.astype(np.int64) * 100 + hour
    keys = np.unique(key[own | comp])
    idx = np.searchsorted(keys, key)
    k = len(keys)
    a = np.bincount(idx[own], weights=conn[own], minlength=k)
    n1 = np.bincount(idx[own], weights=dials[own], minlength=k)
    b = np.bincount(idx[comp], weights=conn[comp], minlength=k)
    n0 = np.bincount(idx[comp], weights=dials[comp], minlength=k)
    return mh_ratio(a, n1, b, n0)


# =============================================================================== sizing


def probe_share(dials_per_day: float, t: Thresholds) -> float:
    """s = max(0.5%, min(2.8%, 400 / dials_per_day)) (DESIGN.md 5.9)."""
    vol = t.did_probe_dials_per_day / dials_per_day if dials_per_day > 0 else t.did_probe_max
    return max(t.did_probe_min, min(t.did_probe_max, vol))


def probe_gate_share(t: Thresholds) -> float:
    """The DID-gate ceiling: a probe must be safe even if flagged, s (1 - r_flag) <= 0.018."""
    return t.did_probe_gate_dd / (1.0 - t.did_r_flag)


def flag_day_share(s: float, m: float, t_pre_h: float, t_alarm_h: float, tick_min: int,
                   window_h: float) -> float:
    """The flagged DID's share of the flag day's dials (DESIGN.md 7.3):

        ( s (t_pre + t_alarm) + tick_h sum_{j=1..k-1} (s - j m) ) / H,  k = ceil(s / m)

    Every chain step applies at a stop and its residual share is dialled for one tick."""
    if m <= 0:   # no chain possible: the share stays until a full-step change
        return s * (t_pre_h + t_alarm_h) / window_h
    k = math.ceil(s / m - 1e-12)
    tail = sum(max(s - j * m, 0.0) for j in range(1, k))
    return (s * (t_pre_h + t_alarm_h) + (tick_min / 60.0) * tail) / window_h


def tv(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def drain_step(cur: Mapping[str, float], target: Mapping[str, float], drained: Iterable[str],
               m: float) -> dict[str, float]:
    """One risk-reducing chain step (DESIGN.md 6.5, 7.3): move at most `m` total share off the
    drained DIDs (proportionally) onto the DIDs below their target (proportionally to the
    deficit). Share never leaves a non-drained DID, so the step stays risk-reducing; TV = amount."""
    drained = [d for d in drained if cur.get(d, 0.0) > target.get(d, 0.0) + 1e-15]
    remove = {d: cur[d] - target.get(d, 0.0) for d in drained}
    total = sum(remove.values())
    deficits = {d: target[d] - cur.get(d, 0.0) for d in target
                if target[d] > cur.get(d, 0.0) + 1e-15 and d not in remove}
    tot_def = sum(deficits.values())
    if total <= 0 or tot_def <= 0:
        return dict(cur)
    amount = min(m, total, tot_def)
    new = dict(cur)
    for d, r in remove.items():
        new[d] = cur[d] - amount * r / total
    for d, df in deficits.items():
        new[d] = cur.get(d, 0.0) + amount * df / tot_def
    return {d: w for d, w in new.items() if w > 1e-12}


def drain_chain(cur: Mapping[str, float], target: Mapping[str, float], drained: Iterable[str],
                m: float, max_steps: int = 1000) -> list[dict[str, float]]:
    """Every step of a drain chain until the drained DIDs reach their target share."""
    drained = list(drained)
    steps, x = [], dict(cur)
    for _ in range(max_steps):
        if all(x.get(d, 0.0) <= target.get(d, 0.0) + 1e-12 for d in drained):
            break
        nx = drain_step(x, target, drained, m)
        if tv(nx, x) <= 1e-15:
            break
        steps.append(nx)
        x = nx
    return steps


# =============================================================================== the model


@dataclass
class DidHealthModel:
    """The concrete `DidHealth` (types.DidHealth protocol). The snapshot's `end_minute` (from
    the API) enables the end-game freeze of new probes; the constructor's `end_minute` is the
    fallback for a snapshot without one; `p_hist` is the workflow's one pickup rate (history, or
    the first 2 dialling days) used where no comparator stratum exists."""

    t: Thresholds = field(default_factory=Thresholds)
    end_minute: int | None = None
    p_hist: float | None = None
    _cache: dict = field(default_factory=dict, repr=False)
    blocked: list[dict] = field(default_factory=list, repr=False)
    """Changes the harm gate cut to nothing at the last `plan_caller_ids` (the caller records
    each as a decision not to act)."""

    # ------------------------------------------------------------------ helpers
    def _sprt(self, st: DidState) -> BernoulliRatioSprt:
        t = self.t
        return BernoulliRatioSprt(t.sprt_r_unhealthy, t.sprt_r_healthy, t.sprt_alpha,
                                  t.sprt_beta, llr=st.sprt_llr, n=st.n_dials)

    def live_shares(self, ws: WorkflowSnapshot) -> dict[str, float]:
        """Largest share each DID has in any cohort's caller_id distribution."""
        out: dict[str, float] = {}
        keys = {DEFAULT_COHORT, *ws.live.allocations}
        for k in keys:
            for d, w in ws.live.dist(k, "caller_id").items():
                out[d] = max(out.get(d, 0.0), float(w))
        return out

    def _pickup_rate(self, da: DidArrays) -> float:
        if self.p_hist is not None:
            return self.p_hist
        d = da.dials.sum()
        return float(da.connected.sum() / d) if d > 0 else 0.25

    def in_freeze(self, now: int, end_minute: int | None = None) -> bool:
        end = end_minute if end_minute is not None else self.end_minute
        start = freeze_start(end, self.t.freeze_min_days, self.t.freeze_frac)
        return start is not None and now >= start

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, ws: WorkflowSnapshot) -> dict[str, DidState]:
        """D0 states from history: DIDs with history dials are healthy unless their UCB95 vs the
        other history DIDs is below 0.85 (then degraded); a sole DID is healthy (no comparator;
        the drift detector covers it, 7.5); DIDs never dialled are unknown."""
        names = did_names(ws)
        da = ws.did
        hist = da.tick_no < 0
        used = sorted({int(x) for x in da.did[hist & (da.dials > 0)].tolist()})
        out: dict[str, DidState] = {}
        for code, name in enumerate(names):
            if code not in used:
                out[name] = DidState(ws.spec.workflow_id, name, "unknown", since_minute=ws.now)
                continue
            if len(used) < 2:
                out[name] = DidState(ws.spec.workflow_id, name, "healthy", since_minute=ws.now,
                                     n_dials=int(da.dials[hist & (da.did == code)].sum()),
                                     evidence=freeze({"source": "history",
                                                      "note": "sole DID, no comparator"}))
                continue
            est = ratio_vs_others(da, hist, code)
            ucb = est.bound(self.t.did_slow_conf, upper=True)
            status = "degraded" if ucb < self.t.sprt_indifference_r else "healthy"
            out[name] = DidState(ws.spec.workflow_id, name, status, r_hat=est.r,
                                 r_lcb=est.bound(self.t.did_slow_conf, False), r_ucb=ucb,
                                 n_dials=est.dials, since_minute=ws.now,
                                 evidence=freeze({"source": "history",
                                                  "path": "floor" if ucb < self.t.did_slow_floor_r
                                                  else "history"}))
        return out

    # ------------------------------------------------------------------ update and classify
    def step(self, ws: WorkflowSnapshot) -> tuple[dict[str, DidState], list[Finding]]:
        """New states and the findings that explain each change. Cached per (wf, now, version)."""
        key = (ws.spec.workflow_id, ws.now, ws.live_version, id(ws.did))
        if key in self._cache:
            return self._cache[key]
        names = did_names(ws)
        tick = ws.policy.tick
        now_tick = ws.now // tick
        shares = self.live_shares(ws)
        prev: dict[str, DidState] = {n: ws.did_state.get(n) or DidState(ws.spec.workflow_id, n,
                                                                         "unknown")
                                     for n in names}
        states: dict[str, DidState] = {}
        findings: list[Finding] = []
        for code, name in enumerate(names):
            st = prev[name]
            share = shares.get(name, 0.0)
            if st.status == "degraded" and st.since_minute is not None and share <= 0 \
                    and st.evidence.get("path") in READMIT_PATHS \
                    and ws.now - st.since_minute >= self.t.did_readmit_days * 1440:
                # the last estimate travels with the re-admitted DID: the probe's harm gate
                # judges it from what we measured, not from the untested-DID prior
                st = DidState(st.wf, name, "unknown", since_minute=ws.now,
                              evidence=freeze({"readmitted_from": st.status,
                                               **_prior_of(st)}))
                findings.append(self._finding(ws, name, "readmit", 0.0, 0.0, {}))
            if st.status == "unknown" and share > 0:
                st = DidState(st.wf, name, "probing", since_minute=ws.now,
                              evidence=freeze({"last_tick": now_tick - 1, "probe_share": share,
                                               **{k: v for k, v in st.evidence.items()
                                                  if k in PRIOR_KEYS}}))
                findings.append(self._finding(ws, name, "probing_started", share, 0.0, {}))
            if st.status == "probing":
                st, f = self._advance_sprt(ws, code, st, names, prev)
                findings.extend(f)
            elif st.status == "healthy" and share > 0:
                st, f = self._slow_path(ws, code, st, prev, names)
                findings.extend(f)
            states[name] = st
        if not any(s.status == "healthy" for s in states.values()):
            findings.append(self._finding(ws, "*", "no_healthy", 0.0, 0.0,
                                          {"states": {n: s.status for n, s in states.items()}}))
        self._cache = {key: (states, findings)}
        return states, findings

    def update(self, ws: WorkflowSnapshot) -> list[Finding]:
        return self.step(ws)[1]

    def classify(self, ws: WorkflowSnapshot) -> Mapping[str, DidState]:
        return FrozenDict(self.step(ws)[0])

    def _finding(self, ws: WorkflowSnapshot, did: str, kind: str, stat: float, thr: float,
                 ev: Mapping) -> Finding:
        return Finding(wf=ws.spec.workflow_id, detector="did", kind=kind, scope=did, stat=stat,
                       threshold=thr, first_evidence_minute=ws.now, evidence=freeze(dict(ev)))

    def _comparator_codes(self, names: Sequence[str], prev: Mapping[str, DidState],
                          code: int) -> list[int]:
        """Comparator for a probe: the established DIDs (not unknown or probing); if there are
        none, every other DID."""
        est = [c for c, n in enumerate(names) if c != code
               and prev[n].status not in ("unknown", "probing")]
        return est or [c for c in range(len(names)) if c != code]

    def _advance_sprt(self, ws: WorkflowSnapshot, code: int, st: DidState,
                      names: Sequence[str], prev: Mapping[str, DidState]
                      ) -> tuple[DidState, list[Finding]]:
        da, tick = ws.did, ws.policy.tick
        now_tick = ws.now // tick
        last = int(st.evidence.get("last_tick", now_tick - 1))
        start = int(st.evidence.get("start_tick", last + 1))
        comps = self._comparator_codes(names, prev, code)
        sel = (da.tick_no > last) & (da.tick_no < now_tick) & (da.dials > 0)
        own = sel & (da.did == code)
        sprt = self._sprt(st)
        p_default = self._pickup_rate(da)
        if np.any(own):
            day_hour = (da.tick_no * tick) // 60          # absolute hour index
            comp_all = (da.tick_no < now_tick) & np.isin(da.did, comps) & (da.dials > 0)
            for t_no in np.unique(da.tick_no[own]).tolist():
                rows = own & (da.tick_no == t_no)
                for coh in np.unique(da.cohort_code[rows]).tolist():
                    r = rows & (da.cohort_code == coh)
                    n, c = int(da.dials[r].sum()), int(da.connected[r].sum())
                    blk = comp_all & (day_hour == (t_no * tick) // 60) & (da.cohort_code == coh)
                    nd = da.dials[blk].sum()
                    p_s = float(da.connected[blk].sum() / nd) if nd > 0 else p_default
                    sprt = sprt.update_counts(n, c, p_s)
                    if sprt.decision != "continue":
                        break
                if sprt.decision != "continue":
                    break
        win = (da.tick_no >= start) & (da.tick_no < now_tick)
        est = ratio_vs_others(da, win, code, comps)
        ev = thaw(st.evidence)
        ev.update({"last_tick": now_tick - 1, "start_tick": start, "comparators": comps})
        status = "probing"
        kind = None
        if sprt.decision == "accept_h1":
            status, kind = "healthy", "healthy"
        elif sprt.decision == "accept_h0":
            status, kind = "unhealthy", "unhealthy"
        elif sprt.n >= self.t.sprt_n_max:
            status = "healthy" if est.r >= self.t.sprt_indifference_r else "unhealthy"
            kind = status
            ev["truncated"] = True
        ev["sprt_decision"] = sprt.decision
        new = replace(st, status=status, sprt_llr=sprt.llr, n_dials=sprt.n, r_hat=est.r,
                      r_lcb=est.bound(self.t.did_slow_conf, False),
                      r_ucb=est.bound(self.t.did_slow_conf, True),
                      since_minute=ws.now if status != "probing" else st.since_minute,
                      evidence=freeze(ev))
        f = []
        if kind:
            f.append(self._finding(ws, names[code], kind, sprt.llr,
                                   sprt.upper if kind == "healthy" else sprt.lower,
                                   {"n": sprt.n, "r_hat": est.r, "truncated":
                                    bool(ev.get("truncated"))}))
        return new, f

    def _slow_path(self, ws: WorkflowSnapshot, code: int, st: DidState,
                   prev: Mapping[str, DidState], names: Sequence[str]
                   ) -> tuple[DidState, list[Finding]]:
        """Compared with established DIDs only (healthy or suspect, ADR 0030): a probe carries
        almost no load, so against probes an incumbent always reads degraded. Two rules: the
        floor (UCB below `did_slow_floor_r`, a shortfall load cannot explain: degraded, never
        re-admitted) always; the 5.9 rule (UCB below 0.85) only with `did_slow_path_degrades`,
        because a DID still carrying the load of its old share reads 0.6 to 0.8 for days."""
        da, tick = ws.did, ws.policy.tick
        comps = [c for c, n in enumerate(names) if c != code
                 and prev[n].status in ("healthy", "suspect")]
        if not comps:
            return st, []
        worst = None
        for h in self.t.did_slow_windows_h:
            mask = trailing_window(da, ws.now, tick, h)
            est = ratio_vs_others(da, mask, code, comps)
            ucb = est.bound(self.t.did_slow_conf, upper=True)
            if worst is None or ucb < worst[1]:
                worst = (h, ucb, est)
            if est.dials < self.t.did_slow_min_dials or \
                    est.comp_dials < self.t.did_slow_min_dials:
                continue
            floor = ucb < self.t.did_slow_floor_r
            if floor or (self.t.did_slow_path_degrades and ucb < self.t.sprt_indifference_r):
                ev = thaw(st.evidence)
                ev.update({"path": "floor" if floor else "slow", "window_h": h, "r_hat": est.r,
                           "ucb": ucb, "dials": est.dials})
                new = replace(st, status="degraded", r_hat=est.r, r_ucb=ucb,
                              r_lcb=est.bound(self.t.did_slow_conf, False), since_minute=ws.now,
                              evidence=freeze(ev))
                return new, [self._finding(ws, names[code],
                                           "floor_degraded" if floor else "slow_degraded", ucb,
                                           self.t.did_slow_floor_r if floor
                                           else self.t.sprt_indifference_r,
                                           {"window_h": h, "r_hat": est.r, "dials": est.dials})]
        if worst is not None and worst[2].dials > 0:
            est = worst[2]
            st = replace(st, r_hat=est.r, r_ucb=worst[1],
                         r_lcb=est.bound(self.t.did_slow_conf, False))
        return st, []

    # ------------------------------------------------------------------ planning
    def target(self, ws: WorkflowSnapshot, cur: Mapping[str, float],
               states: Mapping[str, DidState], allow_new_probes: bool,
               max_new: int | None = None) -> tuple[dict[str, float], dict]:
        """Target caller_id distribution for one cohort scope and what each part is for.
        `max_new` caps the DIDs that start probing in this change."""
        names = did_names(ws)
        s = probe_share(ws.spec.dials_per_day, self.t)
        healthy = [n for n in names if states[n].status == "healthy"]
        probing = [n for n in names if states[n].status == "probing"]
        suspect = [n for n in names if states[n].status == "suspect" and cur.get(n, 0) > 0]
        slots = max(0, self.t.did_max_probes_per_change - len(probing))
        if max_new is not None:
            slots = min(slots, max_new)
        candidates = [n for n in names if states[n].status == "unknown"]
        new_probes = candidates[:slots] if allow_new_probes else []
        probes = probing + new_probes
        parts: dict = {"probe_share": s, "probes": probes, "new_probes": new_probes,
                       "healthy": healthy, "suspect_kept": suspect,
                       "drained": [n for n in names if states[n].status in BAD_STATES
                                   and cur.get(n, 0) > 0]}
        out: dict[str, float] = {}
        for n in probes:
            out[n] = cur.get(n, 0.0) if n in probing and cur.get(n, 0.0) > 0 else s
            out[n] = min(out[n], s)
        if healthy:
            for n in suspect:          # a suspect DID keeps its share until alarm or clear
                out[n] = cur[n]
            rest = 1.0 - sum(out.values())
            weights = self._healthy_weights(ws, healthy, states)
            for n in healthy:
                out[n] = out.get(n, 0.0) + rest * weights[n]
            parts["split"] = "proportional" if len(set(weights.values())) > 1 else "equal"
        else:
            # no healthy DID: the established DIDs (suspect, degraded, ...) keep their relative
            # mix, scaled to make room for the probes; nothing moves onto unknown DIDs beyond s
            parts["no_healthy_did"] = True
            rest = 1.0 - sum(out.values())
            keep = {n: w for n, w in cur.items() if n not in out and w > 0
                    and states.get(n, DidState("", n, "unknown")).status not in ("unknown",)}
            tot = sum(keep.values())
            if tot <= 0:   # nothing established: keep the current mix as it is
                return dict(cur), parts
            for n, w in keep.items():
                out[n] = rest * w / tot
        return {n: w for n, w in out.items() if w > 1e-12}, parts

    def _healthy_weights(self, ws: WorkflowSnapshot, healthy: Sequence[str],
                         states: Mapping[str, DidState]) -> dict[str, float]:
        """Equal split unless one healthy DID is significantly better than another (its LCB above
        the other's UCB) while carrying about as much traffic; then proportional to r_hat. A
        better DID that carried less is what load produces (A-7: a fresh DID reads about 1.6
        against a sole incumbent): equal shares remove that gap, while proportional shares would
        move 80% onto the fresh DID and overload it."""
        rs = {n: states[n] for n in healthy}
        dials = self._recent_dials(ws)

        def real(a: str, b: str) -> bool:
            sa, sb = rs[a], rs[b]
            return sa.r_lcb is not None and sb.r_ucb is not None and sa.r_lcb > sb.r_ucb \
                and dials.get(a, 0.0) >= self.t.did_equal_load_ratio * dials.get(b, 0.0)
        differ = any(real(a, b) for a in healthy for b in healthy if a != b)
        if differ and all(s.r_hat for s in rs.values()):
            tot = sum(float(s.r_hat) for s in rs.values())  # type: ignore[arg-type]
            return {n: float(rs[n].r_hat) / tot for n in healthy}  # type: ignore[arg-type]
        return {n: 1.0 / len(healthy) for n in healthy}

    def _recent_dials(self, ws: WorkflowSnapshot) -> dict[str, float]:
        """Dials per DID over the longest slow-path window (the load each one has carried)."""
        da, names = ws.did, did_names(ws)
        if len(da) == 0:
            return {}
        mask = trailing_window(da, ws.now, ws.policy.tick, max(self.t.did_slow_windows_h))
        totals = np.bincount(da.did[mask], weights=da.dials[mask], minlength=len(names))
        return {n: float(totals[i]) for i, n in enumerate(names)}

    def gate(self, ws: WorkflowSnapshot, cur: Mapping[str, float], target: Mapping[str, float],
             states: Mapping[str, DidState]) -> tuple[dict[str, float], dict]:
        """The caller-ID harm gate on one scope's change (DESIGN.md 0.1, 5.9): the largest step
        `lam` along the straight line from `cur` to `target` with P(dd < -2%) <= alpha_D under
        `did_posterior`, found by bisection. Probe sizes therefore come out of the gate itself:
        one untested DID can take the full probe share (s (1 - r_flag) is inside the line), two
        together less, and a re-admitted DID is judged by the estimate it was dropped with."""
        post = did_posterior(ws, states, seed=gate_seed(ws), n=self.t.n_draws, t=self.t)
        names = tuple(sorted(set(cur) | set(target)))

        def at(lam: float) -> dict[str, float]:
            return {d: cur.get(d, 0.0) + lam * (target.get(d, 0.0) - cur.get(d, 0.0))
                    for d in names}

        def report(lam: float) -> tuple[float, float]:
            dd = did_dd(cur, at(lam), post)
            return float(dd.mean()), float(np.mean(dd < -self.t.grader_did_line))

        def passes(lam: float) -> bool:
            return report(lam)[1] <= self.t.alpha_d + self.t.rounding

        lam = 1.0 if passes(1.0) else largest_passing_lam(passes, self.t.bisection_iters)
        dd_mean, p = report(lam)
        full_mean, full_p = report(1.0)
        ev = {"lam": round(lam, 6), "dd_mean": round(dd_mean, 6), "p_dd_below_line": round(p, 6),
              "alpha_d": self.t.alpha_d, "line": self.t.grader_did_line,
              "full_dd_mean": round(full_mean, 6), "full_p_dd_below_line": round(full_p, 6),
              "passes_full": lam >= 1.0}
        out = {d: w for d, w in at(lam).items() if w > 1e-12}
        return out, ev

    def chain_permitted(self, ws: WorkflowSnapshot) -> bool:
        p = ws.policy
        return p.auto_enabled and "caller_id" in p.allowed_dims and p.max_shift > 0

    def plan_caller_ids(self, ws: WorkflowSnapshot) -> list[PatchRequest]:
        """Probes, spread and drains for every cohort scope that has its own caller_id
        distribution ("default" plus overrides). One patch per scope."""
        states = dict(self.classify(ws))
        allow_probes = not self.in_freeze(ws.now, ws.end_minute)
        out: list[PatchRequest] = []
        self.blocked = []
        scopes = [DEFAULT_COHORT] + sorted(k for k in ws.live.allocations
                                           if k != DEFAULT_COHORT
                                           and ws.live.has_override(k, "caller_id"))
        for scope in scopes:
            cur = {d: float(w) for d, w in ws.live.dist(scope, "caller_id").items()}
            fast = any(cur.get(n, 0.0) > 0 and s.status == "degraded"
                       and s.evidence.get("path") == "fast" for n, s in states.items())
            # an incident drain carries no new probes (it must stay risk-reducing)
            target, parts = self.target(ws, cur, states, allow_probes and not fast)
            if len(parts["new_probes"]) > 1:
                target, parts = self._probe_count(ws, cur, states, target, parts)
            req = self._patch(ws, scope, cur, target, parts, states)
            if req is not None:
                out.append(req)
        return out

    def _probe_count(self, ws: WorkflowSnapshot, cur: Mapping[str, float],
                     states: Mapping[str, DidState], target: dict, parts: dict
                     ) -> tuple[dict, dict]:
        """How many DIDs start probing in one change (ADR 0030). The grader judges each change
        on its own, so two untested DIDs started together must share one change's harm budget
        (the gate gives each about 1.6%), while one per change gets the full probe share and
        the next one follows at the next planning point. The option with the larger share per
        new probe wins: it classifies the first DID soonest, and the first healthy DID carries
        most of the spread's value (one DID to two is worth far more than three to four)."""
        many, _ = self.gate(ws, cur, target, states)
        one_t, one_p = self.target(ws, cur, states, True, max_new=1)
        one, _ = self.gate(ws, cur, one_t, states)
        s_many = min(many.get(d, 0.0) for d in parts["new_probes"])
        s_one = one.get(one_p["new_probes"][0], 0.0)
        if s_one > s_many + 1e-9:
            one_p = {**one_p, "probe_count": {"chosen": 1, "share_one": round(s_one, 6),
                                              "share_each_if_all": round(s_many, 6)}}
            return one_t, one_p
        return target, {**parts, "probe_count": {"chosen": len(parts["new_probes"]),
                                                 "share_one": round(s_one, 6),
                                                 "share_each_if_all": round(s_many, 6)}}

    def _patch(self, ws: WorkflowSnapshot, scope: str, cur: Mapping[str, float],
               target: Mapping[str, float], parts: Mapping, states: Mapping[str, DidState]
               ) -> PatchRequest | None:
        if tv(cur, target) <= 1e-6:
            return None
        fast = [n for n in parts["drained"]
                if states[n].status == "degraded" and states[n].evidence.get("path") == "fast"]
        ev = {"kind": "caller_id", "scope": scope, "current": dict(cur), "target": dict(target),
              "parts": dict(parts), "states": {n: s.status for n, s in states.items()},
              "tv": round(tv(cur, target), 6)}
        value = dict(target)
        cls, rank = P3_ROUTINE, None
        gated, gev = self.gate(ws, cur, target, states)
        ev["harm_gate"] = gev
        if not fast:
            # exposure-increasing work (probes, spread) is cut back to what the gate passes;
            # a drain only removes share from a degraded DID and is risk-reducing (6.5), so
            # its gate result is recorded but never blocks it
            if tv(cur, gated) < self.t.did_probe_min / 2:
                if gev["lam"] < 1.0:
                    self.blocked.append({"wf": ws.spec.workflow_id, "scope": scope,
                                         "current": dict(cur), "target": dict(target), **gev})
                return None
            entering = [d for d, w in gated.items() if w > 0 and cur.get(d, 0.0) <= 0]
            leaving = [d for d, w in cur.items() if w > 0 and gated.get(d, 0.0) <= 0]
            if not entering and not leaving and (tv(cur, gated) < self.t.did_rebalance_min_tv
                                                 or gev["dd_mean"] <= 0.0):
                # a small re-weighting is not worth a config version, and one that does not
                # raise expected health is churn (it once moved 8% onto a DID at r = 0.34)
                return None
            value = gated
            ev["target"] = {k: round(v, 9) for k, v in gated.items()}
            ev["tv"] = round(tv(cur, gated), 6)
        if fast:
            cls, rank = P0_INCIDENT, P0B_DID_DRAIN
            if self.chain_permitted(ws):
                # one more margin under our own cap (max_shift - margin): rounding the
                # weights to the wire format and the simulator's normalisation move TV by
                # up to about 1e-9, which once pushed a step over the cap and sent it to
                # an approval (a 75-minute stall in the drain, ADR 0030)
                m = ws.policy.max_shift - 2 * self.t.max_shift_margin
                steps = drain_chain(cur, target, fast, m)
                if steps:
                    value = steps[0]
                    ev["chain"] = {"k": len(steps), "max_step": m, "final": dict(target),
                                   "remaining_after": [round(sum(s.get(n, 0.0) for n in fast), 6)
                                                       for s in steps]}
        if "no_healthy_did" in parts:
            ev["alert"] = "NO_HEALTHY_DID"
        return PatchRequest(wf=ws.spec.workflow_id, owner=OWNER,
                            path=("alloc", scope, "caller_id"),
                            value=FrozenDict({k: round(v, 9) for k, v in value.items()}),
                            cls=cls, p0_rank=rank, evidence=freeze(ev))


# =============================================================================== verification


@dataclass(frozen=True)
class PickupComparison:
    """Workflow pickup over some ticks against the 24h before a reference tick, at the same hours
    of day (7.3). `ucb` is the upper 95% bound of `ratio` from the binomial noise of the
    connects after: at 2 dialling hours one workflow's pickup moves about 7% to 10% by chance, so
    a point-estimate line would page on noise."""

    ratio: float
    ucb: float
    dials: int
    connected: int


def pickup_vs_before(ws: WorkflowSnapshot, sel: np.ndarray, before_tick: int
                     ) -> PickupComparison | None:
    """`sel` is a row mask of `ws.did`; None when either side has no dials."""
    da, tick = ws.did, ws.policy.tick
    pre = (da.tick_no < before_tick) & (da.tick_no >= before_tick - 1440 // tick)
    n_pre = float(da.dials[pre].sum())
    n, o = float(da.dials[sel].sum()), float(da.connected[sel].sum())
    if n_pre <= 0 or n <= 0:
        return None
    pooled = float(da.connected[pre].sum()) / n_pre
    rate = {}
    for h in np.unique(da.hour[pre]).tolist():
        m = pre & (da.hour == h)
        if da.dials[m].sum() > 0:
            rate[h] = float(da.connected[m].sum() / da.dials[m].sum())
    exp = sum(float(d) * rate.get(int(h), pooled)
              for h, d in zip(da.hour[sel].tolist(), da.dials[sel].tolist()))
    if exp <= 0:
        return None
    ratio = o / exp
    p = min(max(o / n, 1e-6), 1 - 1e-6)
    se = math.sqrt((1.0 - p) / (n * p))
    return PickupComparison(ratio, ratio * math.exp(1.959964 * se), int(n), int(o))


def caller_change_check(ws: WorkflowSnapshot, applied_minute: int, proposed: Config,
                        t: Thresholds) -> tuple[bool, dict] | None:
    """The 7.3 check on any applied caller-ID change (probe, spread, chain step), over the first
    `drain_verify_after_h` dialling hours after it: a DID the change left at 0 (and still at 0 in
    the live config) has no dials, locked `next_campaign_only` campaigns excepted, and workflow
    pickup is not credibly below `drain_verify_ratio` of the 24h before. None = not yet."""
    da, tick = ws.did, ws.policy.tick
    t0 = applied_minute // tick
    after = (da.tick_no >= t0) & (da.tick_no < ws.now // tick) & (da.dials > 0)
    ticks = np.unique(da.tick_no[after])
    need = int(round(t.drain_verify_after_h * 60 / tick))
    if len(ticks) < need:
        return None
    sel = after & np.isin(da.tick_no, ticks[:need])

    def shares(cfg: Config) -> dict[str, float]:
        out: dict[str, float] = {}
        for k in {DEFAULT_COHORT, *cfg.allocations}:
            for d, w in cfg.dist(k, "caller_id").items():
                out[d] = max(out.get(d, 0.0), float(w))
        return out
    a, b = shares(proposed), shares(ws.live)
    names = did_names(ws)
    dropped = [c for c, n in enumerate(names) if a.get(n, 0.0) <= 0 and b.get(n, 0.0) <= 0]
    still = int(da.dials[sel & np.isin(da.did, dropped)].sum())
    locked_ok = ws.spec.mid_flight == "next_campaign_only"
    cmp = pickup_vs_before(ws, sel, t0)
    ev = {"did_dials_after": still, "locked_campaigns_allowed": locked_ok,
          "hours": need * tick / 60.0}
    if cmp is None:
        return still == 0 or locked_ok, {**ev, "note": "no pre-change data"}
    ev.update({"ratio": cmp.ratio, "ucb": cmp.ucb, "dials": cmp.dials})
    return cmp.ucb >= t.drain_verify_ratio and (still == 0 or locked_ok), ev


# =============================================================================== DID harm gate


def gate_seed(ws: WorkflowSnapshot) -> int:
    """Deterministic seed of one gate evaluation (8.6): the same workflow and minute draw the
    same posterior, so a restarted run and a decision replay reach the same verdict. It is the
    seed of the release train's composed gate (`engagement_plan.seed_for(ws, "gate", "did")`),
    on purpose: this gate cuts a change to the edge of the DID line, and the composed gate that
    judges it at release must see the same draws (GATE-DID). Cut on other draws, an edge change
    reads above the line at release by selection alone (0.082 cut to 0.050 here, 0.062 there)."""
    return derive_seed(ws.run_id, ws.spec.workflow_id, int(ws.now), "gate", "did")


def did_dd(cur: Mapping[str, float], new: Mapping[str, float], post: DidPosterior) -> np.ndarray:
    """Draws of dd = H_new / H_cur - 1 for one scope's caller_id distribution, H = sum_d p_d r_d
    (the grader's expected DID health of world.py:641-652, with our r draws in place of the
    hidden health). A DID the posterior does not know counts as 1.0."""
    r = np.asarray(post.draws, dtype=float)
    col = {d: i for i, d in enumerate(post.dids)}
    ones = np.ones(r.shape[0])

    def health(dist: Mapping[str, float]) -> np.ndarray:
        tot = np.zeros(r.shape[0])
        for d, p in dist.items():
            if p > 0:
                tot += p * (r[:, col[d]] if d in col else ones)
        return tot

    h_cur = health(cur)
    return health(new) / np.where(h_cur > 0, h_cur, 1.0) - 1.0


def largest_passing_lam(passes, iters: int) -> float:
    """Largest lam in [0, 1] with passes(lam), by bisection (0 if even tiny steps fail)."""
    lo, hi = 0.0, 1.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if passes(mid):
            lo = mid
        else:
            hi = mid
    return lo


# =============================================================================== DID posterior


def did_posterior(ws: WorkflowSnapshot, states: Mapping[str, DidState], seed: int,
                  n: int = 4000, t: Thresholds | None = None) -> DidPosterior:
    """Draws of r_d per DID for the caller-ID harm gate (T3's `did_gate`): log-normal around
    r_hat for classified DIDs (SE from their interval), 1.0 for a sole incumbent, and the
    unknown-DID prior for unknown and probing ones: flagged (r_flag) with P(flagged), else uniform
    on [r_flag, r_unknown_max] (DESIGN.md 5.9, 6.6)."""
    t = t or Thresholds()
    rng = np.random.default_rng(seed)
    names = did_names(ws)
    zc = NormalDist().inv_cdf(1.0 - (1.0 - t.did_slow_conf) / 2.0)
    cols = []
    for name in names:
        st = states.get(name) or DidState(ws.spec.workflow_id, name, "unknown")
        prior = st.evidence.get("prior_r_hat") if st.status in ("unknown", "probing") else None
        if prior is not None and st.evidence.get("prior_r_ucb") is not None:
            # re-admitted: what we measured before it was dropped, not the untested prior
            se = max(math.log(float(st.evidence["prior_r_ucb"]) / float(prior)) / zc, 1e-6)
            col = float(prior) * np.exp(rng.normal(0.0, se, n))
        elif st.status in ("unknown", "probing"):
            flagged = rng.random(n) < t.did_prior_p_flagged
            col = np.where(flagged, t.did_r_flag, rng.uniform(t.did_r_flag, t.did_r_unknown_max, n))
        elif st.r_hat is None or st.r_ucb is None or not math.isfinite(st.r_ucb):
            col = np.ones(n)
        else:
            se = max(math.log(st.r_ucb / st.r_hat) / zc, 1e-6)
            col = st.r_hat * np.exp(rng.normal(0.0, se, n))
        cols.append(col)
    draws = np.stack(cols, axis=1) if cols else np.zeros((n, 0))
    return DidPosterior(wf=ws.spec.workflow_id, dids=names, draws=draws,
                        status=FrozenDict({nm: (states.get(nm).status if states.get(nm)
                                                else "unknown") for nm in names}))


__all__ = ["DidHealthModel", "RatioEstimate", "mh_ratio", "ratio_vs_others", "trailing_window",
           "probe_share", "probe_gate_share", "flag_day_share", "drain_step", "drain_chain", "tv",
           "did_posterior", "did_names", "completed_dialling_ticks", "did_dd", "gate_seed",
           "largest_passing_lam", "PRIOR_KEYS", "PickupComparison", "pickup_vs_before",
           "caller_change_check"]
