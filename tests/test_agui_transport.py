"""AG-UI companion endpoints (/alice/run + /alice/tool-result).

Covers:
- Event dataclass → SSE line shape (``data: {…}\\n\\n`` with the
  SCREAMING_SNAKE_CASE ``type`` discriminator and camelCase fields).
- ``parse_run_agent_input`` happy-path + validation errors.
- /alice/run SSE sequence: scripted runner emits events, request
  collects them, confirms RunStarted → … → RunFinished order.
- /alice/tool-result: resolves the pending future the runner is
  awaiting, the runner wakes up, downstream events land on the
  stream, run completes.
- Unknown (threadId, runId, toolCallId) on the tool-result route
  returns 404 with no side-effects on in-flight runs.
- Auth: missing/wrong ``X-Modern-Mud-Key`` 401s both companion
  endpoints.
- Malformed /alice/run body 400s with a specific reason.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from alice_speaking.transports._agui import (
    RunAgentInput,
    RunStarted,
    TextMessageContent,
    TextMessageEnd,
    TextMessageStart,
    ToolCallArgs,
    ToolCallEnd,
    ToolCallStart,
    parse_run_agent_input,
)
from alice_speaking.transports._agui_router import (
    AGUIRouter,
    AgentContext,
)
from alice_speaking.transports.modern_mud import (
    KEY_HEADER,
    ModernMudTransport,
)


KEY = "s3cret"


# ---------------------------------------------------------------------------
# Unit tests: event dataclasses.


def test_run_started_sse_line_shape():
    ev = RunStarted(thread_id="t1", run_id="r1")
    line = ev.to_sse_line()
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[len("data: ") : -2])
    assert payload == {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"}


def test_tool_call_events_camelcase_wire():
    assert json.loads(
        ToolCallStart(tool_call_id="c1", tool_call_name="move").to_sse_line()[6:]
    ) == {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "move"}
    assert json.loads(
        ToolCallArgs(tool_call_id="c1", delta='{"d":"north"}').to_sse_line()[6:]
    ) == {
        "type": "TOOL_CALL_ARGS",
        "toolCallId": "c1",
        "delta": '{"d":"north"}',
    }
    assert json.loads(ToolCallEnd(tool_call_id="c1").to_sse_line()[6:]) == {
        "type": "TOOL_CALL_END",
        "toolCallId": "c1",
    }


def test_text_message_events():
    assert json.loads(TextMessageStart(message_id="m1").to_sse_line()[6:]) == {
        "type": "TEXT_MESSAGE_START",
        "messageId": "m1",
        "role": "assistant",
    }
    assert json.loads(
        TextMessageContent(message_id="m1", delta="hi").to_sse_line()[6:]
    ) == {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hi"}
    assert json.loads(TextMessageEnd(message_id="m1").to_sse_line()[6:]) == {
        "type": "TEXT_MESSAGE_END",
        "messageId": "m1",
    }


def test_timestamp_round_trips_when_set():
    ev = RunStarted(thread_id="t", run_id="r", timestamp=1234.5)
    assert json.loads(ev.to_sse_line()[6:]) == {
        "type": "RUN_STARTED",
        "threadId": "t",
        "runId": "r",
        "timestamp": 1234.5,
    }


# ---------------------------------------------------------------------------
# Unit tests: parse_run_agent_input.


def _minimal_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "threadId": "t1",
        "runId": "r1",
        "messages": [],
        "tools": [],
        "context": [],
        "state": None,
        "forwardedProps": {},
    }
    body.update(overrides)
    return body


def test_parse_run_agent_input_happy_path():
    body = _minimal_body(
        messages=[{"id": "m1", "role": "user", "content": "look around"}],
        tools=[
            {
                "name": "look_at",
                "description": "Describe a target",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
            }
        ],
    )
    out = parse_run_agent_input(body)
    assert out.thread_id == "t1"
    assert out.run_id == "r1"
    assert len(out.messages) == 1
    assert out.messages[0].role == "user"
    assert out.messages[0].content == "look around"
    assert len(out.tools) == 1
    assert out.tools[0].name == "look_at"
    assert out.tools[0].parameters["required"] == ["target"]


def test_parse_run_agent_input_rejects_missing_ids():
    with pytest.raises(ValueError):
        parse_run_agent_input({"threadId": "", "runId": "r1"})
    with pytest.raises(ValueError):
        parse_run_agent_input({"threadId": "t1", "runId": ""})


def test_parse_run_agent_input_rejects_bad_shapes():
    with pytest.raises(ValueError):
        parse_run_agent_input(_minimal_body(messages="not a list"))
    with pytest.raises(ValueError):
        parse_run_agent_input(
            _minimal_body(tools=[{"description": "x"}])  # missing name
        )
    with pytest.raises(ValueError):
        parse_run_agent_input(
            _minimal_body(messages=[{"id": "m1", "content": "x"}])  # no role
        )


def test_parse_run_agent_input_preserves_unknown_fields():
    body = _minimal_body(custom="hello")
    out = parse_run_agent_input(body)
    assert out.extra == {"custom": "hello"}


# ---------------------------------------------------------------------------
# AGUIRouter unit tests.


def _make_router(runner):
    # Short heartbeat so the ping-interleave tests don't hang.
    return AGUIRouter(runner=runner, heartbeat_seconds=60.0)


def test_router_emits_run_started_and_finished():
    collected = []

    async def noop_runner(input: RunAgentInput, ctx: AgentContext) -> None:
        return

    router = _make_router(noop_runner)

    async def go():
        input = parse_run_agent_input(_minimal_body())
        async for line in router.iter_sse(input):
            collected.append(line)

    asyncio.run(go())
    types = [json.loads(ln[6:])["type"] for ln in collected]
    assert types == ["RUN_STARTED", "RUN_FINISHED"]


def test_router_run_error_on_runner_exception():
    async def boom_runner(input, ctx):
        raise RuntimeError("fire")

    router = _make_router(boom_runner)
    collected = []

    async def go():
        input = parse_run_agent_input(_minimal_body())
        async for line in router.iter_sse(input):
            collected.append(line)

    asyncio.run(go())
    types = [json.loads(ln[6:])["type"] for ln in collected]
    assert types == ["RUN_STARTED", "RUN_ERROR"]
    err_payload = json.loads(collected[-1][6:])
    assert "fire" in err_payload["message"]


def test_router_resolve_tool_call_wakes_runner():
    """A runner awaiting a tool call wakes on resolve_tool_call."""

    async def runner(input: RunAgentInput, ctx: AgentContext) -> None:
        await ctx.emit(
            ToolCallStart(tool_call_id="c1", tool_call_name="move")
        )
        await ctx.emit(ToolCallArgs(tool_call_id="c1", delta='{"direction":"n"}'))
        await ctx.emit(ToolCallEnd(tool_call_id="c1"))
        result = await ctx.request_tool_call("c1", timeout=2.0)
        await ctx.emit(TextMessageStart(message_id="m0"))
        if result.content:
            await ctx.emit(
                TextMessageContent(message_id="m0", delta=str(result.content))
            )
        await ctx.emit(TextMessageEnd(message_id="m0"))

    router = _make_router(runner)
    collected = []

    async def go():
        input = parse_run_agent_input(_minimal_body())

        # Resolve the tool call shortly after the stream starts so the
        # runner wakes. Even a 20ms delay is enough; we don't rely on
        # observing TOOL_CALL_END first, pre-registration on the
        # router handles the race.
        async def nudge():
            await asyncio.sleep(0.05)
            ok = router.resolve_tool_call(
                thread_id="t1",
                run_id="r1",
                tool_call_id="c1",
                content="A dusty hall",
            )
            assert ok

        asyncio.get_event_loop().create_task(nudge())
        async for line in router.iter_sse(input):
            collected.append(line)

    asyncio.run(go())
    types = [json.loads(ln[6:])["type"] for ln in collected]
    assert types == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    content = json.loads(collected[5][6:])
    assert content["delta"] == "A dusty hall"


def test_router_resolve_unknown_run_is_false():
    router = _make_router(lambda i, c: asyncio.sleep(0))
    assert (
        router.resolve_tool_call(
            thread_id="unknown", run_id="x", tool_call_id="c", content="x"
        )
        is False
    )


def test_router_tool_call_timeout_returns_error_result():
    captured: dict = {}

    async def runner(input, ctx):
        res = await ctx.request_tool_call("c1", timeout=0.05)
        captured["res"] = res
        await ctx.emit(TextMessageStart(message_id="m"))
        await ctx.emit(TextMessageEnd(message_id="m"))

    router = _make_router(runner)
    collected = []

    async def go():
        input = parse_run_agent_input(_minimal_body())
        async for line in router.iter_sse(input):
            collected.append(line)

    asyncio.run(go())
    assert captured["res"].error == "timeout"


# ---------------------------------------------------------------------------
# HTTP integration tests (ASGI, no real socket).


def _make_transport(runner=None):
    async def sender(url, body, headers):
        return

    return ModernMudTransport(
        key=KEY,
        admin_user_ids={"admin1"},
        callback_url="http://game/callback",
        port=0,
        callback_sender=sender,
        agui_runner=runner,
    )


async def _post_json(t, path, body, key=KEY, headers_extra=None):
    transport = httpx.ASGITransport(app=t._build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        headers = {KEY_HEADER: key} if key is not None else {}
        if headers_extra:
            headers.update(headers_extra)
        return await c.post(path, json=body, headers=headers)


def test_alice_run_rejects_missing_key():
    t = _make_transport()
    r = asyncio.run(_post_json(t, "/alice/run", _minimal_body(), key=None))
    assert r.status_code == 401


def test_alice_run_400s_on_bad_body():
    t = _make_transport()
    r = asyncio.run(_post_json(t, "/alice/run", {"threadId": "t"}))
    # missing runId
    assert r.status_code == 400
    assert "runId" in r.json()["error"]


def test_alice_run_emits_sse_stream():
    async def runner(input, ctx):
        await ctx.emit(TextMessageStart(message_id="m"))
        await ctx.emit(TextMessageContent(message_id="m", delta="hi"))
        await ctx.emit(TextMessageEnd(message_id="m"))

    t = _make_transport(runner=runner)

    async def go():
        transport = httpx.ASGITransport(app=t._build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async with c.stream(
                "POST",
                "/alice/run",
                json=_minimal_body(),
                headers={KEY_HEADER: KEY},
            ) as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]
                lines: list[str] = []
                async for chunk in r.aiter_text():
                    lines.append(chunk)
                    joined = "".join(lines)
                    if joined.count("\n\n") >= 5:
                        break
                return "".join(lines)

    raw = asyncio.run(go())
    # Split SSE records at blank-line boundaries
    records = [r for r in raw.split("\n\n") if r.startswith("data: ")]
    types = [json.loads(r[len("data: ") :])["type"] for r in records]
    assert "RUN_STARTED" in types
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "TEXT_MESSAGE_END" in types
    assert "RUN_FINISHED" in types


def test_alice_tool_result_bad_key_401():
    t = _make_transport()
    r = asyncio.run(
        _post_json(
            t,
            "/alice/tool-result",
            {"threadId": "t", "runId": "r", "toolCallId": "c", "content": "x"},
            key="nope",
        )
    )
    assert r.status_code == 401


def test_alice_tool_result_rejects_missing_fields():
    t = _make_transport()
    r = asyncio.run(
        _post_json(t, "/alice/tool-result", {"threadId": "t", "runId": "r"})
    )
    assert r.status_code == 400
    assert "toolCallId" in r.json()["error"]


def test_alice_tool_result_404_when_no_pending():
    t = _make_transport()
    r = asyncio.run(
        _post_json(
            t,
            "/alice/tool-result",
            {
                "threadId": "none",
                "runId": "none",
                "toolCallId": "none",
                "content": "x",
            },
        )
    )
    assert r.status_code == 404


def test_alice_tool_result_wakes_runner_end_to_end():
    """Full round-trip: SSE + companion POST resolves the tool call."""

    async def runner(input, ctx):
        await ctx.emit(
            ToolCallStart(tool_call_id="c1", tool_call_name="say")
        )
        await ctx.emit(ToolCallArgs(tool_call_id="c1", delta='{"text":"hi"}'))
        await ctx.emit(ToolCallEnd(tool_call_id="c1"))
        result = await ctx.request_tool_call("c1", timeout=3.0)
        await ctx.emit(TextMessageStart(message_id="m"))
        await ctx.emit(
            TextMessageContent(
                message_id="m", delta=str(result.content or result.error or "")
            )
        )
        await ctx.emit(TextMessageEnd(message_id="m"))

    t = _make_transport(runner=runner)

    async def go():
        transport = httpx.ASGITransport(app=t._build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Fire the companion POST slightly after the stream opens
            # to confirm resolve_tool_call wakes the runner.
            async def poster():
                await asyncio.sleep(0.05)
                r2 = await c.post(
                    "/alice/tool-result",
                    json={
                        "threadId": "t1",
                        "runId": "r1",
                        "toolCallId": "c1",
                        "content": "You say hi.",
                    },
                    headers={KEY_HEADER: KEY},
                )
                assert r2.status_code == 200
                return r2

            task = asyncio.create_task(poster())

            async with c.stream(
                "POST",
                "/alice/run",
                json=_minimal_body(),
                headers={KEY_HEADER: KEY},
            ) as r:
                assert r.status_code == 200
                buf = ""
                async for chunk in r.aiter_text():
                    buf += chunk
                    if "RUN_FINISHED" in buf:
                        break

            await task
            return buf

    raw = asyncio.run(go())
    records = [r for r in raw.split("\n\n") if r.startswith("data: ")]
    types = [json.loads(r[len("data: ") :])["type"] for r in records]
    assert types.index("TOOL_CALL_END") < types.index("TEXT_MESSAGE_CONTENT")
    content_rec = next(
        json.loads(r[len("data: ") :])
        for r in records
        if '"TEXT_MESSAGE_CONTENT"' in r
    )
    assert content_rec["delta"] == "You say hi."
