"""Decision replay: recompute a stored decision from the audit store alone.

Owner: track T5 (milestone M7). Implements DESIGN.md 9.6 and ADR 0026.

Every `decision` row stores `snapshot_ids`, `seed`, `settings_hash` and `code_version`; every
`posterior_snapshot` stores its `inputs` (the aggregated count arrays, segment weights and prior)
next to the fitted mean and covariance. `replay_decision` loads them, hands them to the
recomputer registered for the decision's (module, rule_id), and compares what it returns with
what was stored (equal after rounding to `Thresholds.rounding`, 1e-9), listing every difference.

The recomputation itself is pluggable, because the gates, rules and bisections belong to other
tracks (learning, incidents, governance). A module that writes decisions registers how to redo
them, next to the code that made them:

    from controller.audit.replay import ReplayContext, register_recomputer

    @register_recomputer("learning.harm", "harm_gate")
    def _replay_harm_gate(ctx: ReplayContext) -> dict:
        mean, cov = ctx.fit("ew", refit=my_model_fit_from_inputs)   # refit, or reuse stored
        draws = seeded_draws(mean, cov, ctx.seed, n=ctx.inputs("ew")["n_draws"])
        return {"outcome": {...recomputed fields...}, "harm_report": {...}}

Only the top-level keys a recomputer returns ("outcome", "harm_report") are compared, and inside
them only the keys it returns: a recomputer may leave out fields that are not a function of the
stored inputs (wall times, ids). Anything it cannot reproduce is a diff, never a silent pass.

Recomputers are found by importing every module of `RECOMPUTER_PACKAGES` once (each module's
`register_recomputer` calls run at import). A decision without a recomputer replays as
"no recomputer registered", with `equal = False`.

Snapshot encoding (the writers must use these, so replay can read what they wrote):
`encode_inputs(dict of arrays) -> bytes` (npz, no pickle), `encode_array(ndarray) -> bytes`
(.npy) for `mean` and `cov`, and `hash_inputs(dict of arrays) -> str` for `inputs_hash`. The
hash is over names, dtypes, shapes and raw bytes, not over the npz file (zip headers carry
timestamps), so it is stable across re-encodings.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import math
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

import numpy as np

from controller.types import FrozenDict, ReplayReport, freeze, thaw

RECOMPUTER_PACKAGES = ("controller.learning", "controller.incidents", "controller.governance",
                       "controller.planner")
"""Packages whose modules may register recomputers (imported once, on the first replay)."""

COMPARED_KEYS = ("outcome", "harm_report")
"""Stored decision columns a recomputer may reproduce."""

Recomputer = Callable[["ReplayContext"], Mapping[str, Any]]
_REGISTRY: dict[tuple[str, str], Recomputer] = {}
_DISCOVERY: dict[str, str] = {}
"""module name -> import error, for the report when no recomputer is found."""
_discovered = False


class ReplayError(RuntimeError):
    """The decision or one of its snapshots cannot be loaded."""


# =============================================================================== snapshot codec

def encode_array(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, np.asarray(a), allow_pickle=False)
    return buf.getvalue()


def decode_array(b: bytes | memoryview | None) -> np.ndarray | None:
    if b is None:
        return None
    return np.load(io.BytesIO(bytes(b)), allow_pickle=False)


def encode_inputs(arrays: Mapping[str, Any]) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **{k: np.asarray(arrays[k]) for k in sorted(arrays)})
    return buf.getvalue()


def decode_inputs(b: bytes | memoryview | None) -> dict[str, np.ndarray] | None:
    if b is None:
        return None
    with np.load(io.BytesIO(bytes(b)), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def hash_inputs(arrays: Mapping[str, Any]) -> str:
    h = hashlib.sha256()
    for k in sorted(arrays):
        a = np.ascontiguousarray(np.asarray(arrays[k]))
        h.update(f"{k}|{a.dtype.str}|{a.shape}|".encode())
        h.update(a.tobytes())
    return h.hexdigest()


# =============================================================================== registry

def register_recomputer(module: str, rule_id: str = "*") -> Callable[[Recomputer], Recomputer]:
    """Register `fn` to replay decisions of `module` with `rule_id` ('*' = any rule)."""

    def deco(fn: Recomputer) -> Recomputer:
        _REGISTRY[(module, rule_id)] = fn
        return fn

    return deco


def unregister_recomputer(module: str, rule_id: str = "*") -> None:
    _REGISTRY.pop((module, rule_id), None)


def discover(packages: Iterable[str] = RECOMPUTER_PACKAGES) -> dict[str, str]:
    """Import every module of `packages` once so their registrations run. Returns import
    errors by module (a broken module must not stop other decisions from replaying)."""
    global _discovered
    if _discovered:
        return dict(_DISCOVERY)
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception as e:  # pragma: no cover - a missing package is reported, not fatal
            _DISCOVERY[pkg_name] = repr(e)
            continue
        for info in pkgutil.iter_modules(getattr(pkg, "__path__", []), prefix=pkg_name + "."):
            try:
                importlib.import_module(info.name)
            except Exception as e:  # pragma: no cover
                _DISCOVERY[info.name] = repr(e)
    _discovered = True
    return dict(_DISCOVERY)


def find_recomputer(module: str, rule_id: str) -> Recomputer | None:
    return _REGISTRY.get((module, rule_id)) or _REGISTRY.get((module, "*"))


# =============================================================================== context

@dataclass(frozen=True, eq=False)
class SnapshotRecord:
    """One `posterior_snapshot` row, decoded."""

    snapshot_id: int
    wf: int
    kind: str
    inputs: dict[str, np.ndarray] | None
    inputs_hash: str | None
    mean: np.ndarray | None
    cov: np.ndarray | None
    summary: Mapping[str, Any]
    seed: int | None
    code_version: str | None
    settings_hash: str | None

    @property
    def inputs_hash_ok(self) -> bool | None:
        if self.inputs is None or self.inputs_hash is None:
            return None
        return hash_inputs(self.inputs) == self.inputs_hash


@dataclass(eq=False)
class ReplayContext:
    """What a recomputer gets: the stored decision and its decoded snapshots."""

    decision_id: int
    module: str
    kind: str
    rule_id: str
    sim_minute: int
    wf: int | None
    inputs_json: Mapping[str, Any]
    """`decision.inputs` (the rule's scalar inputs, as stored)."""
    seed: int
    snapshots: tuple[SnapshotRecord, ...]
    refit: bool
    rounding: float = 1e-9
    notes: list[str] = field(default_factory=list)
    diffs: list[str] = field(default_factory=list)

    def snapshot(self, kind: str | None = None) -> SnapshotRecord:
        """The decision's snapshot of `kind` ('stationary' | 'ew'); the only one if None."""
        cands = [s for s in self.snapshots if kind is None or s.kind == kind]
        if len(cands) != 1:
            raise ReplayError(f"decision {self.decision_id}: expected one {kind or ''} snapshot, "
                              f"found {len(cands)}")
        return cands[0]

    def inputs(self, kind: str | None = None) -> dict[str, np.ndarray]:
        s = self.snapshot(kind)
        if s.inputs is None:
            raise ReplayError(f"snapshot {s.snapshot_id} has no stored inputs")
        return s.inputs

    def rng(self, salt: int = 0) -> np.random.Generator:
        """The generator the decision drew with (seeded from the stored seed)."""
        return np.random.default_rng([self.seed % (2 ** 63), salt]) if salt else \
            np.random.default_rng(self.seed % (2 ** 63))

    def fit(self, kind: str | None,
            refit: Callable[[dict[str, np.ndarray]], tuple[np.ndarray, np.ndarray | None]] | None
            ) -> tuple[np.ndarray, np.ndarray | None]:
        """Mean and covariance to recompute from: refitted from the stored inputs when
        `self.refit` and a refit function is given (and compared with the stored fit, so a
        non-deterministic fit shows up as a diff), else the stored ones (`--no-refit`)."""
        s = self.snapshot(kind)
        if self.refit and refit is not None:
            mean, cov = refit(self.inputs(kind))
            if s.mean is not None and not _arrays_close(mean, s.mean, self.rounding):
                self.diffs.append(f"snapshot {s.snapshot_id} ({s.kind}): refitted mean differs "
                                  f"from the stored mean (max abs diff "
                                  f"{float(np.max(np.abs(np.asarray(mean) - s.mean))):.3g})")
            if s.cov is not None and cov is not None and not _arrays_close(cov, s.cov,
                                                                           self.rounding):
                self.diffs.append(f"snapshot {s.snapshot_id} ({s.kind}): refitted covariance "
                                  "differs from the stored covariance")
            return np.asarray(mean), None if cov is None else np.asarray(cov)
        if s.mean is None:
            raise ReplayError(f"snapshot {s.snapshot_id} has no stored mean")
        if self.refit and refit is None:
            self.notes.append("no refit function given: reused the stored posterior")
        return s.mean, s.cov


# =============================================================================== comparison

def _arrays_close(a: Any, b: Any, tol: float) -> bool:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return a.shape == b.shape and bool(np.allclose(a, b, rtol=tol, atol=tol, equal_nan=True))


def _num(x: Any) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, bool)


