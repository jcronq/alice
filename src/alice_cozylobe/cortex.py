"""Read-only library over the cozylobe-cortex vault (Phase 1 of #378).

The cozylobe-cortex vault lives at ``~/alice-mind/cozylobe-cortex/`` and
is the home-specific knowledge graph that subsequent phases of the
motion-cortex pipeline will read (Phases 2-4: motion classify, guess
lifecycle, qwen reasoning).

This module is the read API. It walks a vault root, parses YAML
frontmatter + inline typed-weighted edges, and returns dataclasses that
later phases consume. Writes happen via the onboarding CLI
(``scripts/cozylobe_cortex_onboard.py``) and, eventually, via the
classify pipeline — never through this module.

Design references:

* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` §2 —
  directory layout + note schemas.
* ``cortex-memory/research/2026-05-26-vault-tiering-and-typed-edges.md``
  — canonical inline edge syntax ``(VERB:weight)[[target]]`` adopted
  by cozylobe-cortex from day one.

Edge syntax recap:

* Bare ``[[target]]`` → ``Edge(verb="SEE-ALSO", weight=1.0, target=...)``
* ``(VERB)[[target]]`` → verb specified, weight defaults to 1.0
* ``(VERB:0.7)[[target]]`` → both specified
* Targets are ``category/Title`` (e.g. ``rooms/Kitchen``) or just
  ``Title`` (resolved against the current note's category).

The library is intentionally tolerant about unknown verbs and partial
notes — the lint command (``scripts/cozylobe_cortex_lint.py``) is the
strict gate. ``load_vault`` returns whatever it can parse so Phase 2/3
code can keep running on a half-populated vault during onboarding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


__all__ = [
    "DEFAULT_VAULT_ROOT",
    "SUBDIRS",
    "CONTROLLED_VERBS",
    "DEFAULT_VERB",
    "DEFAULT_WEIGHT",
    "Edge",
    "Note",
    "Room",
    "Sensor",
    "Person",
    "Destination",
    "Trajectory",
    "Guess",
    "Vault",
    "load_vault",
    "parse_inline_edges",
    "parse_frontmatter",
    "sensor_room",
    "iter_notes",
]


# ---------------------------------------------------------------------------
# Constants

DEFAULT_VAULT_ROOT = Path.home() / "alice-mind" / "cozylobe-cortex"

# The six categorical subdirs from design §2.1. Order matters only for
# stable iteration in tests / lint output.
SUBDIRS: tuple[str, ...] = (
    "rooms",
    "sensors",
    "people",
    "destinations",
    "trajectories",
    "guesses",
)

# Controlled vocabulary: 11 predicates from the vault-tiering design +
# 5 cozylobe-specific predicates from motion-cortex design §5.2. Unknown
# verbs parse fine (they become Edge.verb verbatim) but the lint command
# warns on them.
CONTROLLED_VERBS: frozenset[str] = frozenset(
    {
        # Vault-tiering canonical 11
        "ELABORATES",
        "REFUTES",
        "DEPENDS-ON",
        "IMPLEMENTS",
        "SUPERSEDES",
        "EXAMPLE-OF",
        "ALTERNATIVE-TO",
        "SEE-ALSO",
        "RAISES",
        "RESOLVES",
        "CONTRADICTS",
        # Cozylobe-cortex extensions
        "IS-ADJACENT-TO",
        "COVERS",
        "OFTEN-VISITS",
        "MOTION-TRIGGERED",
        "CONFIRMED-BY",
    }
)

DEFAULT_VERB = "SEE-ALSO"
DEFAULT_WEIGHT = 1.0


# ---------------------------------------------------------------------------
# Edge parsing

# Match ``(VERB:weight)[[target]]`` or ``(VERB)[[target]]`` or
# bare ``[[target]]``. The annotation group is optional; when present
# it carries an uppercase-hyphen verb and an optional ``:float`` weight.
#
# Examples that match:
#   [[rooms/Kitchen]]
#   (IS-ADJACENT-TO)[[rooms/Living Room]]
#   (IS-ADJACENT-TO:0.9)[[rooms/Living Room]]
#   (DEPENDS-ON:1.0)[[Bedroom]]
#
# Whitespace inside the wikilink target is preserved verbatim.
_EDGE_RE = re.compile(
    r"(?:\((?P<verb>[A-Z][A-Z0-9\-]*)?(?::(?P<weight>\d+(?:\.\d+)?))?\))?"
    r"\[\[(?P<target>[^\]\n]+?)\]\]"
)


@dataclass(frozen=True)
class Edge:
    """A typed weighted edge parsed out of note prose.

    Attributes:
        verb: Predicate naming the relationship. Defaults to
            ``DEFAULT_VERB`` (``"SEE-ALSO"``) when the wikilink has no
            annotation prefix.
        weight: Confidence / strength in ``[0.0, 1.0]``. Defaults to
            ``DEFAULT_WEIGHT`` (``1.0``).
        target: The wikilink target as written, e.g. ``"rooms/Kitchen"``
            or ``"Bedroom"``. ``Edge`` does NOT resolve the target — that
            is the caller's job, since resolution depends on which
            category the source note lives in.
        source_line: Optional 1-based line number where the edge was
            found in the note body. ``None`` when the parser did not
            track lines (e.g. when called on a raw string in tests).
    """

    target: str
    verb: str = DEFAULT_VERB
    weight: float = DEFAULT_WEIGHT
    source_line: Optional[int] = None


def parse_inline_edges(body: str) -> list[Edge]:
    """Extract every ``(VERB:weight)[[target]]`` (and bare ``[[target]]``)
    from ``body``.

    Edges are returned in the order they appear, with ``source_line``
    populated to the 1-based line where the match starts. Bare wikilinks
    default to SEE-ALSO with weight 1.0. The parser is tolerant: an
    unrecognized verb still produces an ``Edge`` (the lint command flags
    it later), and a malformed weight (e.g. ``(FOO:abc)[[bar]]``) simply
    fails to match — the bare ``[[bar]]`` will not be recovered.
    """
    edges: list[Edge] = []
    # Pre-compute line offsets so we can map character offsets → line numbers.
    line_starts = [0]
    for i, ch in enumerate(body):
        if ch == "\n":
            line_starts.append(i + 1)

    def _line_for(pos: int) -> int:
        # Binary search would be tidier but the note bodies are small.
        # +1 to go from 0-based index to 1-based line numbers.
        line = 1
        for start in line_starts:
            if start > pos:
                break
            line = line_starts.index(start) + 1
        return line

    for match in _EDGE_RE.finditer(body):
        verb = match.group("verb") or DEFAULT_VERB
        weight_str = match.group("weight")
        weight = float(weight_str) if weight_str is not None else DEFAULT_WEIGHT
        target = match.group("target").strip()
        edges.append(
            Edge(
                target=target,
                verb=verb,
                weight=weight,
                source_line=_line_for(match.start()),
            )
        )
    return edges


# ---------------------------------------------------------------------------
# Frontmatter parsing
#
# We accept either a YAML-libraryless flat parser (``key: value`` per line,
# no nested structures) or full ``yaml.safe_load`` if PyYAML is available.
# The Phase 1 schemas only use scalars + comma-separated lists, so the
# fallback parser is sufficient for everything the onboarding CLI writes.
# Importing yaml lazily keeps the cortex module usable without the dep
# during early bootstrap.

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a note.

    Accepts a missing frontmatter block and returns an empty dict +
    the full text as the body. Tolerant of trailing whitespace on the
    delimiter lines.
    """
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, text
    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    fm = _parse_simple_yaml(fm_text)
    return fm, body


