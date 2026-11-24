"""Tests for kernels.pi.translator.PiEventTranslator.

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

from core.kernel import NullHandler, TurnSummary

from kernels.pi.translator import PiEventTranslator


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


def test_text_deltas_accumulate_and_emit_once_on_text_end() -> None:
    """text_delta events accumulate into KernelResult.text but do
    NOT fire on_text per delta — handlers + the assistant_text
    event get one call per completed block, on text_end. Matches
    AnthropicKernel's TextBlock semantic."""
    fired: list[str] = []
    emitted_texts: list[str] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired.append(text)

    emit, recorded = _emit_recorder()
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
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_end",
                        "content": "Hello world",
                    },
                },
            ],
            handlers=[H()],
        )
    )

    # One handler call, one event emission — both with the full
    # block content rather than per-delta chunks.
    assert fired == ["Hello world"]
    emitted_texts = [f.get("text") for name, f in recorded if name == "assistant_text"]
    assert emitted_texts == ["Hello world"]
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


def test_tool_execution_end_calls_on_tool_result_and_emits() -> None:
    fired: list[tuple] = []

    class H(NullHandler):
        async def on_tool_result(self, tool_use_id, content, is_error):
            fired.append((tool_use_id, content, is_error))

    emit, recorded = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "tool_execution_end",
                    "toolName": "Read",
                    "toolCallId": "tc-1",
                    "result": "file contents",
                    "isError": False,
                }
            ],
            handlers=[H()],
        )
    )
    assert fired == [("tc-1", "file contents", False)]
    tr = [f for (name, f) in recorded if name == "tool_result"]
    assert tr and tr[0]["id"] == "tc-1" and tr[0]["is_error"] is False


def test_tool_execution_end_marks_is_error_true() -> None:
    fired: list[tuple] = []

    class H(NullHandler):
        async def on_tool_result(self, tool_use_id, content, is_error):
            fired.append((tool_use_id, content, is_error))

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(
        _drain(
            t,
            [
                {
                    "type": "tool_execution_end",
                    "toolCallId": "tc-2",
                    "result": "boom",
                    "isError": True,
                }
            ],
            handlers=[H()],
        )
    )
    assert fired == [("tc-2", "boom", True)]


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


# ---------------------------------------------------------------------
# Backend-error on assistant message_end (grand-Alice 2026-11-23 regression)
#
# Pi surfaces upstream provider failures via
# ``stopReason == "error"`` + ``errorMessage`` on the assistant's
# ``message_end`` event rather than a top-level ``error`` event. Doug's
# three forwarded inbounds all hit codex's ChatGPT usage limit:
#
#   {"role": "assistant", "content": [], "usage": {"totalTokens": 0},
#    "stopReason": "error",
#    "errorMessage": "You have hit your ChatGPT usage limit (plus plan).
#                     Try again in ~142 min."}
#
# Before the fix the translator ignored ``stopReason`` and reported a
# successful empty turn (``is_error=False``, no ``KernelResult.error``),
# so the daemon closed the turn with ``missed_reply`` and the user saw
# silence. After the fix the translator must:
#
#   * flip state to is_error with the pi errorMessage as error_message,
#   * emit a ``pi_backend_error`` event with the message + session,
#   * emit the ``result`` event with ``is_error=True`` + ``error=...``,
#   * dispatch ``on_result`` with ``is_error=True``,
#   * surface ``KernelResult.error`` on to_kernel_result.
# ---------------------------------------------------------------------


def _codex_quota_message_end(session_id: str = "sess-doug") -> list[dict]:
    """A captured-shape message_end carrying codex's usage-limit error.
    Session is pre-seeded so the result event also carries the session
    id (that's how a real turn would arrive)."""
    return [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/tmp"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "api": "openai-codex-responses",
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "usage": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 0,
                },
                "stopReason": "error",
                "timestamp": 1795469907857,
                "errorMessage": (
                    "You have hit your ChatGPT usage limit (plus plan). "
                    "Try again in ~142 min."
                ),
            },
        },
    ]


