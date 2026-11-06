"""Detectors: DidDetector (7.3), PromptTrigger and FlagCusum (7.1), DriftDetector (7.5).

Owner: track T4 (milestones M4 and M6). Implements DESIGN.md 7.1 (prompt triggers, D0 screens,
the cross-workflow flag check, the live flag CUSUM), 7.3 (multi-window Mantel-Haenszel DID
detector with a suspect window) and 7.5 (KPI drift Page CUSUM); ADRs 0008, 0009 and 0024.

Every detector thresholds a population statistic, never a single call (constraint 6). Each one
is pure: it reads a WorkflowSnapshot plus its persisted state and returns the new state and the
Findings; nothing here changes config. The incident manager turns findings into incidents.

- DidDetector, per live DID at every stop: MH ratio vs the pooled other live DIDs, stratified by
  (cohort, hour), over the trailing 1, 2, 4 and 8 dialling hours. Alarm in the 2, 4 or 8 h window
  when dials >= 100, z <= -4 and r <= 0.75; suspect in the 1 h window when dials >= 50 and
  z <= -2.5.
- FlagCusum: Bernoulli CUSUM on connected calls under the incumbent prompt, p1 = 2 p0,
  h = ln(30 days of calls), alarm needs >= 5 flags; plus "open on live evidence" when the lower
  99% bound of the live flag rate exceeds 2 p0. Hourly batches (`r_flags` is hourly).
- PromptTrigger: a supervisor `prompt_version` change to a prompt without a cached verdict asks
  for a FULL verdict; D0 screens of business defaults ordered by history flag rate, with the free
  cross-workflow flag check escalating outliers to FULL.
- DriftDetector: per KPI, O/E per closed dialling hour against the history baseline adjusted for
  hour, attempt bucket, cell mix and our own DID changes; downward Page CUSUM k 0.5, h 6;
  upward arm for the view; alert only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Sequence

import numpy as np

from controller.learning.did import did_names, ratio_vs_others, trailing_window
from controller.settings import Thresholds
from controller.stats.sequential import BernoulliCusum, DriftPageCusum, flag_cusum_h
from controller.types import (ConfigChange, DetectorState, DidState, DriftMonitorState, Finding,
                              FlagMonitorState, FrozenDict, Verdict, WorkflowSnapshot, freeze)

CONTROLLER_ACTORS = frozenset({"controller_approved", "controller_auto"})

# =============================================================================== small tails


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k), X ~ Poisson(lam), summed in log space."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    tot = term
    for i in range(1, k + 1):
        term *= lam / i
        tot += term
        if term < 1e-300 and i > lam:
            break
    if tot == 0.0:  # underflow at large lam: normal approximation with continuity correction
        return 0.5 * math.erfc(-((k + 0.5 - lam) / math.sqrt(lam)) / math.sqrt(2))
    return min(tot, 1.0)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k)."""
    return 1.0 - poisson_cdf(k - 1, lam)


def poisson_lower(k: int, conf: float) -> float:
    """One-sided lower `conf` bound of a Poisson mean from k events: the lam with
    P(X >= k | lam) = 1 - conf (bisection)."""
    if k <= 0:
        return 0.0
    lo, hi = 0.0, float(k)
    while poisson_sf(k, hi) < 1 - conf:
        hi *= 2
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if poisson_sf(k, mid) < 1 - conf:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# =============================================================================== DID detector (7.3)


