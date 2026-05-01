"""Translate pi-coding-agent JSONL events to Alice-shaped handler
calls + a final :class:`KernelResult`.

Event vocabulary mapping (see ``docs/refactor/09-spike-pi-coding-agent.md``):

| Pi event                       | Alice action                                 |
|--------------------------------|----------------------------------------------|
| ``session``                    | record session_id + cwd                      |
| ``agent_start``                | (no-op; lifecycle marker)                    |
| ``turn_start``                 | (no-op; lifecycle marker)                    |
| ``message_update`` text_delta  | accumulate text → ``on_text(delta)``         |
| ``message_update`` text_end    | (no-op; final block already accumulated)     |
| ``message_update`` thinking    | ``on_thinking(text)``                        |
| ``tool_execution_start``       | emit ``tool_use`` + ``on_tool_use(...)``     |
| ``tool_execution_end``         | (no-op for now; no on_tool_result yet)       |
| ``message_end`` (assistant)    | capture usage + cost from ``message.usage``  |
| ``turn_end``                   | last seen turn → fed to KernelResult         |
| ``agent_end``                  | terminal; build KernelResult                 |
| ``compaction_*``               | emit ``compaction`` event                    |
| ``auto_retry_*``               | emit ``auto_retry`` event                    |
| ``error``                      | raise RuntimeError(message)                  |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from alice_core.kernel import (
    BlockHandler,
    KernelResult,
    SystemEvent,
    TurnSummary,
    UsageInfo,
)

from .usage import pi_usage_to_info


__all__ = ["PiEventTranslator"]


# Truncation cap propagated from PiKernel.
_DEFAULT_CAP = 2000


def _short(value: Any, cap: int = _DEFAULT_CAP) -> Any:
    """Mirror :func:`alice_core.sdk_compat._short` for stringification."""
    import json as _json

    try:
        text = (
            value
            if isinstance(value, str)
            else _json.dumps(value, default=str, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


@dataclass
class _State:
    parts: list[str] = field(default_factory=list)
    session_id: Optional[str] = None
    usage: Optional[UsageInfo] = None
    duration_ms: Optional[int] = None
    is_error: bool = False
    error_message: Optional[str] = None
    num_turns: int = 0
    finished: bool = False
    # Last assistant message timestamp (ms), for duration calc.
    first_message_ms: Optional[int] = None
    last_message_ms: Optional[int] = None


class PiEventTranslator:
    """Stateful processor for pi --mode json output.

    Constructed once per :meth:`PiKernel.run` call. Driven by the
    pi event loop; each event in turn invokes the right handler
    methods and updates internal state. Call :meth:`to_kernel_result`
    once the stream is drained (or a timeout fires).
    """

    def __init__(
        self,
        emit: Callable[..., None],
        *,
        short_cap: int = _DEFAULT_CAP,
    ) -> None:
        self._emit = emit
        self._cap = short_cap
        self._state = _State()

    @property
    def session_id(self) -> Optional[str]:
        return self._state.session_id

    @property
    def is_error(self) -> bool:
        return self._state.is_error

    async def handle(
        self,
        event: dict,
        handlers: list[BlockHandler],
    ) -> None:
        kind = event.get("type")
        if kind == "session":
            self._state.session_id = event.get("id")
            self._emit(
                "system",
                subtype="pi.session",
                data={
                    "id": event.get("id"),
                    "cwd": event.get("cwd"),
                    "version": event.get("version"),
                },
            )
            sysev = SystemEvent(subtype="pi.session", data=dict(event))
            for h in handlers:
                await h.on_system(sysev)
            return

        if kind == "agent_start" or kind == "turn_start":
            return

        if kind == "message_update":
            await self._handle_message_update(event, handlers)
            return

        if kind == "message_end":
            await self._handle_message_end(event, handlers)
            return

        if kind == "tool_execution_start":
            name = event.get("toolName") or "unknown"
            args = event.get("args") or {}
            tcid = event.get("toolCallId") or ""
            self._emit(
                "tool_use",
                name=name,
                input=_short(args, self._cap),
                id=tcid,
            )
            for h in handlers:
                await h.on_tool_use(name, args, tcid)
            return

        if kind == "tool_execution_end":
            # Pi reports the result; no on_tool_result hook today
            # (Anthropic side doesn't have one either — tool results
            # arrive as the next user message). Leave for now.
            return

        if kind == "turn_end":
            self._state.num_turns += 1
            return

        if kind == "agent_end":
            self._state.finished = True
            return

        if kind in ("compaction_start", "compaction_end"):
            self._emit("compaction", phase=kind, **{
                k: v for k, v in event.items() if k not in ("type",)
            })
            return

        if kind in ("auto_retry_start", "auto_retry_end"):
            self._emit("auto_retry", phase=kind, **{
                k: v for k, v in event.items() if k not in ("type",)
            })
            return

        if kind == "error":
            self._state.is_error = True
            self._state.error_message = (
                event.get("message")
                or event.get("error")
                or "pi reported an error"
            )
            raise RuntimeError(f"pi error: {self._state.error_message}")

        # Unknown event type — emit verbatim under a "pi.unknown"
        # subtype so we can iterate on the translator without losing
        # diagnostics.
        self._emit(
            "system", subtype=f"pi.{kind}", data={"raw": _short(event, self._cap)}
        )

    async def _handle_message_update(
        self, event: dict, handlers: list[BlockHandler]
    ) -> None:
        ame = event.get("assistantMessageEvent") or {}
        ame_kind = ame.get("type")
        if ame_kind == "text_delta":
            delta = ame.get("delta") or ""
            if not delta:
                return
            self._state.parts.append(delta)
            self._emit("assistant_text", text=_short(delta, self._cap))
            for h in handlers:
                await h.on_text(delta)
        elif ame_kind == "thinking_delta":
            delta = ame.get("delta") or ""
            if not delta:
                return
            self._emit("thinking", text=_short(delta, self._cap))
            for h in handlers:
                await h.on_thinking(delta)
        # text_start / text_end / thinking_start / thinking_end are
        # framing markers; no action needed beyond text_delta.

    async def _handle_message_end(
        self, event: dict, handlers: list[BlockHandler]
    ) -> None:
        msg = event.get("message") or {}
        if msg.get("role") != "assistant":
            return

        usage_dict = msg.get("usage")
        usage_info = pi_usage_to_info(usage_dict)
        if usage_info is not None:
            self._state.usage = usage_info

        ts_ms = msg.get("timestamp")
        if isinstance(ts_ms, int):
            if self._state.first_message_ms is None:
                self._state.first_message_ms = ts_ms
            self._state.last_message_ms = ts_ms

        # Derive duration when both endpoints known.
        if (
            self._state.first_message_ms is not None
            and self._state.last_message_ms is not None
        ):
            self._state.duration_ms = (
                self._state.last_message_ms - self._state.first_message_ms
            )

        self._emit(
            "result",
            session_id=self._state.session_id,
            num_turns=self._state.num_turns + 1,
            duration_ms=self._state.duration_ms,
            total_cost_usd=None,
            is_error=False,
            usage=self._usage_event_dict(),
        )
        summary = TurnSummary(
            session_id=self._state.session_id,
            usage=usage_info,
            duration_ms=self._state.duration_ms,
            cost_usd=None,
            is_error=False,
            num_turns=self._state.num_turns + 1,
            raw=msg,
        )
        for h in handlers:
            await h.on_result(summary)

    def _usage_event_dict(self) -> Optional[dict]:
        if self._state.usage is None:
            return None
        import dataclasses

        return dataclasses.asdict(self._state.usage)

    def to_kernel_result(
        self, *, error: Optional[str] = None, is_error: bool = False
    ) -> KernelResult:
        return KernelResult(
            text="".join(self._state.parts).strip(),
            session_id=self._state.session_id,
            usage=self._state.usage,
            duration_ms=self._state.duration_ms,
            cost_usd=None,
            is_error=is_error or self._state.is_error,
            num_turns=self._state.num_turns,
            error=error,
        )
