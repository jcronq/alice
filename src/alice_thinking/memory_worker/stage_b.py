"""Stage B — inbox drain.

Deterministic routing for ``~/alice-mind/inner/notes/*.md``. Each
tick scans the inbox, classifies every note via a chain of rules
(first match wins), writes the routed output into the vault /
``events.jsonl`` / today's daily, and atomic-renames the note into
``inner/notes/.consumed/<YYYY-MM-DD>/`` **only after** the vault
write and journal commit succeed.

Design contract — see
``cortex-memory/research/2026-06-01-memory-worker-extraction-design.md``
§3 (Routing rules) and §6 (Surface contract).

NO LLM CALLS. The routing rules are deterministic substring /
regex / frontmatter checks. Edge cases that no rule matches become
``conflict-candidate`` surfaces for thinking to disambiguate; the
note moves to ``.failed/`` so the next tick doesn't re-emit the
same surface.

Routing chain (first match wins)
--------------------------------

1. **Activity** — `tag` in :data:`ACTIVITY_TAGS` *or* body opens
   with ``HH:MM ...`` — append to today's daily.
2. **Structured event** — `tag` in :data:`EVENT_TAGS` *or* body
   has a deterministic marker (``ate ``, ``workout:``, etc.) —
   write a JSON line to ``events.jsonl`` AND append a one-liner to
   today's daily.
3. **New concept** — `tag` in :data:`CONCEPT_TAGS` — write or
   merge an atomic vault note in the matching folder.
4. **Conflict candidate** — `tag == "conflict-candidate"` —
   write to ``cortex-memory/conflicts/``.
5. **Noise** — `tag == "noise"` or `route == "noise"` — append a
   one-liner to today's daily.
6. **Unclassified** — no rule matched — write a
   ``conflict-candidate`` surface to ``inner/surface/`` AND move
   the note to ``.failed/`` so the surface doesn't re-emit on the
   next tick.

Consumption semantics
---------------------

A note moves to ``.consumed/<YYYY-MM-DD>/<filename>`` via
:func:`os.replace` (POSIX atomic rename) ONLY after the routed
write committed cleanly. Failure mid-routing leaves the note in
the inbox; the next tick retries. ``.consumed/<date>/`` is created
on demand. If a same-named target already exists in
``.consumed/<date>/`` (partial prior run), ``os.replace`` overwrites
— documented and tested in :mod:`tests.test_memory_worker_stage_b`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import pathlib
import re
from typing import Any, Callable, Optional

from indexer.yaml_lite import split_frontmatter


logger = logging.getLogger(__name__)


# ---------- routing tag sets ----------

#: Frontmatter ``tag`` values that route to today's daily as an
#: activity line. Matches the decision tree in thinking's
#: CLAUDE.md (Step 1 / inbox drain).
ACTIVITY_TAGS = frozenset({"activity", "daily", "log", "session"})

#: Frontmatter ``tag`` values that route to ``events.jsonl`` +
#: daily one-liner per the events schema. Each maps 1:1 to the
#: ``type`` field of the resulting JSONL record.
EVENT_TAGS = frozenset({"meal", "workout", "weight", "reminder", "error", "lift", "waist", "note"})

#: Frontmatter ``tag`` values that route to atomic vault notes
#: in the matching folder. ``feedback`` lives under ``feedback/``,
#: ``person`` under ``people/``, etc.
CONCEPT_TAGS = frozenset({"person", "project", "reference", "design", "feedback", "source"})

#: Frontmatter ``tag`` values that drop to a daily one-liner only
#: (no vault note). CozyLobe motion batches and similar low-signal
#: sensor data live here.
NOISE_TAGS = frozenset({"noise", "low-signal", "sensor"})

#: Folder for each concept tag. ``person`` is special — the vault
#: convention is plural ``people/``.
_CONCEPT_FOLDER = {
    "person": "people",
    "project": "projects",
    "reference": "reference",
    "design": "research",  # design docs live alongside research per current vault
    "feedback": "feedback",
    "source": "sources",
}

#: Body markers that promote a note to a structured event when the
#: frontmatter doesn't already say so. Order matters — first hit
#: wins. Patterns are case-insensitive and matched against the
#: first non-empty body line.
_EVENT_BODY_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("meal", re.compile(r"^\s*(ate\b|breakfast:|lunch:|dinner:|snack:|meal:)", re.IGNORECASE)),
    ("workout", re.compile(r"^\s*workout:", re.IGNORECASE)),
    ("weight", re.compile(r"^\s*weight:", re.IGNORECASE)),
    ("reminder", re.compile(r"^\s*reminder:", re.IGNORECASE)),
    ("error", re.compile(r"^\s*error:", re.IGNORECASE)),
)

#: Body opener that promotes a note to an activity line when the
#: frontmatter doesn't already say so. ``HH:MM`` (24-hour) at the
#: start of the first non-empty body line.
_ACTIVITY_BODY_RE = re.compile(r"^\s*\d{1,2}:\d{2}\b")


# ---------- routing data model ----------


@dataclasses.dataclass
class Route:
    """One classified note's routing decision.

    ``kind`` is the rule name that matched. ``payload`` is the
    rule-specific data the writer consumes (e.g. ``slug`` for
    concept routes, ``event_type`` for event routes).
    """

    kind: str
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DrainReport:
    """Summary of one :func:`run` pass for the heartbeat event."""

    scanned: int = 0
    routed_activity: int = 0
    routed_event: int = 0
    routed_concept: int = 0
    routed_conflict: int = 0
    routed_noise: int = 0
    unclassified: int = 0
    malformed: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


# ---------- frontmatter normalization ----------


def _frontmatter_tag(fm: dict[str, Any]) -> Optional[str]:
    """Extract a single tag value from ``fm``, handling both
    ``tag: x`` and ``tags: [x, y, z]`` shapes.

    Returns the first usable string tag, or ``None`` if no
    classifier tag is present. Routing rules that care about
    multi-tag matches read ``fm["tags"]`` directly.
    """
    raw = fm.get("tag")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    tags = fm.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.strip():
                return t.strip().lower()
    if isinstance(tags, str) and tags.strip():
        return tags.strip().lower()
    return None


def _frontmatter_tag_set(fm: dict[str, Any]) -> set[str]:
    """Return every tag-looking string from ``fm`` as a lowercase
    set. Includes the singular ``tag`` field, every entry of
    ``tags``, and (for the noise route) the ``route`` field.

    This is what the routing chain consults when checking whether
    a note carries *any* tag in a category set, regardless of
    where it lives in the frontmatter.
    """
    out: set[str] = set()
    for key in ("tag", "route"):
        v = fm.get(key)
        if isinstance(v, str) and v.strip():
            out.add(v.strip().lower())
    tags = fm.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.strip():
                out.add(t.strip().lower())
    elif isinstance(tags, str) and tags.strip():
        out.add(tags.strip().lower())
    return out


def _first_body_line(body: str) -> str:
    """First non-empty stripped line of ``body``, or empty string."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# ---------- routing rules (first match wins) ----------


