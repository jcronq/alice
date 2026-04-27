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

import logging
from typing import AsyncIterator

from .base import (
    SIGNAL_CAPS,
    Capabilities,
    ChannelRef,
    InboundMessage,
    OutboundMessage,
)


log = logging.getLogger(__name__)


class SignalTransport:
    """Transport adapter for Signal. Wraps an existing :class:`SignalClient`."""

    name = "signal"
    caps: Capabilities = SIGNAL_CAPS

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
    # Inbound — not implemented in Phase 2.

    def messages(self) -> AsyncIterator[InboundMessage]:
        """Inbound for Signal still flows through the daemon's
        ``_signal_producer`` so dedup + allowed-sender + batching keep
        their existing shape. Phase 3 will move it under this interface
        when address-book / principal-based ACL lands.
        """
        raise NotImplementedError(
            "SignalTransport.messages() is not wired in Phase 2 — "
            "inbound is still pumped by daemon._signal_producer"
        )

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> None:
        """Render Alice's text per :data:`SIGNAL_CAPS`, then send chunk-by-chunk.

        ``render()`` strips markdown (Signal renders none) and chunks to
        ``SIGNAL_CAPS.max_message_bytes`` (2000). Each chunk is delivered
        as a separate Signal message. When there are multiple chunks we
        prepend ``(i/N)`` so the recipient can see they go together. When
        there's only one chunk, no prefix — looks like a normal message.

        Attachments ride on the FIRST chunk only. signal-cli stores the
        attachment alongside the message; sending it on every chunk
        would multiply the upload and look like duplicate media.
        """
        # Lazy import: alice_speaking.render imports transports.base, so
        # importing it at module scope would cycle through this module
        # back to render before render finishes initializing.
        from ..render import render

        chunks = render(out.text, self.caps)
        if not chunks:
            log.debug("signal send: render produced no chunks; nothing to do")
            return

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

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """Drive the typing-indicator heartbeat for a recipient."""
        if on:
            await self._signal.start_typing(channel.address)
        else:
            await self._signal.stop_typing(channel.address)
