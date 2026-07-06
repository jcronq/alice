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
import pathlib
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
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


def _header(value: object) -> str:
    if value is None:
        return ""
    return str(make_header(decode_header(str(value))))


def _message_ids(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split() if part.startswith("<") and part.endswith(">"))


# ---------------------------------------------------------------------------
# Sender authentication (SPF / DKIM / DMARC).
#
# Alice fetches mail via IMAP from Gmail's own servers, which means Gmail has
# already validated SPF/DKIM/DMARC and stamped the verdict into an
# ``Authentication-Results`` header. We read that verdict rather than doing
# the crypto ourselves — but only the copy stamped by Gmail's own
# authserv-id is trustworthy. A receiving MTA strips any inbound
# ``Authentication-Results`` bearing its own authserv-id before adding its
# real one (RFC 8601 §5), so a match on ``mx.google.com`` is Gmail's genuine
# verdict; an attacker-forged copy sitting in the message body carries a
# different authserv-id (or none) and is ignored. Missing verdict → fail
# closed (treated as unverified), never open.


def _parse_authentication_results(
    raw_values: list[str], trusted_authserv_id: str
) -> dict[str, str]:
    """Extract SPF/DKIM/DMARC results from the trusted ``Authentication-Results``.

    Returns ``{}`` when no header stamped by ``trusted_authserv_id`` is
    present. Otherwise returns ``{dkim, spf, dmarc, dkim_domain}`` where each
    method is its result token (``pass`` / ``fail`` / ``none`` / …) and
    ``dkim_domain`` is the DKIM signing domain (``header.d`` preferred, else
    ``header.i``) used for alignment checks.
    """
    trusted = trusted_authserv_id.strip().lower()
    for value in raw_values:
        collapsed = " ".join(str(value).split())
        if not collapsed:
            continue
        head, _sep, rest = collapsed.partition(";")
        head_tokens = head.strip().split()
        authserv_id = head_tokens[0].lower() if head_tokens else ""
        if authserv_id != trusted:
            continue
        results: dict[str, str] = {"dkim": "none", "spf": "none", "dmarc": "none"}
        dkim_domain = ""
        for chunk in rest.split(";"):
            method, sep, tail = chunk.strip().partition("=")
            method = method.strip().lower()
            if not sep or method not in results or results[method] != "none":
                # Unknown method, malformed, or already recorded (first
                # entry wins when a message carries multiple signatures).
                continue
            tokens = tail.split()
            results[method] = tokens[0].strip().lower() if tokens else "none"
            if method == "dkim":
                d_domain = ""
                i_domain = ""
                for tok in tokens[1:]:
                    low = tok.lower()
                    if low.startswith("header.d="):
                        d_domain = tok.partition("=")[2]
                    elif low.startswith("header.i="):
                        i_domain = tok.partition("=")[2].lstrip("@").rpartition("@")[2]
                dkim_domain = (d_domain or i_domain).strip().lower().rstrip(";")
        results["dkim_domain"] = dkim_domain
        return results
    return {}


def _domains_aligned(a: str, b: str) -> bool:
    """Relaxed DMARC-style alignment: equal domains or one a subdomain of
    the other (case-insensitive, trailing dot ignored)."""
    a = (a or "").strip().lower().rstrip(".")
    b = (b or "").strip().lower().rstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _evaluate_sender_auth(auth: dict[str, str], from_domain: str) -> tuple[bool, str]:
    """Decide whether the ``From`` domain is authenticated.

    A ``dmarc=pass`` verdict already proves an SPF- or DKIM-based
    identifier aligned with the ``From`` domain, so it is sufficient on its
    own. As a fallback (for senders whose domain publishes no DMARC record)
    we accept a ``dkim=pass`` whose signing domain aligns with ``From``.
    SPF alone is intentionally NOT sufficient — it authenticates the
    envelope sender, not the visible ``From`` header.
    """
    if not auth:
        return False, "no Authentication-Results header from trusted authserv-id"
    if auth.get("dmarc") == "pass":
        return True, "dmarc=pass"
    if auth.get("dkim") == "pass" and _domains_aligned(
        from_domain, auth.get("dkim_domain", "")
    ):
        return True, f"dkim=pass aligned ({auth.get('dkim_domain')})"
    return False, (
        f"unauthenticated (dkim={auth.get('dkim')} spf={auth.get('spf')} "
        f"dmarc={auth.get('dmarc')}); no aligned pass"
    )


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
        require_verified_sender: bool = True,
        trusted_authserv_id: str = "mx.google.com",
        imap_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
        smtp_factory: Callable[..., smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
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
        self._require_verified_sender = require_verified_sender
        self._trusted_authserv_id = trusted_authserv_id.strip().lower()
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._stopping = asyncio.Event()

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
                    await self._accept_message(ctx, uid, inbound)
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

    async def _accept_message(
        self, ctx: DaemonContext, uid: bytes, inbound: InboundMessage
    ) -> None:
        """Apply the intake gates to one fetched message and enqueue it.

        Two gates, both fail closed by marking the message ``\\Seen`` and
        returning without running a turn:

        1. **ACL** — the ``From`` address must map to a known, allowed
           principal.
        2. **Sender verification** — Gmail's SPF/DKIM/DMARC verdict must
           authenticate the ``From`` address (see
           :func:`_evaluate_sender_auth`). A verified ``From`` is the only
           thing that lets an inbound run a turn *as* the matched principal;
           an unverified message that merely claims a known address is a
           spoof and is dropped.
        """
        principal = ctx.address_book.lookup_by_native(
            self.name, inbound.principal.native_id
        )
        if principal is None or not principal.allowed:
            log.info(
                "ignoring Gmail message from unknown sender %s",
                inbound.principal.native_id,
            )
            await asyncio.to_thread(self._mark_seen, uid)
            return
        if self._require_verified_sender and not inbound.metadata.get(
            "sender_verified"
        ):
            reason = inbound.metadata.get("sender_auth", {}).get(
                "reason", "no auth verdict"
            )
            log.warning(
                "rejecting unverified Gmail message claiming to be %s (%s); "
                "sender identity not authenticated",
                inbound.principal.native_id,
                reason,
            )
            await asyncio.to_thread(self._mark_seen, uid)
            return
        inbound.principal = Principal(
            transport=self.name,
            native_id=inbound.principal.native_id,
            display_name=principal.display_name,
        )
        ctx.address_book.learn(inbound)
        event = GmailEvent(message=inbound)
        divert = getattr(ctx, "divert_to_mid_turn", None)
        if divert is not None and divert(inbound.origin, inbound.text, event):
            await asyncio.to_thread(self._mark_seen, uid)
            return
        await ctx._queue.put(event)
        await asyncio.to_thread(self._mark_seen, uid)

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
        auth = _parse_authentication_results(
            [str(v) for v in msg.get_all("Authentication-Results") or ()],
            self._trusted_authserv_id,
        )
        from_domain = sender_address.rpartition("@")[2]
        verified, reason = _evaluate_sender_auth(auth, from_domain)
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
                "sender_verified": verified,
                "sender_auth": {
                    "verified": verified,
                    "reason": reason,
                    "dkim": auth.get("dkim", "none"),
                    "spf": auth.get("spf", "none"),
                    "dmarc": auth.get("dmarc", "none"),
                    "dkim_domain": auth.get("dkim_domain", ""),
                },
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