@dataclass
class DidDetector:
    t: Thresholds = field(default_factory=Thresholds)

    def detect(self, ws: WorkflowSnapshot) -> list[Finding]:
        t, tick, da = self.t, ws.policy.tick, ws.did
        names = did_names(ws)
        if len(da) == 0:
            return []
        widest = trailing_window(da, ws.now, tick, max(t.did_fast_windows_h))
        live = sorted({int(x) for x in da.did[widest & (da.dials > 0)].tolist()})
        if len(live) < 2:   # no comparator: the drift detector covers it (7.5)
            return []
        masks = {h: trailing_window(da, ws.now, tick, h)
                 for h in (t.did_suspect_window_h, *t.did_fast_windows_h)}
        out: list[Finding] = []
        for code in live:
            name = names[code] if code < len(names) else str(code)
            st: DidState | None = ws.did_state.get(name)
            if st is not None and st.status in ("degraded", "unhealthy"):
                continue
            alarm = None
            for h in t.did_fast_windows_h:
                est = ratio_vs_others(da, masks[h], code)
                if est.dials >= t.did_fast_min_dials and est.z <= t.did_fast_z \
                        and est.r <= t.did_fast_r:
                    alarm = (h, est)
                    break
            if alarm is not None:
                h, est = alarm
                out.append(self._f(ws, name, "alarm", est.z, t.did_fast_z, h, est))
                continue
            est = ratio_vs_others(da, masks[t.did_suspect_window_h], code)
            if est.dials >= t.did_suspect_min_dials and est.z <= t.did_suspect_z:
                out.append(self._f(ws, name, "suspect", est.z, t.did_suspect_z,
                                   t.did_suspect_window_h, est))
            elif st is not None and st.status == "suspect":
                out.append(self._f(ws, name, "clear", est.z, t.did_suspect_z,
                                   t.did_suspect_window_h, est))
        return out

    def _f(self, ws: WorkflowSnapshot, name: str, kind: str, z: float, thr: float, h: float,
           est) -> Finding:
        return Finding(wf=ws.spec.workflow_id, detector="did", kind=kind, scope=name, stat=z,
                       threshold=thr, first_evidence_minute=int(ws.now - h * 60),
                       evidence=freeze({"window_h": h, "r": est.r, "se_log": est.se_log,
                                        "dials": est.dials, "connected": est.connected,
                                        "comp_dials": est.comp_dials,
                                        "comp_connected": est.comp_connected}))


# =============================================================================== flag CUSUM (7.1)


def prompt_code(ws: WorkflowSnapshot, prompt: str) -> int:
    opts = tuple(ws.spec.options.get("prompt_version", ()))
    return opts.index(prompt) if prompt in opts else -1


def closed_hours(now: int) -> int:
    """Sim hours h with (h + 1) x 60 <= now are closed."""
    return now // 60


