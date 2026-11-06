"""Contract tests: the simulator behaviours the design depends on, checked over HTTP against a
real simulator process (a copy of public_01 in a temp run dir, port 8700), through SimClient.

These fail first if the graders' copy of the simulator differs from ours (DESIGN.md 10.1). Every
parameter (tick, hours, latency, caps, TTL, max_shift) is read from the API, never assumed.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random

import pytest

from controller.simclient import SimClient
from controller.simclient import errors as E
from controller.types import (ApprovalPublic, Config, SimEpoch, WorkflowSpec, _sim_normalise)
from tests.support.sim_process import SimProcess

pytestmark = pytest.mark.e2e

PORT = 8700
SCENARIO = "scenarios/public_01_dev.yaml"
RENEWAL, KYC, CANCEL = "nw_renewal", "nw_kyc", "ks_cancel"


@pytest.fixture()
def sim(tmp_path):
    with SimProcess(SCENARIO, tmp_path, port=PORT) as proc:
        client = SimClient(proc.url, timeout_s=60, advance_timeout_s=120)
        try:
            yield client
        finally:
            client.close()


# ---------------------------------------------------------------- helpers
def _now(c: SimClient) -> int:
    return c.state().sim_minute


def _advance_to(c: SimClient, minute: int) -> None:
    now = _now(c)
    if minute > now:
        c.advance(minute - now)


def _ceil_tick(m: int, tick: int) -> int:
    return -(-m // tick) * tick


def _voice_change(c: SimClient, wf: str, salt: int = 0) -> dict:
    """A body that changes only the default voice distribution (an approval-only dim)."""
    spec = c.workflow(wf)
    cfg = c.config(wf).config
    voices = list(spec.options["voice"])
    w = {voices[0]: 0.9 - 0.01 * salt, voices[1]: 0.1 + 0.01 * salt}
    return cfg.with_allocation("default", "voice", w).body()


def _request(c: SimClient, wf: str, pid: str, proposed: dict, **overrides) -> dict:
    body = {"proposal_id": pid, "workflow_id": wf, "base_version": c.config(wf).version,
            "proposed_config": proposed, "change_summary": "Voice: try a second voice",
            "diff": {"voice": "test"}, "affected_leads": 10, "dry_run": {"method": "contract"},
            "impact_score": 0.1, "rationale": "contract test"}
    body.update(overrides)
    return body


def _wait_decided(c: SimClient, aid: str, tick: int, limit: int = 3 * 1440) -> ApprovalPublic:
    start = _now(c)
    while True:
        a = c.approval(aid)
        if a.status != "pending":
            return a
        assert _now(c) - start < limit, f"{aid} still pending"
        c.advance(tick)


def _approved(c: SimClient, wf: str, prefix: str, tries: int = 3) -> ApprovalPublic:
    """An approved approval on wf (judgement-call rejections are seeded by proposal id)."""
    tick = c.state().tick_minutes
    for i in range(tries):
        a = c.request_approval(_request(c, wf, f"{prefix}-{i}", _voice_change(c, wf, i)))
        a = _wait_decided(c, a.approval_id, tick)
        if a.status == "approved":
            return a
    pytest.fail(f"no approval approved in {tries} tries")


def _open_minute(spec: WorkflowSpec, day: int) -> int:
    return day * 1440 + spec.governance.approver_hours[0] * 60


def _events(c: SimClient, chunk_numbers) -> list[dict]:
    out = []
    for n in chunk_numbers:
        data = b"".join(c.stream_chunk(n))
        out += [json.loads(line) for line in data.splitlines() if line]
    return out


# ---------------------------------------------------------------- time and events
def test_time_events_and_end(sim):
    st = sim.state()
    tick = st.tick_minutes
    epoch = SimEpoch.from_state(st)
    assert epoch.ts(0) == st.ts and dt.datetime.fromisoformat(st.ts).weekday() == 0  # D0 Monday
    hist = sim.list_chunks("history")
    assert hist and sim.list_chunks("events") == []          # idle world: history only
    lines = b"".join(sim.stream_chunk(hist[0].chunk)).splitlines()
    assert len(lines) == hist[0].events
    ids = [json.loads(x)["event_id"] for x in lines[:1000]]
    assert ids == list(range(hist[0].first_event_id, hist[0].first_event_id + len(ids)))
    with pytest.raises(E.BadMinutes):
        sim.advance(tick + 1)
    r = sim.advance(tick * 4)
    assert r.sim_minute == tick * 4 and not r.finished
    assert r.new_chunks == () and sim.list_chunks("events") == []   # no events, no chunk
    r = sim.advance(10 * 60 - r.sim_minute)                          # batch upload and dialling
    assert r.new_chunks and all(m.kind == "events" for m in r.new_chunks)
    listed = sim.list_chunks("events", after=-1)
    assert [m.chunk for m in r.new_chunks] == [m.chunk for m in listed]
    assert sim.list_chunks("events", after=listed[-1].chunk) == []
    with pytest.raises(E.ChunkNotFound):
        list(sim.stream_chunk(10_000))
    end = sim.state().end_minute
    r = sim.advance(end + 1440 - _ceil_tick(_now(sim), tick))   # overshoot is clamped to the end
    assert r.finished and r.sim_minute == end
    with pytest.raises(E.Finished):
        sim.advance(tick)


# ---------------------------------------------------------------- governance contract
def test_lazy_lapse(sim):
    spec = sim.workflow(RENEWAL)
    tick = sim.state().tick_minutes
    epoch = SimEpoch.from_state(sim.state())
    _advance_to(sim, _open_minute(spec, 0))
    a = _approved(sim, RENEWAL, "lapse")
    apply_by = epoch.minute(a.apply_by)
    assert apply_by == epoch.minute(a.decided_at) + int(spec.governance.apply_window_hours * 60)
    v_before = sim.config(RENEWAL).version
    _advance_to(sim, _ceil_tick(apply_by + 1, tick))
    assert sim.approval(a.approval_id).status == "approved"   # lazy: still reads approved
    with pytest.raises(E.ApprovalLapsed):
        sim.apply(RENEWAL, a.approval_id)
    assert sim.approval(a.approval_id).status == "lapsed"     # only now
    assert sim.config(RENEWAL).version == v_before


def test_proposal_id_consumption_rules(sim):
    spec = sim.workflow(RENEWAL)
    _advance_to(sim, _open_minute(spec, 0))
    body = _voice_change(sim, RENEWAL)
    # synchronous 4xx create no record, so the id stays free
    with pytest.raises(E.StaleBaseVersion):
        sim.request_approval(_request(sim, RENEWAL, "p-sync", body, base_version=99))
    bad = json.loads(json.dumps(body))
    bad["allocations"]["default"]["voice"] = {"V9": 1}
    with pytest.raises(E.InvalidConfig):
        sim.request_approval(_request(sim, RENEWAL, "p-sync", bad))
    with pytest.raises(E.NoChange):
        sim.request_approval(_request(sim, RENEWAL, "p-sync", sim.config(RENEWAL).config.body()))
    assert sim.request_approval(_request(sim, RENEWAL, "p-sync", body)).status == "pending"
    # a request that will be rejected still consumes its id
    doomed = sim.request_approval(_request(sim, RENEWAL, "p-doomed", body, change_summary=" "))
    assert doomed.status == "pending" and doomed.reasons == ()      # reasons hidden until decided
    with pytest.raises(E.DuplicateProposalId):
        sim.request_approval(_request(sim, RENEWAL, "p-doomed", body))
    # auto-apply: a 403 does not consume the id, a success does
    cfg = sim.config(RENEWAL)
    with pytest.raises(E.AutoApplyNotPermitted):
        sim.auto_apply(RENEWAL, {"proposal_id": "a-1", "base_version": cfg.version,
                                 "proposed_config": body, "rationale": "voice is not allowed"})
    dids = list(spec.options["caller_id"])
    spread = cfg.config.with_allocation("default", "caller_id",
                                        {dids[0]: 0.4, dids[1]: 0.4, dids[2]: 0.2})
    r = sim.auto_apply(RENEWAL, {"proposal_id": "a-1", "base_version": cfg.version,
                                 "proposed_config": spread.body(), "rationale": "spread load"})
    assert r.version == cfg.version + 1 and r.changed_dims == ("caller_id",)
    assert sim.config_history(RENEWAL)[-1].proposal_id == "a-1"
    with pytest.raises(E.DuplicateProposalId):
        sim.auto_apply(RENEWAL, {"proposal_id": "a-1", "base_version": r.version,
                                 "proposed_config": spread.body(), "rationale": "again"})
    # one global namespace: across both paths and across workflows
    with pytest.raises(E.DuplicateProposalId):
        sim.auto_apply(RENEWAL, {"proposal_id": "p-sync", "base_version": r.version,
                                 "proposed_config": spread.body(), "rationale": "x"})
    with pytest.raises(E.DuplicateProposalId):
        sim.request_approval(_request(sim, CANCEL, "a-1", _voice_change(sim, CANCEL)))
    a = _wait_decided(sim, doomed.approval_id, sim.state().tick_minutes)
    assert a.status == "rejected" and any("non-empty" in x for x in a.reasons)


def test_decision_visible_only_after_its_tick(sim):
    spec = sim.workflow(RENEWAL)
    st = sim.state()
    tick, epoch = st.tick_minutes, SimEpoch.from_state(st)
    _advance_to(sim, _open_minute(spec, 0))
    cap = spec.governance.max_requests_per_24h
    pending = {sim.request_approval(_request(sim, RENEWAL, f"vis-{i}",
                                             _voice_change(sim, RENEWAL, i))).approval_id
               for i in range(cap)}
    first_visible: dict[str, int] = {}
    chunks_by_stop: dict[int, list[int]] = {}
    while pending - set(first_visible):
        r = sim.advance(tick)
        chunks_by_stop[r.sim_minute] = [m.chunk for m in r.new_chunks]
        for aid in pending - set(first_visible):
            if sim.approval(aid).status != "pending":
                first_visible[aid] = r.sim_minute
    events = {e["approval_id"]: (e, stop) for stop, nums in chunks_by_stop.items()
              for e in _events(sim, nums) if e["type"] == "approval_decided"}
    for aid, seen in first_visible.items():
        a = sim.approval(aid)
        d = epoch.minute(a.decided_at)
        processed = _ceil_tick(d, tick)         # the first tick that starts at or after d
        assert seen == processed + tick, (aid, d, seen)
        ev, stop = events[aid]
        assert stop == seen and epoch.minute(ev["ts"]) == d and ev["status"] == a.status


def test_fatigue_window_is_strict_rolling_24h(sim):
    spec = sim.workflow(RENEWAL)
    tick = sim.state().tick_minutes
    cap = spec.governance.max_requests_per_24h
    t0 = _open_minute(spec, 0)
    _advance_to(sim, t0)
    for i in range(cap):
        sim.request_approval(_request(sim, RENEWAL, f"fat-{i}", _voice_change(sim, RENEWAL, i)))
    _advance_to(sim, t0 + 1440 - tick)          # created_minute > now - 1440: still counted
    over = sim.request_approval(_request(sim, RENEWAL, "fat-over", _voice_change(sim, RENEWAL, 7)))
    assert over.status == "pending"             # accepted, burns a slot, rejected later
    _advance_to(sim, t0 + 1440)                 # exactly 24h: the first batch stops counting
    ok = sim.request_approval(_request(sim, RENEWAL, "fat-ok", _voice_change(sim, RENEWAL, 8)))
    a_over = _wait_decided(sim, over.approval_id, tick)
    a_ok = _wait_decided(sim, ok.approval_id, tick)
    assert a_over.status == "rejected" and any("fatigue" in r for r in a_over.reasons)
    assert not any("fatigue" in r for r in a_ok.reasons)       # the rejected one counted (2 <= cap)
    recent = [a for a in sim.approvals(RENEWAL)
              if SimEpoch.from_state(sim.state()).minute(a.created_at) > t0]
    assert {a.proposal_id for a in recent} == {"fat-over", "fat-ok"}


def test_next_working_has_no_weekends_and_expiry_is_fixed_at_submit(sim):
    renewal, cancel = sim.workflow(RENEWAL), sim.workflow(CANCEL)
    st = sim.state()
    tick, epoch = st.tick_minutes, SimEpoch.from_state(st)
    friday = 4                                       # D0 is Monday 2027-01-04
    assert dt.datetime.fromisoformat(epoch.ts(friday * 1440)).weekday() == 4
    lo_r, hi_r = renewal.governance.approver_hours
    lat_lo = renewal.governance.approver_latency_minutes[0]
    t_sub = friday * 1440 + hi_r * 60 - lat_lo + 15 - tick   # now + L >= close for every L
    assert t_sub + lat_lo >= friday * 1440 + hi_r * 60
    _advance_to(sim, t_sub)
    a = sim.request_approval(_request(sim, RENEWAL, "weekend", _voice_change(sim, RENEWAL)))
    # cancel: a submit late enough that the next working minute is past the TTL always expires
    c_lo, c_hi = cancel.governance.approver_hours
    c_ttl = int(cancel.governance.proposal_ttl_hours * 60)
    t_c = friday * 1440 + c_hi * 60 + 30
    next_open = (friday + 1) * 1440 + c_lo * 60
    assert next_open > t_c + c_ttl                   # deterministic expiry for this policy
    _advance_to(sim, t_c)
    x = sim.request_approval(_request(sim, CANCEL, "expires", _voice_change(sim, CANCEL)))
    expired = _wait_decided(sim, x.approval_id, tick)     # expires before the renewal decides
    assert expired.status == "expired" and expired.decided_at is None
    assert epoch.minute(expired.expires_at) == t_c + c_ttl
    assert _now(sim) == _ceil_tick(t_c + c_ttl, tick) + tick    # seen after the expiry tick
    decided = _wait_decided(sim, a.approval_id, tick)
    saturday_open = (friday + 1) * 1440 + lo_r * 60
    assert epoch.minute(decided.decided_at) == saturday_open
    assert dt.datetime.fromisoformat(decided.decided_at).weekday() == 5   # decided on a Saturday


def test_validation_ids_are_sequential_global_and_cost_credits(sim):
    credits = sim.state().credits_remaining
    runs = [(RENEWAL, "P1", 50), (CANCEL, "P1", 150), (KYC, "P3", 100)]
    for i, (wf, prompt, n) in enumerate(runs, start=1):
        v = sim.validate(wf, prompt, n)
        assert v.validation_run_id == f"val_{i:05d}" and v.workflow_id == wf and v.n == n
        credits -= math.ceil(n / 100)
        assert v.credits_remaining == pytest.approx(credits)
        assert v.pass_rate == pytest.approx(1 - v.failures / n, abs=1e-4)
    with pytest.raises(E.BadPromptVersion):
        sim.validate(KYC, "P4", 100)                 # not in this workflow's catalog
    with pytest.raises(E.BadN):
        sim.validate(RENEWAL, "P1", 49)
    assert sim.state().credits_remaining == pytest.approx(credits)
    assert sim.validate(RENEWAL, "P3", 50).validation_run_id == "val_00004"   # no id consumed
    credits -= 1
    while credits >= 20:
        sim.validate(RENEWAL, "P1", 2000)
        credits -= 20
    with pytest.raises(E.OutOfCredits):
        sim.validate(RENEWAL, "P1", 2000)
    if credits < 3:
        with pytest.raises(E.OutOfCredits):
            sim.suggest_fixes(RENEWAL, "hallucination flags up")
    assert sim.state().credits_remaining == pytest.approx(credits)


def test_auto_apply_403_path(sim):
    spec = sim.workflow(RENEWAL)
    pol = spec.governance.auto_apply
    cur = sim.config(RENEWAL)
    dids = list(spec.options["caller_id"])
    live = cur.config.dist("default", "caller_id")
    history_len = len(sim.config_history(RENEWAL))

    def put(pid: str, cfg: Config, base: int):
        return sim.auto_apply(RENEWAL, {"proposal_id": pid, "base_version": base,
                                        "proposed_config": cfg.body(), "rationale": "contract"})

    assert "voice" not in pol.allowed_dims and "caller_id" in pol.allowed_dims
    with pytest.raises(E.AutoApplyNotPermitted) as e1:
        put("auto-voice", cur.config.with_allocation("default", "voice", {"V2": 1}), cur.version)
    assert any("not permitted" in p for p in e1.value.problems)
    unused = [d for d in dids if d not in live]
    with pytest.raises(E.AutoApplyNotPermitted) as e2:   # TV 1.0 > max_shift
        put("auto-big", cur.config.with_allocation("default", "caller_id", {unused[0]: 1}),
            cur.version)
    assert any("max_shift" in p for p in e2.value.problems)
    with pytest.raises(E.AutoApplyNotPermitted) as e3:
        put("auto-same", cur.config, cur.version)
    assert any("no change" in p for p in e3.value.problems)
    with pytest.raises(E.StaleBaseVersion):
        put("auto-stale", cur.config.with_allocation("default", "caller_id",
                                                     {dids[0]: 1, dids[1]: 1, dids[2]: 1}), 99)
    kyc = sim.config(KYC)
    with pytest.raises(E.AutoApplyNotPermitted) as e4:
        sim.auto_apply(KYC, {"proposal_id": "auto-kyc", "base_version": kyc.version,
                             "proposed_config": kyc.config.with_prompt("P3").body(),
                             "rationale": "x"})
    assert any("not enabled" in p for p in e4.value.problems)
    assert len(sim.config_history(RENEWAL)) == history_len      # no 403 created a version
    # a shift of exactly max_shift is permitted by the simulator (it allows + 1e-9)
    first = next(iter(live))
    target = {k: v for k, v in live.items()}
    target[first] -= pol.max_shift
    target[unused[0]] = pol.max_shift
    exact = cur.config.with_allocation("default", "caller_id", {k: v for k, v in target.items()
                                                                if v > 0})
    if pol.max_shift <= live[first]:
        r = put("auto-edge", exact, cur.version)
        assert r.changed_dims == ("caller_id",)


def test_resending_stored_weights_can_drift_and_body_prevents_it(sim):
    """The float trap behind Config.body(): the simulator re-normalises every body, so stored
    weights sent back verbatim can change in their last bits and be reported as a changed dim."""
    spec = sim.workflow(RENEWAL)
    cur = sim.config(RENEWAL)
    dids = list(spec.options["caller_id"])[:3]
    live = cur.config.dist("default", "caller_id")
    rng = random.Random(3)
    for _ in range(10_000):                          # raw weights whose stored form drifts
        raw = {d: round(rng.random(), 17) for d in dids}
        v = _sim_normalise(raw)
        tv = 0.5 * sum(abs(v.get(o, 0) - live.get(o, 0)) for o in set(v) | set(live))
        if _sim_normalise(v) != v and tv <= spec.governance.auto_apply.max_shift:
            break
    else:
        pytest.skip("no drifting weight vector found")
    r1 = sim.auto_apply(RENEWAL, {"proposal_id": "drift-1", "base_version": cur.version,
                                  "proposed_config": cur.config.with_allocation(
                                      "default", "caller_id", raw).body(), "rationale": "x"})
    stored = sim.config(RENEWAL)
    lo, hi = spec.dialling_window
    sched = stored.config.with_hour_weights("default", {h: 1.0 for h in range(lo, hi)})
    naive = json.loads(json.dumps(sched.stored()))   # stored floats sent back verbatim
    r2 = sim.auto_apply(RENEWAL, {"proposal_id": "drift-2", "base_version": r1.version,
                                  "proposed_config": naive, "rationale": "x"})
    assert r2.changed_dims == ("caller_id", "schedule")        # the spurious caller_id change
    now = sim.config(RENEWAL)
    fixed = now.config.with_hour_weights("default", {h: 2.0 for h in range(lo, hi)})
    assert fixed.wire_exact()
    r3 = sim.auto_apply(RENEWAL, {"proposal_id": "drift-3", "base_version": r2.version,
                                  "proposed_config": fixed.body(), "rationale": "x"})
    assert r3.changed_dims == ("schedule",)
    assert sim.config(RENEWAL).config == fixed


def test_stale_base_at_apply_and_no_double_apply(sim):
    renewal, cancel = sim.workflow(RENEWAL), sim.workflow(CANCEL)
    st = sim.state()
    tick, epoch = st.tick_minutes, SimEpoch.from_state(st)
    _advance_to(sim, _open_minute(renewal, 0))
    a = _approved(sim, RENEWAL, "stale")
    assert _now(sim) <= epoch.minute(a.apply_by) - tick
    cur = sim.config(RENEWAL)
    dids = list(renewal.options["caller_id"])
    bump = sim.auto_apply(RENEWAL, {"proposal_id": "stale-bump", "base_version": cur.version,
                                    "proposed_config": cur.config.with_allocation(
                                        "default", "caller_id", {dids[0]: 1, dids[1]: 1,
                                                                 dids[2]: 1}).body(),
                                    "rationale": "our own auto-apply stales our approval"})
    with pytest.raises(E.StaleBaseVersion) as e:
        sim.apply(RENEWAL, a.approval_id)
    assert e.value.current_version == bump.version
    assert sim.approval(a.approval_id).status == "approved"    # still approved, never applied
    # a fresh approval applies once; a retried apply cannot double-apply
    _advance_to(sim, max(_now(sim), _open_minute(cancel, 0)))
    b = _approved(sim, CANCEL, "apply")
    v = sim.config(CANCEL).version
    r = sim.apply(CANCEL, b.approval_id)
    assert r.version == v + 1 and r.changed_dims == ("voice",)
    with pytest.raises(E.NotApproved) as e2:
        sim.apply(CANCEL, b.approval_id)
    assert e2.value.approval_status == "applied"
    hist = sim.config_history(CANCEL)
    assert hist[-1].approval_id == b.approval_id and hist[-1].actor == "controller_approved"
    with pytest.raises(E.ApprovalNotFound):
        sim.approval("apr_99999")