def compare(stored: Any, recomputed: Any, path: str = "", tol: float = 1e-9) -> list[str]:
    """Differences between a stored value and a recomputed one, recursing only into keys the
    recomputed side has. Numbers are equal within `tol` (absolute and relative)."""
    if isinstance(recomputed, Mapping):
        if not isinstance(stored, Mapping):
            return [f"{path or '.'}: stored {_short(stored)}, recomputed a mapping"]
        out: list[str] = []
        for k in recomputed:
            sub = f"{path}.{k}" if path else str(k)
            if k not in stored:
                out.append(f"{sub}: not in the stored decision, recomputed "
                           f"{_short(recomputed[k])}")
            else:
                out.extend(compare(stored[k], recomputed[k], sub, tol))
        return out
    if isinstance(recomputed, (list, tuple, np.ndarray)) and not isinstance(recomputed, str):
        rec = list(np.asarray(recomputed).tolist()) if isinstance(recomputed, np.ndarray) \
            else list(recomputed)
        if not isinstance(stored, (list, tuple)):
            return [f"{path}: stored {_short(stored)}, recomputed {_short(rec)}"]
        if len(stored) != len(rec):
            return [f"{path}: stored {len(stored)} items, recomputed {len(rec)}"]
        out = []
        for i, (s, r) in enumerate(zip(stored, rec)):
            out.extend(compare(s, r, f"{path}[{i}]", tol))
        return out
    if _num(recomputed) and _num(stored):
        s, r = float(stored), float(recomputed)
        if math.isnan(s) and math.isnan(r):
            return []
        if math.isclose(s, r, rel_tol=tol, abs_tol=tol):
            return []
        return [f"{path}: stored {s!r}, recomputed {r!r}"]
    if isinstance(recomputed, np.generic):
        recomputed = recomputed.item()
    if stored != recomputed:
        return [f"{path}: stored {_short(stored)}, recomputed {_short(recomputed)}"]
    return []