@dataclass
class FlagCusum:
    """Live hallucination-flag CUSUM per workflow. `FlagMonitorState.last_event_id` holds the last
    processed sim hour: `r_flags` is hourly, so that is the unit of progress (contract note in
    the T4 report)."""

    t: Thresholds = field(default_factory=Thresholds)

    def history_rates(self, ws: WorkflowSnapshot) -> tuple[float, float, float]:
        """(pooled history flag rate, connected per history day, flag rate of the live prompt
        over all data before now) from `r_flags`."""
        fs = ws.flags
        hist = fs.sim_hour < 0
        conn, flg = fs.connected[hist].sum(), fs.flags[hist].sum()
        days = max(len(np.unique(fs.sim_hour[hist] // 24)), 1)
        p_hist = float(flg / conn) if conn > 0 else 0.0
        return p_hist, float(conn / days) if conn > 0 else 0.0, p_hist

    def fresh(self, ws: WorkflowSnapshot, prev: FlagMonitorState | None) -> FlagMonitorState:
        """A reset for the live prompt: p0 = that prompt's own history rate if it has one (at
        least 1,000 connected calls), else the pre-change rate (the previous p0, or the pooled
        history rate). Progress continues from the previous state's last hour, so calls made
        under the new prompt before the stop that saw the change are counted."""
        fs = ws.flags
        code = prompt_code(ws, ws.live.prompt_version)
        start = prev.last_event_id if prev is not None else closed_hours(ws.now) - 1
        before = fs.sim_hour <= start
        own = before & (fs.prompt == code)
        p_hist, per_day, _ = self.history_rates(ws)
        if fs.connected[own].sum() >= 1000:
            p0 = float(fs.flags[own].sum() / fs.connected[own].sum())
        elif prev is not None:
            p0 = prev.p0
        else:
            p0 = p_hist
        p0 = max(p0, 1e-4)
        if per_day <= 0:
            conn = fs.connected[before].sum()
            days = max(len(np.unique(fs.sim_hour[before] // 24)), 1)
            per_day = float(conn / days) if conn > 0 else 1000.0
        h = flag_cusum_h(self.t.flag_cusum_horizon_days, per_day)
        return FlagMonitorState(wf=ws.spec.workflow_id, prompt=ws.live.prompt_version, p0=p0,
                                p1=p0 * self.t.flag_cusum_ratio, h=h, stat=0.0,
                                flags_since_reset=0, calls=0,
                                last_event_id=start, alarm=False, reset_minute=ws.now)

    def update(self, ws: WorkflowSnapshot, state: FlagMonitorState | None
               ) -> tuple[FlagMonitorState, list[Finding]]:
        out: list[Finding] = []
        if state is None or state.prompt != ws.live.prompt_version:
            state = self.fresh(ws, state)
            out.append(self._f(ws, "reset", 0.0, state.h, state, {}))
        fs = ws.flags
        code = prompt_code(ws, state.prompt)
        hi = closed_hours(ws.now)
        sel = (fs.sim_hour > state.last_event_id) & (fs.sim_hour < hi) & (fs.prompt == code)
        cus = BernoulliCusum(state.p0, state.p1, state.h, self.t.flag_cusum_min_flags,
                             state.stat, state.flags_since_reset, state.calls, state.alarm)
        was_alarm = cus.alarm
        order = np.argsort(fs.sim_hour[sel], kind="stable")
        for n, k in zip(fs.connected[sel][order].tolist(), fs.flags[sel][order].tolist()):
            cus = cus.update_counts(n=int(n), k=int(k))
        new = replace(state, stat=cus.stat, flags_since_reset=cus.events_since_reset,
                      calls=cus.trials, alarm=cus.alarm, last_event_id=max(hi - 1,
                                                                            state.last_event_id))
        if cus.alarm and not was_alarm:
            out.append(self._f(ws, "alarm", cus.stat, cus.h, new, {}))
        lb = poisson_lower(new.flags_since_reset, self.t.flag_live_open_conf) / new.calls \
            if new.calls > 0 else 0.0
        if new.calls > 0 and lb > self.t.fix_verify_flag_ratio * new.p0:
            out.append(self._f(ws, "live_open", lb, self.t.fix_verify_flag_ratio * new.p0, new,
                               {"lower_bound": lb}))
        return new, out

    def _f(self, ws: WorkflowSnapshot, kind: str, stat: float, thr: float,
           st: FlagMonitorState, extra: Mapping) -> Finding:
        ev = {"prompt": st.prompt, "p0": st.p0, "p1": st.p1, "h": st.h, "stat": st.stat,
              "flags": st.flags_since_reset, "calls": st.calls, **extra}
        return Finding(wf=ws.spec.workflow_id, detector="flag", kind=kind, scope=st.prompt,
                       stat=stat, threshold=thr, first_evidence_minute=st.reset_minute,
                       evidence=freeze(ev))


# =============================================================================== prompt trigger (7.1)


@dataclass(frozen=True)
class ScreenInput:
    """One workflow's business-default prompt and its history flag counts (D0 screen)."""

    wf: str
    tenant: str
    prompt: str
    connected: int
    flags: int

    @property
    def rate(self) -> float:
        return self.flags / self.connected if self.connected > 0 else 0.0


@dataclass(frozen=True)
class ScreenRequest:
    wf: str
    tenant: str
    prompt: str
    mode: str
    """sequential | full"""
    reason: str
    escalated: bool


@dataclass
class PromptTrigger:
    t: Thresholds = field(default_factory=Thresholds)

    def on_config_changes(self, ws: WorkflowSnapshot, changes: Sequence[ConfigChange],
                          verdicts: Mapping[str, Verdict]) -> list[Finding]:
        """A prompt change by anyone but us, to a prompt without a cached verdict: FULL verdict
        at this step. A change to a prompt with a cached UNSAFE verdict opens the incident at once."""
        out: list[Finding] = []
        for ch in changes:
            if "prompt_version" not in ch.changed_dims or ch.actor in CONTROLLER_ACTORS:
                continue
            prompt = ch.config.prompt_version if ch.config is not None else ws.live.prompt_version
            cached = verdicts.get(prompt)
            if cached is None:
                kind = "changed"
            elif cached.verdict == "UNSAFE":
                kind = "unsafe_cached"
            else:
                continue
            out.append(Finding(wf=ws.spec.workflow_id, detector="prompt", kind=kind, scope=prompt,
                               stat=0.0, threshold=0.0, first_evidence_minute=ws.now,
                               evidence=freeze({"version": ch.version, "actor": ch.actor,
                                                "mode": "full"})))
        return out

    def plan_d0_screens(self, inputs: Sequence[ScreenInput], verdicts: Mapping[tuple[str, str],
                                                                               Verdict],
                        low_mode: bool) -> list[ScreenRequest]:
        """Screens of business defaults (SEQUENTIAL), ordered by history flag rate descending;
        the free cross-workflow check escalates outliers to FULL. LOW mode: only escalated ones.
        Prompts with a cached verdict are not screened again."""
        t = self.t
        ref = [i.rate for i in inputs if i.connected >= t.flag_check_min_connected]
        r_min = min(ref) if ref else None
        out: list[ScreenRequest] = []
        for i in sorted(inputs, key=lambda x: (-x.rate, x.wf)):
            if (i.wf, i.prompt) in verdicts:
                continue
            esc = False
            if r_min is not None and r_min > 0 and i.connected >= t.flag_check_min_connected:
                lam = i.connected * r_min
                z = (i.flags - lam) / math.sqrt(lam)
                esc = i.rate / r_min >= t.flag_check_ratio and z >= t.flag_check_z
            if low_mode and not esc:
                continue
            out.append(ScreenRequest(i.wf, i.tenant, i.prompt, "full" if esc else "sequential",
                                     "cross_workflow_flag_check" if esc else "d0_screen", esc))
        return out


# =============================================================================== KPI drift (7.5)


EngageExpect = Callable[[WorkflowSnapshot, int], float]
"""(ws, absolute sim hour) -> expected engaged calls in that block from the effect model (T3)."""


@dataclass
class DriftDetector:
    """Comparator-free KPI drift CUSUM per workflow and KPI. `DriftMonitorState.last_block` is
    the last processed absolute sim hour."""

    t: Thresholds = field(default_factory=Thresholds)
    engage_expect: EngageExpect | None = None

    # ------------------------------------------------------------------ baselines
    def _pickup_baseline(self, ws: WorkflowSnapshot) -> tuple[np.ndarray, str, int, int]:
        """p_hist[cell, hour, bucket] (shrunk to the pooled (hour, bucket) rate as in 5.8), the
        baseline kind, its dial count and the first block it may be applied to."""
        pk = ws.pickup
        hist = pk.sim_day < 0
        kind, first_block = "history", 0
        if not np.any(hist & (pk.dials > 0)):
            days = np.unique(pk.sim_day[pk.dials > 0])
            nd = self.t.drift_no_history_days
            if len(days) < nd:
                return np.zeros(0), "own", 0, 1 << 30
            hist = np.isin(pk.sim_day, days[:nd])
            kind, first_block = "own", int(days[nd - 1] + 1) * 24
        n_cells = int(max(pk.cell.max(initial=0) + 1, 1))
        shape = (n_cells, 24, 3)
        n = np.zeros(shape)
        y = np.zeros(shape)
        np.add.at(n, (pk.cell[hist], pk.hour[hist], pk.attempt_bucket[hist] - 1), pk.dials[hist])
        np.add.at(y, (pk.cell[hist], pk.hour[hist], pk.attempt_bucket[hist] - 1),
                  pk.connected[hist])
        hb_n, hb_y = n.sum(0), y.sum(0)
        overall = hb_y.sum() / hb_n.sum() if hb_n.sum() > 0 else 0.0
        pooled = np.where(hb_n > 0, hb_y / np.maximum(hb_n, 1), overall)
        m = self.t.hour_curve_prior
        p = (y + m * pooled[None]) / (n + m)
        return p, kind, int(hb_n.sum()), first_block

    def _did_factor(self, ws: WorkflowSnapshot, block: int) -> float:
        """DID factor of a block relative to the history DID mix: sum_d share_bd r_d over
        sum_d histshare_d r_d, with r_d from our DID states (1.0 where unknown)."""
        da = ws.did
        if len(da) == 0:
            return 1.0
        names = did_names(ws)
        tick = ws.policy.tick
        hour_of = (da.tick_no * tick) // 60
        r = np.ones(max(len(names), int(da.did.max(initial=0)) + 1))
        for i, nm in enumerate(names):
            st = ws.did_state.get(nm)
            if st is not None and st.r_hat is not None and st.status not in ("unknown",
                                                                             "probing"):
                r[i] = float(st.r_hat)
        def mix(mask: np.ndarray) -> float:
            d = np.bincount(da.did[mask], weights=da.dials[mask], minlength=len(r))
            s = d.sum()
            return float((d / s) @ r) if s > 0 else 1.0
        base = mix(da.tick_no < 0) if np.any(da.tick_no < 0) else 1.0
        cur = mix(hour_of == block)
        return cur / base if base > 0 else 1.0

    # ------------------------------------------------------------------ update
    def update(self, ws: WorkflowSnapshot, states: Mapping[str, DriftMonitorState]
               ) -> tuple[dict[str, DriftMonitorState], list[Finding]]:
        out_states = dict(states)
        findings: list[Finding] = []
        lo, hi = ws.spec.dialling_window
        last_closed = ws.now // 60 - 1
        p_hist, kind, base_dials, first_block = self._pickup_baseline(ws)
        st = states.get("pickup_rate") or DriftMonitorState(ws.spec.workflow_id, "pickup_rate",
                                                            0.0, -1 << 30, kind, False)
        if p_hist.size and base_dials >= self.t.drift_armed_dials:
            st, f = self._run_pickup(ws, st, p_hist, kind, first_block, last_closed, lo, hi)
            findings += f
        out_states["pickup_rate"] = st
        est = states.get("engagement_rate") or DriftMonitorState(
            ws.spec.workflow_id, "engagement_rate", 0.0, -1 << 30, "history", False)
        est, f = self._run_engage(ws, est, last_closed, lo, hi)
        findings += f
        out_states["engagement_rate"] = est
        return out_states, findings

    def _blocks(self, st: DriftMonitorState, first: int, last_closed: int, lo: int,
                hi: int) -> list[int]:
        start = max(st.last_block + 1, first, 0)
        return [b for b in range(start, last_closed + 1) if lo <= b % 24 < hi]

    def _cusum(self, st: DriftMonitorState) -> DriftPageCusum:
        return DriftPageCusum(self.t.drift_k, self.t.drift_h, st.cusum, st.up_cusum, st.alarm)

    def _run_pickup(self, ws, st, p_hist, kind, first_block, last_closed, lo, hi):
        pk = ws.pickup
        abs_hour = pk.sim_day.astype(np.int64) * 24 + pk.hour
        base_rate = float(np.average(p_hist.mean(axis=(0, 2)))) if p_hist.size else 0.25
        cus, f = self._cusum(st), []
        last = st.last_block
        for b in self._blocks(st, first_block, last_closed, lo, hi):
            m = abs_hour == b
            dials = pk.dials[m]
            if dials.sum() == 0:
                last = b
                continue
            cells = np.minimum(pk.cell[m], p_hist.shape[0] - 1)
            e = float(np.sum(dials * p_hist[cells, pk.hour[m], pk.attempt_bucket[m] - 1]))
            e *= self._did_factor(ws, b)
            o = float(pk.connected[m].sum())
            rbar = e / dials.sum() if dials.sum() > 0 else base_rate
            z = (o - e) / math.sqrt(max(e * (1 - rbar), 1e-9))
            cus, f = self._step(ws, cus, z, b, o, e, "pickup", f)
            last = b
        return (replace(st, cusum=cus.down, up_cusum=cus.up, alarm=cus.alarm, last_block=last,
                        baseline_kind=kind), f)

    def _run_engage(self, ws, st, last_closed, lo, hi):
        fs = ws.flags
        hist = fs.sim_hour < 0
        kind, first = "history", 0
        if fs.connected[hist].sum() < self.t.drift_armed_connects:
            days = np.unique(fs.sim_hour[fs.connected > 0] // 24)
            nd = self.t.drift_no_history_days
            if np.any(hist) or len(days) < nd:
                return st, []
            hist = np.isin(fs.sim_hour // 24, days[:nd])
            kind, first = "own", int(days[nd - 1] + 1) * 24
            if fs.connected[hist].sum() < self.t.drift_armed_connects:
                return st, []
        e_all = fs.engaged[hist].sum() / fs.connected[hist].sum()
        rates: dict[int, float] = {}
        for p in np.unique(fs.prompt[hist]).tolist():
            mm = hist & (fs.prompt == p)
            if fs.connected[mm].sum() >= self.t.drift_armed_connects:
                rates[int(p)] = float(fs.engaged[mm].sum() / fs.connected[mm].sum())
        cus, f = self._cusum(st), []
        last = st.last_block
        for b in self._blocks(st, first, last_closed, lo, hi):
            m = fs.sim_hour == b
            conn = fs.connected[m]
            if conn.sum() == 0:
                last = b
                continue
            if self.engage_expect is not None:
                e = float(self.engage_expect(ws, b))
            else:
                e = float(sum(n * rates.get(int(p), e_all)
                              for p, n in zip(fs.prompt[m].tolist(), conn.tolist())))
            o = float(fs.engaged[m].sum())
            rbar = e / conn.sum()
            z = (o - e) / math.sqrt(max(e * (1 - rbar), 1e-9))
            cus, f = self._step(ws, cus, z, b, o, e, "engagement", f)
            last = b
        return (replace(st, cusum=cus.down, up_cusum=cus.up, alarm=cus.alarm, last_block=last,
                        baseline_kind=kind), f)

    def _step(self, ws, cus: DriftPageCusum, z: float, b: int, o: float, e: float, kpi: str,
              f: list[Finding]) -> tuple[DriftPageCusum, list[Finding]]:
        up_before = cus.up >= cus.h
        cus = cus.update(z)
        ev = {"block_hour": b, "observed": o, "expected": e, "z": z, "down": cus.down,
              "up": cus.up}
        if cus.alarm:
            f.append(Finding(wf=ws.spec.workflow_id, detector="drift", kind=f"{kpi}_down",
                             scope="workflow", stat=cus.down, threshold=cus.h,
                             first_evidence_minute=b * 60, evidence=freeze(ev)))
            cus = cus.restart()
        if cus.up >= cus.h and not up_before:
            f.append(Finding(wf=ws.spec.workflow_id, detector="drift", kind=f"{kpi}_up",
                             scope="workflow", stat=cus.up, threshold=cus.h,
                             first_evidence_minute=b * 60, evidence=freeze({**ev,
                                                                            "view_only": True})))
        return cus, f


# =============================================================================== one entry point


@dataclass
class Detectors:
    """`detect(ws, state)` of 11.2 plus the state it advances. Prompt triggers need config
    events, which the snapshot does not carry: call `PromptTrigger.on_config_changes` with the
    ingest's ConfigChanges."""

    t: Thresholds = field(default_factory=Thresholds)
    engage_expect: EngageExpect | None = None

    def advance(self, ws: WorkflowSnapshot, state: DetectorState
                ) -> tuple[DetectorState, list[Finding]]:
        findings = DidDetector(self.t).detect(ws)
        flag, f = FlagCusum(self.t).update(ws, state.flag)
        findings += f
        drift, f = DriftDetector(self.t, self.engage_expect).update(ws, state.drift)
        findings += f
        return replace(state, flag=flag, drift=FrozenDict(drift), last_minute=ws.now), findings


def detect(ws: WorkflowSnapshot, state: DetectorState, t: Thresholds | None = None
           ) -> list[Finding]:
    """DID, flag and drift findings at this stop (11.2); see `Detectors.advance` for the state."""
    return Detectors(t or Thresholds()).advance(ws, state)[1]


__all__ = ["DidDetector", "FlagCusum", "PromptTrigger", "DriftDetector", "Detectors", "detect",
           "ScreenInput", "ScreenRequest", "poisson_cdf", "poisson_sf", "poisson_lower"]