def test_message_end_stop_reason_error_sets_is_error_and_error_message() -> None:
    """A stopReason=error / errorMessage pair on message_end must flip
    the translator to is_error state so KernelResult.error propagates
    to the turn runner. Without this the missed_reply path fires
    silently (grand-Alice 2026-11-23: Doug's three forwarded inbounds
    all codex-quota-blocked, zero signal_sends)."""
    t, _ = _drive(_codex_quota_message_end())
    result = t.to_kernel_result()
    assert result.is_error is True
    assert result.error is not None
    assert "ChatGPT usage limit" in result.error


def test_message_end_stop_reason_error_emits_pi_backend_error_event() -> None:
    """Operator-grep target: a ``pi_backend_error`` event with the
    stopReason, message, and session id surfaces alongside the
    ``result`` event so silent-turn debugging has a concrete trail."""
    _, recorded = _drive(_codex_quota_message_end())
    backend_errors = [
        fields for (name, fields) in recorded if name == "pi_backend_error"
    ]
    assert len(backend_errors) == 1
    be = backend_errors[0]
    assert be["stop_reason"] == "error"
    assert "ChatGPT usage limit" in be["message"]
    assert be["session_id"] == "sess-doug"


def test_message_end_stop_reason_error_result_event_carries_error_fields() -> None:
    """The ``result`` event that replaces the previous silent success
    must carry ``is_error=True`` AND a populated ``error`` field — the
    daemon's missed_reply branch (and any downstream guardrail) reads
    these to decide on a user-visible fallback."""
    _, recorded = _drive(_codex_quota_message_end())
    results = [fields for (name, fields) in recorded if name == "result"]
    assert len(results) == 1
    r = results[0]
    assert r["is_error"] is True
    assert "ChatGPT usage limit" in r.get("error", "")


def test_message_end_stop_reason_error_fires_handler_with_is_error() -> None:
    """``on_result(TurnSummary)`` must reflect the error so handlers
    (ToolCaptureHandler, ChatStatusHandler, …) can react without
    reinspecting the raw pi payload."""
    seen: list = []

    class H(NullHandler):
        async def on_result(self, summary):
            seen.append(summary)

    emit, _ = _emit_recorder()
    t = PiEventTranslator(emit)
    asyncio.run(_drain(t, _codex_quota_message_end(), handlers=[H()]))
    assert len(seen) == 1
    assert seen[0].is_error is True


def test_message_end_stop_reason_error_without_error_message_is_ignored() -> None:
    """Defensive: a bare ``stopReason=="error"`` with NO errorMessage
    shouldn't crash or synthesize a fake error string — pi hasn't
    actually told us anything to surface. Treat as a weird-but-benign
    empty turn (still is_error=False) so downstream handling isn't
    confused by an error payload with no cause."""
    t, recorded = _drive(
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    # errorMessage absent
                },
            },
        ]
    )
    result = t.to_kernel_result()
    assert result.is_error is False
    assert result.error is None
    assert not any(name == "pi_backend_error" for (name, _) in recorded)


def test_message_end_normal_stop_reason_leaves_is_error_false() -> None:
    """Regression guard: successful turns (stopReason=="end" /
    "toolUse" / absent) must keep the old ``is_error=False``
    behaviour. We only flip on the specific error signal."""
    t, recorded = _drive(
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {
                        "input": 10,
                        "output": 2,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 12,
                    },
                    "stopReason": "end",
                    "timestamp": 1777599884167,
                },
            },
        ]
    )
    result = t.to_kernel_result()
    assert result.is_error is False
    assert result.error is None
    results = [fields for (name, fields) in recorded if name == "result"]
    assert results[0]["is_error"] is False
    assert "error" not in results[0]


def test_to_kernel_result_explicit_error_wins_over_captured() -> None:
    """When PiKernel.run hands ``error="timeout"`` to to_kernel_result,
    the explicit identifier must survive even if the translator also
    captured a backend error mid-stream — the timeout path needs its
    stable sentinel for callers that branch on it."""
    t, _ = _drive(_codex_quota_message_end())
    result = t.to_kernel_result(error="timeout", is_error=True)
    assert result.is_error is True
    assert result.error == "timeout"
