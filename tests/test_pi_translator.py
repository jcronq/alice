"""Tests for alice_pi.translator.PiEventTranslator.

Drives the translator with hand-crafted JSONL fixtures (mirroring
the spike's captured stream against gpt-5.3-codex) and asserts:

- text_delta events accumulate into KernelResult.text and call
  on_text(delta) handlers.
- tool_execution_start fires on_tool_use(name, input, id).
- assistant message_end captures usage, fires on_result(TurnSummary).
- error events raise RuntimeError with the message.
- KernelResult is buildable mid-stream (timeout path).
"""

from __future__ import annotations

import asyncio

from alice_core.kernel import NullHandler, TurnSummary

from alice_pi.translator import PiEventTranslator


def _emit_recorder():
    """Returns (callable, list-of-events) to capture the translator's
    emit calls without an EventEmitter dependency."""
    events: list = []

    def emit(_event_name, **fields):
        events.append((_event_name, fields))

    return emit, events


def _drive(events: list[dict]) -> tuple[PiEventTranslator, list[tuple]]:
    emit, recorded = _emit_recorder()
    t = PiEventTranslator(emit, short_cap=2000)
    asyncio.run(_drain(t, events))
    return t, recorded


async def _drain(t: PiEventTranslator, events: list[dict], handlers=None) -> None:
    handlers = handlers or []
    for ev in events:
        await t.handle(ev, handlers)


def test_session_event_records_session_id_and_emits_system() -> None:
    t, recorded = _drive(
        [{"type": "session", "version": 3, "id": "sess-1", "cwd": "/tmp"}]
    )
    assert t.session_id == "sess-1"
    kinds = [e[0] for e in recorded]
    assert "system" in kinds


def test_text_delta_accumulates_and_calls_on_text() -> None:
    fired: list[str] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired.append(text)

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "Hello ",
                    },
                },
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "world",
                    },
                },
            ],
            handlers=[H()],
        )
    )

    assert fired == ["Hello ", "world"]
    assert t.to_kernel_result().text == "Hello world"


def test_tool_execution_start_calls_on_tool_use() -> None:
    fired: list[tuple] = []

    class H(NullHandler):
        async def on_tool_use(self, name, input, id):
            fired.append((name, input, id))

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "tool_execution_start",
                    "toolName": "Read",
                    "toolCallId": "tc-1",
                    "args": {"file_path": "/x"},
                }
            ],
            handlers=[H()],
        )
    )
    assert fired == [("Read", {"file_path": "/x"}, "tc-1")]


def test_message_end_assistant_fires_on_result_with_usage() -> None:
    seen: list[TurnSummary] = []

    class H(NullHandler):
        async def on_result(self, summary):
            seen.append(summary)

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {"type": "session", "version": 3, "id": "sess-2", "cwd": "/tmp"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {
                            "input": 1050,
                            "output": 5,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "totalTokens": 1055,
                        },
                        "timestamp": 1777599884167,
                    },
                },
            ],
            handlers=[H()],
        )
    )
    assert len(seen) == 1
    assert seen[0].session_id == "sess-2"
    assert seen[0].usage is not None
    assert seen[0].usage.input_tokens == 1050
    assert seen[0].usage.output_tokens == 5
    assert seen[0].cost_usd is None  # subscription-billed; not surfaced


def test_message_end_for_user_role_does_not_fire_handler() -> None:
    seen: list = []

    class H(NullHandler):
        async def on_result(self, summary):
            seen.append(summary)

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "message_end",
                    "message": {"role": "user", "content": []},
                }
            ],
            handlers=[H()],
        )
    )
    assert seen == []


def test_error_event_raises_runtime_error() -> None:
    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)

    import pytest

    with pytest.raises(RuntimeError, match="rate limit"):
        asyncio.run(
            _drain(
                t,
                [
                    {
                        "type": "error",
                        "message": "ChatGPT rate limit reached",
                    }
                ],
            )
        )


def test_kernel_result_text_is_stripped() -> None:
    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "  trailing ws  ",
                    },
                }
            ],
        )
    )
    assert t.to_kernel_result().text == "trailing ws"


def test_compaction_events_emit_under_compaction_kind() -> None:
    emit, recorded = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {"type": "compaction_start", "reason": "threshold"},
                {
                    "type": "compaction_end",
                    "reason": "threshold",
                    "aborted": False,
                    "willRetry": False,
                    "result": None,
                },
            ],
        )
    )
    kinds = [e[0] for e in recorded]
    assert kinds.count("compaction") == 2


def test_to_kernel_result_with_timeout_marks_error() -> None:
    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "partial",
                    },
                }
            ],
        )
    )
    result = t.to_kernel_result(error="timeout", is_error=True)
    assert result.error == "timeout"
    assert result.is_error is True
    assert result.text == "partial"
