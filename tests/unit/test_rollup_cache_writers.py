"""Guard for `RollupCache` (controller/snapshot.py, commit 9fb1b29).

`RollupCache` memoises the rollup reads of `load_snapshot`, keyed by the store's `ingested_chunk`
rows. That is only sound while the five rollup tables below are written by nothing except the
ingest transform (`controller/ingest/transform.sql`, same transaction as the chunk's
`ingested_chunk` row) and the migrations that create them. A second writer would change rows
without changing the key, and the cache would then feed a decision a value the store no longer
returns.

This test greps controller/ and scripts/ for SQL that writes those tables and fails with a pointer
to RollupCache. If it fails: either route the write through ingest/transform.sql, or extend
`RollupCache.store_key` so the new writer changes the key, and only then add the file here.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROLLUP_TABLES = ("r_engage", "r_pickup", "r_did", "r_flags", "lead_dim")
SCAN_ROOTS = ("controller", "scripts")
SCAN_SUFFIXES = {".py", ".sql", ".sh"}
ALLOWED = {
    REPO / "controller" / "ingest" / "transform.sql",
}
ALLOWED_DIRS = (REPO / "controller" / "store" / "migrations",)

_T = r"(?:\w+\.)?(?:" + "|".join(ROLLUP_TABLES) + r")(?!\w)"
WRITE = re.compile(
    r"\b(?:INSERT\s+INTO\s+" + _T + r"|UPDATE\s+(?:ONLY\s+)?" + _T + r"|DELETE\s+FROM\s+(?:ONLY\s+)?"
    + _T + r"|TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?" + _T + r"|COPY\s+" + _T + r"\b)",
    re.IGNORECASE)


def _files() -> list[Path]:
    out = []
    for root in SCAN_ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            if p.suffix not in SCAN_SUFFIXES or "__pycache__" in p.parts or ".venv" in p.parts:
                continue
            if p in ALLOWED or any(d in p.parents for d in ALLOWED_DIRS):
                continue
            out.append(p)
    return out


def writers(text: str) -> list[str]:
    return [" ".join(m.group(0).split()) for m in WRITE.finditer(text)]


def test_only_ingest_transform_writes_the_rollup_tables():
    bad = []
    for p in _files():
        for w in writers(p.read_text(errors="replace")):
            bad.append(f"{p.relative_to(REPO)}: {w}")
    assert not bad, (
        "SQL writes to a rollup table outside controller/ingest/transform.sql and the migrations:\n  "
        + "\n  ".join(bad)
        + "\nRollupCache (controller/snapshot.py) memoises the reads of r_engage, r_pickup, r_did, "
          "r_flags and lead_dim keyed by the store's ingested_chunk rows, so it is valid only while "
          "nothing else writes them. Move the write into ingest/transform.sql, or make the new "
          "writer change RollupCache.store_key, then update this test.")


def test_the_pattern_catches_each_write_form():
    for sql in ("INSERT INTO r_did (a) VALUES (1)", "insert\n into  public.lead_dim x",
                "UPDATE lead_dim SET state = 1", "update only r_engage set a=1",
                "DELETE FROM r_pickup WHERE 1", "TRUNCATE TABLE r_flags", "truncate r_did",
                "COPY r_engage FROM STDIN"):
        assert writers(sql), sql


def test_the_pattern_ignores_reads_and_lookalikes():
    for sql in ("SELECT * FROM r_did", "UPDATE r_did_extra SET a=1", "INSERT INTO lead_dimension x",
                "SELECT 1 FROM lead_dim l JOIN r_pickup p USING (wf)"):
        assert not writers(sql), sql


def test_the_scan_sees_the_allowed_writer():
    # the scan must find transform.sql's writes, or the allow-list would hide a broken pattern
    assert len(writers((REPO / "controller/ingest/transform.sql").read_text())) >= 5
