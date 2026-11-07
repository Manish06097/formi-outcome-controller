"""WorkflowSnapshot built from rollup rows (numpy), for the pure learning code.

Owner: track T3 (milestone M5). Implements DESIGN.md 11.2 (`WorkflowSnapshot`) against the
section 3.2 schema of M0, plus the segment arithmetic every learning module shares.

Two builders produce the same object:
- `build_snapshot(...)` from in-memory arrays (tests, simulations, replay), no database.
- `load_snapshot(conn, wf, ...)` from the run schema: `r_engage`, `r_pickup`, `r_did`,
  `r_flags`, `cell`, active `lead_dim` rows, `experiment` / `experiment_arm`, `winner_look`,
  `decay_monitor`, `holdout` and `config_version`. It reads; it never writes.

Every field lives on `types.WorkflowSnapshot` (the T3 and T4 requests were merged in step I1,
ADR 0028): the cell catalogue, the active-lead population, winner looks, decay monitors,
holdouts, our recent config changes and the scenario end minute.

Segments (5.3): a segment is half of a day's dialling window, `seg = 2 * sim_day + half`, with
`half = 1` from `window_start + (window_end - window_start) / 2` on. History days are negative,
so history segments are negative too. `segment_of` is the reference; ingest
(`ingest/transform.sql`, statement `calls`) computes the same rule on each dial's TICK START,
`floor(minute / tick) * tick`, because the simulator randomises the call timestamp inside the
tick. Callers of `segment_of` on raw call minutes must floor to the tick first (the loop's stops
are tick boundaries already). `tests/e2e/platform/test_ingest_paths.py` checks the two agree.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from controller.types import (DEFAULT_COHORT, ActivePopulation, Cell, ChangeRecord, Config,
                              DecayMonitorState, DidArrays, EffectivePolicy, EngageArrays,
                              Experiment, ExperimentArm, FlagSeries, FrozenDict, HoldoutState,
                              LookRecord, Minute, PickupArrays, Proposal, WorkflowSnapshot,
                              WorkflowSpec)

MIN_PER_DAY = 1440

# ============================================================================== segments


def _half_minute(window: tuple[int, int]) -> float:
    """Minute of day at which the second half-window segment starts."""
    lo, hi = window
    return lo * 60 + (hi - lo) * 30.0


def segment_of(minute: Minute, window: tuple[int, int]) -> int:
    """Segment index of a simulated minute: 2 * day + (0 before the half-window, else 1)."""
    day, mod = divmod(int(minute), MIN_PER_DAY)
    return 2 * day + (1 if mod >= _half_minute(window) else 0)


def segment_position(minute: Minute, window: tuple[int, int]) -> float:
    """Fractional segment position counted in DIALLING time (overnight adds nothing).

    segment j starts at position j; position j + 0.5 is half-way through segment j's dialling
    time. Used to extrapolate a trend to an expected apply time (5.7)."""
    lo, hi = window
    day, mod = divmod(int(minute), MIN_PER_DAY)
    seg_len = (hi - lo) * 30.0
    start, half = lo * 60.0, _half_minute(window)
    if mod < start:
        return 2.0 * day
    if mod >= hi * 60:
        return 2.0 * day + 2.0
    if mod < half:
        return 2.0 * day + (mod - start) / seg_len
    return 2.0 * day + 1.0 + (mod - half) / seg_len


def segment_start_minute(seg: int, window: tuple[int, int]) -> Minute:
    day, half = divmod(int(seg), 2)
    return day * MIN_PER_DAY + int(window[0] * 60 if half == 0 else _half_minute(window))


def segment_day(seg: np.ndarray | int) -> np.ndarray | int:
    """Calendar sim day of a segment (floor division keeps history days negative)."""
    return np.floor_divide(seg, 2)


def ew_weights(seg: np.ndarray, seg_now: int, half_life: float) -> np.ndarray:
    """Recency weights 2^(-age / half_life), age = seg_now - seg in segments (5.3). The
    current, in-progress segment has age 0; future segments (none in practice) weight 1."""
    age = np.maximum(seg_now - np.asarray(seg, dtype=float), 0.0)
    return np.power(2.0, -age / half_life)


# ============================================================================== helpers

def option_index(spec: WorkflowSpec, dim: str) -> dict[str, int]:
    """Option name -> code (codes index `spec.options[dim]`, as the rollups store them)."""
    return {o: i for i, o in enumerate(spec.options.get(dim, ()))}


def cohort_of_cells(cfg: Config, cells: Mapping[int, Cell], cell_ids: Sequence[int]) -> list[str]:
    """First-match cohort of each cell under `cfg` (the simulator's rule, DESIGN.md 5.2). A
    cell missing from the catalogue (features unknown) falls to the default cohort."""
    out = []
    for c in cell_ids:
        cell = cells.get(int(c))
        out.append(cfg.cohort_of(cell.features()) if cell is not None else DEFAULT_COHORT)
    return out


def active_population(ws: WorkflowSnapshot, recent_segments: int = 4) -> ActivePopulation:
    """The population the harm gate averages over.

    Prefers the snapshot's tracked active leads (lead_dim, state 0). Without them (a snapshot
    built from rollups only) it falls back to connected calls per cell in the last
    `recent_segments` segments, which is proportional to the active population the dialler
    reaches. `mix` is each cell's share of expected connected calls.
    """
    if ws.active is not None and len(ws.active.cells):
        return ws.active
    e = ws.engage
    window = ws.spec.dialling_window
    seg_now = segment_of(ws.now, window)
    if len(e) == 0:
        cells = np.array(sorted(ws.cells or {0}), dtype=np.int64)
        leads = np.ones(len(cells))
    else:
        recent = e.seg >= seg_now - recent_segments
        sel = recent if recent.any() else np.ones(len(e), dtype=bool)
        cells, inv = np.unique(e.cell[sel], return_inverse=True)
        leads = np.bincount(inv, weights=e.connected[sel].astype(float)).astype(float)
    cmap = cohort_of_cells(ws.live, ws.cells, cells.tolist())
    tot = float(leads.sum()) or 1.0
    return ActivePopulation(cells=np.asarray(cells, dtype=np.int64), leads=leads,
                            mix=leads / tot,
                            cohort_of_cell=FrozenDict({int(c): k for c, k in zip(cells, cmap)}))


def empty_engage() -> EngageArrays:
    z = np.zeros(0, dtype=np.int64)
    return EngageArrays(seg=z.copy(), cell=z.copy(), config_version=z.copy(), prompt=z.copy(),
                        w=z.copy(), v=z.copy(), connected=z.copy(), engaged=z.copy(),
                        flags=z.copy())


def engage_from_columns(cols: Mapping[str, Iterable[Any]]) -> EngageArrays:
    """EngageArrays from column lists or arrays (seg, cell, config_version, prompt, w, v,
    connected, engaged, flags); missing config_version / prompt / flags default to 0."""
    n = len(np.asarray(cols["seg"]))
    def col(name: str) -> np.ndarray:
        if name in cols:
            return np.asarray(cols[name], dtype=np.int64).copy()
        return np.zeros(n, dtype=np.int64)
    return EngageArrays(seg=col("seg"), cell=col("cell"), config_version=col("config_version"),
                        prompt=col("prompt"), w=col("w"), v=col("v"),
                        connected=col("connected"), engaged=col("engaged"), flags=col("flags"))


def _empty_pickup() -> PickupArrays:
    z = np.zeros(0, dtype=np.int64)
    return PickupArrays(sim_day=z.copy(), hour=z.copy(), cell=z.copy(), attempt_bucket=z.copy(),
                        dials=z.copy(), connected=z.copy())


def _empty_did() -> DidArrays:
    z = np.zeros(0, dtype=np.int64)
    return DidArrays(tick_no=z.copy(), hour=z.copy(), cohort_code=z.copy(), did=z.copy(),
                     dials=z.copy(), connected=z.copy())


def _empty_flags() -> FlagSeries:
    z = np.zeros(0, dtype=np.int64)
    return FlagSeries(sim_hour=z.copy(), prompt=z.copy(), connected=z.copy(), flags=z.copy(),
                      engaged=z.copy())


# ============================================================================== builders

def build_snapshot(*, spec: WorkflowSpec, policy: EffectivePolicy, live: Config,
                   live_version: int, now: Minute, engage: EngageArrays | None = None,
                   pickup: PickupArrays | None = None, did: DidArrays | None = None,
                   flags: FlagSeries | None = None, cohorts: Any = None,
                   experiments: Sequence[Experiment] = (), did_state: Mapping[str, Any] | None = None,
                   in_flight: Proposal | None = None, used_24h: Sequence[Minute] = (),
                   cells: Mapping[int, Cell] | None = None, active: ActivePopulation | None = None,
                   looks: Sequence[LookRecord] = (), decay: Sequence[DecayMonitorState] = (),
                   holdouts: Sequence[HoldoutState] = (), changes: Sequence[ChangeRecord] = (),
                   end_minute: Minute | None = None, run_id: str = "",
                   snapshot_id: int = 0) -> WorkflowSnapshot:
    """A snapshot from in-memory arrays. `cohorts` is the T4 CohortSet when available; the
    learning code resolves membership from `live` and `cells` and does not require it."""
    return WorkflowSnapshot(
        spec=spec, policy=policy, live=live, live_version=live_version, cohorts=cohorts,
        engage=engage if engage is not None else empty_engage(),
        pickup=pickup if pickup is not None else _empty_pickup(),
        did=did if did is not None else _empty_did(),
        flags=flags if flags is not None else _empty_flags(),
        experiments=tuple(experiments), did_state=FrozenDict(dict(did_state or {})),
        in_flight=in_flight, used_24h=tuple(used_24h), now=now,
        cells=FrozenDict({int(k): v for k, v in (cells or {}).items()}), active=active,
        looks=tuple(looks), decay=tuple(decay), holdouts=tuple(holdouts),
        changes=tuple(changes), end_minute=end_minute, run_id=run_id, snapshot_id=snapshot_id)


def _arrays(rows: list[tuple[Any, ...]], n_cols: int) -> list[np.ndarray]:
    if not rows:
        return [np.zeros(0, dtype=np.int64) for _ in range(n_cols)]
    a = np.asarray(rows, dtype=np.int64)
    return [a[:, i].copy() for i in range(n_cols)]


class RawRollups:
    """One workflow's rollup arrays and active-lead counts exactly as the queries return them."""

    __slots__ = ("engage", "pickup", "did", "flags", "active_rows")

    def __init__(self, engage: list[np.ndarray], pickup: list[np.ndarray],
                 did: list[np.ndarray], flags: list[np.ndarray],
                 active_rows: list[tuple[Any, ...]]):
        self.engage, self.pickup, self.did, self.flags = engage, pickup, did, flags
        self.active_rows = active_rows


