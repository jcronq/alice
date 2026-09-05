"""Stage B — thalamus intake drain.

Deterministic routing for ``~/alice-mind/inner/thalamus/intake/*.md``.
Each tick scans the intake dir, applies the coalescing / aggregation
rules from the thalamus relay design, writes survivors to
``inner/thalamus/filtered/`` and losers to ``inner/thalamus/dropped/``
(with a reason recorded in frontmatter), then atomic-renames the
intake file into ``inner/thalamus/.consumed/<YYYY-MM-DD>/``.

Design contract — see:

- ``cortex-memory/research/2026-06-02-thalamus-relay-layer.md`` §2.3
  (routing rules and coalescing thresholds).
- ``cortex-memory/research/2026-08-07-agent-home-automation-integration-architecture.md`` §2.2
  (SSE inbound integration; cozylobe writes intake, memory worker
  drains it).
- ``alice-mind/inner/thalamus/README.md`` (on-disk protocol reference
  that ships alongside the directory).

NO LLM CALLS. Every decision is a deterministic frontmatter /
threshold check.

Routing chain
-------------

Applied per intake file after grouping. First match wins.

1. **Fast-path kinds** — ``smoke_detected`` / ``glass_break`` /
   ``doorbell_pressed`` from any source → ``dropped/`` with reason
   ``fast_path_already_handled``. The cozylobe fast-path already
   fired the physical-world response; thalamus records the observation
   for audit only.
2. **Motion (cozylobe)** — group intake by ``(source, room)`` within
   a 5-minute sliding window (:data:`MOTION_COALESCE_WINDOW_SECONDS`).
   Isolated blips (1 event, or on-off pair with duration < window)
   → ``dropped/`` reason ``no_sustained_activity``. ≥2 events in a
   window → coalesced single observation to ``filtered/`` carrying
   ``duration_seconds``.
3. **Temperature / light_change (cozylobe)** — group by
   ``(source, entity_id)``, compute the delta between the min and
   max value in the tick. Change below the threshold
   (:data:`TEMPERATURE_MIN_DELTA_F` for temperature,
   :data:`BRIGHTNESS_MIN_DELTA` for light_change) → ``dropped/``
   reason ``below_threshold``. Above → single coalesced
   ``state_change`` observation to ``filtered/``.
4. **Room-quiet** — for every room that had at least one motion event
   in the tick, if the *most recent* observation is older than
   :data:`ROOM_QUIET_THRESHOLD_SECONDS` relative to the tick's
   reference time, also emit one ``room_quiet`` observation to
   ``filtered/``. One per room per tick.
5. **Passthrough** — thinking ``surface`` / ``observation`` and
   speaking ``note`` / ``surface`` route unconditionally to
   ``filtered/``. Downstream promoters (surface watcher, note
   appender) handle the actual placement into ``inner/surface/`` and
   ``inner/notes/``.
6. **Unclassified** — no rule matched → ``dropped/`` reason
   ``no_matching_rule``. Better than silent noise/ shelf.

Consumption semantics
---------------------

An intake file moves to ``.consumed/<YYYY-MM-DD>/<filename>`` via
:func:`os.replace` only after its routing decision was written to
``filtered/`` or ``dropped/``. Failure mid-write leaves the file in
``intake/``; the next tick retries. ``.consumed/<date>/`` is created
on demand.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import pathlib
import re
from typing import Any, Iterable, Optional

from indexer.yaml_lite import split_frontmatter


logger = logging.getLogger(__name__)


# ---------- thresholds (see README / design spec) ----------

#: Coalescing window for motion events from the same (source, room).
#: Events within this many seconds of each other are grouped into a
#: single observation; groups with fewer than
#: :data:`MOTION_MIN_EVENTS_FOR_SUSTAINED` events drop as
#: ``no_sustained_activity``.
MOTION_COALESCE_WINDOW_SECONDS = 300  # 5 min per design §2.3

#: A motion group must contain at least this many events to count as
#: sustained activity. One-off blips (single event, or a lone on/off
#: pair with duration < 60s) drop as noise.
MOTION_MIN_EVENTS_FOR_SUSTAINED = 2

#: Minimum temperature delta (°F) for a temperature ``state_change``
#: to survive routing. Below → ``dropped/`` reason ``below_threshold``.
TEMPERATURE_MIN_DELTA_F = 2.0

#: Minimum brightness delta (0.0–1.0) for a light_change
#: ``state_change`` to survive routing. Below → ``dropped/`` reason
#: ``below_threshold``.
BRIGHTNESS_MIN_DELTA = 0.05

#: Room-quiet threshold. If the most recent motion event for a room
#: is older than this many seconds relative to the tick's reference
#: time, the router emits a single ``room_quiet`` observation for
#: that room.
ROOM_QUIET_THRESHOLD_SECONDS = 900  # 15 min per design §2.3

#: Kinds handled by cozylobe's CRITICAL fast-path. The observation
#: still lands in intake for the audit trail, but the router drops it
#: with reason ``fast_path_already_handled`` to avoid duplicating the
#: fast-path's direct-to-surface write.
FAST_PATH_KINDS = frozenset(
    {"smoke_detected", "glass_break", "doorbell_pressed"}
)

#: Kinds that pass through the router untouched (one filtered file per
#: intake file). Downstream promoters handle final placement.
PASSTHROUGH_KINDS = frozenset({"surface", "observation", "note"})


# ---------- data model ----------


@dataclasses.dataclass
class IntakeEvent:
    """One parsed intake file.

    ``value_num`` is the coerced numeric value for temperature /
    light_change; ``None`` for kinds where a numeric doesn't apply.
    ``observed_at`` is a timezone-aware :class:`datetime.datetime` so
    coalescing math is deterministic across DST boundaries.
    """

    path: pathlib.Path
    source: str
    kind: str
    room: Optional[str]
    entity_id: Optional[str]
    value: Optional[str]
    value_num: Optional[float]
    observed_at: datetime.datetime
    frontmatter: dict[str, Any]
    body: str


@dataclasses.dataclass
class Decision:
    """One routing decision produced by :func:`route_events`.

    ``route`` is ``"filtered"`` or ``"dropped"``. ``reason`` is populated
    for ``dropped`` decisions and left empty for ``filtered`` ones (the
    filtered kind carries the intent). ``sources`` is the list of intake
    file paths that produced this decision — coalescing decisions cover
    multiple, passthrough decisions cover exactly one.
    """

    route: str
    kind: str
    reason: str
    sources: list[pathlib.Path]
    frontmatter: dict[str, Any]
    body: str


@dataclasses.dataclass
class ThalamusReport:
    """Summary of one :func:`run` pass for the heartbeat event."""

    scanned: int = 0
    filtered: int = 0
    dropped: int = 0
    coalesced: int = 0
    malformed: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


# ---------- filesystem paths ----------


def _thalamus_root(vault: pathlib.Path) -> pathlib.Path:
    return vault / "inner" / "thalamus"


def _intake_dir(vault: pathlib.Path) -> pathlib.Path:
    return _thalamus_root(vault) / "intake"


def _filtered_dir(vault: pathlib.Path) -> pathlib.Path:
    return _thalamus_root(vault) / "filtered"


def _dropped_dir(vault: pathlib.Path) -> pathlib.Path:
    return _thalamus_root(vault) / "dropped"


def _consumed_dir(vault: pathlib.Path, day: datetime.date) -> pathlib.Path:
    return _thalamus_root(vault) / ".consumed" / day.isoformat()


# ---------- parsing ----------


_EPOCH_PREFIX_RE = re.compile(r"^(\d{10,})-")


def _parse_observed_at(fm: dict[str, Any], filename: str) -> Optional[datetime.datetime]:
    """Return a timezone-aware datetime for the observation.

    Preference order: frontmatter ``observed_at`` (ISO-8601 with
    offset), then the ``<epoch>-`` prefix on the filename. Both channels
    yield tz-aware datetimes so coalescing math is DST-safe.

    Returns ``None`` if neither channel produces a parseable timestamp
    — the caller drops the note as malformed.
    """
    raw = fm.get("observed_at")
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.datetime.fromisoformat(raw.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            pass
    m = _EPOCH_PREFIX_RE.match(filename)
    if m:
        try:
            return datetime.datetime.fromtimestamp(
                int(m.group(1)), tz=datetime.timezone.utc
            )
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _coerce_number(v: Any) -> Optional[float]:
    """Coerce ``v`` to float when it's a scalar numeric-shaped value."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def parse_intake(path: pathlib.Path) -> Optional[IntakeEvent]:
    """Read an intake file and return the parsed :class:`IntakeEvent`.

    Returns ``None`` when the file is unreadable, has malformed
    frontmatter, or lacks the required ``source`` / ``kind`` /
    parseable ``observed_at`` fields. The caller drops malformed files
    to ``dropped/`` with reason ``malformed_frontmatter``.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("thalamus: failed to read %s: %s", path, exc)
        return None
    try:
        fm, body = split_frontmatter(raw)
    except Exception as exc:  # noqa: BLE001 — parser must not crash drain
        logger.warning("thalamus: malformed frontmatter in %s: %s", path, exc)
        return None
    source = fm.get("source")
    kind = fm.get("kind")
    if not isinstance(source, str) or not source.strip():
        return None
    if not isinstance(kind, str) or not kind.strip():
        return None
    observed_at = _parse_observed_at(fm, path.name)
    if observed_at is None:
        return None
    room = fm.get("room") if isinstance(fm.get("room"), str) else None
    entity_id = (
        fm.get("entity_id") if isinstance(fm.get("entity_id"), str) else None
    )
    raw_value = fm.get("value")
    value: Optional[str]
    if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
        value = str(raw_value)
    else:
        value = None
    if kind == "light_change":
        value_num = _coerce_number(fm.get("brightness"))
    else:
        value_num = _coerce_number(raw_value)
    return IntakeEvent(
        path=path,
        source=source.strip().lower(),
        kind=kind.strip().lower(),
        room=room,
        entity_id=entity_id,
        value=value,
        value_num=value_num,
        observed_at=observed_at,
        frontmatter=fm,
        body=body,
    )


# ---------- decision helpers (pure) ----------


def _now_utc() -> datetime.datetime:
    """Timezone-aware "now" — extracted so tests can freeze time by
    passing an explicit ``reference_time`` to :func:`route_events`."""
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _fast_path_decisions(events: Iterable[IntakeEvent]) -> list[Decision]:
    """Drop fast-path kinds with reason ``fast_path_already_handled``."""
    out: list[Decision] = []
    for e in events:
        fm = {
            "source": e.source,
            "kind": e.kind,
            "entity_id": e.entity_id,
            "observed_at": _iso(e.observed_at),
            "dropped_reason": "fast_path_already_handled",
        }
        out.append(
            Decision(
                route="dropped",
                kind=e.kind,
                reason="fast_path_already_handled",
                sources=[e.path],
                frontmatter={k: v for k, v in fm.items() if v is not None},
                body=e.body,
            )
        )
    return out


def _motion_group_key(e: IntakeEvent) -> tuple[str, str]:
    return (e.source, e.room or "__unknown__")


def _split_motion_windows(
    events: list[IntakeEvent],
    window_seconds: int,
) -> list[list[IntakeEvent]]:
    """Split a sorted event list into windows.

    Adjacent events whose gap is ≤ ``window_seconds`` land in the same
    window. A gap larger than the window opens a new window. The
    5-minute window is a *sliding* window in the design spec, and this
    interpretation matches: any two consecutive events within 5 min
    stay together.
    """
    if not events:
        return []
    events = sorted(events, key=lambda ev: ev.observed_at)
    windows: list[list[IntakeEvent]] = [[events[0]]]
    for prev, curr in zip(events, events[1:]):
        gap = (curr.observed_at - prev.observed_at).total_seconds()
        if gap <= window_seconds:
            windows[-1].append(curr)
        else:
            windows.append([curr])
    return windows


def _motion_decisions(
    events: list[IntakeEvent],
    reference_time: datetime.datetime,
) -> list[Decision]:
    """Route motion events + emit room_quiet per room.

    Groups by (source, room), splits into 5-min windows. A window with
    ≥2 events is coalesced into a single filtered observation carrying
    ``event_count`` and ``duration_seconds``. Single-event windows drop
    as ``no_sustained_activity``. Additionally, when the most recent
    motion event for a room is older than :data:`ROOM_QUIET_THRESHOLD_SECONDS`
    relative to ``reference_time``, one ``room_quiet`` decision is
    emitted for that room.
    """
    out: list[Decision] = []
    groups: dict[tuple[str, str], list[IntakeEvent]] = {}
    for e in events:
        groups.setdefault(_motion_group_key(e), []).append(e)

    for (source, room), group in groups.items():
        room_display = None if room == "__unknown__" else room
        windows = _split_motion_windows(group, MOTION_COALESCE_WINDOW_SECONDS)
        for window in windows:
            if len(window) < MOTION_MIN_EVENTS_FOR_SUSTAINED:
                for ev in window:
                    fm = {
                        "source": ev.source,
                        "kind": "motion",
                        "room": room_display,
                        "entity_id": ev.entity_id,
                        "observed_at": _iso(ev.observed_at),
                        "dropped_reason": "no_sustained_activity",
                    }
                    out.append(
                        Decision(
                            route="dropped",
                            kind="motion",
                            reason="no_sustained_activity",
                            sources=[ev.path],
                            frontmatter={k: v for k, v in fm.items() if v is not None},
                            body=ev.body,
                        )
                    )
                continue
            # Sustained: coalesce.
            first = window[0]
            last = window[-1]
            duration = (last.observed_at - first.observed_at).total_seconds()
            fm = {
                "source": source,
                "kind": "motion_coalesced",
                "room": room_display,
                "started_at": _iso(first.observed_at),
                "ended_at": _iso(last.observed_at),
                "duration_seconds": int(duration),
                "event_count": len(window),
            }
            body = (
                f"Coalesced {len(window)} motion events in "
                f"{room_display or 'unknown room'} over "
                f"{int(duration)}s ({source})."
            )
            out.append(
                Decision(
                    route="filtered",
                    kind="motion_coalesced",
                    reason="",
                    sources=[ev.path for ev in window],
                    frontmatter={k: v for k, v in fm.items() if v is not None},
                    body=body,
                )
            )

        # Room-quiet: if the latest motion for this room is old enough.
        latest = max(ev.observed_at for ev in group)
        age = (reference_time - latest).total_seconds()
        if age > ROOM_QUIET_THRESHOLD_SECONDS and room_display is not None:
            fm = {
                "source": source,
                "kind": "room_quiet",
                "room": room_display,
                "last_motion_at": _iso(latest),
                "quiet_seconds": int(age),
            }
            body = (
                f"Room {room_display} has been quiet for {int(age)}s "
                f"(last motion {_iso(latest)})."
            )
            out.append(
                Decision(
                    route="filtered",
                    kind="room_quiet",
                    reason="",
                    sources=[],  # synthetic, no single source file
                    frontmatter=fm,
                    body=body,
                )
            )
    return out


def _delta_decisions(
    events: list[IntakeEvent], kind: str, min_delta: float, delta_units: str
) -> list[Decision]:
    """Aggregate temperature or light_change events per entity_id.

    All events for an entity in the tick contribute to one delta
    calculation (min → max of ``value_num``). Below the threshold → all
    contributing files drop as ``below_threshold``. Above the threshold
    → one coalesced ``state_change`` observation to ``filtered/``.
    Events lacking a coerceable numeric value drop as
    ``malformed_frontmatter``.
    """
    out: list[Decision] = []
    groups: dict[tuple[str, str], list[IntakeEvent]] = {}
    unknowns: list[IntakeEvent] = []
    for e in events:
        if e.value_num is None or e.entity_id is None:
            unknowns.append(e)
            continue
        groups.setdefault((e.source, e.entity_id), []).append(e)

    for e in unknowns:
        fm = {
            "source": e.source,
            "kind": e.kind,
            "entity_id": e.entity_id,
            "observed_at": _iso(e.observed_at),
            "dropped_reason": "malformed_frontmatter",
        }
        out.append(
            Decision(
                route="dropped",
                kind=e.kind,
                reason="malformed_frontmatter",
                sources=[e.path],
                frontmatter={k: v for k, v in fm.items() if v is not None},
                body=e.body,
            )
        )

    for (source, entity_id), group in groups.items():
        values = [ev.value_num for ev in group if ev.value_num is not None]
        first = min(group, key=lambda ev: ev.observed_at)
        last = max(group, key=lambda ev: ev.observed_at)
        delta = max(values) - min(values)
        room = next(
            (ev.room for ev in group if ev.room), None
        )
        if delta < min_delta:
            for ev in group:
                fm = {
                    "source": ev.source,
                    "kind": ev.kind,
                    "entity_id": ev.entity_id,
                    "room": ev.room,
                    "observed_at": _iso(ev.observed_at),
                    "value": ev.value_num,
                    "delta_observed": round(delta, 4),
                    "delta_threshold": min_delta,
                    "dropped_reason": "below_threshold",
                }
                out.append(
                    Decision(
                        route="dropped",
                        kind=ev.kind,
                        reason="below_threshold",
                        sources=[ev.path],
                        frontmatter={k: v for k, v in fm.items() if v is not None},
                        body=ev.body,
                    )
                )
            continue
        fm = {
            "source": source,
            "kind": "state_change",
            "sub_kind": kind,
            "entity_id": entity_id,
            "room": room,
            "started_at": _iso(first.observed_at),
            "ended_at": _iso(last.observed_at),
            "start_value": first.value_num,
            "end_value": last.value_num,
            "delta": round(delta, 4),
            "delta_units": delta_units,
            "event_count": len(group),
        }
        body = (
            f"{kind} on {entity_id} changed by {round(delta, 4)}{delta_units} "
            f"({first.value_num} → {last.value_num}) over "
            f"{int((last.observed_at - first.observed_at).total_seconds())}s."
        )
        out.append(
            Decision(
                route="filtered",
                kind="state_change",
                reason="",
                sources=[ev.path for ev in group],
                frontmatter={k: v for k, v in fm.items() if v is not None},
                body=body,
            )
        )
    return out


def _passthrough_decisions(events: Iterable[IntakeEvent]) -> list[Decision]:
    """Every passthrough event maps to one ``filtered`` decision.

    The downstream promoter (surface watcher for ``surface`` kinds,
    note appender for ``note`` / ``observation``) handles final
    placement. Thalamus preserves the note verbatim so the promoter
    keeps full context.
    """
    out: list[Decision] = []
    for e in events:
        fm = dict(e.frontmatter)
        fm["thalamus_route"] = "passthrough"
        out.append(
            Decision(
                route="filtered",
                kind=e.kind,
                reason="",
                sources=[e.path],
                frontmatter=fm,
                body=e.body,
            )
        )
    return out


def _unclassified_decisions(events: Iterable[IntakeEvent]) -> list[Decision]:
    """Everything else drops with reason ``no_matching_rule``."""
    out: list[Decision] = []
    for e in events:
        fm = {
            "source": e.source,
            "kind": e.kind,
            "entity_id": e.entity_id,
            "room": e.room,
            "observed_at": _iso(e.observed_at),
            "dropped_reason": "no_matching_rule",
        }
        out.append(
            Decision(
                route="dropped",
                kind=e.kind,
                reason="no_matching_rule",
                sources=[e.path],
                frontmatter={k: v for k, v in fm.items() if v is not None},
                body=e.body,
            )
        )
    return out


def route_events(
    events: list[IntakeEvent],
    *,
    reference_time: Optional[datetime.datetime] = None,
) -> list[Decision]:
    """Apply the full routing chain to a batch of intake events.

    Pure function — no filesystem side effects. ``reference_time``
    defaults to ``datetime.now(UTC)`` and is threaded into the
    room-quiet calculation; tests pass a fixed value to freeze the
    clock without patching :mod:`datetime`.

    The chain groups events by kind, dispatches to the per-kind
    decision builder, and concatenates the results in a deterministic
    order (fast-path first, then motion + room_quiet, then temperature,
    then light_change, then passthrough, then unclassified).
    """
    if reference_time is None:
        reference_time = _now_utc()

    fast_path: list[IntakeEvent] = []
    motion: list[IntakeEvent] = []
    temperature: list[IntakeEvent] = []
    light_change: list[IntakeEvent] = []
    passthrough: list[IntakeEvent] = []
    other: list[IntakeEvent] = []

    for e in events:
        if e.kind in FAST_PATH_KINDS:
            fast_path.append(e)
        elif e.source == "cozylobe" and e.kind == "motion":
            motion.append(e)
        elif e.source == "cozylobe" and e.kind == "temperature":
            temperature.append(e)
        elif e.source == "cozylobe" and e.kind == "light_change":
            light_change.append(e)
        elif e.kind in PASSTHROUGH_KINDS:
            passthrough.append(e)
        else:
            other.append(e)

    decisions: list[Decision] = []
    decisions.extend(_fast_path_decisions(fast_path))
    decisions.extend(_motion_decisions(motion, reference_time))
    decisions.extend(_delta_decisions(temperature, "temperature", TEMPERATURE_MIN_DELTA_F, "F"))
    decisions.extend(_delta_decisions(light_change, "light_change", BRIGHTNESS_MIN_DELTA, ""))
    decisions.extend(_passthrough_decisions(passthrough))
    decisions.extend(_unclassified_decisions(other))
    return decisions


# ---------- filesystem effects ----------


def _atomic_write_text(path: pathlib.Path, content: str) -> None:
    """Write ``content`` to ``path`` via tmp+rename. Mirrors
    :func:`alice_thinking.memory_worker.stage_b._atomic_write_text`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _render_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize ``fm`` as YAML-lite frontmatter.

    Deterministic key order so successive ticks that produce identical
    decisions write byte-identical files (useful for diffing the
    dropped/ audit trail).
    """
    if not fm:
        return "---\n---\n"
    lines = ["---"]
    for key in sorted(fm):
        val = fm[key]
        if val is None:
            continue
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"{key}: {val}")
        elif isinstance(val, list):
            rendered = ", ".join(json.dumps(v, ensure_ascii=False) for v in val)
            lines.append(f"{key}: [{rendered}]")
        else:
            s = str(val)
            if any(c in s for c in [":", "#", "\n"]) or s != s.strip():
                lines.append(f"{key}: {json.dumps(s, ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _decision_filename(decision: Decision, index: int, day: datetime.date) -> str:
    """Pick a filename for a decision's output.

    Uses the first source's stem when available (so filtered/dropped
    files line up with their intake origin in the audit trail).
    Coalesced/synthetic decisions with no source fall back to a
    timestamp + kind + index composition.
    """
    if decision.sources:
        stem = decision.sources[0].stem
        return f"{stem}.md"
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{decision.kind}-{index:03d}.md"


def _write_decision(
    vault: pathlib.Path, decision: Decision, index: int, day: datetime.date
) -> pathlib.Path:
    """Materialize one :class:`Decision` on disk. Returns the target path."""
    target_dir = (
        _filtered_dir(vault) if decision.route == "filtered" else _dropped_dir(vault)
    )
    name = _decision_filename(decision, index, day)
    target = target_dir / name
    if target.exists():
        # Same-name collision: fall back to a suffixed name so we don't
        # overwrite an earlier decision from the same source.
        suffix = 2
        while True:
            alt = target_dir / f"{target.stem}-v{suffix}.md"
            if not alt.exists():
                target = alt
                break
            suffix += 1
    content = _render_frontmatter(decision.frontmatter) + "\n" + decision.body.strip() + "\n"
    _atomic_write_text(target, content)
    return target


def _consume(vault: pathlib.Path, path: pathlib.Path, day: datetime.date) -> None:
    """Move an intake file to ``.consumed/<day>/<name>`` atomically."""
    dest_dir = _consumed_dir(vault, day)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    os.replace(path, dest)


def _scan_intake(vault: pathlib.Path) -> list[pathlib.Path]:
    """Return top-level ``.md`` files in ``intake/`` in sorted order.

    Deterministic ordering ensures partial-failure recovery on the next
    tick sees the same file at the head of the list.
    """
    intake = _intake_dir(vault)
    if not intake.is_dir():
        return []
    out: list[pathlib.Path] = []
    for child in sorted(intake.iterdir()):
        if not child.is_file():
            continue
        if child.name.startswith("."):
            continue
        if not child.name.endswith(".md"):
            continue
        out.append(child)
    return out


def run(
    vault: pathlib.Path,
    *,
    reference_time: Optional[datetime.datetime] = None,
) -> ThalamusReport:
    """Drain ``inner/thalamus/intake/`` once. Returns a :class:`ThalamusReport`.

    Parameters
    ----------
    vault
        Root of ``~/alice-mind/`` (the worker's view of the mind
        directory). All thalamus paths resolve relative to this.
    reference_time
        Optional override for the tick's "now" — used by the
        room-quiet threshold calculation. Tests freeze the clock by
        passing an explicit value. Defaults to ``datetime.now(UTC)``.

    Empty intake → no-op, returns a zeroed report.
    """
    report = ThalamusReport()
    if not _intake_dir(vault).is_dir():
        return report

    day = datetime.date.today()
    paths = _scan_intake(vault)
    if not paths:
        return report

    events: list[IntakeEvent] = []
    malformed: list[pathlib.Path] = []
    for p in paths:
        report.scanned += 1
        ev = parse_intake(p)
        if ev is None:
            malformed.append(p)
        else:
            events.append(ev)

    # Malformed intakes go straight to dropped/ with a fixed reason.
    for p in malformed:
        try:
            fm = {
                "source": "unknown",
                "kind": "unknown",
                "original_filename": p.name,
                "dropped_reason": "malformed_frontmatter",
            }
            decision = Decision(
                route="dropped",
                kind="unknown",
                reason="malformed_frontmatter",
                sources=[p],
                frontmatter=fm,
                body=f"Malformed intake — see original at .consumed/{day.isoformat()}/{p.name}",
            )
            _write_decision(vault, decision, 0, day)
            _consume(vault, p, day)
            report.malformed += 1
        except OSError as exc:
            logger.warning(
                "thalamus: failed to handle malformed intake %s: %s", p, exc
            )
            report.errors += 1

    decisions = route_events(events, reference_time=reference_time)

    # Track which intake paths each decision covers so we can consume
    # exactly the set that produced routed output.
    consumed_paths: set[pathlib.Path] = set()
    for idx, decision in enumerate(decisions):
        try:
            _write_decision(vault, decision, idx, day)
        except OSError as exc:
            logger.warning(
                "thalamus: failed to write decision (route=%s kind=%s): %s — "
                "leaving %d source(s) in intake for retry",
                decision.route,
                decision.kind,
                exc,
                len(decision.sources),
            )
            report.errors += 1
            continue
        if decision.route == "filtered":
            report.filtered += 1
            if len(decision.sources) > 1:
                report.coalesced += 1
        else:
            report.dropped += 1
        for src in decision.sources:
            consumed_paths.add(src)

    for src in consumed_paths:
        try:
            _consume(vault, src, day)
        except OSError as exc:
            logger.warning(
                "thalamus: consume failed for %s after routing: %s", src, exc
            )
            report.errors += 1

    return report
