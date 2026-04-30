"""BlockHandlers for the speaking daemon.

Compose-time extensions to :class:`alice_core.kernel.AgentKernel` that
encode speaking-specific semantics the kernel doesn't know about:

- :class:`SessionHandler` — on each ``ResultMessage``, update the
  daemon's session_id and (unless silent) persist it to ``session.json``
  so the next process start can ``resume=`` warm.
- :class:`CompactionArmer` — on each ``ResultMessage``, arm the
  compaction flag if ``usage.input_tokens`` crossed the threshold.

The missed-reply detector is NOT a handler. Whether a turn produced
outbound is determined by whether Alice's ``send_message`` tool callback
fired on the daemon — not by observing ``tool_use`` blocks — because the
tool invocation could legally error out between block and callback. The
daemon still tracks that via ``self._turn_did_send``.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Callable

from alice_core.kernel import NullHandler
from alice_core import session as session_state

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

    async def on_result(self, msg) -> None:
        if not msg.session_id:
            return
        self._set_session_id(msg.session_id)
        if not self._persist:
            return
        try:
            session_state.write(self._session_path, msg.session_id)
        except OSError:
            log.exception(
                "failed to persist session_id to %s", self._session_path
            )


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

    async def on_result(self, msg) -> None:
        if not msg.usage:
            return
        if compaction_module.should_compact(msg.usage, self._threshold):
            self._arm()
            effective = (
                (msg.usage.get("input_tokens") or 0)
                + (msg.usage.get("cache_read_input_tokens") or 0)
                + (msg.usage.get("cache_creation_input_tokens") or 0)
            )
            log.info(
                "compaction armed (effective_tokens=%d > threshold=%d; "
                "input=%s cache_read=%s cache_create=%s)",
                effective,
                self._threshold,
                msg.usage.get("input_tokens"),
                msg.usage.get("cache_read_input_tokens"),
                msg.usage.get("cache_creation_input_tokens"),
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

    async def on_result(self, msg) -> None:
        ch = self._cli_channel()
        if ch is None:
            return
        evt: dict = {"type": "result"}
        cost = getattr(msg, "total_cost_usd", None)
        if cost is not None:
            evt["total_cost_usd"] = cost
        dur = getattr(msg, "duration_ms", None)
        if dur is not None:
            evt["duration_ms"] = dur
        await self._transport.push_trace(ch, evt)


__all__ = ["SessionHandler", "CompactionArmer", "CLITraceHandler"]