def _route_activity(fm: dict[str, Any], body: str) -> Optional[Route]:
    """Activity → today's daily.

    Trigger: frontmatter tag in :data:`ACTIVITY_TAGS`, OR the first
    body line opens with ``HH:MM`` (the format of daily entries
    written by Speaking-side dailies).
    """
    tagset = _frontmatter_tag_set(fm)
    if tagset & ACTIVITY_TAGS:
        return Route("activity", {})
    if _ACTIVITY_BODY_RE.match(_first_body_line(body)):
        return Route("activity", {})
    return None


def _route_event(fm: dict[str, Any], body: str) -> Optional[Route]:
    """Structured event → ``events.jsonl`` + daily one-liner.

    Trigger: frontmatter tag in :data:`EVENT_TAGS`, OR the first
    body line opens with a deterministic marker like ``ate `` or
    ``workout:``.

    Returns the event type in ``payload["event_type"]`` so the
    writer can build the schema-compliant record without re-running
    the classifier.
    """
    tagset = _frontmatter_tag_set(fm)
    intersect = tagset & EVENT_TAGS
    if intersect:
        # Deterministic choice when multiple event tags overlap:
        # walk EVENT_TAGS in declaration order so the result is
        # stable across runs.
        for candidate in ("meal", "workout", "weight", "lift", "waist", "reminder", "error", "note"):
            if candidate in intersect:
                return Route("event", {"event_type": candidate})
    first = _first_body_line(body)
    for event_type, pattern in _EVENT_BODY_MARKERS:
        if pattern.match(first):
            return Route("event", {"event_type": event_type})
    return None


