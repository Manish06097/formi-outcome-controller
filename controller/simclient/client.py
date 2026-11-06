"""SimClient: the only way the controller talks to the simulator (HTTP, httpx).

Owner: M0. Implements DESIGN.md 11.2 (interface), 8.1 and 8.4 (retry semantics):
- Idempotent GETs retry on transport errors and 5xx with exponential backoff (0.5 s to 30 s,
  jitter), forever by default; `on_retry` lets the orchestrator page after 5 minutes.
- `advance()` is NEVER retried: a timeout may hide an advance that ran. It raises SimTimeout
  and the caller reconciles with `state()` (8.3).
- Other POSTs and the PUT are not retried here either; the caller reconciles by key
  (proposal_id, approval_id) through the write-ahead intent protocol (8.2).
- `stream_chunk()` yields the chunk's bytes as they arrive (never the whole chunk in memory);
  it retries only before the first byte. A failure mid-stream raises, and the ingest
  transaction for that chunk rolls back and starts over.
Errors are typed per (status, code) in `errors.py`.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Iterator, Mapping

import httpx

from controller.simclient.errors import (ApiError, ServerError, SimTimeout, SimTransportError,
                                         error_from_response)
from controller.types import (AdvanceResult, ApplyResult, ApprovalPublic, ApprovalRequest,
                              AutoApplyRequest, ChunkMeta, ConfigSnapshot, SimState, Suggestion,
                              ValidationRun, VersionMeta, WorkflowSpec)

log = logging.getLogger(__name__)

RetryHook = Callable[[int, float, BaseException], None]
"""on_retry(attempt, elapsed_s, error), called before each retry sleep."""


class SimClient:
    def __init__(self, base_url: str, *, timeout_s: float = 30.0,
                 advance_timeout_s: float = 120.0, retry_base_s: float = 0.5,
                 retry_max_s: float = 30.0, retry_budget_s: float = 0.0,
                 on_retry: RetryHook | None = None, chunk_size: int = 1 << 20,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.advance_timeout_s = advance_timeout_s
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.retry_budget_s = retry_budget_s
        self.on_retry = on_retry
        self.chunk_size = chunk_size
        self._sleep = sleep
        self._rng = random.Random()          # jitter only; never used for decisions
        self._http = httpx.Client(base_url=self.base_url, transport=transport,
                                  timeout=self._timeout(timeout_s))

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> "SimClient":
        e = settings.env
        return cls(e.sim_url, timeout_s=e.http_timeout_s,
                   advance_timeout_s=e.advance_timeout_min_s, retry_base_s=e.get_retry_base_s,
                   retry_max_s=e.get_retry_max_s, retry_budget_s=e.get_retry_budget_s, **kw)

    @staticmethod
    def _timeout(read_s: float) -> httpx.Timeout:
        return httpx.Timeout(read_s, connect=min(10.0, read_s))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SimClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ================================================================ transport
    def _raise_for(self, r: httpx.Response, method: str, path: str) -> None:
        if r.status_code < 400:
            return
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text
        raise error_from_response(r.status_code, body, method, path)

    def _backoff(self, attempt: int, started: float, err: BaseException) -> None:
        """Sleep before retry `attempt` (1-based), or re-raise when the budget is spent."""
        elapsed = time.monotonic() - started
        delay = min(self.retry_max_s, self.retry_base_s * 2 ** (attempt - 1))
        delay *= 0.5 + 0.5 * self._rng.random()
        if self.retry_budget_s > 0 and elapsed + delay > self.retry_budget_s:
            raise err
        if self.on_retry:
            self.on_retry(attempt, elapsed, err)
        log.warning("simulator GET failed (%s); retry %d in %.1fs", err, attempt, delay)
        self._sleep(delay)

    @staticmethod
    def _transport_error(method: str, path: str, e: httpx.TransportError) -> SimTransportError:
        if isinstance(e, httpx.TimeoutException):
            return SimTimeout(method, path, e)
        return SimTransportError(method, path, e)

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Idempotent GET with retry on transport errors and 5xx."""
        started, attempt = time.monotonic(), 0
        while True:
            attempt += 1
            try:
                r = self._http.get(path, params=params)
                self._raise_for(r, "GET", path)
                return r.json()
            except httpx.TransportError as e:
                err: BaseException = self._transport_error("GET", path, e)
            except ServerError as e:
                err = e
            self._backoff(attempt, started, err)

    def _send(self, method: str, path: str, body: Mapping[str, Any],
              timeout_s: float | None = None) -> Any:
        """A non-idempotent request: exactly one attempt."""
        try:
            r = self._http.request(method, path, json=body,
                                   timeout=self._timeout(timeout_s or self.timeout_s))
        except httpx.TransportError as e:
            raise self._transport_error(method, path, e) from e
        self._raise_for(r, method, path)
        return r.json()

    # ================================================================ health and time
    def health(self) -> bool:
        """One probe of GET /health, no retry."""
        try:
            return self._http.get("/health", timeout=self._timeout(5.0)).status_code == 200
        except httpx.TransportError:
            return False

    def wait_healthy(self, timeout_s: float, poll_s: float = 2.0) -> None:
        """Poll /health until it answers (the simulator builds history before it listens)."""
        deadline = time.monotonic() + timeout_s
        while not self.health():
            if time.monotonic() >= deadline:
                raise SimTransportError("GET", "/health", TimeoutError(f"not healthy after {timeout_s}s"))
            self._sleep(poll_s)

    def state(self) -> SimState:
        return SimState.from_api(self._get("/sim/state"))

    def advance(self, minutes: int, timeout_s: float | None = None) -> AdvanceResult:
        """POST /sim/advance. Never retried. SimTimeout means "unknown": reconcile (8.3)."""
        d = self._send("POST", "/sim/advance", {"minutes": int(minutes)},
                       timeout_s=timeout_s or self.advance_timeout_s)
        return AdvanceResult.from_api(d)

    # ================================================================ events
    def list_chunks(self, kind: str = "events", after: int = -1) -> list[ChunkMeta]:
        return [ChunkMeta.from_api(c) for c in
                self._get("/events/chunks", {"kind": kind, "after": after})]

    def stream_chunk(self, n: int) -> Iterator[bytes]:
        """Yield chunk n's NDJSON bytes in pieces of about `chunk_size`."""
        path = f"/events/chunks/{n}"
        started, attempt = time.monotonic(), 0
        while True:
            attempt += 1
            yielded = False
            try:
                with self._http.stream("GET", path) as r:
                    if r.status_code >= 400:
                        r.read()
                        self._raise_for(r, "GET", path)
                    for piece in r.iter_bytes(self.chunk_size):
                        yielded = True
                        yield piece
                return
            except httpx.TransportError as e:
                err: BaseException = self._transport_error("GET", path, e)
                if yielded:
                    raise err from e
            except ServerError as e:
                err = e
            self._backoff(attempt, started, err)

    # ================================================================ workflows and configs
    def workflows(self) -> list[WorkflowSpec]:
        return [WorkflowSpec.from_api(w) for w in self._get("/workflows")]

    def workflow(self, wf: str) -> WorkflowSpec:
        return WorkflowSpec.from_api(self._get(f"/workflows/{wf}"))

    def config(self, wf: str) -> ConfigSnapshot:
        return ConfigSnapshot.from_api(self._get(f"/workflows/{wf}/config"))

    def config_history(self, wf: str) -> list[VersionMeta]:
        return [VersionMeta.from_api(v) for v in self._get(f"/workflows/{wf}/config/history")]

    # ================================================================ governance
    def request_approval(self, body: ApprovalRequest | Mapping[str, Any]) -> ApprovalPublic:
        """POST /approvals. The proposal id is consumed whenever a record is created, even if it
        will be rejected; synchronous 4xx errors create no record (UNDERSTANDING.md 4)."""
        payload = body.body() if isinstance(body, ApprovalRequest) else dict(body)
        return ApprovalPublic.from_api(self._send("POST", "/approvals", payload))

    def approvals(self, wf: str | None = None, status: str | None = None) -> list[ApprovalPublic]:
        params = {k: v for k, v in (("workflow_id", wf), ("status", status)) if v}
        return [ApprovalPublic.from_api(a) for a in self._get("/approvals", params)]

    def approval(self, aid: str) -> ApprovalPublic:
        return ApprovalPublic.from_api(self._get(f"/approvals/{aid}"))

    def apply(self, wf: str, aid: str) -> ApplyResult:
        """POST /workflows/{wf}/apply. Retry-safe by contract (an applied approval returns 409
        not_approved), but still a single attempt here; the intent executor decides."""
        return ApplyResult.from_api(self._send("POST", f"/workflows/{wf}/apply",
                                               {"approval_id": aid}))

    def auto_apply(self, wf: str, body: AutoApplyRequest | Mapping[str, Any]) -> ApplyResult:
        """PUT /workflows/{wf}/config. The proposal id is consumed only on success."""
        payload = body.body() if isinstance(body, AutoApplyRequest) else dict(body)
        return ApplyResult.from_api(self._send("PUT", f"/workflows/{wf}/config", payload))

    # ================================================================ credits
    def validate(self, wf: str, prompt: str, n: int) -> ValidationRun:
        """POST /validation/runs (ceil(n/100) credits). No idempotency: resolved through the
        credit balance after a crash (8.3)."""
        return ValidationRun.from_api(self._send(
            "POST", "/validation/runs", {"workflow_id": wf, "prompt_version": prompt, "n": int(n)}))

    def suggest_fixes(self, wf: str, problem: str) -> list[Suggestion]:
        """POST /llm/suggest-fixes (3 credits). The LLM only proposes candidates (constraint 4);
        called once per hallucination incident (DESIGN.md 17.1)."""
        d = self._send("POST", "/llm/suggest-fixes", {"workflow_id": wf, "problem": problem})
        return [Suggestion.from_api(s) for s in d.get("suggestions") or []]


__all__ = ["SimClient", "ApiError"]
