"""Helpers shared by section renderers.

Keep the surface small: just the staleness-footer builder and the
``asks.jsonl`` reader. Each renderer module composes its own output
from these primitives + section-specific data.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path


def staleness_footer(source: str, *, now: datetime | None = None) -> str:
    """Return the trailing italic line every polled renderer appends.

    Format matches the frontend parser in ``static/index.html``::

        *last_rendered: <iso-local> · source: <source>*

    ``now`` is injectable so tests can pin a deterministic timestamp.
    Default is ``datetime.now().astimezone()`` so the offset reflects
    the container's local TZ.
    """
    ts = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return f"*last_rendered: {ts} · source: {source}*"


def asks_path(status_dir: Path) -> Path:
    """``$STATUS_DIR/asks.jsonl`` — where ``waiting-ask`` writes."""
    return Path(status_dir) / "asks.jsonl"


def read_asks(path: Path) -> list[dict]:
    """Read ``asks.jsonl`` best-effort — one JSON object per line.

    Missing file returns ``[]``. Malformed lines are skipped (same
    forgiveness the threads reader uses). Each record is expected to
    carry at least ``id``, ``text``, ``created``, ``resolved``; the
    ``resolved_at`` field appears on resolved records only.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def new_ask_id() -> str:
    """Short uuid4 prefix — eight hex chars, enough to disambiguate
    the handful of in-flight asks Jason ever has open at once."""
    return uuid.uuid4().hex[:8]


def status_dir_from_env(env: dict[str, str] | None = None) -> Path:
    """Resolve ``$STATUS_DIR`` — same default as ``cli.status_dir``.

    Lets CLIs share the path convention with the server without
    importing the server module (which pulls in HTTP machinery).
    """
    e = dict(os.environ if env is None else env)
    override = e.get("STATUS_DIR")
    if override:
        return Path(override)
    mind = e.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "status"
