"""Tests for TurnLog — append, prune, and field-truncation helpers."""

from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

from alice_speaking.domain import turn_log
from alice_speaking.domain.turn_log import (
    MAX_FIELD_BYTES,
    PRUNE_SIZE_THRESHOLD_BYTES,
    RETENTION_SECONDS,
    TRUNCATION_MARKER,
    Turn,
    TurnLog,
)


def _turn(
    ts: float | None = None,
    inbound: str = "hi",
    outbound: str | None = "hello",
) -> Turn:
    return Turn(
        ts=ts if ts is not None else time.time(),
        sender_number="+1",
        sender_name="Owner",
        inbound=inbound,
        outbound=outbound,
    )


# ---------------------------------------------------------------- append basics


def test_appends_normal_turn(tmp_path: pathlib.Path) -> None:
    log = TurnLog(tmp_path / "speaking-turns.jsonl")
    log.append(_turn(inbound="hello", outbound="hi back"))
    lines = (tmp_path / "speaking-turns.jsonl").read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["inbound"] == "hello"
    assert parsed["outbound"] == "hi back"


def test_empty_file(tmp_path: pathlib.Path) -> None:
    """Appending into a fresh (non-existent) path creates the file with
    exactly one line — no prune kicks in on a tiny file."""
    path = tmp_path / "speaking-turns.jsonl"
    assert not path.exists()
    log = TurnLog(path)
    log.append(_turn())
    assert path.is_file()
    assert len(path.read_text().splitlines()) == 1


# ---------------------------------------------------------------- truncation


def test_truncates_oversized_field(tmp_path: pathlib.Path) -> None:
    log = TurnLog(tmp_path / "speaking-turns.jsonl")
    big = "x" * (MAX_FIELD_BYTES * 3)
    log.append(_turn(inbound=big, outbound="ok"))
    line = (tmp_path / "speaking-turns.jsonl").read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["inbound"].endswith(TRUNCATION_MARKER)
    assert len(parsed["inbound"]) == MAX_FIELD_BYTES
    # Untouched fields stay verbatim.
    assert parsed["outbound"] == "ok"


def test_small_fields_not_truncated(tmp_path: pathlib.Path) -> None:
    log = TurnLog(tmp_path / "speaking-turns.jsonl")
    log.append(_turn(inbound="short", outbound="also short"))
    parsed = json.loads((tmp_path / "speaking-turns.jsonl").read_text().splitlines()[0])
    assert parsed["inbound"] == "short"
    assert parsed["outbound"] == "also short"
    assert TRUNCATION_MARKER not in parsed["inbound"]


# ---------------------------------------------------------------- pruning


