"""Tests for ``alice_speaking.infra.presence`` + the ``_send_message``
suppression path for status-chat replies on Signal.

Covers:
- ``check_focused`` happy paths (focused True / False) + every fail-
  open mode (timeout, non-200, bad JSON, bad shape, URL error).
- The suppression decision: only fires for signal+status-chat+focused;
  every other combination sends normally.
- The emit side-effect on the daemon ``events`` logger.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alice_speaking.infra import presence as presence_mod


# ─── check_focused ─────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class _FakeOpener:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def open(self, req, timeout=None):  # noqa: D401 — opener protocol
        if self._exc is not None:
            raise self._exc
        return self._resp


def test_check_focused_happy_path_focused():
    body = json.dumps(
        {"focused": True, "focused_within_s": 3.2, "window_s": 45.0}
    ).encode()
    op = _FakeOpener(resp=_FakeResp(200, body))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is True
    assert r.focused_within_s == 3.2
    assert r.error is None


def test_check_focused_happy_path_not_focused():
    body = json.dumps(
        {"focused": False, "focused_within_s": 123.0}
    ).encode()
    op = _FakeOpener(resp=_FakeResp(200, body))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False


def test_check_focused_non_200_fails_open():
    op = _FakeOpener(resp=_FakeResp(500, b""))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False
    assert r.error is not None and "500" in r.error


def test_check_focused_bad_json_fails_open():
    op = _FakeOpener(resp=_FakeResp(200, b"not-json"))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False
    assert r.error is not None and "bad json" in r.error


def test_check_focused_bad_shape_fails_open():
    # Server returned JSON, but a list instead of a dict.
    op = _FakeOpener(resp=_FakeResp(200, b"[1, 2, 3]"))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False
    assert r.error == "bad shape"


def test_check_focused_url_error_fails_open():
    import urllib.error

    op = _FakeOpener(exc=urllib.error.URLError("boom"))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False
    assert r.error is not None and "URLError" in r.error


def test_check_focused_timeout_fails_open():
    op = _FakeOpener(exc=TimeoutError("slow"))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is False
    assert r.error is not None and "TimeoutError" in r.error


def test_check_focused_missing_within_s_normalises_to_none():
    body = json.dumps({"focused": True}).encode()
    op = _FakeOpener(resp=_FakeResp(200, body))
    r = presence_mod.check_focused(opener=op)
    # focused_within_s is best-effort; server might not include it.
    assert r.focused is True
    assert r.focused_within_s is None


def test_check_focused_requires_strict_true_rejects_truthy_strings():
    # Review finding #6: a malformed server returning truthy non-
    # bool (e.g. the literal string "false", or 1, or "yes") must
    # NOT be read as focused — that would be fail-closed, the
    # opposite of our invariant.
    for payload in (
        {"focused": "false"},  # truthy string
        {"focused": "yes"},
        {"focused": 1},
        {"focused": "true"},
        {"focused": [1]},
    ):
        body = json.dumps(payload).encode()
        op = _FakeOpener(resp=_FakeResp(200, body))
        r = presence_mod.check_focused(opener=op)
        assert r.focused is False, f"should reject {payload!r}"


def test_check_focused_rejects_bool_as_number_for_within_s():
    # ``isinstance(True, int)`` is True — guard against a server that
    # accidentally serialises a bool where a number is expected.
    body = json.dumps({"focused": True, "focused_within_s": True}).encode()
    op = _FakeOpener(resp=_FakeResp(200, body))
    r = presence_mod.check_focused(opener=op)
    assert r.focused is True
    assert r.focused_within_s is None


class _KeyCapturingOpener:
    """Captures the final URL the opener was called with, so we can
    assert the shared-key query param was appended."""

    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.last_url: str | None = None

    def open(self, req, timeout=None):
        self.last_url = getattr(req, "full_url", str(req.get_full_url()))
        return self._resp


def test_check_focused_appends_shared_key_when_provided():
    body = json.dumps({"focused": True, "focused_within_s": 1.0}).encode()
    op = _KeyCapturingOpener(_FakeResp(200, body))
    presence_mod.check_focused(
        url="http://example/api/presence/status",
        opener=op,
        key="sekret!/key",
    )
    assert op.last_url is not None
    assert "?k=sekret%21%2Fkey" in op.last_url


def test_check_focused_respects_existing_query_string():
    body = json.dumps({"focused": True}).encode()
    op = _KeyCapturingOpener(_FakeResp(200, body))
    presence_mod.check_focused(
        url="http://example/api/presence/status?v=1",
        opener=op,
        key="abc",
    )
    assert op.last_url is not None
    assert op.last_url.endswith("?v=1&k=abc")


def test_check_focused_without_key_sends_no_query_param(tmp_path, monkeypatch):
    # Point the key loader at a non-existent file so _load_key
    # returns None; ``check_focused`` must then GET without ``?k=``.
    monkeypatch.setenv("ALICE_STATUS_KEY_FILE", str(tmp_path / "missing"))
    # Reload the module so it rebinds the key path env var.
    import importlib

    importlib.reload(presence_mod)
    body = json.dumps({"focused": False}).encode()
    op = _KeyCapturingOpener(_FakeResp(200, body))
    presence_mod.check_focused(
        url="http://example/api/presence/status", opener=op
    )
    assert op.last_url == "http://example/api/presence/status"


# ─── _send_message suppression ─────────────────────────────────────────


def _signal_channel():
    from alice_speaking.transports import ChannelRef

    return ChannelRef(
        transport="signal", address="+14357091512", durable=True
    )


def _discord_channel():
    from alice_speaking.transports import ChannelRef

    return ChannelRef(
        transport="discord", address="user#1234", durable=True
    )


def _build_daemon_stub(current_turn_is_status_chat: bool):
    """Return a SimpleNamespace that quacks enough like SpeakingDaemon
    for ``_send_message`` to execute without the whole kitchen sink."""
    daemon = SimpleNamespace()
    daemon._current_turn_is_status_chat = current_turn_is_status_chat
    daemon._current_turn_kind = "surface"
    daemon._emergency_bypass = False
    daemon._current_reply_channel = None
    daemon._turn_did_send = False
    daemon._turn_last_outbound = None
    daemon._current_turn_replied = False
    daemon.events = MagicMock()
    daemon._dispatch_outbound = AsyncMock()

    # face is an async-context-manager stub.
    class _Face:
        def speaking(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    daemon.face = _Face()
    return daemon


@pytest.mark.asyncio
async def test_send_message_suppresses_on_status_chat_focused():
    """Status-chat surface + signal transport + focused → suppress."""
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=True)
    channel = _signal_channel()

    with patch.object(
        presence_mod,
        "check_focused",
        return_value=presence_mod.PresenceReading(
            focused=True, focused_within_s=1.0
        ),
    ) as cf:
        # Bind the real coroutine to our stub.
        await SpeakingDaemon._send_message(daemon, channel, "hi")

    cf.assert_called_once()
    daemon._dispatch_outbound.assert_not_awaited()
    daemon.events.emit.assert_called_once()
    args, kwargs = daemon.events.emit.call_args
    assert args[0] == "signal_suppressed_presence"
    assert daemon._turn_did_send is True
    assert daemon._current_turn_replied is True
    assert daemon._turn_last_outbound == "hi"


@pytest.mark.asyncio
async def test_send_message_sends_on_status_chat_unfocused():
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=True)
    channel = _signal_channel()

    with patch.object(
        presence_mod,
        "check_focused",
        return_value=presence_mod.PresenceReading(
            focused=False, focused_within_s=None
        ),
    ):
        await SpeakingDaemon._send_message(daemon, channel, "hi")

    daemon._dispatch_outbound.assert_awaited_once()
    # No suppression event emitted.
    emitted = [c.args[0] for c in daemon.events.emit.call_args_list]
    assert "signal_suppressed_presence" not in emitted


@pytest.mark.asyncio
async def test_send_message_sends_when_presence_check_fails_fail_open():
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=True)
    channel = _signal_channel()

    with patch.object(
        presence_mod,
        "check_focused",
        return_value=presence_mod.PresenceReading.not_focused(
            error="URLError: Connection refused"
        ),
    ):
        await SpeakingDaemon._send_message(daemon, channel, "hi")

    daemon._dispatch_outbound.assert_awaited_once()
    emitted = [c.args[0] for c in daemon.events.emit.call_args_list]
    assert "signal_suppress_check_failed" in emitted
    assert "signal_suppressed_presence" not in emitted


@pytest.mark.asyncio
async def test_send_message_never_suppresses_non_status_chat_surface():
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=False)
    channel = _signal_channel()

    with patch.object(presence_mod, "check_focused") as cf:
        await SpeakingDaemon._send_message(daemon, channel, "hi")

    # Presence must not even be queried.
    cf.assert_not_called()
    daemon._dispatch_outbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_never_suppresses_non_signal_transport():
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=True)
    channel = _discord_channel()

    with patch.object(presence_mod, "check_focused") as cf:
        await SpeakingDaemon._send_message(daemon, channel, "hi")

    cf.assert_not_called()
    daemon._dispatch_outbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_never_suppresses_emergency():
    from alice_speaking.daemon import SpeakingDaemon

    daemon = _build_daemon_stub(current_turn_is_status_chat=True)
    daemon._emergency_bypass = True
    channel = _signal_channel()

    with patch.object(presence_mod, "check_focused") as cf:
        await SpeakingDaemon._send_message(daemon, channel, "URGENT")

    cf.assert_not_called()
    daemon._dispatch_outbound.assert_awaited_once()
