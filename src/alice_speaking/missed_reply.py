"""Missed-reply guardrail for Signal-inbound turns.

Jason's directive (2026-11-24): "There should always be a response."

When a Signal-inbound turn finishes without Alice ever calling
``send_message``, the dispatcher used to emit a bare
``missed_reply`` event and send a generic fallback apology. The
original fallback shipped for issue #215; this module extends it
into a full guardrail that also:

- Picks a principal-aware fallback message (grandparents get a
  gentler phrasing than Jason/Katie — they don't know what "the
  backend" is).
- Emits a ``fallback_reply_sent`` structured event so the audit
  trail shows the guardrail fired alongside the original
  ``missed_reply``.
- Writes a ``type: fault-detected`` surface under
  ``inner/surface/`` so Speaking picks it up on the next wake
  and investigates the backend that failed to reply.
- When this process is grand-alice (``ALICE_INSTANCE_NAME``
  env var or a hostname match), fires a best-effort A2A notify
  at main-alice so the operator at the other end of the mesh
  sees the fault even though the surface landed on grand-alice.

The surface-triggered and CLI turn paths do NOT invoke this —
their silent completion is legitimate (surface handlers often
have nothing to say back, CLI clients may already have
disconnected). Signal is the only channel where silence is a
guaranteed UX failure (a stale "typing…" indicator).

Design: codex backend on grand-alice occasionally skips the
``send_message`` MCP tool entirely and closes the SDK stream with
only reasoning text. The real fix is in the backend; this module
is the guardrail that keeps the user from staring at nothing while
that fix ships separately.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
import socket
from typing import TYPE_CHECKING, Optional

from .transports import ChannelRef, OutboundMessage


if TYPE_CHECKING:
    from .transports import DaemonContext


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fallback message text — module constants so they can be tuned without
# chasing inline strings through _dispatch.py.

#: Shown to Jason's parents (Carolyn, Douglas) when a Signal turn ends
#: silent. Deliberately gentle, no mention of "backend" or "operator" —
#: they shouldn't need to understand the internal terminology.
FALLBACK_REPLY_GRANDPARENT = (
    "Sorry — I was getting your message but something went wrong on my "
    "end. Jason is looking into it."
)

#: Shown to Jason, Katie, or anyone else. Explicit about the mode of
#: failure so the operator reading it (usually Jason) knows to check
#: backend logs rather than retrying the question.
FALLBACK_REPLY_GENERIC = (
    "Something went wrong on my end — no reply got out of the backend. "
    "Flagging this to the operator."
)

#: Principal ids that get the grandparent-flavored fallback. Matched
#: case-insensitively against the address_book's principal id for the
#: Signal sender. Keep in sync with ``principals.yaml``.
GRANDPARENT_PRINCIPAL_IDS: frozenset[str] = frozenset({"carolyn", "douglas"})


# ---------------------------------------------------------------------------
# Instance identity — "is this main-alice or grand-alice?"
#
# No existing env var was reserved for this (ALICE_INSTANCE_NAME is new
# as of this patch). Fall back to the hostname so a grand-alice deployment
# without the env var still self-identifies correctly (the container
# hostname is set by the sandbox compose file and is stable per-instance).

#: Hostname substrings that identify a grand-alice deployment when the
#: env var is unset. The grandparents-home box is behind WG at 10.200.0.2
#: and runs under a container whose hostname contains "grand".
_GRAND_ALICE_HOSTNAME_MARKERS: tuple[str, ...] = ("grand", "grand-alice")


def resolve_instance_name() -> str:
    """Return a short identifier for this Alice instance.

    Order of resolution:

    1. ``ALICE_INSTANCE_NAME`` env var, if set and non-empty.
    2. Hostname as reported by :func:`socket.gethostname`. If the
       hostname contains one of :data:`_GRAND_ALICE_HOSTNAME_MARKERS`
       we normalize to ``grand-alice``; otherwise it's ``main-alice``.

    The caller uses this for logging and for routing the A2A notify
    on grand-alice. Keep the return small — ``main-alice`` /
    ``grand-alice`` is the taxonomy we actually care about.
    """
    env = (os.environ.get("ALICE_INSTANCE_NAME") or "").strip()
    if env:
        return env
    host = socket.gethostname().lower()
    for marker in _GRAND_ALICE_HOSTNAME_MARKERS:
        if marker in host:
            return "grand-alice"
    return "main-alice"


def is_grand_alice(instance_name: Optional[str] = None) -> bool:
    """Return True when this process is the grand-alice deployment.

    Pass ``instance_name`` to test the policy against a specific value;
    omit to read the live environment via :func:`resolve_instance_name`.
    """
    name = (instance_name if instance_name is not None else resolve_instance_name())
    return "grand" in name.lower()


# ---------------------------------------------------------------------------
# Public API — pick message + write surface + optional A2A notify.
# One public entry point so _dispatch.py stays thin.


def pick_fallback_text(principal_id: Optional[str]) -> str:
    """Return the fallback body text for a given principal id.

    ``principal_id`` is the ``address_book`` record id (``carolyn``,
    ``jcronq``, …) or ``None`` when the sender wasn't matched to a
    known principal. Unknown senders get the generic fallback — same
    as Jason or Katie would — because we don't know whether they're
    friends-and-family-grade or someone technical.
    """
    if principal_id is None:
        return FALLBACK_REPLY_GENERIC
    if principal_id.lower() in GRANDPARENT_PRINCIPAL_IDS:
        return FALLBACK_REPLY_GRANDPARENT
    return FALLBACK_REPLY_GENERIC


# ---------------------------------------------------------------------------
# Surface write
#
# Shape follows watchers/ci_remediation.py's SPAWN_SURFACE_TEMPLATE —
# ``priority: action`` so the surface watcher picks it up for Speaking
# on the next wake, ``type: fault-detected`` so the taxonomy is grep-
# friendly, and a dated created field. Body is prose Alice can read
# cold to understand what happened.


_SURFACE_TEMPLATE = """\
---
priority: action
type: fault-detected
source: missed-reply-fallback
created: {date}
instance: {instance}
principal_id: {principal_id}
principal_display: {principal_display}
original_sender: {original_sender}
turn_id: {turn_id}
occurred_at: {occurred_at}
---

