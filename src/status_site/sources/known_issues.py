"""Known issues I'm on — pure hand-edit zone.

No gh queries, no polling, no staleness footer. The server reads
``inner/status/known-issues.md`` verbatim and splices it into
``index.md``. When the file is missing we render a one-line
placeholder so the section doesn't vanish, but we still don't emit a
staleness line (this isn't live data that can go stale).
"""

from __future__ import annotations

from pathlib import Path


def render(hand_zone_path: Path) -> str:
    """Return the hand-zone body stripped, or a placeholder when
    missing / empty."""
    try:
        body = hand_zone_path.read_text(errors="replace").strip()
    except OSError:
        body = ""
    if not body:
        return "*(no known issues)*"
    return body
