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
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import SdkMcpTool, tool

from ..config import Config
from ..signal_client import SignalClient


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
# missed_reply detection works. ``attachments`` is None when no media
# rides along — callers MAY pass an empty list and we treat that the
# same as None.
SendCallable = Callable[[str, str, Optional[list[str]]], Awaitable[None]]


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
                "'jason', 'katie', or an E.164 number (e.g. '+15555550100')."
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
    signal: Optional[SignalClient] = None,
    sender: Optional[SendCallable] = None,
    outbox_dir: Optional[pathlib.Path] = None,
) -> list[SdkMcpTool[Any]]:
    """Build the messaging tool list.

    One of ``sender`` or ``signal`` must be provided. ``sender`` wins when
    both are present — this lets the daemon wrap SignalClient.send with
    bookkeeping (did-send tracking, event emission, quiet-hours routing).

    ``outbox_dir`` overrides the spool location used to stage attachments.
    Defaults to ``/state/outbox`` (shared between worker and daemon
    containers).
    """
    if sender is None and signal is None:
        raise ValueError("messaging.build requires either `sender` or `signal`")

    actual_sender: SendCallable
    if sender is not None:
        actual_sender = sender
    else:
        assert signal is not None  # narrowing for type checker
        _signal = signal

        async def _direct(
            recipient: str,
            message: str,
            attachments: Optional[list[str]] = None,
        ) -> None:
            await _signal.send(recipient, message, attachments=attachments)

        actual_sender = _direct

    spool_dir = outbox_dir if outbox_dir is not None else DEFAULT_OUTBOX_DIR

    @tool(
        name="send_message",
        description=(
            "Send a Signal message. This is how you reply to the user — "
            "returning text alone does NOT send. Recipient can be "
            "'jason', 'katie', or an E.164 number (e.g. '+15555550100'). "
            "Message is the text body as you want it delivered. "
            "`attachments` is an optional list of filesystem paths "
            "(images, PDFs, etc.) that ride along with the message. "
            "Use this for both inbound replies AND surface-triggered "
            "voicings."
        ),
        input_schema=_SEND_MESSAGE_SCHEMA,
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

        # Validate + normalize attachments. Empty list is treated as None
        # (no attachment, no spool work).
        raw_attachments = args.get("attachments")
        attachment_paths: Optional[list[str]] = None
        if raw_attachments is not None and raw_attachments != []:
            if not isinstance(raw_attachments, list) or not all(
                isinstance(p, str) for p in raw_attachments
            ):
                return _err(
                    "attachments must be a list of filesystem path strings"
                )
            try:
                staged, cleanups = _stage_attachments(
                    list(raw_attachments), spool_dir
                )
            except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
                return _err(f"{type(exc).__name__}: {exc}")
            attachment_paths = staged
        else:
            cleanups = []

        try:
            await actual_sender(number, message, attachment_paths)
        except Exception as exc:  # noqa: BLE001
            _cleanup(cleanups)
            return _err(f"{type(exc).__name__}: {exc}")
        # Cleanup happens after the send returns. signal-cli reads the
        # file synchronously during the JSON-RPC call, so by the time
        # we get control back the upload is done.
        _cleanup(cleanups)

        suffix = (
            f" (+{len(attachment_paths)} attachment"
            f"{'s' if len(attachment_paths) != 1 else ''})"
            if attachment_paths
            else ""
        )
        return _ok(f"sent to {number} ({len(message)} chars){suffix}")

    return [send_message]


__all__ = ["build", "_resolve_recipient", "_stage_attachments"]
