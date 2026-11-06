"""SimClient transport rules (DESIGN.md 8.4) on a mock transport: which calls retry, which never
do, and how error bodies map to typed errors. The real-simulator behaviour is in
tests/contract/test_sim_contract.py."""
from __future__ import annotations

import httpx
import pytest

from controller.simclient import SimClient
from controller.simclient import errors as E


def _client(handler, **kw) -> SimClient:
    return SimClient("http://sim", transport=httpx.MockTransport(handler), sleep=lambda s: None,
                     **kw)


STATE = {"scenario": "x", "sim_minute": 0, "ts": "2027-01-04T00:00:00+05:30", "end_minute": 1440,
         "finished": False, "tick_minutes": 15, "credits_remaining": 200.0, "chunks": 1,
         "last_event_id": 10}


def test_get_retries_5xx_and_transport_errors_then_succeeds():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=req)
        if calls["n"] == 2:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json=STATE)

    retries = []
    c = _client(handler, on_retry=lambda a, el, e: retries.append((a, type(e).__name__)))
    assert c.state().tick_minutes == 15
    assert calls["n"] == 3
    assert retries == [(1, "SimTransportError"), (2, "ServerError")]


def test_get_gives_up_after_the_budget():
    c = _client(lambda req: httpx.Response(500, json={"detail": "x"}), retry_budget_s=1e-9,
                retry_base_s=1.0)
    with pytest.raises(E.ServerError):
        c.state()


def test_advance_is_never_retried():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow", request=req)

    c = _client(handler)
    with pytest.raises(E.SimTimeout):
        c.advance(60, timeout_s=0.1)
    assert calls["n"] == 1


def test_posts_are_single_attempt_even_on_5xx():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway")

    c = _client(handler)
    with pytest.raises(E.ServerError):
        c.apply("wf", "apr_00001")
    assert calls["n"] == 1


@pytest.mark.parametrize("status,body,cls", [
    (400, {"error": "bad_minutes", "detail": "m"}, E.BadMinutes),
    (402, {"error": "out_of_credits", "detail": {"needed": 3}}, E.OutOfCredits),
    (403, {"error": "auto_apply_not_permitted", "detail": ["dims not permitted"]},
     E.AutoApplyNotPermitted),
    (404, {"error": "unknown_workflow", "detail": "zz"}, E.UnknownWorkflow),
    (404, {"error": "not_found", "detail": "apr_9"}, E.ApprovalNotFound),
    (409, {"error": "duplicate_proposal_id", "detail": "p"}, E.DuplicateProposalId),
    (409, {"error": "stale_base_version", "detail": {"current_version": 4}}, E.StaleBaseVersion),
    (409, {"error": "not_approved", "detail": {"status": "applied"}}, E.NotApproved),
    (409, {"error": "finished", "detail": "end"}, E.Finished),
    (410, {"error": "approval_lapsed", "detail": "late"}, E.ApprovalLapsed),
    (422, {"error": "invalid_config", "detail": ["bad"]}, E.InvalidConfig),
    (422, {"error": "missing_fields", "detail": ["x"]}, E.MissingFields),
    (422, {"error": "no_change", "detail": "same"}, E.NoChange),
    (422, {"error": "bad_prompt_version", "detail": "P9"}, E.BadPromptVersion),
    (422, {"error": "bad_n", "detail": "n"}, E.BadN),
    (422, {"detail": [{"loc": ["body"], "msg": "x"}]}, E.RequestValidation),
    (418, {"error": "teapot", "detail": "?"}, E.ApiError),
])
def test_error_mapping(status, body, cls):
    c = _client(lambda req: httpx.Response(status, json=body))
    with pytest.raises(cls) as ei:
        c.request_approval({"proposal_id": "p"})
    assert type(ei.value) is cls and ei.value.status == status
    if cls is E.StaleBaseVersion:
        assert ei.value.current_version == 4
    if cls is E.NotApproved:
        assert ei.value.approval_status == "applied"
    if cls is E.AutoApplyNotPermitted:
        assert ei.value.problems == ["dims not permitted"]


def test_stream_chunk_retries_before_the_first_byte_only():
    calls = {"n": 0}
    data = b'{"type":"lead","event_id":1}\n' * 1000

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, content=data)

    c = _client(handler, chunk_size=4096)
    assert b"".join(c.stream_chunk(0)) == data and calls["n"] == 2
    missing = _client(lambda req: httpx.Response(404, json={"detail": "no such chunk"}))
    with pytest.raises(E.ChunkNotFound):
        list(missing.stream_chunk(99))
