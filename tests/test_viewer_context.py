"""Tests for the /context viewer route + the context_probe_client helpers.

Two layers:

- ``decompose`` is a pure function over a snapshot dict; tested
  directly with synthetic input.
- ``fetch_snapshot`` is exercised end-to-end against a real
  :class:`CLITransport` listening on a tempdir Unix socket — same
  pattern as ``tests/test_cli_context_request.py``.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import threading
from typing import Optional

import pytest

from alice_speaking.diagnostics import ContextProbe
from alice_speaking.transports.cli import CLITransport
from viewer import context_probe_client


# ----------------------------------------------------------------------------
# decompose() — pure function tests


def _example_snapshot() -> dict:
    return {
        "ts": 1000.0,
        "model": "claude-sonnet-4-5",
        "backend": "subscription",
        "session_id": "s-1",
        "system_prompt": {"chars": 14, "text": "you are alice."},
        "tools": {
            "builtin": ["Bash", "Read"],
            "custom": ["mcp__alice__send_message"],
            "count": 3,
        },
        "mcp_servers": {
            "alice": {
                "type": "stdio",
                "tool_count": 1,
                "tool_names": ["send_message"],
            },
        },
        "pending_preamble": None,
        "in_flight": None,
    }


def test_decompose_emits_one_component_per_category():
    out = context_probe_client.decompose(_example_snapshot())
    names = [c["name"] for c in out["components"]]
    assert "system prompt" in names
    assert "builtin tools" in names
    assert "custom tools" in names
    assert "mcp:alice" in names
    # No preamble in this snapshot
    assert "pending preamble" not in names


def test_decompose_total_matches_component_sum():
    out = context_probe_client.decompose(_example_snapshot())
    assert out["total_tokens"] == sum(c["tokens"] for c in out["components"])


def test_decompose_skips_zero_token_components():
    snap = _example_snapshot()
    snap["tools"]["custom"] = []
    out = context_probe_client.decompose(snap)
    names = [c["name"] for c in out["components"]]
    assert "custom tools" not in names


def test_decompose_includes_pending_preamble_when_present():
    snap = _example_snapshot()
    snap["pending_preamble"] = {"chars": 200, "text": "previous turns: ..."}
    out = context_probe_client.decompose(snap)
    names = [c["name"] for c in out["components"]]
    assert "pending preamble" in names


def test_decompose_falls_back_to_chars_when_text_omitted():
    """When the snapshot was fetched with --no-text the text fields are
    None; decompose() should still produce a (rougher) estimate."""
    snap = _example_snapshot()
    snap["system_prompt"] = {"chars": 400, "text": None}
    out = context_probe_client.decompose(snap)
    sp = next(c for c in out["components"] if c["name"] == "system prompt")
    # 400 chars / 4 ≈ 100 tokens. Allow a small range.
    assert 50 <= sp["tokens"] <= 200


# ----------------------------------------------------------------------------
# fetch_snapshot() — talks to a real CLITransport over a tempdir socket


def _make_probe(**overrides) -> ContextProbe:
    defaults = {
        "get_system_prompt": lambda: "you are alice.",
        "get_builtin_tools": lambda: ["Bash", "Read"],
        "get_custom_tool_names": lambda: ["mcp__alice__send_message"],
        "get_mcp_servers": lambda: {"alice": {"type": "stdio"}},
        "get_session_id": lambda: "sess-abc",
        "get_pending_preamble": lambda: None,
        "get_current_turn_kind": lambda: None,
        "get_model": lambda: "claude-sonnet-4-5",
        "get_backend": lambda: "subscription",
        "get_mind_dir": lambda: "/m",
        "get_skills_cwd": lambda: "/s",
    }
    defaults.update(overrides)
    return ContextProbe(**defaults)


@pytest.fixture
def loop_thread():
    """Background-thread event loop so the test can drive both the
    transport (server side) and ``fetch_snapshot`` (client side, also
    async) without nesting event loops."""
    loop = asyncio.new_event_loop()

    def _target():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)
    loop.close()


def _run(loop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5.0)


async def _start_transport(
    socket_path: pathlib.Path,
    probe: Optional[ContextProbe],
) -> CLITransport:
    transport = CLITransport(
        socket_path=socket_path,
        is_allowed=lambda uid: uid == str(os.getuid()),
        context_probe=probe,
    )
    await transport.start()
    return transport


def test_fetch_snapshot_returns_snapshot_payload(tmp_path, loop_thread):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))
    try:
        snapshot = asyncio.run(
            context_probe_client.fetch_snapshot(socket_path=str(sock_path))
        )
    finally:
        _run(loop_thread, transport.stop())
    assert snapshot["session_id"] == "sess-abc"
    assert snapshot["model"] == "claude-sonnet-4-5"
    assert snapshot["tools"]["count"] == 2 + 1


def test_fetch_snapshot_raises_filenotfound_when_socket_missing(tmp_path):
    """No socket file on disk → FileNotFoundError so the route returns
    a friendly 'worker not up' status."""
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            context_probe_client.fetch_snapshot(
                socket_path=str(tmp_path / "no-such.sock")
            )
        )


def test_fetch_snapshot_raises_runtime_when_probe_unwired(tmp_path, loop_thread):
    """The transport replies with ``error`` followed by ``done`` when
    no probe is attached. fetch_snapshot turns that into RuntimeError."""
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, None))
    try:
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(
                context_probe_client.fetch_snapshot(socket_path=str(sock_path))
            )
    finally:
        _run(loop_thread, transport.stop())
    assert "probe unavailable" in str(exc_info.value)


def test_fetch_snapshot_honors_env_var_socket(tmp_path, loop_thread, monkeypatch):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))
    try:
        monkeypatch.setenv("ALICE_CLI_SOCKET", str(sock_path))
        snapshot = asyncio.run(context_probe_client.fetch_snapshot())
    finally:
        _run(loop_thread, transport.stop())
    assert snapshot["session_id"] == "sess-abc"


# ----------------------------------------------------------------------------
# /context route — page render only (the JSON API exercised separately)


def test_context_page_renders(tmp_path, monkeypatch):
    """The page itself shouldn't depend on the worker being up — it
    just renders chrome and lets the JS fetch /api/context."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from viewer.main import create_app
    from viewer.settings import Paths

    # Minimal Paths fixture pointing at a tempdir; the route doesn't
    # actually touch any of these files unless _state_context() does.
    mind = tmp_path / "mind"
    (mind / "inner" / "state").mkdir(parents=True)
    (mind / "memory").mkdir()
    (mind / "memory" / "events.jsonl").write_text("")
    (mind / "inner" / "directive.md").write_text("")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (mind / "inner" / "state").mkdir(parents=True, exist_ok=True)
    paths = Paths(
        mind_dir=mind,
        state_dir=state_dir,
        thinking_log=mind / "memory" / "events.jsonl",
        speaking_log=mind / "memory" / "events.jsonl",
        turn_log=mind / "inner" / "state" / "speaking-turns.jsonl",
    )
    app = create_app(paths=paths)
    client = TestClient(app)

    response = client.get("/context")
    assert response.status_code == 200
    assert "context · live snapshot" in response.text
    assert "/api/context" in response.text
    # Block-grid renderer needs the page-data JSON tag with the model
    # context window so it knows how many cells to allocate.
    assert "context-page-data" in response.text
    assert "context_window" in response.text
