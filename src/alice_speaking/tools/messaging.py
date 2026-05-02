"""Messaging tool — explicit outbox for speaking Alice.

Before v3 the daemon auto-captured the final assistant text of every turn
and sent it to Signal. That worked for inbound-replies but silently
dropped surface-triggered responses. The fix (per
design-unified-context-compaction.md) is to make the outbox explicit:
Alice calls ``send_message(recipient, message)`` whenever she wants text
to reach Signal. Returning text alone no longer sends it.

Recipient resolution (Phase 3 — address-book backed):
- ``"self"`` / ``"reply"`` etc. — reply on the same transport the
  inbound came from. The daemon's :meth:`_send_message` honors the
  current turn's reply channel.
- A principal id or display name (``"owner"``, ``"friend_carol"``)
  — looked up in the :class:`AddressBook`. Resolves to that principal's
  preferred channel (signal by default, but transports/phase-3-onwards
  can pick e.g. discord per-principal).
- An E.164 phone number (anything starting with ``+``) — treated as a
  Signal address. No address-book lookup; Alice may message numbers
  not in the book.
- anything else — error.

Attachment path strategy (cross-container)
==========================================

signal-cli runs in the **alice-daemon** container. The MCP tool
(``send_message``) runs in the **alice-worker** container. The two are
separate Docker containers; they share two host directories that matter
for outbox flow:

- worker has ``${HOME}:/host-home:ro`` — read-only view of the host
  home, including ``alice-mind/``. The daemon does NOT mount this, so
  any worker-only path (``/host-home/...``, ``/home/alice/alice-mind/...``)
  is invisible to signal-cli.
- both containers mount ``${HOME}/.local/state/alice:/state:rw``. ``/state``
  is the only filesystem location that is (a) writable from the worker
  and (b) visible at the same path inside the daemon.

So the resolution: when the MCP tool gets an attachment path, it copies
the file into ``/state/outbox/<uuid>-<basename>``, hands that path to
``signal.send(...)``, and best-effort cleans up after the JSON-RPC call
completes. The daemon sees the file at the same ``/state/outbox/...``
path. If the worker's ``/state/outbox`` is missing it gets created on
first use. Cleanup failures are logged but don't fail the send — the
``outbox/`` dir is small and the worker's nightly grooming can sweep
stragglers.

Override knobs (rare):
- ``ALICE_OUTBOX_DIR`` — override the spool dir. Useful for tests and
  for deployments that bind-mount a different shared volume.
- An attachment path that already lives under the spool dir is passed
  through without copying (caller already staged it).
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import uuid
from typing import Any, Awaitable, Callable, Optional, Union

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.config.personae import Personae, placeholder as placeholder_personae

from ..domain.principals import AddressBook
from ..infra.config import Config
from ..infra.signal_rpc import SignalRPC as SignalClient
from ..transports.base import ChannelRef


log = logging.getLogger(__name__)


# Directory shared between alice-worker and alice-daemon containers.
# Both mount ${HOME}/.local/state/alice at /state, so /state/outbox/ is
# writable from worker and readable from daemon at the same path.
DEFAULT_OUTBOX_DIR = pathlib.Path(
    os.environ.get("ALICE_OUTBOX_DIR", "/state/outbox")
)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


# Sentinel that means "reply to whoever just messaged you, on the same
# transport they used." The daemon's _send_message closure recognises this
# and dispatches via the current turn's reply channel — works for Signal,
# CLI, and any future transport without changing this tool.
SELF_RECIPIENT = "__SELF__"

# Aliases for SELF_RECIPIENT that Alice can spell naturally.
_SELF_ALIASES = frozenset({"self", "reply", "user", "sender"})


# Resolved recipient — either the SELF_RECIPIENT sentinel or a concrete
# :class:`ChannelRef` produced by the address book / E.164 parser.
ResolvedRecipient = Union[str, ChannelRef]


def _resolve_recipient(
    raw: str, address_book: AddressBook
) -> Optional[ResolvedRecipient]:
    """Map a name / number / alias string to a recipient the daemon can
    dispatch on.

    Returns:
        - ``SELF_RECIPIENT`` for self-aliases.
        - A signal :class:`ChannelRef` for E.164 phone numbers.
        - The address book's preferred :class:`ChannelRef` for known
          principal ids / display names.
        - ``None`` for anything unresolvable.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if value.lower() in _SELF_ALIASES:
        return SELF_RECIPIENT
    if value.startswith("+"):
        return ChannelRef(transport="signal", address=value, durable=True)
    return address_book.preferred_channel(value)


