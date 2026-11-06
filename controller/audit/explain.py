"""The audit queries: why a workflow changed, why a customer heard what they heard, and the
weekly operational digest.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.4 (C12) and the digest of 9.3 item 8.

Every answer comes from our own store, never from the simulator: the simulator keeps no config
bodies and never names the approver, so only the audit store can answer Compliance (IRDAI asks
"why did this customer hear this, within 24 hours").

All functions take a psycopg connection whose `search_path` is the run schema (the controller's
`Database.tx()`, the API's read-only transaction, or the CLI). They only read.

Conventions the writers follow (T1 `audit/writer.py`, T2 governance), stated here because the
queries depend on them:
- A `config_version` audit row has `subject_type = 'config_version'` and
  `subject_id = config_version_subject(workflow_id, version)`; a supervisor version's chain of
  consequences starts from it (9.4: stale marks, interrupts, rebase, cooldown).
- A proposal's audit rows have `subject_type = 'proposal'` and `subject_id = proposal_id`; every
  consequence row sets `caused_by` to the row that caused it.
- A decision not to act has `kind = 'no_action'` (the weekly digest counts those of the caller-ID
  and schedule modules).
- `proposal.evidence` may carry `kpi` (the impact score's KPI label), `notes` (supervisor-facing
  lines such as "3rd request today, above your 2/day preference"), `items` and
  `expected_decision_window`; the supervisor view shows them as given.
- `allocation_provenance` is materialised for every cohort of every version, and for
  `prompt_version` under cohort_key 'default'. A missing provenance row for an option a call
  actually heard is a finding (`COHORT_MISMATCH`), never silently skipped.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from controller.types import label

DAY = 1440
NO_MINUTE = -(2 ** 31)
"""Sort key for the business default (version 1 has no applied_minute)."""

CLASS_NAMES = {0: "incident", 1: "rollback", 2: "promotion", 3: "routine"}
OPERATIONAL_DIMS = ("caller_id", "schedule")
SOURCE_WORDS = {
    "default": "business default",
    "arm": "experiment arm",
    "winner": "declared winner",
    "holdout": "holdout control",
    "probe": "caller-ID probe",
    "supervisor": "set by the client supervisor",
    "rollback": "rollback",
}
_WHEN_RE = re.compile(r"^\s*[Dd](-?\d+)\s+(\d{1,2}):(\d{2})\s*$")


class NotFound(LookupError):
    """The workflow, lead or version asked about is not in the store."""


# =============================================================================== time

def parse_when(text: str | int) -> int:
    """'D3 14:00' -> simulated minute 4440; a plain integer (string) is a minute already."""
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    m = _WHEN_RE.match(s)
    if not m:
        raise ValueError(f"expected 'D<day> HH:MM' or a minute, got {text!r}")
    d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if h > 23 or mi > 59:
        raise ValueError(f"bad time of day in {text!r}")
    return d * DAY + h * 60 + mi


def when(minute: int | None) -> str | None:
    return None if minute is None else label(minute)


def config_version_subject(workflow_id: str, version: int) -> str:
    """`audit_log.subject_id` of a config version's audit row (see module docstring)."""
    return f"{workflow_id}@v{version}"


def now_minute(conn: Any) -> int | None:
    """The controller's clock (`clock.sim_minute`), or None before the first step."""
    row = conn.execute("SELECT sim_minute FROM clock WHERE id = 1").fetchone()
    return None if row is None else int(row[0])


# =============================================================================== small helpers

def fetch_all(cur: Any) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_one(cur: Any) -> dict[str, Any] | None:
    rows = fetch_all(cur)
    return rows[0] if rows else None


def _workflow(conn: Any, workflow_id: str) -> dict[str, Any]:
    row = fetch_one(conn.execute("SELECT wf, workflow_id, tenant_id, spec FROM workflow "
                            "WHERE workflow_id = %s", (workflow_id,)))
    if row is None:
        raise NotFound(f"unknown workflow {workflow_id!r}")
    return row


def option_name(spec: dict[str, Any] | None, dim: str, code: int | None) -> str | None:
    """Option code -> option name, from the workflow's option list as the API returned it."""
    if code is None or not spec:
        return None
    opts = (spec.get("options") or {}).get(dim) or []
    return opts[code] if 0 <= code < len(opts) else f"#{code}"


def _pct(x: float | None) -> str:
    if x is None:
        return "?"
    v = 100.0 * float(x)
    return f"{v:.0f}%" if abs(v - round(v)) < 0.05 else f"{v:.1f}%"


