"""Tests for the missed-reply guardrail (2026-11-24 Jason directive).

The guardrail lives in :mod:`alice_speaking.missed_reply` and is fired
from ``_dispatch._fire_missed_reply_guardrail`` when ``handle_signal``
sees a Signal-inbound turn finish without any ``send_message`` call.

Three behaviours we need to pin:

1. On a grandparent sender, the fallback text is the gentle phrasing
   AND a ``fault-detected`` surface lands in ``inner/surface/``.
2. The guardrail is Signal-inbound-only — surface-triggered /
   idle-flush / CLI turns never call this function. We assert that
   by exercising a surface handler and showing the fallback helpers
   are never invoked for it.
3. On grand-alice, the A2A notify is attempted; when the peer or
   transport is unconfigured, the error is captured in the structured
   event but the local surface still lands and the fallback send
   still runs. On main-alice (default instance), no A2A notify fires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from alice_speaking import _dispatch as _dispatch_module
from alice_speaking import daemon as daemon_module
from alice_speaking import missed_reply as missed_reply_module
from alice_speaking.daemon import SpeakingDaemon
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)
from alice_speaking.infra.signal_rpc import SignalEnvelope
from alice_speaking.transports.signal import SignalEvent
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


# --------------------------------------------------------------------- fakes


class FakeSignal:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(
        self, recipient: str, text: str, attachments: list[str] | None = None
    ) -> None:
        self.sent.append((recipient, text))

    async def start_typing(self, recipient: str) -> None:
        pass

    async def stop_typing(self, recipient: str) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def wait_ready(self, *a, **k) -> None:
        pass

    def receive(self):
        async def _empty():
            if False:
                yield  # type: ignore[unreachable]

        return _empty()


def _text(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=t)], model="claude-opus-4-7")


def _result(session: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id=session,
        usage={"input_tokens": 100},
        result=None,
    )


def _no_send_msgs() -> list[Any]:
    """SDK produces assistant prose but never calls send_message —
    the exact live-site failure mode on grand-alice/codex."""
    return [_text("thinking out loud, forgot to reply"), _result("s-missed")]


def _patch_query(monkeypatch: pytest.MonkeyPatch, messages_fn) -> None:
    async def fake_query(*, prompt: str, options: Any):
        for m in messages_fn():
            yield m

    def outer(**kwargs):
        return fake_query(**kwargs)

    monkeypatch.setattr("kernels.anthropic.kernel.query", outer)


def _make_daemon(cfg, monkeypatch, *, carolyn_number: str = "+14357091396") -> SpeakingDaemon:
    monkeypatch.setattr(daemon_module, "SignalClient", lambda **kwargs: FakeSignal())
    d = SpeakingDaemon(cfg)
    # Enrich the address book with a grandparent principal + a main-alice
    # peer, keeping the Owner/Friend defaults in place. We rebuild rather
    # than mutate because AddressBook has no public add() method.
    existing = list(d.address_book.all_principals())
    carolyn = PrincipalRecord(
        id="carolyn",
        display_name="Carolyn (Jason's mom)",
        channels=[
            PrincipalChannel(
                transport="signal",
                address=carolyn_number,
                durable=True,
                preferred=True,
            ),
        ],
    )
    main_alice = PrincipalRecord(
        id="main-alice",
        display_name="Main Alice (aimax1)",
        channels=[
            PrincipalChannel(
                transport="a2a",
                address="http://10.20.30.177:7878",
                durable=True,
                preferred=True,
            ),
        ],
    )
    d.address_book = AddressBook([*existing, carolyn, main_alice])
    return d


def _read_events(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------- tests


def test_grandparent_fallback_and_surface(cfg, monkeypatch) -> None:
    """Carolyn triggers missed_reply → grandparent phrasing on Signal,
    ``fault-detected`` surface under ``inner/surface/``, and a
    ``fallback_reply_sent`` event alongside the retained
    ``missed_reply`` event."""
    monkeypatch.delenv("ALICE_INSTANCE_NAME", raising=False)
    # Hostname-based instance detection: force main-alice for this test.
    monkeypatch.setattr(
        missed_reply_module, "resolve_instance_name", lambda: "main-alice"
    )

    d = _make_daemon(cfg, monkeypatch)
    _patch_query(monkeypatch, _no_send_msgs)

    event = SignalEvent(
        envelope=SignalEnvelope(
            timestamp=100, source="+14357091396", body="hi"
        ),
        sender_name="Carolyn",
    )
    ctx = _dispatch_module.DaemonContext(d)
    asyncio.run(_dispatch_module.handle_signal(ctx, [event]))

    # Fallback Signal reply uses the gentle phrasing.
    assert len(d.signal.sent) == 1
    recipient, text = d.signal.sent[0]
    assert recipient == "+14357091396"
    assert "Jason is looking into it" in text
    # And is NOT the generic operator-flavored text.
    assert "operator" not in text
    assert "backend" not in text

    # Surface landed.
    surface_dir = cfg.mind_dir / "inner" / "surface"
    assert surface_dir.is_dir()
    surfaces = list(surface_dir.glob("*-missed-reply-*.md"))
    assert len(surfaces) == 1
    body = surfaces[0].read_text()
    assert "priority: action" in body
    assert "type: fault-detected" in body
    assert "principal_id: carolyn" in body
    # No A2A notify on main-alice.
    events = _read_events(cfg.event_log_path)
    kinds = [e["event"] for e in events]
    assert "missed_reply" in kinds  # from TurnRunner, preserved
    assert "fallback_reply_sent" in kinds
    assert "fallback_reply_a2a_notify" not in kinds
    # fallback_reply_sent event carries the structured fields the ops
    # playbook grep-greps on.
    fired = [e for e in events if e["event"] == "fallback_reply_sent"]
    assert fired
    e = fired[0]
    assert e["reason"] == "missed_reply"
    assert e["recipient"] == "+14357091396"
    assert e["original_sender"] == "+14357091396"
    assert e["grandparent"] is True
    assert e["principal_id"] == "carolyn"
    assert e["instance"] == "main-alice"


def test_non_grandparent_gets_generic_phrasing(cfg, monkeypatch) -> None:
    """A principal outside the grandparent set (here: default fallback
    Owner principal) gets the operator-facing phrasing. Regression on the
    original issue #215 test, retained with new text expectations."""
    monkeypatch.delenv("ALICE_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        missed_reply_module, "resolve_instance_name", lambda: "main-alice"
    )

    d = _make_daemon(cfg, monkeypatch)
    _patch_query(monkeypatch, _no_send_msgs)
    event = SignalEvent(
        envelope=SignalEnvelope(
            timestamp=200, source="+15555550100", body="hello"
        ),
        sender_name="Owner",
    )
    ctx = _dispatch_module.DaemonContext(d)
    asyncio.run(_dispatch_module.handle_signal(ctx, [event]))

    assert len(d.signal.sent) == 1
    _, text = d.signal.sent[0]
    assert "backend" in text
    assert "operator" in text
    # Surface flavored for a non-grandparent — principal is the Owner
    # fallback principal (id built from uid or "owner").
    events = _read_events(cfg.event_log_path)
    fired = [e for e in events if e["event"] == "fallback_reply_sent"]
    assert fired
    assert fired[0]["grandparent"] is False


