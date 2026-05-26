"""Integration tests for :mod:`alice_forge.task_cli`.

These exercise the argparse layer end-to-end against a tmp-path
store. Coverage:

* ``create`` writes task + emits id.
* ``update`` transitions and prints the new state.
* ``list`` returns matching entries and honours ``--status open``.
* ``view`` prints task metadata + transition history.
* ``close`` is sugar over ``update --status done``.
* Invalid transitions exit non-zero.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_forge import task_cli


def _run(args: list[str], root: pathlib.Path) -> int:
    """Invoke the CLI with a fixed --root and return the exit code."""
    return task_cli.main(["--root", str(root), *args])


def test_create_emits_id(tmp_path, capsys) -> None:
    code = _run(["create", "--title", "x", "--tags", "auto-fix,foo"], tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "task-0001" in out
    assert "[draft]" in out


def test_create_json_mode(tmp_path, capsys) -> None:
    code = _run(
        ["--json", "create", "--title", "x", "--tags", "auto-fix"], tmp_path
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["id"] == "task-0001"
    assert payload["status"] == "draft"
    assert payload["tags"] == ["auto-fix"]


def test_update_valid_transition(tmp_path, capsys) -> None:
    _run(["create", "--title", "x"], tmp_path)
    capsys.readouterr()
    code = _run(
        ["update", "task-0001", "--status", "selected", "--reason", "go"],
        tmp_path,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[selected]" in out


def test_update_invalid_transition_exit_code(tmp_path) -> None:
    _run(["create", "--title", "x"], tmp_path)
    code = _run(["update", "task-0001", "--status", "building"], tmp_path)
    assert code == 3  # InvalidTransition exit code


def test_update_unknown_status_exit_code(tmp_path) -> None:
    _run(["create", "--title", "x"], tmp_path)
    # argparse rejects unknown --status at parse time → SystemExit(2)
    with pytest.raises(SystemExit):
        _run(["update", "task-0001", "--status", "bogus"], tmp_path)


def test_list_filters_open(tmp_path, capsys) -> None:
    _run(["create", "--title", "a"], tmp_path)
    _run(["create", "--title", "b"], tmp_path)
    _run(["update", "task-0001", "--status", "selected"], tmp_path)
    _run(["update", "task-0001", "--status", "building"], tmp_path)
    _run(
        [
            "update",
            "task-0001",
            "--status",
            "done",
            "--merge-ref",
            "https://github.com/jcronq/alice/pull/1",
        ],
        tmp_path,
    )
    capsys.readouterr()
    code = _run(["list", "--status", "open"], tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "task-0002" in out
    assert "task-0001" not in out


def test_view_prints_transitions(tmp_path, capsys) -> None:
    _run(["create", "--title", "x"], tmp_path)
    _run(["update", "task-0001", "--status", "selected", "--reason", "go"], tmp_path)
    capsys.readouterr()
    code = _run(["view", "task-0001"], tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "task-0001" in out
    assert "selected" in out
    assert "go" in out


def test_close_succeeds_with_merge_ref(tmp_path, capsys) -> None:
    _run(["create", "--title", "x"], tmp_path)
    _run(["update", "task-0001", "--status", "selected"], tmp_path)
    _run(["update", "task-0001", "--status", "building"], tmp_path)
    capsys.readouterr()
    code = _run(
        [
            "close",
            "task-0001",
            "--merge-ref",
            "https://github.com/jcronq/alice/pull/1",
        ],
        tmp_path,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[done]" in out


def test_missing_task_exit_code(tmp_path) -> None:
    code = task_cli.main(
        ["--root", str(tmp_path), "view", "task-0099"]
    )
    assert code == 2
