"""The read models behind the API: the per-tenant supervisor view and the ops view.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.3 (supervisor view, JSON) and 9.1
(`GET /ops`). Pure reads over the run schema; `controller/api/app.py` wires them to HTTP.

The supervisor view answers Kestrel Ops' four questions plus health, one section per workflow:
needs your decision, changed today, being tested, winning, health, upcoming, where your default
does not reach, and the weekly digest. Pickup and engagement are always reported side by side as
two numbers, never blended (constraint 3).
"""
from __future__ import annotations

from typing import Any

from controller.audit.explain import (CLASS_NAMES, DAY, NO_MINUTE, fetch_all, fetch_one, label,
                                      now_minute, weekly_digest, when)

PENDING_STATES = ("pending",)
ANNOUNCED_STATES = ("noticed",)
TESTING_ARM_STATES = ("live", "frozen", "control")
USABLE_DID = ("healthy", "probing", "unknown", "suspect")


class TenantNotFound(LookupError):
    pass


# =============================================================================== words

def arm_words(option: str, scope: str, share: float | None, state: str,
              w1: float | None) -> str:
    """'V3 for age60p~a1b2c3: 10% of calls, probably better, not yet proven'."""
    who = "everyone" if scope == "population" else scope
    pct = "?" if share is None else f"{100 * share:.0f}%"
    if state == "control":
        verdict = "the control"
    elif state == "frozen":
        verdict = "paused at its current share"
    elif w1 is None:
        verdict = "too early to tell"
    elif w1 >= 0.95:
        verdict = "very likely better, waiting for the winner rule's remaining checks"
    elif w1 >= 0.8:
        verdict = "probably better, not yet proven"
    elif w1 <= 0.2:
        verdict = "probably worse; its share is shrinking"
    else:
        verdict = "no clear difference yet"
    return f"{option} for {who}: {pct} of calls, {verdict}"


def _opt(spec: dict[str, Any], dim: str, code: int | None) -> str:
    opts = (spec.get("options") or {}).get(dim) or []
    if code is None:
        return "?"
    return opts[code] if 0 <= code < len(opts) else f"#{code}"


# =============================================================================== sections

def _kpis(conn: Any, wf: int, day: int) -> dict[str, Any]:
    p = fetch_one(conn.execute("SELECT coalesce(sum(dials), 0) AS dials, "
                          "coalesce(sum(connected), 0) AS connected FROM r_pickup "
                          "WHERE wf = %s AND sim_day = %s", (wf, day)))
    e = fetch_one(conn.execute("SELECT coalesce(sum(connected), 0) AS connected, "
                          "coalesce(sum(engaged), 0) AS engaged, coalesce(sum(flags), 0) AS flags "
                          "FROM r_engage WHERE wf = %s AND seg IN (%s, %s)",
                          (wf, 2 * day, 2 * day + 1)))
    assert p is not None and e is not None
    return {
        "day": day,
        "pickup_rate": (p["connected"] / p["dials"]) if p["dials"] else None,
        "dials": int(p["dials"]),
        "engagement_rate": (e["engaged"] / e["connected"]) if e["connected"] else None,
        "connected": int(e["connected"]),
        "flag_rate": (e["flags"] / e["connected"]) if e["connected"] else None,
        "note": "pickup and engagement are separate KPIs and are never combined",
    }