# Missed reply guardrail fired on {instance}

A Signal-inbound turn on **{instance}** ended without the backend
ever calling `send_message`. The guardrail sent a fallback apology
so {principal_display} didn't see dead air, but the underlying
backend fault is real.

- **Who sent the inbound**: {principal_display} ({original_sender})
- **Resolved principal**: `{principal_id}`
- **Turn id**: `{turn_id}`
- **Occurred at**: {occurred_at}

Investigate the backend driving this instance — on main-alice that's
the Claude Agent SDK, on grand-alice that's the codex backend. Grep
`speaking.log` for `turn_id={turn_id}` to see the kernel-level trace;
the `missed_reply` event is already in the log alongside the
`fallback_reply_sent` event this surface accompanies.

If this is firing repeatedly from the same instance, the backend's
``send_message`` tool-use path is probably broken rather than the
model skipping it stochastically.
"""


def _slug(text: str, *, max_len: int = 40) -> str:
    """Normalize ``text`` for a filesystem-safe surface filename stem."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (out or "unknown")[:max_len]


def write_fault_surface(
    *,
    mind_dir: Optional[pathlib.Path],
    instance: str,
    principal_id: Optional[str],
    principal_display: str,
    original_sender: str,
    turn_id: str,
) -> Optional[pathlib.Path]:
    """Drop a ``type: fault-detected`` surface into ``inner/surface/``.

    Best-effort: any filesystem error is logged and swallowed so the
    guardrail itself can't raise into the Signal turn loop. Returns the
    written path on success, ``None`` on skip / failure — callers use
    that for the structured event and the A2A notify body.
    """
    if mind_dir is None:
        log.warning("missed-reply surface skipped: mind_dir is None")
        return None
    try:
        surface_dir = pathlib.Path(mind_dir) / "inner" / "surface"
        surface_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
        label_stub = _slug(principal_id or principal_display or "unknown")
        fname = f"{stamp}-missed-reply-{label_stub}-{turn_id}.md"
        path = surface_dir / fname
        body = _SURFACE_TEMPLATE.format(
            date=now.date().isoformat(),
            instance=instance,
            principal_id=principal_id or "(unknown)",
            principal_display=principal_display or "(unknown)",
            original_sender=original_sender or "(unknown)",
            turn_id=turn_id,
            occurred_at=now.isoformat(timespec="seconds"),
        )
        path.write_text(body)
        return path
    except Exception as exc:  # noqa: BLE001 — never raise into the turn loop
        log.warning("missed-reply surface write failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# A2A notify (grand-alice only)
#
# When grand-alice fires the guardrail the local surface is the durable
# record; the A2A notify is a courtesy ping to main-alice so Jason
# sees the fault without having to SSH into the grandparents-home box.
# Address of main-alice comes from the address book (same place
# principals.yaml stores the symmetric peer URL). If anything's
# unconfigured — no transport, no main-alice principal, no a2a
# channel — we log once and move on. The local surface still exists.


_A2A_NOTIFY_TEMPLATE = (
    "[guardrail] grand-alice missed_reply fallback fired for "
    "{principal_display} ({original_sender}); turn_id={turn_id}; "
    "surface={surface_path_short}. Investigate the codex backend — "
    "see speaking.log on grand-alice."
)


async def notify_main_alice_via_a2a(
    ctx: "DaemonContext",
    *,
    principal_display: str,
    original_sender: str,
    turn_id: str,
    surface_path: Optional[pathlib.Path],
) -> Optional[str]:
    """Fire a best-effort A2A notify at main-alice from grand-alice.

    Returns the error string on failure (for the structured event /
    operator trail) or ``None`` on success. Never raises.
    """
    transport = getattr(ctx, "a2a_transport", None)
    if transport is None:
        return "a2a_transport_not_configured"
    try:
        address_book = ctx.address_book
    except Exception:  # noqa: BLE001
        return "address_book_unavailable"
    record = address_book.lookup_by_id("main-alice")
    if record is None:
        return "main_alice_principal_not_in_address_book"
    peer_channel: Optional[ChannelRef] = None
    for ch in record.channels:
        if ch.transport == "a2a":
            peer_channel = ChannelRef(
                transport="a2a", address=ch.address, durable=True
            )
            break
    if peer_channel is None:
        return "main_alice_has_no_a2a_channel"
    surface_path_short = (
        str(surface_path.name) if surface_path is not None else "(no-surface)"
    )
    body = _A2A_NOTIFY_TEMPLATE.format(
        principal_display=principal_display or "(unknown)",
        original_sender=original_sender or "(unknown)",
        turn_id=turn_id,
        surface_path_short=surface_path_short,
    )
    try:
        await transport.send(
            OutboundMessage(destination=peer_channel, text=body)
        )
        return None
    except Exception as exc:  # noqa: BLE001 — courtesy ping, never fatal
        return f"a2a_send_failed: {type(exc).__name__}: {exc}"


__all__ = [
    "FALLBACK_REPLY_GRANDPARENT",
    "FALLBACK_REPLY_GENERIC",
    "GRANDPARENT_PRINCIPAL_IDS",
    "is_grand_alice",
    "notify_main_alice_via_a2a",
    "pick_fallback_text",
    "resolve_instance_name",
    "write_fault_surface",
]
