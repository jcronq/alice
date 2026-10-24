"""Tests for :meth:`TurnRunner._log_pre_turn_hygiene`.

Option A (log-only) visibility check. The contract this file enforces:

* Clean state → INFO log with all four fields populated, no WARNING.
* Stash pile above threshold → WARNING with the ``stash=...`` trigger.
* Feature branch with commits ahead of origin/master → WARNING with the
  ``branch=.../unpushed=...`` trigger.
* Any subprocess failure → DEBUG log, method returns without raising.
* Missing origin/master (git returns non-zero) → ``unpushed=None``, no
  spurious WARNING on the unpushed condition.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any
from unittest.mock import patch

from alice_speaking import turn_runner as tr_module
from alice_speaking.turn_runner import TurnRunner


def _fake_completed(stdout: str = "", returncode: int = 0) -> Any:
    """Build the minimum shape :meth:`_log_pre_turn_hygiene` reads off
    ``subprocess.run``: ``returncode`` and ``stdout``."""

    class _Fake:
        pass

    fake = _Fake()
    fake.returncode = returncode
    fake.stdout = stdout
    fake.stderr = ""
    return fake


def _make_runner() -> TurnRunner:
    """A bare :class:`TurnRunner` shell — ``__init__`` needs a lot of
    collaborators, but ``_log_pre_turn_hygiene`` only touches module-level
    state, so we bypass ``__init__`` entirely."""
    return TurnRunner.__new__(TurnRunner)


# ---------------------------------------------------------------------
# Git call dispatch — the method issues four git calls in this order.

_BRANCH_ARGS = ("rev-parse", "--abbrev-ref", "HEAD")
_STASH_ARGS = ("stash", "list")
_UNTRACKED_ARGS = ("ls-files", "--others", "--exclude-standard")
_UNPUSHED_ARGS = ("rev-list", "--count", "origin/master..HEAD")


def _dispatch(responses: dict[tuple[str, ...], Any]):
    """Return a fake ``subprocess.run`` that picks a response by the
    trailing git args tuple. Missing entries raise so the test fails
    loudly on an unexpected git command instead of silently defaulting."""

    def _run(cmd: list[str], **_kwargs: Any) -> Any:
        assert cmd[0] == "git", f"unexpected exec: {cmd}"
        key = tuple(cmd[1:])
        if key not in responses:
            raise AssertionError(f"no fake response registered for git {key}")
        value = responses[key]
        if isinstance(value, BaseException):
            raise value
        return value

    return _run


# ---------------------------------------------------------------------
# Tests


def test_happy_path_clean_state_emits_info_only(caplog: Any) -> None:
    """Clean repo (master, no stashes, no untracked, no unpushed): a
    single INFO line with all four fields, and NO WARNING."""
    responses = {
        _BRANCH_ARGS: _fake_completed(stdout="master\n"),
        _STASH_ARGS: _fake_completed(stdout=""),
        _UNTRACKED_ARGS: _fake_completed(stdout=""),
        _UNPUSHED_ARGS: _fake_completed(stdout="0\n"),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            runner._log_pre_turn_hygiene()

    info_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(info_records) == 1, [r.getMessage() for r in caplog.records]
    msg = info_records[0].getMessage()
    assert "branch=master" in msg
    assert "stash=0" in msg
    assert "untracked=0" in msg
    assert "unpushed=0" in msg

    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "pre_turn_hygiene" in r.getMessage()
    ]
    assert warn_records == []


def test_stash_threshold_triggers_warning(caplog: Any) -> None:
    """Stash count of 21 > threshold of 20 → WARNING mentioning ``stash``."""
    stash_body = "\n".join(f"stash@{{{i}}}: WIP on foo: bar" for i in range(21))
    responses = {
        _BRANCH_ARGS: _fake_completed(stdout="master\n"),
        _STASH_ARGS: _fake_completed(stdout=stash_body + "\n"),
        _UNTRACKED_ARGS: _fake_completed(stdout=""),
        _UNPUSHED_ARGS: _fake_completed(stdout="0\n"),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            runner._log_pre_turn_hygiene()

    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(warn_records) == 1
    msg = warn_records[0].getMessage()
    assert "stash=21" in msg
    # Should NOT also include the branch/unpushed trigger.
    assert "branch=" not in msg


def test_branch_drift_triggers_warning(caplog: Any) -> None:
    """Non-master branch with unpushed=6 (> threshold 5) → WARNING that
    names both the branch and the unpushed count."""
    responses = {
        _BRANCH_ARGS: _fake_completed(stdout="feat/foo\n"),
        _STASH_ARGS: _fake_completed(stdout=""),
        _UNTRACKED_ARGS: _fake_completed(stdout=""),
        _UNPUSHED_ARGS: _fake_completed(stdout="6\n"),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            runner._log_pre_turn_hygiene()

    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(warn_records) == 1
    msg = warn_records[0].getMessage()
    assert "branch=feat/foo" in msg
    assert "unpushed=6" in msg
    # Should NOT include the stash trigger.
    assert "stash=" not in msg


def test_subprocess_timeout_is_swallowed(caplog: Any) -> None:
    """A raised ``TimeoutExpired`` on ANY git call → DEBUG log, no
    exception, no crash. The rest of the method still runs (each call is
    independent), the INFO line comes out with ``None`` fields for the
    failed calls."""
    responses = {
        _BRANCH_ARGS: subprocess.TimeoutExpired(cmd="git", timeout=2),
        _STASH_ARGS: subprocess.TimeoutExpired(cmd="git", timeout=2),
        _UNTRACKED_ARGS: subprocess.TimeoutExpired(cmd="git", timeout=2),
        _UNPUSHED_ARGS: subprocess.TimeoutExpired(cmd="git", timeout=2),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            # Must not raise.
            runner._log_pre_turn_hygiene()

    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "pre_turn_hygiene" in r.getMessage()
    ]
    assert debug_records, "expected DEBUG log for swallowed TimeoutExpired"

    info_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(info_records) == 1
    msg = info_records[0].getMessage()
    assert "branch=None" in msg
    assert "stash=None" in msg
    assert "untracked=None" in msg
    assert "unpushed=None" in msg


def test_missing_origin_master_is_handled_cleanly(caplog: Any) -> None:
    """``git rev-list --count origin/master..HEAD`` returns non-zero when
    the remote ref is missing. We should log ``unpushed=None`` and NOT
    trigger the branch-drift warning on a ``None`` value."""
    responses = {
        _BRANCH_ARGS: _fake_completed(stdout="feat/new-thing\n"),
        _STASH_ARGS: _fake_completed(stdout=""),
        _UNTRACKED_ARGS: _fake_completed(stdout=""),
        _UNPUSHED_ARGS: _fake_completed(
            stdout="",
            returncode=128,  # git's "unknown revision" exit code
        ),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            runner._log_pre_turn_hygiene()

    info_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(info_records) == 1
    msg = info_records[0].getMessage()
    assert "unpushed=None" in msg
    assert "branch=feat/new-thing" in msg

    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "pre_turn_hygiene" in r.getMessage()
    ]
    assert warn_records == [], "None unpushed must not trip the drift warning"


def test_git_not_installed_is_swallowed(caplog: Any) -> None:
    """The other side of the resilience contract: if the ``git`` binary
    itself isn't on PATH (``FileNotFoundError``) we log DEBUG and move on."""
    responses = {
        _BRANCH_ARGS: FileNotFoundError(2, "No such file or directory: 'git'"),
        _STASH_ARGS: FileNotFoundError(2, "No such file or directory: 'git'"),
        _UNTRACKED_ARGS: FileNotFoundError(2, "No such file or directory: 'git'"),
        _UNPUSHED_ARGS: FileNotFoundError(2, "No such file or directory: 'git'"),
    }
    runner = _make_runner()

    with caplog.at_level(logging.DEBUG, logger=tr_module.log.name):
        with patch.object(tr_module.subprocess, "run", _dispatch(responses)):
            runner._log_pre_turn_hygiene()

    # Method returned normally — that's the test.
    info_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "pre_turn_hygiene" in r.getMessage()
    ]
    assert len(info_records) == 1


def test_module_level_repo_path_resolves_to_repo_root() -> None:
    """``REPO_PATH`` is a ``pathlib.Path`` resolved from ``__file__``.
    Sanity-check that it points at a directory that at least *looks* like
    the alice repo (has ``src/alice_speaking/turn_runner.py``)."""
    assert tr_module.REPO_PATH.is_dir()
    assert (
        tr_module.REPO_PATH / "src" / "alice_speaking" / "turn_runner.py"
    ).is_file()
