"""Async router for ``/alice/run`` + ``/alice/tool-result``.

Two-endpoint dance that lets Alice invoke client-registered tools:

- The client POSTs ``/alice/run`` with a :class:`RunAgentInput` body
  and holds the connection open for an SSE stream.
- The daemon spawns an agent-loop task. The task emits AG-UI events
  onto the SSE queue; when it wants to invoke a client tool it calls
  :meth:`AGUIRouter.request_tool_call` which returns an awaitable
  resolved by the companion endpoint.
- The client, on receipt of a ``ToolCallEnd`` event, runs the tool
  locally and POSTs the result to ``/alice/tool-result`` keyed by
  ``(threadId, runId, toolCallId)``. That POST resolves the pending
  future; the agent loop wakes and continues.

No LLM wiring lives here — the actual agent behaviour is injected via
an :class:`AgentRunner` callable, so tests can script event sequences
and tool-call acks without a real model in the loop.

Blocking semantics: one ``RunContext`` per in-flight ``(threadId,
runId)``. Within a run, each ``toolCallId`` gets its own
``asyncio.Future`` created lazily the first time the runner asks for
it. The tool-result route resolves that future. The runner is
responsible for emitting ``TOOL_CALL_START/ARGS/END`` events before
calling ``request_tool_call``; the client treats ``TOOL_CALL_END`` as
its cue to execute and reply.

Timeouts: the runner can pass a ``timeout`` to
``request_tool_call``; on expiry the future resolves with a
``ToolCallResult(error="timeout")`` and the agent decides how to
react. A stuck run eventually finishes via the runner's own
``RUN_ERROR`` emission — the router does not force-close SSE streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from ._agui import (
    AGUIEvent,
    RunAgentInput,
    RunError,
    RunFinished,
    RunStarted,
    parse_run_agent_input,
)


log = logging.getLogger(__name__)


# Interval between SSE heartbeat comments. Keeps idle proxies from
# closing the stream while the runner waits on a tool result.
DEFAULT_HEARTBEAT_SECONDS = 15.0

# Max per-run tool-result wait. The runner can override, but even a
# forgiving default stops a hung client from leaking the pending map.
DEFAULT_TOOL_RESULT_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Public result shape.


@dataclass(frozen=True)
class ToolCallResult:
    """Outcome of one client-executed tool-call, as the agent sees it."""

    tool_call_id: str
    content: Any = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# AgentRunner protocol — the injection seam for the LLM.


class AgentContext(Protocol):
    """What the runner gets to drive one run."""

    thread_id: str
    run_id: str

    async def emit(self, event: AGUIEvent) -> None: ...
    async def request_tool_call(
        self, tool_call_id: str, *, timeout: Optional[float] = None
    ) -> ToolCallResult: ...


# A runner is an awaitable that drives one run to completion.
# ``input`` is the parsed RunAgentInput; ``ctx`` is the AgentContext.
AgentRunner = Callable[[RunAgentInput, AgentContext], Awaitable[None]]


# ---------------------------------------------------------------------------
# Router + per-run state.


@dataclass
class _RunState:
    """Per-run book-keeping."""

    thread_id: str
    run_id: str
    created_at: float
    events: asyncio.Queue[Optional[AGUIEvent]]  # None is the finish sentinel
    pending_tools: dict[str, asyncio.Future[ToolCallResult]] = field(default_factory=dict)
    # Set by the run-task when it finishes (success OR error) so
    # late ``/alice/tool-result`` POSTs can return 410-ish cleanly
    # without blocking on a now-dead future.
    closed: bool = False


class AGUIRouter:
    """Owns the live-run registry and the two endpoint handlers."""

    def __init__(
        self,
        *,
        runner: AgentRunner,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        default_tool_timeout: float = DEFAULT_TOOL_RESULT_TIMEOUT,
        # Injectable so tests can pin wall-clock ordering.
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._heartbeat_seconds = heartbeat_seconds
        self._default_tool_timeout = default_tool_timeout
        self._now = now
        # (thread_id, run_id) -> RunState
        self._runs: dict[tuple[str, str], _RunState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Run lifecycle (private)

    async def _register_run(self, thread_id: str, run_id: str) -> _RunState:
        key = (thread_id, run_id)
        async with self._lock:
            if key in self._runs:
                # Duplicate (threadId, runId) is the client's bug; we
                # overwrite because a retry should not inherit stale
                # pending futures. The old queue is orphaned; its
                # consumer (the previous SSE handler) will drain its
                # sentinel and hang up.
                old = self._runs[key]
                old.closed = True
                old.events.put_nowait(None)
            state = _RunState(
                thread_id=thread_id,
                run_id=run_id,
                created_at=self._now(),
                events=asyncio.Queue(),
            )
            self._runs[key] = state
            return state

    async def _unregister_run(self, state: _RunState) -> None:
        async with self._lock:
            key = (state.thread_id, state.run_id)
            cur = self._runs.get(key)
            if cur is state:
                self._runs.pop(key, None)
        state.closed = True
        # Any outstanding tool-result waiters must not leak.
        for fut in state.pending_tools.values():
            if not fut.done():
                fut.set_result(
                    ToolCallResult(
                        tool_call_id="",  # caller will have its own id
                        error="run closed before result arrived",
                    )
                )

    # ------------------------------------------------------------------
    # AgentContext — exposed to the runner via a thin adapter.

    def _make_context(self, state: _RunState) -> AgentContext:
        router = self
        default_timeout = self._default_tool_timeout

        class _Ctx:
            thread_id = state.thread_id
            run_id = state.run_id

            async def emit(self, event: AGUIEvent) -> None:
                await state.events.put(event)

            async def request_tool_call(
                self,
                tool_call_id: str,
                *,
                timeout: Optional[float] = None,
            ) -> ToolCallResult:
                if not tool_call_id:
                    raise ValueError("tool_call_id required")
                fut = state.pending_tools.get(tool_call_id)
                if fut is None:
                    loop = asyncio.get_running_loop()
                    fut = loop.create_future()
                    state.pending_tools[tool_call_id] = fut
                wait = timeout if timeout is not None else default_timeout
                try:
                    return await asyncio.wait_for(fut, timeout=wait)
                except asyncio.TimeoutError:
                    state.pending_tools.pop(tool_call_id, None)
                    return ToolCallResult(
                        tool_call_id=tool_call_id,
                        error="timeout",
                    )

        return _Ctx()

    # ------------------------------------------------------------------
    # Public API: resolve a tool call from the companion endpoint.

    def resolve_tool_call(
        self,
        *,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
        content: Any = "",
        error: Optional[str] = None,
    ) -> bool:
        """Deliver a ``/alice/tool-result`` POST into the pending future.

        Returns ``True`` when a waiter was resolved, ``False`` when no
        matching ``(threadId, runId, toolCallId)`` is registered
        (unknown run, duplicate POST, late arrival). The caller turns
        ``False`` into a 404 or 410 response — the router does not
        raise, so the companion route can return a clean JSON error.
        """

        state = self._runs.get((thread_id, run_id))
        if state is None or state.closed:
            return False
        fut = state.pending_tools.get(tool_call_id)
        if fut is None:
            # Pre-register. The runner may POST its ToolCallEnd and the
            # client may reply faster than ``request_tool_call`` runs
            # — race-free because the next ``request_tool_call`` will
            # read-through our already-resolved future.
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            state.pending_tools[tool_call_id] = fut
        if fut.done():
            return False
        fut.set_result(
            ToolCallResult(
                tool_call_id=tool_call_id,
                content=content,
                error=error,
            )
        )
        return True

    # ------------------------------------------------------------------
    # /alice/run — runs the agent and streams events as SSE.

    async def run_stream(self, input: RunAgentInput) -> "asyncio.Queue[Optional[AGUIEvent]]":
        """Register a run, spawn the runner task, return the event queue.

        The queue yields :class:`AGUIEvent` instances; a ``None`` is
        the sentinel that closes the SSE stream. The caller (HTTP
        handler) is responsible for iterating the queue, serialising
        each event to ``data: …\\n\\n``, and interleaving heartbeat
        comments on idle.
        """

        state = await self._register_run(input.thread_id, input.run_id)

        async def _drive() -> None:
            try:
                await state.events.put(RunStarted(thread_id=input.thread_id, run_id=input.run_id))
                ctx = self._make_context(state)
                await self._runner(input, ctx)
                await state.events.put(
                    RunFinished(thread_id=input.thread_id, run_id=input.run_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "agui runner failed thread=%s run=%s",
                    input.thread_id,
                    input.run_id,
                )
                await state.events.put(
                    RunError(message=f"{type(exc).__name__}: {exc}")
                )
            finally:
                await state.events.put(None)
                await self._unregister_run(state)

        asyncio.create_task(
            _drive(), name=f"agui-run:{input.thread_id}:{input.run_id}"
        )
        return state.events

    async def iter_sse(self, input: RunAgentInput):
        """Async generator yielding SSE strings (``data: …\\n\\n``)
        with periodic heartbeat comments.

        The generator exits when the runner signals completion (the
        ``None`` sentinel) or when the caller stops consuming (client
        disconnect). Heartbeats are ``: ping\\n\\n`` — ignored by the
        AG-UI decoder, kept by reverse-proxies' idle timers.
        """

        queue = await self.run_stream(input)
        heartbeat = self._heartbeat_seconds
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                # Comment line — decoded as nothing by an SSE parser,
                # keeps the connection warm.
                yield ": ping\n\n"
                continue
            if event is None:
                return
            yield event.to_sse_line()


# ---------------------------------------------------------------------------
# A tiny built-in runner for smoke tests & echoes.
#
# The default runner invokes each tool in ``input.tools`` once, with
# the args pulled from the last user message (plain text →
# ``{"text": "..."}``) and echoes the tool's content back as a
# single-assistant-message narrative. It is NOT a production agent —
# it exists so the plumbing is exercisable end-to-end without an LLM.


async def echo_runner(input: RunAgentInput, ctx: AgentContext) -> None:
    """Minimal agent-loop stand-in used for integration smoke tests."""
    from ._agui import (
        TextMessageContent,
        TextMessageEnd,
        TextMessageStart,
        ToolCallArgs,
        ToolCallEnd,
        ToolCallStart,
    )

    if not input.tools:
        mid = f"{input.run_id}.m0"
        await ctx.emit(TextMessageStart(message_id=mid))
        await ctx.emit(
            TextMessageContent(message_id=mid, delta="no tools registered")
        )
        await ctx.emit(TextMessageEnd(message_id=mid))
        return

    last_user = ""
    for m in reversed(list(input.messages)):
        if m.role == "user" and isinstance(m.content, str):
            last_user = m.content
            break

    tool = input.tools[0]
    tool_call_id = f"{input.run_id}.t0"
    await ctx.emit(
        ToolCallStart(tool_call_id=tool_call_id, tool_call_name=tool.name)
    )
    args_payload = json.dumps({"text": last_user})
    await ctx.emit(ToolCallArgs(tool_call_id=tool_call_id, delta=args_payload))
    await ctx.emit(ToolCallEnd(tool_call_id=tool_call_id))
    result = await ctx.request_tool_call(tool_call_id)

    mid = f"{input.run_id}.m0"
    await ctx.emit(TextMessageStart(message_id=mid))
    body = result.error or str(result.content)
    if body:
        await ctx.emit(TextMessageContent(message_id=mid, delta=body))
    await ctx.emit(TextMessageEnd(message_id=mid))


__all__ = [
    "AGUIRouter",
    "AgentContext",
    "AgentRunner",
    "ToolCallResult",
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_TOOL_RESULT_TIMEOUT",
    "echo_runner",
]
