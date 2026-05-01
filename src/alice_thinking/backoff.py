"""Sleep-mode wake-cadence backoff policy.

Pure policy module — given the previous interval, the just-finished
wake's mode, and whether that wake did meaningful work, return the
next wake-to-wake interval. Keeping cap/ladder constants here lets
``wake.py`` stay per-turn deterministic and lets the policy be
unit-tested in isolation.

Behavior (from
``cortex-memory/research/2026-05-01-sleep-mode-exponential-backoff-design.md``):

- Active mode: always 5 min — backoff applies to sleep only.
- Sleep mode + did_work=True: hard reset to 5 min.
- Sleep mode + did_work=False: double the previous interval, capped
  at 40 min. Ladder: 5 → 10 → 20 → 40.

Notes:

- ``did_work`` is the design's "meaningful work" signal:
  Stage B inbox drained, Stage C ``did_work: true``, or Stage D
  synthesis written. ``wake.py`` derives it from the wake-file
  frontmatter the agent writes during the wake.
- Inbox-arrival interrupts (new file in ``inner/notes/``) are *not*
  this module's concern — the s6 supervisor handles them by waiting
  on ``inotifywait`` instead of plain ``sleep``.
"""

from __future__ import annotations

import os
import pathlib

from .vault_state import _is_truthy, _parse_frontmatter

BASE_INTERVAL_SECONDS = 5 * 60  # 300 — bottom of the ladder
MAX_INTERVAL_SECONDS = 40 * 60  # 2400 — top of the ladder

_SLEEP_MODE_PREFIX = "sleep"


def next_interval_seconds(
    *,
    prev_seconds: int,
    mode: str,
    did_work: bool,
) -> int:
    """Compute the next wake-to-wake interval in seconds.

    ``mode`` is the just-finished wake's mode name (e.g. ``"active"``,
    ``"sleep:consolidate"``). Anything that doesn't start with
    ``"sleep"`` resets to BASE — backoff is sleep-only by design.
    """
    if not mode.startswith(_SLEEP_MODE_PREFIX):
        return BASE_INTERVAL_SECONDS
    if did_work:
        return BASE_INTERVAL_SECONDS
    floor = max(prev_seconds, BASE_INTERVAL_SECONDS)
    return min(floor * 2, MAX_INTERVAL_SECONDS)


def write_interval_atomic(path: pathlib.Path, seconds: int) -> None:
    """Atomically replace the supervisor's interval file.

    The s6 supervisor reads this between wakes. Write via tmp +
    ``os.replace`` so a partial write can never be observed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{int(seconds)}\n")
    os.replace(tmp, path)


def read_interval(
    path: pathlib.Path, default: int = BASE_INTERVAL_SECONDS
) -> int:
    """Read the supervisor's interval file with a sane default.

    Clamps to ``[BASE, MAX]`` so a corrupt file can't drive the
    supervisor into a hot loop or a multi-hour stall.
    """
    try:
        v = int(path.read_text().strip())
    except (OSError, ValueError):
        return default
    if v < BASE_INTERVAL_SECONDS:
        return BASE_INTERVAL_SECONDS
    if v > MAX_INTERVAL_SECONDS:
        return MAX_INTERVAL_SECONDS
    return v


def detect_did_work(mind: pathlib.Path, *, since_ts: float) -> bool:
    """True if any wake file modified since ``since_ts`` declares
    ``did_work: true`` in its frontmatter.

    Source of truth: the agent writes a wake file under
    ``inner/thoughts/<date>/`` each turn with frontmatter that
    includes ``did_work``. We scan files mtime-newer than the wake
    start and look for an explicit truthy flag.

    Default False if no qualifying file exists or none has the field
    — a wake that didn't write a thoughts file looks idle, which
    matches the design's "stable null passes → back off" intent.
    """
    thoughts = mind / "inner" / "thoughts"
    if not thoughts.is_dir():
        return False
    for day_dir in thoughts.iterdir():
        if not day_dir.is_dir():
            continue
        for f in day_dir.glob("*.md"):
            try:
                if f.stat().st_mtime < since_ts:
                    continue
                text = f.read_text()
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            if _is_truthy(fm.get("did_work", "")):
                return True
    return False
