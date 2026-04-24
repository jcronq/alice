"""Messaging tool — explicit outbox for speaking Alice.

Before v3 the daemon auto-captured the final assistant text of every turn
and sent it to Signal. That worked for inbound-replies but silently
dropped surface-triggered responses. The fix (per
design-unified-context-compaction.md) is to make the outbox explicit:
Alice calls ``send_message(recipient, message)`` whenever she wants text
to reach Signal. Returning text alone no longer sends it.

Recipient resolution:
- ``"jason"`` / ``"katie"`` — resolved against cfg.allowed_senders by
  case-insensitive name match.
- anything starting with ``+`` — treated as an E.164 phone number.
- anything else — error.

The tool drives Signal through the same JSON-RPC daemon the rest of
alice-speaking uses (SignalClient). It honors quiet hours for
inbound-reply text by routing through the daemon's send-or-queue if
available; emergency / proactive callers should pass their own handler.
For the baseline speaking-Alice case, a direct ``signal.send`` is fine —
quiet-hours enforcement for proactive Alice-initiated voice is a
separate design decision that belongs in the daemon, not the tool.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import SdkMcpTool, tool

from ..config import Config
from ..signal_client import SignalClient


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _resolve_recipient(raw: str, cfg: Config) -> Optional[str]:
    """Map a name / number string to an E.164 phone number.

    Returns None if the recipient can't be resolved.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if value.startswith("+"):
        # Already in E.164 form — trust it.
        return value
    lowered = value.lower()
    for number, sender in cfg.allowed_senders.items():
        if sender.name.lower() == lowered:
            return number
    return None


# Type alias for the coroutine that actually sends a message. The daemon
# passes a closure that updates its internal "did-send" tracking so
# missed_reply detection works.
SendCallable = Callable[[str, str], Awaitable[None]]


def build(
    cfg: Config,
    *,
    signal: Optional[SignalClient] = None,
    sender: Optional[SendCallable] = None,
) -> list[SdkMcpTool[Any]]:
    """Build the messaging tool list.

    One of ``sender`` or ``signal`` must be provided. ``sender`` wins when
    both are present — this lets the daemon wrap SignalClient.send with
    bookkeeping (did-send tracking, event emission, quiet-hours routing).
    """
    if sender is None and signal is None:
        raise ValueError("messaging.build requires either `sender` or `signal`")

    actual_sender: SendCallable
    if sender is not None:
        actual_sender = sender
    else:
        assert signal is not None  # narrowing for type checker
        _signal = signal

        async def _direct(recipient: str, message: str) -> None:
            await _signal.send(recipient, message)

        actual_sender = _direct

    @tool(
        name="send_message",
        description=(
            "Send a Signal message. This is how you reply to the user — "
            "returning text alone does NOT send. Recipient can be "
            "'jason', 'katie', or an E.164 number (e.g. '+15555550100'). "
            "Message is the text body as you want it delivered. Use this "
            "for both inbound replies AND surface-triggered voicings."
        ),
        input_schema={"recipient": str, "message": str},
    )
    async def send_message(args: dict) -> dict:
        raw_recipient = args.get("recipient") or ""
        message = args.get("message") or ""
        if not isinstance(message, str) or not message.strip():
            return _err("message must be a non-empty string")
        number = _resolve_recipient(raw_recipient, cfg)
        if number is None:
            return _err(
                f"could not resolve recipient {raw_recipient!r}; "
                "use 'jason', 'katie', or an E.164 number (+...)."
            )
        try:
            await actual_sender(number, message)
        except Exception as exc:  # noqa: BLE001
            return _err(f"{type(exc).__name__}: {exc}")
        return _ok(f"sent to {number} ({len(message)} chars)")

    return [send_message]


__all__ = ["build", "_resolve_recipient"]
