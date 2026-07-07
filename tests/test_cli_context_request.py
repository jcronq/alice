"""End-to-end tests for the CLI socket's ``{"type": "context"}`` RPC.

These tests spin up a real :class:`CLITransport` listening on a
tempdir-scoped Unix socket and connect to it as a client, exercising
the full wire protocol (ack → context_snapshot → done) with and
without a probe wired in.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import threading
from typing import Optional

import pytest

from alice_speaking.diagnostics import ContextProbe
from alice_speaking.transports.cli import CLITransport


def _make_probe(**overrides) -> ContextProbe:
    defaults = {
        "get_system_prompt": lambda: "you are alice.",
        "get_builtin_tools": lambda: ["Bash", "Read"],
        "get_custom_tool_names": lambda: ["mcp__alice__send_message"],
        "get_mcp_servers": lambda: {"alice": {"type": "stdio"}},
        "get_session_id": lambda: "sess-xyz",
        "get_pending_preamble": lambda: None,
        "get_current_turn_kind": lambda: None,
        "get_model": lambda: "claude-sonnet-5",
        "get_backend": lambda: "subscription",
        "get_mind_dir": lambda: "/m",
        "get_skills_cwd": lambda: "/s",
    }
    defaults.update(overrides)
    return ContextProbe(**defaults)


def _read_events_until_done(
    sock: socket.socket, timeout: float = 2.0
) -> list[dict]:
    """Drain JSON events from a connected client socket until ``done``
    or ``error`` arrives. Each event is one line."""
    sock.settimeout(timeout)
    buf = b""
    events: list[dict] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            events.append(event)
            if event.get("type") in ("done", "error"):
                # If the last event is "error" the server sends a "done"
                # right after — keep draining one more line.
                if event["type"] == "error":
                    continue
                return events
    return events


async def _start_transport(
    socket_path: pathlib.Path,
    probe: Optional[ContextProbe],
) -> CLITransport:
    """Construct + start a CLITransport that accepts the test process's
    own uid only (matches the production default)."""
    transport = CLITransport(
        socket_path=socket_path,
        is_allowed=lambda uid: uid == str(os.getuid()),
        context_probe=probe,
    )
    await transport.start()
    return transport


def _client_connect(socket_path: pathlib.Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    return sock


def _run_with_loop(coro):
    """Drive a CLITransport coroutine on a private event loop in a
    background thread, so tests can use blocking sockets in the main
    thread without fighting asyncio for stdin/stdout time."""
    loop = asyncio.new_event_loop()

    def _target():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return loop, t, fut


@pytest.fixture
def loop_thread():
    """Background-thread event loop, cleaned up at test end."""
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


def test_context_request_returns_snapshot(tmp_path, loop_thread):
    sock_path = tmp_path / "alice.sock"
    probe = _make_probe()
    transport = _run(loop_thread, _start_transport(sock_path, probe))

    try:
        client = _client_connect(sock_path)
        try:
            client.sendall(b'{"type": "context"}\n')
            events = _read_events_until_done(client)
        finally:
            client.close()
    finally:
        _run(loop_thread, transport.stop())

    types = [e["type"] for e in events]
    assert "ack" in types
    assert "context_snapshot" in types
    assert "done" in types
    snap = next(e for e in events if e["type"] == "context_snapshot")
    assert snap["data"]["session_id"] == "sess-xyz"
    assert snap["data"]["model"] == "claude-sonnet-5"
    assert snap["data"]["tools"]["count"] == 2 + 1
    assert snap["data"]["system_prompt"]["text"] == "you are alice."


def test_context_request_honors_include_text_false(tmp_path, loop_thread):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))

    try:
        client = _client_connect(sock_path)
        try:
            client.sendall(b'{"type": "context", "include_text": false}\n')
            events = _read_events_until_done(client)
        finally:
            client.close()
    finally:
        _run(loop_thread, transport.stop())

    snap = next(e for e in events if e["type"] == "context_snapshot")
    assert snap["data"]["system_prompt"]["text"] is None
    assert snap["data"]["system_prompt"]["chars"] == len("you are alice.")


def test_context_request_errors_when_probe_missing(tmp_path, loop_thread):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, None))

    try:
        client = _client_connect(sock_path)
        try:
            client.sendall(b'{"type": "context"}\n')
            events = _read_events_until_done(client)
        finally:
            client.close()
    finally:
        _run(loop_thread, transport.stop())

    types = [e["type"] for e in events]
    assert "ack" in types
    assert "context_snapshot" not in types
    err = next(e for e in events if e["type"] == "error")
    assert "probe unavailable" in err["message"]
    assert "done" in types


def test_context_request_can_be_followed_by_more_requests(
    tmp_path, loop_thread
):
    """The connection must stay open after a context reply — interactive
    mode relies on that. Send two contexts back-to-back over one socket."""
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))

    try:
        client = _client_connect(sock_path)
        try:
            client.sendall(b'{"type": "context"}\n')
            first = _read_events_until_done(client)
            client.sendall(b'{"type": "context"}\n')
            second = _read_events_until_done(client)
        finally:
            client.close()
    finally:
        _run(loop_thread, transport.stop())

    assert any(e["type"] == "context_snapshot" for e in first)
    assert any(e["type"] == "context_snapshot" for e in second)


def test_context_probe_attribute_is_settable_post_construction(tmp_path):
    """Daemon wires the probe after CLITransport is built (ordering
    constraint). Confirm the attribute is a normal public field."""
    transport = CLITransport(
        socket_path=tmp_path / "x.sock",
        is_allowed=lambda uid: True,
    )
    assert transport.context_probe is None
    transport.context_probe = _make_probe()
    assert transport.context_probe is not None