def _read_rollups(conn: Any, wf: int) -> RawRollups:
    q = conn.execute
    engage = _arrays(q(
        """SELECT seg, cell, config_version, prompt, w, v, connected, engaged, flags
           FROM r_engage WHERE wf = %s ORDER BY seg, cell, config_version, prompt, w, v""",
        (wf,)).fetchall(), 9)
    pickup = _arrays(q(
        """SELECT sim_day, hour, cell, attempt_bucket, dials, connected FROM r_pickup
           WHERE wf = %s ORDER BY sim_day, hour, cell, attempt_bucket""", (wf,)).fetchall(), 6)
    did = _arrays(q(
        """SELECT tick_no, hour, cohort_code, did, dials, connected FROM r_did WHERE wf = %s
           ORDER BY tick_no, hour, cohort_code, did""", (wf,)).fetchall(), 6)
    flags = _arrays(q(
        """SELECT sim_hour, prompt, connected, flags, engaged FROM r_flags WHERE wf = %s
           ORDER BY sim_hour, prompt""", (wf,)).fetchall(), 5)
    act = q("""SELECT cell, count(*) FROM lead_dim WHERE wf = %s AND state = 0
               AND cell IS NOT NULL GROUP BY cell ORDER BY cell""", (wf,)).fetchall()
    return RawRollups(engage, pickup, did, flags, [tuple(r) for r in act])


