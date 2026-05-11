"""Tests for the SDK-Task interception that lives on
``SpeakingDaemon._intercept_task``.

We don't boot a full daemon — instead we exercise the method
directly with stub state, since it's a pure function over
``(tool_name, tool_input, context)`` plus a bound dispatcher.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from alice_speaking.daemon import SpeakingDaemon


# ---------------------------------------------------------------------------
# Test scaffolding


@dataclass
class _FakeContext:
    """Minimal stand-in for the SDK's ToolPermissionContext.

    The interception code only reads ``agent_id`` from the context
    (to skip interception inside sub-agent recursion). Everything
    else can be omitted.
    """
    agent_id: Optional[str] = None


def _make_intercepter(*, raise_dispatch: bool = False):
    """Bind ``SpeakingDaemon._intercept_task`` against a stub
    dispatcher so we can exercise it without booting a real daemon.

    Returns ``(intercept_callable, dispatch_calls)`` where
    ``dispatch_calls`` is a list mutated by the stub on each invocation
    so tests can assert on (description, prompt) pairs.
    """
    dispatch_calls: list[tuple[str, str]] = []

    async def stub_dispatch(description: str, prompt: str) -> str:
        if raise_dispatch:
            raise RuntimeError("simulated kernel boom")
        dispatch_calls.append((description, prompt))
        return f"bg-stub-{len(dispatch_calls):03d}"

    # SimpleNamespace-y bind: build a thin object with the two
    # attributes _intercept_task reads, plus the _dispatch_subagent
    # method. The real interceptor is an instance method, so we bind
    # it via ``__get__``.
    class _StubDaemon:
        _dispatch_subagent = staticmethod(stub_dispatch)

    stub = _StubDaemon()
    bound = SpeakingDaemon._intercept_task.__get__(stub, _StubDaemon)
    return bound, dispatch_calls


# ---------------------------------------------------------------------------
# Pass-through paths — non-Task tools, sub-agent context, malformed input


def test_non_task_tool_passes_through() -> None:
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Bash",
            {"command": "ls"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)
    assert calls == []


def test_unknown_tool_passes_through() -> None:
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "mcp__alice__send_message",
            {"recipient": "self", "message": "hi"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)
    assert calls == []


def test_subagent_context_passes_through() -> None:
    """Recursive Task call from inside a sub-agent: never intercept.
    Defensive — sub-agents only get BUILTIN_TOOLS without Task
    today, but the guard keeps us safe if that ever changes."""
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Task",
            {"description": "x", "prompt": "y"},
            _FakeContext(agent_id="sub-1"),
        )
    )
    assert isinstance(result, PermissionResultAllow)
    assert calls == []


def test_empty_prompt_passes_through() -> None:
    """If the model issues a malformed Task call (no prompt), let
    the SDK handle it. A clearer model-side error is better than us
    silently dispatching nothing."""
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Task",
            {"description": "x", "prompt": ""},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultAllow)
    assert calls == []


# ---------------------------------------------------------------------------
# Interception path — Task / Agent names, dispatcher invocation, deny shape


@pytest.mark.parametrize("tool_name", ["Task", "Agent"])
def test_task_name_aliases_both_intercept(tool_name: str) -> None:
    """The SDK's capability list calls it 'Task' but tool_use events
    sometimes show 'Agent'. Match both."""
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            tool_name,
            {"description": "research X", "prompt": "find papers"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert calls == [("research X", "find papers")]


def test_intercept_returns_handle_in_deny_message() -> None:
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Task",
            {"description": "deploy verify", "prompt": "ssh strix"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    # Handle must appear in the deny message so Alice can correlate.
    assert "bg-stub-001" in result.message
    # Description must appear so Alice's wrap-up text has context.
    assert "deploy verify" in result.message


def test_intercept_uses_interrupt_false() -> None:
    """interrupt=True would cancel the entire turn, dropping any
    wrap-up text Alice was about to emit. Must be False."""
    intercept, _ = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Task",
            {"description": "x", "prompt": "y"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_intercept_default_description_when_missing() -> None:
    """description is informational; if Alice omits it the call
    should still dispatch."""
    intercept, calls = _make_intercepter()
    result = asyncio.run(
        intercept(
            "Task",
            {"prompt": "do the thing"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert calls == [("background task", "do the thing")]


def test_intercept_strips_whitespace_from_input() -> None:
    intercept, calls = _make_intercepter()
    asyncio.run(
        intercept(
            "Task",
            {"description": "  x  ", "prompt": "\n  y\n"},
            _FakeContext(),
        )
    )
    assert calls == [("x", "y")]


def test_dispatch_failure_returns_deny_not_allow() -> None:
    """If we can't dispatch (kernel construction failed, registry
    full, etc.), DENY with a clear message — passing through to
    the blocking built-in would defeat the whole interception."""
    intercept, _ = _make_intercepter(raise_dispatch=True)
    result = asyncio.run(
        intercept(
            "Task",
            {"description": "x", "prompt": "y"},
            _FakeContext(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert "failed to dispatch" in result.message.lower()
    assert "RuntimeError" in result.message
    assert result.interrupt is False
