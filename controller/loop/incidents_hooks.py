"""Prompt safety and credits in the real loop: the hooks that judge prompts, open hallucination
incidents, choose and propose the fix, and verify it, with every credit-spending call going
through the write-ahead intent executor.

Owner: integration step I5 (milestone M6). Implements DESIGN.md 7.1 (D0 prompt screens, the prompt
trigger on supervisor edits, the live flag CUSUM, two-stage verdicts with the designated
gate-evidence run, ranked fix selection, post-apply verification), 7.2 (ordered credit pools,
LOW mode) and section 17 override 1 (one suggest-fixes call per incident); closes AUDIT-1
finding 4. ADR 0032.

The statistics and the state machine are T4's (`incidents/safety.py`, `incidents/credits.py`,
`incidents/detectors.py`, `incidents/manager.py`); this module feeds them snapshots, gives them
two clients that go through `IntentExecutor`, and writes what they decide.

The money path (AUDIT-1 finding 4). A validation or suggest-fixes call is one intent with
`credits_before` and `cost`. The result transaction (`done`) commits the `validation_run` row, the
`credit_ledger` debit, the `credit_envelope` spend and the evidence designation together; the
`lost` hook (the call ran, its result is gone) commits the debit alone. After a crash the same
key returns the stored response, so the retry never spends again; a lost validation is paid once
more under a second key (at most one extra run), a lost suggest-fixes call is not repeated.

Per step (called by `loop/governance.py: GovernanceHooks`):
  REFRESH   `on_config_changes`: a supervisor prompt change is recorded in `prompt_trigger`
            (REFRESH is one transaction and cannot make the validation HTTP call)
  DETECT    `detect`: the flag CUSUM, then the findings (a prompt change, a CUSUM alarm, live
            evidence) judged by the manager, outside any long transaction
  DECIDE    `decide`: the P0a fix request of every open or blocked prompt incident, every stop
  VERIFY    `verify`: post-apply verification of a live fix; links the incident to its proposal
  start     `bootstrap`: the credit pools, then the D0 screens of the business defaults
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from controller.audit.writer import record, write_decision
from controller.incidents.credits import OrderedCreditBudget, plan_pools
from controller.incidents.detectors import FlagCusum, PromptTrigger, ScreenInput
from controller.incidents.safety import (CreditsUnavailable, SafetyVerdictRunner, gate_plan)
from controller.loop.pickup import json_safe
from controller.obs.alerts import raise_alert
from controller.simclient.errors import ApiError
from controller.store import repos
from controller.types import (CreditEnvelope, FlagMonitorState, Finding, PatchRequest,
                              Suggestion, Tx, ValidationRun, Verdict, WorkflowSnapshot, freeze)

log = logging.getLogger(__name__)

OWNER = "incidents.manager"
VALIDATION_KIND = "validation"
SUGGEST_KIND = "suggest_fixes"


# =============================================================================== intent clients


class _IntentClient:
    """Shared plumbing of the two credit-spending clients."""

    def __init__(self, hooks: "PromptSafety"):
        self.h = hooks

    def _run(self, kind: str, key: str, payload: dict[str, Any], wf: str, minute: int
             ) -> tuple[Mapping[str, Any], bool]:
        """Execute under the write-ahead protocol. Returns (response, replayed): `replayed` is
        True when the intent had already completed (a restarted step re-asking), so no call was
        made and the in-memory budget must not count the spend twice."""
        rt = self.h.rt
        with rt.db.tx() as tx:
            prior = repos.intent_by_key(tx, key)
        replayed = prior is not None and prior.status == "done"
        d = rt.decision(wf=wf, module="incidents.credits", kind=kind, rule_id=f"7.2/{kind}",
                        inputs={"key": key, "purpose": payload.get("purpose"),
                                "cost": payload.get("cost"),
                                "credits_before": payload.get("credits_before")},
                        sim_minute=minute)
        resp = rt.intents.execute(kind, key, payload, d)
        return resp, replayed


class IntentValidationClient(_IntentClient):
    """`ValidationClient` for `SafetyVerdictRunner`: POST /validation/runs as an intent."""

    def validate_with(self, wf: str, prompt: str, n: int, meta: Mapping[str, Any]
                      ) -> ValidationRun:
        h, rt = self.h, self.h.rt
        minute = h.gov.now
        base = (f"val:{wf}:{prompt}:{meta['purpose']}:{meta['stage']}:{minute}:"
                f"{meta.get('decision_key') or '-'}")
        attempt = 1
        while True:
            key = base if attempt == 1 else f"{base}:r{attempt}"
            if attempt > 1 and not h.budget.authorize(
                    meta["tenant"], meta["pool"], meta["purpose"], meta["cost"]):
                raise CreditsUnavailable(f"{meta['purpose']}: no credit left to repeat a lost "
                                         f"validation of {wf}/{prompt}")
            payload = {"workflow_id": wf, "prompt_version": prompt, "n": n,
                       "credits_before": rt.client.state().credits_remaining,
                       "cost": meta["cost"], "purpose": meta["purpose"], "pool": meta["pool"],
                       "tenant": meta["tenant"], "stage": meta["stage"],
                       "decision_key": meta.get("decision_key"),
                       "designated": bool(meta.get("designated")), "sim_minute": minute}
            resp, replayed = self._run(VALIDATION_KIND, key, payload, wf, minute)
            if replayed:
                h.budget.refund(meta["tenant"], meta["pool"], meta["cost"])
            if resp.get("lost"):
                if attempt >= 2:
                    raise CreditsUnavailable(f"validation of {wf}/{prompt} lost twice")
                attempt += 1
                continue
            return ValidationRun.from_api(resp)


class IntentSuggestClient(_IntentClient):
    """The LLM client of the manager: POST /llm/suggest-fixes as an intent, once per incident."""

    def suggest_with(self, wf: str, problem: str, meta: Mapping[str, Any]) -> list[Suggestion]:
        h, rt = self.h, self.h.rt
        minute = h.gov.now
        key = f"sug:{meta['incident_id']}"
        payload = {"workflow_id": wf, "problem": problem,
                   "credits_before": rt.client.state().credits_remaining,
                   "cost": meta["cost"], "purpose": "suggest_fixes", "pool": meta["pool"],
                   "tenant": meta["tenant"], "incident_id": meta["incident_id"],
                   "sim_minute": minute}
        resp, replayed = self._run(SUGGEST_KIND, key, payload, wf, minute)
        if replayed:
            h.budget.refund(meta["tenant"], meta["pool"], meta["cost"])
        if resp.get("lost"):
            return []           # the credits are spent and the answer is gone: not repeated
        return [Suggestion.from_api(s) for s in resp.get("suggestions") or []]

    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]:   # pragma: no cover
        raise RuntimeError("use suggest_with: the call needs its pool and incident")


# =============================================================================== the hooks


class PromptSafety:
    """Prompt-safety hooks for one run, owned by `GovernanceHooks` (`gov`)."""

    def __init__(self, gov: Any):
        self.gov = gov
        self.rt = gov.rt
        self.t = gov.t
        self.mgr = gov.pickup.mgr
        self.flag = FlagCusum(self.t)
        self.trigger = PromptTrigger(self.t)
        self.budget: OrderedCreditBudget
        self.runner: SafetyVerdictRunner
        self.flag_state: dict[str, FlagMonitorState] = {}
        self._written_verdicts: dict[tuple[str, str], Verdict] = {}

    # ------------------------------------------------------------------ wiring (every start)
    def attach(self) -> None:
        rt, t = self.rt, self.t
        specs = rt.catalog.specs
        self.tenant = {s.workflow_id: s.tenant_id for s in specs}
        self._ensure_envelope()
        with rt.db.tx() as tx:
            rows = tx.execute("SELECT pool, tenant, allotted, spent, mode FROM credit_envelope "
                              "ORDER BY pool, tenant").fetchall()
        plan = self._plan()
        self.budget = OrderedCreditBudget.from_envelopes(
            plan, [CreditEnvelope(r[0], r[1], float(r[2]), float(r[3]), r[4]) for r in rows], t)
        self.budget.sync(rt.state.credits_remaining)
        self.runner = SafetyVerdictRunner(
            client=IntentValidationClient(self), credits=self.budget,   # type: ignore[arg-type]
            gate_of=lambda wf: self.gov.registry.policy(wf).min_validation_pass_rate,
            tenant_of=lambda wf: self.tenant[wf], t=t)
        self._restore_runner()
        m = self.mgr
        m.runner, m.credits, m.t = self.runner, self.budget, t
        m.llm = IntentSuggestClient(self)
        m.llm_enabled = bool(rt.settings.env.llm_suggest)
        reg = rt.registry
        reg.attach(VALIDATION_KIND, done=self._validation_done, lost=self._lost)
        reg.attach(SUGGEST_KIND, done=self._suggest_done, lost=self._lost)

    def _plan(self):
        specs = self.rt.catalog.specs
        k = max((len(s.options.get("prompt_version", ())) for s in specs), default=1)
        tight = any(gate_plan(self.gov.registry.policy(s.workflow_id)
                              .min_validation_pass_rate, self.t).tight for s in specs)
        by_tenant: dict[str, int] = {}
        for s in specs:
            by_tenant[s.tenant_id] = by_tenant.get(s.tenant_id, 0) + 1
        return plan_pools(self._c0(), k, tight, by_tenant, self.t,
                          llm_suggest=bool(self.rt.settings.env.llm_suggest))

    def _c0(self) -> float:
        """Credits at the start of the run: the balance plus everything we have spent."""
        with self.rt.db.tx() as tx:
            spent = float(tx.execute("SELECT coalesce(sum(cost), 0) FROM credit_ledger"
                                     ).fetchone()[0])
        return float(self.rt.state.credits_remaining) + spent

    def _ensure_envelope(self) -> None:
        """Fill the ordered pools once (7.2); a restart finds them and changes nothing."""
        with self.rt.db.tx() as tx:
            if tx.execute("SELECT 1 FROM credit_envelope LIMIT 1").fetchone():
                return
            plan = self._plan()
            b = OrderedCreditBudget(plan, self.t)
            for e in b.envelopes():
                tx.execute("INSERT INTO credit_envelope (pool, tenant, allotted, spent, mode) "
                           "VALUES (%s, %s, %s, 0, %s) ON CONFLICT DO NOTHING",
                           (e.pool, e.tenant, e.allotted, e.mode))
            write_decision(tx, self.rt.decision(
                wf=None, module="incidents.credits", kind="credit_plan", rule_id="7.2/pools",
                inputs={"c0": plan.c0, "k": plan.k, "tight": plan.tight, "w": plan.w},
                outcome={"R": plan.r, "S": plan.s, "U": plan.u,
                         "S_by_tenant": dict(plan.s_by_tenant), "start_mode": plan.start_mode},
                sim_minute=repos.get_clock(tx).sim_minute))

    def _restore_runner(self) -> None:
        rt = self.rt
        catalog = rt.catalog
        with rt.db.tx() as tx:
            vrows = tx.execute("""SELECT wf, prompt, verdict, mode, runs, pooled_n,
                                         pooled_failures, gate_run FROM prompt_verdict""")
            verdicts = []
            for wf, pr, v, mode, runs, pn, pf, gr in vrows.fetchall():
                w = self._wf_name(wf)
                verdicts.append(Verdict(w, self._prompt_name(w, pr), v, mode, tuple(runs), pn,
                                        pf, gr, False, 0.0))
            rrows = tx.execute("""SELECT run_id, wf, prompt, n, failures, pass_rate, purpose,
                                         stage, designated, decision_key, pool, cost
                                  FROM validation_run ORDER BY sim_minute, run_id""").fetchall()
        runs: list[ValidationRun] = []
        for rid, wf, pr, n, f, pass_rate, purpose, stage, des, dkey, pool, cost in rrows:
            w = self._wf_name(wf)
            p = self._prompt_name(w, pr)
            runs.append(ValidationRun(rid, w, p, n, f, float(pass_rate), "", None))
            self.runner.run_log.append({"run_id": rid, "wf": w, "prompt": p, "n": n,
                                        "failures": f, "pass_rate": float(pass_rate),
                                        "purpose": purpose, "pool": pool, "stage": stage,
                                        "cost": cost, "decision_key": dkey})
            if des and dkey:
                self.runner.designated[(dkey, w, p)] = rid
        self.runner.restore(verdicts, runs)
        self._written_verdicts = {(v.wf, v.prompt): v for v in verdicts}
        with rt.db.tx() as tx:
            for r in tx.execute("""SELECT wf, prompt, p0, p1, h, stat, flags_since_reset, calls,
                                          last_sim_hour, alarm, reset_minute
                                   FROM flag_monitor""").fetchall():
                w = self._wf_name(r[0])
                self.flag_state[w] = FlagMonitorState(
                    w, self._prompt_name(w, r[1]), r[2], r[3], r[4], r[5], r[6], r[7],
                    int(r[8]), bool(r[9]), r[10] or 0)
        self.mgr.flag_states.update(self.flag_state)

    def _wf_name(self, code: int) -> str:
        for s in self.rt.catalog.specs:
            if self.rt.catalog.code(s.workflow_id) == code:
                return s.workflow_id
        raise KeyError(code)

    def _prompt_name(self, wf: str, code: int) -> str:
        return self.rt.catalog.spec(wf).options["prompt_version"][code]

    def _prompt_code(self, wf: str, prompt: str) -> int:
        return list(self.rt.catalog.spec(wf).options["prompt_version"]).index(prompt)

    # ------------------------------------------------------------------ the money (T2 hooks)
    def _ledger(self, tx: Tx, intent: Any, remaining: float | None) -> None:
        """Debit the pool: ledger row and envelope spend, once per intent."""
        req = intent.request
        if tx.execute("SELECT 1 FROM credit_ledger WHERE intent_id = %s",
                      (intent.intent_id,)).fetchone():
            return
        wf = req["workflow_id"]
        cost = float(req["cost"])
        pool = req["pool"]
        after = remaining if remaining is not None else float(req["credits_before"]) - cost
        tx.execute("""INSERT INTO credit_ledger (sim_minute, tenant, wf, pool, purpose, cost,
                                                 remaining_after, intent_id)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                   (req.get("sim_minute") or 0, req["tenant"], self.rt.catalog.code(wf), pool,
                    req["purpose"], cost, after, intent.intent_id))
        tenant = "*" if pool in ("R", "U") else req["tenant"]
        tx.execute("UPDATE credit_envelope SET spent = spent + %s WHERE pool = %s AND tenant = %s",
                   (cost, pool, tenant))
        tx.execute("UPDATE credit_envelope SET mode = %s", (self.budget.mode(),))

    def _validation_done(self, tx: Tx, intent: Any, resp: Mapping[str, Any]) -> None:
        req = intent.request
        run = ValidationRun.from_api(resp)
        wf = req["workflow_id"]
        tx.execute("""INSERT INTO validation_run (run_id, wf, prompt, n, failures, pass_rate,
                                                  purpose, stage, incident_id, intent_id,
                                                  sim_minute, designated, decision_key, pool,
                                                  cost)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (run_id) DO NOTHING""",
                   (run.validation_run_id, self.rt.catalog.code(wf),
                    self._prompt_code(wf, run.prompt_version), run.n, run.failures,
                    run.pass_rate, req["purpose"], req["stage"], req.get("decision_key"),
                    intent.intent_id, req.get("sim_minute"), bool(req.get("designated")),
                    req.get("decision_key"), req["pool"], float(req["cost"])))
        self._ledger(tx, intent, run.credits_remaining)

    def _suggest_done(self, tx: Tx, intent: Any, resp: Mapping[str, Any]) -> None:
        self._ledger(tx, intent, None)

    def _lost(self, tx: Tx, intent: Any, resp: Mapping[str, Any]) -> None:
        """The call ran and its result is gone: the credits are spent (7.1, 8.3)."""
        self._ledger(tx, intent, resp.get("credits_remaining"))
        wf = intent.request.get("workflow_id")
        raise_alert(tx, "LOST_VALIDATION_RUN", self.rt.catalog.code(wf) if wf else None,
                    intent.idem_key, f"{intent.kind} {intent.idem_key}: the call took effect "
                    f"and its result was lost; its credits are in the ledger",
                    sim_minute=intent.request.get("sim_minute"))
        record(tx, wf=wf, actor="reconciler", action="lost_validation_run",
               subject=("intent", intent.idem_key), summary=f"{intent.kind} result lost",
               details={"cost": intent.request.get("cost")}, caused_by=intent.decision_id)

    # ------------------------------------------------------------------ start: pools and screens
    def bootstrap(self) -> int:
        """D0 screens (7.1): SEQUENTIAL verdicts of the business-default prompts from the screen
        pool S, workflows ordered by history flag rate; LOW mode screens only what the free
        cross-workflow check escalates. An UNSAFE default opens an incident at once."""
        gov = self.gov
        inputs: list[ScreenInput] = []
        snaps: dict[str, WorkflowSnapshot] = {}
        for wf in gov.registry.workflow_ids():
            with gov.scope.tx() as tx:
                ws = gov._snapshot(tx, wf)
            snaps[wf] = ws
            code = self._prompt_code(wf, ws.live.prompt_version)
            fs = ws.flags
            m = (fs.sim_hour < 0) & (fs.prompt == code)
            inputs.append(ScreenInput(wf, self.tenant[wf], ws.live.prompt_version,
                                      int(fs.connected[m].sum()), int(fs.flags[m].sum())))
        reqs = self.trigger.plan_d0_screens(inputs, self.runner.verdicts,
                                            self.budget.mode() == "low")
        n = 0
        for r in reqs:
            ws = snaps[r.wf]
            before = self._mgr_incidents(r.wf)
            try:
                v = self.runner.run(r.wf, r.prompt, r.mode, False,   # type: ignore[arg-type]
                                    purpose="d0_screen", pool="S")
            except CreditsUnavailable as e:
                with gov.scope.tx() as tx:
                    write_decision(tx, gov._decision(
                        r.wf, OWNER, "prompt_verdict", "7.2/screen_skipped",
                        {"prompt": r.prompt, "reason": r.reason}, {"skipped": str(e)}))
                continue
            n += 1
            self.mgr.decisions.append({
                "wf": r.wf, "module": OWNER, "kind": "prompt_verdict",
                "rule_id": f"7.1/{v.mode}", "sim_minute": ws.now,
                "inputs": {"prompt": r.prompt, "trigger": r.reason,
                           "history_rate": next(i.rate for i in inputs if i.wf == r.wf)},
                "outcome": {"verdict": v.verdict, "runs": list(v.runs),
                            "pooled_n": v.pooled_n, "pooled_failures": v.pooled_failures}})
            self.mgr.on_screen_verdict(ws, v)
            self._persist(r.wf, before)
        return n

    # ------------------------------------------------------------------ REFRESH
    def on_config_changes(self, tx: Tx, changes: list[Any]) -> None:
        """A prompt change by anyone but us is judged at the next DETECT."""
        for ch in changes:
            if "prompt_version" not in ch.changed_dims or ch.config is None \
                    or not ch.is_supervisor:
                continue
            wf = ch.workflow_id
            tx.execute("""INSERT INTO prompt_trigger (wf, version, prompt, actor, sim_minute)
                          VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                       (self.rt.catalog.code(wf), ch.version,
                        self._prompt_code(wf, ch.config.prompt_version), ch.actor,
                        self.gov.now))

    # ------------------------------------------------------------------ DETECT
    def detect(self) -> None:
        gov = self.gov
        for wf in gov.registry.workflow_ids():
            with gov.scope.tx() as tx:
                ws = gov._snapshot(tx, wf)
                pending = tx.execute("""SELECT version, prompt, actor FROM prompt_trigger
                                        WHERE wf = %s AND NOT handled ORDER BY version""",
                                     (self.rt.catalog.code(wf),)).fetchall()
            findings: list[Finding] = []
            for version, pcode, actor in pending:
                prompt = self._prompt_name(wf, pcode)
                cached = self.runner.verdict_for(wf, prompt)
                if cached is not None and cached.verdict == "SAFE":
                    continue
                findings.append(Finding(
                    wf, "prompt", "changed" if cached is None else "unsafe_cached", prompt, 0.0,
                    0.0, ws.now, freeze({"version": version, "actor": actor, "mode": "full"})))
            state, flag_findings = self.flag.update(ws, self.flag_state.get(wf))
            self.flag_state[wf] = state
            self.mgr.flag_states[wf] = state
            findings += flag_findings
            before = self._mgr_incidents(wf)
            self.mgr.on_prompt_findings(ws, findings)       # the validation calls happen here
            with gov.scope.tx() as tx:
                self._write_flag(tx, wf, state)
                tx.execute("UPDATE prompt_trigger SET handled = true WHERE wf = %s AND NOT handled",
                           (self.rt.catalog.code(wf),))
            self._persist(wf, before)

    def _write_flag(self, tx: Tx, wf: str, s: FlagMonitorState) -> None:
        tx.execute("""INSERT INTO flag_monitor (wf, prompt, p0, p1, h, stat, flags_since_reset,
                                                calls, last_sim_hour, alarm, reset_minute)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                      ON CONFLICT (wf) DO UPDATE SET prompt = excluded.prompt, p0 = excluded.p0,
                          p1 = excluded.p1, h = excluded.h, stat = excluded.stat,
                          flags_since_reset = excluded.flags_since_reset,
                          calls = excluded.calls, last_sim_hour = excluded.last_sim_hour,
                          alarm = excluded.alarm, reset_minute = excluded.reset_minute""",
                   (self.rt.catalog.code(wf), self._prompt_code(wf, s.prompt), s.p0, s.p1, s.h,
                    s.stat, s.flags_since_reset, s.calls, s.last_sim_hour, s.alarm,
                    s.reset_minute))

    # ------------------------------------------------------------------ DECIDE
    def decide(self) -> int:
        """Every stop: the fix request of each open or blocked prompt incident (P0a)."""
        gov = self.gov
        n = 0
        for wf in gov.registry.workflow_ids():
            if not any(i.state in ("open", "blocked")
                       for i in self.mgr.open_incidents(wf, "prompt_safety")):
                continue
            with gov.scope.tx() as tx:
                ws = gov._snapshot(tx, wf)
            before = self._mgr_incidents(wf)
            reqs = self.mgr.fix_patches(ws)                   # may validate candidates
            with gov.scope.tx() as tx:
                for req in reqs:
                    did = write_decision(tx, gov._decision(
                        wf, req.owner, "patch", "7.1/prompt_fix",
                        {"path": list(req.path), "evidence": json_safe(req.evidence)},
                        {"value": req.value, "class": req.cls}))
                    gov.overlay.add(dataclasses.replace(req, decision_id=did))
                    n += 1
                self._persist(wf, before, tx)
        return n

    # ------------------------------------------------------------------ VERIFY
    def verify(self) -> None:
        gov = self.gov
        for wf in gov.registry.workflow_ids():
            live = [i for i in self.mgr.open_incidents(wf, "prompt_safety")
                    if i.state in ("fixing", "verifying")]
            if not live:
                continue
            with gov.scope.tx() as tx:
                ws = gov._snapshot(tx, wf)
                before = self._mgr_incidents(wf)
                self._link_proposals(tx, wf, live)
                self.mgr.verify(ws)
                self._persist(wf, before, tx)

    def _link_proposals(self, tx: Tx, wf: str, incidents: list[Any]) -> None:
        for inc in incidents:
            if inc.fix_proposal_id is not None:
                continue
            run = inc.evidence.get("fix_gate_run")
            if not run:
                continue
            row = tx.execute("""SELECT proposal_id FROM proposal
                                WHERE wf = %s AND evidence->>'validation_run_id' = %s
                                ORDER BY submitted_minute NULLS LAST LIMIT 1""",
                             (self.rt.catalog.code(wf), run)).fetchone()
            if row:
                self.mgr.on_fix_proposal(inc.incident_id, row[0])

    # ------------------------------------------------------------------ persistence
    def _mgr_incidents(self, wf: str) -> dict[str, Any]:
        return {i.incident_id: i for i in self.mgr.incidents.values() if i.wf == wf}

    def _persist(self, wf: str, before: Mapping[str, Any], tx: Tx | None = None) -> None:
        """Verdicts, then the manager's decisions, alerts and changed incidents of `wf`."""
        if tx is None:
            with self.gov.scope.tx() as t:
                self._persist(wf, before, t)
            return
        for (w, prompt), v in sorted(self.runner.verdicts.items()):
            if self._written_verdicts.get((w, prompt)) == v:
                continue
            tx.execute("""INSERT INTO prompt_verdict (wf, prompt, verdict, mode, runs, pooled_n,
                                                      pooled_failures, gate_run, decided_minute)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (wf, prompt) DO UPDATE SET verdict = excluded.verdict,
                              mode = excluded.mode, runs = excluded.runs,
                              pooled_n = excluded.pooled_n,
                              pooled_failures = excluded.pooled_failures,
                              gate_run = excluded.gate_run""",
                       (self.rt.catalog.code(w), self._prompt_code(w, prompt), v.verdict, v.mode,
                        list(v.runs), v.pooled_n, v.pooled_failures, v.gate_run, self.gov.now))
            self._written_verdicts[(w, prompt)] = v
        self.gov.pickup._flush(tx, wf, before)


__all__ = ["PromptSafety", "IntentValidationClient", "IntentSuggestClient"]
