"""Per-event handlers extracted from :class:`SpeakingDaemon`.

Plan 01 of the speaking-runtime refactor (see
``docs/refactor/01-transport-plugin-interface.md``). The six former
``SpeakingDaemon._handle_*`` methods live here as module-level async
functions taking a :class:`DaemonContext` instead of ``self``. The
context type now lives in :mod:`alice_speaking.transports.base` (Phase 2)
so producers and handlers can both import it without depending on the
daemon module directly.

Behavior is preserved verbatim; only the mechanical move out of the
class scope changed.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional

from core.sdk_compat import _short

from .domain.turn_log import new_turn
from .pipeline.quiet_hours import is_quiet_hours
from .transports import ChannelRef, DaemonContext, OutboundMessage


def _vault_snapshot(ctx: DaemonContext) -> tuple[Optional[str], Optional[list[dict]]]:
    """Read the last cue-runner result off the turn runner so it can be
    attached to the next ``new_turn(...)`` entry.

    ``getattr`` defensively in case the runner shape varies in tests or
    during a partial refactor; ``last_vault_context``/
    ``last_vault_candidates`` are the canonical attributes set by
    :class:`alice_speaking.turn_runner.TurnRunner`.
    """
    runner = getattr(ctx, "turn_runner", None)
    if runner is None:
        return None, None
    return (
        getattr(runner, "last_vault_context", None),
        getattr(runner, "last_vault_candidates", None),
    )


def _tool_calls_snapshot(ctx: DaemonContext) -> list[dict]:
    """Read the structured tool calls the just-finished turn made off the
    turn runner, so they can be persisted on the ``new_turn(...)`` entry.

    Mirrors :func:`_vault_snapshot`: ``getattr`` defensively because
    older/stub runners may not set ``last_tool_calls``. Returns ``[]``
    (never ``None``) so the turn-log field stays a plain list.
    """
    runner = getattr(ctx, "turn_runner", None)
    if runner is None:
        return []
    # ``last_tool_calls`` entries carry a truncated ``input`` for the eval
    # harness; strip it here so the persisted turn log stays name+id only
    # and the ``MAX_FIELD_BYTES`` discipline is unchanged.
    return [
        {"name": tc.get("name"), "id": tc.get("id")}
        for tc in (getattr(runner, "last_tool_calls", None) or [])
    ]

if TYPE_CHECKING:
    from .daemon import (
        A2AEvent,
        CLIEvent,
        DiscordEvent,
        EmergencyEvent,
        SignalEvent,
        SurfaceEvent,
        ViewerChatEvent,
    )
    from .internal.background_task import BackgroundTaskCompleteEvent
    from .internal.cozyhem import CozyHemEvent
    from .internal.idle import IdleEvent
    from .transports.ws import WSEvent


log = logging.getLogger("alice_speaking._dispatch")


def _touch_inbound(ctx: DaemonContext, transport: str, address: str) -> None:
    """Refresh the per-channel idle tracker on every inbound message.

    Stamps ``_last_inbound[(transport, address)]`` with the current
    timestamp and discards any prior ``_idle_flushed`` flag so the
    :class:`IdleFlushSource` re-arms for the next quiet window. Called
    by every conversational inbound handler before the turn runs.

    Issue #373 / design:
    ``cortex-memory/research/2026-04-29-session-close-flush-design.md``.
    """
    key = (transport, address)
    ctx._last_inbound[key] = datetime.datetime.now().astimezone()
    ctx._idle_flushed.discard(key)


# ---------------------------------------------------------------------------
# Signal turn — no auto-capture; Alice replies via send_message.


async def handle_signal(ctx: DaemonContext, batch: list["SignalEvent"]) -> None:
    """Process a batch of one or more SignalEvents from the same sender.

    All events in the batch share the same source + sender (caller
    guarantees, via :meth:`SignalTransport._drain_batch`). One kernel
    turn handles the whole batch; the prompt enumerates each message
    in arrival order, with timestamps + attachments.
    """
    if not batch:
        return
    # SignalEvents only enter the queue when signal is enabled (the
    # producer is gated in :meth:`SpeakingDaemon.run`). The assert
    # narrows the type for the rest of the body and catches accidents.
    assert ctx.signal_transport is not None
    head = batch[0]
    sender_name = head.sender_name
    source = head.envelope.source
    # Idle tracking (issue #373): every inbound refreshes the per-channel
    # ``last seen`` timestamp and clears any prior flush flag so the
    # session-close watcher will re-arm on the next quiet window.
    _touch_inbound(ctx, "signal", source)
    quiet = is_quiet_hours(ctx.cfg.speaking)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    all_attachments = [a for ev in batch for a in ev.envelope.attachments]
    total_chars = sum(len(ev.envelope.body) for ev in batch)
    inbound_preview = (
        " ┃ ".join(_short(ev.envelope.body, 200) for ev in batch if ev.envelope.body)
        or f"({len(all_attachments)} attachment(s), no text)"
    )

    ctx.events.emit(
        "signal_turn_start",
        turn_id=turn_id,
        sender_name=sender_name,
        sender_number=source,
        message_count=len(batch),
        inbound_chars=total_chars,
        inbound=_short(inbound_preview, 600),
        attachments=[
            {
                "id": a.id,
                "path": str(a.path),
                "content_type": a.content_type,
                "filename": a.filename,
            }
            for a in all_attachments
        ],
        quiet=quiet,
    )
    if len(batch) > 1:
        log.info(
            "batched %d signal messages from %s into one turn",
            len(batch),
            sender_name,
        )

    error: Optional[str] = None
    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "signal"
    channel = ChannelRef(transport="signal", address=source, durable=True)
    ctx._current_reply_channel = channel
    ctx._current_principal_display_name = sender_name
    # Reset the drain-stopper for this turn — Alice hasn't replied
    # yet, so mid-turn injection is open. Flips True once she fires
    # send_message; reset here for the NEXT turn's allow-window.
    ctx._current_turn_replied = False
    # Replies to inbound bypass quiet hours — the user expects an
    # answer when they ask something, regardless of the clock. Typing
    # indicator fires too so they see Alice working.
    await ctx.signal_transport.typing(channel, True)
    # State machine: every inbound moves received -> replied | abandoned.
    # "received" fires immediately (per envelope) so the sender sees
    # acknowledgement before the turn starts. Default state is
    # "abandoned" — only flipped to "replied" when we actually send.
    for ev in batch:
        with contextlib.suppress(Exception):
            await ctx.signal_transport.set_message_state(
                channel, ev.envelope.timestamp, "received"
            )
    terminal_state = "abandoned"
    try:
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.signal_transport.build_prompt(
            sender_name=sender_name, stamp=stamp, batch=batch
        )
        await ctx._run_turn(prompt, turn_id=turn_id, outbound_recipient=source)
        if ctx._turn_did_send:
            terminal_state = "replied"
        else:
            # Missed-reply fallback: turn finished cleanly but Alice
            # never called send_message. Send a plain-text apology so
            # Jason doesn't see dead air. Bootstrap/compaction turns
            # use silent=True and run through _run_turn directly (not
            # this handler), so they can't trip this branch. Fallback
            # send failures are non-critical — log and move on.
            # Design: cortex-memory/research/2026-05-18-missed-reply-fallback-design.md
            fallback = (
                "I received your message but didn't have a response — "
                "could you try rephrasing?"
            )
            try:
                await ctx.signal_transport.send(
                    OutboundMessage(destination=channel, text=fallback)
                )
                terminal_state = "replied"
            except Exception:
                log.exception("missed-reply fallback send failed for %s", sender_name)
    except Exception as exc:  # noqa: BLE001
        log.exception("turn failed for %s", sender_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            # Signal turn errors bypass the quiet queue too — same rule
            # applies to error notices as to replies.
            await ctx.signal_transport.send(
                OutboundMessage(
                    destination=channel,
                    text=f"Hit an error ({type(exc).__name__}). Session preserved — reply to retry.",
                )
            )
    finally:
        # Drain any messages that arrived for this channel mid-turn
        # but didn't get injected (tool-less turn). They go back onto
        # the dispatcher queue so they become the next turn's prompt.
        with contextlib.suppress(Exception):
            ctx._flush_mid_turn_inbox(channel)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        for ev in batch:
            with contextlib.suppress(Exception):
                await ctx.signal_transport.set_message_state(
                    channel, ev.envelope.timestamp, terminal_state
                )
        await ctx.signal_transport.typing(channel, False)
        # One turn_log entry per envelope so the inbound audit trail
        # is preserved regardless of batch size. Only the LAST envelope
        # in the batch carries the outbound text — earlier envelopes
        # get None so render_for_prompt() doesn't emit duplicate
        # `[alice]` lines for what was a single reply.
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        for i, ev in enumerate(batch):
            is_last = i == len(batch) - 1
            ctx.turns.append(
                new_turn(
                    sender_number=ev.envelope.source,
                    sender_name=sender_name,
                    inbound=ev.envelope.body,
                    outbound=(ctx._turn_last_outbound if is_last else None),
                    error=error,
                    # Only the last envelope drove the actual SDK turn,
                    # so only it carries the vault retrieval that fed
                    # the prompt; earlier envelopes get None.
                    vault_context=vault_context if is_last else None,
                    vault_candidates=vault_candidates if is_last else None,
                    tool_calls=tool_calls if is_last else [],
                )
            )
        ctx.events.emit(
            "signal_turn_end",
            turn_id=turn_id,
            sender_name=sender_name,
            message_count=len(batch),
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# CLI turn — local-socket transport for terminal users + agents.


async def handle_cli(ctx: DaemonContext, event: "CLIEvent") -> None:
    """Run one turn for a CLI message and signal completion to the
    client when done.

    CLI is conversational like Signal but ephemeral — the client
    connection may have closed by the time the turn finishes. The
    :class:`CLITransport` handles missing-writer cases by logging
    and dropping; we don't need to detect them here.
    """
    assert ctx.cli_transport is not None
    msg = event.message
    # Idle tracking (issue #373) — see handle_signal note.
    _touch_inbound(ctx, "cli", msg.principal.native_id)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    ctx.events.emit(
        "cli_turn_start",
        turn_id=turn_id,
        principal_id=msg.principal.native_id,
        display_name=msg.principal.display_name,
        inbound_chars=len(msg.text),
        inbound=_short(msg.text, 600),
    )

    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "cli"
    ctx._current_reply_channel = msg.origin
    ctx._current_principal_display_name = msg.principal.display_name
    error: Optional[str] = None
    try:
        # Lifecycle: notify the client the turn has been dispatched.
        # Bracketed by ``turn_end`` from the lifecycle handler's
        # ``on_result``. No-op when ``CLI_CAPS.lifecycle_events`` is
        # False (it isn't, but the call site stays defensive).
        if ctx.cli_transport.caps.lifecycle_events:
            with contextlib.suppress(Exception):
                await ctx.cli_transport.push_lifecycle_event(
                    msg.origin, {"type": "turn_start", "turn_id": turn_id}
                )
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.cli_transport.build_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
            acts_on_behalf_of=msg.metadata.get("acts_on_behalf_of"),
        )
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=f"cli:{msg.principal.native_id}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("cli turn failed for %s", msg.principal.display_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await ctx.cli_transport.signal_error(msg.origin, error)
    finally:
        # Always tell the client the turn ended — they drain until
        # {"type":"done"} and treat "error" as non-terminal (see
        # bin/alice-client drain_one_turn). So "done" must follow on the
        # error path too; otherwise a failed/timed-out turn leaves the
        # client hanging forever waiting for a done that never comes.
        # signal_error (sent above) carries the failure; this just closes
        # the turn. Mirrors the error→done sequence the context-probe
        # path already uses.
        with contextlib.suppress(Exception):
            await ctx.cli_transport.signal_done(msg.origin)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
                vault_context=vault_context,
                vault_candidates=vault_candidates,
                tool_calls=tool_calls,
            )
        )
        ctx.events.emit(
            "cli_turn_end",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# WebSocket gateway turn — same wire vocabulary as CLI, off-host clients.


async def handle_ws(ctx: DaemonContext, event: "WSEvent") -> None:
    """Run one turn for a message arriving over the WebSocket gateway.

    Mirrors :func:`handle_cli`: ephemeral channel (the WS connection
    may have already closed by the time the turn finishes — the
    transport's :meth:`signal_done` / :meth:`signal_error` handles
    missing-connection cases by logging and dropping). The dispatch
    path is identical except for the ``ws`` turn-kind label and the
    ``ws_turn_*`` event names; everything downstream (kernel turn,
    outbox routing, vault snapshot, turn log) treats it the same way.
    """
    assert ctx.ws_transport is not None
    msg = event.message
    # Idle tracking (issue #373) — see handle_signal note.
    _touch_inbound(ctx, "ws", msg.principal.native_id)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    ctx.events.emit(
        "ws_turn_start",
        turn_id=turn_id,
        principal_id=msg.principal.native_id,
        display_name=msg.principal.display_name,
        channel=msg.origin.address,
        inbound_chars=len(msg.text),
        inbound=_short(msg.text, 600),
    )

    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "ws"
    ctx._current_reply_channel = msg.origin
    ctx._current_principal_display_name = msg.principal.display_name
    error: Optional[str] = None
    try:
        if ctx.ws_transport.caps.lifecycle_events:
            with contextlib.suppress(Exception):
                await ctx.ws_transport.push_lifecycle_event(
                    msg.origin, {"type": "turn_start", "turn_id": turn_id}
                )
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.ws_transport.build_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
        )
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=f"ws:{msg.origin.address}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ws turn failed for %s", msg.principal.display_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await ctx.ws_transport.signal_error(msg.origin, error)
    finally:
        if error is None:
            with contextlib.suppress(Exception):
                await ctx.ws_transport.signal_done(msg.origin)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
                vault_context=vault_context,
                vault_candidates=vault_candidates,
                tool_calls=tool_calls,
            )
        )
        ctx.events.emit(
            "ws_turn_end",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            channel=msg.origin.address,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Discord turn — DMs only in Phase 3b.


async def handle_discord(ctx: DaemonContext, event: "DiscordEvent") -> None:
    """Run one turn for a Discord DM. Same shape as :func:`handle_cli`
    but the channel is durable, so a missed send_message just shows up
    as silence to the user (no ``signal_done`` analog — Discord clients
    don't have a pending prompt to clear)."""
    assert ctx.discord_transport is not None
    msg = event.message
    # Idle tracking (issue #373) — see handle_signal note.
    _touch_inbound(ctx, "discord", msg.principal.native_id)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    ctx.events.emit(
        "discord_turn_start",
        turn_id=turn_id,
        principal_id=msg.principal.native_id,
        display_name=msg.principal.display_name,
        inbound_chars=len(msg.text),
        inbound=_short(msg.text, 600),
    )

    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "discord"
    ctx._current_reply_channel = msg.origin
    ctx._current_principal_display_name = msg.principal.display_name
    await ctx.discord_transport.typing(msg.origin, True)
    error: Optional[str] = None
    try:
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.discord_transport.build_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
        )
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=f"discord:{msg.principal.native_id}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("discord turn failed for %s", msg.principal.display_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await ctx.discord_transport.send(
                OutboundMessage(
                    destination=msg.origin,
                    text=(
                        f"Hit an error ({type(exc).__name__}). "
                        "Session preserved — reply to retry."
                    ),
                )
            )
    finally:
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
                vault_context=vault_context,
                vault_candidates=vault_candidates,
                tool_calls=tool_calls,
            )
        )
        ctx.events.emit(
            "discord_turn_end",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Viewer-chat turn — local web chat surfaced inside the viewer UI.


async def handle_viewer_chat(ctx: DaemonContext, event: "ViewerChatEvent") -> None:
    """Run one turn for a viewer-chat message.

    Mirrors :func:`handle_cli` — durable channel (the SSE subscriber
    can disconnect and reconnect without losing the conversation), an
    explicit ``signal_done`` at end-of-turn so the front-end can
    re-enable its input field even when Alice never called
    ``send_message``.
    """
    assert ctx.viewer_chat_transport is not None
    msg = event.message
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    ctx.events.emit(
        "viewer_chat_turn_start",
        turn_id=turn_id,
        principal_id=msg.principal.native_id,
        display_name=msg.principal.display_name,
        channel=msg.origin.address,
        inbound_chars=len(msg.text),
        inbound=_short(msg.text, 600),
    )

    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "viewer-chat"
    ctx._current_reply_channel = msg.origin
    ctx._current_principal_display_name = msg.principal.display_name
    # Same drain-stopper reset as signal/cli/discord — viewer-chat
    # participates in mid-turn injection just like the other
    # durable-channel transports.
    ctx._current_turn_replied = False
    error: Optional[str] = None
    try:
        # Lifecycle: notify subscribers the turn has been dispatched
        # — covers the "ack → first chunk" silence that previously
        # left the UI looking frozen for the whole reasoning span.
        if ctx.viewer_chat_transport.caps.lifecycle_events:
            with contextlib.suppress(Exception):
                await ctx.viewer_chat_transport.push_lifecycle_event(
                    msg.origin, {"type": "turn_start", "turn_id": turn_id}
                )
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.viewer_chat_transport.build_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
        )
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=f"viewer-chat:{msg.origin.address}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("viewer-chat turn failed for %s", msg.principal.display_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await ctx.viewer_chat_transport.signal_error(msg.origin, error)
    finally:
        # Always close the turn so the SSE consumer can re-enable input.
        # Errors close above; success closes here.
        if error is None:
            with contextlib.suppress(Exception):
                await ctx.viewer_chat_transport.signal_done(msg.origin)
        # Mid-turn flush mirrors the signal handler: drained-but-unused
        # messages roll over as the next turn's prompt.
        with contextlib.suppress(Exception):
            ctx._flush_mid_turn_inbox(msg.origin)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
                vault_context=vault_context,
                vault_candidates=vault_candidates,
                tool_calls=tool_calls,
            )
        )
        ctx.events.emit(
            "viewer_chat_turn_end",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            channel=msg.origin.address,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# A2A turn — Google Agent2Agent protocol over HTTP/JSON-RPC.


async def handle_a2a(ctx: DaemonContext, event: "A2AEvent") -> None:
    """Run one turn for an A2A task and signal completion to the SDK
    so the SSE stream gets a terminal status update. Same shape as
    :func:`handle_cli`: ephemeral channel (the per-task outbox lives
    only for the duration of the request), the daemon must always
    signal_done so the client's stream closes cleanly."""
    assert ctx.a2a_transport is not None
    msg = event.message
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    ctx.events.emit(
        "a2a_turn_start",
        turn_id=turn_id,
        principal_id=msg.principal.native_id,
        display_name=msg.principal.display_name,
        task_id=msg.origin.address,
        inbound_chars=len(msg.text),
        inbound=_short(msg.text, 600),
    )

    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    ctx._current_turn_kind = "a2a"
    ctx._current_reply_channel = msg.origin
    ctx._current_principal_display_name = msg.principal.display_name
    error: Optional[str] = None
    try:
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx.a2a_transport.build_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
        )
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=f"a2a:{msg.origin.address}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("a2a turn failed for %s", msg.principal.display_name)
        error = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await ctx.a2a_transport.signal_error(msg.origin, error)
    finally:
        # Always close the SSE stream by emitting a terminal status —
        # the SDK won't return from execute() until we do, and a hung
        # task ties up a connection. Errors close above; the success
        # path closes here.
        if error is None:
            with contextlib.suppress(Exception):
                await ctx.a2a_transport.signal_done(msg.origin)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        vault_context, vault_candidates = _vault_snapshot(ctx)
        tool_calls = _tool_calls_snapshot(ctx)
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
                vault_context=vault_context,
                vault_candidates=vault_candidates,
                tool_calls=tool_calls,
            )
        )
        ctx.events.emit(
            "a2a_turn_end",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            task_id=msg.origin.address,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Surface turn


async def handle_surface(ctx: DaemonContext, event: "SurfaceEvent") -> None:
    path = event.path
    if not path.is_file():
        # Already handled by someone else (race). Nothing to do —
        # SurfaceWatcher.handle's finally block clears the
        # dispatched-set slot whether we ran a turn or not.
        return
    body = path.read_text()
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()
    ctx.events.emit(
        "surface_dispatch",
        turn_id=turn_id,
        surface_id=path.name,
        chars=len(body),
        body=_short(body),
    )
    from prompts import load as load_prompt

    prompt = load_prompt(
        "speaking.turn.surface",
        surface_id=path.name,
        body=body,
    )
    error: Optional[str] = None
    prev_kind = ctx._current_turn_kind
    ctx._current_turn_kind = "surface"
    try:
        # Surface turns don't have a single inbound recipient; the
        # ``outbound_recipient`` is informational only. Quiet hours
        # apply here — Alice's own thoughts wait for morning.
        await ctx._run_turn(prompt, turn_id=turn_id, outbound_recipient=None)
    except Exception as exc:  # noqa: BLE001
        log.exception("surface turn failed for %s", path.name)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ctx._current_turn_kind = prev_kind
        if path.is_file():
            try:
                ctx._surface_watcher.archive_unresolved(path)
            except OSError as exc:
                log.warning("unresolved-archive failed for %s: %s", path.name, exc)
        # Dispatched-set release is owned by SurfaceWatcher.handle's
        # finally block (Phase 5 of plan 01).
        ctx.events.emit(
            "surface_turn_end",
            turn_id=turn_id,
            surface_id=path.name,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Emergency turn
#
# External monitors drop files into inner/emergency/. Emergency voice
# BYPASSES quiet hours — that's the whole point. Alice voices via
# send_message like any other turn; the daemon routes around the
# quiet-hours queue when the sender context is "emergency".


async def handle_emergency(ctx: DaemonContext, event: "EmergencyEvent") -> None:
    path = event.path
    if not path.is_file():
        # Already handled by someone else (race). Nothing to do —
        # EmergencyWatcher.handle's finally block clears the
        # dispatched-set slot whether we ran a turn or not.
        return
    body = path.read_text()
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()
    ctx.events.emit(
        "emergency_dispatch",
        turn_id=turn_id,
        emergency_id=path.name,
        chars=len(body),
        body=_short(body),
    )

    emergency_channel = ctx.address_book.emergency_recipient()
    if emergency_channel is None:
        log.error(
            "emergency %s: no signal-capable principal in address book",
            path.name,
        )
        ctx.events.emit(
            "emergency_no_recipient",
            turn_id=turn_id,
            emergency_id=path.name,
        )
        ctx._emergency_watcher.archive(
            path, verdict="no-recipient", action="daemon-archived"
        )
        return
    recipient = emergency_channel.address

    from prompts import load as load_prompt

    prompt = load_prompt(
        "speaking.turn.emergency",
        emergency_id=path.name,
        body=body,
    )

    # For this turn only, flip the emergency bypass so _send_message
    # sends directly even during quiet hours, and label the turn
    # kind so other guards know we're in emergency.
    was_emergency = getattr(ctx, "_emergency_bypass", False)
    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    ctx._emergency_bypass = True
    ctx._current_turn_kind = "emergency"
    # Emergency reply channel = the address book's emergency
    # recipient. recipient='self' on an emergency turn routes here.
    ctx._current_reply_channel = emergency_channel
    verdict = "unknown"
    action = "none"
    try:
        await ctx._run_turn(prompt, turn_id=turn_id, outbound_recipient=recipient)
        if ctx._turn_did_send:
            verdict = "voiced"
            action = f"sent to {recipient} via send_message (bypassed quiet hours)"
            ctx.events.emit(
                "emergency_voiced",
                turn_id=turn_id,
                emergency_id=path.name,
                recipient=recipient,
            )
        else:
            verdict = "downgraded"
            action = "alice did not call send_message — no evidence or false positive"
            ctx.events.emit(
                "emergency_downgraded",
                turn_id=turn_id,
                emergency_id=path.name,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("emergency turn failed for %s", path.name)
        verdict = "error"
        action = f"{type(exc).__name__}: {exc}"
        ctx.events.emit(
            "emergency_error",
            turn_id=turn_id,
            emergency_id=path.name,
            error=action,
        )
    finally:
        ctx._emergency_bypass = was_emergency
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        if path.is_file():
            ctx._emergency_watcher.archive(path, verdict=verdict, action=action)
        # Dispatched-set release is owned by EmergencyWatcher.handle's
        # finally block (Phase 5 of plan 01).
        ctx.events.emit(
            "emergency_turn_end",
            turn_id=turn_id,
            emergency_id=path.name,
            verdict=verdict,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Background-task completion turn — synthetic event from the
# dispatch_background_task tool's per-subagent waiter.


async def handle_background_task_complete(
    ctx: DaemonContext, event: "BackgroundTaskCompleteEvent"
) -> None:
    """Run a fresh turn that delivers a sub-agent's result to Alice.

    Mirrors :func:`handle_signal` / :func:`handle_emergency` shape.
    The synthetic event carries the originating channel + principal
    that were live when Alice originally dispatched, so Alice's
    ``send_message(recipient='self')`` during this turn routes back
    to whoever originally asked.

    Failure modes are surfaced in the prompt rather than swallowed —
    Alice is the right judge of what to do with a failed sub-agent
    (retry, redo manually, escalate, ignore).
    """
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()
    ctx.events.emit(
        "background_task_dispatch",
        turn_id=turn_id,
        handle=event.handle,
        description=event.description,
        is_error=event.is_error,
        result_chars=len(event.result_text or ""),
        result_preview=_short(event.result_text or "", 300),
        principal_name=event.principal_name,
        channel_transport=(event.channel.transport if event.channel else None),
    )

    # Issue #375: transition the SM v2 task to its terminal state.
    # Local import to dodge circulars at module load — auto_fix
    # imports from alice_forge which imports back here in tests.
    from . import auto_fix as _auto_fix

    _auto_fix.record_auto_fix_task_complete(
        event.handle, event.result_text or "", is_error=event.is_error
    )

    framing = "failed" if event.is_error else "completed"
    body = (event.result_text or "").strip() or "(sub-agent returned no text)"
    prompt = (
        f"Background task `{event.handle}` ({event.description!r}) "
        f"{framing}. You dispatched it earlier on behalf of "
        f"{event.principal_name}; here's the sub-agent's final output:\n\n"
        f"{body}\n\n"
        f"Decide what (if anything) to forward to "
        f"{event.principal_name}. Use send_message(recipient='self') "
        f"to reply on the originating channel — it's restored for "
        f"this turn."
    )

    error: Optional[str] = None
    prev_kind = ctx._current_turn_kind
    prev_channel = ctx._current_reply_channel
    prev_display_name = ctx._current_principal_display_name
    # Treat the completion turn like its originating channel so the
    # quiet-hours bypass + outbound routing match what the original
    # request would have used. If channel is None (originating turn
    # had no channel — e.g. a surface), fall through with no kind so
    # quiet hours still apply to outbound.
    if event.channel is not None:
        ctx._current_reply_channel = event.channel
        ctx._current_turn_kind = event.channel.transport
    ctx._current_principal_display_name = event.principal_name
    try:
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=(event.channel.address if event.channel else None),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "background_task completion turn failed for handle %s",
            event.handle,
        )
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        ctx.events.emit(
            "background_task_turn_end",
            turn_id=turn_id,
            handle=event.handle,
            description=event.description,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# CozyHem event handler — routes typed home-automation events by ``kind``.


async def handle_cozyhem_event(ctx: DaemonContext, event: "CozyHemEvent") -> None:
    """Dispatch one :class:`CozyHemEvent` by its ``kind``.

    First concrete consumer is ``doorbell_pressed`` (cozyhem-engine
    PR 2 / PR 3 of the doorbell chain): notify the address book's
    emergency recipient (Jason, in production) with a short ping.
    Quiet hours are bypassed — a doorbell ring is the kind of thing
    you want to know about even at 2am.

    Unknown kinds are logged + dropped on purpose: cozyhem-engine
    may emit event types this build of Alice doesn't know about
    yet, and silently dropping them is preferable to crashing the
    consumer. Adding a new kind = adding a new branch here.
    """
    ctx.events.emit(
        "cozyhem_event",
        kind=event.kind,
        entity_id=event.entity_id,
        received_at=event.received_at,
    )

    if event.kind == "doorbell_pressed":
        await _handle_doorbell_pressed(ctx, event)
        return

    log.info(
        "cozyhem: dropping unknown event kind=%r (entity_id=%s)",
        event.kind,
        event.entity_id,
    )


async def _handle_doorbell_pressed(
    ctx: DaemonContext, event: "CozyHemEvent"
) -> None:
    """Send a doorbell-press ping to the emergency recipient.

    Direct send rather than a kernel turn: v1 is fire-and-forget
    "someone's at the door." Future iterations (snapshot fetch,
    image classification) will add a turn so Alice can decide what
    to say based on who's there.
    """
    recipient = ctx.address_book.emergency_recipient()
    if recipient is None:
        log.warning(
            "cozyhem doorbell_pressed: no emergency recipient configured; "
            "skipping notification"
        )
        ctx.events.emit(
            "cozyhem_doorbell_no_recipient",
            entity_id=event.entity_id,
        )
        return

    text = "Doorbell pressed."
    entity_id = event.entity_id or ""
    if entity_id:
        text = f"Doorbell pressed ({entity_id})."
    try:
        await ctx._dispatch_outbound(
            recipient,
            text,
            None,
            emergency=False,
            bypass_quiet=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("cozyhem doorbell_pressed: send failed")
        ctx.events.emit(
            "cozyhem_doorbell_send_error",
            entity_id=event.entity_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return
    ctx.events.emit(
        "cozyhem_doorbell_voiced",
        entity_id=event.entity_id,
        recipient=recipient.address,
    )


# ---------------------------------------------------------------------------
# Idle-flush turn — silent session-close flush.
#
# Fires when a conversational channel has been quiet for
# ``session_close_timeout_minutes`` (default 10, hot-reloadable from
# ``alice.config.json``). Issue #373 / design:
# ``cortex-memory/research/2026-04-29-session-close-flush-design.md``.
#
# Runs ``_run_turn(..., silent=True)``: no outbound channel, no
# ``send_message`` budget, no missed_reply event, no compaction
# arming. The kernel's only job is to drop any open observations as
# ``append_note`` calls into ``inner/notes/`` so Thinking can drain
# them on her next wake. Valid outcome is a no-op turn.


async def handle_idle(ctx: DaemonContext, event: "IdleEvent") -> None:
    """Run one silent session-close flush turn for an idle channel."""
    now = datetime.datetime.now().astimezone()
    idle_min = int((now - event.idle_since).total_seconds() / 60)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()
    ctx.events.emit(
        "session_close_flush_start",
        turn_id=turn_id,
        sender_name=event.sender_name,
        idle_minutes=idle_min,
    )
    prompt = (
        f"[{event.sender_name}'s conversation idle {idle_min}m. "
        "Session-close flush.] "
        "Run lightweight flush — ≤3 tool calls: write open observations "
        "via append_note to inner/notes/ or drop surface-threshold "
        "insights in inner/surface/. No send_message. Valid outcome: no-op."
    )
    error: Optional[str] = None
    prev_kind = ctx._current_turn_kind
    ctx._current_turn_kind = "idle_flush"
    try:
        await ctx._run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=None,
            silent=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("idle flush failed for %s", event.sender_name)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ctx._current_turn_kind = prev_kind
        ctx.events.emit(
            "session_close_flush_end",
            turn_id=turn_id,
            sender_name=event.sender_name,
            idle_minutes=idle_min,
            error=error,
            duration_ms=int((time.time() - started) * 1000),
        )
