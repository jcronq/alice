"""Tests for the PreToolUse-hook Task interception that lives on
``SpeakingDaemon._pretooluse_hook``.

We don't boot a full daemon — instead we exercise the method
directly with stub state, since it's a pure function over
the SDK's PreToolUseHookInput shape plus a bound dispatcher.
"""

from __future__ import annotations

import asyncio

import pytest

from alice_speaking.daemon import SpeakingDaemon


# ---------------------------------------------------------------------------
# Test scaffolding


def _hook_input(
    tool_name: str,
    tool_input: dict,
    *,
    agent_id: str | None = None,
    tool_use_id: str = "toolu_test_001",
) -> dict:
    """Build a minimal PreToolUseHookInput dict matching what the
    SDK would pass to the callback."""
    out: dict = {
        "session_id": "sess-test",
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }
    if agent_id is not None:
        out["agent_id"] = agent_id
    return out


def _make_hook(*, raise_dispatch: bool = False):
    """Bind ``SpeakingDaemon._pretooluse_hook`` against a stub
    dispatcher so we can exercise it without booting a real daemon.

    Returns ``(hook_callable, dispatch_calls)`` where
    ``dispatch_calls`` is a list mutated by the stub on each
    invocation so tests can assert on (description, prompt) pairs.
    """
    dispatch_calls: list[tuple[str, str]] = []

    async def stub_dispatch(description: str, prompt: str) -> str:
        if raise_dispatch:
            raise RuntimeError("simulated kernel boom")
        dispatch_calls.append((description, prompt))
        return f"bg-stub-{len(dispatch_calls):03d}"

    class _StubDaemon:
        _dispatch_subagent = staticmethod(stub_dispatch)

    stub = _StubDaemon()
    bound = SpeakingDaemon._pretooluse_hook.__get__(stub, _StubDaemon)
    return bound, dispatch_calls


# ---------------------------------------------------------------------------
# Pass-through paths — non-Task tools, sub-agent context, malformed input


def test_non_task_tool_passes_through() -> None:
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(_hook_input("Bash", {"command": "ls"}), "toolu_1", None)
    )
    assert result == {}
    assert calls == []


def test_mcp_tool_passes_through() -> None:
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input(
                "mcp__alice__send_message",
                {"recipient": "self", "message": "hi"},
            ),
            "toolu_1",
            None,
        )
    )
    assert result == {}
    assert calls == []


def test_subagent_context_passes_through() -> None:
    """Recursive Task call from inside a sub-agent: never intercept."""
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input(
                "Task",
                {"description": "x", "prompt": "y"},
                agent_id="sub-1",
            ),
            "toolu_1",
            None,
        )
    )
    assert result == {}
    assert calls == []


def test_empty_prompt_passes_through() -> None:
    """Malformed Task call: pass through, let SDK surface the error."""
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input("Task", {"description": "x", "prompt": ""}),
            "toolu_1",
            None,
        )
    )
    assert result == {}
    assert calls == []


# ---------------------------------------------------------------------------
# Interception path — Task / Agent names, dispatcher invocation, deny shape


@pytest.mark.parametrize("tool_name", ["Task", "Agent"])
def test_task_name_aliases_both_intercept(tool_name: str) -> None:
    """The SDK's capability list calls it 'Task' but tool_use events
    sometimes show 'Agent'. The hook matcher uses ``Task|Agent``."""
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input(
                tool_name,
                {"description": "research X", "prompt": "find papers"},
            ),
            "toolu_1",
            None,
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == [("research X", "find papers")]


def test_intercept_returns_handle_in_deny_reason() -> None:
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input(
                "Task",
                {"description": "deploy verify", "prompt": "ssh strix"},
            ),
            "toolu_1",
            None,
        )
    )
    out = result["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    # Handle must appear so Alice can correlate.
    assert "bg-stub-001" in reason
    # Description must appear so her wrap-up text has context.
    assert "deploy verify" in reason


def test_intercept_default_description_when_missing() -> None:
    """description is informational; if Alice omits it the call
    should still dispatch."""
    hook, calls = _make_hook()
    result = asyncio.run(
        hook(
            _hook_input("Task", {"prompt": "do the thing"}),
            "toolu_1",
            None,
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == [("background task", "do the thing")]


def test_intercept_strips_whitespace_from_input() -> None:
    hook, calls = _make_hook()
    asyncio.run(
        hook(
            _hook_input(
                "Task",
                {"description": "  x  ", "prompt": "\n  y\n"},
            ),
            "toolu_1",
            None,
        )
    )
    assert calls == [("x", "y")]


def test_dispatch_failure_returns_deny_with_clear_message() -> None:
    """If we can't dispatch (kernel boom etc.), DENY with a clear
    message — passing through would defeat the whole interception."""
    hook, _ = _make_hook(raise_dispatch=True)
    result = asyncio.run(
        hook(
            _hook_input("Task", {"description": "x", "prompt": "y"}),
            "toolu_1",
            None,
        )
    )
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "failed to dispatch" in reason.lower()
    assert "RuntimeError" in reason
