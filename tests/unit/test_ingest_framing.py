"""Unit tests of the pure pieces of ingest and crash injection: line framing of the COPY stream,
line splitting of the Python parser, the transform file and CTL_FAULT parsing."""
from __future__ import annotations

import json
import random

import pytest

from controller.ingest.loader import prefix_lines
from controller.ingest.pipeline import load_transform
from controller.ingest.python_parse import EV_COLUMNS, iter_lines, to_row
from controller.reliability.faults import FaultInjector, FaultSpec, parse_faults

DATA = b"".join(json.dumps({"event_id": i, "type": "x" * (i % 7)}).encode() + b"\n"
                for i in range(1, 200))


def pieces(data: bytes, rng: random.Random) -> list[bytes]:
    out, i = [], 0
    while i < len(data):
        n = rng.randint(1, 50)
        out.append(data[i:i + n])
        i += n
    return out


@pytest.mark.parametrize("seed", range(20))
def test_prefix_lines_one_prefix_per_line_for_any_split(seed):
    out = b"".join(prefix_lines(pieces(DATA, random.Random(seed)), b"7\x02"))
    expect = b"".join(b"7\x02" + line + b"\n" for line in DATA.splitlines())
    assert out == expect


def test_prefix_lines_without_trailing_newline():
    assert b"".join(prefix_lines([b"a\nb"], b"P")) == b"Pa\nPb"
    assert b"".join(prefix_lines([b"a\n", b"", b"b\n"], b"P")) == b"Pa\nPb\n"


@pytest.mark.parametrize("seed", range(10))
def test_iter_lines_reassembles_any_split(seed):
    assert list(iter_lines(pieces(DATA, random.Random(seed)))) == DATA.splitlines()


def test_to_row_matches_ev_columns_and_null_rules():
    call = {"type": "call", "event_id": 9, "ts": "2027-01-04T10:07:00+05:30",
            "workflow_id": "w", "variant": {"welcome_message": "W1"}, "qa": None,
            "connected": False, "changed_dims": None}
    row = to_row(call, epoch0=int(__import__("datetime").datetime.fromisoformat(
        "2027-01-04T00:00:00+05:30").timestamp()))
    assert len(row) == len(EV_COLUMNS)
    d = dict(zip(EV_COLUMNS, row))
    assert d["minute"] == 607 and d["w_name"] == "W1" and d["flag"] is None
    assert d["body"] is None and d["changed_dims"] is None
    other = dict(zip(EV_COLUMNS, to_row({"type": "new_kind", "event_id": 1}, 0)))
    assert other["body"] is not None


def test_transform_steps_and_paths():
    steps = load_transform()
    names = [s.name for s in steps]
    assert names[0] == "create_ev" and names[-1] == "chunk_done"
    assert {"parse", "stage_delete"} == {s.name for s in steps if s.paths == {"sql"}}
    assert "@COHORT_CODES_SQL" not in " ".join(s.sql for s in steps)
    assert all("--" not in s.sql.splitlines()[0] for s in steps)


def test_parse_faults():
    assert parse_faults("advance:3:after_http, chunk_copy:1:mid") == (
        FaultSpec("advance", 3, "after_http"), FaultSpec("chunk_copy", 1, "mid"))
    assert parse_faults(None) == () and parse_faults("") == ()
    for bad in ("advance:0:x", "advance:x:y", "nope:1:x", "advance:1"):
        with pytest.raises(ValueError):
            parse_faults(bad)


def test_injector_fires_on_the_nth_hit_once():
    exits, fired = [], []
    inj = FaultInjector((FaultSpec("advance", 2, "after_t1"),), on_fire=fired.append,
                        exit_fn=exits.append)
    inj.hit("advance", "after_t1")
    inj.hit("advance", "after_http")
    assert exits == []
    inj.hit("advance", "after_t1")
    assert exits == [137] and fired == [FaultSpec("advance", 2, "after_t1")]
    inj.hit("advance", "after_t1")
    assert exits == [137]


def test_injector_stays_disarmed_when_already_fired():
    exits = []
    inj = FaultInjector((FaultSpec("advance", 1, "after_t1"),), already_fired=lambda s: True,
                        exit_fn=exits.append)
    inj.hit("advance", "after_t1")
    assert exits == [] and inj.armed == ()


def test_injector_ignores_boundaries_reached_while_recording_a_fault():
    """Regression: on_fire writes an audit row, which passes the audit_write boundary; with a
    second fault still armed this re-entered the injector's lock and hung the process (found by
    scripts/kill_test.sh)."""
    exits = []
    inj: FaultInjector

    def on_fire(spec):
        inj.hit("audit_write", "in_t2")

    inj = FaultInjector((FaultSpec("chunk_copy", 1, "mid"), FaultSpec("audit_write", 1, "in_t2")),
                        on_fire=on_fire, exit_fn=exits.append)
    import threading
    t = threading.Thread(target=inj.hit, args=("chunk_copy", "mid"))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive() and exits == [137]