def _short(x: Any, n: int = 80) -> str:
    s = repr(x)
    return s if len(s) <= n else s[: n - 3] + "..."


def _plain(x: Any) -> Any:
    """JSON-like plain data (numpy scalars and arrays become Python values)."""
    if isinstance(x, Mapping):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


# =============================================================================== replay

_DECISION_SQL = """SELECT decision_id, sim_minute, wf, module, kind, rule_id, inputs, outcome,
                          harm_report, snapshot_ids, seed, settings_hash, code_version
                   FROM decision WHERE decision_id = %s"""
_SNAPSHOT_SQL = """SELECT snapshot_id, wf, kind, inputs, inputs_hash, mean, cov, summary, seed,
                          code_version, settings_hash
                   FROM posterior_snapshot WHERE snapshot_id = ANY(%s) ORDER BY snapshot_id"""


def load_context(conn: Any, decision_id: int, refit: bool = True,
                 rounding: float = 1e-9) -> tuple[ReplayContext, dict[str, Any]]:
    """The replay context and the stored decision row."""
    cur = conn.execute(_DECISION_SQL, (decision_id,))
    row = cur.fetchone()
    if row is None:
        raise ReplayError(f"no decision {decision_id}")
    d = dict(zip([c.name for c in cur.description], row))
    snaps: list[SnapshotRecord] = []
    ids = list(d["snapshot_ids"] or [])
    if ids:
        cur = conn.execute(_SNAPSHOT_SQL, (ids,))
        cols = [c.name for c in cur.description]
        found = {}
        for r in cur.fetchall():
            s = dict(zip(cols, r))
            found[s["snapshot_id"]] = SnapshotRecord(
                snapshot_id=s["snapshot_id"], wf=s["wf"], kind=s["kind"],
                inputs=decode_inputs(s["inputs"]), inputs_hash=s["inputs_hash"],
                mean=decode_array(s["mean"]), cov=decode_array(s["cov"]),
                summary=s["summary"] or {}, seed=s["seed"], code_version=s["code_version"],
                settings_hash=s["settings_hash"])
        missing = [i for i in ids if i not in found]
        if missing:
            raise ReplayError(f"decision {decision_id}: snapshots {missing} are missing")
        snaps = [found[i] for i in ids]
    ctx = ReplayContext(decision_id=d["decision_id"], module=d["module"], kind=d["kind"],
                        rule_id=d["rule_id"], sim_minute=d["sim_minute"], wf=d["wf"],
                        inputs_json=d["inputs"] or {}, seed=int(d["seed"] or 0),
                        snapshots=tuple(snaps), refit=refit, rounding=rounding)
    return ctx, d


