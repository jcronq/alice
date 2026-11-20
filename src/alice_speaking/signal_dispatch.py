"""Signal sender-based dispatch — forward specific inbound Signal senders
to a peer Alice over A2A JSON-RPC instead of handling them locally.

Loaded from ``alice.config.json`` under ``signal.dispatch.forwards``:

    "signal": {
      "dispatch": {
        "forwards": [
          {
            "senders": ["+14357091396"],
            "target": "http://10.200.0.2:7878",
            "label": "grandparents-home"
          }
        ]
      }
    }

Each forward entry maps a set of Signal E.164 sender numbers to a
single A2A JSON-RPC endpoint. :meth:`SignalDispatchConfig.lookup`
returns the matching :class:`ForwardRule` or ``None`` for an
unknown sender.

:func:`forward_to_a2a` builds and POSTs the A2A ``message/send``
JSON-RPC envelope. The payload carries the original Signal sender
number, display name, millisecond timestamp, and dispatch label in
both the inner ``Message.metadata`` and the top-level
``params.metadata`` so the receiving A2A server can plumb the
sender identity through to its reply-routing logic.

The transport fork lives in ``transports/signal.py::_produce`` —
forwarded senders never land on the local inbox. A failed POST is
logged, the original sender receives a short "unreachable" notice
over the shared signal-cli, and the envelope is dropped. The local
inbox is never a fallback for forwarded senders: tenant isolation
requires that cross-household messages never cross over, even on
error.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import pathlib
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForwardRule:
    """One sender-set → one A2A target. Immutable; built from
    ``alice.config.json`` via :func:`from_mapping`."""

    senders: frozenset[str]
    target: str
    label: str


@dataclass(frozen=True)
class SignalDispatchConfig:
    """Parsed ``signal.dispatch`` block. Empty tuple = feature off."""

    forwards: tuple[ForwardRule, ...] = ()

    def lookup(self, sender: str) -> Optional[ForwardRule]:
        """Return the first :class:`ForwardRule` containing ``sender``,
        or ``None``. Rules are scanned in declared order; operators are
        expected not to duplicate a sender across rules, but a dup
        silently picks the first entry rather than erroring."""
        for rule in self.forwards:
            if sender in rule.senders:
                return rule
        return None


class SignalDispatchConfigError(ValueError):
    """Raised on malformed ``signal.dispatch`` shape in alice.config.json."""


def _normalize_senders(raw: Iterable[Any], field_path: str) -> frozenset[str]:
    out: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            raise SignalDispatchConfigError(
                f"{field_path} entries must be strings "
                f"(got {type(value).__name__})"
            )
        stripped = value.strip()
        if stripped:
            out.add(stripped)
    return frozenset(out)


def from_mapping(data: Mapping[str, Any] | None) -> SignalDispatchConfig:
    """Parse the ``signal.dispatch`` sub-mapping from
    ``alice.config.json``. Missing / empty → empty config (feature
    off, local behaviour preserved)."""
    if not data:
        return SignalDispatchConfig()
    raw_forwards = data.get("forwards")
    if raw_forwards is None:
        return SignalDispatchConfig()
    if not isinstance(raw_forwards, list):
        raise SignalDispatchConfigError(
            f"signal.dispatch.forwards must be a list "
            f"(got {type(raw_forwards).__name__})"
        )
    rules: list[ForwardRule] = []
    for i, entry in enumerate(raw_forwards):
        if not isinstance(entry, Mapping):
            raise SignalDispatchConfigError(
                f"signal.dispatch.forwards[{i}] must be a mapping "
                f"(got {type(entry).__name__})"
            )
        senders_raw = entry.get("senders")
        if not isinstance(senders_raw, list) or not senders_raw:
            raise SignalDispatchConfigError(
                f"signal.dispatch.forwards[{i}].senders must be a non-empty list"
            )
        senders = _normalize_senders(
            senders_raw, f"signal.dispatch.forwards[{i}].senders"
        )
        if not senders:
            raise SignalDispatchConfigError(
                f"signal.dispatch.forwards[{i}].senders is empty after "
                f"stripping whitespace"
            )
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            raise SignalDispatchConfigError(
                f"signal.dispatch.forwards[{i}].target must be a non-empty string"
            )
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SignalDispatchConfigError(
                f"signal.dispatch.forwards[{i}].label must be a non-empty string"
            )
        rules.append(
            ForwardRule(
                senders=senders,
                target=target.strip(),
                label=label.strip(),
            )
        )
    return SignalDispatchConfig(forwards=tuple(rules))


def _attachment_to_file_part(attachment: Any) -> dict[str, Any]:
    """Encode one Signal :class:`Attachment` as an A2A ``FilePart``.

    Accepts anything duck-typed with ``path`` (a filesystem path),
    ``content_type`` (MIME string, optional), and ``filename``
    (optional sender-supplied name). Reads the file bytes off disk
    and base64-encodes them inline — simplest cross-host transport,
    no shared filesystem assumption with the peer.

    The shape mirrors the v0.3 ``FilePart`` / ``FileWithBytes`` the
    a2a-sdk accepts via its JSON-RPC ``message/send`` compat adapter:
    ``{"kind": "file", "file": {"bytes": "<b64>", "mime_type": ...,
    "name": ...}}``.
    """
    path = pathlib.Path(getattr(attachment, "path"))
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    mime = getattr(attachment, "content_type", None) or ""
    if not mime:
        guessed, _ = mimetypes.guess_type(path.name)
        mime = guessed or "application/octet-stream"
    name = getattr(attachment, "filename", None) or path.name
    file_payload: dict[str, Any] = {
        "bytes": encoded,
        "mime_type": mime,
    }
    if name:
        file_payload["name"] = name
    return {
        "kind": "file",
        "file": file_payload,
    }


def build_forward_payload(
    *,
    body: str,
    sender: str,
    sender_name: str,
    timestamp_ms: int,
    label: str,
    attachments: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Build a Google A2A ``message/send`` JSON-RPC envelope.

    Schema follows a2a-sdk ``SendMessageRequest`` /
    ``MessageSendParams``: one top-level ``message`` with a ``user``
    role and one text part plus one ``FilePart`` per attachment. We
    carry the Signal sender identity twice — on the inner
    ``Message.metadata`` (so the SDK preserves it on the Task) and
    on ``params.metadata`` (so a receiver inspecting the raw JSON-RPC
    envelope sees it without opening the Message). Both copies are
    identical; the receiver can read whichever is more convenient.

    Fire-and-forget: ``configuration.blocking=False`` tells the peer
    to return as soon as it accepts the task. A2A's v0.3 JSON-RPC
    adapter (the ``message/send`` method) uses the ``blocking``
    field name; proto-side this maps to
    ``SendMessageConfiguration.return_immediately=True``. Without
    this flag the peer blocks until its kernel turn reaches a
    terminal state (typically 15-60s), the forwarder times out, and
    the failure path (correctly) drops the envelope — but the sender
    sees that as a spurious "unreachable" notice even though the peer
    was actually processing fine. `blocking=False` keeps the forward
    path accurate.
    """
    metadata = {
        "signal_sender": sender,
        "signal_sender_name": sender_name,
        "signal_timestamp": int(timestamp_ms),
        "dispatch_label": label,
    }
    parts: list[dict[str, Any]] = [
        {"kind": "text", "text": body or ""},
    ]
    for att in attachments or ():
        try:
            parts.append(_attachment_to_file_part(att))
        except Exception as exc:  # noqa: BLE001
            # Don't let one unreadable attachment sink the whole
            # forward — keep the other parts and log so operators
            # see what dropped. Silent regression was Finding 2;
            # a visible drop is still better than hanging the turn.
            log.warning(
                "signal forward: skipping unreadable attachment "
                "(path=%s): %s",
                getattr(att, "path", "<unknown>"),
                exc,
            )
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "message_id": uuid.uuid4().hex,
                "parts": parts,
                "metadata": metadata,
            },
            "configuration": {"blocking": False},
            "metadata": metadata,
        },
    }


