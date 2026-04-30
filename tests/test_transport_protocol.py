"""Phase 2 of plan 01: protocol-conformance guards.

Cheap CI tests that catch the recurrence pattern called out in
``docs/refactor/00-overview.md`` § "Cross-cutting CI guards":
declared interface → never wired → declaration becomes misleading
documentation. These three tests fail loudly the moment a transport
or startup source forgets one of its protocol members, so the
"add a transport = one new file + one line" promise stays honest.
"""

from __future__ import annotations

import dataclasses
import pathlib

from alice_speaking.startup.base import StartupSource
from alice_speaking.transports import (
    CLITransport,
    SignalTransport,
    Transport,
)
from alice_speaking.transports.a2a import A2ATransport
from alice_speaking.transports.discord import DiscordTransport


def _all_transports():
    """Concrete transport instances we exercise the protocol against.

    Each is constructed with whatever minimal arg-set their __init__
    requires — none of these instances actually open a socket or talk
    to the network."""
    return [
        SignalTransport(signal_client=object()),
        CLITransport(socket_path=pathlib.Path("/tmp/alice-protocol-test.sock")),
        DiscordTransport(token="x"),
        A2ATransport(port=0),
    ]


def test_each_transport_implements_protocol():
    """Every transport class structurally satisfies :class:`Transport`.

    runtime_checkable Protocols only check attribute presence (not
    signatures), so this is a coarse "did you forget to declare one
    of name/caps/event_type/producer/handle/start/stop/messages/send/
    typing" guard. That's exactly the failure mode we care about.
    """
    for t in _all_transports():
        assert isinstance(t, Transport), (
            f"{type(t).__name__} does not implement the Transport protocol"
        )


def test_event_type_is_dataclass():
    """Each transport's :attr:`event_type` is a dataclass.

    The dispatcher will route by ``type(event)`` once Phase 3 lands;
    if event_type ever drifts to something other than a dataclass
    (e.g. a typing alias, ``object``), routing breaks silently. Catch
    that here.
    """
    for t in _all_transports():
        et = t.event_type
        assert isinstance(et, type), (
            f"{type(t).__name__}.event_type must be a class, got {et!r}"
        )
        assert dataclasses.is_dataclass(et), (
            f"{type(t).__name__}.event_type ({et.__name__}) must be a dataclass"
        )


def test_startup_source_protocol_distinct_from_transport():
    """:class:`StartupSource` must not extend :class:`Transport`.

    A startup task is not a transport (no producer / event_type /
    outbound). Collapsing them later would be tempting; this guard
    keeps them sibling Protocols so the distinction stays explicit.
    """
    transport_attrs = set(getattr(Transport, "__annotations__", {}).keys()) | {
        m for m in dir(Transport) if not m.startswith("_")
    }
    startup_attrs = set(getattr(StartupSource, "__annotations__", {}).keys()) | {
        m for m in dir(StartupSource) if not m.startswith("_")
    }
    # StartupSource should NOT carry transport-specific members.
    for forbidden in ("caps", "event_type", "producer", "messages", "send"):
        assert forbidden not in startup_attrs, (
            f"StartupSource declares {forbidden!r} — that belongs on Transport"
        )
    # And should declare its own one-shot entrypoint.
    assert "run_once" in startup_attrs
    # Sanity: the two protocols don't share an inheritance edge.
    assert Transport not in StartupSource.__mro__
    assert StartupSource not in Transport.__mro__
    # And they're not the same protocol via aliasing.
    assert Transport is not StartupSource
    # Final: confirm the transport protocol still carries its own members.
    for required in ("event_type", "producer", "handle"):
        assert required in transport_attrs, (
            f"Transport lost {required!r} — Phase 2 protocol regressed"
        )
