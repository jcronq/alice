"""Tests for session_state — the session.json read/write/clear helpers."""

from __future__ import annotations

import json
import pathlib


from alice_speaking.domain import session_state


def test_write_read_roundtrip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.json"
    session_state.write(path, "abc123")
    result = session_state.read(path)
    assert result is not None
    assert result.session_id == "abc123"
    assert result.saved_at  # ISO stamp populated


def test_write_creates_parent(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "a" / "b" / "session.json"
    session_state.write(path, "xyz")
    assert path.is_file()


def test_write_is_atomic(tmp_path: pathlib.Path) -> None:
    """Writing twice must leave session.json parseable at every
    filesystem snapshot — no torn half-writes."""
    path = tmp_path / "session.json"
    session_state.write(path, "first")
    session_state.write(path, "second")
    # The temp file should have been cleaned up.
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()
    result = session_state.read(path)
    assert result is not None
    assert result.session_id == "second"


def test_read_missing_returns_none(tmp_path: pathlib.Path) -> None:
    assert session_state.read(tmp_path / "missing.json") is None


def test_read_corrupt_returns_none(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("not json {")
    assert session_state.read(path) is None


def test_read_missing_session_id_returns_none(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"other": "field"}))
    assert session_state.read(path) is None


def test_clear_removes_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.json"
    session_state.write(path, "abc")
    session_state.clear(path)
    assert not path.is_file()


def test_clear_is_idempotent(tmp_path: pathlib.Path) -> None:
    # Should not raise when the file is already gone.
    session_state.clear(tmp_path / "never-existed.json")


def test_sdk_session_exists(tmp_path: pathlib.Path) -> None:
    assert not session_state.sdk_session_exists(tmp_path, "abc")
    jsonl = session_state.sdk_session_jsonl_path(tmp_path, "abc")
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}\n")
    assert session_state.sdk_session_exists(tmp_path, "abc")
