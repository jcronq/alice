"""Transport abstraction for bidirectional human-conversation channels.

A *transport* is one medium Alice talks to humans (or other agents) through:
Signal, the local CLI, Discord, etc. Each transport advertises its rendering
capabilities, accepts inbound messages from a principal, and delivers outbound
messages back to a channel.

Surface and emergency events are NOT transports — they're internal triggers
and stay on their own producers. The Transport interface is for human-facing
channels only.

Phase 1 ships the base types and one transport (:class:`CLITransport`).
SignalClient is left on its current path; Phase 2 will refactor it under
this interface.
"""

from .base import (
    Capabilities,
    ChannelRef,
    InboundMessage,
    OutboundMessage,
    Principal,
    Transport,
)
from .cli import CLITransport

__all__ = [
    "Capabilities",
    "ChannelRef",
    "CLITransport",
    "InboundMessage",
    "OutboundMessage",
    "Principal",
    "Transport",
]