class RollupCache:
    """A memo of `_read_rollups`, keyed by what the store says it has ingested.

    r_engage, r_pickup, r_did, r_flags and lead_dim are written only by the ingest transform,
    in the same transaction as the chunk's `ingested_chunk` row (ingest/transform.sql). So the
    rows of a workflow are a pure function of (run schema, chunks committed): the key is read from
    the store on every call (one tiny query), and a changed key (new chunk, another schema, a
    restart that starts with an empty memo) means a re-read. Nothing here can feed a decision a
    value the store would not return right now; it only skips repeating the same SELECT several
    times per step (detect, decide, verify and each hook take their own snapshot)."""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._rows: dict[int, RawRollups] = {}

    @staticmethod
    def store_key(conn: Any) -> tuple[Any, ...]:
        return tuple(conn.execute(
            "SELECT current_schema(), count(*), coalesce(max(chunk_no), -1), "
            "coalesce(max(until_minute), -1), coalesce(sum(events), 0) FROM ingested_chunk"
        ).fetchone())

    def get(self, conn: Any, wf: int) -> RawRollups:
        key = self.store_key(conn)
        if key != self._key:
            self._key, self._rows = key, {}
        got = self._rows.get(wf)
        if got is None:
            got = self._rows[wf] = _read_rollups(conn, wf)
        return got


