"""Unit tests for AgentKernel.

Uses a CapturingEmitter + a fake SDK query() to exercise the kernel's
block dispatch, event emission, handler fan-out, timeout, and error
paths — without hitting the real Claude subprocess.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from alice_core.events import CapturingEmitter
from alice_core.kernel import AgentKernel, KernelSpec, NullHandler


# ---------------------------------------------------------------------------
# Fake SDK types — mirror what the real SDK hands back so isinstance checks
# in the kernel still succeed. We monkeypatch the SDK names the kernel
# imports so our fakes flow through.


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    name: str
    input: Any
    id: str


@dataclass
class _FakeThinkingBlock:
    thinking: str


@dataclass
class _FakeAssistantMessage:
    content: list
    error: Any = None


@dataclass
class _FakeUserMessage:
    content: Any


@dataclass
class _FakeResultMessage:
    session_id: str | None = None
    is_error: bool = False
    usage: dict | None = None
    duration_ms: int | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None
    result: Any = None


@dataclass
class _FakeSystemMessage:
    subtype: str = "init"
    data: dict | None = None


@pytest.fixture
def patched_sdk(monkeypatch):
    """Replace kernel's SDK imports with our fakes."""
    import alice_core.kernel as k

    monkeypatch.setattr(k, "AssistantMessage", _FakeAssistantMessage)
    monkeypatch.setattr(k, "UserMessage", _FakeUserMessage)
    monkeypatch.setattr(k, "ResultMessage", _FakeResultMessage)
    monkeypatch.setattr(k, "SystemMessage", _FakeSystemMessage)
    monkeypatch.setattr(k, "TextBlock", _FakeTextBlock)
    monkeypatch.setattr(k, "ToolUseBlock", _FakeToolUseBlock)
    monkeypatch.setattr(k, "ThinkingBlock", _FakeThinkingBlock)
    monkeypatch.setattr(k, "ClaudeAgentOptions", lambda **kw: SimpleNamespace(**kw))


def _install_fake_query(monkeypatch, messages):
    """Install a fake query() that yields the given messages in order."""
    import alice_core.kernel as k

    async def fake_query(*, prompt, options):
        for msg in messages:
            yield msg

    monkeypatch.setattr(k, "query", fake_query)


# ---------------------------------------------------------------------------
# Tests


@pytest.mark.asyncio
async def test_kernel_emits_events_for_text_tool_and_result(patched_sdk, monkeypatch):
    messages = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
        _FakeAssistantMessage(
            content=[_FakeToolUseBlock(name="Bash", input={"command": "ls"}, id="t1")]
        ),
        _FakeUserMessage(content="tool result text"),
        _FakeResultMessage(
            session_id="sess-1", duration_ms=42, total_cost_usd=0.01, num_turns=1
        ),
    ]
    _install_fake_query(monkeypatch, messages)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap, correlation_id="c-1")
    result = await kernel.run(
        "hello", KernelSpec(model="m", allowed_tools=["Bash"], max_seconds=0)
    )

    assert result.text == "hi"
    assert result.session_id == "sess-1"
    assert result.cost_usd == 0.01
    assert result.is_error is False

    kinds = [e["event"] for e in cap.events]
    assert "assistant_text" in kinds
    assert "tool_use" in kinds
    assert "user_message" in kinds
    assert "result" in kinds

    text_ev = cap.of_kind("assistant_text")[0]
    assert text_ev["text"] == "hi"
    assert text_ev["turn_id"] == "c-1"

    tool_ev = cap.of_kind("tool_use")[0]
    assert tool_ev["name"] == "Bash"
    assert tool_ev["id"] == "t1"


