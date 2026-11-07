from __future__ import annotations

import asyncio
from email.message import EmailMessage

import pytest

from alice_speaking.transports.base import EMAIL_CAPS, ChannelRef, OutboundMessage
from alice_speaking.transports.gmail import (
    GmailAddress,
    GmailTransport,
    decode_address,
    encode_address,
)
from alice_speaking.infra import config as config_module


def _raw_message(
    *,
    message_id: str,
    subject: str = "Project",
    references: str = "",
    in_reply_to: str = "",
    body: str = "Hello Alice",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Jason <JASON@example.com>"
    msg["To"] = "Alice <alice@example.com>"
    msg["Date"] = "Tue, 23 Jun 2026 12:00:00 +0000"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if references:
        msg["References"] = references
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    return msg.as_bytes()


def test_construction_requires_credentials():
    with pytest.raises(ValueError):
        GmailTransport(address="", app_password="x")
    with pytest.raises(ValueError):
        GmailTransport(address="alice@example.com", app_password="")


def test_caps_and_name():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    assert transport.name == "gmail"
    assert transport.caps is EMAIL_CAPS


def test_config_loads_gmail_settings(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(
        "\n".join(
            (
                f"ALICE_MIND_DIR={tmp_path}",
                "GMAIL_ADDRESS=Alice@Example.com",
                "GMAIL_APP_PASSWORD=abcd efgh ijkl mnop",
                "GMAIL_POLL_SECONDS=12.5",
            )
        )
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    cfg = config_module.load()
    assert cfg.gmail_address == "alice@example.com"
    assert cfg.gmail_app_password == "abcdefghijklmnop"
    assert cfg.gmail_poll_seconds == 12.5


def test_address_codec_supports_plain_recipient_and_thread_context():
    assert decode_address("Person@Example.com") == GmailAddress(
        recipient="person@example.com"
    )
    original = GmailAddress(
        recipient="person@example.com",
        subject="Project",
        root_message_id="<root@example.com>",
        reply_to_message_id="<latest@example.com>",
        references=("<root@example.com>", "<latest@example.com>"),
    )
    assert decode_address(encode_address(original)) == original


def test_parse_message_assigns_stable_conversation_id_across_thread():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    first = transport._parse_message(
        _raw_message(message_id="<root@example.com>"), "1"
    )
    reply = transport._parse_message(
        _raw_message(
            message_id="<reply@example.com>",
            references="<root@example.com>",
            in_reply_to="<root@example.com>",
        ),
        "2",
    )
    assert first is not None
    assert reply is not None
    assert first.principal.native_id == "jason@example.com"
    assert first.origin.conversation_id == "<root@example.com>"
    assert reply.origin.conversation_id == "<root@example.com>"
    assert first.origin.address != reply.origin.address
    assert decode_address(reply.origin.address).reply_to_message_id == (
        "<reply@example.com>"
    )


def test_parse_message_keeps_different_threads_separate():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    one = transport._parse_message(_raw_message(message_id="<one@example.com>"), "1")
    two = transport._parse_message(_raw_message(message_id="<two@example.com>"), "2")
    assert one is not None and two is not None
    assert one.origin.conversation_id != two.origin.conversation_id


def test_send_sets_reply_headers_and_attaches_files(tmp_path):
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def login(self, address, password):
            assert address == "alice@example.com"
            assert password == "secret"

        def send_message(self, message):
            sent.append(message)

    transport = GmailTransport(
        address="alice@example.com",
        app_password="secret",
        smtp_factory=FakeSMTP,
    )
    attachment = tmp_path / "note.txt"
    attachment.write_text("attached")
    destination = GmailAddress(
        recipient="jason@example.com",
        subject="Project",
        root_message_id="<root@example.com>",
        reply_to_message_id="<latest@example.com>",
        references=("<root@example.com>", "<latest@example.com>"),
    )

    async def go():
        return await transport.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="gmail",
                    address=encode_address(destination),
                    durable=True,
                    conversation_id=destination.root_message_id,
                ),
                text="**Status:** done",
                attachments=[str(attachment)],
            )
        )

    assert asyncio.run(go()) == 1
    assert len(sent) == 1
    message = sent[0]
    assert message["To"] == "jason@example.com"
    assert message["Subject"] == "Re: Project"
    assert message["In-Reply-To"] == "<latest@example.com>"
    assert message["References"] == "<root@example.com> <latest@example.com>"
    assert "Status: done" in message.get_body(preferencelist=("plain",)).get_content()
    assert list(message.iter_attachments())[0].get_filename() == "note.txt"


