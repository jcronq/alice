"""Infra — supporting plumbing that doesn't model the domain.

These modules are wiring:

- :mod:`config` — runtime configuration loading from
  ``alice.env`` + ``alice.config.json``.
- :mod:`events` — JSONL event-emitter the daemon writes to
  ``speaking.log`` (consumed by the viewer).
- :mod:`signal_rpc` — the low-level Signal JSON-RPC adapter that
  ``transports/signal.py`` composes. Renamed from ``signal_client``
  to disambiguate from the transport (Plan 02 of the speaking
  refactor); ``SignalClient`` is still available as an alias.
"""