# =============================================================================== why_changed

@dataclass(frozen=True)
class AuditEvent:
    """One row of the "what happened after" chain, in order."""

    audit_id: int
    sim_minute: int
    at: str
    actor: str
    action: str
    subject: str
    summary: str
    caused_by: int | None


@dataclass(frozen=True)
class ChangeExplanation:
    """Answer to "why did workflow X change at T, who approved it, and what happened after?"."""

    workflow_id: str
    at_minute: int
    at: str
    version: int
    applied_minute: int | None
    applied_at: str | None
    actor: str
    changed_dims: list[str]
    body_source: str
    proposal: dict[str, Any] | None
    approval: dict[str, Any] | None
    decision: dict[str, Any] | None
    transitions: list[dict[str, Any]]
    after: list[AuditEvent]
    findings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        return render_change(self)


_CHANGE_SQL = """
WITH v AS (
  SELECT cv.*
  FROM config_version cv
  WHERE cv.wf = %(wf)s AND coalesce(cv.applied_minute, %(none)s) <= %(t)s
  ORDER BY coalesce(cv.applied_minute, %(none)s) DESC, cv.version DESC
  LIMIT 1)
SELECT v.version, v.applied_minute, v.actor, v.changed_dims, v.body_source,
       v.proposal_id AS v_proposal_id, v.approval_id AS v_approval_id,
       p.proposal_id, p.class, p.route, p.state, p.change_summary, p.rationale, p.impact_score,
       p.affected_leads, p.dry_run, p.diff, p.evidence, p.verify, p.submitted_minute,
       p.base_version, p.decision_id,
       a.approval_id, a.created_minute, a.decided_minute, a.expires_minute, a.apply_by_minute,
       a.status AS approval_status, a.reasons, a.approver_name,
       d.decision_id AS d_id, d.sim_minute AS d_minute, d.module, d.kind, d.rule_id, d.inputs,
       d.outcome, d.harm_report, d.snapshot_ids, d.seed, d.settings_hash, d.code_version
FROM v
LEFT JOIN proposal p ON p.proposal_id = v.proposal_id
LEFT JOIN approval_mirror a ON a.approval_id = coalesce(v.approval_id, p.approval_id)
LEFT JOIN decision d ON d.decision_id = p.decision_id
"""

_CHAIN_SQL = """
WITH RECURSIVE chain AS (
  SELECT * FROM audit_log WHERE subject_type = %(st)s AND subject_id = %(sid)s
  UNION
  SELECT a.* FROM audit_log a JOIN chain c ON a.caused_by = c.audit_id)
SELECT audit_id, sim_minute, actor, action, subject_type, subject_id, summary, caused_by
FROM chain ORDER BY sim_minute, audit_id
"""


def audit_chain(conn: Any, subject_type: str, subject_id: str) -> list[AuditEvent]:
    """Every audit row about a subject plus everything it caused, transitively, in order."""
    rows = fetch_all(conn.execute(_CHAIN_SQL, {"st": subject_type, "sid": subject_id}))
    return [AuditEvent(audit_id=r["audit_id"], sim_minute=r["sim_minute"],
                       at=label(r["sim_minute"]), actor=r["actor"], action=r["action"],
                       subject=f"{r['subject_type']}:{r['subject_id']}", summary=r["summary"],
                       caused_by=r["caused_by"]) for r in rows]


