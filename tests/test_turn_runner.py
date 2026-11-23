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
import pathlib
import subprocess
from typing import Any
from unittest.mock import patch

from alice_speaking import turn_runner as tr_module
from alice_speaking.domain.turn_log import Turn, TurnLog
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


# ---------------------------------------------------------------------
# Pi-mono preamble-every-turn (session-continuity bug fix).
#
# Pi has no SDK-session resume (``pi -p ... --no-session`` is one-shot);
# turn N+1 would see only the latest user message unless TurnRunner
# rebuilds the turn-log preamble every real turn. These tests cover the
# three branches of :meth:`TurnRunner._prime_pi_mono_preamble`:
#
#   * summary present → ``build_summary_preamble(summary, tail)``
#   * tail non-empty, no summary → ``build_bootstrap_preamble(tail)``
#   * tail empty, no summary → preamble cleared (first fresh turn)
#
# Plus two run_turn integration branches:
#
#   * pi-mono + non-silent turn → rebuild fires
#   * pi-mono + silent (compaction/bootstrap) turn → rebuild skipped
#


class _StubBackend:
    """Minimal BackendSpec-shaped stub — TurnRunner only reads
    ``.harness`` for the pi-mono branch."""

    def __init__(self, harness: str) -> None:
        self.harness = harness


class _StubEvents:
    """Collects ``emit`` calls so tests can assert on emitted events."""

    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    def emit(self, name: str, **fields: Any) -> None:
        self.emitted.append({"name": name, **fields})


def _pi_mono_runner(
    tmp_path: pathlib.Path,
    *,
    harness: str = "pi-mono",
    preamble_turns: int = 10,
) -> TurnRunner:
    """Bare runner shell populated with just the fields
    :meth:`_prime_pi_mono_preamble` + :meth:`_build_handlers` touch."""
    runner = TurnRunner.__new__(TurnRunner)
    runner._cfg = type(
        "C",
        (),
        {"speaking": {"pi_mono_preamble_turns": preamble_turns}},
    )()
    runner._turns = TurnLog(tmp_path / "turns.jsonl")
    runner._summary_path = tmp_path / "context-summary.md"
    runner._events = _StubEvents()
    runner._backend = _StubBackend(harness)
    runner._pending_preamble = None
    return runner


def _write_turn(log: TurnLog, inbound: str, outbound: str) -> None:
    log.append(
        Turn(
            ts=1_700_000_000.0,
            sender_number="+1",
            sender_name="jason",
            inbound=inbound,
            outbound=outbound,
        )
    )


def test_prime_pi_mono_preamble_uses_turn_log_tail_when_no_summary(
    tmp_path: pathlib.Path,
) -> None:
    """Non-empty turn_log, no summary → preamble from
    ``build_bootstrap_preamble(tail)`` + a ``context_bootstrap`` event
    tagged ``source=pi_mono_turn_log``."""
    runner = _pi_mono_runner(tmp_path)
    _write_turn(runner._turns, "hello", "hi there")
    _write_turn(runner._turns, "what's my name?", "I don't know yet")

    runner._prime_pi_mono_preamble()

    assert runner._pending_preamble is not None
    assert "hello" in runner._pending_preamble
    assert "hi there" in runner._pending_preamble
    assert "Resume naturally" in runner._pending_preamble
    source_events = [
        e for e in runner._events.emitted if e["name"] == "context_bootstrap"
    ]
    assert len(source_events) == 1
    assert source_events[0]["source"] == "pi_mono_turn_log"
    assert source_events[0]["tail_len"] == 2


def test_prime_pi_mono_preamble_prefers_summary_when_present(
    tmp_path: pathlib.Path,
) -> None:
    """Summary on disk → ``build_summary_preamble(summary, tail)`` +
    a ``context_bootstrap`` event tagged ``source=pi_mono_summary``."""
    runner = _pi_mono_runner(tmp_path)
    _write_turn(runner._turns, "latest question", "latest reply")
    runner._summary_path.write_text("COMPACTION SUMMARY TEXT\n")

    runner._prime_pi_mono_preamble()

    assert runner._pending_preamble is not None
    assert "COMPACTION SUMMARY TEXT" in runner._pending_preamble
    assert "Context summary" in runner._pending_preamble
    # Recent-turns tail is also appended by build_summary_preamble.
    assert "latest question" in runner._pending_preamble
    source_events = [
        e for e in runner._events.emitted if e["name"] == "context_bootstrap"
    ]
    assert source_events[0]["source"] == "pi_mono_summary"


def test_prime_pi_mono_preamble_empty_turn_log_clears_pending(
    tmp_path: pathlib.Path,
) -> None:
    """Empty turn_log + no summary → ``_pending_preamble`` is cleared
    (so a stale daemon-start bootstrap doesn't survive into turn 2)."""
    runner = _pi_mono_runner(tmp_path)
    runner._pending_preamble = "stale preamble from daemon start"

    runner._prime_pi_mono_preamble()

    assert runner._pending_preamble is None
    # No event emitted for the degenerate empty case.
    assert [e for e in runner._events.emitted if e["name"] == "context_bootstrap"] == []


def test_prime_pi_mono_preamble_respects_configured_tail_length(
    tmp_path: pathlib.Path,
) -> None:
    """``pi_mono_preamble_turns`` caps how many prior turns the preamble
    pulls — 3 turns written, cap=2 → ``tail_len=2`` in the emitted event
    and the oldest turn is not quoted in the preamble."""
    runner = _pi_mono_runner(tmp_path, preamble_turns=2)
    _write_turn(runner._turns, "OLDEST", "older-reply")
    _write_turn(runner._turns, "middle", "middle-reply")
    _write_turn(runner._turns, "newest", "newest-reply")

    runner._prime_pi_mono_preamble()

    assert runner._pending_preamble is not None
    assert "OLDEST" not in runner._pending_preamble
    assert "newest" in runner._pending_preamble
    source_events = [
        e for e in runner._events.emitted if e["name"] == "context_bootstrap"
    ]
    assert source_events[0]["tail_len"] == 2


def test_prime_pi_mono_preamble_replaces_existing_pending(
    tmp_path: pathlib.Path,
) -> None:
    """The whole point: a previous turn left no session-level context
    carry (pi has no resume), so we OVERWRITE whatever preamble was
    there with the current turn_log rebuild — not append."""
    runner = _pi_mono_runner(tmp_path)
    _write_turn(runner._turns, "canonical question", "canonical answer")
    runner._pending_preamble = "STALE FROM PRIOR TURN — SHOULD BE REPLACED"

    runner._prime_pi_mono_preamble()

    assert runner._pending_preamble is not None
    assert "STALE FROM PRIOR TURN" not in runner._pending_preamble
    assert "canonical question" in runner._pending_preamble


def test_prime_bootstrap_preamble_early_returns_when_session_id_set(
    tmp_path: pathlib.Path,
) -> None:
    """Sanity check: for non-pi-mono backends, the original
    :meth:`prime_bootstrap_preamble` still guards on session_id so the
    Layer 1 (SDK resume) path keeps its current ownership of turn N+1
    context. The pi-mono branch bypasses this guard; everything else
    retains it."""
    runner = _pi_mono_runner(tmp_path, harness="claude-code")
    _write_turn(runner._turns, "q", "a")
    runner.session_id = "existing-sdk-session-id"

    runner.prime_bootstrap_preamble()

    assert runner._pending_preamble is None
