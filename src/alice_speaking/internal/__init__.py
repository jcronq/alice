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

Phase 5 of plan 01 fleshes this out: ``surfaces.py`` (today's
``daemon._surface_producer`` + ``_handle_surface``) and ``emergency.py``
(today's ``_emergency_producer`` + ``_handle_emergency``) will land
here. Phase 2 ships only the protocol so the rest of the runtime can
already type its way around the seam.
"""

from .base import InternalSource

__all__ = ["InternalSource"]