# ---------------------------------------------------------------------------
# HTTP delivery
#
# The default poster uses httpx — same client used by signal_rpc. The
# hook is injectable so unit tests can replace it with a fake that
# records calls / raises on command, without needing httpx's MockTransport.


HttpPost = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def _default_http_post(url: str, payload: dict[str, Any]) -> Any:
    """Baseline POST — 3s connect / 5s read.

    A2A JSON-RPC lives at the root of the target URL (``/``). Caller
    passes the base URL; we normalise trailing-slash handling here so
    both ``http://host:7878`` and ``http://host:7878/`` work.

    The 5s read budget is comfortable for the fire-and-forget
    ``blocking=False`` path (peer returns in milliseconds once it
    accepts the task). A longer window would let a mis-configured
    blocking peer stall the Signal consumer.
    """
    timeout = httpx.Timeout(connect=3, read=5, write=5, pool=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url.rstrip("/") + "/",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp


async def forward_to_a2a(
    *,
    rule: ForwardRule,
    body: str,
    sender: str,
    sender_name: str,
    timestamp_ms: int,
    attachments: Sequence[Any] | None = None,
    http_post: HttpPost | None = None,
) -> None:
    """POST a Signal envelope to a peer Alice over A2A.

    Raises any transport-level exception (``httpx.HTTPError``,
    ``httpx.HTTPStatusError``, etc.) so the caller can log it and send
    the original sender an "unreachable" notice. Does not retry — one
    attempt, then the caller decides how to signal the failure back.
    The caller MUST NOT fall back to local handling for forwarded
    senders — tenant isolation requires that cross-household messages
    stay cross-household.

    ``attachments`` — iterable of Signal :class:`Attachment` objects
    (or anything duck-typed with ``path`` / ``content_type`` /
    ``filename``). Each becomes a ``FilePart`` on the A2A message
    alongside the text body. ``None`` or an empty iterable sends
    text-only.
    """
    payload = build_forward_payload(
        body=body,
        sender=sender,
        sender_name=sender_name,
        timestamp_ms=timestamp_ms,
        label=rule.label,
        attachments=attachments,
    )
    poster = http_post or _default_http_post
    await poster(rule.target, payload)


__all__ = [
    "ForwardRule",
    "SignalDispatchConfig",
    "SignalDispatchConfigError",
    "from_mapping",
    "build_forward_payload",
    "forward_to_a2a",
    "HttpPost",
]
