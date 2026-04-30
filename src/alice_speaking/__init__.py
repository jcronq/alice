"""Alice's speaking hemisphere — the runtime that talks to humans.

The package is organised by intent:

- :mod:`alice_speaking.daemon` — entry orchestrator that wires
  every other piece together.
- :mod:`alice_speaking.factory` — assembles the
  :class:`SourceRegistry` from a :class:`Config`.
- :mod:`alice_speaking.turn_runner` — per-turn kernel-call
  orchestration (session id, bootstrap preamble, retry).
- :mod:`alice_speaking.transports` — bidirectional human-channel
  plugins (Signal, CLI, Discord, A2A).
- :mod:`alice_speaking.internal` — internal event sources
  (surface watcher, emergency watcher).
- :mod:`alice_speaking.startup` — once-per-session priming tasks.
- :mod:`alice_speaking.pipeline` — middleware run around every
  turn (compaction, dedup, handlers, outbox, quiet-hours).
- :mod:`alice_speaking.domain` — the model: principals, render,
  session state, turn log.
- :mod:`alice_speaking.infra` — supporting plumbing: config, the
  events emitter, the Signal JSON-RPC adapter.
- :mod:`alice_speaking.tools` — MCP tool definitions wired into
  the agent kernel.

Public-API re-exports below let callers reach the most common
names without knowing the subpackage layout. Plan 02 of the
speaking refactor moved most modules into subpackages; the
deprecated re-export shims at the package root will retire in
Phase 7.
"""

from .daemon import SpeakingDaemon
from .domain.principals import AddressBook, PrincipalChannel, PrincipalRecord
from .infra.config import Config
from .infra.config import load as load_config
from .transports.base import (
    ChannelRef,
    InboundMessage,
    OutboundMessage,
    Principal,
)


__all__ = [
    "AddressBook",
    "ChannelRef",
    "Config",
    "InboundMessage",
    "OutboundMessage",
    "Principal",
    "PrincipalChannel",
    "PrincipalRecord",
    "SpeakingDaemon",
    "load_config",
]