def replay_decision(decision_id: int, refit: bool = True, *, conn: Any = None,
                    settings: Any = None) -> ReplayReport:
    """Recompute decision `decision_id` from the audit store and compare (DESIGN.md 9.6).

    `conn` is a connection on the run schema; without it the CLI's connection rules apply
    (`controller.audit.__main__.open_connection`). `settings` defaults to the running
    settings, whose hash and code version are compared with the stored ones.
    """
    if conn is None:
        from controller.audit.__main__ import open_connection
        with open_connection() as c:
            return replay_decision(decision_id, refit, conn=c, settings=settings)
    if settings is None:
        from controller.settings import load_settings
        settings = load_settings()
    rounding = float(settings.t.rounding)
    ctx, d = load_context(conn, decision_id, refit=refit, rounding=rounding)
    stored = {k: d[k] for k in COMPARED_KEYS}
    stored_view = {"module": d["module"], "kind": d["kind"], "rule_id": d["rule_id"],
                   "sim_minute": d["sim_minute"], "seed": d["seed"],
                   "settings_hash": d["settings_hash"], "code_version": d["code_version"],
                   "snapshot_ids": list(d["snapshot_ids"] or []), **stored}
    diffs: list[str] = []
    for s in ctx.snapshots:
        if s.inputs_hash_ok is False:
            diffs.append(f"snapshot {s.snapshot_id}: stored inputs do not match inputs_hash "
                         "(the audit store was altered or written with another codec)")
    discover()
    fn = find_recomputer(d["module"], d["rule_id"])
    recomputed: dict[str, Any] = {}
    if fn is None:
        diffs.append(f"no recomputer registered for {d['module']}/{d['rule_id']}")
        errs = {m: e for m, e in _DISCOVERY.items()}
        if errs:
            diffs.append("modules that failed to import: " + ", ".join(sorted(errs)))
    else:
        try:
            got = fn(ctx)
        except Exception as e:
            diffs.append(f"recomputer {d['module']}/{d['rule_id']} raised {e!r}")
            got = {}
        unknown = [k for k in got if k not in COMPARED_KEYS]
        if unknown:
            diffs.append(f"recomputer returned keys that are not stored columns: {unknown}")
        recomputed = _plain({k: v for k, v in got.items() if k in COMPARED_KEYS})
        diffs.extend(ctx.diffs)
        for k, v in recomputed.items():
            diffs.extend(compare(stored[k], v, k, rounding))
    cur_hash = settings.settings_hash()
    cur_code = settings.env.code_version
    if ctx.notes:
        recomputed["_notes"] = list(ctx.notes)
    return ReplayReport(decision_id=decision_id, stored=freeze(_plain(stored_view)),
                        recomputed=freeze(recomputed), equal=not diffs and fn is not None,
                        diffs=tuple(diffs), settings_hash_match=(d["settings_hash"] == cur_hash),
                        code_version_match=(d["code_version"] == cur_code))


def sample_decisions(conn: Any, n: int = 20, seed: int = 0,
                     replayable_only: bool = True) -> list[int]:
    """`n` decision ids chosen deterministically (by a seeded hash of the id), restricted to
    decisions that have a registered recomputer when `replayable_only`."""
    discover()
    rows = conn.execute(
        "SELECT decision_id, module, rule_id FROM decision "
        "ORDER BY md5(decision_id::text || ':' || %s::text), decision_id", (seed,)).fetchall()
    out = []
    for did, module, rule_id in rows:
        if replayable_only and find_recomputer(module, rule_id) is None:
            continue
        out.append(int(did))
        if len(out) >= n:
            break
    return out


def render_report(rep: ReplayReport) -> str:
    """The side-by-side text the CLI prints (mismatched settings or code first)."""
    s = thaw(rep.stored) if isinstance(rep.stored, FrozenDict) else dict(rep.stored)
    out: list[str] = []
    if not rep.settings_hash_match:
        out.append(f"WARNING: decision {rep.decision_id} was made with settings "
                   f"{s.get('settings_hash')}, the running settings differ.")
    if not rep.code_version_match:
        out.append(f"WARNING: decision {rep.decision_id} was made by code "
                   f"{s.get('code_version')}, the running code differs.")
    out.append(f"Decision {rep.decision_id}: {s.get('module')} / {s.get('kind')} (rule "
               f"{s.get('rule_id')}), seed {s.get('seed')}, snapshots {s.get('snapshot_ids')}")
    rec = thaw(rep.recomputed)
    for k in COMPARED_KEYS:
        if k in rec:
            out.append(f"  {k}:")
            out.extend(_side_by_side(s.get(k), rec[k], "    "))
    for note in rec.get("_notes", []):
        out.append(f"  note: {note}")
    out.append("  RESULT: " + ("equal (after rounding to 1e-9)" if rep.equal else "DIFFERENT"))
    out.extend(f"    - {d}" for d in rep.diffs)
    return "\n".join(out)


def _side_by_side(stored: Any, rec: Any, indent: str, path: str = "") -> list[str]:
    if isinstance(rec, Mapping):
        lines = []
        for k, v in rec.items():
            sv = stored.get(k) if isinstance(stored, Mapping) else None
            lines.extend(_side_by_side(sv, v, indent, f"{path}.{k}" if path else str(k)))
        return lines
    return [f"{indent}{path:<32} stored {_short(stored, 40):<42} recomputed {_short(rec, 40)}"]
