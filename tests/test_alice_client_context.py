"""End-to-end tests for the ``alice-client --context`` subcommand.

The script lives in ``bin/alice-client`` (no ``.py`` extension), so we
load it as a module from disk and drive ``main()`` directly while
running a real :class:`CLITransport` on a tempdir socket. This catches
argparse + IO-handling regressions in the CLI wrapper without needing
docker / the worker container.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import threading
from typing import Optional

import pytest

from alice_speaking.diagnostics import ContextProbe
from alice_speaking.transports.cli import CLITransport


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENT_PATH = REPO_ROOT / "bin" / "alice-client"


@pytest.fixture(scope="module")
def alice_client_module():
    """Import bin/alice-client (no .py extension) as a module."""
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("alice_client_under_test", str(CLIENT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _make_probe(**overrides) -> ContextProbe:
    defaults = {
        "get_system_prompt": lambda: "you are alice.",
        "get_builtin_tools": lambda: ["Bash", "Read"],
        "get_custom_tool_names": lambda: [
            "mcp__alice__send_message",
            "mcp__memory__search",
        ],
        "get_mcp_servers": lambda: {
            "alice": {"type": "stdio"},
            "memory": {"type": "http"},
        },
        "get_session_id": lambda: "sess-cli",
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


def test_human_mode_prints_readable_summary(
    tmp_path, loop_thread, alice_client_module, capsys
):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))
    try:
        rc = alice_client_module.main(["--context", "--socket", str(sock_path)])
    finally:
        _run(loop_thread, transport.stop())
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice context snapshot" in out
    assert "claude-sonnet-4-5" in out
    assert "sess-cli" in out
    assert "system prompt: 14 chars" in out
    assert "tools:        4 total (2 builtin + 2 custom)" in out
    # MCP server breakdown
    assert "alice (stdio): 1 tools" in out
    assert "memory (http): 1 tools" in out


def test_json_mode_emits_raw_events(
    tmp_path, loop_thread, alice_client_module, capsys
):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))
    try:
        rc = alice_client_module.main(
            ["--context", "--json", "--socket", str(sock_path)]
        )
    finally:
        _run(loop_thread, transport.stop())
    assert rc == 0
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    import json

    events = [json.loads(line) for line in lines]
    types = [e["type"] for e in events]
    assert "ack" in types
    assert "context_snapshot" in types
    assert "done" in types
    snap = next(e for e in events if e["type"] == "context_snapshot")["data"]
    assert snap["session_id"] == "sess-cli"


def test_no_text_flag_omits_system_prompt_body(
    tmp_path, loop_thread, alice_client_module, capsys
):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, _make_probe()))
    try:
        rc = alice_client_module.main(
            [
                "--context",
                "--no-text",
                "--json",
                "--socket",
                str(sock_path),
            ]
        )
    finally:
        _run(loop_thread, transport.stop())
    assert rc == 0
    import json

    snap = None
    for line in capsys.readouterr().out.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "context_snapshot":
            snap = event["data"]
    assert snap is not None
    assert snap["system_prompt"]["text"] is None
    # Sizes still come through.
    assert snap["system_prompt"]["chars"] == len("you are alice.")


def test_probe_unavailable_returns_exit_code_2(
    tmp_path, loop_thread, alice_client_module, capsys
):
    sock_path = tmp_path / "alice.sock"
    transport = _run(loop_thread, _start_transport(sock_path, None))
    try:
        rc = alice_client_module.main(
            ["--context", "--socket", str(sock_path)]
        )
    finally:
        _run(loop_thread, transport.stop())
    assert rc == 2
    err = capsys.readouterr().err
    assert "probe unavailable" in err
