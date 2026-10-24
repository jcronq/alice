from __future__ import annotations

import asyncio
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Union

import pytest

from alice_speaking.transports.base import EMAIL_CAPS, ChannelRef, OutboundMessage
from alice_speaking.transports.gmail import (
    GmailAddress,
    GmailTransport,
    _domains_aligned,
    _evaluate_sender_auth,
    _parse_authentication_results,
    decode_address,
    encode_address,
)
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)
from alice_speaking.infra import config as config_module


# A DMARC-pass verdict as Gmail would stamp it for a genuine gmail.com sender.
GOOD_AUTH = (
    "mx.google.com; dkim=pass header.i=@example.com header.s=sel header.b=AbC; "
    "spf=pass (google.com: domain of jason@example.com designates 1.2.3.4) "
    "smtp.mailfrom=jason@example.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) "
    "header.from=example.com"
)


def _raw_message(
    *,
    message_id: str,
    subject: str = "Project",
    references: str = "",
    in_reply_to: str = "",
    body: str = "Hello Alice",
    auth_results: Union[str, list[str], None] = None,
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
    if auth_results is not None:
        values = [auth_results] if isinstance(auth_results, str) else auth_results
        for value in values:
            msg["Authentication-Results"] = value
    msg.set_content(body)
    return msg.as_bytes()


def _fake_imap_factory():
    """IMAP client that no-ops login/select/store so ``_mark_seen`` works
    in tests without touching the network."""

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args):
            return ("OK", [b""])

        def select(self, mailbox):
            return ("OK", [b"1"])

        def uid(self, *args):
            return ("OK", [b""])

        def logout(self):
            return ("BYE", [b""])

    return FakeIMAP


def _jason_book() -> AddressBook:
    return AddressBook(
        [
            PrincipalRecord(
                id="jason",
                display_name="Jason",
                channels=[
                    PrincipalChannel(transport="gmail", address="jason@example.com")
                ],
            )
        ]
    )


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
    # Sender verification is fail-closed by default.
    assert cfg.gmail_require_verified is True
    assert cfg.gmail_trusted_authserv_id == "mx.google.com"


def test_config_verification_can_be_disabled(tmp_path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(
        "\n".join(
            (
                f"ALICE_MIND_DIR={tmp_path}",
                "GMAIL_ADDRESS=alice@example.com",
                "GMAIL_APP_PASSWORD=secret",
                "GMAIL_REQUIRE_VERIFIED=0",
                "GMAIL_TRUSTED_AUTHSERV_ID=mx.internal.test",
            )
        )
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env_file))
    cfg = config_module.load()
    assert cfg.gmail_require_verified is False
    assert cfg.gmail_trusted_authserv_id == "mx.internal.test"


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


# ---------------------------------------------------------------------------
# Sender authentication (Recommendation 1: fail-closed From verification).


def test_domains_aligned():
    assert _domains_aligned("example.com", "example.com")
    assert _domains_aligned("mail.example.com", "example.com")  # relaxed subdomain
    assert _domains_aligned("example.com.", "example.com")  # trailing dot
    assert not _domains_aligned("evil.com", "example.com")
    assert not _domains_aligned("", "example.com")
    assert not _domains_aligned("example.com", "")


def test_parse_authentication_results_selects_trusted_authserv_id():
    # A forged verdict claiming pass sits alongside Gmail's real fail verdict.
    forged = "attacker.example; dkim=pass; spf=pass; dmarc=pass"
    real = "mx.google.com; dkim=fail; spf=softfail; dmarc=fail"
    parsed = _parse_authentication_results([forged, real], "mx.google.com")
    assert parsed["dkim"] == "fail"
    assert parsed["dmarc"] == "fail"


def test_parse_authentication_results_missing_trusted_header():
    forged = "attacker.example; dkim=pass; dmarc=pass"
    assert _parse_authentication_results([forged], "mx.google.com") == {}


def test_evaluate_sender_auth_rules():
    assert _evaluate_sender_auth({}, "example.com")[0] is False
    # dmarc=pass is sufficient on its own.
    assert _evaluate_sender_auth({"dmarc": "pass"}, "example.com")[0] is True
    # dkim=pass with an aligned signing domain passes even without dmarc.
    ok, _ = _evaluate_sender_auth(
        {"dkim": "pass", "dmarc": "none", "dkim_domain": "example.com"},
        "example.com",
    )
    assert ok is True
    # dkim=pass but signed by a different domain does NOT authenticate From.
    bad, _ = _evaluate_sender_auth(
        {"dkim": "pass", "dmarc": "none", "dkim_domain": "evil.com"},
        "example.com",
    )
    assert bad is False
    # spf alone is never sufficient (it authenticates the envelope, not From).
    spf_only, _ = _evaluate_sender_auth(
        {"spf": "pass", "dkim": "none", "dmarc": "none"}, "example.com"
    )
    assert spf_only is False


def test_parse_message_marks_verified_sender():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    inbound = transport._parse_message(
        _raw_message(message_id="<a@example.com>", auth_results=GOOD_AUTH), "1"
    )
    assert inbound is not None
    assert inbound.metadata["sender_verified"] is True
    assert inbound.metadata["sender_auth"]["dmarc"] == "pass"


def test_parse_message_marks_unverified_when_no_auth_header():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    inbound = transport._parse_message(_raw_message(message_id="<a@example.com>"), "1")
    assert inbound is not None
    assert inbound.metadata["sender_verified"] is False


def test_parse_message_rejects_forged_authserv_id():
    transport = GmailTransport(address="alice@example.com", app_password="x")
    forged = "evil.relay; dkim=pass header.d=example.com; dmarc=pass"
    inbound = transport._parse_message(
        _raw_message(message_id="<a@example.com>", auth_results=forged), "1"
    )
    assert inbound is not None
    assert inbound.metadata["sender_verified"] is False


def test_accept_message_gate_enforces_verification():
    transport = GmailTransport(
        address="alice@example.com",
        app_password="x",
        imap_factory=_fake_imap_factory(),
    )
    ctx = SimpleNamespace(address_book=_jason_book(), _queue=asyncio.Queue())

    verified = transport._parse_message(
        _raw_message(message_id="<good@example.com>", auth_results=GOOD_AUTH), "1"
    )
    spoofed = transport._parse_message(
        _raw_message(message_id="<spoof@example.com>"), "2"
    )
    assert verified.metadata["sender_verified"] is True
    assert spoofed.metadata["sender_verified"] is False

    async def go():
        await transport._accept_message(ctx, b"1", verified)
        after_verified = ctx._queue.qsize()
        await transport._accept_message(ctx, b"2", spoofed)
        after_spoof = ctx._queue.qsize()
        return after_verified, after_spoof

    after_verified, after_spoof = asyncio.run(go())
    assert after_verified == 1  # verified message ran
    assert after_spoof == 1  # spoof was dropped, queue unchanged


def test_accept_message_gate_can_be_disabled():
    transport = GmailTransport(
        address="alice@example.com",
        app_password="x",
        require_verified_sender=False,
        imap_factory=_fake_imap_factory(),
    )
    ctx = SimpleNamespace(address_book=_jason_book(), _queue=asyncio.Queue())
    spoofed = transport._parse_message(
        _raw_message(message_id="<spoof@example.com>"), "1"
    )

    async def go():
        await transport._accept_message(ctx, b"1", spoofed)
        return ctx._queue.qsize()

    assert asyncio.run(go()) == 1  # unverified allowed when gate is off
