"""The audit store writer: decision rows, audit log rows and allocation provenance.

Owner: track T1 (milestone M2). Implements DESIGN.md 8.2 (every decision is in the store before
its action), 9.4 (provenance materialised at write time) and 11.2 (`record`).

All three functions write inside the caller's transaction and never commit, so a decision, the
state change it causes and its audit row land together or not at all. Rows carry the simulated
minute (from `clock` when the caller gives none) and never a wall-clock value in their content,
so two runs of one world write identical rows (DESIGN.md 8.6; `audit_log.wall` is the only wall
column and the determinism test excludes it).
"""
from __future__ import annotations

from typing import Any, Mapping

from psycopg.types.json import Jsonb

from controller.reliability.faults import maybe_crash
from controller.types import AGENT_DIMS, Config, Decision, Tx, WorkflowSpec, thaw

_CLOCK = "(SELECT sim_minute FROM clock WHERE id = 1)"
_WF = "(SELECT wf FROM workflow WHERE workflow_id = %s)"


def write_decision(tx: Tx, d: Decision) -> int:
    """Insert a decision row and return its id. `harm_report` is stored as its dataclass fields."""
    harm = None
    if d.harm_report is not None:
        harm = {k: thaw(v) for k, v in vars(d.harm_report).items()}
    r = tx.execute(
        f"""INSERT INTO decision (sim_minute, wf, module, kind, rule_id, inputs, outcome,
                                  harm_report, parent_decision_id, snapshot_ids, seed,
                                  settings_hash, code_version)
            VALUES (coalesce(%s, {_CLOCK}), {_WF}, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING decision_id""",
        (d.sim_minute, d.wf, d.module, d.kind, d.rule_id, Jsonb(thaw(d.inputs)),
         Jsonb(thaw(d.outcome)), Jsonb(harm) if harm is not None else None,
         d.parent_decision_id, list(d.snapshot_ids), d.seed, d.settings_hash,
         d.code_version)).fetchone()
    return int(r[0])


def set_outcome(tx: Tx, decision_id: int, outcome: Mapping[str, Any]) -> None:
    """Record the result of the action a decision caused (T2 of the intent protocol)."""
    tx.execute("UPDATE decision SET outcome = %s WHERE decision_id = %s",
               (Jsonb(thaw(outcome)), decision_id))


def record(tx: Tx, *, wf: str | None, actor: str, action: str, subject: tuple[str, str],
           summary: str, details: Mapping[str, Any], caused_by: int | None = None,
           sim_minute: int | None = None) -> int:
    """Append one audit_log row (DESIGN.md 11.2). `caused_by` is the decision id behind it."""
    r = tx.execute(
        f"""INSERT INTO audit_log (sim_minute, wf, actor, action, subject_type, subject_id,
                                   summary, details, caused_by)
            VALUES (coalesce(%s, {_CLOCK}), {_WF}, %s, %s, %s, %s, %s, %s, %s)
            RETURNING audit_id""",
        (sim_minute, wf, actor, action, subject[0], subject[1], summary,
         Jsonb(thaw(details)), caused_by)).fetchone()
    maybe_crash("audit_write", "in_t2")
    return int(r[0])


ProvenanceInfo = Mapping[str, Any]
"""source (default | arm | winner | holdout | probe | supervisor | rollback) plus optional
experiment_id, proposal_id, approval_id, decision_id."""


def provenance(tx: Tx, wf: int, version: int, config: Config, spec: WorkflowSpec,
               default_source: ProvenanceInfo,
               overrides: Mapping[tuple[str, str], ProvenanceInfo] | None = None) -> int:
    """Materialise `allocation_provenance` for EVERY cohort of the version (and 'default') and
    every agent dim, plus prompt_version under 'default' (DESIGN.md 9.4).

    A cohort with no override for a dim gets the default's rows with `inherited = true`, so "why
    did this customer hear this" is one lookup by (version, cohort, dim). `overrides` maps
    (cohort, dim) to a more specific source (an arm, a probe); everything else carries
    `default_source`. Returns the number of rows written. Idempotent (upsert)."""
    overrides = overrides or {}
    rows: list[tuple[Any, ...]] = []
    cohorts = ["default"] + [c.id for c in config.cohorts]
    for cohort in cohorts:
        for dim in AGENT_DIMS:
            inherited = cohort != "default" and not config.has_override(cohort, dim)
            src_key = ("default", dim) if inherited else (cohort, dim)
            info = overrides.get(src_key, default_source)
            options = list(spec.options.get(dim, ()))
            for name, share in sorted(config.dist(cohort, dim).items()):
                code = options.index(name) if name in options else -1
                rows.append(_prov_row(wf, version, cohort, dim, code, share, info, inherited))
    prompts = list(spec.options.get("prompt_version", ()))
    p = config.prompt_version
    rows.append(_prov_row(wf, version, "default", "prompt_version",
                          prompts.index(p) if p in prompts else -1, 1.0,
                          overrides.get(("default", "prompt_version"), default_source), False))
    for row in rows:
        tx.execute(
            """INSERT INTO allocation_provenance (wf, version, cohort_key, dim, option, share,
                   source, inherited, experiment_id, proposal_id, approval_id, decision_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (wf, version, cohort_key, dim, option) DO UPDATE SET
                   share = excluded.share, source = excluded.source,
                   inherited = excluded.inherited, experiment_id = excluded.experiment_id,
                   proposal_id = excluded.proposal_id, approval_id = excluded.approval_id,
                   decision_id = excluded.decision_id""", row)
    return len(rows)


def _prov_row(wf: int, version: int, cohort: str, dim: str, code: int, share: float,
              info: ProvenanceInfo, inherited: bool) -> tuple[Any, ...]:
    return (wf, version, cohort, dim, code, float(share), info.get("source", "default"),
            inherited, info.get("experiment_id"), info.get("proposal_id"),
            info.get("approval_id"), info.get("decision_id"))
