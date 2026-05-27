"""Tests for bin/_log_auto_commit_failure.py.

Covers the bounded-growth contract for inner/state/auto-commit-failures.jsonl:
  - `output` truncated to 2 KB + marker
  - consecutive failures with matching (exit_code, files) dedupe via `count`
  - differing `files` appends a fresh entry
  - empty / missing file writes a single entry
  - malformed last line falls back to plain append (no crash)

See jcronq/alice#403.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# bin/ isn't a package and the helper has a leading underscore, so import it
# by path rather than relying on PYTHONPATH games.
_HELPER_PATH = (
    Path(__file__).resolve().parent.parent / "bin" / "_log_auto_commit_failure.py"
)
_spec = importlib.util.spec_from_file_location("_log_auto_commit_failure", _HELPER_PATH)
assert _spec is not None and _spec.loader is not None
log_mod = importlib.util.module_from_spec(_spec)
sys.modules["_log_auto_commit_failure"] = log_mod
_spec.loader.exec_module(log_mod)


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "auto-commit-failures.jsonl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_truncate_output(log_path: Path) -> None:
    # 3 KB of repeating content — well above the 2 KB cap.
    big = "x" * 3072
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output=big,
    )
    entries = _lines(log_path)
    assert len(entries) == 1
    out = entries[0]["output"]
    assert out.startswith("x" * 2048)
    assert "...[truncated, 1024 bytes omitted]" in out
    # Body stays within the cap + marker (small, fixed-size).
    assert len(out) < 2048 + 128


def test_dedup_consecutive(log_path: Path) -> None:
    common = dict(exit_code=1, files="a.md,b.md", output="lint failed\n")
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        **common,
    )
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:05:00-04:00",
        **common,
    )
    entries = _lines(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["count"] == 2
    # Original timestamp preserved; last_seen_at carries the latest.
    assert entry["timestamp"] == "2026-05-26T10:00:00-04:00"
    assert entry["last_seen_at"] == "2026-05-26T10:05:00-04:00"
    assert entry["files"] == "a.md,b.md"

    # A third identical failure increments count further.
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:10:00-04:00",
        **common,
    )
    entries = _lines(log_path)
    assert len(entries) == 1
    assert entries[0]["count"] == 3
    assert entries[0]["last_seen_at"] == "2026-05-26T10:10:00-04:00"


def test_different_files_appends(log_path: Path) -> None:
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output="boom",
    )
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:05:00-04:00",
        exit_code=1,
        files="b.md",
        output="boom",
    )
    entries = _lines(log_path)
    assert len(entries) == 2
    assert entries[0]["files"] == "a.md"
    assert entries[1]["files"] == "b.md"
    assert entries[0]["count"] == 1
    assert entries[1]["count"] == 1


def test_empty_file_first_write(log_path: Path) -> None:
    # Pre-create an empty file to exercise the empty-but-existing branch.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output="boom",
    )
    entries = _lines(log_path)
    assert len(entries) == 1
    assert entries[0]["files"] == "a.md"
    assert entries[0]["count"] == 1


def test_missing_file_first_write(log_path: Path) -> None:
    assert not log_path.exists()
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output="boom",
    )
    assert log_path.exists()
    assert len(_lines(log_path)) == 1


def test_malformed_last_line_appends(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("not valid json at all\n")
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output="boom",
    )
    text = log_path.read_text().splitlines()
    # Garbage line preserved; new JSON line appended after it.
    assert text[0] == "not valid json at all"
    assert json.loads(text[1])["files"] == "a.md"


def test_different_exit_code_appends(log_path: Path) -> None:
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output="boom",
    )
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:05:00-04:00",
        exit_code=2,
        files="a.md",
        output="boom",
    )
    assert len(_lines(log_path)) == 2


def test_dedup_keeps_original_output_truncation(log_path: Path) -> None:
    # The original output is large; a second identical-shape failure with a
    # different (also large) output must NOT replace the stored output.
    big_a = "a" * 3072
    big_b = "b" * 3072
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:00:00-04:00",
        exit_code=1,
        files="a.md",
        output=big_a,
    )
    log_mod.log_failure(
        log_path=log_path,
        timestamp="2026-05-26T10:05:00-04:00",
        exit_code=1,
        files="a.md",
        output=big_b,
    )
    entries = _lines(log_path)
    assert len(entries) == 1
    assert entries[0]["count"] == 2
    # Original output (truncated 'a's) is preserved, not replaced with 'b's.
    assert entries[0]["output"].startswith("a" * 2048)
