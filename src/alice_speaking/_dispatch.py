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

from alice_core.sdk_compat import _short

from .quiet_hours import is_quiet_hours
from .transports import ChannelRef, DaemonContext, OutboundMessage
from .turn_log import new_turn

if TYPE_CHECKING:
    from .daemon import (
        A2AEvent,
        CLIEvent,
        DiscordEvent,
        EmergencyEvent,
        SignalEvent,
        SurfaceEvent,
    )


log = logging.getLogger("alice_speaking._dispatch")


# ---------------------------------------------------------------------------
# Signal turn — no auto-capture; Alice replies via send_message.


async def handle_signal(ctx: DaemonContext, batch: list["SignalEvent"]) -> None:
    """Process a batch of one or more SignalEvents from the same sender.

    All events in the batch share the same source + sender (caller
    guarantees, via :meth:`SpeakingDaemon._drain_signal_batch`). One
    kernel turn handles the whole batch; the prompt enumerates each
    message in arrival order, with timestamps + attachments.
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
    quiet = is_quiet_hours(ctx.cfg.speaking)
    turn_id = uuid.uuid4().hex[:12]
    started = time.time()

    all_attachments = [a for ev in batch for a in ev.envelope.attachments]
    total_chars = sum(len(ev.envelope.body) for ev in batch)
    inbound_preview = " ┃ ".join(
        _short(ev.envelope.body, 200) for ev in batch if ev.envelope.body
    ) or f"({len(all_attachments)} attachment(s), no text)"

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
        prompt = ctx._build_signal_prompt(
            sender_name=sender_name, stamp=stamp, batch=batch
        )
        await ctx._run_turn(prompt, turn_id=turn_id, outbound_recipient=source)
        if ctx._turn_did_send:
            terminal_state = "replied"
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
        for i, ev in enumerate(batch):
            ctx.turns.append(
                new_turn(
                    sender_number=ev.envelope.source,
                    sender_name=sender_name,
                    inbound=ev.envelope.body,
                    outbound=(
                        ctx._turn_last_outbound
                        if i == len(batch) - 1
                        else None
                    ),
                    error=error,
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
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = ctx._build_cli_prompt(
            principal_name=msg.principal.display_name,
            stamp=stamp,
            text=msg.text,
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
        # Always tell the client the turn ended, even if Alice never
        # called send_message — they need {"type":"done"} to know to
        # prompt the user again. Errors are sent above, so we only
        # send "done" on the success path.
        if error is None:
            with contextlib.suppress(Exception):
                await ctx.cli_transport.signal_done(msg.origin)
        ctx._current_turn_kind = prev_kind
        ctx._current_reply_channel = prev_channel
        ctx._current_principal_display_name = prev_display_name
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
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
# Discord turn — DMs only in Phase 3b.


async def handle_discord(ctx: DaemonContext, event: "DiscordEvent") -> None:
    """Run one turn for a Discord DM. Same shape as :func:`handle_cli`
    but the channel is durable, so a missed send_message just shows up
    as silence to the user (no ``signal_done`` analog — Discord clients
    don't have a pending prompt to clear)."""
    assert ctx.discord_transport is not None
    msg = event.message
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
        prompt = ctx._build_discord_prompt(
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
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
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
        prompt = ctx._build_a2a_prompt(
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
        ctx.turns.append(
            new_turn(
                sender_number=msg.principal.native_id,
                sender_name=msg.principal.display_name,
                inbound=msg.text,
                outbound=ctx._turn_last_outbound,
                error=error,
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
        # Already handled by someone else (race). Nothing to do.
        ctx._dispatched_surfaces.discard(path.name)
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
    prompt = (
        f"[Internal — a thought just surfaced from reflection: {path.name}]\n\n"
        f"{body}\n\n"
        "This is your own thought that just came to you. Decide what to do: "
        "voice it to the user via the `send_message` tool, file it into "
        "memory, reply to thinking via a note (append_note), or let it "
        "pass. When you've decided, call mcp__alice__resolve_surface with "
        "the file's `id` (its filename), a short `verdict`, and "
        "`action_taken`. If you voice it, call send_message BEFORE "
        "resolve_surface."
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
                ctx._archive_unresolved(path)
            except OSError as exc:
                log.warning("unresolved-archive failed for %s: %s", path.name, exc)
        ctx._dispatched_surfaces.discard(path.name)
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
        ctx._dispatched_emergencies.discard(path.name)
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
        ctx._archive_emergency(path, verdict="no-recipient", action="daemon-archived")
        return
    recipient = emergency_channel.address

    prompt = (
        f"[EMERGENCY — signal from an external monitor: {path.name}]\n\n"
        f"{body}\n\n"
        "Review this emergency. Verify the frontmatter contains "
        "`evidence_paths` with at least one verifiable source. If the "
        "evidence is insufficient, do NOT call send_message — let the "
        "turn close and the daemon will archive it as downgraded.\n\n"
        "If the emergency is real, call `send_message` to voice it. "
        "Your send_message call during an emergency bypasses quiet "
        "hours automatically. Be concise and direct — name the "
        "emergency, the evidence, and the recommended action in one "
        "short message."
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
        await ctx._run_turn(
            prompt, turn_id=turn_id, outbound_recipient=recipient
        )
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
            ctx._archive_emergency(path, verdict=verdict, action=action)
        ctx._dispatched_emergencies.discard(path.name)
        ctx.events.emit(
            "emergency_turn_end",
            turn_id=turn_id,
            emergency_id=path.name,
            verdict=verdict,
            duration_ms=int((time.time() - started) * 1000),
        )
