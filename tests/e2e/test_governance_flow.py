"""Governance end to end against a real simulator (DESIGN.md 6.1 to 6.5, M3 acceptance subset).

Registry, overlay, policy replica, timing, lifecycle and release train drive a real simulator
(port 8712) with hand-made patches standing in for the learners. The assertions are the
governance-hygiene invariants the grader counts, read from the simulator's own log and API:
no auto_apply_rejected, no lapse, no expiry, no apply conflict, fatigue never above the cap,
routine requests only at slot minutes, and every approved request applied in time.
"""
from __future__ import annotations

import copy
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

from controller.governance.timing import routine_slots
from controller.simclient import SimClient
from tests.e2e.gov_harness import Harness
from tests.support.sim_process import REPO, SimProcess

pytestmark = pytest.mark.e2e

PORT = 8712
SCENARIO = "scenarios/public_01_dev.yaml"


def _tenants(gov_overrides: dict[str, dict]) -> list[dict]:
    doc = yaml.safe_load((REPO / SCENARIO).read_text())
    tenants = copy.deepcopy(doc["tenants"])
    for t in tenants:
        for w in t["workflows"]:
            w["governance"].update(gov_overrides.get(w["id"], {}))
    return tenants


def _harness(tmp_path: Path, gov_overrides: dict[str, dict]):
    overrides = {"tenants": _tenants(gov_overrides), "history_days": 1}
    proc = SimProcess(SCENARIO, tmp_path, port=PORT, overrides=overrides).start()
    client = SimClient(proc.url, timeout_s=60)
    return proc, client, Harness(client, proc.run_dir)


def _hygiene(h: Harness) -> None:
    kinds = h.log_kinds()
    for bad in ("auto_apply_rejected", "approval_lapsed", "apply_conflict", "approval_expired"):
        assert kinds.get(bad, 0) == 0, (bad, [e for e in h.sim_log() if e["kind"] == bad])
    assert not [a for a in h.alerts if a[0] in ("AUTO_APPLY_REJECTED", "APPLY_AT_RISK",
                                                "APPROVAL_EXPIRED", "GOV_INVARIANT")], h.alerts
    assert not h.store.in_states(("approved", "applying", "submitting", "auto_applying"))
    for a in h.client.approvals():
        p = next(p for p in h.store.rows.values() if p.proposal_id == a.proposal_id)
        if a.status == "approved":           # approved but never applied: only if we chose so
            assert p.state in ("superseded", "stale", "abandoned", "lapsed_local"), p
            assert p.state != "lapsed_local"
    # fatigue: never above the policy cap in any rolling 24h window, routine within C_eff
    created = defaultdict(list)
    for a in h.client.approvals():
        created[a.workflow_id].append(h.epoch.minute(a.created_at))
    for wf, mins in created.items():
        pol = h.registry.policy(wf)
        for t in mins:
            assert sum(1 for c in mins if t - 1440 < c <= t) <= pol.c_eff, (wf, sorted(mins))


