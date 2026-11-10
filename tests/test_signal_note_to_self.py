"""Note-to-Self ingestion for Signal (Alice as a linked device on the owner's
own account) and the guards that keep her from replying to herself forever.

Real signal-cli shape: a message the owner types in "Note to Self" on their
phone reaches Alice as ``syncMessage.sentMessage`` with ``destination`` equal
to the account's own number — never as a ``dataMessage``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib


from alice_speaking.infra import signal_rpc
from alice_speaking.infra.signal_rpc import SignalRPC, _parse_envelope

ME = "+15555550100"
OTHER = "+15555550199"
UUID = "184b7dd7-3315-4a8c-b7e8-df9010762d09"


def _sync_line(
    *,
    body: str | None = "hello",
    dest: str | None = ME,
    ts: int = 1_000,
    source: str = ME,
    dest_uuid: str | None = UUID,
    group: bool = False,
    attachments: list | None = None,
) -> str:
    sent: dict = {"timestamp": ts}
    if body is not None:
        sent["message"] = body
    if dest is not None:
        sent["destinationNumber"] = dest
        sent["destination"] = dest
    if dest_uuid is not None:
        sent["destinationUuid"] = dest_uuid
    if group:
        sent["groupInfo"] = {"groupId": "abc"}
    if attachments is not None:
        sent["attachments"] = attachments
    return json.dumps(
        {
            "envelope": {
                "source": source,
                "sourceNumber": source,
                "sourceUuid": UUID,
                "timestamp": ts + 5,
                "syncMessage": {"sentMessage": sent},
            },
            "account": ME,
        }
    )


def _data_line(*, body: str = "hi", source: str = OTHER, ts: int = 2_000) -> str:
    return json.dumps(
        {
            "envelope": {
                "source": source,
                "sourceNumber": source,
                "timestamp": ts,
                "dataMessage": {"message": body, "timestamp": ts},
            }
        }
    )


def _client(tmp_path: pathlib.Path, *, note_to_self: bool = True) -> SignalRPC:
    return SignalRPC(
        api="http://127.0.0.1:1",
        account=ME,
        log_path=tmp_path / "log",
        offset_path=tmp_path / "offset",
        note_to_self=note_to_self,
    )


# -- parser ----------------------------------------------------------------


def test_note_to_self_accepted_when_enabled() -> None:
    env = _parse_envelope(_sync_line(body="what's the word?", ts=1234), account=ME)
    assert env is not None
    assert env.body == "what's the word?"
    assert env.source == ME
    assert env.timestamp == 1234  # sentMessage timestamp, not the envelope's
    assert env.from_self_sync is True


def test_note_to_self_ignored_when_disabled() -> None:
    # No account passed => feature off => today's behaviour (drop all syncs).
    assert _parse_envelope(_sync_line()) is None


def test_sync_sent_to_someone_else_ignored() -> None:
    assert _parse_envelope(_sync_line(dest=OTHER), account=ME) is None


def test_sync_with_no_destination_ignored() -> None:
    assert _parse_envelope(_sync_line(dest=None, dest_uuid=None), account=ME) is None


def test_group_sync_ignored() -> None:
    assert _parse_envelope(_sync_line(group=True), account=ME) is None


def test_uuid_mismatch_ignored() -> None:
    line = _sync_line(dest_uuid="00000000-0000-0000-0000-000000000000")
    assert _parse_envelope(line, account=ME) is None


def test_sync_from_other_source_ignored() -> None:
    assert _parse_envelope(_sync_line(source=OTHER), account=ME) is None


def test_empty_sync_ignored() -> None:
    assert _parse_envelope(_sync_line(body=None), account=ME) is None
    assert _parse_envelope(_sync_line(body=""), account=ME) is None


def test_sync_attachment_only_accepted() -> None:
    line = _sync_line(
        body=None,
        attachments=[{"id": "a.jpg", "contentType": "image/jpeg", "size": 3}],
    )
    env = _parse_envelope(line, account=ME)
    assert env is not None and len(env.attachments) == 1 and env.from_self_sync


def test_normal_data_message_unchanged_with_feature_on() -> None:
    env = _parse_envelope(_data_line(), account=ME)
    assert env is not None
    assert env.source == OTHER
    assert env.from_self_sync is False


# -- loop guards -----------------------------------------------------------


def _env(body: str, ts: int) -> signal_rpc.SignalEnvelope:
    return signal_rpc.SignalEnvelope(
        timestamp=ts, source=ME, body=body, from_self_sync=True
    )


def test_guard_accepts_first_owner_note(tmp_path) -> None:
    assert _client(tmp_path)._allow_self_note(_env("hello", 1)) is True


def test_guard_drops_echo_matching_send_timestamp(tmp_path) -> None:
    c = _client(tmp_path)
    c._remember_self_send("reply text", {"result": {"timestamp": 777}})
    # Different text on purpose: only the timestamp should trigger the drop.
    assert c._allow_self_note(_env("something else", 777)) is False


def test_guard_drops_echo_matching_recent_text(tmp_path) -> None:
    c = _client(tmp_path)
    c._remember_self_send("the reply", {"result": {}})  # no ts in response
    assert c._allow_self_note(_env("the reply", 999)) is False


def test_guard_text_match_expires(tmp_path) -> None:
    c = _client(tmp_path)
    c._remember_self_send("the reply", {"result": {}})
    later = signal_rpc.time.monotonic() + signal_rpc._SELF_SENT_TEXT_WINDOW_S + 1
    assert c._allow_self_note(_env("the reply", 999), now=later) is True


def test_guard_drops_duplicate_timestamp(tmp_path) -> None:
    c = _client(tmp_path)
    assert c._allow_self_note(_env("a", 5)) is True
    assert c._allow_self_note(_env("a", 5)) is False


def test_guard_circuit_breaker_caps_runaway(tmp_path) -> None:
    c = _client(tmp_path)
    t0 = signal_rpc.time.monotonic()
    accepted = [
        c._allow_self_note(_env(f"m{i}", 100 + i), now=t0 + i * 0.1)
        for i in range(signal_rpc._SELF_RATE_LIMIT + 4)
    ]
    assert accepted.count(True) == signal_rpc._SELF_RATE_LIMIT
    assert accepted[signal_rpc._SELF_RATE_LIMIT :] == [False] * 4


def test_guard_rate_window_recovers(tmp_path) -> None:
    c = _client(tmp_path)
    t0 = signal_rpc.time.monotonic()
    for i in range(signal_rpc._SELF_RATE_LIMIT):
        assert c._allow_self_note(_env(f"m{i}", 100 + i), now=t0) is True
    assert c._allow_self_note(_env("blocked", 900), now=t0 + 1) is False
    after = t0 + signal_rpc._SELF_RATE_WINDOW_S + 1
    assert c._allow_self_note(_env("fine again", 901), now=after) is True


# -- send() bookkeeping ----------------------------------------------------


def _stub_rpc(client: SignalRPC, ts: int | None) -> list[dict]:
    calls: list[dict] = []

    async def fake_rpc(method, params, *, request_id="rpc"):
        calls.append(params)
        return {"result": {"timestamp": ts}} if ts is not None else {"result": {}}

    client._rpc = fake_rpc  # type: ignore[method-assign]
    return calls


def test_send_to_self_records_timestamp_and_text(tmp_path) -> None:
    c = _client(tmp_path)
    _stub_rpc(c, 4242)
    asyncio.run(c.send(ME, "reply"))
    assert 4242 in c._sent_ts
    assert any(text == "reply" for _, text in c._sent_texts)


def test_send_to_other_does_not_record(tmp_path) -> None:
    c = _client(tmp_path)
    _stub_rpc(c, 4242)
    asyncio.run(c.send(OTHER, "reply"))
    assert not c._sent_ts and not c._sent_texts


def test_send_does_not_record_when_feature_off(tmp_path) -> None:
    c = _client(tmp_path, note_to_self=False)
    _stub_rpc(c, 4242)
    asyncio.run(c.send(ME, "reply"))
    assert not c._sent_ts and not c._sent_texts


# -- end-to-end loop simulation --------------------------------------------


def test_reply_loop_is_broken(tmp_path) -> None:
    """Owner note -> Alice replies to self -> the echo of that reply must not
    come back as a new inbound message, even if signal-cli syncs it."""
    c = _client(tmp_path)
    _stub_rpc(c, 5000)

    inbound = _parse_envelope(_sync_line(body="ping", ts=1000), account=ME)
    assert inbound is not None and c._allow_self_note(inbound) is True

    asyncio.run(c.send(inbound.source, "pong"))  # Alice's reply, ts=5000

    echo_by_ts = _parse_envelope(_sync_line(body="pong (edited?)", ts=5000), account=ME)
    echo_by_text = _parse_envelope(_sync_line(body="pong", ts=5001), account=ME)
    assert echo_by_ts is not None and echo_by_text is not None
    assert c._allow_self_note(echo_by_ts) is False
    assert c._allow_self_note(echo_by_text) is False

    # A genuine follow-up from the owner still gets through.
    nxt = _parse_envelope(_sync_line(body="thanks", ts=6000), account=ME)
    assert nxt is not None and c._allow_self_note(nxt) is True


def test_receive_filters_log_end_to_end(tmp_path) -> None:
    """Drive the real tail loop: sync note + echo + normal DM in one log."""
    c = _client(tmp_path)
    c._remember_self_send("echoed reply", {"result": {"timestamp": 3000}})
    log = tmp_path / "log"
    log.write_text(
        "\n".join(
            [
                "INFO not json",
                _sync_line(body="owner note", ts=1000),
                _sync_line(body="echoed reply", ts=3000),
                _sync_line(body="to someone else", dest=OTHER, ts=3500),
                _data_line(body="dm from other", ts=4000),
            ]
        )
        + "\n"
    )

    async def collect() -> list[str]:
        out: list[str] = []

        async def run() -> None:
            async for env in c.receive():
                out.append(env.body)
                if len(out) == 2:
                    return

        await asyncio.wait_for(run(), timeout=5)
        return out

    assert asyncio.run(collect()) == ["owner note", "dm from other"]
