"""Tests for alice_speaking.tools.deploy.

The deploy tool writes a sentinel file that the alice-reload-watcher s6
service picks up via inotify. Tests cover:

- the tool's input/output contract (async, returns dict[str, Any] per the
  fs.py pattern)
- sentinel JSON shape (type, reason, ts, git_head)
- expected-head file written for post-restart verification
- _git_head fallback to "unknown" when git is unavailable

The watcher itself is a bash script and isn't unit-tested here; the
restart loop is end-to-end behaviour that requires a live s6 environment.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_speaking.tools import deploy


@pytest.mark.asyncio
async def test_request_worker_reload_writes_sentinel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    sentinel = tmp_path / "reload-requested"
    expected_head = tmp_path / "reload-expected-head"
    monkeypatch.setattr(deploy, "_SENTINEL_PATH", sentinel)
    monkeypatch.setattr(deploy, "_EXPECTED_HEAD_PATH", expected_head)
    monkeypatch.setattr(deploy, "_git_head", lambda: "abc1234")

    tools = deploy.build(cfg=None)  # type: ignore[arg-type]
    assert len(tools) == 1
    handler = tools[0].handler  # type: ignore[attr-defined]

    result = await handler({"reason": "test reload"})

    assert isinstance(result, dict)
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "abc1234" in result["content"][0]["text"]
    assert result.get("isError") is not True

    assert sentinel.exists()
    payload = json.loads(sentinel.read_text())
    assert payload["type"] == "hot"
    assert payload["reason"] == "test reload"
    assert payload["git_head"] == "abc1234"
    assert "ts" in payload  # ISO timestamp

    assert expected_head.exists()
    assert expected_head.read_text() == "abc1234"


@pytest.mark.asyncio
async def test_request_worker_reload_no_reason_default(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    sentinel = tmp_path / "reload-requested"
    expected_head = tmp_path / "reload-expected-head"
    monkeypatch.setattr(deploy, "_SENTINEL_PATH", sentinel)
    monkeypatch.setattr(deploy, "_EXPECTED_HEAD_PATH", expected_head)
    monkeypatch.setattr(deploy, "_git_head", lambda: "deadbee")

    tools = deploy.build(cfg=None)  # type: ignore[arg-type]
    handler = tools[0].handler  # type: ignore[attr-defined]

    # Empty args dict: reason should default to ""
    result = await handler({})

    payload = json.loads(sentinel.read_text())
    assert payload["reason"] == ""
    # No "Reason:" suffix when reason is empty
    assert "Reason:" not in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_request_worker_reload_sentinel_write_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """If the sentinel can't be written, return isError=True. Expected-head
    write failure is non-fatal (best-effort) so we don't test that branch
    as an error — tested separately."""
    # Point at a path that can't be written (a directory)
    sentinel = tmp_path / "blocking-dir"
    sentinel.mkdir()
    expected_head = tmp_path / "reload-expected-head"
    monkeypatch.setattr(deploy, "_SENTINEL_PATH", sentinel)
    monkeypatch.setattr(deploy, "_EXPECTED_HEAD_PATH", expected_head)
    monkeypatch.setattr(deploy, "_git_head", lambda: "abc")

    tools = deploy.build(cfg=None)  # type: ignore[arg-type]
    handler = tools[0].handler  # type: ignore[attr-defined]

    result = await handler({"reason": "x"})
    assert result.get("isError") is True
    assert "sentinel write failed" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_request_worker_reload_expected_head_write_is_best_effort(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Expected-head write failure does NOT block the sentinel write. The
    sentinel is the load-bearing artifact; expected-head is post-restart
    verification convenience."""
    sentinel = tmp_path / "reload-requested"
    # expected_head is "in" a path that has a regular file as parent
    blocked = tmp_path / "regular-file"
    blocked.write_text("x")
    expected_head = blocked / "reload-expected-head"  # parent is a file, not dir
    monkeypatch.setattr(deploy, "_SENTINEL_PATH", sentinel)
    monkeypatch.setattr(deploy, "_EXPECTED_HEAD_PATH", expected_head)
    monkeypatch.setattr(deploy, "_git_head", lambda: "abc")

    tools = deploy.build(cfg=None)  # type: ignore[arg-type]
    handler = tools[0].handler  # type: ignore[attr-defined]

    result = await handler({"reason": "x"})
    # Sentinel still got written, no error
    assert result.get("isError") is not True
    assert sentinel.exists()


def test_git_head_returns_unknown_on_missing_repo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(deploy, "_REPO_PATH", "/nonexistent/repo")
    head = deploy._git_head()
    assert head == "unknown"


def test_git_head_returns_short_hash_on_real_repo(tmp_path: pathlib.Path):
    """Best-effort smoke test: in CI we run from a real repo; verify the
    fallback hash format (7 hex chars) when subprocess succeeds."""
    head = deploy._git_head()
    # Either a 7-12 char hex string (real git output) or "unknown" fallback
    assert head == "unknown" or all(c in "0123456789abcdef" for c in head)