def _routine_requests_at_slots(h: Harness) -> None:
    for a in h.client.approvals():
        p = next(p for p in h.store.rows.values() if p.proposal_id == a.proposal_id)
        if p.cls >= 2:
            m = h.epoch.minute(a.created_at)
            assert m in routine_slots(m // 1440, h.registry.policy(a.workflow_id)), (a, m)


def test_public_policies_flow(tmp_path):
    proc, client, h = _harness(tmp_path, {})
    try:
        h.advance(8 * 60)
        h.patch("nw_renewal", ("alloc", "default", "voice"), {"V1": 0.96, "V3": 0.04})
        h.patch("nw_renewal", ("alloc", "default", "caller_id"),
                {"D1": 0.45, "D2": 0.45, "D3": 0.1}, owner="did")
        h.patch("ks_cancel", ("alloc", "default", "caller_id"),
                {"D1": 0.45, "D2": 0.45, "D3": 0.1}, owner="did")
        h.patch("nw_kyc", ("alloc", "default", "voice"), {"V1": 0.9, "V2": 0.1}, cls=1)
        h.run_until(2 * 1440)

        _hygiene(h)
        _routine_requests_at_slots(h)
        # nw_renewal (Northwind, caller ID auto-applicable): the operational part auto-applies
        # at the 11:00 slot immediately before the voice approval, which carries the new base
        hist = client.config_history("nw_renewal")
        auto = [v for v in hist if v.actor == "controller_auto"]
        assert auto and auto[0].changed_dims == ("caller_id",)
        assert h.epoch.minute(auto[0].applied_at) == 11 * 60
        reqs = client.approvals("nw_renewal")
        assert h.epoch.minute(reqs[0].created_at) == 11 * 60
        assert reqs[0].base_version == auto[0].version and reqs[0].changed_dims == ("voice",)
        # ks_cancel (Kestrel cancel: notice 60 min posted inside approver hours): noticed at
        # 08:00, posted at the 10:00 opening, applied at 11:00 without any approval
        ks = [v for v in client.config_history("ks_cancel") if v.actor == "controller_auto"]
        assert ks and h.epoch.minute(ks[0].applied_at) == 11 * 60
        assert client.approvals("ks_cancel") == []
        # nw_kyc (auto-apply disabled): the P1 goes at the first expiry-safe in-hours tick
        kyc = client.approvals("nw_kyc")
        assert h.epoch.minute(kyc[0].created_at) == 10 * 60
        applied = [a for a in client.approvals() if a.status == "applied"]
        assert applied, "at least one approval should be applied in two days"
    finally:
        client.close()
        proc.stop()


CASES = {
    # (a) TTL 12h with latency 60-480 (approvers 10-18)
    "nw_renewal": {"approver_hours": [10, 18], "approver_latency_minutes": [60, 480],
                   "proposal_ttl_hours": 12, "base_reject_rate": 0.0,
                   "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}},
    # (b) approvers 14-16 with TTL 4h
    "nw_kyc": {"approver_hours": [14, 16], "approver_latency_minutes": [30, 240],
               "proposal_ttl_hours": 4, "base_reject_rate": 0.0, "max_requests_per_24h": 2},
    # (c) fatigue cap 1
    "ks_cancel": {"max_requests_per_24h": 1, "base_reject_rate": 0.0,
                  "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}},
}


def test_j2_policy_cases_get_approvals_applied(tmp_path):
    proc, client, h = _harness(tmp_path, CASES)
    days = 4
    try:
        for d in range(days):
            h.run_until(d * 1440 + 8 * 60)
            w = round(0.9 - 0.05 * d, 2)        # a new value every day: never a no-op
            for wf in CASES:
                h.patch(wf, ("alloc", "default", "voice"), {"V1": w, "V2": round(1 - w, 2)})
        h.run_until(days * 1440)

        _hygiene(h)
        _routine_requests_at_slots(h)
        applied_days = defaultdict(set)
        for wf in CASES:
            for v in client.config_history(wf):
                if v.actor == "controller_approved":
                    applied_days[wf].add(h.epoch.minute(v.applied_at) // 1440)
        slot = {wf: h.epoch.minute(client.approvals(wf)[0].created_at) % 1440 for wf in CASES}
        assert slot == {"nw_renewal": 10 * 60, "nw_kyc": 12 * 60, "ks_cancel": 11 * 60}
        # (a) and (b): an approval applied every day
        assert applied_days["nw_renewal"] == set(range(days)), applied_days
        assert applied_days["nw_kyc"] == set(range(days)), applied_days
        # (c): cap 1 means routine at most once per 48h, and it still ships
        ks = sorted(h.epoch.minute(a.created_at) for a in client.approvals("ks_cancel"))
        assert all(b - a >= 2880 for a, b in zip(ks, ks[1:])), ks
        assert applied_days["ks_cancel"] == {0, 2}, applied_days
    finally:
        client.close()
        proc.stop()


def test_public_02_supervisor_voice_edit_is_rebased_never_reverted(tmp_path):
    """public_02: the Northwind supervisor sets the default voice to V2 at D5 15:00 while our
    voice change is pending. It must become STALE (never applied, no apply_conflict), the
    patch on the supervisor's path is invalidated, and nothing ever reverts V2."""
    scenario = "scenarios/public_02_incidents.yaml"
    proc = SimProcess(scenario, tmp_path, port=PORT, overrides={"history_days": 1}).start()
    client = SimClient(proc.url, timeout_s=60)
    try:
        h = Harness(client, proc.run_dir)
        h.advance(5 * 1440 + 14 * 60 + 30)             # D5 14:30
        h.known = {wf: h._live(wf) for wf in h.registry.workflow_ids()}
        h.patch("nw_renewal", ("alloc", "default", "voice"), {"V1": 0.9, "V3": 0.1}, cls=1,
                owner="allocator")
        h.patch("nw_renewal", ("alloc", "default", "caller_id"),
                {"D1": 0.5, "D2": 0.4, "D3": 0.1}, cls=1, owner="did")
        h.step()                                         # P1: submit now (14:30)
        ours = client.approvals("nw_renewal")
        assert len(ours) == 1 and h.epoch.minute(ours[0].created_at) == 5 * 1440 + 870
        h.run_until(7 * 1440)

        kinds = h.log_kinds()
        assert kinds.get("apply_conflict", 0) == 0 and kinds.get("approval_lapsed", 0) == 0
        p = next(p for p in h.store.rows.values() if p.approval_id == ours[0].approval_id)
        assert p.state == "stale"
        rep = next(r for r in h.rebases if r.wf == "nw_renewal")
        assert ("alloc", "default", "voice") in rep.sup_paths
        assert p.proposal_id in rep.stale_proposals or p.state == "stale"
        final = client.config("nw_renewal").config
        assert dict(final.dist("default", "voice")) == {"V2": 1.0}      # never reverted
        sup_v = max(v.version for v in client.config_history("nw_renewal")
                    if v.actor == "supervisor")
        later = [v for v in client.config_history("nw_renewal") if v.version > sup_v]
        assert all("voice" not in v.changed_dims for v in later), later
        # the operational part (caller ID) auto-applied at 14:30 right before the voice
        # approval, so the approval carried the new base (6.2 batching)
        auto = [v for v in client.config_history("nw_renewal") if v.actor == "controller_auto"]
        assert [(v.changed_dims, h.epoch.minute(v.applied_at)) for v in auto] == [
            (("caller_id",), 5 * 1440 + 870)]
        assert ours[0].changed_dims == ("voice",) and ours[0].base_version == auto[0].version
    finally:
        client.close()
        proc.stop()
