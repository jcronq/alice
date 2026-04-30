"""Transport abstraction for bidirectional human-conversation channels.

A *transport* is one medium Alice talks to humans (or other agents) through:
Signal, the local CLI, Discord, etc. Each transport advertises its rendering
capabilities, accepts inbound messages from a principal, and delivers outbound
messages back to a channel.

Surface and emergency events are NOT transports — they're internal triggers
and stay on their own producers. The Transport interface is for human-facing
channels only.

Phase 1 shipped the base types and :class:`CLITransport`. Phase 2 adds
:class:`SignalTransport` for outbound dispatch (inbound still flows
through the daemon's ``_signal_producer`` until Phase 3).
"""

from .base import (
    Capabilities,
    ChannelRef,
    DaemonContext,
    Event,
    InboundMessage,
    OutboundMessage,
    Principal,
    Transport,
)
from .cli import CLITransport
from .registry import SourceRegistry
from .signal import SignalTransport

# DiscordTransport is intentionally NOT re-exported here: ``transports.discord``
# does ``import discord`` at module top, so importing it eagerly would crash
# the speaking daemon at startup whenever the optional ``discord.py``
# dependency isn't installed (or the worker image is stale). Callers that
# actually need DiscordTransport import it lazily from
# ``alice_speaking.transports.discord`` only when a Discord bot token is
# configured. See daemon.py.

__all__ = [
    "Capabilities",
    "ChannelRef",
    "CLITransport",
    "DaemonContext",
    "Event",
    "InboundMessage",
    "OutboundMessage",
    "Principal",
    "SignalTransport",
    "SourceRegistry",
    "Transport",
]