def _needs_decision(conn: Any, wf: int, now: int) -> list[dict[str, Any]]:
    rows = fetch_all(conn.execute(
        """SELECT p.proposal_id, p.class, p.impact_score, p.change_summary, p.diff,
                  p.affected_leads, p.dry_run, p.evidence, p.rationale, p.submitted_minute,
                  a.approval_id, a.created_minute, a.expires_minute, a.status
           FROM proposal p LEFT JOIN approval_mirror a ON a.approval_id = p.approval_id
           WHERE p.wf = %s AND p.state = ANY(%s)
           ORDER BY p.class, p.impact_score DESC NULLS LAST, p.proposal_id""",
        (wf, list(PENDING_STATES))))
    out = []
    for r in rows:
        ev = r["evidence"] or {}
        dry = r["dry_run"] or {}
        out.append({
            "proposal_id": r["proposal_id"], "approval_id": r["approval_id"],
            "class": r["class"], "class_name": CLASS_NAMES.get(r["class"], str(r["class"])),
            "impact_score": r["impact_score"], "impact_kpi": ev.get("kpi"),
            "summary": r["change_summary"], "items": ev.get("items") or [],
            "diff": r["diff"], "affected_leads": r["affected_leads"],
            "dry_run_headline": dry.get("headline", dry),
            "requested_at": when(r["created_minute"] or r["submitted_minute"]),
            "expires_at": when(r["expires_minute"]),
            "minutes_to_expiry": (r["expires_minute"] - now) if r["expires_minute"] is not None
            else None,
            "expected_decision_window": ev.get("expected_decision_window"),
            "why": r["rationale"],
        })
    return out


def _changed_today(conn: Any, wf: int, day: int) -> list[dict[str, Any]]:
    rows = fetch_all(conn.execute(
        """SELECT cv.version, cv.actor, cv.applied_minute, cv.changed_dims, cv.approval_id,
                  p.proposal_id, p.change_summary, p.verify, p.decision_id, p.evidence, p.route
           FROM config_version cv LEFT JOIN proposal p ON p.proposal_id = cv.proposal_id
           WHERE cv.wf = %s AND cv.applied_minute >= %s AND cv.applied_minute < %s
           ORDER BY cv.applied_minute, cv.version""", (wf, day * DAY, (day + 1) * DAY)))
    out = []
    for r in rows:
        ev = r["evidence"] or {}
        summary = r["change_summary"] or ("client supervisor edit" if r["actor"] == "supervisor"
                                          else "(no summary recorded)")
        out.append({
            "version": r["version"], "actor": r["actor"], "at": label(r["applied_minute"]),
            "changed_dims": list(r["changed_dims"] or []), "summary": summary,
            "proposal_id": r["proposal_id"], "approval_id": r["approval_id"],
            "route": r["route"], "verification": r["verify"],
            "decision": (f"/explain/decision/{r['decision_id']}" if r["decision_id"] is not None
                         else None),
            "notes": list(ev.get("notes") or []),
        })
    return out


