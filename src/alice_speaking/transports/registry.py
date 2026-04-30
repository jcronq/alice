"""Event-type registry for dispatcher routing.

Plan 01 Phase 3 of the speaking refactor (see
``docs/refactor/01-transport-plugin-interface.md``). Replaces the
isinstance ladder in :meth:`SpeakingDaemon._consumer`: each transport
or internal source registers itself by its declared ``event_type``,
and the consumer looks up the source for the event it just pulled
off the queue.

Three kinds of source share one registry:

- :class:`Transport` instances — bidirectional human-conversation
  channels (CLI, Discord, A2A; Signal owns its own loop after
  Phase 2a and is intentionally NOT registered).
- :class:`InternalSource` instances — non-conversational triggers
  (surfaces, emergencies). Same dispatcher shape, no outbound.
- :class:`StartupSource` instances — once-per-session tasks. No
  ``event_type``; stored separately because the dispatcher iterates
  them by name at startup, not lookup-by-type.

The registry is intentionally tiny: a hash map from event class to
source, plus a flat list of startup sources. Phase 6's
``TurnDispatcher`` will own the actual lookup loop; Phase 3 just
stores the mapping and returns it on demand.
"""

from __future__ import annotations

from typing import Iterable, Optional, Union

from ..internal.base import InternalSource
from ..startup.base import StartupSource
from .base import Transport


# A "source" with a handler is either a Transport or an InternalSource —
# both protocols carry the ``event_type`` / ``handle`` shape the
# dispatcher needs.
EventSource = Union[Transport, InternalSource]


class SourceRegistry:
    """Hash-map of event types to their handler source.

    Two read paths:

    - :meth:`lookup` — dispatcher's per-event lookup. Returns the
      source whose ``handle(ctx, event)`` should run.
    - :meth:`all_event_sources` / :meth:`all_startup_sources` —
      iteration paths used during daemon startup to schedule
      producers and run startup tasks.

    Mutation is write-once: every :meth:`register` /
    :meth:`register_internal` enforces no duplicate ``event_type``.
    Two transports producing the same event class would silently
    fight for dispatch; surface that as a startup-time ValueError
    instead.
    """

    def __init__(self) -> None:
        self._by_event_type: dict[type, EventSource] = {}
        self._startup: list[StartupSource] = []

    # ------------------------------------------------------------------
    # Registration

    def register(self, transport: Transport) -> None:
        """Register a :class:`Transport`. The dispatcher will route
        events of ``transport.event_type`` to ``transport.handle``."""
        self._add_event_source(transport)

    def register_internal(self, source: InternalSource) -> None:
        """Register an :class:`InternalSource` (surfaces, emergencies, …).
        Same dispatch shape as a transport; kept method-distinct so
        callers' intent is visible at the call site."""
        self._add_event_source(source)

    def register_startup(self, source: StartupSource) -> None:
        """Register a :class:`StartupSource` to run once before the
        dispatcher loop accepts its first event."""
        self._startup.append(source)

    def _add_event_source(self, source: EventSource) -> None:
        et = source.event_type
        if et in self._by_event_type:
            existing = self._by_event_type[et]
            raise ValueError(
                f"event_type {et.__name__} already registered to "
                f"{type(existing).__name__}; cannot also bind to "
                f"{type(source).__name__}"
            )
        self._by_event_type[et] = source

    # ------------------------------------------------------------------
    # Lookup

    def lookup(self, event_type: type) -> Optional[EventSource]:
        """Return the source registered for ``event_type``, or None
        when nothing is registered. Callers (the dispatcher) decide
        how to handle the None case — typically log + drop."""
        return self._by_event_type.get(event_type)

    def all_event_sources(self) -> Iterable[EventSource]:
        """Every registered Transport / InternalSource. Order is
        insertion order (Python 3.7+ dict guarantee)."""
        return self._by_event_type.values()

    def all_startup_sources(self) -> Iterable[StartupSource]:
        return tuple(self._startup)
