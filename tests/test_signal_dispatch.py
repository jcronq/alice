"""Tests for Signal sender-based dispatch.

Covers the config loader, the ``_produce`` fork in ``SignalTransport``,
and the fallback-on-failure path. Also smoke-checks the A2A receiver
shim that plumbs forwarded Signal metadata into the kernel prompt.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from alice_speaking.infra.signal_rpc import SignalEnvelope
from alice_speaking.signal_dispatch import (
    ForwardRule,
    SignalDispatchConfig,
    SignalDispatchConfigError,
    build_forward_payload,
    forward_to_a2a,
    from_mapping,
)
from alice_speaking.transports.signal import SignalEvent, SignalTransport


# ---------------------------------------------------------------------------
# Config loader


def test_from_mapping_empty_returns_empty_config():
    assert from_mapping(None).forwards == ()
    assert from_mapping({}).forwards == ()
    assert from_mapping({"forwards": []}).forwards == ()


def test_from_mapping_happy_path():
    cfg = from_mapping(
        {
            "forwards": [
                {
                    "senders": ["+14357091396", "+14357091400"],
                    "target": "http://10.200.0.2:7878",
                    "label": "grandparents-home",
                }
            ]
        }
    )
    assert len(cfg.forwards) == 1
    rule = cfg.forwards[0]
    assert rule.senders == frozenset({"+14357091396", "+14357091400"})
    assert rule.target == "http://10.200.0.2:7878"
    assert rule.label == "grandparents-home"


def test_from_mapping_strips_whitespace_senders():
    cfg = from_mapping(
        {
            "forwards": [
                {
                    "senders": [" +14357091396 "],
                    "target": "http://10.200.0.2:7878 ",
                    "label": " grandparents-home ",
                }
            ]
        }
    )
    rule = cfg.forwards[0]
    assert rule.senders == frozenset({"+14357091396"})
    assert rule.target == "http://10.200.0.2:7878"
    assert rule.label == "grandparents-home"


@pytest.mark.parametrize(
    "bad_payload, needle",
    [
        ({"forwards": "nope"}, "must be a list"),
        ({"forwards": [42]}, "must be a mapping"),
        (
            {"forwards": [{"target": "http://x", "label": "l"}]},
            "senders must be a non-empty list",
        ),
        (
            {"forwards": [{"senders": [], "target": "http://x", "label": "l"}]},
            "senders must be a non-empty list",
        ),
        (
            {
                "forwards": [
                    {"senders": ["+1"], "target": "", "label": "l"}
                ]
            },
            "target must be a non-empty string",
        ),
        (
            {
                "forwards": [
                    {"senders": ["+1"], "target": "http://x", "label": ""}
                ]
            },
            "label must be a non-empty string",
        ),
        (
            {"forwards": [{"senders": [42], "target": "http://x", "label": "l"}]},
            "entries must be strings",
        ),
    ],
)
def test_from_mapping_rejects_malformed(bad_payload, needle):
    with pytest.raises(SignalDispatchConfigError) as excinfo:
        from_mapping(bad_payload)
    assert needle in str(excinfo.value)


def test_lookup_returns_matching_rule():
    cfg = SignalDispatchConfig(
        forwards=(
            ForwardRule(
                senders=frozenset({"+14357091396"}),
                target="http://10.200.0.2:7878",
                label="grandparents-home",
            ),
        )
    )
    assert cfg.lookup("+14357091396").label == "grandparents-home"
    assert cfg.lookup("+15555550001") is None


def test_lookup_scans_in_order_on_overlap():
    first = ForwardRule(
        senders=frozenset({"+10000000001"}),
        target="http://a",
        label="first",
    )
    second = ForwardRule(
        senders=frozenset({"+10000000001"}),
        target="http://b",
        label="second",
    )
    cfg = SignalDispatchConfig(forwards=(first, second))
    assert cfg.lookup("+10000000001").label == "first"


# ---------------------------------------------------------------------------
# alice.config.json wiring (config.py)


def test_infra_config_loads_signal_dispatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """``signal.dispatch.forwards`` round-trips through
    :func:`alice_speaking.infra.config.load`."""
    from alice_speaking.infra.config import load as infra_load

    mind_dir = tmp_path / "mind"
    (mind_dir / "config").mkdir(parents=True)
    env_path = tmp_path / "alice.env"
    env_path.write_text(
        f"WORK_DIR={mind_dir}\n"
        f"ALICE_MIND_DIR={mind_dir}\n"
        f"STATE_DIR={tmp_path / 'state'}\n"
    )
    (mind_dir / "config" / "alice.config.json").write_text(
        json.dumps(
            {
                "signal": {
                    "dispatch": {
                        "forwards": [
                            {
                                "senders": ["+14357091396"],
                                "target": "http://10.200.0.2:7878",
                                "label": "grandparents-home",
                            }
                        ]
                    }
                }
            }
        )
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env_path))
    for var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SIGNAL_ACCOUNT",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = infra_load()
    assert cfg.signal_dispatch.forwards
    rule = cfg.signal_dispatch.forwards[0]
    assert rule.target == "http://10.200.0.2:7878"
    assert "+14357091396" in rule.senders


def test_infra_config_missing_signal_dispatch_is_empty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Deploys without ``signal.dispatch`` keep working — empty forwards,
    no exception. Regression guard for the "feature off" default."""
    from alice_speaking.infra.config import load as infra_load

    mind_dir = tmp_path / "mind"
    (mind_dir / "config").mkdir(parents=True)
    env_path = tmp_path / "alice.env"
    env_path.write_text(
        f"WORK_DIR={mind_dir}\n"
        f"ALICE_MIND_DIR={mind_dir}\n"
        f"STATE_DIR={tmp_path / 'state'}\n"
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env_path))
    for var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SIGNAL_ACCOUNT",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = infra_load()
    assert cfg.signal_dispatch.forwards == ()