def test_verify_inbound_table():
    from alice_speaking.transports.gmail import verify_inbound

    ok = "mx.google.com; dkim=pass header.d=example.com; spf=pass; dmarc=pass header.from=example.com"
    assert verify_inbound([ok], "a@example.com") == (True, "dmarc_pass")
    assert verify_inbound([], "a@example.com") == (False, "no_auth_results")
    assert verify_inbound(["evil.com; dmarc=pass"], "a@example.com") == (
        False,
        "authserv_mismatch",
    )
    assert verify_inbound(
        ["mx.google.com; dkim=pass header.d=example.com; dmarc=fail"], "a@example.com"
    ) == (True, "dkim_aligned")
    assert verify_inbound(
        ["mx.google.com; dkim=pass header.d=evil.com; dmarc=fail"], "a@example.com"
    ) == (False, "dkim_pass_from_misalign")
    assert verify_inbound(
        ["mx.google.com; spf=pass; dkim=fail; dmarc=fail"], "a@example.com"
    ) == (False, "dkim_fail_dmarc_fail")


# ---------------------------------------------------------------------------
# Config loading — limited-trust env overrides (net-new for issue #682)


def test_config_gmail_limited_trust_defaults_on(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(f"ALICE_MIND_DIR={tmp_path}\n")
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    monkeypatch.delenv("GMAIL_LIMITED_TRUST", raising=False)
    monkeypatch.delenv("GMAIL_LIMITED_TRUST_ALLOWED_TOOLS", raising=False)
    cfg = config_module.load()
    assert cfg.gmail_limited_trust is True
    assert cfg.gmail_limited_trust_allowed_tools == ()
    assert cfg.gmail_require_verified is True
    assert cfg.gmail_trusted_authserv_id == "mx.google.com"


def test_config_gmail_limited_trust_disabled_via_env(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(f"ALICE_MIND_DIR={tmp_path}\n")
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    monkeypatch.setenv("GMAIL_LIMITED_TRUST", "0")
    cfg = config_module.load()
    assert cfg.gmail_limited_trust is False


def test_config_gmail_allowed_tools_parses_csv(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(f"ALICE_MIND_DIR={tmp_path}\n")
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    monkeypatch.setenv(
        "GMAIL_LIMITED_TRUST_ALLOWED_TOOLS", "Bash,  Write , , Edit"
    )
    cfg = config_module.load()
    assert cfg.gmail_limited_trust_allowed_tools == ("Bash", "Write", "Edit")


def test_config_gmail_require_verified_disabled_via_env(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(f"ALICE_MIND_DIR={tmp_path}\n")
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    monkeypatch.setenv("GMAIL_REQUIRE_VERIFIED", "0")
    cfg = config_module.load()
    assert cfg.gmail_require_verified is False


# ---------------------------------------------------------------------------
# Drop telemetry integration — ``ctx.events.emit("gmail_inbound_dropped", …)``


def test_unverified_inbound_emits_drop_event_and_skips_queue(monkeypatch):
    """A principal-matching inbound with no Authentication-Results header
    is dropped; ``ctx.events.emit`` fires with the classified reason and
    ``ctx._queue.put`` is NOT reached."""
    from types import SimpleNamespace

    from alice_speaking.transports.gmail import (
        DROP_COUNTS,
        GmailTransport,
        verify_inbound,
    )
    from alice_speaking.transports.base import ChannelRef, InboundMessage, Principal

    # Reset in-memory counter so the assertion is deterministic across
    # test ordering.
    DROP_COUNTS.clear()

    transport = GmailTransport(
        address="alice@example.com",
        app_password="x",
        require_verified_sender=True,
        trusted_authserv_id="mx.google.com",
    )

    # Craft one fetched inbound with a matching principal but no
    # Authentication-Results header — that is the "no_auth_results"
    # bucket in ``verify_inbound``.
    inbound = InboundMessage(
        principal=Principal(
            transport="gmail",
            native_id="jason@example.com",
            display_name="Jason",
        ),
        origin=ChannelRef(
            transport="gmail",
            address="jason@example.com",
            durable=True,
            conversation_id="<root@example.com>",
        ),
        text="hello",
        timestamp=0.0,
        metadata={"auth_results": []},
    )

    # Stop the transport immediately once the loop finishes processing
    # the single fetch batch, so the test doesn't hang on the poll wait.
    async def one_shot_fetch():
        transport._stopping.set()
        return [(b"42", inbound)]

    marked_seen: list[bytes] = []
    transport._mark_seen = lambda uid: marked_seen.append(uid)  # type: ignore[assignment]

    # Replace _fetch_unseen so asyncio.to_thread returns our one message.
    def fake_fetch_unseen():
        transport._stopping.set()
        return [(b"42", inbound)]

    transport._fetch_unseen = fake_fetch_unseen  # type: ignore[assignment]

    emits: list[tuple[str, dict]] = []

    class _Events:
        def emit(self, name, **fields):
            emits.append((name, fields))

    queue_puts: list = []

    class _Queue:
        async def put(self, item):
            queue_puts.append(item)

    class _Principal:
        allowed = True
        display_name = "Jason"

    class _AddressBook:
        def lookup_by_native(self, transport, native_id):
            return _Principal() if native_id == "jason@example.com" else None

        def learn(self, inbound):
            pass

    ctx = SimpleNamespace(
        address_book=_AddressBook(),
        events=_Events(),
        _queue=_Queue(),
    )

    async def run_once():
        await transport._produce(ctx)

    asyncio.run(run_once())

    # One drop event was emitted with the right fields.
    assert any(
        name == "gmail_inbound_dropped"
        and fields.get("reason") == "no_auth_results"
        and fields.get("native_id") == "jason@example.com"
        for name, fields in emits
    ), emits
    # The turn never fired (nothing enqueued).
    assert queue_puts == []
    # Message was marked \Seen so it isn't re-polled.
    assert marked_seen == [b"42"]
    # In-memory counter kept in sync for non-events consumers.
    assert DROP_COUNTS["no_auth_results"] == 1


@pytest.mark.parametrize(
    "auth_headers,expected_reason",
    [
        ([], "no_auth_results"),
        (
            ["evil-mx.attacker.com; dmarc=pass header.from=example.com"],
            "authserv_mismatch",
        ),
        (
            ["mx.google.com; dkim=pass header.d=evil.com; dmarc=fail"],
            "dkim_pass_from_misalign",
        ),
        (
            ["mx.google.com; spf=pass; dkim=fail; dmarc=fail"],
            "dkim_fail_dmarc_fail",
        ),
    ],
)
def test_drop_reason_classification_across_four_buckets(
    auth_headers, expected_reason
):
    """``verify_inbound`` is the drop classifier — a message routed
    through it must land in exactly one of the four reason buckets
    issue #682 asks us to count."""
    from alice_speaking.transports.gmail import verify_inbound

    accepted, reason = verify_inbound(auth_headers, "jason@example.com")
    assert accepted is False
    assert reason == expected_reason
