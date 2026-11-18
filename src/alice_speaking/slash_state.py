"""State sentinels for Signal slash commands (issue #624 slice-2).

Slash commands like ``/pause``, ``/quiet``, and ``/interrupt`` need to
affect the running daemon from outside the turn that issued them. Rather
than wire mid-flight cancellation primitives through the kernel, the
slice-2 contract uses small on-disk sentinel files under
``/state/worker/`` (or the configured ``state_dir``) that the daemon's
pre-turn hook consults.

Sentinels are forward-only — writing one never retroactively changes
history. The pause-gate in ``_dispatch.handle_signal`` and the quiet-
hours check in ``signal_outbound`` read these at the start of each
inbound turn.

Each helper accepts an optional ``state_dir`` so tests can isolate on
tmp paths. Production callers pass ``ctx.cfg.state_dir`` from the
daemon context.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from typing import Optional

log = logging.getLogger(__name__)


PAUSE_SENTINEL = "paused"
QUIET_SENTINEL = "quiet-until"
INTERRUPT_SENTINEL = "interrupt-requested"
CANCEL_SENTINEL = "cancel-requested"


def _default_state_dir() -> pathlib.Path:
    """Resolve the default state dir when none is supplied.

    The daemon writes its sentinel files to ``/state/worker`` in the
    container; tests that don't thread ``state_dir`` fall back to
    ``$ALICE_STATE_DIR`` or the container default.
    """
    import os

    override = os.environ.get("ALICE_STATE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path("/state/worker")


def _resolve(state_dir: Optional[pathlib.Path], name: str) -> pathlib.Path:
    base = pathlib.Path(state_dir) if state_dir is not None else _default_state_dir()
    return base / name


# ---------------------------------------------------------------------------
# /pause and /resume


def set_paused(
    *,
    principal_id: str,
    reason: str = "",
    state_dir: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Create the pause sentinel.

    Body is a one-line JSON record so operators can see who requested
    the pause and when without parsing opaque bytes. Overwrites any
    existing sentinel (idempotent per-call).
    """
    path = _resolve(state_dir, PAUSE_SENTINEL)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    body = json.dumps(
        {"paused_at": stamp, "principal_id": principal_id, "reason": reason}
    )
    path.write_text(body + "\n")
    return path


def clear_paused(*, state_dir: Optional[pathlib.Path] = None) -> bool:
    """Delete the pause sentinel. Returns True when a file was present."""
    path = _resolve(state_dir, PAUSE_SENTINEL)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("clear_paused: unlink failed")
        return False
    return True


def is_paused(*, state_dir: Optional[pathlib.Path] = None) -> bool:
    """True iff the pause sentinel exists."""
    return _resolve(state_dir, PAUSE_SENTINEL).is_file()


def read_pause_record(
    *, state_dir: Optional[pathlib.Path] = None
) -> Optional[dict]:
    """Return the parsed pause record, or ``None`` when not paused.

    Malformed files produce a ``{}`` record rather than ``None`` so
    callers can distinguish "paused but metadata missing" from "not
    paused at all".
    """
    path = _resolve(state_dir, PAUSE_SENTINEL)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# /quiet


def set_quiet_until(
    *,
    until: datetime.datetime,
    principal_id: str,
    state_dir: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Record a soft quiet deadline. Non-urgent outbound suppressed until it."""
    path = _resolve(state_dir, QUIET_SENTINEL)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "until": until.astimezone().isoformat(timespec="seconds"),
            "principal_id": principal_id,
        }
    )
    path.write_text(body + "\n")
    return path


def clear_quiet(*, state_dir: Optional[pathlib.Path] = None) -> bool:
    path = _resolve(state_dir, QUIET_SENTINEL)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("clear_quiet: unlink failed")
        return False
    return True


def quiet_until(
    *,
    now: Optional[datetime.datetime] = None,
    state_dir: Optional[pathlib.Path] = None,
) -> Optional[datetime.datetime]:
    """Return the quiet deadline if one is active, else ``None``.

    Expired deadlines are reaped (the sentinel is deleted) so callers
    don't see stale state.
    """
    path = _resolve(state_dir, QUIET_SENTINEL)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None
    value = payload.get("until") if isinstance(payload, dict) else None
    if not isinstance(value, str):
        return None
    try:
        deadline = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    reference = now or datetime.datetime.now().astimezone()
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=reference.tzinfo)
    if deadline <= reference:
        # Expired — reap so the sentinel reflects live state.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return deadline


# ---------------------------------------------------------------------------
# /interrupt and /cancel


def record_interrupt(
    *,
    principal_id: str,
    message: str,
    state_dir: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Append an interrupt record. One JSON line per interrupt.

    The daemon's turn-runner polls this file at tool-call boundaries
    (future work); until that polling exists, this call still leaves
    an auditable trail so Jason can see what was attempted.
    """
    path = _resolve(state_dir, INTERRUPT_SENTINEL)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    record = json.dumps(
        {"ts": stamp, "principal_id": principal_id, "message": message}
    )
    with path.open("a") as fh:
        fh.write(record + "\n")
    return path


def record_cancel(
    *,
    principal_id: str,
    target: str,
    state_dir: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Append a cancel record. ``target`` is ``current`` or an issue
    number / task slug per the slice-2 spec."""
    path = _resolve(state_dir, CANCEL_SENTINEL)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    record = json.dumps(
        {"ts": stamp, "principal_id": principal_id, "target": target}
    )
    with path.open("a") as fh:
        fh.write(record + "\n")
    return path


__all__ = [
    "PAUSE_SENTINEL",
    "QUIET_SENTINEL",
    "INTERRUPT_SENTINEL",
    "CANCEL_SENTINEL",
    "set_paused",
    "clear_paused",
    "is_paused",
    "read_pause_record",
    "set_quiet_until",
    "clear_quiet",
    "quiet_until",
    "record_interrupt",
    "record_cancel",
]
