"""Typed simulator errors, one class per (status, code) of the contract.

Owner: M0. Implements DESIGN.md 8.4 (every response the controller must handle) over the error
shapes of the simulator: `{"error": code, "detail": ...}` from its ApiError handler, and
FastAPI's `{"detail": ...}` for HTTPException and request validation.

Callers branch on the class, never on message text:

    try:
        client.apply(wf, aid)
    except StaleBaseVersion as e:      # 409: the base changed after we proposed
        ...e.current_version
    except NotApproved as e:           # 409: already applied (a retried apply), or not approved
        ...e.approval_status
"""
from __future__ import annotations

from typing import Any


class SimError(Exception):
    """Any failure talking to the simulator."""


class SimTransportError(SimError):
    """No HTTP response: connection refused or reset, DNS, protocol error."""

    def __init__(self, method: str, path: str, cause: BaseException):
        super().__init__(f"{method} {path}: {type(cause).__name__}: {cause}")
        self.method, self.path, self.cause = method, path, cause


class SimTimeout(SimTransportError):
    """No response within the timeout. On advance this is NOT a failure of the advance: it may
    have run. Reconcile with GET /sim/state (DESIGN.md 8.3); never retry blindly."""


class ApiError(SimError):
    """An HTTP error response. `code` is the simulator's error code (or a synthetic one)."""

    status: int = 0
    code: str = ""

    def __init__(self, status: int, code: str, detail: Any, method: str = "", path: str = ""):
        super().__init__(f"{status} {code}: {detail!r} ({method} {path})".rstrip())
        self.status, self.code, self.detail = status, code, detail
        self.method, self.path = method, path


# ---------------------------------------------------------------- by status
class BadRequest(ApiError):
    status = 400


class PaymentRequired(ApiError):
    status = 402


class Forbidden(ApiError):
    status = 403


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


class Gone(ApiError):
    status = 410


class Unprocessable(ApiError):
    status = 422


class ServerError(ApiError):
    """5xx: retried for idempotent GETs, reconciled by key for POSTs (8.4)."""

    status = 500


# ---------------------------------------------------------------- by (status, code)
class BadMinutes(BadRequest):
    """400 bad_minutes: minutes not a positive multiple of tick. A bug (we read tick)."""

    code = "bad_minutes"


class OutOfCredits(PaymentRequired):
    """402 out_of_credits: page; stop non-incident spend (7.2)."""

    code = "out_of_credits"


class AutoApplyNotPermitted(Forbidden):
    """403 auto_apply_not_permitted: the local policy replica was wrong. Never retry the body.
    The proposal id was NOT consumed. `problems` lists the simulator's reasons."""

    code = "auto_apply_not_permitted"

    @property
    def problems(self) -> list[str]:
        return list(self.detail) if isinstance(self.detail, list) else [str(self.detail)]


class UnknownWorkflow(NotFound):
    code = "unknown_workflow"


class ApprovalNotFound(NotFound):
    """404 not_found on /approvals/{id} or /workflows/{id}/apply. After a restart an unknown
    approval means a new world (8.5)."""

    code = "not_found"


class ChunkNotFound(NotFound):
    code = "no_such_chunk"


class DuplicateProposalId(Conflict):
    """409 duplicate_proposal_id: the id was consumed earlier (an approval record exists, or an
    auto-apply succeeded). A reconciliation signal, never a new request (8.1)."""

    code = "duplicate_proposal_id"


class StaleBaseVersion(Conflict):
    """409 stale_base_version: at request time no record is created; at apply time the
    simulator logs an apply_conflict. `current_version` is the simulator's version now."""

    code = "stale_base_version"

    @property
    def current_version(self) -> int | None:
        d = self.detail
        return int(d["current_version"]) if isinstance(d, dict) and "current_version" in d else None


class NotApproved(Conflict):
    """409 not_approved: the approval is not in status approved (e.g. already applied: a retried
    apply cannot double-apply). `status` is the approval's status."""

    code = "not_approved"

    @property
    def approval_status(self) -> str | None:
        d = self.detail
        return str(d["status"]) if isinstance(d, dict) and "status" in d else None


class Finished(Conflict):
    """409 finished: the scenario has ended; final ingest and reconcile, exit 0."""

    code = "finished"


class ApprovalLapsed(Gone):
    """410 approval_lapsed: applied after apply_by. Counted by the grader; should be impossible
    because we check apply_by - tick first (6.1)."""

    code = "approval_lapsed"


class InvalidConfig(Unprocessable):
    code = "invalid_config"

    @property
    def problems(self) -> list[str]:
        return list(self.detail) if isinstance(self.detail, list) else [str(self.detail)]


class MissingFields(Unprocessable):
    code = "missing_fields"


class NoChange(Unprocessable):
    """422 no_change: the proposed config equals the current one (approvals only)."""

    code = "no_change"


class BadPromptVersion(Unprocessable):
    code = "bad_prompt_version"


class BadN(Unprocessable):
    code = "bad_n"


class RequestValidation(Unprocessable):
    """FastAPI rejected the request shape before the simulator saw it."""

    code = "request_validation"


_BY_CODE: dict[tuple[int, str], type[ApiError]] = {
    (cls.status, cls.code): cls for cls in (
        BadMinutes, OutOfCredits, AutoApplyNotPermitted, UnknownWorkflow, ApprovalNotFound,
        ChunkNotFound, DuplicateProposalId, StaleBaseVersion, NotApproved, Finished,
        ApprovalLapsed, InvalidConfig, MissingFields, NoChange, BadPromptVersion, BadN,
        RequestValidation)
}
_BY_STATUS: dict[int, type[ApiError]] = {
    400: BadRequest, 402: PaymentRequired, 403: Forbidden, 404: NotFound, 409: Conflict,
    410: Gone, 422: Unprocessable}


def error_from_response(status: int, body: Any, method: str = "", path: str = "") -> ApiError:
    """Map a status and decoded JSON body (or text) to the most specific error class."""
    code, detail = "", body
    if isinstance(body, dict) and "error" in body:
        code, detail = str(body["error"]), body.get("detail")
    elif isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        if status == 422 and isinstance(detail, list):
            code = "request_validation"
        elif status == 404 and path.startswith("/events/chunks/"):
            code = "no_such_chunk"
        else:
            code = "http_error"
    cls = _BY_CODE.get((status, code))
    if cls is None:
        cls = ServerError if status >= 500 else _BY_STATUS.get(status, ApiError)
    return cls(status, code or "http_error", detail, method, path)
