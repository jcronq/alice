"""Agent-level hook surface.

Hooks fire at agent-loop boundaries — before the agent starts, before
each turn's messages go to the model, after each turn's result comes
back, and after the agent ends. They are NOT tool-call-boundary
hooks (the SDK doesn't expose the cooperation that would require).

This module defines the surface ONLY:

- :class:`AgentHook` Protocol — the four lifecycle methods every hook
  may implement.
- :class:`BaseAgentHook` — subclassable no-op base, so consumers
  override only the methods they care about.
- :class:`TurnResult` — backend-agnostic dataclass passed to
  :meth:`AgentHook.after_turn`. Carries the assistant text, the tool
  calls the agent made, the tool results that came back, and the
  stop reason.
- :class:`Reporter` Protocol — small side-channel for hooks to
  surface warnings / errors / structured events outside the
  injection model.
- :class:`LoggingReporter` — default :class:`Reporter` impl that
  routes through an :class:`~core.events.EventEmitter` (if
  supplied) and stdlib :mod:`logging`.

Composition, registration, and run-loop wiring land in PR2 / PR3.
The first concrete consumer (the markdown frontmatter validator)
lands in PR4. Full design: `inner/designs/
2026-05-20-kernel-hooks-and-dot-claude-audit.md`.

The module imports only from stdlib, ``claude_agent_sdk``, and
sibling ``core`` modules — the ``test_core_isolation`` guard
enforces that boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from claude_agent_sdk import Message

from ..events import EventEmitter
from .types import KernelResult, KernelSpec


__all__ = [
    "AgentHook",
    "BaseAgentHook",
    "LoggingReporter",
    "Message",
    "Reporter",
    "ToolResult",
    "ToolUse",
    "TurnResult",
]


# ---------------------------------------------------------------------------
# Turn-result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolUse:
    """One tool call the agent emitted this turn.

    Backend-agnostic mirror of the SDK's ``ToolUseBlock``. Each
    backend translates its native block into this shape before
    handing it to hooks, so hook code never imports SDK types
    directly. ``input`` is whatever the model produced — typically
    a ``dict`` for tool-input JSON, but kept ``Any`` to tolerate
    backends that pass through raw strings or other shapes.
    """

    name: str
    input: Any
    id: str


@dataclass(frozen=True)
class ToolResult:
    """One tool-result block the agent received this turn.

    Pairs with a :class:`ToolUse` by ``tool_use_id``. ``content`` is
    whatever the tool returned — usually a string for stdout-style
    tools, sometimes a list of content blocks for richer tools.
    ``is_error`` is the SDK's signal that the tool failed (e.g.
    non-zero exit, raised exception).
    """

    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class TurnResult:
    """Result of one agent turn, passed to :meth:`AgentHook.after_turn`.

    ``text`` is the concatenated assistant text content for the turn.
    ``tool_uses`` lists every tool call the model emitted; ``tool_results``
    lists the corresponding results the agent loop fed back in. ``stop_reason``
    mirrors the SDK's stop signal (``"end_turn"``, ``"tool_use"``,
    ``"max_tokens"``, etc.) — the runtime uses it to decide whether the
    loop continues. Hooks may inspect it but cannot change it.

    PR3 populates this from the per-turn state the run loop already
    tracks; PR1 only fixes the shape so downstream consumers (PR4's
    markdown validator) can code against it.
    """

    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    stop_reason: str = ""


# ---------------------------------------------------------------------------
# Reporter — side-channel for hook warnings / errors / events
# ---------------------------------------------------------------------------


@runtime_checkable
class Reporter(Protocol):
    """Side-channel for hooks to surface non-injection signals.

    The :meth:`AgentHook.after_turn` return value is the primary
    feedback channel (string → synthetic user message into the next
    turn). Reporter is the secondary channel for things that don't
    belong in the model's context — log lines, telemetry events,
    operator-visible warnings.

    Three methods cover the common shapes: ``warn`` and ``error`` for
    human-readable severity-tagged messages; ``emit_event`` for
    structured records that flow into the JSONL event stream.
    """

    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def emit_event(self, event: str, **fields: Any) -> None: ...


class LoggingReporter:
    """Default :class:`Reporter` impl.

    Routes ``warn`` / ``error`` through stdlib :mod:`logging` and
    ``emit_event`` through an optional :class:`~core.events.EventEmitter`.
    Both backends are optional — pass ``None`` to silence one channel
    (useful in tests where the events stream isn't wired up).

    The default logger name is ``"core.kernel.hooks"``; override
    via the ``logger`` arg if a hook wants its own namespace in the
    log output.
    """

    def __init__(
        self,
        emitter: Optional[EventEmitter] = None,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._emitter = emitter
        self._logger = logger if logger is not None else logging.getLogger(
            "core.kernel.hooks"
        )

    def warn(self, msg: str) -> None:
        self._logger.warning(msg)
        if self._emitter is not None:
            self._emitter.emit("hook_warn", msg=msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)
        if self._emitter is not None:
            self._emitter.emit("hook_error", msg=msg)

    def emit_event(self, event: str, **fields: Any) -> None:
        if self._emitter is not None:
            self._emitter.emit(event, **fields)


# ---------------------------------------------------------------------------
# Hook surface
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentHook(Protocol):
    """Hooks at agent-loop boundaries.

    The runtime calls these around each turn of
    ``make_kernel(...).run(...)``. All methods are async and all are
    optional — concrete hooks typically inherit from
    :class:`BaseAgentHook` and override only the ones they care about.

    Returning ``None`` from a mutator (``before_agent_start``,
    ``before_turn``, ``after_turn``) means "no change"; returning a
    value means "use this in place of the original".

    For ``after_turn`` specifically, the return string is appended to
    the next turn's messages as a synthetic user message — the same
    mechanism Signal uses for mid-conversation context injection.
    Convention: prefix the string with ``[validation]`` (for
    correction hooks) or ``[hook: <name>]`` (for general hooks) so
    its synthetic origin is obvious to the model.
    """

    async def before_agent_start(
        self, spec: KernelSpec
    ) -> Optional[KernelSpec]:
        """Modify the spec before the session starts, or return ``None``
        to leave it unchanged. Useful for tightening allowed-tools,
        injecting system-prompt prefixes, or switching model based on
        runtime context."""
        ...

    async def before_turn(
        self, messages: list[Message]
    ) -> Optional[list[Message]]:
        """Modify the messages going into this turn, or return ``None``
        to leave them unchanged. Useful for stripping secrets from
        outgoing prompts or injecting just-in-time context."""
        ...

    async def after_turn(self, turn_result: TurnResult) -> Optional[str]:
        """Inspect the turn's output and optionally return a string to
        inject as a synthetic user message into the next turn. Return
        ``None`` to inject nothing."""
        ...

    async def after_agent_end(self, result: KernelResult) -> None:
        """Observation only — the agent is done; nothing the hook
        returns affects the result. Useful for cleanup, archival, or
        final telemetry."""
        ...


class BaseAgentHook:
    """No-op base class for :class:`AgentHook` impls.

    Subclass this and override only the lifecycle methods you need.
    Every default impl returns ``None`` so the runtime treats it as
    "no change". The :class:`Reporter` arrives via ``__init__`` and
    lives on ``self.reporter`` for the hook's lifetime — per the
    design's "constructor injection" decision (avoids passing the
    reporter on every call).

    Subclasses that need their own ``__init__`` must call
    ``super().__init__(reporter)`` so the reporter is wired up.
    """

    def __init__(self, reporter: Reporter) -> None:
        self.reporter = reporter

    async def before_agent_start(
        self, spec: KernelSpec
    ) -> Optional[KernelSpec]:
        return None

    async def before_turn(
        self, messages: list[Message]
    ) -> Optional[list[Message]]:
        return None

    async def after_turn(self, turn_result: TurnResult) -> Optional[str]:
        return None

    async def after_agent_end(self, result: KernelResult) -> None:
        return None