def why_changed(conn: Any, workflow_id: str, at: str | int) -> ChangeExplanation:
    """The config version live on `workflow_id` at `at`, the proposal and approval behind it,
    the decision (with its statistics at decision time), and everything that followed."""
    t = parse_when(at)
    wf = _workflow(conn, workflow_id)
    r = fetch_one(conn.execute(_CHANGE_SQL, {"wf": wf["wf"], "t": t, "none": NO_MINUTE}))
    if r is None:
        raise NotFound(f"{workflow_id}: no config version recorded at or before {label(t)}")
    findings: list[str] = []
    proposal = approval = decision = None
    transitions: list[dict[str, Any]] = []
    if r["proposal_id"]:
        proposal = {k: r[k] for k in ("proposal_id", "class", "route", "state", "change_summary",
                                      "rationale", "impact_score", "affected_leads", "dry_run",
                                      "diff", "evidence", "verify", "base_version",
                                      "submitted_minute")}
        proposal["class_name"] = CLASS_NAMES.get(r["class"], str(r["class"]))
        proposal["submitted_at"] = when(r["submitted_minute"])
        transitions = fetch_all(conn.execute(
            "SELECT seq, from_state, to_state, sim_minute, reason, decision_id "
            "FROM proposal_transition WHERE proposal_id = %s ORDER BY seq", (r["proposal_id"],)))
        for tr in transitions:
            tr["at"] = label(tr["sim_minute"])
    elif r["v_proposal_id"]:
        findings.append(f"version {r['version']} names proposal {r['v_proposal_id']}, which is not "
                        "in our proposal table (UNKNOWN_WRITER)")
    elif r["actor"] in ("controller_approved", "controller_auto"):
        findings.append(f"version {r['version']} was written by the controller but carries no "
                        "proposal id (UNKNOWN_WRITER)")
    if r["approval_id"]:
        approval = {k: r[k] for k in ("approval_id", "created_minute", "decided_minute",
                                      "expires_minute", "apply_by_minute", "reasons",
                                      "approver_name")}
        approval["status"] = r["approval_status"]
        approval["created_at"] = when(r["created_minute"])
        approval["decided_at"] = when(r["decided_minute"])
        if not r["approver_name"]:
            approval["approver_note"] = ("the simulator API returns only approval_id and "
                                         "decided_at, never the approver's identity")
    if r["d_id"] is not None:
        decision = {"decision_id": r["d_id"], "sim_minute": r["d_minute"],
                    "at": label(r["d_minute"]), "module": r["module"], "kind": r["kind"],
                    "rule_id": r["rule_id"], "inputs": r["inputs"], "outcome": r["outcome"],
                    "harm_report": r["harm_report"], "snapshot_ids": list(r["snapshot_ids"] or []),
                    "seed": r["seed"], "settings_hash": r["settings_hash"],
                    "code_version": r["code_version"]}
    elif proposal is not None:
        findings.append(f"proposal {proposal['proposal_id']} has no decision row: every change "
                        "must be recorded before it is made")
    if proposal is not None:
        after = audit_chain(conn, "proposal", proposal["proposal_id"])
    else:
        after = audit_chain(conn, "config_version", config_version_subject(workflow_id, r["version"]))
    return ChangeExplanation(
        workflow_id=workflow_id, at_minute=t, at=label(t), version=r["version"],
        applied_minute=r["applied_minute"], applied_at=when(r["applied_minute"]), actor=r["actor"],
        changed_dims=list(r["changed_dims"] or []), body_source=r["body_source"],
        proposal=proposal, approval=approval, decision=decision, transitions=transitions,
        after=after, findings=findings)


def render_change(x: ChangeExplanation) -> str:
    applied = f"applied {x.applied_at}" if x.applied_at else "the business default"
    dims = ", ".join(x.changed_dims) if x.changed_dims else "nothing (initial config)"
    out = [f"{x.workflow_id} at {x.at}: version {x.version}, {applied} by {x.actor}; "
           f"changed {dims}."]
    p = x.proposal
    if p:
        out.append(f"  Proposal {p['proposal_id']} ({p['class_name']}, route {p['route']}, now "
                   f"{p['state']}), submitted {p['submitted_at'] or '-'} on base version "
                   f"{p['base_version']}: {p['change_summary'] or '(no summary)'}")
        if p["rationale"]:
            out.append(f"  Why: {p['rationale']}")
        kpi = (p.get("evidence") or {}).get("kpi") if isinstance(p.get("evidence"), dict) else None
        out.append(f"  Impact score {p['impact_score']}"
                   + (f" ({kpi})" if kpi else "")
                   + f"; affected leads {p['affected_leads']}.")
    elif x.actor == "supervisor":
        out.append("  A client supervisor edit (no proposal of ours). Our reaction is below.")
    a = x.approval
    if a:
        out.append(f"  Approval {a['approval_id']}: requested {a['created_at'] or '-'}, decided "
                   f"{a['decided_at'] or '-'}, status {a['status']}. Approver: "
                   f"{a['approver_name'] or 'not available (the API never names the approver)'}.")
    elif p and p["route"] == "auto":
        out.append("  Auto-applied under the workflow's auto-apply policy (no approval).")
    d = x.decision
    if d:
        out.append(f"  Decision {d['decision_id']} at {d['at']}: {d['module']} / {d['kind']} "
                   f"(rule {d['rule_id']}); settings {d['settings_hash']}, code "
                   f"{d['code_version']}.")
        hr = d.get("harm_report")
        if hr:
            out.append(f"    harm gate: de mean {hr.get('de_mean')}, P(de < -1%) "
                       f"{hr.get('p_de_below_1pct')}, dd mean {hr.get('dd_mean')}, "
                       f"P(dd < -2%) {hr.get('p_dd_below_2pct')}, passes {hr.get('passes')}")
    if p and p.get("verify"):
        out.append(f"  Verification: {p['verify']}")
    if x.after:
        out.append("  What happened after:")
        out.extend(f"    {e.at}  {e.actor:<12} {e.action:<20} {e.summary}" for e in x.after)
    for f in x.findings:
        out.append(f"  FINDING: {f}")
    return "\n".join(out)


