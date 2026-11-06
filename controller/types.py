"""Value types and interfaces shared by every track: the M0 contract.

Owner: M0. Implements DESIGN.md section 11.2 (public interfaces), with the simulator's wire types
from docs/SIMULATOR.md and the persisted shapes of section 3.2. After M0 no track edits this file;
contract changes go to the lead (docs/design/BUILD_BRIEF.md, "Ownership"). The track requests were
merged in step I1 (ADR 0028).

Rules every type here follows:
- Frozen dataclasses. Mappings are `FrozenDict` (a read-only, hashable, picklable dict), sequences
  are tuples, so values can be cached, hashed and compared.
- No I/O. Constructors named `from_api` parse simulator JSON; `body()` / `to_json()` produce JSON.
- Minutes are simulated minutes since D0 00:00 (negative in history), as the simulator counts them.

The one type with real behaviour is `Config`, the simulator config body. It normalises exactly like
the simulator (`sim/world.py:normalise_config`, mirrored, never imported) and answers the questions
the policy replica needs: `dist()` with the default fallback, `changed_dims()` exactly as the
simulator computes it, `body()` that the simulator stores bit-for-bit, and a stable `hash()`.
"""
from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import (TYPE_CHECKING, Any, Iterable, Iterator, Literal, Mapping, Protocol, Sequence,
                    TypeVar, runtime_checkable)

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

    Tx = psycopg.Connection[Any]
else:
    Tx = Any

# =============================================================================== aliases and enums

Cohort = str
"""A cohort key, e.g. "age60p_ta~3fa2c1" (DESIGN.md 5.2); "default" for the default cohort."""

Minute = int
"""Simulated minute since D0 00:00 (negative before D0)."""

Dim = Literal["welcome_message", "voice", "caller_id", "prompt_version", "schedule", "cohorts"]
AgentDim = Literal["welcome_message", "voice", "caller_id"]

AGENT_DIMS: tuple[str, ...] = ("welcome_message", "voice", "caller_id")
"""Dims sampled per dial from a cohort distribution (SIMULATOR.md 3)."""
CHANGE_DIMS: tuple[str, ...] = ("cohorts", "welcome_message", "voice", "caller_id",
                                "prompt_version", "schedule")
"""Dims reported in `changed_dims`, in the simulator's order (SIMULATOR.md 4.2)."""
CUSTOMER_FACING_DIMS: frozenset[str] = frozenset({"welcome_message", "voice", "prompt_version"})
"""Customer-facing dims (DESIGN.md 6.5); caller ID and schedule are operational."""
OPERATIONAL_DIMS: frozenset[str] = frozenset({"caller_id", "schedule"})
FEATURE_NAMES: tuple[str, ...] = ("lang", "city_tier", "age_band", "value_band")
"""Lead features on `lead` events (SIMULATOR.md 2). Their values are learned from events."""
DEFAULT_COHORT: Cohort = "default"

# Priority classes (DESIGN.md 6.2) and the fixed P0 order.
P0_INCIDENT, P1_ROLLBACK, P2_PROMOTION, P3_ROUTINE = 0, 1, 2, 3
PriorityClass = Literal[0, 1, 2, 3]
P0A_PROMPT_SAFETY, P0B_DID_DRAIN = 0, 1
"""`PatchRequest.p0_rank`: 0 = P0a prompt safety before 1 = P0b DID drain."""

ProposalState = Literal[
    "draft", "discarded", "submitting", "noticed", "auto_applying", "pending", "approved",
    "applying", "applied", "verifying", "verified", "rollback", "rejected", "expired", "stale",
    "lapsed_local", "superseded", "abandoned", "failed", "failed_policy"]
PROPOSAL_STATES: frozenset[str] = frozenset(ProposalState.__args__)  # type: ignore[attr-defined]
IN_FLIGHT_STATES: frozenset[str] = frozenset(
    {"submitting", "pending", "approved", "applying", "noticed", "auto_applying"})
"""States covered by the `one_in_flight` partial unique index (DESIGN.md 3.2, 6.2)."""
REQUEUE_STATES: frozenset[str] = frozenset(
    {"rejected", "expired", "stale", "lapsed_local", "superseded", "abandoned"})
"""Terminal states whose still-wanted patches re-queue as a NEW proposal (DESIGN.md 6.1)."""

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "applied", "lapsed"]
IntentKind = Literal["advance", "submit_approval", "auto_apply", "apply", "validation",
                     "suggest_fixes"]
IntentStatus = Literal["started", "done", "failed", "aborted", "unknown"]
FaultKind = Literal["advance", "submit_approval", "auto_apply", "apply", "validation",
                    "suggest_fixes", "chunk_transform", "audit_write"]
FaultPhase = Literal["before_t1", "after_t1", "after_http", "in_t2"]
DidStatus = Literal["unknown", "probing", "healthy", "unhealthy", "degraded", "suspect"]
ArmState = Literal["live", "frozen", "removed", "futile", "winner", "control"]
WinnerState = Literal["declared", "promoted", "rolled_back"]
CohortOrigin = Literal["grid", "business", "supervisor", "segment"]
BodySource = Literal["ours", "fetched", "unknown"]
CreditPool = Literal["R", "S", "U"]
CreditMode = Literal["normal", "low"]
FitKind = Literal["stationary", "ew"]
VerdictMode = Literal["sequential", "full", "stage1_only"]
CustomerFacingAuto = Literal["none", "risk_reducing_only", "ramps_and_risk_reducing"]
RiskLabel = Literal["reducing", "increasing"]
KPI = Literal["pickup_rate", "engagement_rate"]
ProvenanceSource = Literal["default", "arm", "winner", "holdout", "probe", "supervisor", "rollback"]

# =============================================================================== frozen containers

K = TypeVar("K")
V = TypeVar("V")


