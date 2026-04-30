"""Outbound dispatch for the speaking daemon.

Plan 01 Phase 6a extracts the per-event-loop routing logic from
``SpeakingDaemon`` into :class:`OutboxRouter`. One source of truth
for:

- which :class:`Transport` instance owns a given ``ChannelRef``
- whether a delivery queues for quiet hours or bypasses
- the canonical ``<transport>_send`` event emitted after every
  successful delivery
- attachment handling on transports that don't support files

The daemon still owns per-turn state (``_current_reply_channel``,
``_current_turn_kind``, the did-send flag) because that state's
lifecycle is bound to ``_run_turn`` and the handlers — splitting
that out is a separate refactor. The router takes those values as
arguments to :meth:`dispatch` and consults them only for the
purpose of emitting events / picking a queue policy.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from ..events import EventLogger
from ..principals import AddressBook
from ..transports import ChannelRef, OutboundMessage, Transport
from .quiet_hours import QueuedMessage, QuietQueue, is_quiet_hours


# Resolve a transport name to its instance. Lazy so the router
# always sees the daemon's *current* transport map — the daemon (and
# tests) sometimes swap a transport in after the router is built.
TransportLookup = Callable[[str], Optional[Transport]]


log = logging.getLogger(__name__)


# Transports whose delivery the quiet-queue holds when the wall-
# clock is inside quiet hours. CLI / A2A never queue (interactive
# turns, the user / agent is waiting). Signal and Discord queue
# when no bypass is in effect.
_QUEUEABLE_TRANSPORTS = frozenset({"signal", "discord"})


class OutboxRouter:
    """Single dispatch path for outbound messages.

    Handlers (via ``ctx``) and ``SpeakingDaemon._send_message``
    converge on :meth:`dispatch`. The router never reads daemon
    state directly — every per-turn knob (``turn_id``, the
    in-flight principal's display name, whether this delivery
    should bypass quiet hours) is an explicit argument.
    """

    def __init__(
        self,
        *,
        transport_for: TransportLookup,
        address_book: AddressBook,
        events: EventLogger,
        quiet_queue: QuietQueue,
        speaking_cfg: dict,
    ) -> None:
        # ``transport_for`` is a callable rather than a mapping so
        # the router always sees the daemon's current transport
        # state (the test suite — and one day a hot-swap path —
        # swap a transport in after construction).
        self._transport_for = transport_for
        self._address_book = address_book
        self._events = events
        self._quiet_queue = quiet_queue
        self._speaking_cfg = speaking_cfg

    # ------------------------------------------------------------------
    # Transport lookup

    def transport_for(self, name: str) -> Optional[Transport]:
        """Return the transport instance for the given name (e.g.
        ``"cli"``, ``"signal"``), or ``None`` if it isn't configured."""
        return self._transport_for(name)

    # ------------------------------------------------------------------
    # Dispatch

    async def dispatch(
        self,
        channel: ChannelRef,
        text: str,
        attachments: Optional[list[str]] = None,
        *,
        turn_id: Optional[str] = None,
        emergency: bool = False,
        bypass_quiet: bool = False,
        principal_display_name: Optional[str] = None,
    ) -> None:
        """Deliver ``text`` to ``channel``.

        Honors quiet hours for queueable transports unless
        ``bypass_quiet`` is set. Emits the canonical
        ``<transport>_send`` event after delivery (or
        ``quiet_queue_enter`` when held).
        """
        transport = self.transport_for(channel.transport)
        if transport is None:
            raise RuntimeError(
                f"transport {channel.transport!r} is not available "
                "(disabled or not configured)"
            )

        queueable = channel.transport in _QUEUEABLE_TRANSPORTS
        if queueable and not bypass_quiet and is_quiet_hours(self._speaking_cfg):
            self._queue_for_quiet_hours(channel, text, turn_id=turn_id)
            return

        if attachments and channel.transport != "signal":
            log.warning(
                "ignoring %d attachment(s) for %s reply; transport "
                "doesn't support outbound files yet",
                len(attachments),
                channel.transport,
            )
            attachments = None

        chunk_count = await transport.send(
            OutboundMessage(
                destination=channel,
                text=text,
                attachments=list(attachments) if attachments else [],
            )
        )
        log.info(
            "%s send to %s (%d chars%s)",
            "emergency" if emergency else "reply",
            channel.address,
            len(text),
            f", {len(attachments)} attachment(s)" if attachments else "",
        )
        self._emit_send_event(
            channel=channel,
            text_len=len(text),
            chunk_count=chunk_count,
            attachment_count=len(attachments) if attachments else 0,
            emergency=emergency,
            # ``bypassed_quiet`` is True only when delivery happened
            # despite the wall-clock being inside the quiet window.
            bypassed_quiet=is_quiet_hours(self._speaking_cfg),
            turn_id=turn_id,
            principal_display_name=principal_display_name,
        )

    # ------------------------------------------------------------------
    # Internals

    def _queue_for_quiet_hours(
        self,
        channel: ChannelRef,
        text: str,
        *,
        turn_id: Optional[str],
    ) -> None:
        self._quiet_queue.append(
            QueuedMessage(
                transport=channel.transport,
                recipient=channel.address,
                text=text,
                queued_at=time.time(),
            )
        )
        sender_name = self._address_book.display_name_for(
            channel.transport, channel.address
        )
        log.info(
            "quiet hours: queued %s reply for %s (%d chars); queue size=%d",
            channel.transport,
            sender_name,
            len(text),
            self._quiet_queue.size(),
        )
        self._events.emit(
            "quiet_queue_enter",
            turn_id=turn_id,
            transport=channel.transport,
            recipient=channel.address,
            sender_name=sender_name,
            text_len=len(text),
            queue_size=self._quiet_queue.size(),
        )

    def _emit_send_event(
        self,
        *,
        channel: ChannelRef,
        text_len: int,
        chunk_count: int,
        attachment_count: int,
        emergency: bool,
        bypassed_quiet: bool,
        turn_id: Optional[str],
        principal_display_name: Optional[str],
    ) -> None:
        """Single canonical ``<transport>_send`` event shape across all
        transports. ``bypassed_quiet`` is True only when delivery
        happened despite the wall-clock being inside the quiet window
        (i.e. a real bypass took effect, not just "we sent at 3pm").

        ``sender_name`` resolution: prefer the address book; fall back
        to the in-flight turn's principal display name; finally fall
        back to the channel address. The middle case matters for CLI
        replies — the channel.address is an ephemeral conn_id with no
        address-book entry.
        """
        sender_name = self._address_book.display_name_for(
            channel.transport, channel.address
        )
        if sender_name == channel.address and principal_display_name:
            sender_name = principal_display_name
        self._events.emit(
            f"{channel.transport}_send",
            turn_id=turn_id,
            recipient=channel.address,
            sender_name=sender_name,
            text_len=text_len,
            chunk_count=chunk_count,
            attachment_count=attachment_count,
            emergency=emergency,
            bypassed_quiet=bypassed_quiet,
        )
