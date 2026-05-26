"""Typed-edge trajectory growth for the cozylobe motion-cortex pipeline
(Phase 4 of #381).

When the qwen classify call returns a positional inference that names a
``next_room_hypothesis``, the trajectory recorder persists the observed
edge as a per-person, per-(from-room, to-room) note under
``cozylobe-cortex/trajectories/<person-slug>/<from>-to-<to>.md``. Each
subsequent observation of the same edge bumps an ``observation_count``,
adds the relevant time-of-day bucket, and recomputes a confidence
weight using the same curve as :mod:`alice_cozylobe.adjacency`.

Design references:

* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` §2.2
  (Trajectory note schema) — the design's title-of-three-rooms scheme
  is the high-confidence aggregation; Phase 4 persists the per-edge
  building blocks the aggregator will later consume.
* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` §5.2
  (typed-weighted-edges) — body carries the canonical inline edge
  ``(OFTEN-VISITS:weight)[[rooms/To]]`` so the relationship is
  queryable through the standard cortex graph.

Time-bucket scheme (4-hour windows):

* ``00-06`` — overnight
* ``06-12`` — morning
* ``12-18`` — afternoon
* ``18-24`` — evening

A trajectory observed at multiple times-of-day accumulates buckets;
the bucket set is part of the frontmatter so downstream consumers
(the next-room predictor, thinking's pattern grooming) can filter
by time-of-day.

The recorder is sandbox-only — only writes inside the vault root via
atomic tempfile+rename. Failure modes log and fail-open: a broken
write must never take the motion pipeline down.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


__all__ = [
    "TIME_BUCKETS",
    "TRAJECTORY_PROMOTION_CURVE",
    "TrajectoryRecord",
    "bucket_for_hour",
    "load_trajectory",
    "record_trajectory",
    "trajectory_path",
    "trajectory_weight",
]


log = logging.getLogger(__name__)


# Time-of-day buckets — 6-hour windows. Chosen to match the design's
# "Kitchen-at-07:00" person-pattern shape: morning routines fall in
# 06-12, evenings in 18-24. Wide enough that a typical week clusters
# into one or two buckets; narrow enough that 07:00 visits don't blur
# with 22:00 visits.
TIME_BUCKETS: tuple[str, ...] = ("00-06", "06-12", "12-18", "18-24")


# Confidence curve. Same shape + asymptote as the adjacency promotion
# curve — observation count bumps weight along discrete thresholds,
# capped at 0.9 so explicit operator confirmation can still push to 1.0.
TRAJECTORY_PROMOTION_CURVE: tuple[tuple[int, float], ...] = (
    (1, 0.1),
    (5, 0.3),
    (10, 0.5),
    (20, 0.7),
    (50, 0.9),
)


def bucket_for_hour(hour: int) -> str:
    """Map a 0..23 hour-of-day to one of :data:`TIME_BUCKETS`."""
    if not 0 <= hour <= 23:
        # Out-of-range → bucket it like 0; defensive only.
        hour = 0
    for bucket in TIME_BUCKETS:
        start_s, end_s = bucket.split("-")
        start, end = int(start_s), int(end_s)
        if start <= hour < end:
            return bucket
    return TIME_BUCKETS[-1]


def trajectory_weight(count: int) -> float:
    """Map an observation count → confidence weight in [0.0, 0.9]."""
    weight = 0.0
    for threshold, w in TRAJECTORY_PROMOTION_CURVE:
        if count >= threshold:
            weight = w
        else:
            break
    return weight


# ---------------------------------------------------------------------------
# Slug helpers


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: Optional[str], fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    s = _SLUG_RE.sub("-", str(value).lower()).strip("-")
    return s or fallback


# ---------------------------------------------------------------------------
# Dataclass


@dataclass
class TrajectoryRecord:
    """In-memory shape of one trajectory edge note.

    Round-trips through :func:`load_trajectory` + :func:`record_trajectory`.
    The set of fields matches the Phase 4 spec exactly so hand-inspecting
    a vault note is unambiguous.
    """

    person: str  # title-cased like "Jason" or "unknown"
    from_room: str  # room title
    to_room: str  # room title
    observation_count: int = 0
    time_buckets: set[str] = field(default_factory=set)
    confidence: float = 0.0
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    last_observed: Optional[datetime] = None
    path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Path resolution


def trajectory_path(vault_root: Path, person: str, from_room: str, to_room: str) -> Path:
    """Return the on-disk path for a (person, from, to) trajectory edge.

    Shape: ``trajectories/<person-slug>/<from-slug>-to-<to-slug>.md``.
    The per-person subdir keeps the directory listing readable when
    multiple residents accumulate trajectories.
    """
    person_slug = _slug(person, fallback="unknown")
    filename = f"{_slug(from_room)}-to-{_slug(to_room)}.md"
    return vault_root / "trajectories" / person_slug / filename


# ---------------------------------------------------------------------------
# IO helpers
#
# Duplicated (not imported) from guesses._atomic_write — keeps the
# trajectories module's dependency surface minimal.


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-", suffix=".md"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Render + parse


def _render(record: TrajectoryRecord) -> str:
    """Render a TrajectoryRecord as the markdown body that lands on disk."""
    fm_lines: list[str] = [
        "---",
        f"title: {record.person}: {record.from_room} → {record.to_room}",
        "tags: [trajectory, cozylobe-cortex]",
        f"created: {_iso(record.created)}",
        f"updated: {_iso(record.updated)}",
        f"person: \"[[people/{record.person}]]\"",
        f"from_room: \"[[rooms/{record.from_room}]]\"",
        f"to_room: \"[[rooms/{record.to_room}]]\"",
        f"observation_count: {record.observation_count}",
        f"time_buckets: [{', '.join(sorted(record.time_buckets))}]",
        f"confidence: {record.confidence:.3f}",
    ]
    if record.last_observed is not None:
        fm_lines.append(f"last_observed: {_iso(record.last_observed)}")
    fm_lines.append("---")

    body_lines = [
        "",
        f"# {record.person}: {record.from_room} → {record.to_room}",
        "",
        f"Observed {record.observation_count}× across buckets "
        f"{sorted(record.time_buckets)}.",
        "",
        # Canonical inline typed-weighted edge so the relationship is
        # queryable through the cortex graph the same way every other
        # cortex relation is.
        f"(OFTEN-VISITS:{record.confidence:.2f})[[rooms/{record.to_room}]]",
        "",
    ]
    return "\n".join(fm_lines) + "\n".join(body_lines)


_FENCE = "---"


def _split_fm(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != _FENCE:
        return "", text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        return "", text
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :])


def _parse_fm(fm_text: str) -> dict:
    """Minimal YAML parser for trajectory frontmatter.

    Recognises: scalar strings, inline-list ``[a, b, c]``, ints, floats.
    The vault doesn't have nested structures in trajectory frontmatter,
    so a flat parser is sufficient. Failures degrade to an empty dict —
    the load path uses safe defaults for anything missing.
    """
    out: dict = {}
    for raw in fm_text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            out[key] = [item.strip() for item in inner.split(",") if item.strip()]
            continue
        # Strip surrounding quotes.
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\]]+)\]\]")


def _strip_wikilink(value: str, category: str) -> str:
    """Reverse the ``[[category/Title]]`` shape; tolerant of the bare
    category-stripped form too.
    """
    s = (value or "").strip().strip("\"'")
    if not s:
        return ""
    m = _WIKILINK_RE.match(s)
    if m:
        target = m.group("target").strip()
        if "/" in target:
            return target.split("/", 1)[-1]
        return target
    if s.startswith(f"{category}/"):
        return s[len(category) + 1 :].strip()
    return s


def load_trajectory(path: Path) -> Optional[TrajectoryRecord]:
    """Parse a trajectory note from disk; return ``None`` if unreadable.

    Tolerant of partial frontmatter — missing fields default to safe
    values. The path's parent directory name is used as a fallback
    person slug when the frontmatter doesn't carry one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_text, _body = _split_fm(text)
    fm = _parse_fm(fm_text)

    person = _strip_wikilink(str(fm.get("person", "")), "people")
    if not person:
        person = path.parent.name or "unknown"
    from_room = _strip_wikilink(str(fm.get("from_room", "")), "rooms")
    to_room = _strip_wikilink(str(fm.get("to_room", "")), "rooms")
    try:
        count = int(fm.get("observation_count", 0))
    except (TypeError, ValueError):
        count = 0
    try:
        confidence = float(fm.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    buckets_raw = fm.get("time_buckets", [])
    if isinstance(buckets_raw, list):
        buckets = {b for b in buckets_raw if b in TIME_BUCKETS}
    else:
        buckets = set()
    created = _parse_iso(fm.get("created"))
    updated = _parse_iso(fm.get("updated"))
    last_observed = _parse_iso(fm.get("last_observed"))

    return TrajectoryRecord(
        person=person,
        from_room=from_room,
        to_room=to_room,
        observation_count=count,
        time_buckets=buckets,
        confidence=confidence,
        created=created,
        updated=updated,
        last_observed=last_observed,
        path=path,
    )


# ---------------------------------------------------------------------------
# Record


def record_trajectory(
    vault_root: Path,
    person: Optional[str],
    from_room: str,
    to_room: str,
    ts: datetime,
) -> TrajectoryRecord:
    """Persist one (person, from, to) observation at time ``ts``.

    Increments ``observation_count``, adds the relevant time bucket,
    recomputes ``confidence`` per :func:`trajectory_weight`, and writes
    the result atomically. Creates the note on first observation.

    Returns the in-memory record post-update so callers (the motion
    pipeline) can log or telemetry it. Raises ``OSError`` only on a
    truly broken write — the caller (the pipeline) catches and
    fail-opens.
    """
    if not from_room or not to_room:
        raise ValueError(
            f"trajectory needs from+to rooms; got from={from_room!r} to={to_room!r}"
        )
    person_clean = person if person and person.strip() else "unknown"

    path = trajectory_path(vault_root, person_clean, from_room, to_room)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    existing = load_trajectory(path) if path.is_file() else None
    if existing is not None:
        existing.observation_count += 1
        existing.time_buckets.add(bucket_for_hour(ts.astimezone(timezone.utc).hour))
        existing.confidence = trajectory_weight(existing.observation_count)
        existing.updated = ts
        existing.last_observed = ts
        # Person / room titles in the existing note are authoritative,
        # but fill in if they got hand-edited away.
        if not existing.person:
            existing.person = person_clean
        if not existing.from_room:
            existing.from_room = from_room
        if not existing.to_room:
            existing.to_room = to_room
        record = existing
    else:
        record = TrajectoryRecord(
            person=person_clean,
            from_room=from_room,
            to_room=to_room,
            observation_count=1,
            time_buckets={bucket_for_hour(ts.astimezone(timezone.utc).hour)},
            confidence=trajectory_weight(1),
            created=ts,
            updated=ts,
            last_observed=ts,
            path=path,
        )
    _atomic_write(path, _render(record))
    record.path = path
    return record


# ---------------------------------------------------------------------------
# Iteration helper for tests + callers that want to walk all trajectories


def iter_trajectories(vault_root: Path) -> Iterable[TrajectoryRecord]:
    """Yield every parseable trajectory record under ``vault_root``."""
    root = vault_root / "trajectories"
    if not root.is_dir():
        return
    for person_dir in sorted(root.iterdir()):
        if not person_dir.is_dir():
            continue
        for md in sorted(person_dir.glob("*.md")):
            record = load_trajectory(md)
            if record is not None:
                yield record
