"""Persistent session_id state for cross-restart context recovery.

The Claude Agent SDK keeps each session's transcript in a local JSONL
file at ``work_dir/.claude/sessions/<session_id>.jsonl``. Passing
``resume=session_id`` on a subsequent call replays that file and keeps
the context warm. That means if we persist the daemon's latest
session_id to disk, a process restart can pick up exactly where it left
off — no bootstrap prompt needed.

This module owns the read/write/clear primitives for
``inner/state/session.json`` plus a preflight helper that checks whether
the SDK's local session JSONL still exists before we try to resume.
Everything here is pure I/O; the daemon composes these calls at its
lifecycle boundaries.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


@dataclass
class PersistedSession:
    session_id: str
    saved_at: str


def write(path: pathlib.Path, session_id: str) -> None:
    """Atomically write ``{session_id, saved_at}`` to ``path``.

    Uses a ``.tmp`` sibling + ``os.replace`` to avoid torn writes — a
    half-written session.json is worse than a missing one, since the
    daemon would try to parse it on restart and log a false failure.
    """
    payload = {
        "session_id": session_id,
        "saved_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload) + "\n")
    tmp.replace(path)


def read(path: pathlib.Path) -> Optional[PersistedSession]:
    """Read a previously-persisted session. Return None on any failure —
    the caller treats that as "start cold". Errors are logged but not
    raised because a corrupt session.json should never block startup."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        log.warning("session.json at %s has no session_id; ignoring", path)
        return None
    saved_at = data.get("saved_at") or ""
    return PersistedSession(session_id=session_id, saved_at=saved_at)


def clear(path: pathlib.Path) -> None:
    """Delete the persisted session file. No-op if it doesn't exist."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("could not clear %s: %s", path, exc)


def sdk_session_jsonl_path(work_dir: pathlib.Path, session_id: str) -> pathlib.Path:
    """Return the SDK's local session-log path for ``session_id``.

    The Claude Agent SDK stores session transcripts at
    ``<work_dir>/.claude/sessions/<session_id>.jsonl``. We consult this
    path for the preflight existence check before attempting ``resume=``
    — if the JSONL is gone (for example, after a ``find -delete`` purge),
    the resume would fail on the network call; detecting locally is both
    cheaper and quieter.
    """
    return work_dir / ".claude" / "sessions" / f"{session_id}.jsonl"


def sdk_session_exists(work_dir: pathlib.Path, session_id: str) -> bool:
    return sdk_session_jsonl_path(work_dir, session_id).is_file()


__all__ = [
    "PersistedSession",
    "write",
    "read",
    "clear",
    "sdk_session_jsonl_path",
    "sdk_session_exists",
]
