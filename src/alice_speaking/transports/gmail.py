"""Gmail transport using IMAP for inbound mail and SMTP for outbound mail.

Authentication uses a Gmail app password.  The wire protocol is otherwise
standard email: thread identity comes from RFC ``Message-ID`` /
``In-Reply-To`` / ``References`` headers, so Gmail groups replies without
making the transport depend on Gmail's HTTP API.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import email
import imaplib
import json
import logging
import mimetypes
import os
import pathlib
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from collections import Counter
from typing import AsyncIterator, Callable, Optional

from .base import (
    EMAIL_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GmailAddress:
    recipient: str
    subject: str = ""
    root_message_id: str = ""
    reply_to_message_id: str = ""
    references: tuple[str, ...] = ()


@dataclass
class GmailEvent:
    message: InboundMessage


def _normalize_email(value: str) -> str:
    return parseaddr(value)[1].strip().lower()


def encode_address(address: GmailAddress) -> str:
    """Encode thread-aware delivery data into the transport-private address."""
    payload = {
        "to": _normalize_email(address.recipient),
        "subject": address.subject,
        "root": address.root_message_id,
        "reply_to": address.reply_to_message_id,
        "references": list(address.references),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return "v1:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_address(value: str) -> GmailAddress:
    """Decode a Gmail channel address; a plain email is a new conversation."""
    if not value.startswith("v1:"):
        recipient = _normalize_email(value)
        if not recipient:
            raise ValueError(f"invalid Gmail recipient: {value!r}")
        return GmailAddress(recipient=recipient)
    encoded = value[3:]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        recipient = _normalize_email(payload["to"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Gmail channel address") from exc
    if not recipient:
        raise ValueError("Gmail channel address has no recipient")
    return GmailAddress(
        recipient=recipient,
        subject=str(payload.get("subject") or ""),
        root_message_id=str(payload.get("root") or ""),
        reply_to_message_id=str(payload.get("reply_to") or ""),
        references=tuple(str(v) for v in payload.get("references") or ()),
    )


# Drop counters by reason (telemetry: a spike = spoof attempt or a
# verification regression).
DROP_COUNTS: Counter = Counter()


def parse_authentication_results(
    headers: list[str], trusted_authserv_id: str = "mx.google.com"
) -> Optional[dict[str, dict[str, str]]]:
    """Parse ``Authentication-Results`` headers (RFC 8601).

    Only a header whose authserv-id equals ``trusted_authserv_id`` is
    trusted. Returns ``{method: {"result": ..., "domain": ...}}`` for the
    first trusted header, or ``None`` if there is none.
    """
    for raw in headers:
        parts = [p.strip() for p in " ".join(str(raw).split()).split(";")]
        if not parts:
            continue
        authserv = parts[0].split(" ")[0].strip().lower()
        if authserv != trusted_authserv_id.lower():
            continue
        out: dict[str, dict[str, str]] = {}
        for part in parts[1:]:
            m = re.match(r"(spf|dkim|dmarc)\s*=\s*(\w+)", part, re.I)
            if not m:
                continue
            method = m.group(1).lower()
            if method in out:
                continue
            d = re.search(r"header\.(?:d|i|from)\s*=\s*\"?@?([^\s\";]+)", part, re.I)
            domain = d.group(1).lower().rpartition("@")[2] if d else ""
            out[method] = {"result": m.group(2).lower(), "domain": domain}
        return out
    return None


def _aligned(signing: str, from_domain: str) -> bool:
    return bool(signing) and (
        from_domain == signing or from_domain.endswith("." + signing)
    )


def verify_inbound(
    auth_headers: list[str],
    from_address: str,
    trusted_authserv_id: str = "mx.google.com",
) -> tuple[bool, str]:
    """Fail-closed sender verification. Returns ``(accepted, reason)``."""
    if not auth_headers:
        return False, "no_auth_results"
    results = parse_authentication_results(auth_headers, trusted_authserv_id)
    if results is None:
        return False, "authserv_mismatch"
    if results.get("dmarc", {}).get("result") == "pass":
        return True, "dmarc_pass"
    dkim = results.get("dkim", {})
    if dkim.get("result") == "pass":
        from_domain = from_address.rpartition("@")[2].lower()
        if _aligned(dkim.get("domain", ""), from_domain):
            return True, "dkim_aligned"
        return False, "dkim_pass_from_misalign"
    return False, "dkim_fail_dmarc_fail"


def _header(value: object) -> str:
    if value is None:
        return ""
    return str(make_header(decode_header(str(value))))


def _message_ids(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split() if part.startswith("<") and part.endswith(">"))


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _body_text(msg: Message) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    content = body.get_content()
    if body.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(str(content))
        return " ".join("".join(parser.parts).split())
    return str(content).strip()


class GmailTransport:
    name = "gmail"
    caps: Capabilities = EMAIL_CAPS
    event_type = GmailEvent

    def __init__(
        self,
        *,
        address: str,
        app_password: str,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 465,
        mailbox: str = "INBOX",
        poll_seconds: float = 30.0,
        inbox_size: int = 64,
        imap_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
        smtp_factory: Callable[..., smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
        require_verified_sender: Optional[bool] = None,
        trusted_authserv_id: Optional[str] = None,
    ) -> None:
        self._address = _normalize_email(address)
        if not self._address or not app_password:
            raise ValueError("GmailTransport requires address and app_password")
        self._app_password = app_password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._mailbox = mailbox
        self._poll_seconds = max(1.0, poll_seconds)
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._stopping = asyncio.Event()
        # Fail-closed sender verification. Prefer ctor params (daemon
        # threads cfg values in); fall back to the raw env read so
        # tests / tools constructing the transport without cfg still
        # honor GMAIL_REQUIRE_VERIFIED / GMAIL_TRUSTED_AUTHSERV_ID.
        if require_verified_sender is None:
            require_verified_sender = (
                os.environ.get("GMAIL_REQUIRE_VERIFIED", "1") != "0"
            )
        self._require_verified = require_verified_sender
        self._authserv_id = (
            trusted_authserv_id
            or os.environ.get("GMAIL_TRUSTED_AUTHSERV_ID")
            or "mx.google.com"
        )

    async def start(self) -> None:
        self._stopping.clear()

    async def stop(self) -> None:
        self._stopping.set()

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        return asyncio.create_task(self._produce(ctx), name="gmail-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        while not self._stopping.is_set():
            try:
                fetched = await asyncio.to_thread(self._fetch_unseen)
                for uid, inbound in fetched:
                    principal = ctx.address_book.lookup_by_native(
                        self.name, inbound.principal.native_id
                    )
                    if principal is None or not principal.allowed:
                        log.info(
                            "ignoring Gmail message from unknown sender %s",
                            inbound.principal.native_id,
                        )
                        await asyncio.to_thread(self._mark_seen, uid)
                        continue
                    if self._require_verified:
                        ok, reason = verify_inbound(
                            inbound.metadata.get("auth_results") or [],
                            inbound.principal.native_id,
                            self._authserv_id,
                        )
                        if not ok:
                            DROP_COUNTS[reason] += 1
                            log.warning(
                                "dropping unverified Gmail message from %s: %s",
                                inbound.principal.native_id,
                                reason,
                            )
                            # Durable telemetry for spike detection — a
                            # run of ``gmail_inbound_dropped`` events in
                            # events.jsonl signals attempted spoofing or
                            # a verification regression. Fire before
                            # marking \Seen so the drop is always
                            # recorded, even if IMAP write below fails.
                            emit = getattr(getattr(ctx, "events", None), "emit", None)
                            if emit is not None:
                                try:
                                    emit(
                                        "gmail_inbound_dropped",
                                        reason=reason,
                                        native_id=inbound.principal.native_id,
                                    )
                                except Exception:  # noqa: BLE001
                                    log.exception(
                                        "gmail drop telemetry emit failed"
                                    )
                            await asyncio.to_thread(self._mark_seen, uid)
                            continue
                    inbound.principal = Principal(
                        transport=self.name,
                        native_id=inbound.principal.native_id,
                        display_name=principal.display_name,
                    )
                    ctx.address_book.learn(inbound)
                    event = GmailEvent(message=inbound)
                    divert = getattr(ctx, "divert_to_mid_turn", None)
                    if divert is not None and divert(
                        inbound.origin, inbound.text, event
                    ):
                        await asyncio.to_thread(self._mark_seen, uid)
                        continue
                    await ctx._queue.put(event)
                    await asyncio.to_thread(self._mark_seen, uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Gmail IMAP poll failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _imap(self):
        client = self._imap_factory(
            self._imap_host,
            self._imap_port,
            ssl_context=ssl.create_default_context(),
        )
        client.login(self._address, self._app_password)
        status, _ = client.select(self._mailbox)
        if status != "OK":
            client.logout()
            raise RuntimeError(f"cannot select Gmail mailbox {self._mailbox!r}")
        return client

    def _fetch_unseen(self) -> list[tuple[bytes, InboundMessage]]:
        client = self._imap()
        try:
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("Gmail IMAP UNSEEN search failed")
            result: list[tuple[bytes, InboundMessage]] = []
            for uid in (data[0] or b"").split():
                status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next(
                    (
                        item[1]
                        for item in fetched
                        if isinstance(item, tuple) and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    continue
                inbound = self._parse_message(raw, uid.decode())
                if inbound is not None:
                    result.append((uid, inbound))
            return result
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def _mark_seen(self, uid: bytes) -> None:
        client = self._imap()
        try:
            client.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    def _parse_message(self, raw: bytes, uid: str) -> Optional[InboundMessage]:
        msg = email.message_from_bytes(raw, policy=default)
        sender_name, sender_address = parseaddr(_header(msg.get("From")))
        sender_address = _normalize_email(sender_address)
        if not sender_address or sender_address == self._address:
            return None
        text = _body_text(msg)
        if not text:
            return None
        message_id = str(msg.get("Message-ID") or make_msgid()).strip()
        references = _message_ids(str(msg.get("References") or ""))
        in_reply_to = _message_ids(str(msg.get("In-Reply-To") or ""))
        root = references[0] if references else (in_reply_to[0] if in_reply_to else message_id)
        all_refs = tuple(dict.fromkeys((*references, *in_reply_to, message_id)))
        subject = _header(msg.get("Subject"))
        address = encode_address(
            GmailAddress(
                recipient=sender_address,
                subject=subject,
                root_message_id=root,
                reply_to_message_id=message_id,
                references=all_refs,
            )
        )
        try:
            timestamp = parsedate_to_datetime(str(msg.get("Date"))).timestamp()
        except (TypeError, ValueError, OverflowError):
            timestamp = time.time()
        return InboundMessage(
            principal=Principal(
                transport=self.name,
                native_id=sender_address,
                display_name=sender_name or sender_address,
            ),
            origin=ChannelRef(
                transport=self.name,
                address=address,
                durable=True,
                conversation_id=root,
            ),
            text=text,
            timestamp=timestamp,
            metadata={
                "gmail_uid": uid,
                "message_id": message_id,
                "thread_root_message_id": root,
                "subject": subject,
                "references": list(all_refs),
                "auth_results": [str(v) for v in msg.get_all("Authentication-Results") or []],
            },
        )

    async def send(self, out: OutboundMessage) -> int:
        from ..domain.render import render

        destination = decode_address(out.destination.address)
        chunks = render(out.text, self.caps)
        if not chunks:
            return 0
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            message = EmailMessage()
            message["From"] = self._address
            message["To"] = destination.recipient
            subject = destination.subject or "Message from Alice"
            if destination.reply_to_message_id and not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            message["Subject"] = subject
            message["Message-ID"] = make_msgid(domain=self._address.partition("@")[2])
            if destination.reply_to_message_id:
                message["In-Reply-To"] = destination.reply_to_message_id
                refs = tuple(
                    dict.fromkeys(
                        (*destination.references, destination.reply_to_message_id)
                    )
                )
                message["References"] = " ".join(refs)
            payload = f"({index}/{total}) {chunk}" if total > 1 else chunk
            message.set_content(payload)
            if index == 1:
                for attachment in out.attachments:
                    path = pathlib.Path(attachment)
                    content_type, _ = mimetypes.guess_type(path.name)
                    main, sub = (content_type or "application/octet-stream").split("/", 1)
                    message.add_attachment(
                        path.read_bytes(),
                        maintype=main,
                        subtype=sub,
                        filename=path.name,
                    )
            await asyncio.to_thread(self._send_sync, message)
        return total

    def _send_sync(self, message: EmailMessage) -> None:
        with self._smtp_factory(
            self._smtp_host,
            self._smtp_port,
            context=ssl.create_default_context(),
        ) as client:
            client.login(self._address, self._app_password)
            client.send_message(message)

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        return None

    async def push_lifecycle_event(self, channel: ChannelRef, event: dict) -> None:
        return None

    def build_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        subject: str,
        text: str,
    ) -> str:
        from prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        return load_prompt(
            "speaking.turn.gmail",
            principal_name=principal_name,
            stamp=stamp,
            subject=subject or "(no subject)",
            text=text,
            capability=capability_prompt_fragment("gmail", self.caps),
        )

    async def handle(self, ctx: DaemonContext, event: GmailEvent) -> None:
        from .._dispatch import handle_gmail

        await handle_gmail(ctx, event)