# ---------------------------------------------------------------------------
# Payload shape


def test_build_forward_payload_structure():
    payload = build_forward_payload(
        body="hi alice",
        sender="+14357091396",
        sender_name="Carolyn Cronquist",
        timestamp_ms=1_700_000_000_000,
        label="grandparents-home",
    )
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "message/send"
    assert isinstance(payload["id"], str) and payload["id"]
    params = payload["params"]
    message = params["message"]
    assert message["role"] == "user"
    assert message["kind"] == "message"
    assert message["parts"] == [{"kind": "text", "text": "hi alice"}]
    # Fire-and-forget: the receiver must not block the forwarder on
    # the kernel turn or the httpx read-timeout triggers the
    # fallback-to-local path and produces a duplicate reply. See
    # Finding 1.
    assert params["configuration"] == {"blocking": False}
    # Metadata lives on both the message and the envelope so either
    # inspection point sees it.
    for meta in (message["metadata"], params["metadata"]):
        assert meta["signal_sender"] == "+14357091396"
        assert meta["signal_sender_name"] == "Carolyn Cronquist"
        assert meta["signal_timestamp"] == 1_700_000_000_000
        assert meta["dispatch_label"] == "grandparents-home"


def test_forward_payload_is_fire_and_forget():
    """Regression guard for Finding 1 — the A2A ``message/send`` v0.3
    JSON-RPC adapter maps ``configuration.blocking`` to the proto
    ``SendMessageConfiguration.return_immediately`` flag. Without
    this, the peer waits for its full kernel turn (15-60s), the
    forwarder's httpx read timeout trips, the fallback path lands
    the envelope on the local inbox, and the receiver (which ran to
    completion anyway) and local Alice both reply — two messages
    for one inbound.
    """
    payload = build_forward_payload(
        body="x",
        sender="+1",
        sender_name="",
        timestamp_ms=0,
        label="l",
    )
    assert payload["params"]["configuration"]["blocking"] is False


