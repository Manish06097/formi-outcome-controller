"""Snapshot codec: a `WorkflowSnapshot` as the arrays of one `posterior_snapshot.inputs` npz.

Owner: integration step I4 (milestone M5). Implements DESIGN.md 9.6 (decision replay) for the
engagement decisions: everything an engagement rule reads from a snapshot is stored with the
posterior it fitted, so `replay-decision` rebuilds the exact snapshot from the audit store
alone, refits the model, redraws with the stored seed and reruns the rule. ADR 0031.

What is stored (all inside the npz, so `inputs_hash` covers it):
- the `r_engage` rows (`eng_*`, int64), the active population (`act_cells`, `act_leads`) and the
  cell catalogue (`cell_id` and its four feature strings);
- `meta_json`: UTF-8 JSON of the non-array fields (the workflow spec as the API returned it, the
  effective policy, the live config in its stored form, experiments with their arm rows, winner
  looks, decay monitors, holdouts, our applied changes, DID states, the clock and the end minute)
  plus the rule's own scalars (`extra`).
- with `with_pickup`, the pickup rollups summed over day and attempt bucket (`pk_*`): the composed
  gate's guardrail forecast of a schedule or caller-ID change reads them per (cell, hour), and the
  sums give the same forecast at a fraction of the rows.
DID and flag rollups are not stored: no engagement rule reads them.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Mapping

import numpy as np

from controller.snapshot import build_snapshot
from controller.types import (ActivePopulation, Cell, ChangeRecord, Config, DecayMonitorState,
                              DidState, EffectivePolicy, EngageArrays, Experiment, ExperimentArm,
                              FrozenDict, HoldoutState, LookRecord, PickupArrays, WorkflowSnapshot,
                              WorkflowSpec, freeze, thaw)

ENGAGE_COLS = ("seg", "cell", "config_version", "prompt", "w", "v", "connected", "engaged",
               "flags")


# ============================================================================== dataclass JSON

def _plain(x: Any) -> Any:
    if isinstance(x, (frozenset, set)):
        return sorted(_plain(v) for v in x)
    if isinstance(x, Mapping):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


def dc_json(obj: Any) -> dict[str, Any]:
    """A frozen dataclass as plain JSON (frozensets as sorted lists, tuples as lists)."""
    return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}


def _restore(annotation: str, v: Any) -> Any:
    if v is None:
        return None
    a = str(annotation)
    if a.startswith("frozenset"):
        return frozenset(v)
    if a.startswith("tuple"):
        return tuple(_restore("tuple", x) if isinstance(x, list) else x for x in v)
    if "FrozenDict" in a or a.startswith("Mapping"):
        return freeze(v)
    return v


def dc_from_json(cls: type, d: Mapping[str, Any], **override: Any) -> Any:
    """Inverse of `dc_json` for the dataclasses stored here."""
    kw = {}
    for f in dataclasses.fields(cls):
        if f.name in override:
            kw[f.name] = override[f.name]
        elif f.name in d:
            kw[f.name] = _restore(f.type, d[f.name])
    return cls(**kw)


# ============================================================================== encode

def _pickup_sums(pk: PickupArrays) -> dict[str, np.ndarray]:
    """`r_pickup` summed per (cell, hour)."""
    if len(pk) == 0:
        return {k: np.zeros(0, dtype=np.int64) for k in ("pk_cell", "pk_hour", "pk_dials",
                                                         "pk_connected")}
    keys = np.stack([np.asarray(pk.cell), np.asarray(pk.hour)], axis=1)
    uniq, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = inv.ravel()
    d = np.bincount(inv, weights=pk.dials.astype(float), minlength=len(uniq))
    c = np.bincount(inv, weights=pk.connected.astype(float), minlength=len(uniq))
    return {"pk_cell": uniq[:, 0].astype(np.int64), "pk_hour": uniq[:, 1].astype(np.int64),
            "pk_dials": d.astype(np.int64), "pk_connected": c.astype(np.int64)}


def encode_snapshot(ws: WorkflowSnapshot, extra: Mapping[str, Any] | None = None,
                    with_pickup: bool = False) -> dict[str, np.ndarray]:
    """The npz arrays of `ws` (see the module docstring)."""
    e = ws.engage
    arrays: dict[str, np.ndarray] = {f"eng_{c}": np.asarray(getattr(e, c), dtype=np.int64)
                                     for c in ENGAGE_COLS}
    if with_pickup:
        arrays.update(_pickup_sums(ws.pickup))
    if ws.active is not None:
        arrays["act_cells"] = np.asarray(ws.active.cells, dtype=np.int64)
        arrays["act_leads"] = np.asarray(ws.active.leads, dtype=float)
    ids = sorted(int(c) for c in ws.cells)
    arrays["cell_id"] = np.asarray(ids, dtype=np.int64)
    for attr in ("lang", "tier", "age", "value"):
        arrays[f"cell_{attr}"] = np.asarray([str(getattr(ws.cells[c], attr)) for c in ids],
                                            dtype=np.str_)
    meta = {
        "spec": thaw(ws.spec.raw), "policy": dc_json(ws.policy),
        "live": ws.live.stored(), "live_version": int(ws.live_version), "now": int(ws.now),
        "end_minute": ws.end_minute, "run_id": ws.run_id,
        "experiments": [{**dc_json(x), "arm_rows": [dc_json(r) for r in x.arm_rows]}
                        for x in ws.experiments],
        "looks": [dc_json(x) for x in ws.looks], "decay": [dc_json(x) for x in ws.decay],
        "holdouts": [dc_json(x) for x in ws.holdouts],
        "changes": [dc_json(x) for x in ws.changes],
        "did_state": {k: dc_json(v) for k, v in sorted(ws.did_state.items())},
        "extra": _plain(dict(extra or {})),
    }
    blob = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()
    arrays["meta_json"] = np.frombuffer(blob, dtype=np.uint8).copy()
    return arrays


def meta_of(arrays: Mapping[str, np.ndarray]) -> dict[str, Any]:
    return json.loads(bytes(np.asarray(arrays["meta_json"], dtype=np.uint8)).decode())


# ============================================================================== decode

def decode_snapshot(arrays: Mapping[str, np.ndarray]) -> WorkflowSnapshot:
    """The snapshot `encode_snapshot` stored (DID and flag rollups empty, pickup only as the
    stored per (cell, hour) sums)."""
    meta = meta_of(arrays)
    spec = WorkflowSpec.from_api(meta["spec"])
    policy = dc_from_json(EffectivePolicy, meta["policy"])
    live = Config.from_stored(meta["live"])
    engage = EngageArrays(*[np.asarray(arrays[f"eng_{c}"], dtype=np.int64) for c in ENGAGE_COLS])
    cells = {int(c): Cell(int(c), str(lang), str(tier), str(age), str(value))
             for c, lang, tier, age, value in zip(arrays["cell_id"], arrays["cell_lang"],
                                                  arrays["cell_tier"], arrays["cell_age"],
                                                  arrays["cell_value"])}
    active = None
    if "act_cells" in arrays:
        from controller.snapshot import cohort_of_cells
        ac = np.asarray(arrays["act_cells"], dtype=np.int64)
        al = np.asarray(arrays["act_leads"], dtype=float)
        cmap = cohort_of_cells(live, cells, ac.tolist())
        active = ActivePopulation(cells=ac, leads=al, mix=al / al.sum(),
                                  cohort_of_cell=FrozenDict({int(c): k for c, k in
                                                             zip(ac, cmap)}))
    exps = tuple(dc_from_json(Experiment, x, arm_rows=tuple(
        dc_from_json(ExperimentArm, r) for r in x.get("arm_rows", ())))
        for x in meta["experiments"])
    pickup = None
    if "pk_cell" in arrays:
        n = len(arrays["pk_cell"])
        pickup = PickupArrays(np.full(n, -1, dtype=np.int64),
                              np.asarray(arrays["pk_hour"], dtype=np.int64),
                              np.asarray(arrays["pk_cell"], dtype=np.int64),
                              np.ones(n, dtype=np.int64),
                              np.asarray(arrays["pk_dials"], dtype=np.int64),
                              np.asarray(arrays["pk_connected"], dtype=np.int64))
    return build_snapshot(
        spec=spec, policy=policy, live=live, live_version=int(meta["live_version"]),
        now=int(meta["now"]), engage=engage, pickup=pickup, experiments=exps,
        did_state={k: dc_from_json(DidState, v) for k, v in meta["did_state"].items()},
        cells=cells, active=active,
        looks=[dc_from_json(LookRecord, x) for x in meta["looks"]],
        decay=[dc_from_json(DecayMonitorState, x) for x in meta["decay"]],
        holdouts=[dc_from_json(HoldoutState, x) for x in meta["holdouts"]],
        changes=[dc_from_json(ChangeRecord, x) for x in meta["changes"]],
        end_minute=meta["end_minute"], run_id=meta["run_id"])


__all__ = ["encode_snapshot", "decode_snapshot", "meta_of", "dc_json", "dc_from_json"]