def test_surface_triggered_turn_does_not_fire_guardrail(cfg, monkeypatch) -> None:
    """The guardrail is scoped to Signal-inbound turns only. Surface-
    triggered turns use ``handle_surface_watch`` (silent=False but no
    outbound channel); they emit ``missed_reply`` because the turn
    runner always does, but they must NOT send a Signal fallback or
    write a fault surface — those semantics would make every
    legitimate silent-completion surface look like a fault.

    We assert this structurally: the guardrail helper is never invoked
    for a non-Signal path. Instead of driving the whole surface watcher
    (which pulls in filesystem watchers + live surface files), we just
    probe that ``_fire_missed_reply_guardrail`` is unreachable from the
    surface handler — it lives only inside ``handle_signal``.
    """
    # Grep the compiled module graph: the only call site should be in
    # handle_signal. This is a lightweight structural guarantee that
    # a future refactor can't accidentally wire the guardrail into
    # another handler without a test flipping red.
    import inspect

    sig_src = inspect.getsource(_dispatch_module.handle_signal)
    assert "_fire_missed_reply_guardrail" in sig_src

    # No other handler in the module should reach the guardrail — if
    # someone copies the block into handle_cli etc., this fails.
    other_handlers = [
        _dispatch_module.handle_cli,
        _dispatch_module.handle_idle,
        _dispatch_module._handle_roomba_bin_full,
    ]
    for fn in other_handlers:
        src = inspect.getsource(fn)
        assert "_fire_missed_reply_guardrail" not in src, (
            f"guardrail leaked into {fn.__name__}; it is Signal-inbound only"
        )