def load_snapshot(conn: Any, wf: int, *, spec: WorkflowSpec, policy: EffectivePolicy,
                  live: Config, live_version: int, now: Minute, cohorts: Any = None,
                  did_state: Mapping[str, Any] | None = None, in_flight: Proposal | None = None,
                  used_24h: Sequence[Minute] = (), end_minute: Minute | None = None,
                  run_id: str = "", snapshot_id: int = 0,
                  rollups: "RollupCache | None" = None) -> WorkflowSnapshot:
    """Read one workflow's rollups and learned state from the run schema (search_path set).

    `wf` is the dense workflow code of `workflow.wf`. Spec, policy and the live config come from
    the world registry and config mirror (tracks T1 and T2), which own those reads.
    """
    q = conn.execute
    raw = rollups.get(conn, wf) if rollups is not None else _read_rollups(conn, wf)
    engage = EngageArrays(*[a.copy() for a in raw.engage])
    pickup = PickupArrays(*[a.copy() for a in raw.pickup])
    did = DidArrays(*[a.copy() for a in raw.did])
    flags = FlagSeries(*[a.copy() for a in raw.flags])
    cells = {int(r[0]): Cell(int(r[0]), r[1], r[2], r[3], r[4]) for r in q(
        "SELECT cell_id, lang, tier, age, value FROM cell ORDER BY cell_id").fetchall()}
    act = raw.active_rows
    active = None
    if act:
        ac = np.asarray([r[0] for r in act], dtype=np.int64)
        al = np.asarray([r[1] for r in act], dtype=float)
        cmap = cohort_of_cells(live, cells, ac.tolist())
        active = ActivePopulation(cells=ac, leads=al, mix=al / al.sum(),
                                  cohort_of_cell=FrozenDict({int(c): k for c, k in
                                                             zip(ac, cmap)}))
    experiments = _load_experiments(conn, wf, spec)
    names = {e.exp_id: e for e in experiments}
    looks = []
    for r in q("""SELECT l.exp_id, l.arm, l.scope, l.look_no, l.n_at_look, l.sim_minute,
                         l.w1, l.w2, l.w3, l.passed
                  FROM winner_look l JOIN experiment e USING (exp_id) WHERE e.wf = %s
                  ORDER BY l.exp_id, l.arm, l.scope, l.look_no""", (wf,)).fetchall():
        exp = names.get(r[0])
        arm = _opt_name(spec, exp.dim, r[1]) if exp else str(r[1])
        looks.append(LookRecord(exp_id=r[0], arm=arm, scope=r[2], look_no=int(r[3]),
                                n_at_look=int(r[4]), sim_minute=int(r[5]), w1=r[6], w2=r[7],
                                w3=r[8], passed=bool(r[9])))
    decay = [DecayMonitorState(wf=spec.workflow_id, scope=r[0], dim=r[1],
                               arm=_opt_name(spec, r[1], r[2]), cusum=float(r[3]),
                               seg_last=int(r[4]) if r[4] is not None else -10 ** 6,
                               theta_ref=float(r[5]) if r[5] is not None else 0.0,
                               alarm=bool(r[6]),
                               seg_first=int(r[7]) if r[7] is not None else None)
             for r in q("""SELECT scope, dim, arm, cusum, seg_last, theta_ref, alarm, seg_first
                           FROM decay_monitor WHERE wf = %s ORDER BY scope, dim, arm""",
                        (wf,)).fetchall()]
    holdouts = [HoldoutState(wf=spec.workflow_id, scope=r[0], dim=r[1],
                             winner_arm=_opt_name(spec, r[1], r[2]) if r[2] is not None else "",
                             control=_opt_name(spec, r[1], r[3]), share=float(r[4]),
                             since_version=r[5], state=r[6])
                for r in q("""SELECT h.scope, h.dim, w.arm, h.control, h.share, h.since_version,
                                     h.state
                              FROM holdout h LEFT JOIN winner w USING (winner_id)
                              WHERE h.wf = %s ORDER BY h.scope, h.dim""", (wf,)).fetchall()]
    changes = [ChangeRecord(minute=int(r[0]), version=int(r[1]), dims=tuple(r[2] or ()),
                            actor=r[3])
               for r in q("""SELECT applied_minute, version, changed_dims, actor
                             FROM config_version WHERE wf = %s AND applied_minute IS NOT NULL
                               AND actor LIKE 'controller%%' ORDER BY version""",
                          (wf,)).fetchall()]
    return build_snapshot(spec=spec, policy=policy, live=live, live_version=live_version,
                          now=now, engage=engage, pickup=pickup, did=did, flags=flags,
                          cohorts=cohorts, experiments=experiments, did_state=did_state,
                          in_flight=in_flight, used_24h=used_24h, cells=cells, active=active,
                          looks=looks, decay=decay, holdouts=holdouts, changes=changes,
                          end_minute=end_minute, run_id=run_id, snapshot_id=snapshot_id)