def _being_tested(conn: Any, wf: int, spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = fetch_all(conn.execute(
        """SELECT e.exp_id, e.dim, e.state AS exp_state, a.arm, a.scope, a.state, a.share,
                  (SELECT l.w1 FROM winner_look l
                    WHERE l.exp_id = a.exp_id AND l.arm = a.arm AND l.scope = a.scope
                    ORDER BY l.look_no DESC LIMIT 1) AS w1,
                  (SELECT count(*) FROM winner_look l
                    WHERE l.exp_id = a.exp_id AND l.arm = a.arm AND l.scope = a.scope) AS looks
           FROM experiment e JOIN experiment_arm a USING (exp_id)
           WHERE e.wf = %s AND a.state = ANY(%s)
           ORDER BY e.exp_id, a.scope, a.arm""", (wf, list(TESTING_ARM_STATES))))
    out = []
    for r in rows:
        name = _opt(spec, r["dim"], r["arm"])
        out.append({"experiment": r["exp_id"], "dim": r["dim"], "option": name,
                    "scope": r["scope"], "state": r["state"], "share": r["share"],
                    "looks_done": int(r["looks"]),
                    "words": arm_words(name, r["scope"], r["share"], r["state"], r["w1"])})
    return out


def _winning(conn: Any, wf: int, spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = fetch_all(conn.execute(
        """SELECT w.winner_id, w.exp_id, w.dim, w.arm, w.scope, w.state, w.declared_minute,
                  d.outcome, h.share AS holdout_share, h.control AS holdout_control,
                  h.state AS holdout_state, m.cusum, m.alarm
           FROM winner w
           LEFT JOIN decision d ON d.decision_id = w.decision_id
           LEFT JOIN holdout h ON h.winner_id = w.winner_id
           LEFT JOIN decay_monitor m ON m.wf = w.wf AND m.scope = w.scope AND m.dim = w.dim
                                    AND m.arm = w.arm
           WHERE w.wf = %s AND w.state <> 'rolled_back'
           ORDER BY w.declared_minute""", (wf,)))
    out = []
    for r in rows:
        oc = r["outcome"] or {}
        out.append({
            "winner_id": r["winner_id"], "experiment": r["exp_id"], "dim": r["dim"],
            "option": _opt(spec, r["dim"], r["arm"]), "scope": r["scope"], "state": r["state"],
            "declared_at": label(r["declared_minute"]), "effect": oc.get("effect"),
            "ci95": oc.get("ci95"),
            "holdout": None if r["holdout_share"] is None else {
                "share": r["holdout_share"], "control": _opt(spec, r["dim"], r["holdout_control"]),
                "state": r["holdout_state"]},
            "decay_monitor": None if r["cusum"] is None else {"cusum": r["cusum"],
                                                              "alarm": r["alarm"]},
        })
    return out


def _health(conn: Any, wf: int, spec: dict[str, Any], now: int) -> dict[str, Any]:
    incidents = fetch_all(conn.execute(
        """SELECT incident_id, kind, state, opened_minute, confirmed_minute, resolved_minute,
                  fix_proposal_id, credits_spent
           FROM incident WHERE wf = %s
           ORDER BY resolved_minute IS NOT NULL, opened_minute DESC LIMIT 20""", (wf,)))
    for i in incidents:
        i["open"] = i["resolved_minute"] is None
        i["timeline"] = [
            {"at": label(r["sim_minute"]), "actor": r["actor"], "action": r["action"],
             "summary": r["summary"]}
            for r in fetch_all(conn.execute(
                "SELECT sim_minute, actor, action, summary FROM audit_log "
                "WHERE subject_type = 'incident' AND subject_id = %s ORDER BY sim_minute, audit_id",
                (i["incident_id"],)))]
        i["opened_at"] = label(i["opened_minute"])
        i["resolved_at"] = when(i["resolved_minute"])
    dids = fetch_all(conn.execute(
        "SELECT did, status, r_hat, r_lcb, r_ucb, n_dials, sprt_llr, since_minute "
        "FROM did_state WHERE wf = %s ORDER BY did", (wf,)))
    for d in dids:
        d["caller_id"] = _opt(spec, "caller_id", d.pop("did"))
    flag = fetch_one(conn.execute("SELECT prompt, p0, stat, h, calls, alarm, reset_minute "
                             "FROM flag_monitor WHERE wf = %s", (wf,)))
    if flag:
        flag["prompt"] = _opt(spec, "prompt_version", flag["prompt"])
        flag["baseline_flag_rate"] = flag.pop("p0")
    drift = fetch_all(conn.execute("SELECT kpi, cusum, up_cusum, alarm, baseline_kind "
                               "FROM drift_monitor WHERE wf = %s ORDER BY kpi", (wf,)))
    return {"incidents": incidents, "caller_ids": dids, "flag_rate_monitor": flag,
            "drift_monitors": drift,
            "no_healthy_caller_id": bool(dids) and not any(d["status"] in USABLE_DID
                                                           for d in dids)}


def _upcoming(conn: Any, wf: int, now: int) -> dict[str, Any]:
    rows = fetch_all(conn.execute(
        """SELECT proposal_id, class, change_summary, notice_until_minute, diff
           FROM proposal WHERE wf = %s AND state = ANY(%s)
           ORDER BY notice_until_minute""", (wf, list(ANNOUNCED_STATES))))
    announced = [{"proposal_id": r["proposal_id"], "summary": r["change_summary"],
                  "class_name": CLASS_NAMES.get(r["class"], str(r["class"])),
                  "applies_at": when(r["notice_until_minute"]),
                  "minutes_until": (r["notice_until_minute"] - now)
                  if r["notice_until_minute"] is not None else None,
                  "diff": r["diff"]} for r in rows]
    return {"announced_auto_applies": announced,
            "next_routine_slot": None,
            "next_routine_slot_note": "computed by governance timing (6.3); not in the store yet"}


def _live_version(conn: Any, wf: int, now: int) -> int | None:
    row = conn.execute("SELECT max(version) FROM config_version WHERE wf = %s AND "
                       "coalesce(applied_minute, %s) <= %s", (wf, NO_MINUTE, now)).fetchone()
    return None if row is None else row[0]


def _default_does_not_reach(conn: Any, wf: int, version: int | None) -> list[dict[str, Any]]:
    if version is None:
        return []
    rows = fetch_all(conn.execute(
        """SELECT cc.position, cc.cohort_id, coalesce(cc.cohort_key, cc.cohort_id) AS cohort_key,
                  cc.owner, cd.origin, cd.where_canon,
                  coalesce(array_agg(DISTINCT ap.dim ORDER BY ap.dim)
                           FILTER (WHERE ap.inherited = false), '{}') AS overrides
           FROM config_cohort cc
           LEFT JOIN cohort_def cd ON cd.cohort_key = cc.cohort_key
           LEFT JOIN allocation_provenance ap
                  ON ap.wf = cc.wf AND ap.version = cc.version
                 AND ap.cohort_key = coalesce(cc.cohort_key, cc.cohort_id)
           WHERE cc.wf = %s AND cc.version = %s AND cc.cohort_id <> 'default'
           GROUP BY cc.position, cc.cohort_id, cc.cohort_key, cc.owner, cd.origin, cd.where_canon
           ORDER BY cc.position""", (wf, version)))
    out = []
    for r in rows:
        overrides = [d for d in (r["overrides"] or []) if d]
        if not overrides and r["owner"] != "supervisor":
            continue
        why = []
        if overrides:
            why.append(f"has its own allocation for {', '.join(overrides)}, so a change to the "
                       "default does not reach it")
        if r["owner"] == "supervisor":
            why.append("a business cohort: we never redefine it")
        out.append({"cohort": r["cohort_key"], "position": r["position"], "owner": r["owner"],
                    "origin": r["origin"], "where": r["where_canon"], "overrides": overrides,
                    "words": f"cohort {r['cohort_key']} " + "; ".join(why)})
    return out


def _holds(conn: Any, wf: int, now: int) -> list[dict[str, Any]]:
    rows = fetch_all(conn.execute(
        "SELECT hold_id, set_by, reason, from_minute, until_minute, released_minute, released_by "
        "FROM hold "
        "WHERE wf = %s AND (released_minute IS NULL OR released_minute > %s) "
        "AND (until_minute IS NULL OR until_minute > %s) ORDER BY hold_id", (wf, now, now)))
    for r in rows:
        r["from"] = label(r["from_minute"])
        r["until"] = when(r["until_minute"])
    return rows


def _requests_24h(conn: Any, wf: int, now: int) -> int:
    row = conn.execute("SELECT count(*) FROM approval_mirror WHERE wf = %s AND "
                       "created_minute > %s AND created_minute <= %s",
                       (wf, now - DAY, now)).fetchone()
    return int(row[0])


def supervisor_view(conn: Any, tenant_id: str) -> dict[str, Any]:
    """One screen per tenant (DESIGN.md 9.3)."""
    wfs = fetch_all(conn.execute("SELECT wf, workflow_id, spec FROM workflow WHERE tenant_id = %s "
                             "ORDER BY workflow_id", (tenant_id,)))
    if not wfs:
        raise TenantNotFound(tenant_id)
    now = now_minute(conn) or 0
    day = now // DAY
    sections = []
    for w in wfs:
        spec = w["spec"] or {}
        version = _live_version(conn, w["wf"], now)
        gov = spec.get("governance") or {}
        sections.append({
            "workflow_id": w["workflow_id"],
            "use_case": spec.get("use_case"),
            "primary_kpi": (spec.get("objective") or {}).get("primary"),
            "live_version": version,
            "kpis_today": _kpis(conn, w["wf"], day),
            "requests_used_24h": _requests_24h(conn, w["wf"], now),
            "request_cap_24h": gov.get("max_requests_per_24h"),
            "holds": _holds(conn, w["wf"], now),
            "needs_your_decision": _needs_decision(conn, w["wf"], now),
            "changed_today": _changed_today(conn, w["wf"], day),
            "being_tested": _being_tested(conn, w["wf"], spec),
            "winning": _winning(conn, w["wf"], spec),
            "health": _health(conn, w["wf"], spec, now),
            "upcoming": _upcoming(conn, w["wf"], now),
            "where_your_default_does_not_reach": _default_does_not_reach(conn, w["wf"], version),
        })
    credits = fetch_all(conn.execute(
        "SELECT pool, tenant, allotted, spent, allotted - spent AS left, mode "
        "FROM credit_envelope WHERE tenant IN (%s, '*') ORDER BY pool, tenant", (tenant_id,)))
    digest = weekly_digest(conn, tenant_id, until=now)
    return {"tenant_id": tenant_id, "now": label(now), "sim_minute": now,
            "workflows": sections, "credits": credits, "weekly_digest": digest.to_json()}


# =============================================================================== ops

def ops_view(conn: Any, steps: int = 20) -> dict[str, Any]:
    """Loop health (DESIGN.md 9.1): recent steps, lags, liveness, credits, invariants, alerts."""
    now = now_minute(conn)
    clock = fetch_one(conn.execute("SELECT sim_minute, end_minute, tick, last_step_no FROM clock "
                              "WHERE id = 1"))
    recent = fetch_all(conn.execute(
        "SELECT step_no, from_minute, to_minute, phase_ms, chunks, events, orphan_calls, "
        "decisions, proposals_submitted, applies, credits_remaining, rss_mb, db_bytes, wall "
        "FROM loop_step ORDER BY step_no DESC LIMIT %s", (steps,)))
    ingest = fetch_one(conn.execute(
        "SELECT max(until_minute) AS until_minute, max(chunk_no) AS last_chunk, "
        "max(ingested_wall) AS last_wall, count(*) AS chunks, coalesce(sum(events), 0) AS events "
        "FROM ingested_chunk"))
    assert ingest is not None
    hb = fetch_one(conn.execute("SELECT wall, pid, step_no, "
                           "extract(epoch FROM now() - wall)::float8 AS age_s FROM heartbeat WHERE id = 1"))
    invariants = fetch_one(conn.execute(
        """SELECT count(*) FILTER (WHERE state = 'failed_policy') AS failed_policy,
                  count(*) FILTER (WHERE state = 'lapsed_local') AS lapsed_local,
                  count(*) FILTER (WHERE state = 'expired') AS expired,
                  count(*) FILTER (WHERE state = 'stale') AS stale
           FROM proposal"""))
    open_alerts = fetch_all(conn.execute(
        "SELECT alert_id, severity, code, wf, sim_minute, opened_wall, message, runbook "
        "FROM alert WHERE closed_wall IS NULL ORDER BY severity DESC, opened_wall"))
    remaining = conn.execute("SELECT remaining_after FROM credit_ledger "
                             "ORDER BY seq DESC LIMIT 1").fetchone()
    by_wf = fetch_all(conn.execute(
        """SELECT w.workflow_id, w.tenant_id,
                  (SELECT count(*) FROM approval_mirror a WHERE a.wf = w.wf
                     AND a.created_minute > %(lo)s AND a.created_minute <= %(now)s) AS requests_24h,
                  (w.spec->'governance'->>'max_requests_per_24h')::int AS request_cap_24h,
                  (SELECT count(*) FROM proposal p WHERE p.wf = w.wf
                     AND p.state IN ('submitting','pending','approved','applying','noticed',
                                     'auto_applying')) AS in_flight
           FROM workflow w ORDER BY w.workflow_id""",
        {"lo": (now or 0) - DAY, "now": now or 0}))
    db_bytes = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    return {
        "now": when(now), "clock": clock,
        "ingest": {**ingest,
                   "lag_sim_min": (now - ingest["until_minute"])
                   if now is not None and ingest["until_minute"] is not None else None},
        "heartbeat": hb, "recent_steps": recent, "governance_invariants": invariants,
        "credits_remaining": None if remaining is None else remaining[0],
        "workflows": by_wf, "open_alerts": open_alerts, "db_bytes": int(db_bytes),
    }