def _route_concept(fm: dict[str, Any], body: str, filename: str) -> Optional[Route]:
    """New concept → atomic vault note (or merge).

    Trigger: frontmatter tag in :data:`CONCEPT_TAGS`. The slug is
    derived from the source filename (without ``.md``); the folder
    is determined by :data:`_CONCEPT_FOLDER`.
    """
    tagset = _frontmatter_tag_set(fm)
    intersect = tagset & CONCEPT_TAGS
    if not intersect:
        return None
    # Stable choice when multiple concept tags overlap.
    for candidate in ("person", "project", "reference", "design", "feedback", "source"):
        if candidate in intersect:
            return Route(
                "concept",
                {
                    "concept_tag": candidate,
                    "folder": _CONCEPT_FOLDER[candidate],
                    "slug": _slug_from_filename(filename),
                },
            )
    return None


def _route_conflict(fm: dict[str, Any], _body: str, filename: str) -> Optional[Route]:
    """Conflict candidate → ``cortex-memory/conflicts/``.

    Trigger: frontmatter tag == ``"conflict-candidate"`` (matches
    the convention from the cortex-memory skill).
    """
    if "conflict-candidate" in _frontmatter_tag_set(fm):
        return Route("conflict", {"slug": _slug_from_filename(filename)})
    return None


def _route_noise(fm: dict[str, Any], _body: str) -> Optional[Route]:
    """Noise → daily one-liner.

    Trigger: frontmatter tag (or ``route:``) in :data:`NOISE_TAGS`.

    Threshold: the rule is intentionally narrow — a note must
    *explicitly* be tagged as noise/low-signal/sensor. Sensor
    routing from Speaking-side already uses ``route: noise`` in the
    frontmatter (see CozyLobe motion batches in
    ``.consumed/2026-05-31/``). Anything not explicitly flagged
    falls through to the unclassified rule, which is the safer
    failure mode for ambiguous content.
    """
    if _frontmatter_tag_set(fm) & NOISE_TAGS:
        return Route("noise", {})
    return None


def _classify(fm: dict[str, Any], body: str, filename: str) -> Optional[Route]:
    """Run the routing chain. First match wins; ``None`` means
    unclassified (handled by the caller)."""
    for fn in (
        lambda: _route_activity(fm, body),
        lambda: _route_event(fm, body),
        lambda: _route_concept(fm, body, filename),
        lambda: _route_conflict(fm, body, filename),
        lambda: _route_noise(fm, body),
    ):
        result = fn()
        if result is not None:
            return result
    return None


# ---------- writer helpers ----------


def _today() -> datetime.date:
    return datetime.date.today()


