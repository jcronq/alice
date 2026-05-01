"""Backend-agnostic types for the kernel layer.

Every Kernel impl (anthropic, pi) takes the SAME :class:`KernelSpec`,
returns the SAME :class:`KernelResult`, and feeds handlers the SAME
:class:`TurnSummary` + :class:`SystemEvent`. SDK-specific types
(Anthropic's ``ResultMessage``, pi's ``agent_end`` event) are
translated by each impl before crossing the abstraction boundary.

Adding a new backend means writing a translator from native types
to these dataclasses. Agent code (turn_runner, wake) never touches
backend types directly.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


__all__ = [
    "ThinkingLevel",
    "KernelSpec",
    "KernelResult",
    "UsageInfo",
    "TurnSummary",
    "SystemEvent",
]


# Normalized thinking-effort enum. Each Kernel impl translates to its
# native shape (Anthropic: ThinkingConfig dict; pi: --thinking flag).
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class UsageInfo:
    """Normalized token usage.

    Field names mirror Anthropic's wire format because existing
    event-log consumers (``alice_viewer.aggregators._usage_breakdown``)
    parse those exact keys from JSONL. Pi-side usage dicts map to
    these names in :func:`alice_pi.usage.pi_usage_to_info`.
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class KernelSpec:
    """Inputs to one :meth:`Kernel.run` invocation. Backend-agnostic.

    Each Kernel impl translates the fields to its native option
    shape. Callers populate this dataclass using only the documented
    field types — never construct SDK-specific dicts here.

    ``mcp_servers`` is honored only by AnthropicKernel; PiKernel
    ignores it (pi has no built-in MCP).

    ``thinking`` is a normalized effort level; AnthropicKernel maps
    it to ``ThinkingConfig`` and PiKernel maps it to its
    ``--thinking`` flag.
    """

    model: str
    allowed_tools: list[str] = field(default_factory=list)
    cwd: Optional[pathlib.Path] = None
    resume: Optional[str] = None
    max_seconds: int = 0  # 0 or negative = unbounded
    thinking: Optional[ThinkingLevel] = None
    append_system_prompt: Optional[str] = None
    mcp_servers: Optional[dict] = None  # Anthropic-specific; PiKernel ignores
    add_dirs: Optional[list[pathlib.Path]] = None


@dataclass
class KernelResult:
    """What :meth:`Kernel.run` returns. Backend-agnostic.

    ``text`` is the concatenated assistant text content. ``usage``
    is the normalized token-usage summary; may be ``None`` if the
    turn errored before producing one. ``cost_usd`` is ``None`` for
    subscription-billed backends (pi-codex via ChatGPT subscription
    can't surface a real USD cost). ``error`` is set on timeout or
    internal kernel error; the caller decides whether to retry.
    """

    text: str
    session_id: Optional[str]
    usage: Optional[UsageInfo]
    duration_ms: Optional[int]
    cost_usd: Optional[float]
    is_error: bool
    num_turns: Optional[int]
    error: Optional[str] = None  # "timeout" | None — extend as cases come up


@dataclass(frozen=True)
class TurnSummary:
    """``a turn just finished`` payload for handlers.

    Replaces backend-specific message types (Anthropic's
    ``ResultMessage``, pi's ``turn_end``) in
    :meth:`BlockHandler.on_result`. ``raw`` is the original backend
    object as an escape hatch — handlers should NOT rely on its
    shape; if you need a field, surface it on this dataclass.
    """

    session_id: Optional[str]
    usage: Optional[UsageInfo]
    duration_ms: Optional[int]
    cost_usd: Optional[float]
    is_error: bool
    num_turns: Optional[int]
    raw: Any = None


@dataclass(frozen=True)
class SystemEvent:
    """System message normalized for handlers."""

    subtype: str
    data: dict