# =============================================================================== why_heard

@dataclass(frozen=True)
class DimProvenance:
    """Why one call heard one option of one dim."""

    dim: str
    option_code: int | None
    option: str | None
    version: int | None
    scope: str
    """the cohort the share applies to ('default' for prompt_version)"""
    share: float | None
    source: str | None
    inherited: bool | None
    experiment_id: str | None
    proposal_id: str | None
    approval_id: str | None
    approval_decided_at: str | None
    decision_id: int | None
    missing: bool
    sentence: str


@dataclass(frozen=True)
class HeardCall:
    """One call to the lead on the day asked about, with per-dim provenance."""

    event_id: int
    minute: int
    at: str
    attempt_no: int
    campaign_id: str
    config_version: int
    live_version: int | None
    cohort_id: str | None
    cohort_key: str | None
    connected: bool
    engaged: bool
    flag: bool
    dims: list[DimProvenance]


@dataclass(frozen=True)
class HeardExplanation:
    lead_id: str
    person_id: str
    workflow_id: str
    day: int
    features: dict[str, Any] | None
    calls: list[HeardCall]
    findings: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        return render_heard(self)


_HEARD_SQL = """
WITH k AS (
  SELECT c.event_id, c.wf, c.minute, c.attempt_no, c.config_version, c.cohort_code,
         c.w, c.v, c.did, c.prompt, c.connected, c.engaged, c.flag,
         l.lead_id, l.person_id, l.campaign_id, l.cell,
         cc.cohort_id, coalesce(cc.cohort_key, cc.cohort_id) AS cohort_key,
         (SELECT max(cv.version) FROM config_version cv
           WHERE cv.wf = c.wf AND coalesce(cv.applied_minute, %(none)s) <= c.minute) AS live_version
  FROM call_fact c
  JOIN lead_dim l USING (lead_sk)
  LEFT JOIN config_cohort cc ON cc.wf = c.wf AND cc.version = c.config_version
                            AND cc.cohort_code = c.cohort_code
  WHERE l.lead_id = %(lead)s AND c.sim_day = %(day)s)
SELECT k.*, d.dim, d.option, d.version, d.scope_key,
       ap.share, ap.source, ap.inherited, ap.experiment_id, ap.proposal_id, ap.approval_id,
       ap.decision_id, am.decided_minute AS approval_decided_minute,
       w.workflow_id, w.spec
FROM k
JOIN workflow w ON w.wf = k.wf
CROSS JOIN LATERAL (VALUES ('welcome_message', k.w,      k.config_version, k.cohort_key, 1),
                           ('voice',           k.v,      k.config_version, k.cohort_key, 2),
                           ('caller_id',       k.did,    k.config_version, k.cohort_key, 3),
                           ('prompt_version',  k.prompt, k.live_version,   'default',    4))
     AS d(dim, option, version, scope_key, ord)
LEFT JOIN allocation_provenance ap
       ON ap.wf = k.wf AND ap.version = d.version AND ap.cohort_key = d.scope_key
      AND ap.dim = d.dim AND ap.option = d.option
LEFT JOIN approval_mirror am ON am.approval_id = ap.approval_id
ORDER BY k.minute, k.event_id, d.ord
"""


def _sentence(dim: str, name: str | None, r: dict[str, Any]) -> str:
    if r["source"] is None:
        return (f"{dim} {name}: NO provenance row for version {r['version']} cohort "
                f"{r['scope_key']}; our record says this option was not live (COHORT_MISMATCH).")
    parts = [f"{dim} {name} at {_pct(r['share'])}"]
    src = SOURCE_WORDS.get(r["source"], r["source"])
    if r["source"] == "arm" and r["experiment_id"]:
        src = f"arm of {r['experiment_id']}"
    parts.append(f": {src}")
    if r["inherited"]:
        parts.append(f" (inherited from default: cohort {r['cohort_key']} has no override)")
    if r["proposal_id"]:
        parts.append(f", proposed by {r['proposal_id']}")
    if r["approval_id"]:
        dec = when(r["approval_decided_minute"])
        parts.append(f", approved {r['approval_id']}" + (f" at {dec}" if dec else ""))
    if dim == "prompt_version":
        parts.append(f"; the prompt live at the call minute (version {r['version']}), one per "
                     "workflow, applied immediately even on locked campaigns.")
    else:
        parts.append(f"; dialled with version {r['version']}, randomised per call.")
    return "".join(parts)