def _now_iso_tz() -> str:
    """Local-time ISO-8601 with offset, matching the convention in
    ``events.jsonl`` records produced by ``event-log``."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _slug_from_filename(filename: str) -> str:
    """Filename without the ``.md`` extension. Deterministic; no
    title-frontmatter fallback (titles are free-form prose that
    would collide on whitespace, casing, and emoji)."""
    return filename[:-3] if filename.endswith(".md") else filename


def _daily_path(vault: pathlib.Path, day: datetime.date) -> pathlib.Path:
    return vault / "cortex-memory" / "dailies" / f"{day.isoformat()}.md"


def _ensure_daily(vault: pathlib.Path, day: datetime.date) -> pathlib.Path:
    """Create today's daily from the canonical template if missing.

    Format matches recent dailies (``2026-05-30.md`` etc.): a
    frontmatter block followed by an ``# YYYY-MM-DD`` heading.
    """
    path = _daily_path(vault, day)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    iso = day.isoformat()
    template = (
        "---\n"
        f"title: {iso}\n"
        "tags: [daily]\n"
        f"created: {iso}\n"
        f"updated: {iso}\n"
        f"last_accessed: {iso}\n"
        "access_count: 0\n"
        "---\n"
        "\n"
        f"# {iso}\n"
        "\n"
    )
    path.write_text(template, encoding="utf-8")
    return path


def _append_to_daily(vault: pathlib.Path, day: datetime.date, line: str) -> None:
    """Append a single daily entry. The entry is a bullet that
    matches the format already in use (``- **HH:MM EDT** — ...``).

    The caller passes the full line (without trailing newline). The
    writer ensures the file ends with exactly one newline before
    the append, and adds a trailing newline after.
    """
    path = _ensure_daily(vault, day)
    existing = path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + line + "\n", encoding="utf-8")


def _local_hhmm() -> str:
    """``HH:MM`` of the local clock for daily-entry prefixes."""
    return datetime.datetime.now().strftime("%H:%M")


def _abbrev_tz() -> str:
    """Local timezone abbreviation (``EDT`` / ``EST``) for daily
    entry suffixes. Falls back to UTC offset when ``tzname()``
    returns ``None`` (some container locales)."""
    name = datetime.datetime.now().astimezone().tzname()
    return name or "UTC"


def _events_jsonl_path(vault: pathlib.Path) -> pathlib.Path:
    return vault / "memory" / "events.jsonl"


def _conflicts_dir(vault: pathlib.Path) -> pathlib.Path:
    return vault / "cortex-memory" / "conflicts"


def _surface_dir(vault: pathlib.Path) -> pathlib.Path:
    return vault / "inner" / "surface"


def _inbox_dir(vault: pathlib.Path) -> pathlib.Path:
    return vault / "inner" / "notes"


def _consumed_dir(vault: pathlib.Path, day: datetime.date) -> pathlib.Path:
    return _inbox_dir(vault) / ".consumed" / day.isoformat()


def _failed_dir(vault: pathlib.Path, day: datetime.date) -> pathlib.Path:
    return _inbox_dir(vault) / ".failed" / day.isoformat()


# ---------- per-route writers ----------


def _write_activity(
    vault: pathlib.Path, fm: dict[str, Any], body: str, filename: str
) -> None:
    """Append an activity entry to today's daily.

    If the body's first line already contains an ``HH:MM`` prefix,
    we use it verbatim as the entry body — Speaking's activity
    notes already format that way. Otherwise we prepend the local
    time so the daily stays scannable.
    """
    first = _first_body_line(body) or _slug_from_filename(filename)
    if _ACTIVITY_BODY_RE.match(first):
        line = f"- {first}"
    else:
        line = f"- **{_local_hhmm()} {_abbrev_tz()}** — {first}"
    _append_to_daily(vault, _today(), line)


def _write_event(
    vault: pathlib.Path,
    fm: dict[str, Any],
    body: str,
    filename: str,
    event_type: str,
) -> None:
    """Append a schema-conformant record to ``events.jsonl`` AND a
    one-liner to today's daily.

    The ``data`` block is best-effort: we copy any frontmatter
    fields that look like primitive scalars (excluding the
    bookkeeping fields ``tag``/``tags``/``created``/etc.). The
    daily one-liner gives Jason a human-readable trace of the
    event without forcing him to ``jq`` the events file.

    Per :data:`memory/EVENTS-SCHEMA.md` (the "both or neither"
    rule), the events.jsonl append and the daily append are a
    single logical operation — if the events write succeeds but
    the daily write fails, the caller's exception bubbles up and
    the note stays in the inbox for retry.
    """
    record = {
        "ts": _now_iso_tz(),
        "type": event_type,
        "subject": str(fm.get("subject") or "jason"),
        "data": _coerce_event_data(fm, body),
        "source": str(fm.get("source") or "memory-worker"),
    }
    path = _events_jsonl_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = _event_summary(record, body, filename)
    line = f"- **{_local_hhmm()} {_abbrev_tz()}** — {event_type}: {summary}"
    _append_to_daily(vault, _today(), line)


def _coerce_event_data(fm: dict[str, Any], body: str) -> dict[str, Any]:
    """Build the ``data`` block for an events.jsonl record.

    Strategy: copy primitive scalars from frontmatter (excluding
    the bookkeeping keys), then fall back to the first body line
    as ``summary`` if nothing scalar was present. This stays
    lossless — the original note is preserved in ``.consumed/`` —
    and gives downstream consumers enough to query on.
    """
    skip = {"tag", "tags", "created", "updated", "last_accessed",
            "access_count", "subject", "source", "type", "route", "title"}
    data: dict[str, Any] = {}
    for k, v in fm.items():
        if k in skip:
            continue
        if isinstance(v, (str, int, float, bool)) and not isinstance(v, bool):
            data[k] = v
        elif isinstance(v, bool):
            data[k] = v
    if not data:
        first = _first_body_line(body)
        if first:
            data["summary"] = first
    return data


def _event_summary(record: dict[str, Any], body: str, filename: str) -> str:
    """One-line human summary for the daily entry."""
    summary = record["data"].get("summary")
    if isinstance(summary, str) and summary:
        return summary
    first = _first_body_line(body)
    if first:
        return first
    return _slug_from_filename(filename)


def _write_concept(
    vault: pathlib.Path,
    fm: dict[str, Any],
    body: str,
    filename: str,
    folder: str,
    slug: str,
) -> None:
    """Write a new atomic vault note, or merge into an existing
    one with the same slug.

    Merge semantics: the existing note is preserved as-is and a
    dated section is appended (``## Update YYYY-MM-DD``). We do
    NOT rewrite the frontmatter; the cortex-memory skill is the
    authoritative editor for top-level fields.

    Fresh notes get a frontmatter block conforming to the vault
    convention (``created``, ``updated``, ``last_accessed``,
    ``access_count: 0``).
    """
    target_dir = vault / "cortex-memory" / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{slug}.md"
    today = _today().isoformat()
    if target.is_file():
        # Merge — append a dated section, leave frontmatter alone.
        existing = target.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        section = (
            f"\n## Update {today}\n\n"
            f"{body.strip()}\n"
        )
        target.write_text(existing + section, encoding="utf-8")
        return
    # Fresh note — full template.
    title = str(fm.get("title") or slug)
    fm_tag = _frontmatter_tag(fm) or folder
    header = (
        "---\n"
        f"title: {title}\n"
        f"tags: [{fm_tag}]\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"last_accessed: {today}\n"
        "access_count: 0\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        f"{body.strip()}\n"
    )
    target.write_text(header, encoding="utf-8")


def _write_conflict(
    vault: pathlib.Path, fm: dict[str, Any], body: str, slug: str
) -> None:
    """Write a conflict-candidate entry to
    ``cortex-memory/conflicts/<YYYY-MM-DD>-<slug>.md``.

    Frontmatter mirrors the format already in use under
    ``cortex-memory/conflicts/`` (e.g. ``2026-05-28-motion-actionable-surface-tier.md``).
    """
    target_dir = _conflicts_dir(vault)
    target_dir.mkdir(parents=True, exist_ok=True)
    today = _today().isoformat()
    target = target_dir / f"{today}-{slug}.md"
    title = str(fm.get("title") or slug)
    header = (
        "---\n"
        f"title: {title}\n"
        "tags: [conflict, conflict-candidate]\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"last_accessed: {today}\n"
        "access_count: 0\n"
        "status: open\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        f"{body.strip()}\n"
    )
    target.write_text(header, encoding="utf-8")


def _write_noise(
    vault: pathlib.Path, fm: dict[str, Any], body: str, filename: str
) -> None:
    """Append a noise marker to today's daily.

    Per the design — noise notes drop to a single daily line
    summarizing what arrived (room, sensor, etc.). The original
    note still goes to ``.consumed/`` so the noise trail can be
    audited later.
    """
    first = _first_body_line(body) or _slug_from_filename(filename)
    line = f"- **{_local_hhmm()} {_abbrev_tz()}** — noise: {first}"
    _append_to_daily(vault, _today(), line)


def _write_unclassified_surface(
    vault: pathlib.Path,
    fm: dict[str, Any],
    body: str,
    filename: str,
) -> pathlib.Path:
    """Write a ``conflict-candidate`` surface for thinking.

    Format follows design §6: ``type: conflict-candidate``,
    ``source: memory-worker``, ``stage: B``, plus a free-form
    detail field carrying the original frontmatter + body so
    thinking can decide what the note should have been.

    Filename convention: ``<YYYY-MM-DDTHH-MM-SS>-stage-b-unclassified-<slug>.md``
    (matches the timestamp prefix already in use under
    ``inner/surface/.handled/``).
    """
    surface_dir = _surface_dir(vault)
    surface_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    slug = _slug_from_filename(filename)
    target = surface_dir / f"{ts}-stage-b-unclassified-{slug}.md"
    detail = (
        f"Original filename: {filename}\n\n"
        f"Frontmatter: {json.dumps(fm, ensure_ascii=False, default=str)}\n\n"
        f"Body:\n{body.strip()}\n"
    )
    header = (
        "---\n"
        "type: conflict-candidate\n"
        "source: memory-worker\n"
        "stage: B\n"
        f"created: {_today().isoformat()}\n"
        f"detail: stage-b-unclassified-{slug}\n"
        "reply_expected: false\n"
        "---\n"
        "\n"
        "# Stage B — unclassified inbox note\n"
        "\n"
        "Memory worker's deterministic routing chain didn't match. "
        "Thinking should decide what this note should have become.\n\n"
        f"{detail}"
    )
    target.write_text(header, encoding="utf-8")
    return target


# ---------- consumption (atomic rename) ----------


def _consume(note: pathlib.Path, vault: pathlib.Path) -> pathlib.Path:
    """Move ``note`` to ``inner/notes/.consumed/<today>/<filename>``.

    Atomic via :func:`os.replace`. The destination directory is
    created on demand. If a file with the same name already exists
    in the consumed directory (partial prior run / duplicate inbox
    arrival), the rename overwrites — POSIX ``rename(2)`` is
    atomic w.r.t. concurrent readers, and our routing has already
    produced the durable vault output, so the older consumed copy
    is no more valuable than the current one.
    """
    dest_dir = _consumed_dir(vault, _today())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / note.name
    os.replace(note, dest)
    return dest


def _fail(note: pathlib.Path, vault: pathlib.Path) -> pathlib.Path:
    """Move ``note`` to ``inner/notes/.failed/<today>/<filename>``.

    Used for malformed notes (frontmatter parse errors) and for
    the unclassified path where leaving the note in the inbox
    would re-emit the same surface every tick.
    """
    dest_dir = _failed_dir(vault, _today())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / note.name
    os.replace(note, dest)
    return dest


# ---------- inbox scan + main entry ----------


def _scan_inbox(vault: pathlib.Path) -> list[pathlib.Path]:
    """Return the list of inbox notes to process this tick.

    Skips dotfiles, ``.consumed/``, ``.failed/``, and any
    subdirectory (per the inbox convention, top-level files only).
    Notes are returned in deterministic name order so a partial
    failure during one tick is recoverable on the next: the same
    note will be at the head of the list.
    """
    inbox = _inbox_dir(vault)
    if not inbox.is_dir():
        return []
    out: list[pathlib.Path] = []
    for child in sorted(inbox.iterdir()):
        if not child.is_file():
            continue
        if child.name.startswith("."):
            continue
        if not child.name.endswith(".md"):
            continue
        out.append(child)
    return out


def _process_one(
    note: pathlib.Path,
    vault: pathlib.Path,
    journal_path: Optional[pathlib.Path],
    report: DrainReport,
    *,
    on_route: Optional[Callable[[Route], None]] = None,
) -> None:
    """Route one inbox note. Caller wraps this in try/except so
    a single bad note doesn't crash the whole drain.

    Order of operations per design §3 + §6:
    1. Parse frontmatter; malformed → move to .failed/, log, return.
    2. Classify; if no rule matches → write surface + move to .failed/.
    3. Otherwise dispatch the writer for the matched route.
    4. Atomic-rename the note to .consumed/ ONLY after the writer
       returns cleanly. A writer raising leaves the note in inbox
       for the next tick to retry.
    """
    try:
        raw = note.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("memory-worker stage_b: failed to read %s: %s", note, exc)
        report.errors += 1
        return

    try:
        fm, body = split_frontmatter(raw)
    except Exception as exc:  # noqa: BLE001 — parser must not crash drain
        logger.warning(
            "memory-worker stage_b: malformed frontmatter in %s: %s — moving to .failed/",
            note,
            exc,
        )
        try:
            _fail(note, vault)
        except OSError as move_exc:
            logger.warning(
                "memory-worker stage_b: failed to move malformed note %s to .failed/: %s",
                note,
                move_exc,
            )
            report.errors += 1
            return
        report.malformed += 1
        return

    route = _classify(fm, body, note.name)
    if route is None:
        # Unclassified — surface for thinking, move note to .failed/.
        try:
            _write_unclassified_surface(vault, fm, body, note.name)
            _fail(note, vault)
        except OSError as exc:
            logger.warning(
                "memory-worker stage_b: unclassified-surface write failed for %s: %s",
                note,
                exc,
            )
            report.errors += 1
            return
        report.unclassified += 1
        if on_route is not None:
            on_route(Route("unclassified", {}))
        return

    # Dispatch. A writer raising leaves the note in the inbox.
    try:
        if route.kind == "activity":
            _write_activity(vault, fm, body, note.name)
            report.routed_activity += 1
        elif route.kind == "event":
            _write_event(vault, fm, body, note.name, route.payload["event_type"])
            report.routed_event += 1
        elif route.kind == "concept":
            _write_concept(
                vault,
                fm,
                body,
                note.name,
                route.payload["folder"],
                route.payload["slug"],
            )
            report.routed_concept += 1
        elif route.kind == "conflict":
            _write_conflict(vault, fm, body, route.payload["slug"])
            report.routed_conflict += 1
        elif route.kind == "noise":
            _write_noise(vault, fm, body, note.name)
            report.routed_noise += 1
        else:  # pragma: no cover — exhaustive above
            raise RuntimeError(f"unknown route kind: {route.kind}")
    except Exception as exc:  # noqa: BLE001 — leave note in inbox for retry
        logger.warning(
            "memory-worker stage_b: writer for kind=%s failed on %s: %s — "
            "leaving in inbox for next tick",
            route.kind,
            note,
            exc,
        )
        report.errors += 1
        return

    # Vault write succeeded — atomic-rename to .consumed/. If the
    # rename fails the writer's output is durable but the note will
    # re-process next tick. The concept writer's merge path is
    # idempotent enough to tolerate this; the activity/event paths
    # would double-write a daily line, which is the price of the
    # design (better than losing the routed output).
    try:
        _consume(note, vault)
    except OSError as exc:
        logger.warning(
            "memory-worker stage_b: consume failed for %s after routing: %s",
            note,
            exc,
        )
        report.errors += 1
        return

    if on_route is not None:
        on_route(route)


def run(
    vault: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
    on_route: Optional[Callable[[Route], None]] = None,
) -> DrainReport:
    """Drain the inbox once. Returns a :class:`DrainReport`.

    Parameters
    ----------
    vault
        Root of ``~/alice-mind/`` (the worker's view of the mind
        directory). All inbox / vault / events.jsonl paths resolve
        relative to this.
    journal_path
        Optional path to the write-ahead journal. Currently
        unused — Stage B writes are individually atomic (file
        append for daily/events, full-file write for concept /
        conflict / surface) so the journal isn't required for
        Stage B correctness. Reserved for Stage C/D, which mutate
        existing notes and need rollback on crash.
    on_route
        Optional callback invoked with each successfully-routed
        :class:`Route`. Used by the wake loop / tests to attach
        observability without threading the emitter through every
        writer.

    Empty inbox → no-op, returns a zeroed report.
    """
    report = DrainReport()
    for note in _scan_inbox(vault):
        report.scanned += 1
        _process_one(note, vault, journal_path, report, on_route=on_route)
    return report
