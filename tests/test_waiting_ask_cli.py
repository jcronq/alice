"""Tests for the ``waiting-ask`` CLI (``main_waiting_ask``).

Covers the three documented modes plus the error cases Jason actually
cares about: duplicate resolve (idempotent), missing-file resolve
(exits 1), missing-text append (exits 2), round-trip through
``asks.jsonl``.

All I/O goes through ``tmp_path`` + ``STATUS_DIR`` env override; no
live mind repo touched.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from status_site import cli as _cli


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_append_writes_record_and_prints_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["ping Jason about SM v2 tagging"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out  # record id printed
    records = _read_jsonl(tmp_path / "asks.jsonl")
    assert len(records) == 1
    r = records[0]
    assert r["id"] == out
    assert r["text"] == "ping Jason about SM v2 tagging"
    assert r["resolved"] is False
    assert "created" in r
    assert "resolved_at" not in r


def test_append_strips_text_and_rejects_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["   "])
    assert rc == 2
    # No file created on error.
    assert not (tmp_path / "asks.jsonl").exists()


def test_list_prints_pending_oldest_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    _cli.main_waiting_ask(["second"])
    _cli.main_waiting_ask(["third"])
    capsys.readouterr()  # drain
    # Insert an older ask manually.
    records = _read_jsonl(tmp_path / "asks.jsonl")
    older = {
        "id": "aaaaaaaa",
        "text": "first",
        "created": "2020-01-01T00:00:00+00:00",
        "resolved": False,
    }
    (tmp_path / "asks.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [older, *records]) + "\n"
    )
    rc = _cli.main_waiting_ask(["--list"])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("aaaaaaaa: first")
    assert out[1].endswith("second")
    assert out[2].endswith("third")


def test_list_empty_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["--list"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "(no pending asks)"


def test_resolve_marks_resolved_and_adds_timestamp(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["need a decision on X"])
    assert rc == 0
    ask_id = capsys.readouterr().out.strip()
    rc = _cli.main_waiting_ask(["--resolve", ask_id])
    assert rc == 0
    capsys.readouterr()
    [record] = _read_jsonl(tmp_path / "asks.jsonl")
    assert record["resolved"] is True
    assert "resolved_at" in record


def test_resolve_missing_id_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["real one"])
    capsys.readouterr()
    rc = _cli.main_waiting_ask(["--resolve", "doesnotexist"])
    assert rc == 1


def test_resolve_missing_file_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    rc = _cli.main_waiting_ask(["--resolve", "abc12345"])
    assert rc == 1


def test_resolve_idempotent_on_already_resolved(tmp_path, monkeypatch, capsys):
    """Resolving an already-resolved id is a no-op that exits 0.

    This matches how dispatcher retries should behave: if the previous
    attempt wrote ``resolved: true`` before an unrelated failure, a
    retry must not blow up or double-write ``resolved_at``."""
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    _cli.main_waiting_ask(["x"])
    ask_id = capsys.readouterr().out.strip()
    assert _cli.main_waiting_ask(["--resolve", ask_id]) == 0
    first = _read_jsonl(tmp_path / "asks.jsonl")[0]
    capsys.readouterr()
    assert _cli.main_waiting_ask(["--resolve", ask_id]) == 0
    second = _read_jsonl(tmp_path / "asks.jsonl")[0]
    assert first == second  # resolved_at not re-stamped


def test_append_and_resolve_round_trip_via_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    _cli.main_waiting_ask(["kept"])
    kept_id = capsys.readouterr().out.strip()
    _cli.main_waiting_ask(["drop"])
    drop_id = capsys.readouterr().out.strip()
    _cli.main_waiting_ask(["--resolve", drop_id])
    capsys.readouterr()
    rc = _cli.main_waiting_ask(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert kept_id in out
    assert drop_id not in out


def test_rejects_list_and_resolve_together(tmp_path, monkeypatch):
    """argparse's mutually-exclusive group rejects both flags at once."""
    monkeypatch.setenv("STATUS_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        _cli.main_waiting_ask(["--list", "--resolve", "abc"])