def test_forward_payload_includes_attachments(tmp_path: pathlib.Path):
    """Regression guard for Finding 2 — the forward path used to drop
    every attachment on the floor while the local inbox path kept
    them. Photos and voice notes must ride along as A2A
    ``FilePart``s so the receiver sees the same content the local
    path would have.
    """
    import base64

    from alice_speaking.infra.signal_rpc import Attachment

    blob = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    attachment_file = tmp_path / "pic.png"
    attachment_file.write_bytes(blob)
    attachment = Attachment(
        id="abc",
        path=attachment_file,
        content_type="image/png",
        filename="original.png",
    )

    payload = build_forward_payload(
        body="look at this",
        sender="+1",
        sender_name="Carolyn",
        timestamp_ms=1,
        label="l",
        attachments=[attachment],
    )
    parts = payload["params"]["message"]["parts"]
    assert len(parts) == 2
    assert parts[0] == {"kind": "text", "text": "look at this"}
    file_part = parts[1]
    assert file_part["kind"] == "file"
    assert file_part["file"]["mime_type"] == "image/png"
    assert file_part["file"]["name"] == "original.png"
    assert base64.b64decode(file_part["file"]["bytes"]) == blob


def test_build_forward_payload_empty_body_renders_empty_text():
    payload = build_forward_payload(
        body="",
        sender="+14357091396",
        sender_name="Carolyn",
        timestamp_ms=1,
        label="l",
    )
    assert payload["params"]["message"]["parts"][0]["text"] == ""


# ---------------------------------------------------------------------------
# forward_to_a2a wire behaviour


@pytest.mark.asyncio
async def test_forward_to_a2a_calls_injected_poster():
    calls: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict):
        calls.append((url, payload))

    rule = ForwardRule(
        senders=frozenset({"+1"}),
        target="http://10.200.0.2:7878",
        label="grandparents-home",
    )
    await forward_to_a2a(
        rule=rule,
        body="hello",
        sender="+1",
        sender_name="Carolyn",
        timestamp_ms=99,
        http_post=fake_post,
    )
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://10.200.0.2:7878"
    assert payload["method"] == "message/send"
    # The forward MUST remain fire-and-forget end-to-end, not just
    # when callers go through build_forward_payload directly.
    assert payload["params"]["configuration"] == {"blocking": False}


@pytest.mark.asyncio
async def test_forward_to_a2a_propagates_poster_errors():
    async def bad_post(url, payload):
        raise RuntimeError("boom")

    rule = ForwardRule(
        senders=frozenset({"+1"}), target="http://x", label="l"
    )
    with pytest.raises(RuntimeError, match="boom"):
        await forward_to_a2a(
            rule=rule,
            body="",
            sender="+1",
            sender_name="",
            timestamp_ms=0,
            http_post=bad_post,
        )


# ---------------------------------------------------------------------------
# SignalTransport._produce fork
#
# These exercise the integration: a producer loop iteration consults
# ``ctx.cfg.signal_dispatch``, forwards matching senders, and never
# puts the event on the local inbox. On HTTP failure the fallback
# path lands the event on the local inbox exactly like an unforwarded
# sender, so operators keep visibility.


class _FakeSignalClient:
    """Minimal signal-client stand-in that yields a fixed list of envelopes
    then blocks forever (simulating a live stream)."""

    def __init__(self, envelopes: list[SignalEnvelope]) -> None:
        self._envelopes = envelopes
        self._gate = asyncio.Event()

    async def receive(self):
        for env in self._envelopes:
            yield env
        await self._gate.wait()


class _AllowAllAddressBook:
    def is_allowed(self, *_a, **_kw) -> bool:
        return True

    def display_name_for(self, transport: str, source: str) -> str:
        return f"Owner<{source}>"


