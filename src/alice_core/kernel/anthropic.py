"""AnthropicKernel — :class:`Kernel` impl backed by ``claude_agent_sdk``.

One SDK ``query()`` call, end-to-end, with observability. The kernel
is the smallest reusable unit of agent work: given a prompt, a
model, a tool allowlist, and an optional ``resume`` pointer, it
drives the SDK's async generator to completion, emits structured
events at every block boundary, and returns a
:class:`KernelResult`.

It knows NOTHING about:

- Signal, quiet hours, emergencies, surfaces, inner/notes, or the
  speaking daemon's producer/consumer queue.
- Session persistence across restarts (that's a handler's job — see
  :class:`BlockHandler`).
- Context compaction (also a handler).
- Bootstrap preambles, missed-reply detection, outbox gating.

It DOES handle:

- Building :class:`ClaudeAgentOptions` from a typed
  :class:`KernelSpec` (translating ``ThinkingLevel`` to the SDK's
  ``ThinkingConfig`` dict, wrapping ``append_system_prompt`` in the
  SDK's preset shape, etc.).
- Dispatching ``AssistantMessage`` blocks (text / tool_use /
  thinking) to observers + handlers.
- Emitting kernel-level events (``assistant_text``, ``tool_use``,
  ``thinking``, ``user_message``, ``result``, ``system``) via the
  shared :class:`EventEmitter`.
- Converting SDK message types (``ResultMessage``,
  ``SystemMessage``) to backend-agnostic
  :class:`TurnSummary` / :class:`SystemEvent` before calling
  handlers.
- Timeout wrapping (``max_seconds > 0``) and graceful cancellation.
- Catching + emitting ``exception`` events without swallowing them.

Handlers are the extension mechanism. A handler implements a
subset of :class:`BlockHandler` methods and gets called for each
relevant block before the kernel moves on. The speaking daemon
composes three handlers (session persistence, compaction armer,
CLI trace); the thinking hemisphere doesn't currently use any.

Design notes:

- Events carry an optional ``correlation_id`` — daemon uses
  turn_id; thinking wakes use wake_id. Passed once at construction.
- ``silent=True`` suppresses kernel-level event emission for
  internal turns (bootstrap preamble injection, compaction
  summaries). Handlers still fire — they may choose to observe
  silent turns or not.
- ``RuntimeError`` from the inner loop (rate_limit, SDK error,
  result is_error) propagates. The caller decides retry semantics.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Optional

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

from ..events import EventEmitter
from ..sdk_compat import _short
from .protocol import BlockHandler
from .types import (
    KernelResult,
    KernelSpec,
    SystemEvent,
    ThinkingLevel,
    TurnSummary,
    UsageInfo,
)


__all__ = ["AnthropicKernel"]


def _thinking_to_sdk_dict(level: Optional[ThinkingLevel]) -> Optional[dict]:
    """Map our normalized :data:`ThinkingLevel` to the SDK's
    ``ThinkingConfig`` dict shape.

    Mapping preserves the previous default behaviour: callers that
    used to pass ``{"type": "adaptive", "display": "summarized"}``
    now pass ``"medium"`` and the dict is reconstructed here.
    """
    if level is None or level == "off":
        return None
    if level == "minimal":
        return {"type": "adaptive", "display": "minimal"}
    if level in ("low", "medium"):
        return {"type": "adaptive", "display": "summarized"}
    if level == "high":
        return {"type": "extended", "display": "summarized"}
    raise ValueError(f"unknown ThinkingLevel: {level!r}")


def _anthropic_usage_to_info(raw: Optional[dict]) -> Optional[UsageInfo]:
    """Convert Anthropic's ``ResultMessage.usage`` dict to
    :class:`UsageInfo`. Returns ``None`` if the source is missing or
    not a dict (errors before usage was emitted)."""
    if not raw or not isinstance(raw, dict):
        return None
    return UsageInfo(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_input_tokens=raw.get("cache_read_input_tokens"),
        cache_creation_input_tokens=raw.get("cache_creation_input_tokens"),
        total_tokens=raw.get("total_tokens"),
    )


def _usage_info_to_event_dict(usage: Optional[UsageInfo]) -> Optional[dict]:
    """Serialize :class:`UsageInfo` for the JSONL event log so
    aggregators (which expect Anthropic-shaped keys) keep working
    unchanged across both backends."""
    if usage is None:
        return None
    return dataclasses.asdict(usage)


class AnthropicKernel:
    """Drive one SDK ``query()`` to completion with observability +
    handlers. Implements the :class:`Kernel` Protocol.

    Usage::

        kernel = AnthropicKernel(emitter, correlation_id="turn-abc123")
        result = await kernel.run(
            prompt="hello",
            spec=KernelSpec(model="claude-sonnet-4-6", allowed_tools=["Bash"]),
            handlers=[SessionHandler(...)],
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
        usage_info: Optional[UsageInfo] = None
        duration_ms: Optional[int] = None
        cost_usd: Optional[float] = None
        is_error = False
        num_turns: Optional[int] = None

        async def _drive() -> None:
            nonlocal session_id, usage_info, duration_ms, cost_usd, is_error, num_turns
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
                    usage_info = _anthropic_usage_to_info(msg.usage)
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
                        usage=_usage_info_to_event_dict(usage_info),
                    )
                    summary = TurnSummary(
                        session_id=session_id,
                        usage=usage_info,
                        duration_ms=duration_ms,
                        cost_usd=cost_usd,
                        is_error=is_error,
                        num_turns=num_turns,
                        raw=msg,
                    )
                    for h in handlers:
                        await h.on_result(summary)
                    if msg.is_error:
                        detail = getattr(msg, "result", None) or "unknown"
                        raise RuntimeError(f"claude result error: {detail}")
                elif isinstance(msg, SystemMessage):
                    raw_data = msg.data or {}
                    filtered = _filter_system_data(raw_data, cap=self._cap)
                    self._emit("system", subtype=msg.subtype, data=filtered)
                    event = SystemEvent(subtype=msg.subtype, data=raw_data)
                    for h in handlers:
                        await h.on_system(event)

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
                usage=usage_info,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                is_error=True,
                num_turns=num_turns,
                error="timeout",
            )

        return KernelResult(
            text="".join(parts).strip(),
            session_id=session_id,
            usage=usage_info,
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
            # SDK default is 1MB, which dense tool-use turns blow through
            # (observed CLIJSONDecodeError 2026-04-26). 10MB is a generous
            # margin without unbounded memory exposure.
            "max_buffer_size": 10 * 1024 * 1024,
        }
        if spec.cwd is not None:
            kwargs["cwd"] = str(spec.cwd)
        if spec.mcp_servers is not None:
            kwargs["mcp_servers"] = spec.mcp_servers
        if spec.resume:
            kwargs["resume"] = spec.resume
        thinking_dict = _thinking_to_sdk_dict(spec.thinking)
        if thinking_dict is not None:
            kwargs["thinking"] = thinking_dict
        if spec.add_dirs:
            kwargs["add_dirs"] = [str(p) for p in spec.add_dirs]
        if spec.append_system_prompt:
            # The SDK exposes append-to-default via the
            # ``system_prompt`` preset shape:
            # ``{type: preset, preset: claude_code, append: <str>}``
            # — there is no top-level ``append_system_prompt`` kwarg.
            # Using the preset keeps the Claude Code CLI's default
            # system prompt (tools, MCP servers, etc.) intact and
            # adds our personae fragment after it.
            kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": spec.append_system_prompt,
            }
        return ClaudeAgentOptions(**kwargs)


# Fields we don't surface in system events — random uuids, ambient
# settings the trace doesn't need to repeat per turn, version strings,
# etc. Everything else flows through (and gets _short-truncated).
_SYSTEM_DATA_NOISE = {
    "type",                 # redundant with event="system"
    "uuid",                 # per-event random
    "analytics_disabled",
    "fast_mode_state",
    "apiKeySource",
    "output_style",
    "permissionMode",       # we control this
    "agents",               # long preset list
    "plugins",              # long preset list
    "memory_paths",         # private filesystem paths
}


def _filter_system_data(data: dict, *, cap: int) -> dict:
    """Drop noise + truncate large values so system events stay readable."""
    out: dict = {}
    for key, value in data.items():
        if key in _SYSTEM_DATA_NOISE:
            continue
        if isinstance(value, (list, dict)):
            # Compact summary — full lists/dicts blow up log size.
            if isinstance(value, list):
                out[key] = (
                    value[:20] if all(isinstance(v, (str, int, float, bool)) for v in value)
                    else f"[{len(value)} items]"
                )
            else:
                out[key] = list(value.keys())[:30]
        elif isinstance(value, str):
            out[key] = _short(value, cap)
        else:
            out[key] = value
    return out
