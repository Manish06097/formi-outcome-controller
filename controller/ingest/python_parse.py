"""Fallback parser: NDJSON lines -> typed `ev` rows with `json.loads` and `COPY ... write_row`.

Owner: track T1 (milestone M1). Implements DESIGN.md 3.1 ("Fallback", selected by
`CTL_INGEST=python`).

The two ingest paths differ only in how a chunk becomes the typed temp table `ev`: the SQL path
COPYs raw lines into `stage_raw` and parses them with `jsonb_to_record` (transform.sql step
`parse`); this path parses in Python inside the chunk transaction and COPYs typed rows. Every
later step (facts, lead state, rollups, the exactly-once row) is the same SQL, so the paths give
identical rollups by construction and `tests/e2e/test_ingest_paths.py` checks it on a fixture.

`EV_COLUMNS` must match `create_ev` in transform.sql column for column.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Iterable, Iterator

from psycopg.types.json import Jsonb

from controller.types import Tx

EV_COLUMNS = (
    "event_id", "type", "minute", "workflow_id", "campaign_id", "lead_id", "person_id",
    "attempt_no", "day_index", "config_version", "cohort_id", "w_name", "v_name", "did_name",
    "prompt_name", "connected", "engaged", "disposition", "duration_s", "turns", "flag",
    "adherence", "lang", "tier", "age", "value", "leads", "cv_at_creation", "version", "actor",
    "changed_dims", "proposal_id", "approval_id", "status", "reasons", "body")

KNOWN_TYPES = frozenset({"campaign_created", "lead", "call", "config_changed",
                         "approval_decided"})


def iter_lines(pieces: Iterable[bytes]) -> Iterator[bytes]:
    """Split a byte stream into lines without holding more than one piece plus a partial line."""
    rest = b""
    for piece in pieces:
        if not piece:
            continue
        data = rest + piece
        parts = data.split(b"\n")
        rest = parts.pop()
        for p in parts:
            if p:
                yield p
    if rest:
        yield rest


def _minute(ts: Any, epoch0: int) -> int | None:
    if not isinstance(ts, str):
        return None
    secs = dt.datetime.fromisoformat(ts).timestamp() - epoch0
    return int(secs // 60)


def _bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _float(v: Any) -> float | None:
    return None if v is None else float(v)


def to_row(e: dict[str, Any], epoch0: int) -> tuple[Any, ...]:
    """One event as an `ev` row, with the same NULL rules as the SQL path's jsonb_to_record."""
    variant = e.get("variant") if isinstance(e.get("variant"), dict) else {}
    qa = e.get("qa") if isinstance(e.get("qa"), dict) else {}
    feats = e.get("features") if isinstance(e.get("features"), dict) else {}
    dims = e.get("changed_dims")
    reasons = e.get("reasons")
    typ = e.get("type")
    return (
        e.get("event_id"), typ, _minute(e.get("ts"), epoch0), e.get("workflow_id"),
        e.get("campaign_id"), e.get("lead_id"), e.get("person_id"), e.get("attempt_no"),
        e.get("campaign_day_index"), e.get("config_version"), e.get("cohort_id"),
        variant.get("welcome_message"), variant.get("voice"), variant.get("caller_id"),
        variant.get("prompt_version"), _bool(e.get("connected")), _bool(e.get("engaged")),
        e.get("disposition"), e.get("duration_s"), e.get("turns"),
        _bool(qa.get("hallucination_flag")), _float(qa.get("flow_adherence")),
        feats.get("lang"), feats.get("city_tier"), feats.get("age_band"), feats.get("value_band"),
        e.get("leads"), e.get("config_version_at_creation"), e.get("version"), e.get("actor"),
        [str(d) for d in dims] if isinstance(dims, list) else None,
        e.get("proposal_id"), e.get("approval_id"), e.get("status"),
        Jsonb(reasons) if reasons is not None else None,
        Jsonb(e) if typ not in KNOWN_TYPES else None)


def parse_into_ev(tx: Tx, pieces: Iterable[bytes], epoch0: int) -> int:
    """COPY every line of a chunk into `ev` (already created). Returns the bytes read."""
    total = 0

    def counted() -> Iterator[bytes]:
        nonlocal total
        for p in pieces:
            total += len(p)
            yield p

    cols = ", ".join(EV_COLUMNS)
    with tx.cursor().copy(f"COPY ev ({cols}) FROM STDIN") as cp:
        for line in iter_lines(counted()):
            cp.write_row(to_row(json.loads(line), epoch0))
    return total
