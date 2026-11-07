"""Alert rules. Every alert code has a severity, a one-line meaning and a runbook anchor.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.2 (pages vs morning digest) and 9.5
(one `docs/RUNBOOK.md` anchor per code; `tests/obs/test_alerts.py` fails if one is missing).

Two kinds of alert:
- Condition alerts are evaluated from the store every step (`evaluate`, pure over
  `AlertInputs`, which `gather_inputs` reads). `sync` opens an alert when a condition starts
  and closes it when the condition clears, so a page is open exactly while the problem lasts.
- Event alerts are raised by the module that sees the event (`raise_alert`), for example
  governance on a 403 (`GOV_INVARIANT`) or run resolution on a new world (`NEW_WORLD`). They
  stay open until an operator closes them (`close_alert`), because each one needs a look.
  Digest codes are recorded already closed: the morning digest reads them, nobody is paged.

Never an alert on a single call, a routine submission or an auto-applied schedule refresh
(9.2): every condition below is over populations, proposals, incidents or the loop itself.

Alerts are de-duplicated by (code, workflow, subject): the subject is the thing the alert is
about (a proposal id, an incident id, "ingest"), stored in `alert.dedupe_key`; the unique index
`alert_open_key` (migration 0012, ADR 0028) allows one open row per key. This module is the only
alert writer: the loop, ingest and the reconciler raise their alerts through `raise_alert` too.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from controller.types import label

Severity = Literal["page", "digest"]
Kind = Literal["condition", "event"]
RUNBOOK = "docs/RUNBOOK.md"


@dataclass(frozen=True)
class AlertCode:
    code: str
    severity: Severity
    kind: Kind
    meaning: str

    @property
    def runbook(self) -> str:
        return f"{RUNBOOK}#{self.code}"


CODES: dict[str, AlertCode] = {c.code: c for c in (
    # ------------------------------------------------------------------ pages (9.2 left column)
    AlertCode("INGEST_STALLED", "page", "condition",
              "a listed chunk is not committed after 5 wall minutes"),
    AlertCode("APPLY_AT_RISK", "page", "condition",
              "an approved proposal may lapse: apply_by is within one step"),
    AlertCode("GOV_INVARIANT", "page", "event",
              "the simulator disagreed with our local policy replica (403, 410, 422, fatigue or "
              "evidence rejection, apply conflict)"),
    AlertCode("CREDITS_BELOW_RESERVE", "page", "condition",
              "the incident reserve fell below one worst incident, LOW credit mode, or a 402"),
    AlertCode("INCIDENT_UNRESOLVED", "page", "condition",
              "an incident has no submitted fix after 2 sim hours or is open after 6, or a DID "
              "drain waits more than 2 hours behind a prompt-safety approval"),
    AlertCode("NO_SAFE_PROMPT", "page", "event", "every catalog prompt is UNSAFE"),
    AlertCode("CLIENT_GATE_UNREACHABLE", "page", "event",
              "safe prompts cannot pass the client validation gate reliably"),
    AlertCode("INCIDENT_FIX_FAILED", "page", "event",
              "a prompt fix failed its post-apply verification; the next candidate goes as a P0"),
    AlertCode("NO_HEALTHY_DID", "page", "condition", "no caller ID can take traffic"),
    AlertCode("DID_DEGRADED", "page", "event",
              "the fast DID detector alarmed on a caller ID (7.3); a P0b drain is running"),
    AlertCode("DID_DRAIN_UNVERIFIED", "page", "event",
              "two dialling hours after a DID drain, the drained DID still dials or workflow "
              "pickup is below 0.9 of its pre-incident level (7.3)"),
    AlertCode("DID_CHANGE_UNVERIFIED", "page", "event",
              "two dialling hours after a caller-ID change, a DID it dropped still dials or "
              "workflow pickup is credibly below 0.9 of the 24h before (5.9, 7.3)"),
    AlertCode("SCHEDULE_UNVERIFIED", "page", "event",
              "the 2 segments after an hour table fell below its credible band; a P1 restores "
              "the previous table (5.8)"),
    AlertCode("UNKNOWN_WORKFLOW", "page", "event",
              "the API lists a workflow that was not there when this run started; it is ignored "
              "until a new run"),
    AlertCode("NO_SAFE_WINDOW", "page", "event", "no expiry-safe submit minute in the next 24h"),
    AlertCode("NEW_WORLD", "page", "event", "the simulator restarted or the scenario re-ran"),
    AlertCode("UNKNOWN_WRITER", "page", "event",
              "a config version or approval exists that no intent of ours explains"),
    AlertCode("COHORT_MISMATCH", "page", "event",
              "a call's cohort or option disagrees with our recorded resolution"),
    AlertCode("CRASH_LOOP", "page", "condition", "more than 3 restarts in 10 minutes"),
    AlertCode("SIM_UNREACHABLE", "page", "condition",
              "idempotent GETs to the simulator have been retrying for 5 minutes"),
    AlertCode("CLOCK_MISMATCH", "page", "event",
              "after reconciliation our clock differs from the simulator's; we follow the "
              "simulator"),
    AlertCode("INTENT_UNKNOWN", "page", "event",
              "reconciliation could not tell whether a side effect happened"),
    AlertCode("SAFE_MODE", "page", "event",
              "a hook crashed repeatedly at one step; the loop keeps advancing without it"),
    AlertCode("LIMP_MODE", "page", "event",
              "the controller could not run; the scenario is only being advanced to its end"),
    AlertCode("DIRECTIVE_UNMATCHED", "page", "event",
              "a client directive matches no workflow, so its memo promise is not applied"),
    # ------------------------------------------------------------------ morning digest (right column)
    AlertCode("APPROVAL_REJECTED", "digest", "event",
              "an approver rejected a request (re-queued with fresh data automatically)"),
    AlertCode("APPROVAL_EXPIRED", "digest", "event",
              "a request expired (treated as a rejection and re-queued)"),
    AlertCode("HARM_GATE_BLOCK", "digest", "event", "the harm gate blocked a change"),
    AlertCode("DID_NOT_ACT", "digest", "event", "a module decided not to act, with its reason"),
    AlertCode("DECAY_ALARM_CLEARED", "digest", "event", "a decay CUSUM alarm cleared"),
    AlertCode("FUTILITY_DROP", "digest", "event", "an arm was dropped for futility"),
    AlertCode("ARM_REMOVED", "digest", "event", "an arm was removed"),
    AlertCode("LOST_VALIDATION_RUN", "digest", "event",
              "a validation or suggest-fixes call took effect and its result was lost; its "
              "credits are in the ledger and the run was repeated at most once"),
    AlertCode("KPI_DRIFT", "digest", "event",
              "a KPI drift monitor alarmed (7.5), with its mix decomposition"),
    AlertCode("SUPERVISOR_EDIT", "digest", "event", "a client supervisor edited a config"),
    AlertCode("EXPERIMENT_INTERRUPTED", "digest", "event",
              "a supervisor edit interrupted an experiment"),
    AlertCode("RETRY_SUCCEEDED", "digest", "event", "a retried request succeeded"),
    AlertCode("ORPHAN_CALLS", "digest", "event", "calls arrived whose lead was never seen"),
    AlertCode("HOLD_AUTO_RELEASED", "digest", "event", "a held patch set was auto-released"),
    AlertCode("WEEKLY_DIGEST", "digest", "event", "the weekly operational digest is ready"),
)}

CONDITION_CODES = frozenset(c.code for c in CODES.values() if c.kind == "condition")


def runbook_anchor(code: str) -> str:
    return CODES[code].runbook


# =============================================================================== inputs

@dataclass(frozen=True)
class ApplyRisk:
    wf: int
    proposal_id: str
    state: str
    apply_by_minute: int


@dataclass(frozen=True)
class OpenIncident:
    wf: int
    incident_id: str
    kind: str
    opened_minute: int
    fix_submitted_minute: int | None
    p0a_in_flight_since: int | None = None
    """For a DID incident: since when a prompt-safety (P0a) proposal of the same workflow has
    been in flight, blocking the drain; None if nothing blocks it."""


@dataclass(frozen=True)
class AlertInputs:
    """Everything the condition rules look at, as of one step."""

    wall_now: _dt.datetime
    sim_minute: int
    tick: int
    next_stop: int | None = None
    """The minute the next advance will stop at (from the step rule), if known."""
    listed_chunk_max: int | None = None
    """Highest chunk number GET /events/chunks lists (from the loop), if known."""
    ingested_chunk_max: int | None = None
    last_ingested_wall: _dt.datetime | None = None
    apply_risks: tuple[ApplyRisk, ...] = ()
    reserve_left: float | None = None
    worst_incident_cost: float | None = None
    low_mode_tenants: tuple[str, ...] = ()
    incidents: tuple[OpenIncident, ...] = ()
    did_status: dict[int, tuple[str, ...]] = field(default_factory=dict)
    restart_walls: tuple[_dt.datetime, ...] = ()
    get_retry_since: _dt.datetime | None = None


@dataclass(frozen=True)
class Firing:
    code: str
    wf: int | None
    subject: str
    message: str

    @property
    def key(self) -> tuple[str, int | None, str]:
        return (self.code, self.wf, self.subject)


# =============================================================================== rules (pure)

def _ingest(i: AlertInputs, t: Any) -> list[Firing]:
    if i.listed_chunk_max is None or i.last_ingested_wall is None:
        return []
    behind = i.listed_chunk_max - (i.ingested_chunk_max if i.ingested_chunk_max is not None else -1)
    stalled_s = (i.wall_now - i.last_ingested_wall).total_seconds()
    if behind > 0 and stalled_s >= t.alert_ingest_stalled_s:
        return [Firing("INGEST_STALLED", None, "ingest",
                       f"{behind} listed chunk(s) above {i.ingested_chunk_max} not committed for "
                       f"{stalled_s / 60:.0f} wall minutes; look at the last loop_step phase_ms "
                       "and the Postgres log")]
    return []


def _apply_at_risk(i: AlertInputs, t: Any) -> list[Firing]:
    horizon = max(i.sim_minute + i.tick, i.next_stop if i.next_stop is not None else 0)
    out = []
    for r in i.apply_risks:
        if r.state in ("approved", "applying") and r.apply_by_minute <= horizon:
            out.append(Firing("APPLY_AT_RISK", r.wf, r.proposal_id,
                              f"proposal {r.proposal_id} is {r.state} with apply_by "
                              f"{label(r.apply_by_minute)}, within the next step (now "
                              f"{label(i.sim_minute)}); see its pre-check decision row"))
    return out


def _credits(i: AlertInputs, t: Any) -> list[Firing]:
    out = []
    if (i.reserve_left is not None and i.worst_incident_cost is not None
            and i.reserve_left < i.worst_incident_cost):
        out.append(Firing("CREDITS_BELOW_RESERVE", None, "reserve",
                          f"incident reserve has {i.reserve_left:g} credits left, below one worst "
                          f"incident ({i.worst_incident_cost:g}); see credit_ledger by pool"))
    for tenant in i.low_mode_tenants:
        out.append(Firing("CREDITS_BELOW_RESERVE", None, f"low:{tenant}",
                          f"credit pools for {tenant} are in LOW mode (stage-1-only verdicts, no "
                          "screens)"))
    return out


def _incidents(i: AlertInputs, t: Any) -> list[Firing]:
    out = []
    for inc in i.incidents:
        age = i.sim_minute - inc.opened_minute
        if inc.fix_submitted_minute is None and age >= t.alert_incident_no_fix_min:
            out.append(Firing("INCIDENT_UNRESOLVED", inc.wf, inc.incident_id,
                              f"incident {inc.incident_id} ({inc.kind}) open {age} sim minutes "
                              "without a submitted fix; check NO_SAFE_PROMPT, "
                              "CLIENT_GATE_UNREACHABLE and requests_used_24h"))
        elif age >= t.alert_incident_unresolved_min:
            out.append(Firing("INCIDENT_UNRESOLVED", inc.wf, inc.incident_id,
                              f"incident {inc.incident_id} ({inc.kind}) unresolved after {age} sim "
                              "minutes; check the in-flight fix proposal"))
        if (inc.p0a_in_flight_since is not None
                and i.sim_minute - inc.p0a_in_flight_since > t.alert_drain_wait_min):
            out.append(Firing("INCIDENT_UNRESOLVED", inc.wf, f"{inc.incident_id}:drain",
                              f"DID drain for {inc.incident_id} has waited "
                              f"{i.sim_minute - inc.p0a_in_flight_since} sim minutes behind a "
                              "prompt-safety approval"))
    return out


def _no_healthy_did(i: AlertInputs, t: Any) -> list[Firing]:
    usable = {"healthy", "probing", "unknown", "suspect"}
    return [Firing("NO_HEALTHY_DID", wf, "caller_id",
                   f"every caller ID is {', '.join(sorted(set(st)))}; probes keep running, "
                   "provision a new caller ID if all are degraded")
            for wf, st in sorted(i.did_status.items()) if st and not set(st) & usable]


def _crash_loop(i: AlertInputs, t: Any) -> list[Firing]:
    window = _dt.timedelta(seconds=t.alert_crash_loop_window_s)
    recent = [w for w in i.restart_walls if i.wall_now - w <= window]
    if len(recent) > t.alert_crash_loop_restarts:
        return [Firing("CRASH_LOOP", None, "controller",
                       f"{len(recent)} restarts in the last {t.alert_crash_loop_window_s // 60} "
                       "minutes; inspect intent rows in 'started' and the log tail")]
    return []


def _sim_unreachable(i: AlertInputs, t: Any) -> list[Firing]:
    if i.get_retry_since is None:
        return []
    s = (i.wall_now - i.get_retry_since).total_seconds()
    if s >= t.alert_get_retry_page_s:
        return [Firing("SIM_UNREACHABLE", None, "simulator",
                       f"GETs to the simulator have been retrying for {s / 60:.0f} minutes "
                       "(still retrying); check SIM_URL and the simulator process")]
    return []


RULES = (_ingest, _apply_at_risk, _credits, _incidents, _no_healthy_did, _crash_loop,
         _sim_unreachable)


def evaluate(inputs: AlertInputs, thresholds: Any) -> list[Firing]:
    """Every condition alert that holds now (pure; thresholds from settings.Thresholds)."""
    out: list[Firing] = []
    for rule in RULES:
        out.extend(rule(inputs, thresholds))
    return out


# =============================================================================== store

_INSERT = ("INSERT INTO alert (severity, code, wf, sim_minute, message, runbook, dedupe_key, "
           "closed_wall) VALUES (%s, %s, %s, coalesce(%s, (SELECT sim_minute FROM clock WHERE "
           "id = 1)), %s, %s, %s, CASE WHEN %s THEN now() END) "
           "ON CONFLICT (code, coalesce(wf, -1), dedupe_key) WHERE closed_wall IS NULL "
           "DO NOTHING RETURNING alert_id")


def open_alerts(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT alert_id, severity, code, wf, sim_minute, message, runbook, "
                       "dedupe_key FROM alert WHERE closed_wall IS NULL ORDER BY alert_id")
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def raise_alert(tx: Any, code: str, wf: int | None, subject: str, text: str,
                sim_minute: int | None = None) -> int | None:
    """Record an event alert. Pages stay open until closed; digests are recorded closed.
    `sim_minute` defaults to the controller's clock. Returns the new alert_id, or None if the
    same (code, wf, subject) is already open (the unique index decides, so two writers in one
    race still open one row)."""
    c = CODES[code]
    row = tx.execute(_INSERT, (c.severity, code, wf, sim_minute, text, c.runbook, subject,
                               c.severity == "digest")).fetchone()
    return None if row is None else int(row[0])


def close_alert(tx: Any, alert_id: int) -> bool:
    row = tx.execute("UPDATE alert SET closed_wall = now() WHERE alert_id = %s AND closed_wall "
                     "IS NULL RETURNING alert_id", (alert_id,)).fetchone()
    return row is not None


def sync(tx: Any, firings: Iterable[Firing], sim_minute: int | None = None) -> dict[str, list]:
    """Open condition alerts that started and close those that cleared. Event alerts are not
    touched. Returns {"opened": [...keys], "closed": [...alert ids]}."""
    want = {f.key: f for f in firings if f.code in CONDITION_CODES}
    have: dict[tuple[str, int | None, str], int] = {}
    closed: list[int] = []
    for a in open_alerts(tx):
        if a["code"] not in CONDITION_CODES:
            continue
        key = (a["code"], a["wf"], a["dedupe_key"])
        if key in want and key not in have:
            have[key] = a["alert_id"]
        else:
            close_alert(tx, a["alert_id"])
            closed.append(a["alert_id"])
    opened = []
    for key, f in want.items():
        if key in have:
            continue
        c = CODES[f.code]
        tx.execute(_INSERT, (c.severity, f.code, f.wf, sim_minute, f.message, c.runbook,
                             f.subject, False))
        opened.append(key)
    return {"opened": opened, "closed": closed}


def worst_incident(thresholds: Any, k: int) -> float:
    """W of DESIGN.md 7.2: a FULL verdict on the new prompt plus one on every other candidate,
    13 K with K the largest prompt catalog (the tight-gate evidence runs are left out here)."""
    return float(thresholds.credit_full_verdict) * max(k, 1)


def gather_inputs(conn: Any, thresholds: Any, *, wall_now: _dt.datetime | None = None,
                  next_stop: int | None = None, listed_chunk_max: int | None = None,
                  restart_walls: Iterable[_dt.datetime] = (),
                  get_retry_since: _dt.datetime | None = None) -> AlertInputs:
    """Read `AlertInputs` from the run schema. What only the running loop knows (the chunk
    listing, the next stop, restarts, a GET retry in progress) is passed in."""
    wall = wall_now or conn.execute("SELECT now()").fetchone()[0]
    clock = conn.execute("SELECT sim_minute, tick FROM clock WHERE id = 1").fetchone()
    sim_minute, tick = (int(clock[0]), int(clock[1])) if clock else (0, 15)
    ing = conn.execute("SELECT max(chunk_no), max(ingested_wall) FROM ingested_chunk").fetchone()
    risks = tuple(ApplyRisk(wf=r[0], proposal_id=r[1], state=r[2], apply_by_minute=r[3])
                  for r in conn.execute(
                      "SELECT wf, proposal_id, state, apply_by_minute FROM proposal "
                      "WHERE state IN ('approved', 'applying') AND apply_by_minute IS NOT NULL"))
    reserve = conn.execute("SELECT sum(allotted - spent) FROM credit_envelope "
                           "WHERE pool = 'R'").fetchone()[0]
    k_row = conn.execute("SELECT max(jsonb_array_length(spec->'options'->'prompt_version')) "
                         "FROM workflow").fetchone()
    k = int(k_row[0] or 1)
    low = tuple(r[0] for r in conn.execute(
        "SELECT DISTINCT tenant FROM credit_envelope WHERE mode = 'low' ORDER BY tenant"))
    incidents = []
    for r in conn.execute(
            """SELECT i.wf, i.incident_id, i.kind, i.opened_minute, p.submitted_minute,
                      (SELECT min(q.submitted_minute) FROM proposal q JOIN patch pa
                          ON pa.proposal_id = q.proposal_id
                        WHERE q.wf = i.wf AND q.class = 0 AND pa.p0_rank = 0
                          AND q.state IN ('submitting','pending','approved','applying'))
               FROM incident i LEFT JOIN proposal p ON p.proposal_id = i.fix_proposal_id
               WHERE i.resolved_minute IS NULL ORDER BY i.incident_id"""):
        is_did = "did" in r[2]
        incidents.append(OpenIncident(wf=r[0], incident_id=r[1], kind=r[2], opened_minute=r[3],
                                      fix_submitted_minute=r[4],
                                      p0a_in_flight_since=r[5] if is_did and r[4] is None
                                      else None))
    did_status: dict[int, list[str]] = {}
    for wf, status in conn.execute("SELECT wf, status FROM did_state ORDER BY wf, did"):
        did_status.setdefault(wf, []).append(status)
    return AlertInputs(
        wall_now=wall, sim_minute=sim_minute, tick=tick, next_stop=next_stop,
        listed_chunk_max=listed_chunk_max, ingested_chunk_max=ing[0], last_ingested_wall=ing[1],
        apply_risks=risks, reserve_left=None if reserve is None else float(reserve),
        worst_incident_cost=float(worst_incident(thresholds, k)), low_mode_tenants=low,
        incidents=tuple(incidents), did_status={k: tuple(v) for k, v in did_status.items()},
        restart_walls=tuple(restart_walls), get_retry_since=get_retry_since)