def why_heard(conn: Any, lead_id: str, day: int) -> HeardExplanation:
    """Every call to `lead_id` on sim day `day`, each with the provenance of the welcome, voice
    and caller ID (from the version the call was dialled with) and of the prompt (from the
    version live at the call's minute)."""
    lead = fetch_one(conn.execute(
        "SELECT l.lead_id, l.person_id, l.wf, w.workflow_id, c.lang, c.tier, c.age, c.value "
        "FROM lead_dim l JOIN workflow w USING (wf) LEFT JOIN cell c ON c.cell_id = l.cell "
        "WHERE l.lead_id = %s", (lead_id,)))
    if lead is None:
        raise NotFound(f"unknown lead {lead_id!r}")
    rows = fetch_all(conn.execute(_HEARD_SQL, {"lead": lead_id, "day": int(day), "none": NO_MINUTE}))
    calls: list[HeardCall] = []
    findings: list[str] = []
    by_event: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_event.setdefault(r["event_id"], []).append(r)
    for event_id, dims in by_event.items():
        r0 = dims[0]
        if r0["cohort_id"] is None:
            findings.append(f"call {event_id} at {label(r0['minute'])}: cohort code "
                            f"{r0['cohort_code']} of version {r0['config_version']} is not in "
                            "config_cohort (COHORT_MISMATCH)")
        provs = []
        for r in dims:
            name = option_name(r["spec"], r["dim"], r["option"])
            missing = r["source"] is None
            if missing and r0["cohort_id"] is not None:
                findings.append(f"call {event_id} at {label(r['minute'])}: {r['dim']} {name} has no "
                                f"provenance in version {r['version']} cohort {r['scope_key']} "
                                "(COHORT_MISMATCH)")
            provs.append(DimProvenance(
                dim=r["dim"], option_code=r["option"], option=name, version=r["version"],
                scope=r["scope_key"], share=r["share"], source=r["source"],
                inherited=r["inherited"], experiment_id=r["experiment_id"],
                proposal_id=r["proposal_id"], approval_id=r["approval_id"],
                approval_decided_at=when(r["approval_decided_minute"]),
                decision_id=r["decision_id"], missing=missing, sentence=_sentence(r["dim"], name, r)))
        calls.append(HeardCall(
            event_id=event_id, minute=r0["minute"], at=label(r0["minute"]),
            attempt_no=r0["attempt_no"], campaign_id=r0["campaign_id"],
            config_version=r0["config_version"], live_version=r0["live_version"],
            cohort_id=r0["cohort_id"], cohort_key=r0["cohort_key"], connected=r0["connected"],
            engaged=r0["engaged"], flag=r0["flag"], dims=provs))
    features = None
    if lead["lang"] is not None:
        features = {"lang": lead["lang"], "city_tier": lead["tier"], "age_band": lead["age"],
                    "value_band": lead["value"]}
    return HeardExplanation(lead_id=lead_id, person_id=lead["person_id"],
                            workflow_id=lead["workflow_id"], day=int(day), features=features,
                            calls=calls, findings=findings)


def render_heard(x: HeardExplanation) -> str:
    feats = ", ".join(f"{k} {v}" for k, v in (x.features or {}).items()) or "features unknown"
    out = [f"Lead {x.lead_id} (person {x.person_id}, {x.workflow_id}; {feats}) on D{x.day}: "
           f"{len(x.calls)} call(s)."]
    for c in x.calls:
        outcome = "engaged" if c.engaged else ("connected" if c.connected else "not connected")
        out.append(f"  {c.at} attempt {c.attempt_no}, campaign {c.campaign_id}, cohort "
                   f"{c.cohort_key or '?'}, dialled with version {c.config_version} (live "
                   f"{c.live_version}): {outcome}{', flagged' if c.flag else ''}.")
        out.extend(f"    {d.sentence}" for d in c.dims)
    for f in x.findings:
        out.append(f"  FINDING: {f}")
    return "\n".join(out)


# =============================================================================== weekly digest