class FrozenDict(dict):  # type: ignore[type-arg]
    """A read-only dict: hashable, picklable, JSON-serialisable (it is a dict subclass).

    Equality with a plain dict holds (`FrozenDict({"a": 1}) == {"a": 1}`), so code may compare
    against literals in tests.
    """

    __slots__ = ("_hash",)

    def _blocked(self, *_: Any, **__: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _blocked  # type: ignore[assignment]
    __ior__ = _blocked  # type: ignore[assignment]

    def __hash__(self) -> int:  # type: ignore[override]
        try:
            return self._hash  # type: ignore[no-any-return]
        except AttributeError:
            h = hash(frozenset(self.items()))
            object.__setattr__(self, "_hash", h)
            return h

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> "FrozenDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        return self

    def __repr__(self) -> str:
        return f"FrozenDict({dict.__repr__(self)})"

    def thaw(self) -> dict[Any, Any]:
        """A mutable deep copy (nested FrozenDicts and tuples become dicts and lists)."""
        return thaw(self)


def freeze(obj: Any) -> Any:
    """Deep-freeze JSON-like data: dict -> FrozenDict, list -> tuple, recursively."""
    if isinstance(obj, FrozenDict):
        return obj
    if isinstance(obj, Mapping):
        return FrozenDict({k: freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(freeze(v) for v in obj)
    return obj


def thaw(obj: Any) -> Any:
    """Inverse of `freeze`: FrozenDict -> dict, tuple -> list, recursively."""
    if isinstance(obj, Mapping):
        return {k: thaw(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [thaw(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys, no spaces, shortest round-trip floats) for hashing."""
    return json.dumps(thaw(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


# =============================================================================== simulated time

@dataclass(frozen=True)
class SimEpoch:
    """Converts between simulator timestamps and simulated minutes.

    `epoch0 = parse(state.ts) - state.sim_minute` (DESIGN.md 3.1), read from the API, never
    hard-coded. Simulator timestamps look like "2027-01-04T10:00:00+05:30".
    """

    epoch0: _dt.datetime

    @classmethod
    def from_state(cls, state: "SimState") -> "SimEpoch":
        t = _dt.datetime.fromisoformat(state.ts)
        return cls(t - _dt.timedelta(minutes=state.sim_minute))

    def minute(self, ts: str) -> Minute:
        delta = _dt.datetime.fromisoformat(ts) - self.epoch0
        return int(round(delta.total_seconds() / 60))

    def ts(self, minute: Minute) -> str:
        return (self.epoch0 + _dt.timedelta(minutes=minute)).isoformat(timespec="seconds")


def day_of(minute: Minute) -> int:
    """Simulated day index D (floor division, so history minutes give negative days)."""
    return minute // 1440


def label(minute: Minute) -> str:
    """Human label "D3 14:00" (the form used by the audit CLI and the supervisor view)."""
    d, m = divmod(minute, 1440)
    return f"D{d} {m // 60:02d}:{m % 60:02d}"


def make_proposal_id(run6: str, workflow_id: str, minute: Minute, cls: int, seq: int) -> str:
    """Deterministic proposal id `{run6}-{wf}-D{d}T{HHMM}-{class}-{seq}` (DESIGN.md 6.1)."""
    d, m = divmod(minute, 1440)
    return f"{run6}-{workflow_id}-D{d}T{m // 60:02d}{m % 60:02d}-{cls}-{seq:04d}"


def advance_key(from_minute: Minute, to_minute: Minute) -> str:
    """Idempotency key of an advance intent, `adv:<from>-><to>` (DESIGN.md 8.1)."""
    return f"adv:{from_minute}->{to_minute}"


# =============================================================================== simulator wire types

@dataclass(frozen=True)
class SimState:
    """`GET /sim/state`."""

    scenario: str
    sim_minute: Minute
    ts: str
    end_minute: Minute
    finished: bool
    tick_minutes: int
    credits_remaining: float
    chunks: int
    last_event_id: int

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "SimState":
        return cls(scenario=str(d["scenario"]), sim_minute=int(d["sim_minute"]), ts=str(d["ts"]),
                   end_minute=int(d["end_minute"]), finished=bool(d["finished"]),
                   tick_minutes=int(d["tick_minutes"]),
                   credits_remaining=float(d["credits_remaining"]), chunks=int(d["chunks"]),
                   last_event_id=int(d["last_event_id"]))


@dataclass(frozen=True)
class ChunkMeta:
    """One entry of `GET /events/chunks` or `advance().new_chunks`."""

    chunk: int
    kind: Literal["history", "events"]
    events: int
    first_event_id: int
    last_event_id: int
    until_minute: Minute
    until_ts: str
    path: str = ""
    """Simulator-internal path. Informational only: controller code never reads chunk files."""

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "ChunkMeta":
        return cls(chunk=int(d["chunk"]), kind=d["kind"], events=int(d["events"]),
                   first_event_id=int(d["first_event_id"]), last_event_id=int(d["last_event_id"]),
                   until_minute=int(d["until_minute"]), until_ts=str(d["until_ts"]),
                   path=str(d.get("path", "")))


@dataclass(frozen=True)
class AdvanceResult:
    """`POST /sim/advance`."""

    sim_minute: Minute
    ts: str
    finished: bool
    new_chunks: tuple[ChunkMeta, ...]

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "AdvanceResult":
        return cls(sim_minute=int(d["sim_minute"]), ts=str(d["ts"]), finished=bool(d["finished"]),
                   new_chunks=tuple(ChunkMeta.from_api(c) for c in d.get("new_chunks") or ()))


@dataclass(frozen=True)
class AutoApplyPolicy:
    enabled: bool
    allowed_dims: frozenset[str]
    max_shift: float

    @classmethod
    def from_api(cls, d: Mapping[str, Any] | None) -> "AutoApplyPolicy":
        d = d or {}
        return cls(enabled=bool(d.get("enabled", False)),
                   allowed_dims=frozenset(d.get("allowed_dims") or ()),
                   max_shift=float(d.get("max_shift", 0.0)))


@dataclass(frozen=True)
class GovernancePolicy:
    """The `governance` block of `GET /workflows/{id}` (SIMULATOR.md 4)."""

    auto_apply: AutoApplyPolicy
    approver_hours: tuple[int, int]
    approver_latency_minutes: tuple[int, int]
    proposal_ttl_hours: float
    apply_window_hours: float
    max_requests_per_24h: int
    base_reject_rate: float
    min_validation_pass_rate: float
    extra: FrozenDict = field(default_factory=FrozenDict)
    """Keys the API returned that this version does not model (forward compatibility)."""

    KNOWN = ("auto_apply", "approver_hours", "approver_latency_minutes", "proposal_ttl_hours",
             "apply_window_hours", "max_requests_per_24h", "base_reject_rate",
             "min_validation_pass_rate")

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "GovernancePolicy":
        ah, lat = d["approver_hours"], d["approver_latency_minutes"]
        return cls(auto_apply=AutoApplyPolicy.from_api(d.get("auto_apply")),
                   approver_hours=(int(ah[0]), int(ah[1])),
                   approver_latency_minutes=(int(lat[0]), int(lat[1])),
                   proposal_ttl_hours=float(d["proposal_ttl_hours"]),
                   apply_window_hours=float(d["apply_window_hours"]),
                   max_requests_per_24h=int(d["max_requests_per_24h"]),
                   base_reject_rate=float(d.get("base_reject_rate", 0.0)),
                   min_validation_pass_rate=float(d["min_validation_pass_rate"]),
                   extra=freeze({k: v for k, v in d.items() if k not in cls.KNOWN}))


@dataclass(frozen=True)
class Objective:
    """`objective`: the primary KPI and the guardrail on the other one (never blended)."""

    primary: str
    guardrail_kpi: str | None
    guardrail_max_drop: float | None
    raw: FrozenDict

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "Objective":
        g = d.get("guardrail") or {}
        kpi, drop = None, None
        for k, v in g.items():
            if k.endswith("_max_drop"):
                kpi, drop = k[: -len("_max_drop")], float(v)
                break
        return cls(primary=str(d["primary"]), guardrail_kpi=kpi, guardrail_max_drop=drop,
                   raw=freeze(d))


@dataclass(frozen=True)
class DailyBatch:
    size: int
    at_hour: int
    returning_share: float

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "DailyBatch":
        return cls(size=int(d["size"]), at_hour=int(d["at_hour"]),
                   returning_share=float(d.get("returning_share", 0.0)))


@dataclass(frozen=True)
class WorkflowSpec:
    """`GET /workflows/{id}`: everything the business owns plus the governance policy."""

    workflow_id: str
    tenant_id: str
    use_case: str
    languages: tuple[str, ...]
    dialling_window: tuple[int, int]
    """[start, end) hours of day."""
    dials_per_hour: int
    cadence: tuple[int, ...]
    min_gap_minutes: int
    daily_batch: DailyBatch
    mid_flight: str
    """"allowed" or "next_campaign_only"."""
    objective: Objective
    options: FrozenDict
    """dim -> tuple of option names, for welcome_message, voice, caller_id, prompt_version."""
    prompt_catalog: FrozenDict
    """prompt_version -> one-line summary."""
    governance: GovernancePolicy
    current_version: int
    raw: FrozenDict

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "WorkflowSpec":
        dw = d["dialling_window"]
        return cls(workflow_id=str(d["workflow_id"]), tenant_id=str(d["tenant_id"]),
                   use_case=str(d.get("use_case") or ""),
                   languages=tuple(d.get("languages") or ()),
                   dialling_window=(int(dw[0]), int(dw[1])),
                   dials_per_hour=int(d["dials_per_hour"]),
                   cadence=tuple(int(c) for c in d["cadence"]),
                   min_gap_minutes=int(d["min_gap_minutes"]),
                   daily_batch=DailyBatch.from_api(d["daily_batch"]),
                   mid_flight=str(d["mid_flight"]), objective=Objective.from_api(d["objective"]),
                   options=FrozenDict({k: tuple(v) for k, v in d["options"].items()}),
                   prompt_catalog=freeze(d.get("prompt_catalog") or {}),
                   governance=GovernancePolicy.from_api(d["governance"]),
                   current_version=int(d["current_version"]), raw=freeze(d))

    @property
    def dials_per_day(self) -> int:
        return self.dials_per_hour * (self.dialling_window[1] - self.dialling_window[0])

    def spec_hash(self) -> str:
        """Hash of everything but `current_version` (which changes with every config version)."""
        return sha256_hex({k: v for k, v in self.raw.items() if k != "current_version"})


# =============================================================================== config

class ConfigInvalid(ValueError):
    """A body the simulator would refuse with 422 invalid_config during normalisation."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class CohortDef:
    """One `cohorts[]` entry: AND across features, OR within a feature's list.

    `where` is stored sorted by feature name, each feature's values in the order given, so that
    equality is exactly the simulator's comparison (dict equality: key order irrelevant, list
    order relevant).
    """

    id: str
    where: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def make(cls, id: str, where: Mapping[str, Iterable[str]] | None = None) -> "CohortDef":
        w = where or {}
        return cls(id=id, where=tuple(sorted((k, tuple(v)) for k, v in w.items())))

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "CohortDef":
        return cls.make(str(d.get("id")), d.get("where") or {})

    def where_dict(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.where}

    def body(self) -> dict[str, Any]:
        return {"id": self.id, "where": self.where_dict()}

    def matches(self, features: Mapping[str, str]) -> bool:
        return all(features.get(f) in vals for f, vals in self.where)


def _sim_normalise(weights: Mapping[str, Any]) -> dict[str, float]:
    """`normalise_config` for one distribution: `float(w) / float(sum)`, zero weights dropped.

    The arithmetic is the simulator's, in its order, so the floats are bit-identical.
    """
    s = float(sum(weights.values()))
    return {o: float(w) / s for o, w in weights.items() if w > 0}


def _positive(weights: Mapping[str, Any]) -> FrozenDict:
    """Raw weights without zero entries. Adding 0.0 is exact, so the simulator's sum and
    therefore its normalised floats are unchanged; an unlisted zero-weight option is not sent."""
    return FrozenDict({o: w for o, w in weights.items() if w > 0})


def _ulp_steps(x: float, n: int) -> list[float]:
    out, up, down = [x], x, x
    for _ in range(n):
        up, down = math.nextafter(up, math.inf), math.nextafter(down, -math.inf)
        out += [up, down]
    return out


@functools.lru_cache(maxsize=4096)
def _preimage(stored: tuple[tuple[str, float], ...]) -> tuple[tuple[str, float], ...] | None:
    """Weights `u` with `_sim_normalise(u) == stored`, bit for bit, or None if none was found.

    Why this exists: the simulator re-normalises every body it receives. A stored distribution
    whose floats do not sum to exactly 1.0 (about 23% of normalised random weights) changes in
    its last bits when sent back unchanged, and the simulator then reports that dim as changed
    (a spurious `changed_dims` entry, a 403 if the dim is not auto-applicable). Searching scaled
    copies `u = v * T` for `T` within 64 ulps of 1, plus up to 3 ulps on the largest weight,
    finds a preimage for about 99.5% of such distributions (measured on 100k random cases).
    """
    v = dict(stored)
    if _sim_normalise(v) == v:
        return stored
    keys = list(v)
    jmax = max(keys, key=lambda k: v[k])
    for t in _ulp_steps(1.0, 64):
        u = {k: v[k] * t for k in keys}
        if _sim_normalise(u) == v:
            return tuple(u.items())
        for x in _ulp_steps(u[jmax], 3)[1:]:
            u2 = dict(u)
            u2[jmax] = x
            if _sim_normalise(u2) == v:
                return tuple(u2.items())
    return None


def _freeze_alloc(allocations: Mapping[str, Mapping[str, Mapping[str, float]]]) -> FrozenDict:
    return FrozenDict({k: FrozenDict({d: FrozenDict({o: float(w) for o, w in ws.items()})
                                      for d, ws in dims.items()})
                       for k, dims in allocations.items()})


@dataclass(frozen=True)
class Config:
    """A workflow config in the form the simulator STORES it (normalised, zero weights dropped).

    Constructors:
    - `Config.from_body(body)`: a body we intend to SEND. Normalised exactly like the simulator.
    - `Config.from_stored(cfg)`: the `config` of `GET /workflows/{id}/config` (already stored).
      Taken verbatim: re-normalising a stored config can change its last bits (see `_preimage`).

    Equality (`==`, `hash()`) is structural over the stored form. `changed_dims(other)` is the
    simulator's own diff, so `a.changed_dims(b) == []` is "the simulator sees no change" (for
    example an override equal to the default is no change, but is structurally different).
    """

    cohorts: tuple[CohortDef, ...]
    allocations: FrozenDict
    """cohort -> agent dim -> option -> weight (normalised). Must contain "default"."""
    prompt_version: str
    hour_weights: FrozenDict
    """cohort -> hour (int) -> weight. An empty table is kept: it differs from no table."""
    lead_preferred_hour: FrozenDict = field(default_factory=FrozenDict)
    """person_id -> hour."""
    wire: FrozenDict = field(default_factory=FrozenDict, compare=False, hash=False, repr=False)
    """(cohort, dim) -> raw weights we were given, whose simulator normalisation is the stored
    distribution. Used by `body()` so a config built from raw weights round-trips exactly."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cohorts", tuple(self.cohorts))
        if not isinstance(self.allocations, FrozenDict):
            object.__setattr__(self, "allocations", _freeze_alloc(self.allocations))
        if not isinstance(self.hour_weights, FrozenDict) or any(
                not isinstance(t, FrozenDict) for t in self.hour_weights.values()):
            object.__setattr__(self, "hour_weights", FrozenDict(
                {k: FrozenDict({int(h): float(w) for h, w in t.items()})
                 for k, t in self.hour_weights.items()}))
        if not isinstance(self.lead_preferred_hour, FrozenDict):
            object.__setattr__(self, "lead_preferred_hour", FrozenDict(
                {str(k): int(v) for k, v in self.lead_preferred_hour.items()}))
        if not isinstance(self.wire, FrozenDict):
            object.__setattr__(self, "wire", FrozenDict(self.wire))

    # ------------------------------------------------------------------ constructors
    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "Config":
        """Normalise a body exactly like `sim/world.py:normalise_config` (allocation weights
        `float(w) / float(sum)`, zero weights dropped; hour keys `int`, weights `float`;
        `lead_preferred_hour` entries kept only when the hour is an int).

        Raises ConfigInvalid for bodies the simulator cannot normalise (non-numeric or negative
        weights, weights summing to 0, bad hour keys). Policy checks that need the workflow spec
        (options, window, cohort count) are in `problems(spec)`.
        """
        problems: list[str] = []
        cohorts = []
        for i, c in enumerate(body.get("cohorts") or []):
            if not isinstance(c, Mapping):
                problems.append(f"cohorts[{i}] must be an object")
                continue
            cohorts.append(CohortDef.from_api(c))
        allocs: dict[str, dict[str, dict[str, float]]] = {}
        wire: dict[tuple[str, str], FrozenDict] = {}
        for key, dims in (body.get("allocations") or {}).items():
            allocs[key] = {}
            for dim, weights in (dims or {}).items():
                if not isinstance(weights, Mapping) or not weights:
                    problems.append(f"allocations.{key}.{dim}: must be a non-empty map")
                    continue
                if any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0
                       or (isinstance(w, float) and math.isnan(w)) for w in weights.values()):
                    problems.append(f"allocations.{key}.{dim}: weights must be >= 0")
                    continue
                if float(sum(weights.values())) <= 0:
                    problems.append(f"allocations.{key}.{dim}: weights sum to 0")
                    continue
                allocs[key][dim] = _sim_normalise(weights)
                wire[(key, dim)] = _positive(weights)
        sched = body.get("schedule") or {}
        hw: dict[str, dict[int, float]] = {}
        for key, table in (sched.get("hour_weights") or {}).items():
            clean: dict[int, float] = {}
            for h, w in (table or {}).items():
                try:
                    hh = int(h)
                except (TypeError, ValueError):
                    problems.append(f"schedule.hour_weights.{key}: bad hour {h!r}")
                    continue
                if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
                    problems.append(f"schedule.hour_weights.{key}.{h}: weight must be >= 0")
                    continue
                clean[hh] = float(w)
            hw[key] = clean
        lph = {str(k): int(v) for k, v in (sched.get("lead_preferred_hour") or {}).items()
               if isinstance(v, int) and not isinstance(v, bool)}
        if problems:
            raise ConfigInvalid(problems)
        pv = body.get("prompt_version")
        return cls(cohorts=tuple(cohorts), allocations=_freeze_alloc(allocs),
                   prompt_version=pv if isinstance(pv, str) else "", hour_weights=hw,
                   lead_preferred_hour=lph, wire=FrozenDict(wire))

    @classmethod
    def from_stored(cls, cfg: Mapping[str, Any]) -> "Config":
        """The config as the simulator stores and returns it (`GET .../config`), verbatim."""
        sched = cfg.get("schedule") or {}
        return cls(cohorts=tuple(CohortDef.from_api(c) for c in cfg.get("cohorts") or ()),
                   allocations=_freeze_alloc({k: dict(v or {}) for k, v in
                                              (cfg.get("allocations") or {}).items()}),
                   prompt_version=str(cfg.get("prompt_version") or ""),
                   hour_weights={k: dict(t or {}) for k, t in
                                 (sched.get("hour_weights") or {}).items()},
                   lead_preferred_hour=dict(sched.get("lead_preferred_hour") or {}))

    # ------------------------------------------------------------------ reading
    def cohort_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.cohorts)

    def dist(self, cohort: Cohort, dim: str) -> Mapping[str, float]:
        """The distribution a lead of `cohort` is dialled with: the cohort's own override for
        `dim`, else `default`'s (SIMULATOR.md 3; world.py `_dist`). For "prompt_version" the
        one workflow-wide prompt with weight 1."""
        if dim == "prompt_version":
            return FrozenDict({self.prompt_version: 1.0})
        if dim not in AGENT_DIMS:
            raise ValueError(f"dist() is defined for agent dims and prompt_version, not {dim!r}")
        own = (self.allocations.get(cohort) or {}).get(dim)
        return own or self.allocations[DEFAULT_COHORT][dim]

    def has_override(self, cohort: Cohort, dim: str) -> bool:
        return cohort != DEFAULT_COHORT and bool((self.allocations.get(cohort) or {}).get(dim))

    def hour_table(self, cohort: Cohort) -> Mapping[int, float] | None:
        """The hour table a cohort's leads are ranked by: its own, else default's, else None
        (weight 1 every hour). Hours missing from a table get weight 0 (SIMULATOR.md 3)."""
        if cohort in self.hour_weights:
            return self.hour_weights[cohort]
        return self.hour_weights.get(DEFAULT_COHORT)

    def cohort_of(self, features: Mapping[str, str]) -> Cohort:
        """First matching cohort, else "default"."""
        for c in self.cohorts:
            if c.matches(features):
                return c.id
        return DEFAULT_COHORT

    # ------------------------------------------------------------------ diff (simulator's own)
    def changed_dims(self, new: "Config") -> list[str]:
        """Exactly `sim/world.py:changed_dims(self, new)`: cohort list compared in order; each
        agent dim compared through `dist()` over the union of allocation keys; prompt; any
        schedule difference (including `lead_preferred_hour`). Returned in CHANGE_DIMS order."""
        ch: set[str] = set()
        if self.cohorts != new.cohorts:
            ch.add("cohorts")
        keys = set(self.allocations) | set(new.allocations)
        for dim in AGENT_DIMS:
            if any(self.dist(k, dim) != new.dist(k, dim) for k in keys):
                ch.add(dim)
        if self.prompt_version != new.prompt_version:
            ch.add("prompt_version")
        if (self.hour_weights != new.hour_weights
                or self.lead_preferred_hour != new.lead_preferred_hour):
            ch.add("schedule")
        return [d for d in CHANGE_DIMS if d in ch]

    def equivalent(self, other: "Config") -> bool:
        """True iff the simulator would see no change between the two configs."""
        return not self.changed_dims(other)

    # ------------------------------------------------------------------ writing
    def _wire_weights(self, cohort: str, dim: str, stored: Mapping[str, float]) -> dict[str, float]:
        raw = self.wire.get((cohort, dim))
        if raw is not None and _sim_normalise(raw) == stored:
            return dict(raw)
        pre = _preimage(tuple(stored.items()))
        return dict(pre) if pre is not None else dict(stored)

    def body(self) -> dict[str, Any]:
        """The JSON body for `proposed_config`. The simulator's normalisation of it equals this
        config bit for bit whenever `wire_exact()` is True (see `_preimage`)."""
        return {
            "cohorts": [c.body() for c in self.cohorts],
            "allocations": {k: {d: self._wire_weights(k, d, ws) for d, ws in dims.items()}
                            for k, dims in self.allocations.items()},
            "prompt_version": self.prompt_version,
            "schedule": {
                "hour_weights": {k: {str(h): w for h, w in t.items()}
                                 for k, t in self.hour_weights.items()},
                "lead_preferred_hour": dict(self.lead_preferred_hour)},
        }

    def stored(self) -> dict[str, Any]:
        """The stored form as JSON (what `GET .../config` returns for this config)."""
        return {
            "cohorts": [c.body() for c in self.cohorts],
            "allocations": {k: {d: dict(ws) for d, ws in dims.items()}
                            for k, dims in self.allocations.items()},
            "prompt_version": self.prompt_version,
            "schedule": {
                "hour_weights": {k: {str(h): w for h, w in t.items()}
                                 for k, t in self.hour_weights.items()},
                "lead_preferred_hour": dict(self.lead_preferred_hour)},
        }

    def as_stored(self) -> "Config":
        """What the simulator will store if sent `body()`. The policy replica compares the live
        config with THIS, never with `self`, so a last-bit drift can never surprise it."""
        return Config.from_body(self.body())

    def wire_exact(self) -> bool:
        return self.as_stored() == self

    def hash(self) -> str:
        """sha256 of the canonical JSON of the stored form (`config_version.config_hash`)."""
        return sha256_hex(self.stored())

    # ------------------------------------------------------------------ building
    def with_allocation(self, cohort: Cohort, dim: str, weights: Mapping[str, float]) -> "Config":
        """A copy with `allocations[cohort][dim]` set from raw weights, normalised like the
        simulator. Empty `weights` removes the override (the cohort falls back to default)."""
        allocs = {k: dict(v) for k, v in self.allocations.items()}
        wire = dict(self.wire)
        if weights:
            allocs.setdefault(cohort, {})[dim] = _sim_normalise(weights)
            wire[(cohort, dim)] = _positive(weights)
        else:
            allocs.get(cohort, {}).pop(dim, None)
            wire.pop((cohort, dim), None)
        return replace(self, allocations=_freeze_alloc(allocs), wire=FrozenDict(wire))

    def with_prompt(self, prompt_version: str) -> "Config":
        return replace(self, prompt_version=prompt_version)

    def with_cohorts(self, cohorts: Sequence[CohortDef]) -> "Config":
        return replace(self, cohorts=tuple(cohorts))

    def with_hour_weights(self, cohort: Cohort, table: Mapping[int, float] | None) -> "Config":
        """Set (or with None remove) one cohort's hour table."""
        hw = {k: dict(t) for k, t in self.hour_weights.items()}
        if table is None:
            hw.pop(cohort, None)
        else:
            hw[cohort] = {int(h): float(w) for h, w in table.items()}
        return replace(self, hour_weights=FrozenDict(
            {k: FrozenDict(t) for k, t in hw.items()}))

    # ------------------------------------------------------------------ local validation
    def problems(self, spec: WorkflowSpec, max_cohorts: int = 20) -> list[str]:
        """The 422 checks of `normalise_config` that need the workflow spec. Empty: valid.

        `max_cohorts` is the contract cap of SIMULATOR.md 3 (settings.Thresholds.max_cohorts).
        Feature VALUES are not checked: the controller learns them from lead events.
        """
        p: list[str] = []
        ids: list[str] = []
        for i, c in enumerate(self.cohorts):
            if not c.id or c.id == DEFAULT_COHORT or c.id in ids:
                p.append(f"cohorts[{i}].id must be a unique non-empty string other than 'default'")
                continue
            for f, vals in c.where:
                if f not in FEATURE_NAMES:
                    p.append(f"cohort {c.id}: unknown feature {f}")
                elif not vals:
                    p.append(f"cohort {c.id}: empty value list for {f}")
            ids.append(c.id)
        if len(ids) > max_cohorts:
            p.append(f"at most {max_cohorts} cohorts")
        if DEFAULT_COHORT not in self.allocations:
            p.append("allocations.default is required")
        for key, dims in self.allocations.items():
            if key != DEFAULT_COHORT and key not in ids:
                p.append(f"allocations.{key}: not a defined cohort")
                continue
            for dim, ws in dims.items():
                if dim not in AGENT_DIMS:
                    p.append(f"allocations.{key}.{dim}: unknown dimension")
                    continue
                bad = [o for o in ws if o not in spec.options.get(dim, ())]
                if bad:
                    p.append(f"allocations.{key}.{dim}: options not allowed: {bad}")
            if key == DEFAULT_COHORT:
                missing = [d for d in AGENT_DIMS if d not in dims]
                if missing:
                    p.append(f"allocations.default missing dims {missing}")
        if self.prompt_version not in spec.options.get("prompt_version", ()):
            p.append(f"prompt_version {self.prompt_version!r} not allowed")
        lo, hi = spec.dialling_window
        for key, table in self.hour_weights.items():
            if key != DEFAULT_COHORT and key not in ids:
                p.append(f"schedule.hour_weights.{key}: not a defined cohort")
                continue
            for h in table:
                if not lo <= h < hi:
                    p.append(f"schedule.hour_weights.{key}: hour {h} outside [{lo},{hi})")
        if len(self.lead_preferred_hour) > 200_000:
            p.append("schedule.lead_preferred_hour: at most 200k entries")
        if any(not lo <= h < hi for h in self.lead_preferred_hour.values()):
            p.append("schedule.lead_preferred_hour: hour outside dialling window")
        return p


@dataclass(frozen=True)
class ConfigSnapshot:
    """`GET /workflows/{id}/config`."""

    workflow_id: str
    version: int
    actor: str
    applied_at: str | None
    config: Config

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "ConfigSnapshot":
        return cls(workflow_id=str(d["workflow_id"]), version=int(d["version"]),
                   actor=str(d["actor"]), applied_at=d.get("applied_at"),
                   config=Config.from_stored(d["config"]))


@dataclass(frozen=True)
class VersionMeta:
    """One row of `GET /workflows/{id}/config/history` (no config body: we keep bodies)."""

    version: int
    actor: str
    """business_default | supervisor | controller_approved | controller_auto"""
    changed_dims: tuple[str, ...]
    applied_at: str | None
    proposal_id: str | None
    approval_id: str | None

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "VersionMeta":
        return cls(version=int(d["version"]), actor=str(d["actor"]),
                   changed_dims=tuple(d.get("changed_dims") or ()), applied_at=d.get("applied_at"),
                   proposal_id=d.get("proposal_id"), approval_id=d.get("approval_id"))


@dataclass(frozen=True)
class ApprovalRequest:
    """`POST /approvals` body (SIMULATOR.md 4.1)."""

    proposal_id: str
    workflow_id: str
    base_version: int
    proposed_config: Config
    change_summary: str
    diff: Mapping[str, Any]
    affected_leads: int
    dry_run: Mapping[str, Any]
    impact_score: float
    rationale: str
    evidence: Mapping[str, Any] = field(default_factory=FrozenDict)
    expires_in_hours: float | None = None
    """Left at None (the policy TTL), per DESIGN.md 6.3 (F11)."""

    def body(self) -> dict[str, Any]:
        b: dict[str, Any] = {
            "proposal_id": self.proposal_id, "workflow_id": self.workflow_id,
            "base_version": self.base_version, "proposed_config": self.proposed_config.body(),
            "change_summary": self.change_summary, "diff": thaw(self.diff),
            "affected_leads": int(self.affected_leads), "dry_run": thaw(self.dry_run),
            "impact_score": float(self.impact_score), "rationale": self.rationale,
            "evidence": thaw(self.evidence)}
        if self.expires_in_hours is not None:
            b["expires_in_hours"] = self.expires_in_hours
        return b


@dataclass(frozen=True)
class ApprovalPublic:
    """An approval as `GET /approvals[/{id}]` returns it. Reasons and `decided_at` appear only once
    decided; `apply_by` only while `approved`. A lapse is lazy: an approval past `apply_by` still
    reads `approved` until an apply is attempted (UNDERSTANDING.md 4)."""

    approval_id: str
    proposal_id: str
    workflow_id: str
    status: str
    base_version: int
    changed_dims: tuple[str, ...]
    created_at: str
    expires_at: str
    decided_at: str | None = None
    reasons: tuple[str, ...] = ()
    apply_by: str | None = None

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "ApprovalPublic":
        return cls(approval_id=str(d["approval_id"]), proposal_id=str(d["proposal_id"]),
                   workflow_id=str(d["workflow_id"]), status=str(d["status"]),
                   base_version=int(d["base_version"]),
                   changed_dims=tuple(d.get("changed_dims") or ()),
                   created_at=str(d["created_at"]), expires_at=str(d["expires_at"]),
                   decided_at=d.get("decided_at"), reasons=tuple(d.get("reasons") or ()),
                   apply_by=d.get("apply_by"))


@dataclass(frozen=True)
class ApplyResult:
    """`POST /workflows/{id}/apply` and `PUT /workflows/{id}/config`."""

    workflow_id: str
    version: int
    changed_dims: tuple[str, ...]

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "ApplyResult":
        return cls(workflow_id=str(d["workflow_id"]), version=int(d["version"]),
                   changed_dims=tuple(d.get("changed_dims") or ()))


@dataclass(frozen=True)
class AutoApplyRequest:
    """`PUT /workflows/{id}/config` body. The proposal id is consumed only on success."""

    proposal_id: str
    base_version: int
    proposed_config: Config
    rationale: str
    evidence: Mapping[str, Any] = field(default_factory=FrozenDict)

    def body(self) -> dict[str, Any]:
        return {"proposal_id": self.proposal_id, "base_version": self.base_version,
                "proposed_config": self.proposed_config.body(), "rationale": self.rationale,
                "evidence": thaw(self.evidence)}


@dataclass(frozen=True)
class ValidationRun:
    """`POST /validation/runs`. Ids are sequential and global across workflows, and the id seeds
    the outcome (DESIGN.md 8.6)."""

    validation_run_id: str
    workflow_id: str
    prompt_version: str
    n: int
    failures: int
    pass_rate: float
    ts: str
    credits_remaining: float | None = None

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "ValidationRun":
        cr = d.get("credits_remaining")
        return cls(validation_run_id=str(d["validation_run_id"]),
                   workflow_id=str(d["workflow_id"]), prompt_version=str(d["prompt_version"]),
                   n=int(d["n"]), failures=int(d["failures"]), pass_rate=float(d["pass_rate"]),
                   ts=str(d["ts"]), credits_remaining=None if cr is None else float(cr))


@dataclass(frozen=True)
class Suggestion:
    """One `POST /llm/suggest-fixes` suggestion. The LLM only proposes (constraint 4)."""

    prompt_version: str
    summary: str

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "Suggestion":
        return cls(prompt_version=str(d["prompt_version"]), summary=str(d.get("summary", "")))


# =============================================================================== directives, policy

@dataclass(frozen=True)
class ResolvedDirective:
    """The client-agreement layer for one workflow after scope resolution (DESIGN.md 6.5).

    Produced by `controller.world.directives.DirectiveLayer.resolve`. `world/registry.py` combines
    it with the API policy into `EffectivePolicy` (a directive can only tighten the API policy).
    """

    tenant_id: str
    workflow_id: str
    customer_facing_auto_apply: str
    """none | risk_reducing_only | ramps_and_risk_reducing"""
    cohort_harm_line: float
    """x_k of the per-cohort harm gate (0.02 default, 0.01 by directive)."""
    notice_minutes: int
    notice_scope: str
    """customer_facing | non_incident: which auto-applies need the notice."""
    notice_inside_approver_hours: bool
    """The notice must be posted inside approver hours (Kestrel-like, DESIGN.md 4.1)."""
    max_requests_per_day: int | None
    not_before_min: int | None
    """Minute of day before which no routine request is submitted (hard), e.g. 660 = 11:00."""
    avoid_prompt_terms: tuple[str, ...]
    weekly_digest: bool
    preferred_slot_offset_max_min: int
    preferred_slot_offset_window_frac: float
    """`*` default slot: approver_open + min(offset_max, frac x approver window) (DESIGN.md 6.3)."""
    pre_open_submits: str
    """only_if_no_safe_in_hours (the `*` default) | never"""
    scopes: tuple[str, ...]
    """Matched scopes, least specific first, e.g. ("*", "kestrel_finance", "kestrel_finance/use_case~cancel")."""
    sources: FrozenDict
    """key -> the memo item it came from (for the audit table and the supervisor view)."""


@dataclass(frozen=True)
class EffectivePolicy:
    """API policy AND directives, the stricter of each (DESIGN.md 6.5, 11.2). Built by
    `world/registry.py`; every governance rule reads limits from here, never from constants."""

    tick: int
    approver_hours: tuple[int, int]
    approver_open_min: int
    latency_minutes: tuple[int, int]
    ttl_hours: float
    apply_window_min: int
    c_policy: int
    c_eff: int
    reserve: int
    routine_budget: int
    auto_enabled: bool
    allowed_dims: frozenset[str]
    max_shift: float
    customer_facing_auto: str
    not_before_min: int | None
    preferred_slot_min: int
    notice_minutes: int
    cohort_harm_line: float
    mid_flight: str
    min_validation_pass_rate: float
    avoid_prompt_terms: tuple[str, ...]
    # Additions to 11.2 (M0): identity and the remaining API and directive fields.
    workflow_id: str = ""
    tenant_id: str = ""
    base_reject_rate: float = 0.0
    notice_scope: str = "customer_facing"
    notice_inside_approver_hours: bool = False
    weekly_digest: bool = False
    dialling_window: tuple[int, int] = (0, 24)
    # Additions of step I1 (T2 request, ADR 0028).
    pre_open_submits: str = "only_if_no_safe_in_hours"
    """Directive: only_if_no_safe_in_hours | never."""
    api_auto_enabled: bool | None = None
    """What the API policy permits before directives ("policy permits, client forbids", F15).
    None means "same as `auto_enabled`" and is resolved in `__post_init__`."""
    api_allowed_dims: frozenset[str] | None = None
    """API `allowed_dims` before directives; None resolves to `allowed_dims`."""
    use_case: str = ""
    directive_scopes: tuple[str, ...] = ()
    """The directive scopes that matched, least specific first (audit and supervisor view)."""

    def __post_init__(self) -> None:
        if self.api_auto_enabled is None:
            object.__setattr__(self, "api_auto_enabled", self.auto_enabled)
        if self.api_allowed_dims is None:
            object.__setattr__(self, "api_allowed_dims", frozenset(self.allowed_dims))

    @property
    def approver_close_min(self) -> int:
        """Minute of day the approvers stop deciding."""
        return self.approver_hours[1] * 60

    @property
    def ttl_min(self) -> int:
        """Proposal TTL in whole minutes, with the simulator's own rounding `int(ttl * 60)`."""
        return int(self.ttl_hours * 60)


# =============================================================================== world and ingest

@dataclass(frozen=True)
class Cell:
    """One observed (lang, city_tier, age_band, value_band) combination (DESIGN.md 5.2)."""

    cell_id: int
    lang: str
    tier: str
    age: str
    value: str

    def features(self) -> dict[str, str]:
        return {"lang": self.lang, "city_tier": self.tier, "age_band": self.age,
                "value_band": self.value}


@dataclass(frozen=True)
class ConfigEvent:
    """A `config_changed` event as ingested (`config_event` table)."""

    event_id: int
    workflow_id: str
    version: int
    actor: str
    changed_dims: tuple[str, ...]
    proposal_id: str | None
    approval_id: str | None
    minute: Minute


@dataclass(frozen=True)
class ApprovalEvent:
    """An `approval_decided` event as ingested (`approval_event` table)."""

    event_id: int
    approval_id: str
    workflow_id: str
    status: str
    reasons: tuple[str, ...]
    minute: Minute


@dataclass(frozen=True)
class ConfigChange:
    """What `ConfigMirror.on_config_changed` learned about one new version."""

    workflow_id: str
    version: int
    actor: str
    body_source: str
    """ours | fetched | unknown (two edits in one step: the earlier body is never seen)"""
    config: Config | None
    changed_dims: tuple[str, ...]
    changed_paths: tuple[tuple[str, ...], ...]
    """Overlay paths that differ from the previous known body (DESIGN.md 6.7 step 1)."""

    @property
    def is_supervisor(self) -> bool:
        return self.actor == "supervisor"


@dataclass(frozen=True)
class IngestReport:
    """Result of `Ingestor.ingest` for one batch of chunks."""

    chunks: tuple[int, ...]
    events: int
    calls: int
    leads: int
    orphan_calls: int
    last_event_id: int
    until_minute: Minute
    wall_ms: int
    config_events: tuple[ConfigEvent, ...] = ()
    approval_events: tuple[ApprovalEvent, ...] = ()
    skipped_chunks: tuple[int, ...] = ()
    """Chunks already in `ingested_chunk` (exactly-once guard)."""


# =============================================================================== snapshot arrays

def _readonly(*arrays: np.ndarray) -> None:
    for a in arrays:
        a.setflags(write=False)


@dataclass(frozen=True, eq=False)
class EngageArrays:
    """`r_engage` rows for one workflow as parallel arrays (one row per key combination).

    Option codes index the workflow's option lists (`WorkflowSpec.options[dim]`)."""

    seg: np.ndarray
    cell: np.ndarray
    config_version: np.ndarray
    prompt: np.ndarray
    w: np.ndarray
    v: np.ndarray
    connected: np.ndarray
    engaged: np.ndarray
    flags: np.ndarray

    def __post_init__(self) -> None:
        _readonly(self.seg, self.cell, self.config_version, self.prompt, self.w, self.v,
                  self.connected, self.engaged, self.flags)

    def __len__(self) -> int:
        return int(self.seg.shape[0])


@dataclass(frozen=True, eq=False)
class PickupArrays:
    """`r_pickup` rows: dials and connects by (sim_day, hour, cell, attempt bucket 1 | 2-3 | 4+)."""

    sim_day: np.ndarray
    hour: np.ndarray
    cell: np.ndarray
    attempt_bucket: np.ndarray
    dials: np.ndarray
    connected: np.ndarray

    def __post_init__(self) -> None:
        _readonly(self.sim_day, self.hour, self.cell, self.attempt_bucket, self.dials,
                  self.connected)

    def __len__(self) -> int:
        return int(self.sim_day.shape[0])


@dataclass(frozen=True, eq=False)
class DidArrays:
    """`r_did` rows: dials and connects by (tick_no, hour, cohort_code, did). tick_no = minute //
    tick, so trailing 1/2/4/8 h windows are exact at any stop (DESIGN.md 3.2, 7.3)."""

    tick_no: np.ndarray
    hour: np.ndarray
    cohort_code: np.ndarray
    did: np.ndarray
    dials: np.ndarray
    connected: np.ndarray

    def __post_init__(self) -> None:
        _readonly(self.tick_no, self.hour, self.cohort_code, self.did, self.dials, self.connected)

    def __len__(self) -> int:
        return int(self.tick_no.shape[0])


@dataclass(frozen=True, eq=False)
class FlagSeries:
    """`r_flags` rows: connected calls, hallucination flags and engaged by (sim_hour, prompt)."""

    sim_hour: np.ndarray
    prompt: np.ndarray
    connected: np.ndarray
    flags: np.ndarray
    engaged: np.ndarray

    def __post_init__(self) -> None:
        _readonly(self.sim_hour, self.prompt, self.connected, self.flags, self.engaged)

    def __len__(self) -> int:
        return int(self.sim_hour.shape[0])


@dataclass(frozen=True, eq=False)
class ActivePopulation:
    """The population the harm gate averages over: our tracked active leads (DESIGN.md 0.1).

    `cells[i]` has `leads[i]` active leads and next-24h connected mix weight `mix[i]` (6.6)."""

    cells: np.ndarray
    leads: np.ndarray
    mix: np.ndarray
    cohort_of_cell: FrozenDict
    """cell_id -> cohort key under the config the gate evaluates."""

    def __post_init__(self) -> None:
        _readonly(self.cells, self.leads, self.mix)


# =============================================================================== learning

@dataclass(frozen=True, eq=False)
class ParamIndex:
    """Maps model parameters to columns, and arm effects to per-cell linear maps.

    `effect_maps[(dim, arm)]` is an (n_cells, n_params) matrix E with E @ theta = the arm's
    change of the linear predictor in each cell vs that dim's control. `scale` says what the
    linear predictor is: "log" (Beta-binomial log-ratio parameters, the predictor IS the log
    ratio) or "logit" (ridge-logit; then `control_map` @ theta + `offset` is the control's
    logit per cell, and the log ratio is log sigmoid(base + delta) - log sigmoid(base)).
    """

    names: tuple[str, ...]
    cell_ids: tuple[int, ...]
    effect_maps: FrozenDict
    scale: Literal["log", "logit"] = "log"
    control_map: np.ndarray | None = None
    offset: np.ndarray | None = None

    @property
    def n_params(self) -> int:
        return len(self.names)

    def col(self, name: str) -> int:
        return self.names.index(name)


@dataclass(frozen=True, eq=False)
class Posterior:
    """One fit of the engagement model. Gaussian (Laplace) for ridge-logit; for the
    Beta-binomial model `mean`/`cov` hold the log-ratio approximation and `extra` holds the
    exact Beta parameters (for example {"alpha": ..., "beta": ...})."""

    wf: str
    kind: FitKind
    mean: np.ndarray
    cov: np.ndarray | None
    index: ParamIndex
    inputs_hash: str
    summary: FrozenDict = field(default_factory=FrozenDict)
    extra: FrozenDict = field(default_factory=FrozenDict)
    snapshot_id: int | None = None
    """`posterior_snapshot.snapshot_id` once stored (replay, 9.6)."""


@dataclass(frozen=True, eq=False)
class Draws:
    """Seeded posterior draws of one fit (DESIGN.md 5.3, 8.6). n_draws = settings.n_draws."""

    kind: FitKind
    seed: int
    snapshot_id: int
    params: np.ndarray
    """(n_draws, n_params)"""
    index: ParamIndex

    def __post_init__(self) -> None:
        _readonly(self.params)

    def cell_effects(self, dim: str, arm: str) -> np.ndarray:
        """(n_draws, n_cells) log engagement ratios of `arm` vs the dim's control per cell."""
        emap = self.index.effect_maps.get((dim, arm))
        if emap is None:
            return np.zeros((self.params.shape[0], len(self.index.cell_ids)))
        delta = self.params @ np.asarray(emap).T
        if self.index.scale == "log":
            return delta
        if self.index.control_map is None:
            raise ValueError("logit scale needs control_map")
        base = self.params @ self.index.control_map.T
        if self.index.offset is not None:
            base = base + self.index.offset
        # log sigmoid(x) = -log1p(exp(-x)), computed stably
        return -np.logaddexp(0.0, -(base + delta)) + np.logaddexp(0.0, -base)


@dataclass(frozen=True)
class DidState:
    """One caller ID's classification (`did_state` table, DESIGN.md 5.9)."""

    wf: str
    did: str
    status: str
    """unknown | probing | healthy | unhealthy | degraded | suspect"""
    r_hat: float | None = None
    r_lcb: float | None = None
    r_ucb: float | None = None
    n_dials: int = 0
    sprt_llr: float = 0.0
    since_minute: Minute | None = None
    evidence: FrozenDict = field(default_factory=FrozenDict)


@dataclass(frozen=True, eq=False)
class DidPosterior:
    """Per-DID relative pickup r_d for the caller-ID harm gate (DESIGN.md 5.9, 6.6).

    `draws[:, j]` are draws of r for `dids[j]`; an unknown DID ranges over
    [r_flag, r_unknown_max] with the P(flagged) prior."""

    wf: str
    dids: tuple[str, ...]
    draws: np.ndarray
    status: FrozenDict
    """did -> DidStatus"""

    def __post_init__(self) -> None:
        _readonly(self.draws)


@dataclass(frozen=True)
class GuardrailForecast:
    """Predicted guardrail KPI under a proposed config (DESIGN.md 5.10, 6.6)."""

    kpi: str
    current: float
    proposed: float
    max_drop_abs: float
    drop_mean: float
    p_breach: float
    mix_term: float = 0.0
    within_term: float = 0.0


@dataclass(frozen=True)
class HarmReport:
    """The harm gate's answer (DESIGN.md 0.1, 5.4)."""

    de_mean: float
    p_de_below_1pct: float
    de_k: FrozenDict
    """cohort -> (mean, P(de_k < -x_k))"""
    dd_mean: float
    p_dd_below_2pct: float
    guardrail_kpi: str
    guardrail_drop_mean: float
    p_guardrail_breach: float
    passes: bool
    failed: tuple[str, ...]
    alpha_used: float

    def to_json(self) -> dict[str, Any]:
        return {"de_mean": self.de_mean, "p_de_below_1pct": self.p_de_below_1pct,
                "de_k": {k: list(v) for k, v in self.de_k.items()}, "dd_mean": self.dd_mean,
                "p_dd_below_2pct": self.p_dd_below_2pct, "guardrail_kpi": self.guardrail_kpi,
                "guardrail_drop_mean": self.guardrail_drop_mean,
                "p_guardrail_breach": self.p_guardrail_breach, "passes": self.passes,
                "failed": list(self.failed), "alpha_used": self.alpha_used}


@dataclass(frozen=True)
class Finding:
    """Any detector output, e.g. ("did", "alarm"), ("drift", "pickup_down")."""

    wf: str
    detector: str
    kind: str
    scope: str
    stat: float
    threshold: float
    first_evidence_minute: Minute
    evidence: FrozenDict = field(default_factory=FrozenDict)


Breach = Finding
"""A guardrail breach is a Finding with detector "guardrail" (11.1 names it Breach)."""


@dataclass(frozen=True)
class Winner:
    """A declared winner (DESIGN.md 5.5)."""

    exp_id: str
    wf: str
    dim: str
    arm: str
    scope: str
    """a cohort key or "population" """
    look_no: int
    n: int
    p_pos: float
    p_best: float
    ew_p_pos: float
    harm: HarmReport


@dataclass(frozen=True)
class HourCurveSet:
    """Shrunk hour-of-day pickup curves (DESIGN.md 5.8)."""

    wf: str
    curves: FrozenDict
    """cohort -> hour -> pickup probability"""
    pooled: FrozenDict
    """hour -> pooled workflow pickup"""
    dials: FrozenDict
    """cohort -> hour -> dials behind the curve"""
    prior_strength: float


@dataclass(frozen=True)
class ExperimentArm:
    """`experiment_arm` row: one arm in one scope."""

    exp_id: str
    arm: str
    scope: str
    state: str
    """live | frozen | removed | futile | winner | control"""
    share: float
    first_dial_minute: Minute | None = None
    removed_minute: Minute | None = None
    decision_id: int | None = None


@dataclass(frozen=True)
class Experiment:
    """`experiment` row plus its arms (DESIGN.md 5.3)."""

    exp_id: str
    wf: str
    dim: str
    arms: tuple[str, ...]
    control: str
    cohort_set_version: int
    state: str
    started_version: int
    started_minute: Minute
    interrupted_by: int | None = None
    """config version of the supervisor edit that interrupted it"""
    arm_rows: tuple[ExperimentArm, ...] = ()


@dataclass(frozen=True)
class DecayMonitorState:
    """`decay_monitor` row: one-sided Page CUSUM per (scope, dim, arm) (DESIGN.md 5.7)."""

    wf: str
    scope: str
    dim: str
    arm: str
    cusum: float
    seg_last: int
    theta_ref: float
    alarm: bool


@dataclass(frozen=True)
class DriftMonitorState:
    """`drift_monitor` row: downward Page CUSUM per KPI (DESIGN.md 7.5)."""

    wf: str
    kpi: str
    cusum: float
    last_block: int
    baseline_kind: str
    alarm: bool
    up_cusum: float = 0.0


@dataclass(frozen=True)
class FlagMonitorState:
    """`flag_monitor` row: the live Bernoulli flag CUSUM (DESIGN.md 7.1)."""

    wf: str
    prompt: str
    p0: float
    p1: float
    h: float
    stat: float
    flags_since_reset: int
    calls: int
    last_event_id: int
    alarm: bool
    reset_minute: Minute


@dataclass(frozen=True)
class DetectorState:
    """Persisted detector state for one workflow, rebuilt from the store on start."""

    wf: str
    did: FrozenDict = field(default_factory=FrozenDict)
    """did -> DidState"""
    flag: FlagMonitorState | None = None
    drift: FrozenDict = field(default_factory=FrozenDict)
    """kpi -> DriftMonitorState"""
    decay: tuple[DecayMonitorState, ...] = ()
    last_minute: Minute | None = None


# =============================================================================== incidents

@dataclass(frozen=True)
class Verdict:
    """A prompt safety verdict (DESIGN.md 7.1)."""

    wf: str
    prompt: str
    verdict: Literal["SAFE", "UNSAFE"]
    mode: VerdictMode
    runs: tuple[str, ...]
    pooled_n: int
    pooled_failures: int
    gate_run: str | None
    """The designated evidence run, if it passed the client gate g."""
    approvable: bool
    credits: float


@dataclass(frozen=True)
class Incident:
    """`incident` row."""

    incident_id: str
    wf: str
    kind: str
    """prompt_safety | did_degraded | ..."""
    state: str
    opened_minute: Minute
    confirmed_minute: Minute | None = None
    resolved_minute: Minute | None = None
    evidence: FrozenDict = field(default_factory=FrozenDict)
    fix_proposal_id: str | None = None
    credits_spent: float = 0.0


@dataclass(frozen=True)
class CreditEnvelope:
    """`credit_envelope` row: one ordered pool (DESIGN.md 7.2). tenant "*" for shared pools."""

    pool: str
    tenant: str
    allotted: float
    spent: float
    mode: str


# =============================================================================== planner, governance

@dataclass(frozen=True)
class PatchRequest:
    """What learning and incident modules emit. Carries no risk flag: risk is computed from the
    composed config by `governance/policy.classify_risk` (DESIGN.md 6.5, ADR 0025).

    Paths: ("alloc", cohort_key, dim), ("prompt",), ("hour_weights", cohort_key), ("cohorts",).
    """

    wf: str
    owner: str
    path: tuple[str, ...]
    value: Any
    cls: int
    """0 incident, 1 rollback, 2 promotion, 3 routine"""
    p0_rank: int | None
    """0 = P0a prompt safety, 1 = P0b DID drain; None if cls > 0"""
    evidence: FrozenDict
    decision_id: int


@dataclass(frozen=True)
class Patch:
    """`patch` row: a PatchRequest after the overlay accepted it."""

    patch_id: int
    wf: str
    owner: str
    path: tuple[str, ...]
    value: Any
    cls: int
    state: str
    decision_id: int
    p0_rank: int | None = None
    proposal_id: str | None = None
    supervisor_touch: int | None = None
    """config version of the supervisor edit that invalidated it"""
    held_until_minute: Minute | None = None
    hold_count: int = 0
    evidence: FrozenDict = field(default_factory=FrozenDict)


@dataclass(frozen=True)
class Composition:
    """Live config plus patches, ready for the gates (DESIGN.md 11.2)."""

    wf: str
    base_version: int
    live: Config
    proposed: Config
    patches: tuple[Patch, ...]
    changed_dims: tuple[str, ...]
    tv_by_cohort_dim: FrozenDict
    """(cohort, dim) -> total-variation distance"""
    risk: FrozenDict
    """(cohort, dim) -> "reducing" | "increasing" (classify_risk)"""
    cls: int
    p0_rank: int | None


@dataclass(frozen=True)
class Proposal:
    """`proposal` row (DESIGN.md 3.2, 6.1)."""

    proposal_id: str
    wf: str
    cls: int
    route: str
    """approval | auto"""
    state: str
    base_version: int
    proposed_config: Config | None = None
    request: FrozenDict = field(default_factory=FrozenDict)
    change_summary: str = ""
    diff: FrozenDict = field(default_factory=FrozenDict)
    affected_leads: int = 0
    dry_run: FrozenDict = field(default_factory=FrozenDict)
    impact_score: float = 0.0
    rationale: str = ""
    evidence: FrozenDict = field(default_factory=FrozenDict)
    approval_id: str | None = None
    submitted_minute: Minute | None = None
    decided_minute: Minute | None = None
    apply_by_minute: Minute | None = None
    notice_until_minute: Minute | None = None
    applied_version: int | None = None
    superseded_by: str | None = None
    requeued_from: str | None = None
    decision_id: int | None = None
    verify: FrozenDict = field(default_factory=FrozenDict)
    p0_rank: int | None = None
    """0 = P0a prompt safety, 1 = P0b DID drain, None unless cls == 0 (`proposal.p0_rank`,
    migration 0009; release precedence and supersede read it, ADR 0013)."""

    @property
    def in_flight(self) -> bool:
        return self.state in IN_FLIGHT_STATES


@dataclass(frozen=True)
class Hold:
    """`hold` row: a supervisor pause of non-incident changes (DESIGN.md 9.3)."""

    hold_id: int
    wf: str
    set_by: str
    reason: str
    from_minute: Minute
    until_minute: Minute | None
    released_minute: Minute | None = None

    def active(self, now: Minute) -> bool:
        if self.released_minute is not None and self.released_minute <= now:
            return False
        return self.from_minute <= now and (self.until_minute is None or now < self.until_minute)


@dataclass(frozen=True)
class VelocityEntry:
    """One applied auto-apply's TV on one (cohort, dim) (DESIGN.md 6.5 rule 4)."""

    minute: Minute
    cohort: str
    dim: str
    tv: float
    increasing: bool
    proposal_id: str


@dataclass(frozen=True)
class VelocityLedger:
    """Rolling-window TV budget of exposure-increasing auto-applies per (cohort, dim)."""

    wf: str
    entries: tuple[VelocityEntry, ...] = ()

    def used(self, cohort: str, dim: str, now: Minute, window_min: int,
             increasing_only: bool = False) -> float:
        """Cumulative TV on (cohort, dim) with `now - window_min < minute <= now`. Risk-reducing
        chain steps count too unless `increasing_only` (DESIGN.md 6.5 "Chaining, bounded")."""
        return sum(e.tv for e in self.entries
                   if e.cohort == cohort and e.dim == dim and now - window_min < e.minute <= now
                   and (e.increasing or not increasing_only))


@dataclass(frozen=True)
class Substitution:
    """Rebase step 5 (DESIGN.md 6.7): a cohort override moved from the supervisor's old control
    to the new one."""

    patch_id: int
    cohort: str
    dim: str
    old_control: str
    new_control: str
    applied: bool
    """False: the new control is credibly worse in this cohort; kept and listed for approval"""


@dataclass(frozen=True)
class RebaseReport:
    """What `Overlay.rebase_on_supervisor_edit` did (DESIGN.md 6.7)."""

    wf: str
    version: int
    sup_paths: tuple[tuple[str, ...], ...]
    invalidated_patches: tuple[int, ...]
    interrupted_experiments: tuple[str, ...]
    stale_proposals: tuple[str, ...]
    cooldown_paths: tuple[tuple[str, ...], ...]
    cooldown_until: Minute | None
    substitutions: tuple[Substitution, ...] = ()


# =============================================================================== audit, loop, reliability

@dataclass(frozen=True)
class Decision:
    """A decision row, written before acting (DESIGN.md 8.2). Includes decisions not to act."""

    wf: str | None
    module: str
    kind: str
    rule_id: str
    inputs: FrozenDict
    outcome: FrozenDict
    harm_report: HarmReport | None
    snapshot_ids: tuple[int, ...]
    seed: int
    settings_hash: str
    code_version: str
    parent_decision_id: int | None = None
    sim_minute: Minute | None = None
    """Filled by the writer from `clock` when None."""


@dataclass(frozen=True)
class Intent:
    """`intent` row (DESIGN.md 8.2)."""

    intent_id: int
    kind: str
    idem_key: str
    request: FrozenDict
    status: str
    """started | done | failed | aborted | unknown"""
    decision_id: int | None
    sim_minute: Minute
    response: FrozenDict | None = None


@dataclass(frozen=True)
class WorkflowSnapshot:
    """Everything a learning or incident module may read about one workflow at one stop."""

    spec: WorkflowSpec
    policy: EffectivePolicy
    live: Config
    live_version: int
    cohorts: "CohortSet"
    engage: EngageArrays
    pickup: PickupArrays
    did: DidArrays
    flags: FlagSeries
    experiments: tuple[Experiment, ...]
    did_state: FrozenDict
    """did -> DidState"""
    in_flight: Proposal | None
    used_24h: tuple[Minute, ...]
    """created minutes of our requests in the rolling 24h (fatigue)"""
    now: Minute


@dataclass(frozen=True)
class WorkflowPlanState:
    """Per-workflow facts `loop/stepper.next_stop` needs (DESIGN.md 4.2)."""

    workflow_id: str
    policy: EffectivePolicy
    dialling_window: tuple[int, int]
    daily_batch_at_min: int
    has_pending: bool = False
    pending_class: int | None = None
    pending_created: Minute | None = None
    approved_apply_by: tuple[Minute, ...] = ()
    """apply_by minutes of APPROVED (not yet applied) proposals"""
    open_incident: bool = False
    suspect_did: bool = False
    suspect_prompt: bool = False
    drain_chain_pending: bool = False
    planned_slots: tuple[Minute, ...] = ()
    notice_expiries: tuple[Minute, ...] = ()


@dataclass(frozen=True)
class PlannerContext:
    """Input of `next_stop(now, ctx)` (DESIGN.md 4.2). Pure data, so the step rule is testable."""

    tick: int
    base_step: int
    end_minute: Minute
    workflows: tuple[WorkflowPlanState, ...]


@dataclass(frozen=True)
class StepReport:
    """One PLAN..OPS pass (`loop_step` row, DESIGN.md 4.1, 9.1)."""

    step_no: int
    from_minute: Minute
    to_minute: Minute
    phase_ms: FrozenDict
    chunks: int = 0
    events: int = 0
    orphan_calls: int = 0
    decisions: int = 0
    proposals_submitted: int = 0
    applies: int = 0
    credits_remaining: float | None = None
    finished: bool = False


@dataclass(frozen=True)
class IntentResolution:
    intent_id: int
    kind: str
    idem_key: str
    resolution: str
    """e.g. "done", "aborted", "resent", "lost_validation_run", "lapsed_local" """
    detail: FrozenDict = field(default_factory=FrozenDict)


@dataclass(frozen=True)
class ReconcileReport:
    """Result of `Reconciler.run` (DESIGN.md 8.3)."""

    resolved: tuple[IntentResolution, ...] = ()
    unknown_writers: tuple[str, ...] = ()
    """config versions or approval ids written by a controller actor with no intent of ours"""
    alerts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeReport:
    """Result of `Orchestrator.resume` (DESIGN.md 2 restart sequence, 8.5)."""

    schema_name: str
    resumed: bool
    """False: a fresh schema was created (first start, re-run or restarted simulator)"""
    sim_minute: Minute
    reconcile: ReconcileReport
    chunks_ingested: int = 0
    bootstrap_rerun: bool = False
    alerts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayReport:
    """Result of `replay_decision` (DESIGN.md 9.6)."""

    decision_id: int
    stored: FrozenDict
    recomputed: FrozenDict
    equal: bool
    diffs: tuple[str, ...] = ()
    settings_hash_match: bool = True
    code_version_match: bool = True


# =============================================================================== interfaces (11.2)
# Structural protocols for the classes of DESIGN.md 11.2. The concrete classes live in the named
# modules and are owned by their tracks; these protocols are the contract they are typed against.

@runtime_checkable
class Ingestor(Protocol):
    """ingest/pipeline.py (and ingest/python_parse.py behind the same interface)."""

    def ingest(self, chunks: list[ChunkMeta]) -> IngestReport: ...   # exactly-once, in order


@runtime_checkable
class ConfigMirror(Protocol):
    """world/configs.py"""

    def on_config_changed(self, tx: Tx, event: ConfigEvent) -> ConfigChange: ...
    def body(self, wf: str, version: int) -> Config | None: ...
    def live(self, wf: str) -> tuple[int, Config]: ...


@runtime_checkable
class CohortSet(Protocol):
    """cohorts/cohortset.py"""

    version: int

    def cohort_of(self, cell: int) -> Cohort: ...          # first match, business cohorts first
    def to_config(self) -> tuple[CohortDef, ...]: ...
    def membership(self, config_version: int) -> Mapping[Cohort, frozenset[int]]: ...
    def owner(self, cohort: Cohort) -> Literal["ours", "supervisor"]: ...
    def winner_scope_eligible(self, cohort: Cohort, ws: WorkflowSnapshot) -> bool: ...


@runtime_checkable
class EngagementModel(Protocol):
    """learning/engagement.py (Beta-binomial first, ridge-logit second; DESIGN.md 17.2)."""

    def fit(self, ws: WorkflowSnapshot, kind: FitKind) -> Posterior: ...
    def draws(self, post: Posterior, seed: int, n: int = 4000) -> Draws: ...


@runtime_checkable
class DecayMonitor(Protocol):
    """learning/decay.py; state in `decay_monitor`."""

    def update(self, ws: WorkflowSnapshot, segment: int) -> list[Finding]: ...
    def rollback_plan(self, ws: WorkflowSnapshot, ew: Draws, t_apply: int) -> list[PatchRequest]: ...


@runtime_checkable
class DidHealth(Protocol):
    """learning/did.py; state in `did_state`."""

    def update(self, ws: WorkflowSnapshot) -> list[Finding]: ...       # SPRT, slow path
    def classify(self, ws: WorkflowSnapshot) -> Mapping[str, DidState]: ...
    def plan_caller_ids(self, ws: WorkflowSnapshot) -> list[PatchRequest]: ...


@runtime_checkable
class HourCurves(Protocol):
    """learning/schedule.py"""

    def fit(self, ws: WorkflowSnapshot) -> HourCurveSet: ...
    def plan_hour_weights(self, ws: WorkflowSnapshot, curves: HourCurveSet) -> list[PatchRequest]: ...


@runtime_checkable
class GuardrailMonitor(Protocol):
    """learning/guardrail.py"""

    def predict(self, ws: WorkflowSnapshot, cur: Config, new: Config) -> GuardrailForecast: ...
    def check(self, ws: WorkflowSnapshot) -> list[Finding]: ...


@runtime_checkable
class SafetyVerdict(Protocol):
    """incidents/safety.py"""

    def run(self, wf: str, prompt: str, mode: VerdictMode, for_proposal: bool) -> Verdict: ...


@runtime_checkable
class IncidentManager(Protocol):
    """incidents/manager.py; state in `incident`."""

    def on_findings(self, ws: WorkflowSnapshot, findings: list[Finding]) -> list[PatchRequest]: ...
    def verify(self, ws: WorkflowSnapshot) -> list[Finding]: ...


@runtime_checkable
class CreditBudget(Protocol):
    """incidents/credits.py; state in `credit_envelope`, `credit_ledger`."""

    def mode(self) -> CreditMode: ...
    def authorize(self, tenant: str, pool: CreditPool, purpose: str, amount: float) -> bool: ...
    def record(self, tx: Tx, intent_id: int, pool: str, cost: float,
               remaining_after: float) -> None: ...


@runtime_checkable
class Overlay(Protocol):
    """planner/overlay.py"""

    def add(self, req: PatchRequest) -> Patch: ...   # refuses a request with no decision_id
    # `paths`: an optional predicate over patch paths; compose only the patches it accepts.
    def compose(self, wf: str, live: Config, classes: set[int], *, base_version: int = 0,
                paths: Any = None) -> Composition: ...
    def rebase_on_supervisor_edit(self, wf: str, old: Config | None, new: Config,
                                  v: int) -> RebaseReport: ...


@runtime_checkable
class ProposalLifecycle(Protocol):
    """governance/lifecycle.py. `package` is a `governance.builder.RequestPackage`."""

    def tick(self, now: int) -> list[Proposal]: ...
    def submit(self, comp: Composition, now: int, *, package: Any = None,
               check: bool = True) -> Proposal: ...
    def supersede(self, holder: Proposal, by: Composition, *, now: int | None = None,
                  package: Any = None, route: str = "approval",
                  bundle_keeps_drain: bool = False, rationale: str = "") -> Proposal: ...
    def auto_apply(self, comp: Composition, now: int, *, rationale: str,
                   notice_until: int | None = None, package: Any = None) -> Proposal: ...
    def verify(self, now: int) -> list[Proposal]: ...
    def mark_stale(self, wf: str, version: int, now: int) -> list[Proposal]: ...
    def policy_reread(self, wf: str) -> None: ...


@runtime_checkable
class ReleaseTrain(Protocol):
    """governance/release.py"""

    def run(self, now: int) -> None: ...                  # P0a, P0b, P1, auto, routine slot


@runtime_checkable
class Orchestrator(Protocol):
    """loop/orchestrator.py"""

    def resume(self) -> ResumeReport: ...
    def run_step(self) -> StepReport: ...
    def run(self) -> int: ...                             # exit code 0 / 1 / 3


@runtime_checkable
class IntentExecutor(Protocol):
    """reliability/intents.py"""

    def execute(self, kind: str, key: str, payload: dict[str, Any], decision: Decision) -> Any: ...


@runtime_checkable
class Reconciler(Protocol):
    """reliability/intents.py"""

    def run(self) -> ReconcileReport: ...


def iter_paths(cfg: Config) -> Iterator[tuple[str, ...]]:
    """Every overlay path present in a config (DESIGN.md 6.7), in a fixed order."""
    yield ("cohorts",)
    for k in sorted(cfg.allocations):
        for d in AGENT_DIMS:
            if d in cfg.allocations[k]:
                yield ("alloc", k, d)
    for k in sorted(cfg.hour_weights):
        yield ("hour_weights", k)
    yield ("prompt",)
