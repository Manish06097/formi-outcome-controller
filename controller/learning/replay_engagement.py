"""Replay recomputers for the engagement decisions (DESIGN.md 9.6): harm gate, allocator, winner
rule, harm stop and decay rollback.

Owner: integration step I4 (milestone M5). ADR 0031.

Each recomputer rebuilds the snapshot the decision saw from its `posterior_snapshot` inputs
(`learning/ws_codec.py`), refits the model it used (the refit is compared with the stored mean
and covariance by `ReplayContext.fit`), redraws with the stored seed and reruns the same
function the loop ran (`learning/engagement_plan.py`). It returns the outcome fields that are a
function of those inputs; everything else (decision ids, wall times) is left out.
"""
from __future__ import annotations

from typing import Any, Mapping

from controller.audit.replay import ReplayContext, ReplayError, register_recomputer
from controller.learning import engagement_plan as ep
from controller.learning.ws_codec import decode_snapshot, meta_of
from controller.settings import Thresholds
from controller.types import Config, WorkflowSnapshot


def _thresholds() -> Thresholds:
    return Thresholds()


def _rebuild(ctx: ReplayContext, kinds: tuple[str, ...]) -> tuple[WorkflowSnapshot, ep.Fits,
                                                                  Mapping[str, Any]]:
    """The decision's snapshot and its fits, refitted (or the stored ones with --no-refit),
    drawn with each snapshot's stored seed."""
    t = _thresholds()
    arrays = ctx.inputs(kinds[0])
    ws = decode_snapshot(arrays)
    extra = meta_of(arrays)["extra"]
    model = ep.model_named(extra.get("model", "beta_binomial"), t)
    post, draws, seeds = {}, {}, {}
    for k in kinds:
        snap = ctx.snapshot(k)
        ref = model.fit(decode_snapshot(ctx.inputs(k)), k)  # type: ignore[arg-type]
        mean, cov = ctx.fit(k, lambda _a, ref=ref: (ref.mean, ref.cov))
        if snap.seed is None:
            raise ReplayError(f"snapshot {snap.snapshot_id} has no seed")
        post[k] = ref
        draws[k] = ep.draws_from(mean, cov, ref, int(snap.seed), t)
        seeds[k] = int(snap.seed)
    return ws, ep.Fits(model=extra.get("model", "beta_binomial"), post=post, draws=draws,
                       seeds=seeds), extra


@register_recomputer("learning.allocator", "5.4/plan_allocation")
def _replay_allocation(ctx: ReplayContext) -> dict[str, Any]:
    ws, fits, extra = _rebuild(ctx, ("stationary", "ew"))
    t = _thresholds()
    inp = ctx.inputs_json
    pws = ep.frozen_view(ws) if inp.get("in_freeze") else ws
    plan = ep.run_allocation(pws, fits, t, int(inp["t_apply"]))
    out = ep.allocation_outcome(plan)
    kind = plan.kind
    if inp.get("ramp_stop") and kind != "risk_reducing":
        kind = f"{kind}_blocked_by_freeze"
    out["action"] = kind
    return {"outcome": out, "harm_report": _harm(plan.harm)}


@register_recomputer("learning.winners", "5.5/evaluate_looks")
def _replay_looks(ctx: ReplayContext) -> dict[str, Any]:
    ws, fits, _ = _rebuild(ctx, ("stationary", "ew"))
    out = ep.run_looks(ws, fits, _thresholds(), int(ctx.inputs_json["t_apply"]))
    return {"outcome": ep.looks_outcome(out)}


@register_recomputer("learning.allocator", "5.4/harm_stop")
def _replay_harm_stop(ctx: ReplayContext) -> dict[str, Any]:
    ws, fits, _ = _rebuild(ctx, ("ew",))
    reqs = ep.run_harm_stops(ws, fits.ew, _thresholds())
    return {"outcome": {"patches": ep.patches_json(reqs)}}


@register_recomputer("learning.decay", "5.7/rollback")
def _replay_rollback(ctx: ReplayContext) -> dict[str, Any]:
    ws, fits, _ = _rebuild(ctx, ("ew",))
    reqs = ep.run_rollbacks(ws, fits.ew, _thresholds(), int(ctx.inputs_json["t_apply"]))
    return {"outcome": {"patches": ep.patches_json(reqs)}}


@register_recomputer("learning.harm", "0.1/harm_gate")
def _replay_gate(ctx: ReplayContext) -> dict[str, Any]:
    t = _thresholds()
    arrays = ctx.inputs("ew")
    extra = meta_of(arrays)["extra"]
    if extra.get("model") == "none":         # no engagement lever judged: no posterior
        ws, ew = decode_snapshot(arrays), None
    else:
        ws, fits, extra = _rebuild(ctx, ("ew",))
        ew = fits.ew
    cur = Config.from_stored(extra["cur"])
    new = Config.from_stored(extra["new"])
    v = ep.run_gate(ws, ew, cur, new, t, float(extra["alpha"]), ep.seed_for(ws, "gate", "did"),
                    extra.get("t_apply"))
    return {"outcome": v.outcome(), "harm_report": _harm(v.report)}


def _harm(r: Any) -> dict[str, Any] | None:
    """A HarmReport as the decision writer stores it (its dataclass fields)."""
    if r is None:
        return None
    from controller.types import thaw
    return {k: thaw(v) for k, v in vars(r).items()}


__all__: list[str] = []
