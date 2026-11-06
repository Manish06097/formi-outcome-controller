"""IncidentManager: incident state machine, prompt fix selection, DID drain, verification.

Owner: track T4 (milestones M4 and M6). Implements DESIGN.md 7.1 (prompt safety incidents, fix
selection, post-apply verification, MTTR), 7.3 (DID drain and its verification) and section 17
override 1 (one suggest-fixes call per hallucination incident); ADRs 0008 and 0009.

States (`incident.state`):

    open --fix chosen--> fixing --fix live--> verifying --checks pass--> resolved
      |                    ^                     | checks fail: the fix is excluded, next candidate
      | no candidate       +---------------------+ (page INCIDENT_FIX_FAILED)
      v
    blocked (NO_SAFE_PROMPT or CLIENT_GATE_UNREACHABLE): nothing changes blindly; re-tried at
    every step, so a new cached verdict or credit top-up can unblock it

DID incidents: open (alarm) --> draining (a P0b chain step or one approval per stop until the DID
has no share) --> verifying (2 dialling hours after the last step) --> resolved | open again.

Prompt incidents open on: a FULL verdict of UNSAFE on a supervisor's new prompt, a cached UNSAFE
verdict, a flag CUSUM alarm confirmed by a pooled FULL verdict, live evidence alone (the flag
rate's lower 99% bound above 2 p0), or an UNSAFE D0 screen of a business default.

Fix selection (deterministic; the LLM never orders or picks): last known good first (SEQUENTIAL
verdict if none is cached), then the ranked catalog (FULL verdicts) in a rank fixed before any
result is seen, stopping at the first SAFE and approvable prompt. suggest-fixes is called once per
incident when the incident opens (3 credits from R) unless credits are LOW, and the skip is
recorded as a decision. The prompt patch is P0a, carrying the designated evidence run.

Everything learned stays in `Incident.evidence`, so the state machine survives a restart from the
`incident` table. Validation and suggest-fixes go through the injected clients only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Protocol

import numpy as np

from controller.incidents.credits import OrderedCreditBudget
from controller.incidents.safety import (Candidate, CreditsUnavailable, SafetyVerdictRunner,
                                         last_known_good, live_engagement_from_flags,
                                         rank_candidates)
from controller.learning.did import DidHealthModel, did_names, pickup_vs_before
from controller.settings import Thresholds
from controller.types import (P0_INCIDENT, P0A_PROMPT_SAFETY, DidState, Finding,
                              FlagMonitorState, FrozenDict, Incident, PatchRequest, Suggestion,
                              Verdict, WorkflowSnapshot, freeze, label, thaw)

OWNER = "incidents.manager"
OPEN_STATES = frozenset({"open", "fixing", "verifying", "blocked", "draining"})


class SuggestClient(Protocol):
    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]: ...


# =============================================================================== MTTR (7.1)


def predicted_mttr(edit_minute: int, stop_minute: int, latency_min: int, tick: int) -> int:
    """MTTR = D + L + V: D = seen at the next stop, L = approver latency, V = the decision is
    processed at ceil_to_tick(d) and visible after the advance ending one tick later, where the
    fast-apply step applies it. Returns minutes from the edit to the apply."""
    decide = stop_minute + latency_min
    processed = -(-decide // tick) * tick
    visible = processed + tick
    return visible - edit_minute


# =============================================================================== the manager


@dataclass
class IncidentManagerImpl:
    """The concrete `IncidentManager` (types.IncidentManager protocol)."""

    runner: SafetyVerdictRunner | None
    """None until the prompt-safety path is wired (milestone M6); DID and drift findings never
    touch it."""
    credits: OrderedCreditBudget | None
    did_model: DidHealthModel
    llm: SuggestClient | None = None
    llm_enabled: bool = True
    t: Thresholds = field(default_factory=Thresholds)
    engagement_of: Callable[[WorkflowSnapshot], Mapping[str, float]] | None = None
    """Live engagement per prompt (the effect model's pi[p], 5.3); default: from `r_flags`."""
    incidents: dict[str, Incident] = field(default_factory=dict)
    did_overrides: dict[tuple[str, str], DidState] = field(default_factory=dict)
    """(wf, did) -> state set by the fast path (suspect, degraded); the caller persists it."""
    decisions: list[dict] = field(default_factory=list)
    """Decision rows to write (the caller flushes them, before acting, 8.2)."""
    alerts: list[dict] = field(default_factory=list)
    flag_states: dict[str, FlagMonitorState] = field(default_factory=dict)
    """wf -> the latest flag CUSUM state (the orchestrator keeps it current); read by the
    engagement-drift response of 7.5."""

    # ------------------------------------------------------------------ helpers
    def open_incidents(self, wf: str, kind: str | None = None) -> list[Incident]:
        return [i for i in self.incidents.values() if i.wf == wf and i.state in OPEN_STATES
                and (kind is None or i.kind == kind)]

    def _decide(self, ws: WorkflowSnapshot, kind: str, rule: str, inputs: Mapping,
                outcome: Mapping) -> None:
        self.decisions.append({"wf": ws.spec.workflow_id, "module": OWNER, "kind": kind,
                               "rule_id": rule, "inputs": dict(inputs),
                               "outcome": dict(outcome), "sim_minute": ws.now})

    def _alert(self, ws: WorkflowSnapshot, code: str, message: str) -> None:
        self.alerts.append({"wf": ws.spec.workflow_id, "code": code, "message": message,
                            "sim_minute": ws.now})

    def _put(self, inc: Incident) -> Incident:
        self.incidents[inc.incident_id] = inc
        return inc

    def _ev(self, inc: Incident, **kw) -> Incident:
        ev = thaw(inc.evidence)
        ev.update(kw)
        return self._put(replace(inc, evidence=freeze(ev)))

    def states_for(self, ws: WorkflowSnapshot) -> WorkflowSnapshot:
        """The snapshot with our fast-path DID overrides applied."""
        merged = dict(ws.did_state)
        for (wf, did), st in self.did_overrides.items():
            if wf == ws.spec.workflow_id:
                merged[did] = st
        return replace(ws, did_state=FrozenDict(merged))

    # ------------------------------------------------------------------ entry point
    def on_findings(self, ws: WorkflowSnapshot, findings: list[Finding]) -> list[PatchRequest]:
        """Open, advance and fix incidents for this stop; returns P0 patches (P0a prompt fixes,
        P0b drain steps). Called every stop, with or without new findings, so open incidents keep
        moving (a drain chain emits one step per stop)."""
        out: list[PatchRequest] = []
        for f in sorted(findings, key=lambda x: (x.detector, x.kind, x.scope)):
            if f.detector in ("prompt", "flag", "screen"):
                self._on_prompt_finding(ws, f)
        self.on_detections(ws, findings)
        for inc in sorted(self.open_incidents(ws.spec.workflow_id, "prompt_safety"),
                          key=lambda i: i.incident_id):
            if inc.state in ("open", "blocked"):
                req = self._select_fix(ws, inc)
                if req is not None:
                    out.append(req)
        out.extend(self.drain_patches(ws))
        return out

    def on_detections(self, ws: WorkflowSnapshot, findings: list[Finding]) -> None:
        """The DID and KPI drift part of `on_findings` (7.3, 7.5): suspect, clear and alarm
        states, DID incidents, drift digest items. Creates no patch, so the loop can run it in
        DETECT and ask for the drain step in DECIDE (`drain_patches`)."""
        for f in sorted(findings, key=lambda x: (x.detector, x.kind, x.scope)):
            if f.detector == "did":
                self._on_did_finding(ws, f)
            elif f.detector == "drift" and f.kind.endswith("_down"):
                self._on_drift(ws, f)

    # ------------------------------------------------------------------ persistence
    def restore(self, incidents: Mapping[str, Incident]) -> None:
        """Rebuild the incident set from the `incident` table (on every start)."""
        self.incidents = dict(incidents)
        self.did_overrides = {}
        self.decisions, self.alerts = [], []

    # ------------------------------------------------------------------ KPI drift (7.5)
    def _on_drift(self, ws: WorkflowSnapshot, f: Finding) -> None:
        """A drift alarm is a digest item and never changes config by itself. Pickup drift with
        no DID comparator (a sole live DID) marks that DID suspect, so the next plan probes
        unknown DIDs (the no-healthy-DID rule of 5.9) and creates the comparator the fast and
        slow paths need. Engagement drift pays for a FULL verdict on the incumbent prompt only if
        the flag CUSUM is above h/2."""
        wf = ws.spec.workflow_id
        self._alert(ws, "KPI_DRIFT", f"{f.kind}: CUSUM {f.stat:.2f} >= {f.threshold:.2f}")
        self._decide(ws, "kpi_drift", "7.5", {"kind": f.kind, "evidence": thaw(f.evidence)},
                     {"digest": True})
        if f.kind == "pickup_down":
            # "no comparator" (7.5): one established DID carries the traffic. Probes at a 2%
            # share are no comparator for it (ADR 0030); a degraded DID still dialling is one
            # (the DID detector found it against the others, which explains the drift).
            def status(d: str) -> str:
                return (self.did_overrides.get((wf, d)) or ws.did_state.get(d)
                        or DidState(wf, d, "healthy")).status
            live = [d for d, w in ws.live.dist("default", "caller_id").items() if w > 0
                    and status(d) not in ("unknown", "probing")]
            if len(live) == 1 and status(live[0]) == "healthy":
                did = live[0]
                cur = self.did_overrides.get((wf, did)) or ws.did_state.get(did) or \
                    DidState(wf, did, "healthy")
                if cur.status == "healthy":
                    self.did_overrides[(wf, did)] = replace(
                        cur, status="suspect", since_minute=ws.now,
                        evidence=freeze({**thaw(cur.evidence), "suspect_by": "drift"}))
        elif f.kind == "engagement_down":
            st = self.flag_states.get(wf)
            if st is None or st.stat < self.t.flag_engage_drift_frac * st.h:
                return
            prompt = ws.live.prompt_version
            try:
                v = self.runner.run(wf, prompt, "full", False, purpose="verdict_incumbent",
                                    pool_with_prior=True)
            except CreditsUnavailable as e:
                self._alert(ws, "CREDITS_LOW", f"incumbent verdict on {prompt}: {e}")
                return
            if v.verdict == "UNSAFE":
                self._on_prompt_finding(ws, Finding(wf, "screen", "unsafe", prompt,
                                                    float(v.pooled_failures), 0.0, ws.now))

    # ------------------------------------------------------------------ prompt incidents
    def _baseline_p0(self, ws: WorkflowSnapshot) -> float:
        fs = ws.flags
        hist = fs.sim_hour < 0
        c = fs.connected[hist].sum() if np.any(hist) else fs.connected.sum()
        f = fs.flags[hist].sum() if np.any(hist) else fs.flags.sum()
        return float(f / c) if c > 0 else 0.01

    def _on_prompt_finding(self, ws: WorkflowSnapshot, f: Finding) -> None:
        wf = ws.spec.workflow_id
        prompt = f.scope if f.detector != "flag" else str(f.evidence.get("prompt", f.scope))
        if any(i.evidence.get("bad_prompt") == prompt for i in self.open_incidents(wf,
                                                                              "prompt_safety")):
            return
        if f.kind == "reset":
            return
        verdict: Verdict | None = None
        try:
            if f.detector == "prompt" and f.kind == "changed":
                verdict = self.runner.run(wf, prompt, "full", False, purpose="verdict_full")
            elif f.detector == "flag" and f.kind == "alarm":
                verdict = self.runner.run(wf, prompt, "full", False, purpose="verdict_cusum",
                                          pool_with_prior=True)
        except CreditsUnavailable as e:
            self._alert(ws, "CREDITS_LOW", f"verdict on {prompt} not possible: {e}")
            self._decide(ws, "prompt_verdict", "7.2/credits", {"prompt": prompt},
                         {"skipped": "credits unavailable"})
            return
        if verdict is not None:
            self._decide(ws, "prompt_verdict", f"7.1/{verdict.mode}",
                         {"prompt": prompt, "trigger": f"{f.detector}:{f.kind}"},
                         {"verdict": verdict.verdict, "runs": list(verdict.runs),
                          "pooled_n": verdict.pooled_n,
                          "pooled_failures": verdict.pooled_failures})
            if verdict.verdict == "SAFE":
                return
        elif f.kind not in ("unsafe_cached", "live_open", "unsafe"):
            return
        inc = Incident(incident_id=f"{wf}-prompt-{ws.now}-{prompt}", wf=wf, kind="prompt_safety",
                       state="open", opened_minute=ws.now, confirmed_minute=ws.now,
                       evidence=freeze({"bad_prompt": prompt, "trigger": f"{f.detector}:{f.kind}",
                                        "first_evidence_minute": f.first_evidence_minute,
                                        "p0": self._baseline_p0(ws), "tried": [],
                                        "verdict_runs": list(verdict.runs) if verdict else []}),
                       credits_spent=verdict.credits if verdict else 0.0)
        self._put(inc)
        self._decide(ws, "incident_open", "7.1", {"trigger": f"{f.detector}:{f.kind}",
                                                  "prompt": prompt},
                     {"incident_id": inc.incident_id})
        self._suggest_once(ws, inc)

    def on_screen_verdict(self, ws: WorkflowSnapshot, verdict: Verdict) -> None:
        """A D0 screen judged a business default UNSAFE: open the incident (no LKG exists, so the
        ranked catalog runs)."""
        if verdict.verdict == "UNSAFE":
            self._on_prompt_finding(ws, Finding(ws.spec.workflow_id, "screen", "unsafe",
                                                verdict.prompt, float(verdict.pooled_failures),
                                                0.0, ws.now))

    def _suggest_once(self, ws: WorkflowSnapshot, inc: Incident) -> None:
        """DESIGN.md 17.1: one suggest-fixes call per hallucination incident, 3 credits from R;
        skipped (and recorded) in LOW mode, when disabled, or when R and U cannot pay."""
        if inc.evidence.get("suggest") is not None:
            return
        wf = ws.spec.workflow_id
        reason = None
        if not self.llm_enabled or self.llm is None:
            reason = "disabled (CTL_LLM_SUGGEST=0)"
        elif self.credits.mode() == "low":
            reason = "credits LOW"
        elif self.credits.authorize_incident(self.runner.tenant_of(wf), "suggest_fixes",
                                             self.t.cost_suggest_fixes) is None:
            reason = "credits refused"
        if reason is not None:
            self._ev(inc, suggest={"called": False, "reason": reason})
            self._decide(ws, "suggest_fixes", "17.1/skip", {"incident_id": inc.incident_id},
                         {"called": False, "reason": reason})
            return
        problem = (f"Prompt {inc.evidence['bad_prompt']} on {wf} judged unsafe "
                   f"(hallucination); need a safer prompt version.")
        try:
            got = self.llm.suggest_fixes(wf, problem)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001 - the call is advisory; record and go on
            self._ev(inc, suggest={"called": True, "error": type(e).__name__})
            self._decide(ws, "suggest_fixes", "17.1/error", {"incident_id": inc.incident_id},
                         {"error": str(e)})
            return
        sugg = [{"prompt_version": s.prompt_version, "summary": s.summary} for s in got]
        inc = self.incidents[inc.incident_id]
        self._put(replace(inc, credits_spent=inc.credits_spent + self.t.cost_suggest_fixes))
        self._ev(self.incidents[inc.incident_id], suggest={"called": True, "suggestions": sugg})
        self._decide(ws, "suggest_fixes", "17.1/call", {"incident_id": inc.incident_id,
                                                         "problem": problem},
                     {"suggestions": sugg})

    def candidate_order(self, ws: WorkflowSnapshot, inc: Incident
                        ) -> tuple[list[tuple[str, str]], list[Candidate], list[dict]]:
        """The full, fixed order for this incident before any new validation: LKG prompts first
        (most recent first), then the ranked catalog. Excludes the bad prompt and every prompt
        already tried (a failed verification)."""
        wf = ws.spec.workflow_id
        verdicts = {p: v for (w, p), v in self.runner.verdicts.items() if w == wf}
        tried = set(inc.evidence.get("tried", ())) | {inc.evidence["bad_prompt"]}
        lkg = [(p, why) for p, why in last_known_good(ws, verdicts, self.t) if p not in tried]
        sugg = [Suggestion(s["prompt_version"], s.get("summary", ""))
                for s in (inc.evidence.get("suggest") or {}).get("suggestions", ())]
        live = dict(self.engagement_of(ws) if self.engagement_of else
                    live_engagement_from_flags(ws))
        ranked, dropped = rank_candidates(ws.spec, inc.evidence["bad_prompt"], verdicts, live,
                                          ws.policy.avoid_prompt_terms, sugg,
                                          exclude=sorted(tried | {p for p, _ in lkg}))
        return lkg, ranked, dropped

    def _select_fix(self, ws: WorkflowSnapshot, inc: Incident) -> PatchRequest | None:
        wf = ws.spec.workflow_id
        lkg, ranked, dropped = self.candidate_order(ws, inc)
        order = [(p, "lkg", "sequential", "verdict_lkg") for p, _ in lkg] + \
                [(c.prompt, c.tier, "full", "fix_candidate") for c in ranked]
        tried_log = []
        safe_not_approvable = False
        spent = 0.0
        for prompt, tier, mode, purpose in order:
            try:
                v = self.runner.run(wf, prompt, mode, True, purpose=purpose,  # type: ignore[arg-type]
                                    decision_key=inc.incident_id)
            except CreditsUnavailable as e:
                self._alert(ws, "CREDITS_LOW", f"{inc.incident_id}: {e}")
                inc = self._ev(inc, last_attempt={"minute": ws.now, "stopped": "credits",
                                                  "checked": tried_log})
                return None
            spent += v.credits
            tried_log.append({"prompt": prompt, "tier": tier, "verdict": v.verdict,
                              "mode": v.mode, "approvable": v.approvable,
                              "gate_run": v.gate_run, "runs": list(v.runs)})
            if v.verdict == "SAFE" and not v.approvable:
                safe_not_approvable = True
            if v.verdict == "SAFE" and v.approvable:
                return self._propose(ws, inc, prompt, tier, v, tried_log, dropped, spent)
        code = "CLIENT_GATE_UNREACHABLE" if safe_not_approvable else "NO_SAFE_PROMPT"
        if inc.state != "blocked":
            self._alert(ws, code, f"{inc.incident_id}: no safe, approvable prompt")
            self._decide(ws, "fix_selection", "7.1/none", {"incident_id": inc.incident_id,
                                                           "checked": tried_log},
                         {"blocked": code})
        self._put(replace(self._ev(inc, blocked=code, checked=tried_log,
                                   dropped_suggestions=dropped),
                          state="blocked",
                          credits_spent=inc.credits_spent + spent))
        return None

    def _propose(self, ws: WorkflowSnapshot, inc: Incident, prompt: str, tier: str, v: Verdict,
                 tried_log: list[dict], dropped: list[dict], spent: float) -> PatchRequest:
        wf = ws.spec.workflow_id
        bad = inc.evidence["bad_prompt"]
        runs_by_id = {r["run_id"]: r for r in self.runner.run_log}
        bad_runs = [runs_by_id[r] for r in inc.evidence.get("verdict_runs", ()) if r in runs_by_id]
        gate = runs_by_id.get(v.gate_run or "", {})
        bad_txt = "; ".join(f"{r['failures']} failures in {r['n']}" for r in bad_runs) or \
            "unsafe on live evidence"
        summary = (f"Change script from {bad} to {prompt} after the {label(inc.opened_minute)} "
                   f"incident: {bad} validation {bad_txt} (our limit "
                   f"{self.t.verdict_stage1_unsafe_f - 1} in {self.t.verdict_stage1_n}); "
                   f"{prompt}: {gate.get('failures', '?')} in {gate.get('n', '?')}"
                   f" ({'last known good' if tier == 'lkg' else 'ranked catalog candidate'}).")
        all_runs = sorted({r for t in tried_log for r in t["runs"]}
                          | set(inc.evidence.get("verdict_runs", ())))
        ev = {"incident_id": inc.incident_id, "validation_run_id": v.gate_run,
              "all_validation_runs": all_runs, "verdict": {"prompt": prompt, "mode": v.mode,
                                                           "pooled_n": v.pooled_n,
                                                           "pooled_failures": v.pooled_failures},
              "tier": tier, "checked": tried_log, "summary": summary,
              "dropped_suggestions": dropped, "harm_gate_exempt": "P0 prompt fix (0.1)"}
        self._decide(ws, "fix_selection", f"7.1/{tier}", {"incident_id": inc.incident_id,
                                                          "order_checked": tried_log},
                     {"prompt": prompt, "gate_run": v.gate_run})
        inc = self._ev(inc, fix_prompt=prompt, fix_tier=tier, checked=tried_log,
                       fix_proposed_minute=ws.now)
        self._put(replace(inc, state="fixing", credits_spent=inc.credits_spent + spent))
        return PatchRequest(wf=wf, owner=OWNER, path=("prompt",), value=prompt, cls=P0_INCIDENT,
                            p0_rank=P0A_PROMPT_SAFETY, evidence=freeze(ev))

    def on_fix_proposal(self, incident_id: str, proposal_id: str) -> None:
        """Governance submitted the fix (links `incident.fix_proposal_id`)."""
        inc = self.incidents[incident_id]
        self._put(replace(inc, fix_proposal_id=proposal_id))

    # ------------------------------------------------------------------ DID incidents
    def _on_did_finding(self, ws: WorkflowSnapshot, f: Finding) -> None:
        wf, did = ws.spec.workflow_id, f.scope
        cur = self.did_overrides.get((wf, did)) or ws.did_state.get(did) or \
            DidState(wf, did, "healthy")
        if f.kind == "suspect" and cur.status == "healthy":
            self.did_overrides[(wf, did)] = replace(cur, status="suspect", since_minute=ws.now,
                                                    evidence=freeze({**thaw(cur.evidence),
                                                                     "suspect_z": f.stat}))
        elif f.kind == "clear" and cur.status == "suspect":
            self.did_overrides[(wf, did)] = replace(cur, status="healthy", since_minute=ws.now)
        elif f.kind == "alarm":
            if any(i.evidence.get("did") == did for i in self.open_incidents(wf, "did_degraded")):
                return
            r = float(f.evidence["r"])
            se = float(f.evidence.get("se_log", math.inf))
            zc = 1.959964
            self.did_overrides[(wf, did)] = replace(
                cur, status="degraded", since_minute=ws.now, r_hat=r,
                r_lcb=r * math.exp(-zc * se) if math.isfinite(se) else 0.0,
                r_ucb=r * math.exp(zc * se) if math.isfinite(se) else None,
                evidence=freeze({**thaw(cur.evidence), "path": "fast",
                                 "alarm": thaw(f.evidence)}))
            share = max((float(ws.live.dist(k, "caller_id").get(did, 0.0))
                         for k in {"default", *ws.live.allocations}), default=0.0)
            inc = Incident(incident_id=f"{wf}-did-{ws.now}-{did}", wf=wf, kind="did_degraded",
                           state="draining", opened_minute=ws.now, confirmed_minute=ws.now,
                           evidence=freeze({"did": did, "share_at_alarm": share,
                                            "first_evidence_minute": f.first_evidence_minute,
                                            "alarm": thaw(f.evidence), "steps": []}))
            self._put(inc)
            self._alert(ws, "DID_DEGRADED",
                        f"{did}: pickup ratio {r:.2f} vs the other caller IDs (z {f.stat:.1f}, "
                        f"{f.evidence['window_h']}h window); draining its {share:.1%} share")
            self._decide(ws, "incident_open", "7.3/alarm", {"did": did, "z": f.stat,
                                                            "window_h": f.evidence["window_h"]},
                         {"incident_id": inc.incident_id, "share": share})

    def drain_patches(self, ws: WorkflowSnapshot) -> list[PatchRequest]:
        """The next P0b drain step of every draining DID incident of this workflow (7.3).
        Idempotent at a given minute (a restart may ask twice): a step is logged once."""
        wf = ws.spec.workflow_id
        draining = [i for i in self.open_incidents(wf, "did_degraded") if i.state == "draining"]
        if not draining:
            return []
        ws2 = self.states_for(ws)
        reqs = [r for r in self.did_model.plan_caller_ids(ws2) if r.cls == P0_INCIDENT]
        for inc in draining:
            did = inc.evidence["did"]
            left = max((float(ws.live.dist(k, "caller_id").get(did, 0.0))
                        for k in {"default", *ws.live.allocations}), default=0.0)
            if left <= 1e-9:
                self._put(replace(self._ev(inc, drained_minute=ws.now), state="verifying"))
                continue
            steps = list(inc.evidence.get("steps", ()))
            if not steps or steps[-1].get("minute") != ws.now:
                steps.append({"minute": ws.now, "share_before": left,
                              "planned": bool(reqs)})
                self._ev(inc, steps=steps)
        draining = [i for i in draining if self.incidents[i.incident_id].state == "draining"]
        if not draining:
            return []
        return [replace(r, evidence=freeze({**thaw(r.evidence),
                                            "incident_ids": sorted(i.incident_id
                                                                   for i in draining)}))
                for r in reqs]

    # ------------------------------------------------------------------ verification
    def verify(self, ws: WorkflowSnapshot) -> list[Finding]:
        out: list[Finding] = []
        for inc in sorted(self.open_incidents(ws.spec.workflow_id), key=lambda i: i.incident_id):
            if inc.kind == "prompt_safety":
                out.extend(self._verify_prompt(ws, inc))
            elif inc.kind == "did_degraded" and inc.state == "verifying":
                out.extend(self._verify_drain(ws, inc))
        return out

    def _verify_prompt(self, ws: WorkflowSnapshot, inc: Incident) -> list[Finding]:
        fix = inc.evidence.get("fix_prompt")
        if fix is None:
            return []
        if inc.state == "fixing":
            if ws.live.prompt_version != fix:
                return []
            inc = self._put(replace(self._ev(inc, fix_live_minute=ws.now), state="verifying"))
        if inc.state != "verifying":
            return []
        res = prompt_fix_check(ws, inc, self.t)
        if res is None:
            return []
        ok, ev = res
        wf = ws.spec.workflow_id
        if ok:
            self._put(replace(self._ev(inc, verification=ev), state="resolved",
                              resolved_minute=ws.now))
            kind = "verified"
        else:
            tried = list(inc.evidence.get("tried", ())) + [fix]
            self._put(replace(self._ev(inc, verification=ev, tried=tried, fix_prompt=None),
                              state="open"))
            self._alert(ws, "INCIDENT_FIX_FAILED", f"{inc.incident_id}: {fix} failed verification")
            kind = "verify_failed"
        self._decide(ws, "incident_verify", "7.1/verify", {"incident_id": inc.incident_id},
                     {"result": kind, **ev})
        return [Finding(wf, "incident", kind, inc.incident_id, float(ev.get("flag_ucb", 0.0)),
                        float(ev.get("flag_limit", 0.0)), ws.now, freeze(ev))]

    def _verify_drain(self, ws: WorkflowSnapshot, inc: Incident) -> list[Finding]:
        res = drain_check(ws, inc, self.t)
        if res is None:
            return []
        ok, ev = res
        wf = ws.spec.workflow_id
        if ok:
            self._put(replace(self._ev(inc, verification=ev), state="resolved",
                              resolved_minute=ws.now))
        else:
            self._put(replace(self._ev(inc, verification=ev), state="draining"))
            self._alert(ws, "DID_DRAIN_UNVERIFIED", f"{inc.incident_id}: {ev}")
        kind = "verified" if ok else "verify_failed"
        self._decide(ws, "incident_verify", "7.3/verify", {"incident_id": inc.incident_id},
                     {"result": kind, **ev})
        return [Finding(wf, "incident", kind, inc.incident_id, float(ev.get("ratio", 0.0)),
                        self.t.drain_verify_ratio, ws.now, freeze(ev))]


# =============================================================================== checks (pure)


def prompt_fix_check(ws: WorkflowSnapshot, inc: Incident, t: Thresholds
                     ) -> tuple[bool, dict] | None:
    """7.1 post-apply verification, from 300 connected calls under the fix: flag-rate UCB95
    below 2 p0, and engagement over the (up to) 24h after the fix vs the 72h before the incident,
    matched by hour of day, with LCB95 of the relative change >= -5%. None = not yet.

    Sequential reading (deviation, ADR 0009): at 300 calls a prompt AT the baseline flag rate
    has UCB95 above 2 p0 about half the time (4.4 expected flags give a bound of about 10/300),
    so a strict one-shot check would reopen good fixes. The check therefore passes when both
    bounds clear, fails as soon as either bound shows the fix is bad (flag LCB95 above 2 p0, or
    engagement UCB95 below -5%), and otherwise keeps collecting until the 24h window ends, then
    decides on the point estimates."""
    from controller.incidents.detectors import poisson_lower
    fs = ws.flags
    opts = tuple(ws.spec.options.get("prompt_version", ()))
    fix = inc.evidence["fix_prompt"]
    code = opts.index(fix) if fix in opts else -1
    start_h = int(inc.evidence.get("fix_live_minute", ws.now)) // 60 - 1
    after = (fs.prompt == code) & (fs.sim_hour >= start_h) & (fs.sim_hour < ws.now // 60) \
        & (fs.sim_hour < start_h + t.fix_verify_after_h)
    n_after = int(fs.connected[after].sum())
    if n_after < t.fix_verify_connected:
        return None
    flags = int(fs.flags[after].sum())
    p0 = float(inc.evidence.get("p0", 0.01))
    limit = t.fix_verify_flag_ratio * p0
    ucb = _poisson_upper(flags, 0.975) / n_after          # two-sided 95% bounds
    lcb = poisson_lower(flags, 0.975) / n_after
    flag_state = "ok" if ucb < limit else "bad" if lcb > limit else "open"
    open_h = inc.opened_minute // 60
    before = (fs.sim_hour < open_h) & (fs.sim_hour >= open_h - t.fix_verify_before_h)
    hod_a = fs.sim_hour[after] % 24
    c_a, e_a = fs.connected[after], fs.engaged[after]
    hod_b = fs.sim_hour[before] % 24
    c_b, e_b = fs.connected[before], fs.engaged[before]
    window_done = ws.now // 60 >= start_h + t.fix_verify_after_h
    ev: dict = {"connected_after": n_after, "flags_after": flags, "flag_ucb": ucb,
                "flag_lcb": lcb, "flag_limit": limit, "flag_state": flag_state,
                "window_done": window_done}
    eng_state = "ok"
    if c_b.sum() > 0 and c_a.sum() > 0:
        rate_b = {}
        for h in np.unique(hod_b).tolist():
            m = hod_b == h
            if c_b[m].sum() > 0:
                rate_b[h] = e_b[m].sum() / c_b[m].sum()
        pooled_b = e_b.sum() / c_b.sum()
        exp_b = sum(float(c) * rate_b.get(int(h), pooled_b)
                    for h, c in zip(hod_a.tolist(), c_a.tolist()))
        ea = float(e_a.sum()) / float(c_a.sum())
        eb = exp_b / float(c_a.sum())
        se = math.sqrt(max((1 - ea) / max(ea * c_a.sum(), 1e-9), 0)
                       + max((1 - eb) / max(eb * c_b.sum(), 1e-9), 0))
        log_ratio = math.log(max(ea, 1e-9) / max(eb, 1e-9))
        lcb_rel = math.exp(log_ratio - 1.959964 * se) - 1.0
        ucb_rel = math.exp(log_ratio + 1.959964 * se) - 1.0
        eng_state = ("ok" if lcb_rel >= t.fix_verify_engage_lcb
                     else "bad" if ucb_rel < t.fix_verify_engage_lcb else "open")
        ev.update({"engagement_after": ea, "engagement_before_matched": float(eb),
                   "rel_lcb95": lcb_rel, "rel_ucb95": ucb_rel})
    else:
        ev["engagement_note"] = "no pre-incident data"
    ev["engagement_state"] = eng_state
    ev["engagement_ok"] = eng_state == "ok"
    if "bad" in (flag_state, eng_state):
        return False, ev
    if flag_state == eng_state == "ok":
        return True, ev
    if not window_done:
        return None
    # the window is over and the evidence is still inconclusive: decide on the point estimates
    ok = flags / n_after < limit and (ev.get("engagement_after", 1.0)
                                      >= (1 + t.fix_verify_engage_lcb)
                                      * ev.get("engagement_before_matched", 0.0))
    ev["decided_on_point_estimates"] = True
    return ok, ev


def _poisson_upper(k: int, conf: float) -> float:
    """Upper bound lam with P(X <= k | lam) = 1 - conf."""
    from controller.incidents.detectors import poisson_cdf
    lo, hi = float(k), float(k) + 10.0
    while poisson_cdf(k, hi) > 1 - conf:
        hi *= 2
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if poisson_cdf(k, mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def drain_check(ws: WorkflowSnapshot, inc: Incident, t: Thresholds) -> tuple[bool, dict] | None:
    """7.3 verification, 2 dialling hours after the last step: the drained DID has no new dials
    (campaigns locked under `next_campaign_only` excepted), and workflow pickup over those hours
    is not credibly below 0.9 of the pre-incident 24h at the same hours of day. None = not yet."""
    da, tick = ws.did, ws.policy.tick
    names = did_names(ws)
    did = inc.evidence["did"]
    code = names.index(did) if did in names else -1
    t0 = int(inc.evidence.get("drained_minute", ws.now)) // tick
    after = (da.tick_no >= t0) & (da.tick_no < ws.now // tick) & (da.dials > 0)
    ticks = np.unique(da.tick_no[after])
    need = int(round(t.drain_verify_after_h * 60 / tick))
    if len(ticks) < need:
        return None
    sel = after & np.isin(da.tick_no, ticks[:need])     # exactly the first 2 dialling hours
    hours_done = need * tick / 60.0
    did_dials = int(da.dials[sel & (da.did == code)].sum())
    locked_ok = ws.spec.mid_flight == "next_campaign_only"
    cmp = pickup_vs_before(ws, sel, inc.opened_minute // tick)
    if cmp is None:
        return True, {"note": "no pre-incident data", "did_dials_after": did_dials}
    # the upper bound, not the point estimate: at 2 dialling hours the workflow's pickup moves
    # 7% to 10% by chance, and a good drain would page (ADR 0030)
    ok = cmp.ucb >= t.drain_verify_ratio and (did_dials == 0 or locked_ok)
    return ok, {"ratio": cmp.ratio, "ucb": cmp.ucb, "did_dials_after": did_dials,
                "hours": hours_done, "locked_campaigns_allowed": locked_ok}


__all__ = ["IncidentManagerImpl", "predicted_mttr", "prompt_fix_check", "drain_check",
           "SuggestClient", "OPEN_STATES"]