def _parse_simple_yaml(fm_text: str) -> dict:
    """Parse the YAML-lite frontmatter format the onboarding CLI writes.

    Recognized shapes (one per line):

    * ``key: scalar`` — string scalar
    * ``key: [a, b, c]`` — flow-style list; commas split, surrounding
      whitespace stripped
    * ``key:`` followed by ``- item`` block lines — block-style list
    * Lines beginning with ``#`` are ignored.

    PyYAML would be more robust but it's a runtime dep we'd rather not
    pull into the lint path. If PyYAML is importable we use it; the
    simple parser is a fallback.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        yaml = None  # type: ignore[assignment]
    if yaml is not None:
        try:
            loaded = yaml.safe_load(fm_text)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            # Fall through to the simple parser so a half-broken
            # frontmatter still gets best-effort key extraction.
            pass

    out: dict = {}
    current_key: Optional[str] = None
    current_list: Optional[list] = None
    for raw in fm_text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if current_list is not None and line.lstrip().startswith("- "):
            current_list.append(line.lstrip()[2:].strip())
            continue
        # New top-level key terminates any open block list.
        if current_list is not None and current_key is not None:
            out[current_key] = current_list
            current_list = None
            current_key = None
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            current_key = key
            current_list = []
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                out[key] = []
            else:
                out[key] = [item.strip() for item in inner.split(",")]
            continue
        out[key] = value
    if current_list is not None and current_key is not None:
        out[current_key] = current_list
    return out


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Public entry point for frontmatter parsing. Returns ``(fm, body)``."""
    return _split_frontmatter(text)