def _opt_name(spec: WorkflowSpec, dim: str, code: Any) -> str:
    opts = spec.options.get(dim, ())
    try:
        return str(opts[int(code)])
    except (IndexError, TypeError, ValueError):
        return str(code)


def _load_experiments(conn: Any, wf: int, spec: WorkflowSpec) -> tuple[Experiment, ...]:
    rows = conn.execute(
        """SELECT exp_id, dim, arms, control, cohort_set_version, state, started_version,
                  started_minute, interrupted_by
           FROM experiment WHERE wf = %s ORDER BY exp_id""", (wf,)).fetchall()
    out = []
    for r in rows:
        dim = r[1]
        arm_rows = tuple(
            ExperimentArm(exp_id=a[0], arm=_opt_name(spec, dim, a[1]), scope=a[2], state=a[3],
                          share=float(a[4]) if a[4] is not None else 0.0,
                          first_dial_minute=a[5], removed_minute=a[6], decision_id=a[7])
            for a in conn.execute(
                """SELECT exp_id, arm, scope, state, share, first_dial_minute, removed_minute,
                          decision_id FROM experiment_arm WHERE exp_id = %s
                   ORDER BY arm, scope""", (r[0],)).fetchall())
        out.append(Experiment(exp_id=r[0], wf=spec.workflow_id, dim=dim,
                              arms=tuple(_opt_name(spec, dim, a) for a in (r[2] or ())),
                              control=_opt_name(spec, dim, r[3]), cohort_set_version=r[4] or 0,
                              state=r[5], started_version=r[6] or 0, started_minute=r[7] or 0,
                              interrupted_by=r[8], arm_rows=arm_rows))
    return tuple(out)
