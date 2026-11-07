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
