"""Integration tests for the rate-limit pre-flight gate on SM spawns.

Issue #786 M3. Verifies each ``spawn_*_agent`` honors the
``rate_gate`` kwarg: when the gate defers, no subprocess is spawned,
no audit comment is posted, and the function returns ``None``.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from alice_forge import rate_limit as rl
from alice_forge.dispatcher import spawn as spawn_mod


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Popen:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = list(args[0]) if args else []
        self.kwargs = kwargs
        self.pid = 12345


class _Comments:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, repo: str, number: int, body: str) -> None:
        self.calls.append((repo, number, body))


def _issue(number: int, *, art_label: str = "art:code") -> dict[str, Any]:
    return {
        "number": number,
        "title": "test issue",
        "body": "body",
        "labels": [
            {"name": "sm:selected"},
            {"name": art_label},
            {"name": "source:jcronq"},
        ],
        "author": {"login": "jcronq"},
        "createdAt": "2026-11-07T00:00:00Z",
    }


def _defer_gate(_kind: str, **_kwargs: Any) -> rl.DeferDecision:
    return rl.DeferDecision(
        defer=True,
        reason="test forced defer",
        retry_after_s=60.0,
    )


def _pass_gate(_kind: str, **_kwargs: Any) -> rl.DeferDecision:
    return rl.DeferDecision(defer=False, reason="test forced pass", retry_after_s=None)


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------


def test_spawn_agent_defer_returns_none_without_popen_or_comment(
    tmp_path: pathlib.Path,
) -> None:
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []
    comments = _Comments()

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_agent(
        _issue(500, art_label="art:config_change"),
        "art:config_change",
        "jcronq/alice",
        spawn_dir=spawn_dir,
        claude_bin="/usr/bin/claude",
        post_comment=comments,
        popen=popen,
        now_iso=lambda: "2026-11-07T00:00:00Z",
        log=lambda _m: None,
        rate_gate=_defer_gate,
    )

    assert result is None
    assert popen_calls == []
    assert comments.calls == []
    # No work dir was created either.
    assert list(spawn_dir.iterdir()) == []


def test_spawn_agent_pass_gate_proceeds_normally(tmp_path: pathlib.Path) -> None:
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []
    comments = _Comments()

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_agent(
        _issue(501, art_label="art:config_change"),
        "art:config_change",
        "jcronq/alice",
        spawn_dir=spawn_dir,
        claude_bin="/usr/bin/claude",
        post_comment=comments,
        popen=popen,
        now_iso=lambda: "2026-11-07T00:00:00Z",
        log=lambda _m: None,
        rate_gate=_pass_gate,
    )

    assert result is not None
    assert len(popen_calls) == 1
    assert len(comments.calls) == 1


def test_spawn_agent_none_gate_disables_check(tmp_path: pathlib.Path) -> None:
    """``rate_gate=None`` is the test escape hatch — gate is skipped."""
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []
    comments = _Comments()

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_agent(
        _issue(502, art_label="art:config_change"),
        "art:config_change",
        "jcronq/alice",
        spawn_dir=spawn_dir,
        claude_bin="/usr/bin/claude",
        post_comment=comments,
        popen=popen,
        now_iso=lambda: "2026-11-07T00:00:00Z",
        log=lambda _m: None,
        rate_gate=None,
    )

    assert result is not None
    assert len(popen_calls) == 1


# ---------------------------------------------------------------------------
# spawn_thinking_agent
# ---------------------------------------------------------------------------


def test_spawn_thinking_agent_defer_returns_none(tmp_path: pathlib.Path) -> None:
    spawn_dir = tmp_path / "thinking-spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []
    comments = _Comments()

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_thinking_agent(
        _issue(600),
        "art:code",
        "jcronq/alice",
        spawn_dir=spawn_dir,
        python_bin="/usr/bin/python3",
        post_comment=comments,
        popen=popen,
        now_iso=lambda: "2026-11-07T00:00:00Z",
        log=lambda _m: None,
        rate_gate=_defer_gate,
    )

    assert result is None
    assert popen_calls == []
    assert comments.calls == []


# ---------------------------------------------------------------------------
# spawn_speaking_agent
# ---------------------------------------------------------------------------


def test_spawn_speaking_agent_defer_returns_none(tmp_path: pathlib.Path) -> None:
    spawn_dir = tmp_path / "speaking-spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []
    comments = _Comments()

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_speaking_agent(
        _issue(700),
        "art:code",
        "jcronq/alice",
        design_note_path=tmp_path / "fake-design.md",
        spawn_dir=spawn_dir,
        python_bin="/usr/bin/python3",
        post_comment=comments,
        popen=popen,
        now_iso=lambda: "2026-11-07T00:00:00Z",
        log=lambda _m: None,
        rate_gate=_defer_gate,
    )

    assert result is None
    assert popen_calls == []
    assert comments.calls == []


# ---------------------------------------------------------------------------
# spawn_rebase_agent
# ---------------------------------------------------------------------------


def test_spawn_rebase_agent_defer_returns_none(tmp_path: pathlib.Path) -> None:
    spawn_dir = tmp_path / "spawns"
    spawn_dir.mkdir()
    popen_calls: list[_Popen] = []

    def popen(*args: Any, **kwargs: Any) -> _Popen:
        p = _Popen(*args, **kwargs)
        popen_calls.append(p)
        return p

    result = spawn_mod.spawn_rebase_agent(
        _issue(900),
        "jcronq/alice",
        "feat/branch",
        "needs rebase",
        spawn_dir=spawn_dir,
        claude_bin="/usr/bin/claude",
        popen=popen,
        log=lambda _m: None,
        rate_gate=_defer_gate,
    )

    assert result is None
    assert popen_calls == []


# ---------------------------------------------------------------------------
# rate_limit_gate helper — verifies the real wiring
# ---------------------------------------------------------------------------


def test_rate_limit_gate_reads_backoff_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate helper consults the backoff file via ``should_defer``."""
    backoff_path = tmp_path / "backoff.json"
    monkeypatch.setenv("ALICE_GH_RATE_LIMIT_BACKOFF_PATH", str(backoff_path))

    # No file yet → fail-open.
    decision = spawn_mod.rate_limit_gate("sm-code-worker", log=lambda _m: None)
    assert decision.defer is False

    # Record a hit → gate defers.
    rl.record_rate_limit_hit(reason="forced", jitter=lambda lo, hi: 0.0)
    decision = spawn_mod.rate_limit_gate("sm-code-worker", log=lambda _m: None)
    assert decision.defer is True


def test_rate_limit_gate_log_includes_worker_kind(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backoff_path = tmp_path / "backoff.json"
    monkeypatch.setenv("ALICE_GH_RATE_LIMIT_BACKOFF_PATH", str(backoff_path))
    rl.record_rate_limit_hit(reason="forced", jitter=lambda lo, hi: 0.0)
    log_lines: list[str] = []

    spawn_mod.rate_limit_gate("sm-thinking-agent", log=log_lines.append)

    assert any("sm-thinking-agent" in line for line in log_lines)
    assert any("rate-limit gate deferred" in line for line in log_lines)
