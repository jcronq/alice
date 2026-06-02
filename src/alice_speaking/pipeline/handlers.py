"""BlockHandlers for the speaking daemon.

Compose-time extensions to :class:`core.kernel.Kernel` that
encode speaking-specific semantics the kernel doesn't know about:

- :class:`SessionHandler` — on each :class:`TurnSummary`, update the
  daemon's session_id and (unless silent) persist it to ``session.json``
  so the next process start can ``resume=`` warm.
- :class:`CompactionArmer` — on each :class:`TurnSummary`, arm the
  compaction flag if ``usage.input_tokens`` crossed the threshold.

The missed-reply detector is NOT a handler. Whether a turn produced
outbound is determined by whether Alice's ``send_message`` tool callback
fired on the daemon — not by observing ``tool_use`` blocks — because the
tool invocation could legally error out between block and callback. The
daemon still tracks that via ``self._turn_did_send``.

Handlers receive backend-agnostic types (:class:`TurnSummary`,
:class:`SystemEvent`) — they work unchanged across AnthropicKernel
and PiKernel.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Callable

from core.kernel import NullHandler, TurnSummary
from core import session as session_state

from . import compaction as compaction_module  # sibling within pipeline/


log = logging.getLogger(__name__)


# Per-tool "primary" parameter — the one humans care about at a glance.
# Anything not in this map falls back to the first stringy key.
_PRIMARY_PARAM: dict = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Task": "description",
}

_PRIMARY_MAX_CHARS = 80


def _trim_input(name: str, input) -> dict | None:
    """Pull the most useful single field out of a tool's input for the
    CLI trace stream. Returns ``{key: short_value}`` or ``None`` when
    there's nothing meaningful to show. Bounded length keeps the wire
    event small and the TUI's one-line summary readable.
    """
    if not isinstance(input, dict) or not input:
        return None
    primary = _PRIMARY_PARAM.get(name)
    # Fallback: first key whose value is a non-empty string.
    if primary is None or primary not in input:
        primary = next(
            (k for k, v in input.items() if isinstance(v, str) and v),
            None,
        )
    if primary is None:
        keys = ",".join(list(input.keys())[:4])
        return {"args": keys} if keys else None
    val = str(input.get(primary, ""))
    if len(val) > _PRIMARY_MAX_CHARS:
        val = val[: _PRIMARY_MAX_CHARS - 1] + "…"
    return {primary: val}


class SessionHandler(NullHandler):
    """Update the daemon's session_id on each turn's result.

    When ``persist=True``, also writes ``session.json`` so a process
    restart can resume warm. Silent turns (bootstrap, compaction) use
    ``persist=False`` — we still track the active session_id in memory
    so later turns pass ``resume=``, but we don't flap the file across a
    compaction roll.
    """

    def __init__(
        self,
        *,
        session_path: pathlib.Path,
        set_session_id: Callable[[str], None],
        persist: bool,
    ) -> None:
        self._session_path = session_path
        self._set_session_id = set_session_id
        self._persist = persist

    async def on_result(self, summary: TurnSummary) -> None:
        if not summary.session_id:
            return
        self._set_session_id(summary.session_id)
        if not self._persist:
            return
        try:
            session_state.write(self._session_path, summary.session_id)
        except OSError:
            log.exception("failed to persist session_id to %s", self._session_path)


class CompactionArmer(NullHandler):
    """Arm the daemon's compaction flag when ``input_tokens`` crosses
    the configured threshold.

    The flag is checked by the consumer loop *before the next event* —
    so the current turn always completes normally; compaction happens in
    the gap between turns, not mid-turn.
    """

    def __init__(
        self,
        *,
        threshold: int,
        arm: Callable[[], None],
    ) -> None:
        self._threshold = threshold
        self._arm = arm

    async def on_result(self, summary: TurnSummary) -> None:
        if not summary.usage:
            return
        # should_compact still takes a dict for backward-compat with its
        # existing tests (they fuzz with malformed input). UsageInfo is
        # the canonical typed shape; convert at the boundary.
        usage_dict = {
            "input_tokens": summary.usage.input_tokens,
            "output_tokens": summary.usage.output_tokens,
            "cache_read_input_tokens": summary.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": summary.usage.cache_creation_input_tokens,
            "iterations": summary.usage.iterations,
        }
        if compaction_module.should_compact(usage_dict, self._threshold):
            self._arm()
            iterations = summary.usage.iterations or []
            last = iterations[-1] if iterations else None
            if last:
                effective = (
                    (last.get("input_tokens") or 0)
                    + (last.get("cache_read_input_tokens") or 0)
                    + (last.get("cache_creation_input_tokens") or 0)
                )
                log.info(
                    "compaction armed (last-call effective=%d > threshold=%d; "
                    "iterations=%d cache_read=%s cache_create=%s)",
                    effective,
                    self._threshold,
                    len(iterations),
                    last.get("cache_read_input_tokens"),
                    last.get("cache_creation_input_tokens"),
                )
            else:
                effective = (
                    (summary.usage.input_tokens or 0)
                    + (summary.usage.cache_read_input_tokens or 0)
                    + (summary.usage.cache_creation_input_tokens or 0)
                )
                log.info(
                    "compaction armed (cumulative effective=%d > threshold=%d; "
                    "no iterations breakdown — input=%s cache_read=%s cache_create=%s)",
                    effective,
                    self._threshold,
                    summary.usage.input_tokens,
                    summary.usage.cache_read_input_tokens,
                    summary.usage.cache_creation_input_tokens,
                )


class CLITraceHandler(NullHandler):
    """Forward tool_use + result events to a connected CLI client.

    Lets a TUI (e.g. bin/alice-tui) render Claude-Code-style tool
    indicators and per-turn cost/duration footers. The handler is a
    no-op when the active reply channel isn't a CLI channel — safe to
    install unconditionally.

    The transport's push_trace handles the "client disconnected
    mid-turn" case silently.
    """

    def __init__(
        self,
        *,
        transport,
        get_channel: Callable[[], object],
    ) -> None:
        self._transport = transport
        self._get_channel = get_channel

    def _cli_channel(self):
        ch = self._get_channel()
        if ch is None:
            return None
        if getattr(ch, "transport", None) != "cli":
            return None
        return ch

    async def on_tool_use(self, name: str, input, id: str) -> None:
        ch = self._cli_channel()
        if ch is None:
            return
        await self._transport.push_trace(
            ch,
            {"type": "tool_use", "name": name, "input": _trim_input(name, input)},
        )

    async def on_tool_result(self, tool_use_id: str, content, is_error: bool) -> None:
        ch = self._cli_channel()
        if ch is None:
            return
        await self._transport.push_trace(
            ch,
            {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error},
        )

    async def on_result(self, summary: TurnSummary) -> None:
        ch = self._cli_channel()
        if ch is None:
            return
        evt: dict = {"type": "result"}
        if summary.cost_usd is not None:
            evt["total_cost_usd"] = summary.cost_usd
        if summary.duration_ms is not None:
            evt["duration_ms"] = summary.duration_ms
        await self._transport.push_trace(ch, evt)


class TurnLifecycleHandler(NullHandler):
    """Bridge kernel block events to per-transport lifecycle broadcasts.

    Emits an additive event vocabulary that complements the existing
    ``ack`` / ``chunk`` / ``done`` wire so streaming clients (CLI TUI,
    viewer-chat SSE) can render the in-flight state of a turn —
    "Alice is thinking…", "Calling Read…", progressive text — instead
    of staring at a frozen UI for the whole reasoning + tool span.

    Wire vocabulary (per `2026-05-11-turn-lifecycle-and-token-streaming-design`):

    - ``turn_start`` (emitted by ``_dispatch``, not this handler)
    - ``tool_call_start`` / ``tool_call_end`` — bracket each tool block
    - ``text_start`` / ``text_chunk`` / ``text_end`` — bracket the
      assistant's visible text across the whole turn
    - ``thinking_start`` / ``thinking_chunk`` / ``thinking_end`` —
      bracket reasoning text across the whole turn
    - ``turn_end`` — fires after ``on_result``

    The handler is a no-op when the active reply channel's transport
    doesn't advertise ``caps.lifecycle_events=True`` (Signal, Discord,
    A2A), so installing unconditionally is safe.

    Note on tool boundaries: ``on_tool_use`` opens the span with
    ``tool_call_start``; ``on_tool_result`` closes it with
    ``tool_call_end`` carrying the matching ``tool_use_id`` and the
    ``is_error`` flag. Both kernels surface the result boundary now
    (AnthropicKernel extracts the ToolResultBlock from the follow-up
    user message; PiKernel maps ``tool_execution_end``), so the UI can
    bracket each tool exactly instead of inferring the end from the
    next event.
    """

    def __init__(
        self,
        *,
        transport_for: Callable[[str], object],
        get_channel: Callable[[], object],
    ) -> None:
        self._transport_for = transport_for
        self._get_channel = get_channel
        self._text_open = False
        self._thinking_open = False

    def _active_transport(self):
        """Return the transport for the active channel iff it opts into
        lifecycle events, else ``None``."""
        ch = self._get_channel()
        if ch is None:
            return None, None
        transport = self._transport_for(getattr(ch, "transport", None))
        if transport is None:
            return None, None
        caps = getattr(transport, "caps", None)
        if not getattr(caps, "lifecycle_events", False):
            return None, None
        return transport, ch

    async def _push(self, event: dict) -> None:
        transport, ch = self._active_transport()
        if transport is None or ch is None:
            return
        push = getattr(transport, "push_lifecycle_event", None)
        if push is None:
            return
        try:
            await push(ch, event)
        except Exception:  # noqa: BLE001
            # Don't let a transport hiccup break the kernel loop — the
            # lifecycle stream is UX gravy, not a correctness contract.
            log.exception("push_lifecycle_event failed")

    async def on_text(self, text: str) -> None:
        if not text:
            return
        if not self._text_open:
            self._text_open = True
            await self._push({"type": "text_start"})
        await self._push({"type": "text_chunk", "text": text})

    async def on_tool_use(self, name: str, input, id: str) -> None:
        await self._push(
            {
                "type": "tool_call_start",
                "tool_use_id": id,
                "name": name,
                "input": _trim_input(name, input),
            }
        )

    async def on_tool_result(self, tool_use_id: str, content, is_error: bool) -> None:
        await self._push(
            {
                "type": "tool_call_end",
                "tool_use_id": tool_use_id,
                "is_error": is_error,
            }
        )

    async def on_thinking(self, text: str) -> None:
        if not text:
            return
        if not self._thinking_open:
            self._thinking_open = True
            await self._push({"type": "thinking_start"})
        await self._push({"type": "thinking_chunk", "text": text})

    async def on_result(self, summary: TurnSummary) -> None:
        if self._thinking_open:
            await self._push({"type": "thinking_end"})
            self._thinking_open = False
        if self._text_open:
            await self._push({"type": "text_end"})
            self._text_open = False
        await self._push({"type": "turn_end"})


__all__ = [
    "SessionHandler",
    "CompactionArmer",
    "CLITraceHandler",
    "TurnLifecycleHandler",
]