@dataclass(frozen=True)
class DigestItem:
    workflow_id: str
    version: int
    at: str
    actor: str
    changed_dims: list[str]
    summary: str
    reason: str
    proposal_id: str | None
    approval_id: str | None
    decision_id: int | None
    rule: str | None


@dataclass(frozen=True)
class WeeklyDigest:
    tenant_id: str
    from_minute: int
    to_minute: int
    period: str
    items: list[DigestItem]
    incidents: list[dict[str, Any]]
    not_acted: int
    """decisions in the period on operational levers that chose not to act"""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        out = [f"Operational changes for {self.tenant_id}, {self.period}: {len(self.items)} "
               f"change(s), {len(self.incidents)} incident(s), {self.not_acted} decision(s) not "
               "to act."]
        for i in self.items:
            out.append(f"  {i.at} {i.workflow_id} v{i.version} ({i.actor}, "
                       f"{', '.join(i.changed_dims)}): {i.summary}")
            if i.reason:
                out.append(f"    why: {i.reason}")
        for inc in self.incidents:
            out.append(f"  incident {inc['incident_id']} on {inc['workflow_id']} ({inc['kind']}): "
                       f"{inc['state']}, opened {inc['opened_at']}, resolved "
                       f"{inc['resolved_at'] or '-'}")
        return "\n".join(out)


def weekly_digest(conn: Any, tenant_id: str, until: str | int | None = None,
                  days: int = 7, dims: Iterable[str] = OPERATIONAL_DIMS) -> WeeklyDigest:
    """Operational changes (caller ID, schedule by default) on a tenant's workflows over the
    `days` before `until` (default: the controller's clock), with the reason for each, from our
    proposal and decision rows (Northwind memo 02 item 5)."""
    to_m = parse_when(until) if until is not None else (now_minute(conn) or 0)
    from_m = to_m - days * DAY
    dims = list(dims)
    rows = fetch_all(conn.execute(
        """SELECT w.workflow_id, cv.version, cv.applied_minute, cv.actor, cv.changed_dims,
                  p.proposal_id, p.change_summary, p.rationale, cv.approval_id,
                  d.decision_id, d.module, d.kind, d.rule_id
           FROM config_version cv
           JOIN workflow w USING (wf)
           LEFT JOIN proposal p ON p.proposal_id = cv.proposal_id
           LEFT JOIN decision d ON d.decision_id = p.decision_id
           WHERE w.tenant_id = %s AND cv.applied_minute > %s AND cv.applied_minute <= %s
             AND cv.changed_dims && %s::text[]
           ORDER BY cv.applied_minute, w.workflow_id, cv.version""",
        (tenant_id, from_m, to_m, dims)))
    items = [DigestItem(
        workflow_id=r["workflow_id"], version=r["version"], at=label(r["applied_minute"]),
        actor=r["actor"], changed_dims=list(r["changed_dims"] or []),
        summary=r["change_summary"] or ("client supervisor edit" if r["actor"] == "supervisor"
                                        else "(no summary recorded)"),
        reason=r["rationale"] or "", proposal_id=r["proposal_id"], approval_id=r["approval_id"],
        decision_id=r["decision_id"],
        rule=f"{r['module']}/{r['rule_id']}" if r["module"] else None) for r in rows]
    incidents = fetch_all(conn.execute(
        """SELECT i.incident_id, w.workflow_id, i.kind, i.state, i.opened_minute,
                  i.resolved_minute
           FROM incident i JOIN workflow w USING (wf)
           WHERE w.tenant_id = %s AND i.opened_minute > %s AND i.opened_minute <= %s
           ORDER BY i.opened_minute""", (tenant_id, from_m, to_m)))
    for inc in incidents:
        inc["opened_at"] = label(inc["opened_minute"])
        inc["resolved_at"] = when(inc["resolved_minute"])
    not_acted = conn.execute(
        """SELECT count(*) FROM decision d JOIN workflow w USING (wf)
           WHERE w.tenant_id = %s AND d.sim_minute > %s AND d.sim_minute <= %s
             AND d.kind = 'no_action'
             AND (d.module LIKE '%%did%%' OR d.module LIKE '%%schedule%%')""",
        (tenant_id, from_m, to_m)).fetchone()[0]
    return WeeklyDigest(tenant_id=tenant_id, from_minute=from_m, to_minute=to_m,
                        period=f"{label(from_m)} to {label(to_m)}", items=items,
                        incidents=incidents, not_acted=int(not_acted))