class _StubDedup:
    def __init__(self) -> None:
        self._seen: set[int] = set()

    def seen(self, ts: int) -> bool:
        return ts in self._seen

    def mark(self, ts: int) -> None:
        self._seen.add(ts)


class _FakeCfg:
    def __init__(self, dispatch: SignalDispatchConfig) -> None:
        self.signal_dispatch = dispatch
        self.speaking: dict = {}


class _StubCtx:
    def __init__(self, dispatch: SignalDispatchConfig) -> None:
        self.address_book = _AllowAllAddressBook()
        self.dedup = _StubDedup()
        self.cfg = _FakeCfg(dispatch)


async def _run_producer_once(
    envelopes: list[SignalEnvelope],
    dispatch: SignalDispatchConfig,
    *,
    monkeypatch: pytest.MonkeyPatch,
    poster=None,
    raise_from_poster: Exception | None = None,
) -> tuple[SignalTransport, list[tuple[str, dict]]]:
    """Run :meth:`SignalTransport._produce` long enough to consume
    every envelope in ``envelopes``, then cancel. Returns the transport
    (so the caller can inspect the inbox) and the list of forward POST
    calls captured by the injected fake poster.
    """
    captured: list[tuple[str, dict]] = []

    async def default_fake_post(url: str, payload: dict):
        captured.append((url, payload))
        if raise_from_poster is not None:
            raise raise_from_poster

    monkeypatch.setattr(
        "alice_speaking.signal_dispatch._default_http_post",
        poster or default_fake_post,
    )

    client = _FakeSignalClient(envelopes)
    transport = SignalTransport(signal_client=client)
    ctx = _StubCtx(dispatch)

    produce_task = asyncio.create_task(transport._produce(ctx))
    # Give the producer time to drain the finite envelope list. The
    # fake client blocks after the last yield, so the producer stays
    # alive until we cancel.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if transport._inbox.qsize() + len(captured) >= len(envelopes):
            break
    await asyncio.sleep(0.02)  # let any trailing iteration finish
    produce_task.cancel()
    try:
        await produce_task
    except asyncio.CancelledError:
        pass
    return transport, captured


_FORWARD_RULE = ForwardRule(
    senders=frozenset({"+14357091396"}),
    target="http://10.200.0.2:7878",
    label="grandparents-home",
)
_DISPATCH = SignalDispatchConfig(forwards=(_FORWARD_RULE,))


@pytest.mark.asyncio
async def test_forwarded_sender_hits_poster_not_inbox(
    monkeypatch: pytest.MonkeyPatch,
):
    envelopes = [
        SignalEnvelope(timestamp=10, source="+14357091396", body="hi alice"),
    ]
    transport, captured = await _run_producer_once(
        envelopes, _DISPATCH, monkeypatch=monkeypatch
    )
    assert transport._inbox.empty(), "forwarded envelope must skip local inbox"
    assert len(captured) == 1
    url, payload = captured[0]
    assert url == "http://10.200.0.2:7878"
    assert payload["method"] == "message/send"
    assert (
        payload["params"]["message"]["parts"][0]["text"] == "hi alice"
    )
    assert (
        payload["params"]["message"]["metadata"]["signal_sender"]
        == "+14357091396"
    )


@pytest.mark.asyncio
async def test_non_forwarded_sender_lands_on_local_inbox(
    monkeypatch: pytest.MonkeyPatch,
):
    envelopes = [
        SignalEnvelope(timestamp=11, source="+15555550100", body="morning"),
    ]
    transport, captured = await _run_producer_once(
        envelopes, _DISPATCH, monkeypatch=monkeypatch
    )
    assert captured == []
    assert transport._inbox.qsize() == 1
    queued: SignalEvent = transport._inbox.get_nowait()
    assert queued.envelope.body == "morning"
    assert queued.envelope.source == "+15555550100"


