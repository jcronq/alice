"""Tests for ``scripts/strip_sm_labels.py`` (issue #829, PR-6 of #823).

The ``gh`` CLI is never invoked here; the script's ``runner`` injection
point lets every test stub exact command/response pairs via a local
``FakeRunner`` (substring-of-joined-args → canned JSON).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from unittest.mock import Mock

import pytest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def strip_module():
    """Import the script by path so tests don't depend on PYTHONPATH."""
    if str(SCRIPT_PATH) not in sys.path:
        sys.path.insert(0, str(SCRIPT_PATH))
    mod = importlib.import_module("strip_sm_labels")
    return importlib.reload(mod)


class FakeRunner:
    """Returns pre-canned responses for ``gh`` commands + records calls.

    Keys are substring matches on the joined args. Values can be a
    plain string (returned) or a callable ``(args) -> str`` (invoked;
    may raise). Callables let a test observe write-before-raise
    ordering by checking the restore file at call time.
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        joined = " ".join(args)
        for key, value in self._responses.items():
            if key in joined:
                if callable(value):
                    return value(args)
                return value
        raise RuntimeError(f"FakeRunner: no response configured for {joined!r}")


def _issue(number: int, labels: list[str]) -> dict:
    return {
        "number": number,
        "labels": [{"name": name} for name in labels],
        "url": f"https://github.com/jcronq/alice/issues/{number}",
    }


# ---------------------------------------------------------------------------
# 1. Dry-run prints and makes no mutation
# ---------------------------------------------------------------------------


def test_dry_run_prints_without_mutation(strip_module, monkeypatch, capsys) -> None:
    issues = [
        _issue(100, ["sm:selected", "art:code"]),
        _issue(101, ["sm:design", "sm:review", "art:code"]),
        _issue(102, ["art:code"]),  # no sm:* — must be skipped
    ]
    runner = FakeRunner({"issue list": json.dumps(issues)})
    # Patch the module default so build_restore_manifest uses our runner.
    monkeypatch.setattr(strip_module, "_run_gh", runner)

    rc = strip_module.main(["--repo", "jcronq/alice"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "jcronq/alice #100: ['sm:selected']" in captured.out
    assert "jcronq/alice #101: ['sm:design', 'sm:review']" in captured.out
    assert "#102" not in captured.out  # no sm:* → skipped

    # Zero mutation: no issue edit anywhere in the call log.
    for args in runner.calls:
        assert "edit" not in args, f"dry-run must not call `gh issue edit`: {args!r}"


# ---------------------------------------------------------------------------
# 2. Apply writes the restore file BEFORE the first mutation
# ---------------------------------------------------------------------------


def test_apply_writes_restore_file_before_first_mutation(
    strip_module, monkeypatch, tmp_path
) -> None:
    issues = [_issue(200, ["sm:selected"]), _issue(201, ["sm:done"])]
    restore_path = tmp_path / "restore.json"
    monkeypatch.setattr(strip_module, "DEFAULT_LOG_FILE", tmp_path / "strip.log")

    observed: dict[str, bool] = {"restore_exists_at_edit_time": False}

    def edit_raises(args: list[str]) -> str:
        # Capture the ordering fact: by the time the first `gh issue
        # edit` would run, the restore file must already be on disk.
        observed["restore_exists_at_edit_time"] = restore_path.exists()
        raise RuntimeError("simulated crash on first edit")

    runner = FakeRunner(
        {
            "issue list": json.dumps(issues),
            "issue edit": edit_raises,
        }
    )
    monkeypatch.setattr(strip_module, "_run_gh", runner)

    rc = strip_module.main(
        [
            "--apply",
            "--repo",
            "jcronq/alice",
            "--restore-file",
            str(restore_path),
        ]
    )
    assert rc == 1  # mutation crashed → nonzero
    assert observed["restore_exists_at_edit_time"], (
        "restore file must exist on disk BEFORE the first `gh issue edit` call"
    )

    # The restore file persists after the crash with the full pre-
    # mutation (repo, issue, labels) list.
    assert restore_path.exists()
    saved = json.loads(restore_path.read_text())
    assert saved == [
        {"repo": "jcronq/alice", "issue": 200, "labels": ["sm:selected"]},
        {"repo": "jcronq/alice", "issue": 201, "labels": ["sm:done"]},
    ]


# ---------------------------------------------------------------------------
# 3. Issues with no sm:* label are skipped from manifest AND dry-run output
# ---------------------------------------------------------------------------


def test_no_sm_label_issues_are_skipped_entirely(
    strip_module, monkeypatch, tmp_path, capsys
) -> None:
    issues = [
        _issue(300, ["sm:selected", "art:code"]),  # kept
        _issue(301, ["art:code"]),  # skipped — no sm:*
    ]
    restore_path = tmp_path / "restore.json"
    monkeypatch.setattr(strip_module, "DEFAULT_LOG_FILE", tmp_path / "strip.log")
    runner = FakeRunner(
        {
            "issue list": json.dumps(issues),
            "issue edit": "",
        }
    )
    monkeypatch.setattr(strip_module, "_run_gh", runner)

    rc = strip_module.main(
        [
            "--apply",
            "--repo",
            "jcronq/alice",
            "--restore-file",
            str(restore_path),
        ]
    )
    assert rc == 0

    saved = json.loads(restore_path.read_text())
    assert saved == [
        {"repo": "jcronq/alice", "issue": 300, "labels": ["sm:selected"]}
    ]
    # No edit call references the skipped issue number.
    for args in runner.calls:
        if "edit" in args:
            assert "301" not in args, (
                f"no-label issue must not be edited: {args!r}"
            )


# ---------------------------------------------------------------------------
# 4. Rate-limit defer: apply_strip sleeps, re-polls, then proceeds
# ---------------------------------------------------------------------------


def test_apply_strip_defers_then_proceeds(strip_module, tmp_path) -> None:
    from alice_forge import rate_limit as rl

    manifest = [
        {"repo": "jcronq/alice", "issue": 400, "labels": ["sm:selected"]}
    ]
    runner = FakeRunner({"issue edit": ""})
    sleep = Mock()

    decisions = [
        rl.DeferDecision(defer=True, reason="under floor", retry_after_s=7.5),
        rl.DeferDecision(defer=False, reason="within budget", retry_after_s=None),
    ]

    def fake_should_defer():
        return decisions.pop(0)

    strip_module.apply_strip(
        manifest,
        runner=runner,
        sleep=sleep,
        should_defer=fake_should_defer,
        log_path=tmp_path / "strip.log",
    )

    sleep.assert_called_once_with(7.5)
    # After the deferral cleared, the mutation went through.
    edits = [c for c in runner.calls if "edit" in c]
    assert len(edits) == 1
    assert "--remove-label" in edits[0]
    assert "sm:selected" in edits[0]
    # Log line was appended.
    log_content = (tmp_path / "strip.log").read_text().splitlines()
    assert len(log_content) == 1
    log_entry = json.loads(log_content[0])
    assert log_entry["repo"] == "jcronq/alice"
    assert log_entry["issue"] == 400
    assert log_entry["labels_removed"] == ["sm:selected"]