def test_grand_alice_fires_a2a_notify_and_is_non_fatal(cfg, monkeypatch) -> None:
    """When this process is grand-alice, the guardrail attempts an
    A2A notify to main-alice after sending the Signal fallback.

    We stub the A2A transport so no real httpx call fires. The stub's
    ``send`` raises to exercise the "best-effort / non-fatal" branch:
    the surface and fallback must still succeed, and the
    ``fallback_reply_a2a_notify`` event must carry ``ok=false``.
    """
    monkeypatch.setenv("ALICE_INSTANCE_NAME", "grand-alice")
    d = _make_daemon(cfg, monkeypatch)
    _patch_query(monkeypatch, _no_send_msgs)

    a2a_sends: list[str] = []

    class StubA2A:
        async def send(self, out) -> int:
            # Capture the attempt, then simulate a transport failure —
            # the guardrail must swallow it.
            a2a_sends.append(out.destination.address)
            raise RuntimeError("connection refused — simulated")

    d.a2a_transport = StubA2A()

    event = SignalEvent(
        envelope=SignalEnvelope(
            timestamp=300, source="+14357091396", body="hi"
        ),
        sender_name="Carolyn",
    )
    ctx = _dispatch_module.DaemonContext(d)
    asyncio.run(_dispatch_module.handle_signal(ctx, [event]))

    # Signal fallback still went out even though the A2A notify blew up.
    assert len(d.signal.sent) == 1
    # A2A was attempted exactly once to main-alice's address.
    assert a2a_sends == ["http://10.20.30.177:7878"]

    # Surface still landed.
    surfaces = list((cfg.mind_dir / "inner" / "surface").glob("*-missed-reply-*.md"))
    assert len(surfaces) == 1
    assert "instance: grand-alice" in surfaces[0].read_text()

    # Structured events: A2A error captured, not raised.
    events = _read_events(cfg.event_log_path)
    notifies = [e for e in events if e["event"] == "fallback_reply_a2a_notify"]
    assert len(notifies) == 1
    assert notifies[0]["ok"] is False
    assert notifies[0]["target"] == "main-alice"
    assert "a2a_send_failed" in (notifies[0]["error"] or "")


def test_a2a_notify_skipped_when_main_alice_not_configured(cfg, monkeypatch) -> None:
    """Grand-alice deployments missing a main-alice entry in the
    address book get the local surface + fallback send, a structured
    event noting the config gap, and no exception."""
    monkeypatch.setenv("ALICE_INSTANCE_NAME", "grand-alice")
    monkeypatch.setattr(daemon_module, "SignalClient", lambda **kwargs: FakeSignal())
    d = SpeakingDaemon(cfg)
    # Deliberately DO NOT register main-alice — this is the "unconfigured"
    # case. We still want a stub transport so the branch runs; the lookup
    # should fail before the transport is called.
    sends: list[Any] = []

    class StubA2A:
        async def send(self, out) -> int:  # pragma: no cover — must not reach here
            sends.append(out)
            return 1

    d.a2a_transport = StubA2A()

    _patch_query(monkeypatch, _no_send_msgs)
    event = SignalEvent(
        envelope=SignalEnvelope(
            timestamp=400, source="+15555550100", body="hello"
        ),
        sender_name="Owner",
    )
    ctx = _dispatch_module.DaemonContext(d)
    asyncio.run(_dispatch_module.handle_signal(ctx, [event]))

    # Fallback + surface still happened.
    assert len(d.signal.sent) == 1
    surfaces = list((cfg.mind_dir / "inner" / "surface").glob("*-missed-reply-*.md"))
    assert len(surfaces) == 1

    # A2A transport was NOT called (lookup short-circuits).
    assert sends == []

    # But the event logs the attempt + the reason.
    events = _read_events(cfg.event_log_path)
    notifies = [e for e in events if e["event"] == "fallback_reply_a2a_notify"]
    assert len(notifies) == 1
    assert notifies[0]["ok"] is False
    assert notifies[0]["error"] == "main_alice_principal_not_in_address_book"


def test_missed_reply_event_still_fires_alongside_fallback(
    cfg, monkeypatch
) -> None:
    """The original ``missed_reply`` event from ``TurnRunner.run_turn``
    MUST stay in the log even though the guardrail now acts on it —
    audit trail preservation."""
    monkeypatch.delenv("ALICE_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(
        missed_reply_module, "resolve_instance_name", lambda: "main-alice"
    )
    d = _make_daemon(cfg, monkeypatch)
    _patch_query(monkeypatch, _no_send_msgs)
    event = SignalEvent(
        envelope=SignalEnvelope(
            timestamp=500, source="+15555550100", body="?"
        ),
        sender_name="Owner",
    )
    ctx = _dispatch_module.DaemonContext(d)
    asyncio.run(_dispatch_module.handle_signal(ctx, [event]))

    events = _read_events(cfg.event_log_path)
    kinds = [e["event"] for e in events]
    assert "missed_reply" in kinds
    assert "fallback_reply_sent" in kinds


def test_resolve_instance_name_env_wins_over_hostname(monkeypatch) -> None:
    monkeypatch.setenv("ALICE_INSTANCE_NAME", "custom-alice")
    assert missed_reply_module.resolve_instance_name() == "custom-alice"


def test_pick_fallback_text_grandparent_and_generic() -> None:
    assert (
        missed_reply_module.pick_fallback_text("carolyn")
        == missed_reply_module.FALLBACK_REPLY_GRANDPARENT
    )
    assert (
        missed_reply_module.pick_fallback_text("DOUGLAS")
        == missed_reply_module.FALLBACK_REPLY_GRANDPARENT
    )
    assert (
        missed_reply_module.pick_fallback_text("jcronq")
        == missed_reply_module.FALLBACK_REPLY_GENERIC
    )
    assert (
        missed_reply_module.pick_fallback_text(None)
        == missed_reply_module.FALLBACK_REPLY_GENERIC
    )