# ---------------------------------------------------------------------------
# Note dataclasses

@dataclass(frozen=True)
class Note:
    """Base note — everything in the vault has these attributes."""

    slug: str  # ``category/Title`` form, e.g. ``rooms/Kitchen``
    category: str  # one of SUBDIRS
    title: str
    path: Path
    frontmatter: dict
    body: str
    edges: tuple[Edge, ...]


@dataclass(frozen=True)
class Room(Note):
    adjacent: tuple[str, ...] = ()  # wikilink targets as written
    sensors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sensor(Note):
    room: Optional[str] = None  # wikilink target
    kind: Optional[str] = None
    install_date: Optional[str] = None


@dataclass(frozen=True)
class Person(Note):
    time_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Destination(Note):
    person: Optional[str] = None
    room: Optional[str] = None
    time_window: Optional[str] = None


@dataclass(frozen=True)
class Trajectory(Note):
    room_sequence: tuple[str, ...] = ()
    weight: Optional[float] = None


@dataclass(frozen=True)
class Guess(Note):
    person: Optional[str] = None
    room: Optional[str] = None
    confidence: Optional[float] = None
    status: Optional[str] = None  # pending | confirmed | refuted


# ---------------------------------------------------------------------------
# Vault container

@dataclass
class Vault:
    """Parsed cozylobe-cortex vault.

    The accessors (``rooms``, ``sensors``, ``people``, …) are dicts keyed
    by slug — ``"rooms/Kitchen"`` etc. Use :meth:`get` for ergonomic
    lookups by either slug or bare title (e.g. ``vault.get("Kitchen",
    category="rooms")``).
    """

    root: Path
    rooms: dict[str, Room] = field(default_factory=dict)
    sensors: dict[str, Sensor] = field(default_factory=dict)
    people: dict[str, Person] = field(default_factory=dict)
    destinations: dict[str, Destination] = field(default_factory=dict)
    trajectories: dict[str, Trajectory] = field(default_factory=dict)
    guesses: dict[str, Guess] = field(default_factory=dict)

    def all_notes(self) -> list[Note]:
        out: list[Note] = []
        out.extend(self.rooms.values())
        out.extend(self.sensors.values())
        out.extend(self.people.values())
        out.extend(self.destinations.values())
        out.extend(self.trajectories.values())
        out.extend(self.guesses.values())
        return out

    def get(self, key: str, *, category: Optional[str] = None) -> Optional[Note]:
        """Resolve a wikilink target to a Note, or None if missing.

        Accepts ``"rooms/Kitchen"`` (category-prefixed) or ``"Kitchen"``
        (bare title — requires ``category=``). Lookup is exact-match;
        we do not normalize case or whitespace.
        """
        if "/" in key:
            cat, _, title = key.partition("/")
            return self._by_category(cat).get(f"{cat}/{title}")
        if category is None:
            # Search every category. First hit wins; ambiguous, but
            # convenient for tests.
            for cat in SUBDIRS:
                hit = self._by_category(cat).get(f"{cat}/{key}")
                if hit is not None:
                    return hit
            return None
        return self._by_category(category).get(f"{category}/{key}")

    def _by_category(self, cat: str) -> dict[str, Note]:
        return {
            "rooms": self.rooms,
            "sensors": self.sensors,
            "people": self.people,
            "destinations": self.destinations,
            "trajectories": self.trajectories,
            "guesses": self.guesses,
        }.get(cat, {})


# ---------------------------------------------------------------------------
# Loading

_WIKILINK_TARGET_RE = re.compile(r"\[\[(?P<target>[^\]\n]+?)\]\]")


def _frontmatter_targets(value: object) -> tuple[str, ...]:
    """Extract bare ``[[target]]`` strings from a frontmatter value.

    Frontmatter examples in design §2.2:

        adjacent: [[rooms/Living Room]], [[rooms/Dining Room]]
        sensors: [[sensors/hue_kitchen_motion]]

    The simple-YAML parser passes these through as strings (or list of
    strings); strip the brackets and return the targets in order.
    """
    if value is None:
        return ()
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = [str(value)]
    out: list[str] = []
    for item in items:
        # Strip leading/trailing whitespace + any standalone brackets.
        s = item.strip()
        matches = _WIKILINK_TARGET_RE.findall(s)
        if matches:
            out.extend(m.strip() for m in matches)
        elif s and not s.startswith("[["):
            # A bare value (no brackets) — keep verbatim. Onboarding
            # writes wikilink lists but operators may hand-edit.
            out.append(s)
    return tuple(out)


