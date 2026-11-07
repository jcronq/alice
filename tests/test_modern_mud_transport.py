"""Tests for the ModernMudTransport: auth gate, 202 flow, callback."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from alice_speaking.transports.base import ChannelRef, OutboundMessage
from alice_speaking.transports.modern_mud import (
    KEY_HEADER,
    SIGNATURE_HEADER,
    ModernMudTransport,
    load_key,
)

KEY = "s3cret"


def _make(calls=None, **kw):
    async def sender(url, body, headers):
        if calls is not None:
            calls.append((url, body, headers))

    return ModernMudTransport(
        key=KEY,
        admin_user_ids={"admin1"},
        callback_url="http://game/api/alice-callback",
        port=0,
        callback_sender=sender,
        **kw,
    )


async def _post(t, payload, key=KEY):
    transport = httpx.ASGITransport(app=t._build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        headers = {KEY_HEADER: key} if key is not None else {}
        return await c.post("/api/modern-mud/turn", json=payload, headers=headers)


BODY = {"user_id": "admin1", "session_id": "s", "message": "hello"}


def test_valid_request_returns_202_and_queues():
    t = _make()
    r = asyncio.run(_post(t, BODY))
    assert r.status_code == 202
    turn_id = r.json()["turn_id"]
    msg = t._inbox.get_nowait()
    assert msg.origin.address == turn_id
    assert msg.principal.native_id == "jason"
    assert msg.text == "hello"


def test_bad_key_401_and_missing_key_401():
    t = _make()
    assert asyncio.run(_post(t, BODY, key="nope")).status_code == 401
    assert asyncio.run(_post(t, BODY, key=None)).status_code == 401
    assert t._inbox.empty()


def test_non_admin_403():
    t = _make()
    r = asyncio.run(_post(t, {**BODY, "user_id": "someone"}))
    assert r.status_code == 403
    assert t._inbox.empty()


def test_callback_success_is_signed_and_carries_turn_id():
    calls = []
    t = _make(calls)

    async def go():
        await t.send(
            OutboundMessage(
                destination=ChannelRef(transport="modern-mud", address="t1", durable=False),
                text="hi there",
            )
        )
        await t.finish_turn("t1")

    asyncio.run(go())
    url, body, headers = calls[0]
    assert url == "http://game/api/alice-callback"
    payload = json.loads(body)
    assert payload == {"turn_id": "t1", "text": "hi there"}
    assert headers[KEY_HEADER] == KEY
    assert headers[SIGNATURE_HEADER] == hmac.new(
        KEY.encode(), body, hashlib.sha256
    ).hexdigest()


def test_callback_error_has_error_field():
    calls = []
    t = _make(calls)
    asyncio.run(t.finish_turn("t2", error="boom"))
    assert json.loads(calls[0][1]) == {"turn_id": "t2", "error": "boom"}


def test_load_key_reads_file(tmp_path):
    p = tmp_path / "k"
    p.write_text("abc\n")
    assert load_key(str(p)) == "abc"
    assert load_key(str(tmp_path / "missing")) == ""
