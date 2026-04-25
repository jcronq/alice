"""AgentKernel — one SDK ``query()`` call, end-to-end, with observability.

The kernel is the smallest reusable unit of agent work: given a prompt,
a model, a tool allowlist, and an optional ``resume`` pointer, it drives
the SDK's async generator to completion, emits structured events at
every block boundary, and returns a :class:`KernelResult`.

It knows NOTHING about:
- Signal, quiet hours, emergencies, surfaces, inner/notes, or the
  speaking daemon's producer/consumer queue.
- Session persistence across restarts (that's a handler's job — see
  :class:`BlockHandler`).
- Context compaction (also a handler).
- Bootstrap preambles, missed-reply detection, outbox gating.

It DOES handle:
- Building :class:`ClaudeAgentOptions` from a typed :class:`KernelSpec`.
- Dispatching AssistantMessage blocks (text / tool_use / thinking) to
  observers + handlers.
- Emitting SDK-level events (``assistant_text``, ``tool_use``,
  ``thinking``, ``user_message``, ``result``, ``system``) via an
  :class:`EventEmitter`.
- Timeout wrapping (``max_seconds > 0``) and graceful cancellation.
- Catching + emitting ``exception`` events without swallowing them.

Handlers are the extension mechanism. A handler implements a subset of
:class:`BlockHandler` methods and gets called for each relevant block
before the kernel moves on. The speaking daemon composes three handlers
(session persistence, compaction armer, missed-reply detector); the
thinking hemisphere doesn't need any.

Design notes:

- Events carry an optional ``correlation_id`` — daemon uses turn_id;
  thinking wakes use wake_id. Passed once at construction.
- ``silent=True`` suppresses kernel-level event emission for internal
  turns (bootstrap preamble injection, compaction summaries). Handlers
  still fire — they may choose to observe silent turns or not.
- RuntimeError from the inner loop (rate_limit, SDK error, result
  is_error) propagates. The caller decides retry semantics.
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .events import EventEmitter
from .sdk_compat import _short


@dataclass
class KernelSpec:
    """Inputs to one :meth:`AgentKernel.run` invocation.

    All fields map directly to SDK ``ClaudeAgentOptions`` arguments or
    wrapper-level behavior. The kernel builds the SDK options from this
    spec — callers never touch ``ClaudeAgentOptions`` directly.

    ``thinking`` controls extended-thinking emission. When ``None``, the
    SDK uses its own default (which omits thinking text entirely). To
    actually see what the model is reasoning about, pass
    ``{"type": "adaptive", "display": "summarized"}`` — that makes
    ThinkingBlocks come back with non-empty text the viewer can render.
    """

    model: str
    allowed_tools: list[str] = field(default_factory=list)
    cwd: Optional[pathlib.Path] = None
    mcp_servers: Optional[dict] = None
    resume: Optional[str] = None
    max_seconds: int = 0  # 0 or negative = unbounded
    thinking: Optional[dict] = None  # ThinkingConfig dict from claude_agent_sdk.types


@dataclass
class KernelResult:
    """What the kernel returns from a single turn.

    ``text`` is the concatenated assistant TextBlock output. ``usage`` is
    the ResultMessage's usage dict (may be None if the turn errored
    before producing one). ``error`` is set on timeout or internal
    kernel error; the caller decides whether to surface or retry.
    """

    text: str
    session_id: Optional[str]
    usage: Optional[dict]
    duration_ms: Optional[int]
    cost_usd: Optional[float]
    is_error: bool
    num_turns: Optional[int]
    error: Optional[str] = None  # "timeout" | None — more categories as they come up


@runtime_checkable
class BlockHandler(Protocol):
    """Observe blocks as they stream in from the SDK.

    All methods are ``async`` so handlers can do I/O (persist session
    state, write to a summary file, etc.) without blocking the event
    loop. Default implementations are no-ops; subclass :class:`NullHandler`
    and override only what matters.
    """

    async def on_text(self, text: str) -> None: ...
    async def on_tool_use(self, name: str, input: Any, id: str) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_user_message(self, content: Any) -> None: ...
    async def on_result(self, msg: ResultMessage) -> None: ...
    async def on_system(self, msg: SystemMessage) -> None: ...


class NullHandler:
    """No-op BlockHandler base class — subclass and override what you need."""

    async def on_text(self, text: str) -> None: ...
    async def on_tool_use(self, name: str, input: Any, id: str) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_user_message(self, content: Any) -> None: ...
    async def on_result(self, msg: ResultMessage) -> None: ...
    async def on_system(self, msg: SystemMessage) -> None: ...


class AgentKernel:
    """Drive one ``query()`` to completion with observability + handlers.

    Usage::

        kernel = AgentKernel(emitter, correlation_id="turn-abc123")
        result = await kernel.run(
            prompt="hello",
            spec=KernelSpec(model="claude-sonnet-4-6", allowed_tools=["Bash"]),
            handlers=[SessionPersistenceHandler(path=...)],
        )
    """

    def __init__(
        self,
        emitter: EventEmitter,
        *,
        correlation_id: Optional[str] = None,
        silent: bool = False,
        short_cap: int = 2000,
    ) -> None:
        self.emitter = emitter
        self.correlation_id = correlation_id
        self.silent = silent
        self._cap = short_cap

    def _emit(self, event: str, **fields: Any) -> None:
        if self.silent:
            return
        if self.correlation_id is not None:
            fields.setdefault("turn_id", self.correlation_id)
        self.emitter.emit(event, **fields)

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult:
        handlers = list(handlers or [])
        options = self._build_options(spec)

        parts: list[str] = []
        session_id: Optional[str] = None
        usage: Optional[dict] = None
        duration_ms: Optional[int] = None
        cost_usd: Optional[float] = None
        is_error = False
        num_turns: Optional[int] = None

        async def _drive() -> None:
            nonlocal session_id, usage, duration_ms, cost_usd, is_error, num_turns
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None) == "rate_limit":
                        raise RuntimeError("claude rate_limit")
                    if getattr(msg, "error", None):
                        raise RuntimeError(f"claude error: {msg.error}")
                    for block in msg.content:
                        await self._dispatch_block(block, parts, handlers)
                elif isinstance(msg, UserMessage):
                    self._emit("user_message", content=_short(msg.content, self._cap))
                    for h in handlers:
                        await h.on_user_message(msg.content)
                elif isinstance(msg, ResultMessage):
                    session_id = msg.session_id
                    usage = msg.usage
                    duration_ms = getattr(msg, "duration_ms", None)
                    cost_usd = getattr(msg, "total_cost_usd", None)
                    is_error = msg.is_error
                    num_turns = getattr(msg, "num_turns", None)
                    self._emit(
                        "result",
                        session_id=msg.session_id,
                        num_turns=num_turns,
                        duration_ms=duration_ms,
                        total_cost_usd=cost_usd,
                        is_error=msg.is_error,
                        usage=msg.usage,
                    )
                    for h in handlers:
                        await h.on_result(msg)
                    if msg.is_error:
                        detail = getattr(msg, "result", None) or "unknown"
                        raise RuntimeError(f"claude result error: {detail}")
                elif isinstance(msg, SystemMessage):
                    self._emit(
                        "system",
                        subtype=msg.subtype,
                        data_keys=list((msg.data or {}).keys()),
                    )
                    for h in handlers:
                        await h.on_system(msg)

        try:
            if spec.max_seconds and spec.max_seconds > 0:
                async with asyncio.timeout(spec.max_seconds):
                    await _drive()
            else:
                await _drive()
        except asyncio.TimeoutError:
            self._emit("timeout", max_seconds=spec.max_seconds)
            return KernelResult(
                text="".join(parts),
                session_id=session_id,
                usage=usage,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                is_error=True,
                num_turns=num_turns,
                error="timeout",
            )

        return KernelResult(
            text="".join(parts).strip(),
            session_id=session_id,
            usage=usage,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            is_error=is_error,
            num_turns=num_turns,
        )

    async def _dispatch_block(
        self,
        block: Any,
        parts: list[str],
        handlers: list[BlockHandler],
    ) -> None:
        if isinstance(block, TextBlock):
            parts.append(block.text)
            self._emit("assistant_text", text=_short(block.text, self._cap))
            for h in handlers:
                await h.on_text(block.text)
        elif isinstance(block, ToolUseBlock):
            self._emit(
                "tool_use",
                name=block.name,
                input=_short(block.input, self._cap),
                id=block.id,
            )
            for h in handlers:
                await h.on_tool_use(block.name, block.input, block.id)
        elif isinstance(block, ThinkingBlock):
            self._emit("thinking", text=_short(block.thinking, self._cap))
            for h in handlers:
                await h.on_thinking(block.thinking)

    def _build_options(self, spec: KernelSpec) -> ClaudeAgentOptions:
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "allowed_tools": spec.allowed_tools,
        }
        if spec.cwd is not None:
            kwargs["cwd"] = str(spec.cwd)
        if spec.mcp_servers is not None:
            kwargs["mcp_servers"] = spec.mcp_servers
        if spec.resume:
            kwargs["resume"] = spec.resume
        if spec.thinking is not None:
            kwargs["thinking"] = spec.thinking
        return ClaudeAgentOptions(**kwargs)


__all__ = [
    "AgentKernel",
    "KernelSpec",
    "KernelResult",
    "BlockHandler",
    "NullHandler",
]
