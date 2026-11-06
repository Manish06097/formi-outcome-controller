"""T4 wiring against a real simulator over HTTP (port 8714): the safety verdict, the gate
evidence, the one suggest-fixes call and the credit budget, driven through the real SimClient.

The incident logic itself is pure and unit-tested; this checks what only the real API can: the
validation and suggest-fixes payloads parse, every credit we book equals what the simulator
charged, and the verdict on a public_02-like unsafe prompt (P2 on ks_cancel) is UNSAFE while the
business default is SAFE. Validation outcomes are seeded by the run id, so this is deterministic.
"""
from __future__ import annotations

import numpy as np
import pytest

from controller.incidents.credits import OrderedCreditBudget, plan_pools
from controller.incidents.manager import IncidentManagerImpl
from controller.incidents.safety import SafetyVerdictRunner, gate_plan
from controller.learning.did import DidHealthModel
from controller.simclient import SimClient
from controller.types import Finding, FrozenDict
from tests.support import synth
from tests.support.sim_process import SimProcess

pytestmark = pytest.mark.e2e

PORT = 8714
SCENARIO = "scenarios/public_02_incidents.yaml"


@pytest.fixture()
def client(tmp_path):
    with SimProcess(SCENARIO, tmp_path, port=PORT, overrides={"history_days": 2}) as proc:
        c = SimClient(proc.url, timeout_s=60, advance_timeout_s=120)
        try:
            yield c
        finally:
            c.close()


def test_verdicts_evidence_suggest_and_credits_over_http(client):
    specs = {w.workflow_id: w for w in client.workflows()}
    cancel = specs["ks_cancel"]
    c0 = client.state().credits_remaining
    k = max(len(s.options["prompt_version"]) for s in specs.values())
    g = cancel.governance.min_validation_pass_rate
    tenants = {s.tenant_id for s in specs.values()}
    plan = plan_pools(c0, k, gate_plan(g).tight,
                      {t: sum(1 for s in specs.values() if s.tenant_id == t) for t in tenants})
    budget = OrderedCreditBudget(plan)
    runner = SafetyVerdictRunner(
        client, budget, gate_of=lambda wf: specs[wf].governance.min_validation_pass_rate,
        tenant_of=lambda wf: specs[wf].tenant_id)

    # D0 screen of the business default, paid from the tenant's screen envelope
    default_prompt = client.config("ks_cancel").config.prompt_version
    screen = runner.run("ks_cancel", default_prompt, "sequential", False, purpose="d0_screen",
                        pool="S")
    assert screen.verdict == "SAFE"
    assert budget.left("S", cancel.tenant_id) == pytest.approx(
        plan.s_by_tenant[cancel.tenant_id] - screen.credits)

    # a supervisor ships the unsafe prompt: the manager runs FULL, opens, suggests once, fixes
    prompts = list(cancel.options["prompt_version"])
    unsafe = "P2" if "P2" in prompts else prompts[1]
    rng = np.random.default_rng(0)
    code = prompts.index(default_prompt)
    flags = [(h, code, 100, int(rng.binomial(100, 0.015)), 47)
             for h in range(-14 * 24, 0) if 10 <= h % 24 < 20]
    policy = synth.make_policy(cancel, avoid_prompt_terms=("verbatim", "word for word"))
    ws = synth.snapshot(cancel, flags=synth.flag_series(flags), now=12 * 60, policy=policy,
                        live=synth.config({cancel.options["caller_id"][0]: 1.0}, prompt=unsafe))
    m = IncidentManagerImpl(runner, budget, DidHealthModel(), llm=client, llm_enabled=True)
    f = Finding("ks_cancel", "prompt", "changed", unsafe, 0.0, 0.0, ws.now,
                FrozenDict({"actor": "supervisor"}))
    reqs = m.on_findings(ws, [f])
    assert len(reqs) == 1 and reqs[0].value == default_prompt and reqs[0].p0_rank == 0
    gate_run = reqs[0].evidence["validation_run_id"]
    assert gate_run == screen.runs[0]           # not tight at 0.97: the screen's stage-1 run
    inc = m.open_incidents("ks_cancel")[0]
    assert inc.evidence["suggest"]["called"] is True
    assert all(s["prompt_version"] in prompts for s in inc.evidence["suggest"]["suggestions"])

    # every credit we booked is a credit the simulator charged
    booked = sum(p.spent for p in budget.pools.values())
    assert c0 - client.state().credits_remaining == pytest.approx(booked)