@pytest.mark.asyncio
async def test_forward_failure_falls_back_to_local_inbox(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    envelopes = [
        SignalEnvelope(timestamp=12, source="+14357091396", body="still here"),
    ]
    import logging

    caplog.set_level(logging.WARNING, logger="alice_speaking.transports.signal")
    transport, captured = await _run_producer_once(
        envelopes,
        _DISPATCH,
        monkeypatch=monkeypatch,
        raise_from_poster=RuntimeError("peer down"),
    )
    # Poster was called once (the attempt), THEN the event fell back.
    assert len(captured) == 1
    assert transport._inbox.qsize() == 1
    queued = transport._inbox.get_nowait()
    assert queued.envelope.source == "+14357091396"
    # A log line records the failure so the operator can see the retry
    # happened via local handling.
    msgs = "\n".join(rec.message for rec in caplog.records)
    assert "signal forward failed" in msgs


@pytest.mark.asyncio
async def test_empty_dispatch_preserves_existing_behaviour(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression guard: with no forwards configured, Jason + Katie's
    normal Signal path (sender -> local inbox) must be unchanged.
    """
    envelopes = [
        SignalEnvelope(timestamp=13, source="+14357091512", body="jason here"),
        SignalEnvelope(timestamp=14, source="+15857544003", body="katie here"),
    ]
    transport, captured = await _run_producer_once(
        envelopes, SignalDispatchConfig(), monkeypatch=monkeypatch
    )
    assert captured == []
    assert transport._inbox.qsize() == 2


# ---------------------------------------------------------------------------
# A2A receiver shim — build_prompt branches on signal_sender metadata.


def test_a2a_build_prompt_signal_forward_branch():
    from alice_speaking.transports.a2a import A2ATransport

    t = A2ATransport(port=0)
    prompt = t.build_prompt(
        principal_name="A2A peer agent",
        stamp="Friday, November 20, 2026 at 3:00 PM EST",
        text="hi alice",
        metadata={
            "signal_sender": "+14357091396",
            "signal_sender_name": "Carolyn Cronquist",
            "dispatch_label": "grandparents-home",
        },
    )
    assert "Carolyn Cronquist" in prompt
    assert "+14357091396" in prompt
    # The forwarded branch steers Alice to reply by Signal number
    # (the template also includes a "do NOT reply with recipient='self'"
    # negative-cue line, so we only assert the positive directive).
    assert "recipient='+14357091396'" in prompt
    assert "Do NOT reply with `recipient='self'`" in prompt


def test_a2a_build_prompt_default_branch_unchanged():
    from alice_speaking.transports.a2a import A2ATransport

    t = A2ATransport(port=0)
    prompt = t.build_prompt(
        principal_name="peer",
        stamp="Friday, November 20, 2026 at 3:00 PM EST",
        text="hi",
    )
    assert "recipient='self'" in prompt
    assert "Signal" not in prompt.split("---")[0]  # pre-divider stays clean


# ---------------------------------------------------------------------------
# Attachments flow end-to-end through _produce (Finding 2 regression)


@pytest.mark.asyncio
async def test_produce_forwards_attachments_to_a2a(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """An inbound envelope carrying a photo must surface as a
    ``FilePart`` on the outbound A2A payload, not silently drop."""
    import base64

    from alice_speaking.infra.signal_rpc import Attachment

    blob = b"photo-bytes"
    attachment_file = tmp_path / "pic.jpg"
    attachment_file.write_bytes(blob)
    attachment = Attachment(
        id="z",
        path=attachment_file,
        content_type="image/jpeg",
        filename="selfie.jpg",
    )
    envelopes = [
        SignalEnvelope(
            timestamp=20,
            source="+14357091396",
            body="see this",
            attachments=[attachment],
        )
    ]
    transport, captured = await _run_producer_once(
        envelopes, _DISPATCH, monkeypatch=monkeypatch
    )
    assert transport._inbox.empty(), "forwarded envelope must skip local inbox"
    assert len(captured) == 1
    _, payload = captured[0]
    parts = payload["params"]["message"]["parts"]
    kinds = [p["kind"] for p in parts]
    assert kinds == ["text", "file"]
    file_part = parts[1]
    assert file_part["file"]["mime_type"] == "image/jpeg"
    assert base64.b64decode(file_part["file"]["bytes"]) == blob


# ---------------------------------------------------------------------------
# Hot-reload — signal.dispatch picks up alice.config.json edits without
# a daemon restart (Finding 3 regression). We drive
# ``SpeakingDaemon._maybe_reload_config`` directly rather than wiring a
# full daemon; it's a pure method on the daemon class that reads from
# ``self._config_path`` + ``config_module.load`` and mutates
# ``self.cfg``, which is all we need to exercise.


def test_signal_dispatch_hot_reload_picks_up_new_forwards(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    from alice_speaking import daemon as daemon_module

    mind_dir = tmp_path / "mind"
    (mind_dir / "config").mkdir(parents=True)
    env_path = tmp_path / "alice.env"
    env_path.write_text(
        f"WORK_DIR={mind_dir}\n"
        f"ALICE_MIND_DIR={mind_dir}\n"
        f"STATE_DIR={tmp_path / 'state'}\n"
    )
    config_json = mind_dir / "config" / "alice.config.json"
    config_json.write_text("{}")
    monkeypatch.setenv("ALICE_CONFIG", str(env_path))
    for var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SIGNAL_ACCOUNT",
    ):
        monkeypatch.delenv(var, raising=False)

    initial_cfg = daemon_module.config_module.load()
    assert initial_cfg.signal_dispatch.forwards == ()

    # Build a minimal stand-in holding just the attributes
    # ``_maybe_reload_config`` touches. Avoids spinning up a full
    # :class:`SpeakingDaemon` (which needs transports, LLM clients,
    # etc.) just to exercise the reload path.
    class _Events:
        def __init__(self) -> None:
            self.emitted: list[tuple[str, dict]] = []

        def emit(self, name: str, **kwargs):
            self.emitted.append((name, kwargs))

    class _Shim:
        _config_path = config_json
        _config_mtime: float = 0.0

        def __init__(self) -> None:
            self.cfg = initial_cfg
            self.events = _Events()

    shim = _Shim()
    daemon_module.SpeakingDaemon._maybe_reload_config(shim)  # type: ignore[arg-type]
    # Initial read pins the mtime.
    assert shim.cfg.signal_dispatch.forwards == ()

    # Now drop a forward entry into alice.config.json and bump mtime.
    config_json.write_text(
        json.dumps(
            {
                "signal": {
                    "dispatch": {
                        "forwards": [
                            {
                                "senders": ["+14357091396"],
                                "target": "http://10.200.0.2:7878",
                                "label": "grandparents-home",
                            }
                        ]
                    }
                }
            }
        )
    )
    import os

    future = shim._config_mtime + 10.0
    os.utime(config_json, (future, future))

    daemon_module.SpeakingDaemon._maybe_reload_config(shim)  # type: ignore[arg-type]

    # Hot-reload must have swapped signal_dispatch — the sender now
    # resolves via the new ruleset without a container restart.
    assert len(shim.cfg.signal_dispatch.forwards) == 1
    rule = shim.cfg.signal_dispatch.lookup("+14357091396")
    assert rule is not None
    assert rule.target == "http://10.200.0.2:7878"
    assert rule.label == "grandparents-home"
    # Change was surfaced on the reload event for observability.
    # The first _maybe_reload_config call pinned mtime with the same
    # cfg we loaded manually (empty forwards both sides, no diff); the
    # second — after the mtime bump — is the one that should carry
    # signal_dispatch in its changes list.
    reload_events = [kw for name, kw in shim.events.emitted if name == "config_reload"]
    assert reload_events, "at least one config_reload event expected"
    assert any("signal_dispatch" in kw["changes"] for kw in reload_events)