def _build_note(
    category: str, path: Path, text: str
) -> Optional[Note]:
    fm, body = parse_frontmatter(text)
    title = str(fm.get("title", path.stem))
    slug = f"{category}/{title}"
    edges = tuple(parse_inline_edges(body))

    if category == "rooms":
        return Room(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            adjacent=_frontmatter_targets(fm.get("adjacent")),
            sensors=_frontmatter_targets(fm.get("sensors")),
        )
    if category == "sensors":
        return Sensor(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            room=(_frontmatter_targets(fm.get("room")) or (None,))[0],
            kind=fm.get("kind"),
            install_date=fm.get("install_date"),
        )
    if category == "people":
        return Person(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            time_patterns=_frontmatter_targets(fm.get("time_patterns")),
        )
    if category == "destinations":
        return Destination(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            person=(_frontmatter_targets(fm.get("person")) or (None,))[0],
            room=(_frontmatter_targets(fm.get("room")) or (None,))[0],
            time_window=fm.get("time_window"),
        )
    if category == "trajectories":
        weight_raw = fm.get("weight")
        try:
            weight_val = float(str(weight_raw).split()[0]) if weight_raw else None
        except (TypeError, ValueError):
            weight_val = None
        return Trajectory(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            room_sequence=_frontmatter_targets(fm.get("room_sequence")),
            weight=weight_val,
        )
    if category == "guesses":
        try:
            conf_val = float(fm["confidence"]) if "confidence" in fm else None
        except (TypeError, ValueError):
            conf_val = None
        return Guess(
            slug=slug,
            category=category,
            title=title,
            path=path,
            frontmatter=fm,
            body=body,
            edges=edges,
            person=(_frontmatter_targets(fm.get("person")) or (None,))[0],
            room=(_frontmatter_targets(fm.get("room")) or (None,))[0],
            confidence=conf_val,
            status=fm.get("status"),
        )
    return None


def iter_notes(root: Path) -> Iterable[tuple[str, Path]]:
    """Yield ``(category, path)`` for every ``*.md`` note under the vault.

    Skips ``README.md`` files (those are schema docs, not notes) and any
    file that doesn't sit directly under one of the six known
    categorical subdirs.
    """
    for cat in SUBDIRS:
        subdir = root / cat
        if not subdir.is_dir():
            continue
        for md in sorted(subdir.glob("*.md")):
            if md.name.lower() == "readme.md":
                continue
            yield cat, md


def load_vault(root: Path) -> Vault:
    """Walk ``root`` and parse every note in the six categorical subdirs.

    Returns an empty :class:`Vault` if ``root`` does not exist. This is
    intentional — Phases 2/3 may call ``load_vault`` before the operator
    has run the onboarding CLI, and we don't want that to crash the
    daemon. The lint command is the strict gate; the read API is
    tolerant.
    """
    vault = Vault(root=root)
    if not root.is_dir():
        return vault
    for cat, path in iter_notes(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        note = _build_note(cat, path, text)
        if note is None:
            continue
        bucket = vault._by_category(cat)
        bucket[note.slug] = note  # type: ignore[assignment]
    return vault


# ---------------------------------------------------------------------------
# Convenience helpers

def sensor_room(vault: Vault, sensor_slug_or_title: str) -> Optional[Room]:
    """Resolve a sensor → the room it covers.

    Accepts either a full slug (``sensors/hue_kitchen_motion``) or a
    bare title (``hue_kitchen_motion``). Returns ``None`` if the sensor
    is unknown or its ``room:`` frontmatter is missing / dangling.
    """
    sensor: Optional[Sensor]
    if "/" in sensor_slug_or_title:
        sensor = vault.sensors.get(sensor_slug_or_title)
    else:
        sensor = vault.sensors.get(f"sensors/{sensor_slug_or_title}")
    if sensor is None or sensor.room is None:
        return None
    target = sensor.room
    # Accept "rooms/Kitchen" or bare "Kitchen".
    if "/" in target:
        return vault.rooms.get(target)
    return vault.rooms.get(f"rooms/{target}")