@pytest.mark.asyncio
async def test_kernel_fires_handlers_for_each_block(patched_sdk, monkeypatch):
    messages = [
        _FakeAssistantMessage(
            content=[
                _FakeThinkingBlock(thinking="reasoning..."),
                _FakeTextBlock(text="final"),
                _FakeToolUseBlock(name="Read", input={"file_path": "/x"}, id="t2"),
            ]
        ),
        _FakeResultMessage(session_id="sess-2"),
    ]
    _install_fake_query(monkeypatch, messages)

    seen: dict[str, list] = {"text": [], "tool": [], "thinking": [], "result": []}

    class SpyHandler(NullHandler):
        async def on_text(self, text): seen["text"].append(text)
        async def on_tool_use(self, name, input, id): seen["tool"].append(name)
        async def on_thinking(self, text): seen["thinking"].append(text)
        async def on_result(self, msg): seen["result"].append(msg.session_id)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap)
    await kernel.run("x", KernelSpec(model="m"), handlers=[SpyHandler()])

    assert seen["text"] == ["final"]
    assert seen["tool"] == ["Read"]
    assert seen["thinking"] == ["reasoning..."]
    assert seen["result"] == ["sess-2"]


@pytest.mark.asyncio
async def test_kernel_silent_suppresses_emission_but_fires_handlers(
    patched_sdk, monkeypatch
):
    messages = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="silent text")]),
        _FakeResultMessage(session_id="s"),
    ]
    _install_fake_query(monkeypatch, messages)

    fired: list[str] = []

    class H(NullHandler):
        async def on_text(self, text): fired.append(text)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap, correlation_id="c", silent=True)
    await kernel.run("x", KernelSpec(model="m"), handlers=[H()])

    assert fired == ["silent text"]  # handlers still run
    assert cap.events == []  # no events emitted


@pytest.mark.asyncio
async def test_kernel_timeout_returns_error_result(patched_sdk, monkeypatch):
    async def slow_query(*, prompt, options):
        await asyncio.sleep(10)
        # never yields
        if False:
            yield None

    import alice_core.kernel as k
    monkeypatch.setattr(k, "query", slow_query)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap, correlation_id="c")
    # 0.05s timeout — cancels the sleep(10) above
    result = await kernel.run("x", KernelSpec(model="m", max_seconds=1))

    # max_seconds=1 hits real timer. Use a smaller fraction by patching
    # asyncio.timeout instead? Simpler: use max_seconds=1 and confirm error
    # semantics — test suite already runs in <1s per test, so this adds ~1s.
    # For speed, we could pass a fractional spec but the kernel expects int.
    assert result.is_error is True
    assert result.error == "timeout"
    assert cap.of_kind("timeout"), "kernel should emit a timeout event"


@pytest.mark.asyncio
async def test_kernel_raises_on_rate_limit(patched_sdk, monkeypatch):
    messages = [_FakeAssistantMessage(content=[], error="rate_limit")]
    _install_fake_query(monkeypatch, messages)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap)
    with pytest.raises(RuntimeError, match="rate_limit"):
        await kernel.run("x", KernelSpec(model="m"))


@pytest.mark.asyncio
async def test_kernel_raises_on_result_is_error(patched_sdk, monkeypatch):
    messages = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="partial")]),
        _FakeResultMessage(is_error=True, result="claude failed"),
    ]
    _install_fake_query(monkeypatch, messages)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap)
    with pytest.raises(RuntimeError, match="claude result error"):
        await kernel.run("x", KernelSpec(model="m"))


@pytest.mark.asyncio
async def test_kernel_resume_passes_through_to_options(patched_sdk, monkeypatch):
    captured: dict[str, Any] = {}

    async def capturing_query(*, prompt, options):
        captured["options"] = options
        yield _FakeResultMessage(session_id="s")

    import alice_core.kernel as k
    monkeypatch.setattr(k, "query", capturing_query)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap)
    await kernel.run(
        "prompt",
        KernelSpec(model="m", resume="prev-session-id", mcp_servers={"x": 1}),
    )

    opts = captured["options"]
    assert opts.resume == "prev-session-id"
    assert opts.mcp_servers == {"x": 1}
    assert opts.model == "m"


@pytest.mark.asyncio
async def test_kernel_without_correlation_id_omits_turn_id(patched_sdk, monkeypatch):
    messages = [
        _FakeAssistantMessage(content=[_FakeTextBlock(text="hi")]),
        _FakeResultMessage(session_id="s"),
    ]
    _install_fake_query(monkeypatch, messages)

    cap = CapturingEmitter()
    kernel = AgentKernel(cap)  # no correlation_id
    await kernel.run("x", KernelSpec(model="m"))

    for ev in cap.events:
        assert "turn_id" not in ev, f"event {ev} should not carry a turn_id"
