"""SignalTransport: wraps :class:`SignalClient` under the Transport interface.

Phase 2 scope is narrow: the daemon stops branching on transport type for
**outbound** dispatch. Inbound still flows through the daemon's
``_signal_producer`` (which owns dedup, allowed-sender filtering, and
multi-message batching from the same source) — those behaviors are
signal-specific and don't generalize cleanly under
:meth:`Transport.messages` yet. Phase 3 (address book + principal-based
ACL) is the natural place to move inbound under this interface.

What this class adds today:

- :meth:`send` applies :func:`render` (markdown stripping + chunking via
  :data:`SIGNAL_CAPS`) before handing each chunk to
  :meth:`SignalClient.send`. Multi-chunk messages get a ``(i/N)``
  prefix so recipients can tell they go together. Attachments ride on
  chunk 1 only — same rule as :meth:`SignalClient.send`.
- :meth:`typing` delegates to the client's typing heartbeat.

The class composes :class:`SignalClient` rather than reimplementing it —
``signal-cli``'s JSON-RPC, the receive log tail, the offset file, etc.
all stay where they are. Lifecycle (``wait_ready``, ``aclose``) stays
with the daemon for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from ..signal_client import SignalEnvelope
from .base import (
    SIGNAL_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
)


@dataclass
class SignalEvent:
    """One inbound Signal message, carried with the resolved sender display
    name so handlers and the prompt builder don't have to re-look it up.

    Lives next to :class:`SignalTransport` (Phase 2 / Plan 01 Phase 4): the
    transport produces this type, and the dispatcher routes by
    ``type(event)`` to :meth:`SignalTransport.handle`. Daemon code
    historically defined the dataclass; it's re-exported from
    ``alice_speaking.daemon`` for back-compat with callers and tests.
    """

    envelope: SignalEnvelope
    sender_name: str


log = logging.getLogger(__name__)


# Per-message state -> emoji. The daemon transitions an inbound through
# received -> replied | abandoned and the transport renders the state as
# a reaction on the originating message. Cosmetic feedback only — never
# load-bearing.
_STATE_EMOJI: dict[str, str] = {
    "received": "👀",
    "replied": "✅",
    "abandoned": "❌",
}


class SignalTransport:
    """Transport adapter for Signal. Wraps an existing :class:`SignalClient`."""

    name = "signal"
    caps: Capabilities = SIGNAL_CAPS
    event_type = SignalEvent

    def __init__(self, *, signal_client) -> None:
        # Delayed import-style annotation: SignalClient lives in a sibling
        # module and importing it eagerly would create a cycle through
        # alice_speaking.daemon. Type-checkers can still see it via
        # `from .signal_client import SignalClient` at the call site.
        self._signal = signal_client

    # ------------------------------------------------------------------
    # Lifecycle (no-op — SignalClient lifecycle stays on the daemon)

    async def start(self) -> None:
        """No-op. The daemon owns ``signal_client.wait_ready()`` because
        the readiness check happens before any transports start.
        """
        return

    async def stop(self) -> None:
        """No-op. The daemon calls ``signal_client.aclose()`` itself
        during shutdown.
        """
        return

    # ------------------------------------------------------------------
    # Inbound — not implemented as an async iterator yet.
    #
    # Signal's dedup + allowed-sender + per-source batching never fit
    # cleanly under :meth:`Transport.messages`; the producer below
    # publishes :class:`SignalEvent` objects directly onto the daemon
    # queue instead.

    def messages(self) -> AsyncIterator[InboundMessage]:
        raise NotImplementedError(
            "SignalTransport doesn't expose messages() — its producer "
            "publishes SignalEvent directly onto the daemon queue"
        )

    # ------------------------------------------------------------------
    # Dispatcher integration (Phase 2 of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Long-running task that pumps Signal envelopes onto
        ``ctx._queue`` as :class:`SignalEvent` objects.

        Owns ACL gating (``ctx.address_book.is_allowed``), envelope
        dedup (``ctx.dedup``), and display-name resolution — all the
        per-source state that used to live in
        ``SpeakingDaemon._signal_producer``.
        """
        return asyncio.create_task(self._produce(ctx), name="sig-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for env in self._signal.receive():
            if not ctx.address_book.is_allowed("signal", env.source):
                log.info("ignoring envelope from %s", env.source)
                continue
            if ctx.dedup.seen(env.timestamp):
                log.debug("duplicate ts=%d; skipping", env.timestamp)
                continue
            ctx.dedup.mark(env.timestamp)
            sender_name = ctx.address_book.display_name_for("signal", env.source)
            await ctx._queue.put(
                SignalEvent(envelope=env, sender_name=sender_name)
            )

    async def handle(self, ctx: DaemonContext, event: SignalEvent) -> None:
        """Run one turn for one Signal event.

        Phase 2 — not yet wired into the daemon's consumer loop, which
        still calls :func:`_dispatch.handle_signal` directly with a
        coalesced batch. The single-event adapter here exists for
        protocol conformance and Phase 3's registry dispatch; Phase 2a
        relocates batch coalescing into the transport itself.
        """
        from .._dispatch import handle_signal

        await handle_signal(ctx, [event])

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> int:
        """Render Alice's text per :data:`SIGNAL_CAPS`, then send chunk-by-chunk.

        ``render()`` strips markdown (Signal renders none) and chunks to
        ``SIGNAL_CAPS.max_message_bytes`` (2000). Each chunk is delivered
        as a separate Signal message. When there are multiple chunks we
        prepend ``(i/N)`` so the recipient can see they go together. When
        there's only one chunk, no prefix — looks like a normal message.

        Attachments ride on the FIRST chunk only. signal-cli stores the
        attachment alongside the message; sending it on every chunk
        would multiply the upload and look like duplicate media.

        Returns the number of chunks delivered (0 when ``render`` produces
        an empty list — e.g. text was whitespace).
        """
        # Lazy import: alice_speaking.render imports transports.base, so
        # importing it at module scope would cycle through this module
        # back to render before render finishes initializing.
        from ..render import render

        chunks = render(out.text, self.caps)
        if not chunks:
            log.debug("signal send: render produced no chunks; nothing to do")
            return 0

        recipient = out.destination.address
        attachments = list(out.attachments) if out.attachments else None
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            payload = f"({i}/{total}) {chunk}" if total > 1 else chunk
            # signal-cli's _CHUNK_LIMIT is 4000 chars and our chunks are
            # ≤2000 bytes (≤2000 chars in the worst case), so SignalClient
            # treats each call as one message and won't add its own (i/N).
            await self._signal.send(
                recipient,
                payload,
                attachments=attachments if i == 1 else None,
            )
        return total

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """Drive the typing-indicator heartbeat for a recipient."""
        if on:
            await self._signal.start_typing(channel.address)
        else:
            await self._signal.stop_typing(channel.address)

    async def set_message_state(
        self,
        channel: ChannelRef,
        target_timestamp: int,
        state: str,
    ) -> None:
        """React to a prior inbound message with the emoji for ``state``.

        Known states: ``received`` (we accepted it), ``replied`` (we sent
        a response), ``abandoned`` (we gave up — error or no reply).
        Unknown states are logged and skipped. Reactions are cosmetic;
        any RPC failure is swallowed rather than failing the turn.
        """
        emoji = _STATE_EMOJI.get(state)
        if not emoji:
            log.warning("unknown signal message state %r; skipping", state)
            return
        try:
            await self._signal.send_reaction(
                recipient=channel.address,
                target_author=channel.address,
                target_timestamp=target_timestamp,
                emoji=emoji,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "send_reaction failed (ts=%d state=%s): %s",
                target_timestamp,
                state,
                exc,
            )
