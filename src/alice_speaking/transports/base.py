"""Core types for the transport abstraction.

Three concepts kept independent:

- :class:`Principal` — *who*. A stable identity: owner, friend_carol, an
  agent with a known token. Transport-independent.
- :class:`ChannelRef` — *where*. A delivery address: a Signal phone number,
  a CLI socket connection, a Discord channel id. Transport-private payload
  in ``address``; the router never inspects it.
- :class:`Transport` — *how*. The protocol plugin that knows how to receive
  inbound messages and deliver outbound ones for a given transport name.

A :class:`InboundMessage` carries both ``principal`` and ``origin`` (the
ChannelRef the message came from). An :class:`OutboundMessage` carries
``destination`` and the rendered text. The router's default is
``destination = msg.origin``, but Alice can target any destination she
wants — that's what makes proactive and cross-transport replies fall out
naturally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Identity & addressing


@dataclass(frozen=True)
class Principal:
    """Who sent the message. Stable across reconnects within a transport.

    Two principals are equal when ``transport`` and ``native_id`` match —
    ``display_name`` is for prompts and logs only.
    """

    transport: str       # "signal" | "cli" | "discord"
    native_id: str       # phone number, unix uid, discord user id
    display_name: str

    def __post_init__(self) -> None:
        if not self.transport or not self.native_id:
            raise ValueError("Principal requires non-empty transport + native_id")


@dataclass(frozen=True)
class ChannelRef:
    """Where to deliver an outbound message. Opaque ``address`` is
    transport-private — only the originating transport's :meth:`Transport.send`
    knows how to interpret it.

    ``durable=True`` means the channel can be reached at any time later
    (Signal phone, Discord channel id). ``durable=False`` means the
    channel only exists during an active session (a CLI socket
    connection). The address book never persists ephemeral refs.
    """

    transport: str
    address: str
    durable: bool


# ---------------------------------------------------------------------------
# Capabilities


@dataclass(frozen=True)
class Capabilities:
    """What this transport can render and how big its messages can be.

    Used in two places:

    1. The system prompt fragment generator advertises these to Alice so
       she writes in the right shape (no markdown for Signal, etc.).
    2. The per-transport renderer enforces them as a safety net before
       send (strip markdown, split into chunks).
    """

    markdown: Literal["full", "limited", "none"]
    code_blocks: bool
    images_outbound: bool
    files_outbound: bool
    max_message_bytes: int
    long_message_strategy: Literal["split", "attachment", "truncate"]
    typing_indicator: bool
    reactions: bool
    interactive: bool


# Concrete capability profiles. SIGNAL_CAPS and DISCORD_CAPS are scaffolding
# for Phase 2/3 — only CLI_CAPS is actually wired in Phase 1.

CLI_CAPS = Capabilities(
    markdown="full",
    code_blocks=True,
    images_outbound=False,
    files_outbound=False,
    max_message_bytes=1_000_000,
    long_message_strategy="split",
    typing_indicator=False,
    reactions=False,
    interactive=True,
)

SIGNAL_CAPS = Capabilities(
    markdown="none",
    code_blocks=False,
    images_outbound=True,
    files_outbound=True,
    max_message_bytes=2000,
    long_message_strategy="split",
    typing_indicator=True,
    reactions=True,
    interactive=False,
)

DISCORD_CAPS = Capabilities(
    markdown="limited",
    code_blocks=True,
    images_outbound=True,
    files_outbound=True,
    max_message_bytes=1900,
    long_message_strategy="split",
    typing_indicator=True,
    reactions=True,
    interactive=False,
)


# ---------------------------------------------------------------------------
# Messages


@dataclass
class InboundMessage:
    """An inbound message handed by a transport to the router."""

    principal: Principal
    origin: ChannelRef
    text: str
    timestamp: float
    # Transport-private metadata (e.g. signal envelope timestamp, discord
    # message id). The router doesn't read this; transports may consult
    # it to thread/quote replies.
    metadata: dict = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """An outbound message handed by the router back to a transport.

    ``attachments`` is a list of filesystem paths the transport can read.
    Paths must be visible to the transport's underlying delivery process
    (e.g. for Signal, paths must be visible to signal-cli's container).
    Transports that don't support attachments may log + drop them.
    """

    destination: ChannelRef
    text: str
    in_reply_to: Optional[InboundMessage] = None
    attachments: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Transport protocol


@runtime_checkable
class Transport(Protocol):
    """A bidirectional human-conversation channel.

    Lifecycle: ``start()`` opens the listener / connection. ``messages()``
    is an async iterator that yields :class:`InboundMessage` until the
    transport stops. ``send()`` delivers an :class:`OutboundMessage`.
    ``stop()`` cleans up.

    Implementations should be safe to call ``send()`` concurrently with
    ``messages()`` iteration. They should NOT block the event loop —
    network I/O goes through asyncio primitives.
    """

    name: str
    caps: Capabilities

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def messages(self) -> AsyncIterator[InboundMessage]: ...

    async def send(self, out: OutboundMessage) -> int:
        """Deliver ``out``. Returns the number of chunks the rendered
        text was split into so the caller can emit a uniform
        ``<transport>_send`` event with ``chunk_count``."""
        ...

    async def typing(self, channel: ChannelRef, on: bool) -> None: ...
