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
logged and the envelope falls through to the local inbox as a
best-effort fallback so the operator still has a trail.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

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


def build_forward_payload(
    *,
    body: str,
    sender: str,
    sender_name: str,
    timestamp_ms: int,
    label: str,
) -> dict[str, Any]:
    """Build a Google A2A ``message/send`` JSON-RPC envelope.

    Schema follows a2a-sdk ``SendMessageRequest`` /
    ``MessageSendParams``: one top-level ``message`` with a ``user``
    role and a single text part. We carry the Signal sender identity
    twice — on the inner ``Message.metadata`` (so the SDK preserves
    it on the Task) and on ``params.metadata`` (so a receiver
    inspecting the raw JSON-RPC envelope sees it without opening the
    Message). Both copies are identical; the receiver can read
    whichever is more convenient.
    """
    metadata = {
        "signal_sender": sender,
        "signal_sender_name": sender_name,
        "signal_timestamp": int(timestamp_ms),
        "dispatch_label": label,
    }
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "message_id": uuid.uuid4().hex,
                "parts": [{"kind": "text", "text": body or ""}],
                "metadata": metadata,
            },
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
    """Baseline POST — 3s connect / 10s read.

    A2A JSON-RPC lives at the root of the target URL (``/``). Caller
    passes the base URL; we normalise trailing-slash handling here so
    both ``http://host:7878`` and ``http://host:7878/`` work.
    """
    timeout = httpx.Timeout(connect=3, read=10, write=10, pool=10)
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
    http_post: HttpPost | None = None,
) -> None:
    """POST a Signal envelope to a peer Alice over A2A.

    Raises any transport-level exception (``httpx.HTTPError``,
    ``httpx.HTTPStatusError``, etc.) so the caller can decide to fall
    back to local handling. Does not retry — one attempt, then the
    caller's fallback policy takes over.
    """
    payload = build_forward_payload(
        body=body,
        sender=sender,
        sender_name=sender_name,
        timestamp_ms=timestamp_ms,
        label=rule.label,
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
