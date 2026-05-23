"""Sleep-mode wake-cadence backoff policy.

Pure policy module — given the previous interval, the just-finished
wake's mode, and whether that wake did meaningful work, return the
next wake-to-wake interval. Keeping cap/ladder constants here lets
``wake.py`` stay per-turn deterministic and lets the policy be
unit-tested in isolation.

Behavior (from
``cortex-memory/research/2026-05-01-sleep-mode-exponential-backoff-design.md``,
revised by issue #323 — wake cadence fixes):

- Active mode: always 5 min — backoff applies to sleep only.
- Sleep mode + did_work=True: hard reset to 5 min.
- Sleep mode + did_work=False: double the previous interval, capped
  at 30 min. Ladder: 5 → 10 → 20 → 30. Cap lowered from 40 min by
  issue #323 — at 40 min the ladder produced only 3 wakes over 8 hours
  of sleep; 30 min guarantees ≥16 wakes per 8h period as a baseline.
- ``MIN_WAKE_PERIOD`` (30 min) is the cadence-level floor enforced by
  ``wake.py`` against ``last-wake-timestamp``: if the last wake fired
  more recently than that and the computed next interval is above the
  floor, the interval is clamped down to the floor.

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
# Top of the ladder. Lowered from 40 min → 30 min by issue #323; at 40
# min the May-22 sleep cycle managed only 3 wakes over 8 hours because
# every Stage B/D drain was invisible to ``detect_did_work`` and the
# ladder climbed unchallenged. 30 min × 16 = 8 hours, so this floors
# the per-night wake count at ~16 even if every single wake is idle.
MAX_INTERVAL_SECONDS = 30 * 60  # 1800 — top of the ladder
# Cadence-level minimum-wake guarantee. Used by ``wake.py`` to clamp
# the supervisor interval against ``last-wake-timestamp`` so a long
# tail of idle wakes can never push the next wake past 30 min from
# the previous one. Kept here so the cap + the floor share a single
# source of truth.
MIN_WAKE_PERIOD = 30 * 60  # 1800

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


TIMESTAMP_FILE_NAME = "last-wake-timestamp"


def apply_min_wake_period(
    next_seconds: int,
    *,
    last_wake_ts: float | None,
    now_ts: float,
    min_period: int = MIN_WAKE_PERIOD,
) -> int:
    """Cadence-level minimum-wake guarantee (issue #323 fix 2).

    The supervisor measures intervals between consecutive wakes; if a
    wake fires very recently and the next-interval write still sits
    near the top of the ladder, the *combined* effect can stretch the
    cycle past the supervisor's expected cadence. This clamps
    ``next_seconds`` down to ``min_period`` whenever:

    * we have a recent ``last_wake_ts`` (``now_ts - last_wake_ts <
      min_period``), AND
    * the computed next interval would otherwise exceed ``min_period``.

    Why only "if it would otherwise exceed": the floor only kicks in
    when backoff has climbed. A reset-to-BASE after meaningful work
    must stay at BASE so cadence accelerates back to active levels.

    Returns the (possibly clamped) interval in seconds.
    """
    if last_wake_ts is None:
        return next_seconds
    try:
        elapsed = now_ts - float(last_wake_ts)
    except (TypeError, ValueError):
        return next_seconds
    if elapsed < min_period and next_seconds > min_period:
        return min_period
    return next_seconds


def read_last_wake_timestamp(path: pathlib.Path) -> float | None:
    """Read ``last-wake-timestamp`` if present; return None on any error.

    The timestamp file lives alongside ``next-thinking-interval-seconds``
    in the worker state dir. Stamped after every backoff write so a
    later wake can compute "how long since the prior wake fired".
    """
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def write_last_wake_timestamp(path: pathlib.Path, ts: float) -> None:
    """Atomically write the wall-clock timestamp of the current wake.

    Mirrors :func:`write_interval_atomic`'s tmp + replace pattern so a
    partial write can never be observed by a concurrent reader.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{float(ts):.6f}\n")
    os.replace(tmp, path)


def write_interval_atomic(path: pathlib.Path, seconds: int) -> None:
    """Atomically replace the supervisor's interval file.

    The s6 supervisor reads this between wakes. Write via tmp +
    ``os.replace`` so a partial write can never be observed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{int(seconds)}\n")
    os.replace(tmp, path)


def read_interval(path: pathlib.Path, default: int = BASE_INTERVAL_SECONDS) -> int:
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
