"""Regression: the viewer's JSONL tail-parse cache must not grow without bound.

thinking.log is append-only and never rotated (11 GB in production). The
old ``_cached_jsonl`` decoded the whole file and retained every parsed
event forever, so the viewer child grew to 30+ GB and swapped the host.
These tests pin the bounded trailing-window behaviour.
"""

from __future__ import annotations

import gc
import json
import pathlib
import tracemalloc

import pytest

from viewer import sources


@pytest.fixture(autouse=True)
def _clear_cache():
    sources._jsonl_caches.clear()
    yield
    sources._jsonl_caches.clear()


def _line(i: int, pad: int = 200) -> bytes:
    rec = {"ts": float(i), "event": "assistant", "i": i, "text": "x" * pad}
    return (json.dumps(rec) + "\n").encode()


def _append(path: pathlib.Path, start: int, n: int, pad: int = 200) -> None:
    with path.open("ab") as f:
        f.write(b"".join(_line(i, pad) for i in range(start, start + n)))


def test_cold_start_only_parses_trailing_window(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_VIEWER_JSONL_WINDOW_BYTES", str(10_000))
    log = tmp_path / "thinking.log"
    _append(log, 0, 1000)  # ~250 KB

    events = sources.read_thinking(log)

    assert 0 < len(events) <= 10_000 // 200
    ids = [e.detail["i"] for e in events]
    # Contiguous run ending at the last record; the partial line at the cut
    # is skipped rather than mis-parsed.
    assert ids == list(range(ids[0], 1000))
    assert log.stat().st_size - sources._jsonl_caches[str(log)].starts[0] <= 10_000


def test_small_file_fully_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_VIEWER_JSONL_WINDOW_BYTES", str(10_000_000))
    log = tmp_path / "thinking.log"
    _append(log, 0, 50)
    assert [e.detail["i"] for e in sources.read_thinking(log)] == list(range(50))


def test_chunked_reader_handles_boundaries_and_partial_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_JSONL_READ_CHUNK_BYTES", 37)  # splits lines
    log = tmp_path / "speaking.log"
    _append(log, 0, 40, pad=13)
    with log.open("ab") as f:
        f.write(b"\n")  # blank line
        f.write(b"not json\n")
        f.write(b'{"ts": 99, "event": "partial"')  # writer mid-flush
    got = list(sources._iter_jsonl_tail(log, 0))
    recs = [r for r, _ in got if r is not None]
    assert [r["i"] for r in recs] == list(range(40))
    assert len(got) == 42  # 40 records + blank + malformed; partial withheld
    assert got[-1][1] == log.stat().st_size - len(b'{"ts": 99, "event": "partial"')


def test_eviction_does_not_mutate_previously_returned_list(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_VIEWER_JSONL_WINDOW_BYTES", str(5_000))
    log = tmp_path / "thinking.log"
    _append(log, 0, 10)
    first = sources.read_thinking(log)
    snapshot = list(first)
    _append(log, 10, 200)
    second = sources.read_thinking(log)
    assert first[: len(snapshot)] == snapshot
    assert second[-1].detail["i"] == 209
    assert second[0].detail["i"] > 0


def test_retained_memory_flat_as_log_grows(tmp_path, monkeypatch):
    """Repeated incremental reads of an ever-growing log must plateau.

    With the pre-fix cache, retained memory scaled linearly with total
    bytes appended (~5x growth between the two checkpoints below).
    """
    window = 200_000
    monkeypatch.setenv("ALICE_VIEWER_JSONL_WINDOW_BYTES", str(window))
    log = tmp_path / "thinking.log"

    def run(batches: int, start: int) -> int:
        nxt = start
        for _ in range(batches):
            _append(log, nxt, 500)  # ~125 KB per batch
            nxt += 500
            sources.read_thinking(log)
        return nxt

    tracemalloc.start()
    try:
        nxt = run(5, 0)  # ~625 KB appended — already past the window
        gc.collect()
        early, _ = tracemalloc.get_traced_memory()
        run(20, nxt)  # ~3.1 MB total appended
        gc.collect()
        late, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    events = sources._jsonl_caches[str(log)].events
    assert len(events) <= window // 200
    assert late < early * 1.5, f"retained memory grew {early} -> {late}"
