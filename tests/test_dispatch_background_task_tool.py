"""Tests for the ``dispatch_background_task`` MCP tool builder.

Stubs the dispatcher and text-sender so no real sub-agent is
spawned. The daemon-side wiring (registry, kernel construction,
drain handling) is covered separately by
``test_background_task_completion.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from alice_speaking.tools.background_task import build


def _stub_cfg():
    """The tool builder accepts a Config but doesn't read from it.
    A bare object satisfies the type without dragging in the full
    config-load machinery for unit tests."""
    return object()


# ---------------------------------------------------------------------------
# build() shape


def test_build_returns_one_tool() -> None:
    async def dispatcher(desc: str, instr: str) -> str:
        return "bg-stub"

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    assert len(tools) == 1
    assert tools[0].name == "dispatch_background_task"


def test_build_accepts_no_text_sender() -> None:
    async def dispatcher(desc: str, instr: str) -> str:
        return "bg-stub"

    # text_sender is optional — must not raise.
    tools = build(_stub_cfg(), dispatcher=dispatcher, text_sender=None)
    assert tools


# ---------------------------------------------------------------------------
# Single dispatch path


def test_single_dispatch_calls_dispatcher_and_returns_handle() -> None:
    seen: list[tuple[str, str]] = []

    async def dispatcher(desc: str, instr: str) -> str:
        seen.append((desc, instr))
        return "bg-abc123"

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    fn = tools[0].handler

    result = asyncio.run(
        fn(
            {
                "description": "research X",
                "instructions": "find recent papers about X",
            }
        )
    )

    assert seen == [("research X", "find recent papers about X")]
    assert result["status"] == "dispatched"
    assert result["id"] == "bg-abc123"
    assert result["task_count"] == 1
    # Tool-result content is what the LLM actually sees.
    assert "bg-abc123" in result["content"][0]["text"]


def test_single_dispatch_missing_args_returns_error() -> None:
    async def dispatcher(desc: str, instr: str) -> str:
        pytest.fail("dispatcher should not be called with missing args")

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    fn = tools[0].handler

    # Empty args.
    result = asyncio.run(fn({}))
    assert result.get("isError") is True

    # Missing instructions.
    result = asyncio.run(fn({"description": "x"}))
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# Fan-out (tasks: list)


def test_fanout_dispatches_each_task_in_order() -> None:
    seen: list[tuple[str, str]] = []
    counter = {"n": 0}

    async def dispatcher(desc: str, instr: str) -> str:
        seen.append((desc, instr))
        counter["n"] += 1
        return f"bg-{counter['n']}"

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    fn = tools[0].handler

    result = asyncio.run(
        fn(
            {
                "tasks": [
                    {"description": "A", "instructions": "do A"},
                    {"description": "B", "instructions": "do B"},
                    {"description": "C", "instructions": "do C"},
                ]
            }
        )
    )

    assert seen == [("A", "do A"), ("B", "do B"), ("C", "do C")]
    assert result["status"] == "dispatched"
    assert result["task_count"] == 3
    assert result["handles"] == ["bg-1", "bg-2", "bg-3"]


def test_fanout_skips_entries_missing_fields() -> None:
    seen: list[str] = []

    async def dispatcher(desc: str, instr: str) -> str:
        seen.append(desc)
        return f"bg-{desc}"

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    fn = tools[0].handler

    result = asyncio.run(
        fn(
            {
                "tasks": [
                    {"description": "A", "instructions": "do A"},
                    {"description": "B"},  # missing instructions
                    {"instructions": "do C"},  # missing description
                    {"description": "", "instructions": "blank desc"},  # blank
                    {"description": "D", "instructions": "do D"},
                ]
            }
        )
    )

    assert seen == ["A", "D"]
    assert result["task_count"] == 2


def test_fanout_with_all_invalid_returns_error() -> None:
    async def dispatcher(desc: str, instr: str) -> str:
        pytest.fail("dispatcher should not be called when all tasks invalid")

    tools = build(_stub_cfg(), dispatcher=dispatcher)
    fn = tools[0].handler

    result = asyncio.run(
        fn({"tasks": [{"description": "x"}, {"instructions": "y"}]})
    )
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# user_facing_message routing


def test_user_facing_message_invokes_text_sender() -> None:
    sent: list[str] = []

    async def dispatcher(desc: str, instr: str) -> str:
        return "bg-1"

    async def text_sender(msg: str) -> None:
        sent.append(msg)

    tools = build(_stub_cfg(), dispatcher=dispatcher, text_sender=text_sender)
    fn = tools[0].handler

    asyncio.run(
        fn(
            {
                "description": "x",
                "instructions": "y",
                "user_facing_message": "kicked off X, will report",
            }
        )
    )

    assert sent == ["kicked off X, will report"]


def test_user_facing_message_send_failure_does_not_undo_dispatch() -> None:
    """If the ack send fails, the tool should still report success —
    the sub-agent is already running; failing the tool would mislead
    Alice into thinking the dispatch didn't happen."""
    dispatched: list[str] = []

    async def dispatcher(desc: str, instr: str) -> str:
        dispatched.append(desc)
        return "bg-1"

    async def text_sender(msg: str) -> None:
        raise RuntimeError("transport down")

    tools = build(_stub_cfg(), dispatcher=dispatcher, text_sender=text_sender)
    fn = tools[0].handler

    result = asyncio.run(
        fn(
            {
                "description": "x",
                "instructions": "y",
                "user_facing_message": "kicked off",
            }
        )
    )

    assert dispatched == ["x"]
    assert result["status"] == "dispatched"
    assert result.get("isError") is None or result.get("isError") is False


def test_user_facing_message_without_text_sender_is_no_op() -> None:
    """No text_sender wired → message logged + dropped, dispatch
    still reports success."""
    dispatched: list[str] = []

    async def dispatcher(desc: str, instr: str) -> str:
        dispatched.append(desc)
        return "bg-1"

    tools = build(_stub_cfg(), dispatcher=dispatcher, text_sender=None)
    fn = tools[0].handler

    result = asyncio.run(
        fn(
            {
                "description": "x",
                "instructions": "y",
                "user_facing_message": "kicked off",
            }
        )
    )

    assert dispatched == ["x"]
    assert result["status"] == "dispatched"