def _stage_attachments(
    paths: list[str], outbox_dir: pathlib.Path
) -> tuple[list[str], list[str]]:
    """Copy each attachment into the shared outbox so the daemon can see it.

    Returns ``(staged_paths, copies_to_clean)``. ``staged_paths`` is what
    we hand to signal-cli (already in the daemon-visible spool). ``copies_to_clean``
    is the subset we actually created and should remove after send.
    Files already living under the outbox dir are passed through and are
    NOT added to the cleanup list (caller staged them, caller owns them).

    Raises ``FileNotFoundError`` / ``IsADirectoryError`` / ``PermissionError``
    on bad input — the caller turns those into a tool error.
    """
    outbox_dir.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    cleanups: list[str] = []
    for raw in paths:
        src = pathlib.Path(raw).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"attachment not found: {raw}")
        if src.is_dir():
            raise IsADirectoryError(f"attachment is a directory: {raw}")
        try:
            # If the file is already in the outbox, just pass it through.
            src_resolved = src.resolve()
            if src_resolved.is_relative_to(outbox_dir.resolve()):
                staged.append(str(src_resolved))
                continue
        except (OSError, ValueError):
            # resolve() can fail on broken symlinks / odd FS; fall through
            # to the copy path which will surface a clearer error.
            pass
        dest = outbox_dir / f"{uuid.uuid4().hex}-{src.name}"
        shutil.copyfile(src, dest)
        staged.append(str(dest))
        cleanups.append(str(dest))
    return staged, cleanups


def _cleanup(paths: list[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError as exc:
            log.warning("outbox cleanup failed for %s: %s", p, exc)


# Type alias for the coroutine that actually sends a message. The daemon
# passes a closure that updates its internal "did-send" tracking so
# missed_reply detection works. The first argument is either the
# :data:`SELF_RECIPIENT` sentinel (str) or a resolved :class:`ChannelRef`.
# ``attachments`` is None when no media rides along — callers MAY pass an
# empty list and we treat that the same as None.
SendCallable = Callable[
    [ResolvedRecipient, str, Optional[list[str]]], Awaitable[None]
]


async def send_message_from_args(
    args: dict,
    *,
    address_book: AddressBook,
    sender: SendCallable,
    outbox_dir: Optional[pathlib.Path] = None,
) -> dict:
    """Execute a ``send_message`` call from a backend-native tool payload.

    The Claude Agent SDK reaches this through an MCP tool. Pi has no MCP
    client, so its extension bridge calls this same helper from a
    :class:`BlockHandler` when pi emits ``tool_execution_start``.
    """
    raw_recipient = args.get("recipient") or ""
    message = args.get("message") or ""
    if not isinstance(message, str) or not message.strip():
        return _err("message must be a non-empty string")
    resolved = _resolve_recipient(raw_recipient, address_book)
    if resolved is None:
        return _err(
            f"could not resolve recipient {raw_recipient!r}; "
            "use 'self', a known principal id / display name, or an "
            "E.164 number (+...)."
        )

    spool_dir = outbox_dir if outbox_dir is not None else DEFAULT_OUTBOX_DIR
    raw_attachments = args.get("attachments")
    attachment_paths: Optional[list[str]] = None
    if raw_attachments is not None and raw_attachments != []:
        if not isinstance(raw_attachments, list) or not all(
            isinstance(p, str) for p in raw_attachments
        ):
            return _err("attachments must be a list of filesystem path strings")
        try:
            staged, cleanups = _stage_attachments(list(raw_attachments), spool_dir)
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            return _err(f"{type(exc).__name__}: {exc}")
        attachment_paths = staged
    else:
        cleanups = []

    try:
        await sender(resolved, message, attachment_paths)
    except Exception as exc:  # noqa: BLE001
        _cleanup(cleanups)
        return _err(f"{type(exc).__name__}: {exc}")
    _cleanup(cleanups)

    suffix = (
        f" (+{len(attachment_paths)} attachment"
        f"{'s' if len(attachment_paths) != 1 else ''})"
        if attachment_paths
        else ""
    )
    if resolved == SELF_RECIPIENT:
        target_desc = "via current channel"
    else:
        assert isinstance(resolved, ChannelRef)
        target_desc = f"to {resolved.transport}:{resolved.address}"
    return _ok(f"sent {target_desc} ({len(message)} chars){suffix}")


# JSON Schema for the send_message tool. We use the explicit JSON-Schema
# form (rather than the {"name": str} shorthand) because the SDK forces
# every key in the shorthand into ``required`` — there's no way to mark
# ``attachments`` optional without writing the schema out longhand.
_SEND_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipient": {
            "type": "string",
            "description": (
                "Who to send to. Options: "
                "'self' / 'reply' — reply on the same transport the inbound "
                "came from (works for Signal, the local CLI, and future "
                "channels); "
                "a principal id or display name from the address book; "
                "an E.164 phone number (anything starting with '+') — "
                "Signal recipient by number."
            ),
        },
        "message": {
            "type": "string",
            "description": "The text body as you want it delivered.",
        },
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional list of filesystem paths to send as Signal "
                "attachments (images, PDFs, etc.). Paths are resolved "
                "from the worker's filesystem; the tool copies each "
                "into a shared spool dir so the daemon can see them. "
                "Omit or pass [] if you have no attachments."
            ),
        },
    },
    "required": ["recipient", "message"],
}