def _write_fixture(path: pathlib.Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_prunes_old_entries(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "speaking-turns.jsonl"
    now = time.time()
    old_ts = now - (RETENTION_SECONDS + 86400)  # 31 days old
    recent_ts = now - 3600  # 1 hour old
    fixture = [
        {
            "ts": old_ts,
            "sender_number": "+1",
            "sender_name": "Owner",
            "inbound": "ancient",
            "outbound": "history",
        },
        {
            "ts": recent_ts,
            "sender_number": "+1",
            "sender_name": "Owner",
            "inbound": "recent",
            "outbound": "now",
        },
    ]
    _write_fixture(path, fixture)
    log = TurnLog(path)
    log._prune_old_entries()
    remaining = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    assert len(remaining) == 1
    assert remaining[0]["inbound"] == "recent"


def test_prune_keeps_entries_without_ts(tmp_path: pathlib.Path) -> None:
    """Entries without a parseable ``ts`` field are retained — we can't
    judge their age, and silently dropping them is worse than keeping a
    little cruft."""
    path = tmp_path / "speaking-turns.jsonl"
    _write_fixture(
        path,
        [
            {"sender_name": "no-ts-field", "inbound": "keep me"},
            {
                "ts": time.time() - (RETENTION_SECONDS + 86400),
                "sender_name": "old",
                "inbound": "drop me",
            },
        ],
    )
    TurnLog(path)._prune_old_entries()
    remaining = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    assert len(remaining) == 1
    assert remaining[0]["sender_name"] == "no-ts-field"


def test_prune_noop_when_nothing_old(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "speaking-turns.jsonl"
    now = time.time()
    _write_fixture(
        path,
        [
            {"ts": now - 3600, "sender_name": "A", "inbound": "hi"},
            {"ts": now - 1800, "sender_name": "B", "inbound": "hi"},
        ],
    )
    mtime_before = path.stat().st_mtime
    TurnLog(path)._prune_old_entries()
    # No rewrite happened — mtime is unchanged.
    assert path.stat().st_mtime == mtime_before


def test_prune_atomic(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prune must rewrite via a ``.tmp`` sibling and ``os.replace`` so a
    crash mid-prune never leaves a partial log."""
    path = tmp_path / "speaking-turns.jsonl"
    old_ts = time.time() - (RETENTION_SECONDS + 86400)
    _write_fixture(
        path,
        [
            {"ts": old_ts, "sender_name": "A", "inbound": "drop"},
            {"ts": time.time(), "sender_name": "B", "inbound": "keep"},
        ],
    )

    seen_paths: list[tuple[str, str]] = []
    original_replace = os.replace

    def spy_replace(src, dst):
        seen_paths.append((str(src), str(dst)))
        return original_replace(src, dst)

    monkeypatch.setattr(turn_log.os, "replace", spy_replace)
    TurnLog(path)._prune_old_entries()
    assert seen_paths, "prune did not call os.replace"
    src, dst = seen_paths[0]
    assert src.endswith(".tmp")
    assert dst == str(path)
    # And the .tmp file is gone after the swap.
    assert not pathlib.Path(src).exists()


def test_append_triggers_prune_when_file_exceeds_threshold(
    tmp_path: pathlib.Path,
) -> None:
    """When the on-disk size crosses the prune threshold, an append
    should opportunistically drop entries older than the retention
    window. This is the size-gate documented in the module comments."""
    path = tmp_path / "speaking-turns.jsonl"
    now = time.time()
    old_ts = now - (RETENTION_SECONDS + 86400)
    # Seed enough old bytes to exceed the prune threshold without
    # needing a million entries. Each old entry pads ``inbound`` with
    # ~20 KB so we cross 1 MB quickly.
    padding = "p" * 20_000
    fixture = []
    while sum(len(json.dumps(e)) + 1 for e in fixture) < PRUNE_SIZE_THRESHOLD_BYTES:
        fixture.append(
            {
                "ts": old_ts,
                "sender_number": "+1",
                "sender_name": "Owner",
                "inbound": padding,
                "outbound": "old",
            }
        )
    _write_fixture(path, fixture)
    assert path.stat().st_size >= PRUNE_SIZE_THRESHOLD_BYTES

    log = TurnLog(path)
    log.append(_turn(inbound="brand new", outbound="now"))

    remaining = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    # All old fixture entries should be gone; the brand-new one stays.
    assert len(remaining) == 1
    assert remaining[0]["inbound"] == "brand new"


def test_append_skips_prune_under_threshold(tmp_path: pathlib.Path) -> None:
    """A small file should never get a full rewrite on every append —
    that's the whole point of the size gate."""
    path = tmp_path / "speaking-turns.jsonl"
    log = TurnLog(path)
    log.append(_turn(inbound="first"))
    log.append(_turn(inbound="second"))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    # Both entries survive — no prune happened.
    assert json.loads(lines[0])["inbound"] == "first"
    assert json.loads(lines[1])["inbound"] == "second"


# ---------------------------------------------------------------- tail still works


def test_tail_after_prune(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "speaking-turns.jsonl"
    now = time.time()
    old_ts = now - (RETENTION_SECONDS + 86400)
    _write_fixture(
        path,
        [
            {
                "ts": old_ts,
                "sender_number": "+1",
                "sender_name": "Old",
                "inbound": "ancient",
                "outbound": None,
            },
            {
                "ts": now - 60,
                "sender_number": "+1",
                "sender_name": "Recent",
                "inbound": "hi",
                "outbound": "hello",
            },
        ],
    )
    log = TurnLog(path)
    log._prune_old_entries()
    tail = log.tail(10)
    assert len(tail) == 1
    assert tail[0].sender_name == "Recent"
