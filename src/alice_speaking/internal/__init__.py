"""Internal sources — non-conversational events that flow through the
same dispatch shape as transports but originate inside the speaking
hemisphere itself.

Plan 01 of the speaking refactor splits inbound work into two
categories with sibling Protocols:

- :class:`alice_speaking.transports.Transport` — bidirectional human
  channels (Signal, CLI, Discord, A2A). Receive from outside, send
  outside.
- :class:`InternalSource` — one-way internal triggers (surfaces from
  thinking, sentinel files dropped by external monitors, etc.). No
  outbound concept; the reaction goes back through ``send_message`` /
  the address book like any other turn.

Phase 3 of plan 01 lands the handler-shaped wrappers
(:class:`SurfaceWatcher`, :class:`EmergencyWatcher`) so the
dispatcher's registry has a uniform ``(event_type → source)``
mapping. Phase 5 will move the producer bodies (currently
``SpeakingDaemon._surface_producer`` / ``_emergency_producer``)
into the wrappers' :meth:`producer` methods.
"""

from .base import InternalSource
from .emergency import EmergencyEvent, EmergencyWatcher
from .surfaces import SurfaceEvent, SurfaceWatcher

__all__ = [
    "EmergencyEvent",
    "EmergencyWatcher",
    "InternalSource",
    "SurfaceEvent",
    "SurfaceWatcher",
]
