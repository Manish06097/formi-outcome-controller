"""Chunk loader: stream one chunk's NDJSON bytes from the simulator straight into `stage_raw`.

Owner: track T1 (milestone M1). Implements DESIGN.md 3.1 (first half) and 2 (loader thread).

`COPY stage_raw (chunk_no, line) FROM STDIN (FORMAT csv, DELIMITER E'\\x02', QUOTE E'\\x01')`
puts every line verbatim into one text column: JSON encoders escape control characters, so
neither control byte can occur in a line and no quoting ever happens. The chunk number is
prefixed to every line as the first CSV field while the bytes stream through (`prefix_lines`),
so a chunk is never held in memory: pieces of about 1 MiB go from httpx to the COPY.

The COPY is one statement in its own transaction: a chunk is staged completely or not at all.
Staged rows are deleted by the transform transaction that ingests the chunk; rows of a chunk
that was never ingested are stale and `reset_stage` truncates them on restart (DESIGN.md 2,
restart step 2). `PrefetchingStager` runs the loader in one background thread so chunk k+1
streams while the main thread transforms chunk k.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterable, Iterator, Protocol

from controller.reliability.faults import maybe_crash
from controller.simclient.errors import SimTransportError
from controller.store.db import Database
from controller.types import ChunkMeta

log = logging.getLogger(__name__)

COPY_SQL = ("COPY stage_raw (chunk_no, line) FROM STDIN "
            "(FORMAT csv, DELIMITER E'\\x02', QUOTE E'\\x01')")


class ChunkSource(Protocol):
    """The part of SimClient the ingest path uses (tests feed fixtures through it)."""

    def stream_chunk(self, n: int) -> Iterator[bytes]: ...
    def list_chunks(self, kind: str = "events", after: int = -1) -> list[ChunkMeta]: ...


def prefix_lines(pieces: Iterable[bytes], prefix: bytes) -> Iterator[bytes]:
    """Prefix every line of a byte stream with `prefix`, whatever the piece boundaries are.

    A line is prefixed when its first byte arrives; a trailing newline does not start a line, so
    the output has exactly one prefix per input line."""
    at_line_start = True
    for piece in pieces:
        if not piece:
            continue
        ends_nl = piece.endswith(b"\n")
        body = piece[:-1] if ends_nl else piece
        out = body.replace(b"\n", b"\n" + prefix)
        if at_line_start:
            out = prefix + out
        if ends_nl:
            out += b"\n"
        at_line_start = ends_nl
        yield out


class StageLoader:
    """Stages one chunk into `stage_raw` on a pooled connection (the loader's connection)."""

    def __init__(self, db: Database, source: ChunkSource, attempts: int = 3,
                 retry_sleep_s: float = 2.0):
        self.db = db
        self.source = source
        self.attempts = attempts
        self.retry_sleep_s = retry_sleep_s

    def stage(self, meta: ChunkMeta) -> int:
        """COPY chunk `meta.chunk`; returns the bytes streamed. Retries a stream that broke after
        its first byte (the COPY rolled back, so nothing partial is left)."""
        for attempt in range(1, self.attempts + 1):
            try:
                return self._stage_once(meta)
            except SimTransportError as e:
                if attempt == self.attempts:
                    raise
                log.warning("chunk %d stream failed (%s); retry %d", meta.chunk, e, attempt)
                time.sleep(self.retry_sleep_s * attempt)
        raise AssertionError("unreachable")

    def _stage_once(self, meta: ChunkMeta) -> int:
        prefix = f"{meta.chunk}\x02".encode()
        half = max(1, meta.events // 2)
        lines = 0
        crossed = False
        total = 0
        def counted() -> Iterator[bytes]:
            nonlocal total
            for raw in self.source.stream_chunk(meta.chunk):
                total += len(raw)
                yield raw

        with self.db.connection() as conn:
            with conn.transaction():
                with conn.cursor().copy(COPY_SQL) as cp:
                    for out in prefix_lines(counted(), prefix):
                        cp.write(out)
                        lines += out.count(b"\n")
                        if not crossed and lines >= half:
                            crossed = True
                            maybe_crash("chunk_copy", "mid")
        return total


class PrefetchingStager:
    """Stages chunks in order on one background thread, at most `ahead` chunks before use."""

    def __init__(self, loader: StageLoader, ahead: int = 1):
        self.loader = loader
        self.ahead = max(0, ahead)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chunk-loader")

    def run(self, chunks: list[ChunkMeta]) -> Iterator[tuple[ChunkMeta, int]]:
        """Yield (meta, bytes) in order once each chunk is staged."""
        futures: list[Future[int]] = []
        for i, meta in enumerate(chunks):
            while len(futures) <= min(i + self.ahead, len(chunks) - 1):
                futures.append(self._pool.submit(self.loader.stage, chunks[len(futures)]))
            yield meta, futures[i].result()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


def reset_stage(db: Database) -> None:
    """Drop staged rows of chunks never ingested (restart step 2). Run before any loader."""
    with db.tx() as conn:
        conn.execute("TRUNCATE stage_raw")
