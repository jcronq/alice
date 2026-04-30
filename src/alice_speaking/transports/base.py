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

import asyncio
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from ..daemon import SpeakingDaemon


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

# A2A: agents talking to Alice over Google's A2A protocol. Other agents
# generally consume the structured event stream rather than rendered
# text-message chunks, so we leave markdown unrestricted and pick a
# generous max_message_bytes — splitting only kicks in for genuinely
# huge replies (which the SDK will package as multiple text artifacts).
# No typing indicator (A2A's status updates fill that role); no reactions.
A2A_CAPS = Capabilities(
    markdown="full",
    code_blocks=True,
    images_outbound=False,
    files_outbound=False,
    max_message_bytes=200_000,
    long_message_strategy="split",
    typing_indicator=False,
    reactions=False,
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
# Event marker + dispatcher context (Phase 2 of plan 01)
#
# Each Transport produces events of a known dataclass type (`event_type`).
# The dispatcher uses the event's runtime type to route to the right
# transport's `handle()` — no isinstance ladder. `Event` is just a name
# for "the dataclass a transport produces"; we don't enforce a base
# class because transport events are plain dataclasses today (Signal's
# wraps an envelope, CLI/Discord/A2A wrap an InboundMessage). The alias
# documents intent without forcing a refactor of every event class.


Event = Any  # see comment above; intentionally permissive


class DaemonContext:
    """Per-turn handle into the speaking daemon, passed to handlers and
    producers as ``ctx``.

    Phase 2 ships this as a thin passthrough proxy onto
    :class:`SpeakingDaemon`: every attribute access (read or write)
    routes back to the underlying daemon instance, so handlers and
    producers continue to share daemon state exactly as they did when
    they were methods. Phase 6 narrows this to a real public surface
    backed by `OutboxRouter` / `CompactionTrigger`.

    Lives in `transports/base.py` (not `_dispatch.py`) so transports
    can refer to it in their `producer()` / `handle()` signatures
    without importing from the daemon module.
    """

    __slots__ = ("_daemon",)

    def __init__(self, daemon: "SpeakingDaemon") -> None:
        object.__setattr__(self, "_daemon", daemon)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._daemon, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_daemon":
            object.__setattr__(self, name, value)
        else:
            setattr(self._daemon, name, value)


# ---------------------------------------------------------------------------
# Transport protocol


@runtime_checkable
class Transport(Protocol):
    """A bidirectional human-conversation channel.

    Two layers of obligations, both required for a class to satisfy this
    protocol:

    *Channel layer* — ``start()``/``stop()``/``messages()``/``send()``/
    ``typing()``. The transport's job as a wire-format adapter: open
    the listener, yield :class:`InboundMessage` objects, deliver
    :class:`OutboundMessage` objects, drive the typing indicator if any.

    *Dispatcher layer* — ``event_type``/``producer()``/``handle()``. How
    the transport plugs into the speaking daemon's event loop:
    ``producer(ctx)`` returns a long-running task that pumps inbound
    messages onto ``ctx._queue`` as ``event_type`` instances, and
    ``handle(ctx, event)`` runs one turn for one such event. Phase 3
    of plan 01 will let the daemon route by ``event_type`` through a
    registry; until then the daemon still wires producers manually
    in :meth:`SpeakingDaemon.run`.

    Implementations should be safe to call ``send()`` concurrently with
    ``messages()`` iteration and should NOT block the event loop —
    network I/O goes through asyncio primitives.
    """

    name: str
    caps: Capabilities
    event_type: type

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def messages(self) -> AsyncIterator[InboundMessage]: ...

    async def send(self, out: OutboundMessage) -> int:
        """Deliver ``out``. Returns the number of chunks the rendered
        text was split into so the caller can emit a uniform
        ``<transport>_send`` event with ``chunk_count``."""
        ...

    async def typing(self, channel: ChannelRef, on: bool) -> None: ...

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Schedule a long-running task that pushes events onto
        ``ctx._queue``. Returns the task so the daemon can supervise
        it, or ``None`` if this transport has no producer (e.g. wired
        only as an outbound sink)."""
        ...

    async def handle(self, ctx: DaemonContext, event: Event) -> None:
        """Process one event of :attr:`event_type`. Owns the prompt
        build + kernel call + outbox send for this transport."""
        ...