def build(
    cfg: Config,
    *,
    address_book: AddressBook,
    signal: Optional[SignalClient] = None,
    sender: Optional[SendCallable] = None,
    outbox_dir: Optional[pathlib.Path] = None,
    personae: Optional[Personae] = None,
) -> list[SdkMcpTool[Any]]:
    """Build the messaging tool list.

    One of ``sender`` or ``signal`` must be provided. ``sender`` wins when
    both are present — this lets the daemon wrap transport.send with
    bookkeeping (did-send tracking, event emission, quiet-hours routing).

    ``address_book`` is used for principal-name → channel resolution.
    ``outbox_dir`` overrides the spool location used to stage attachments.
    Defaults to ``/state/outbox`` (shared between worker and daemon
    containers).
    """
    if sender is None and signal is None:
        raise ValueError("messaging.build requires either `sender` or `signal`")
    p = personae or placeholder_personae()
    user_name = p.user.name

    actual_sender: SendCallable
    if sender is not None:
        actual_sender = sender
    else:
        assert signal is not None  # narrowing for type checker
        _signal = signal

        async def _direct(
            recipient: ResolvedRecipient,
            message: str,
            attachments: Optional[list[str]] = None,
        ) -> None:
            # Direct (no-daemon) path is signal-only: the bare SignalClient
            # has no concept of CLI / Discord / SELF_RECIPIENT. Reject
            # anything we can't translate to a Signal address.
            if isinstance(recipient, str):
                raise RuntimeError(
                    "direct sender cannot route SELF_RECIPIENT — wrap with "
                    "the daemon's _send_message closure to use 'self'"
                )
            if recipient.transport != "signal":
                raise RuntimeError(
                    f"direct sender is signal-only; got transport "
                    f"{recipient.transport!r}"
                )
            await _signal.send(recipient.address, message, attachments=attachments)

        actual_sender = _direct

    @tool(
        name="send_message",
        description=(
            "Send a message. This is how you reply to the user — returning "
            "text alone does NOT send. Recipient: 'self' or 'reply' replies "
            "on the same transport the inbound came from (Signal, the local "
            "CLI, etc.); a principal id from the address book or an E.164 "
            "phone number sends via Signal specifically. `attachments` is "
            "an optional list of filesystem paths (images, PDFs) — Signal-"
            "only today. Use this for both inbound replies AND surface-"
            "triggered voicings."
        ),
        input_schema=_SEND_MESSAGE_SCHEMA,
    )
    async def send_message(args: dict) -> dict:
        return await send_message_from_args(
            args,
            address_book=address_book,
            sender=actual_sender,
            outbox_dir=outbox_dir,
        )

    return [send_message]


__all__ = [
    "build",
    "send_message_from_args",
    "SELF_RECIPIENT",
    "_resolve_recipient",
    "_stage_attachments",
]
