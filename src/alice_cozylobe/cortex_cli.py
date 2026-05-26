"""Command-line entry points for cozylobe-cortex Phase 1 (#378).

Houses the onboarding and lint logic so it's importable both as a
package (the ``[project.scripts]`` console entry points wire these in)
and as the targets of the thin shim scripts under ``scripts/``. Tests
import this module directly rather than importlib-loading the scripts.

* :func:`onboard_main` — interactive vault seeding.
* :func:`lint_main` — schema validation.

Module-level helpers (``materialize``, ``ensure_subdirs``,
``OnboardingAnswers``, ``lint_vault``, ``LintIssue``) are part of the
public surface for tests and for thinking-side grooming.

Design references:

* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` —
  §2.1 directory layout, §2.2 schemas, §4.1 bootstrap.
* ``src/alice_cozylobe/cortex.py`` — the read API the linter and tests
  consume.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional

from .cortex import (
    CONTROLLED_VERBS,
    DEFAULT_VAULT_ROOT,
    SUBDIRS,
    Vault,
    load_vault,
    parse_inline_edges,
)


__all__ = [
    # Onboarding
    "ANSWERS_SCHEMA",
    "DEFAULT_COZYHEM_BASE_URL",
    "TEMPLATES_ROOT",
    "OnboardingAnswers",
    "fetch_cozyhem_sensors",
    "load_sensors_from_file",
    "interactive_answers",
    "ensure_subdirs",
    "write_room_note",
    "write_sensor_note",
    "write_person_note",
    "materialize",
    "vault_has_notes",
    "answers_from_json",
    "onboard_main",
    # Lint
    "LintIssue",
    "lint_vault",
    "lint_main",
]


# ---------------------------------------------------------------------------
# Shared constants

DEFAULT_COZYHEM_BASE_URL = "http://localhost:8000"

# Templates live in the repo root, one level above ``src/``. When the
# package is installed via wheel, templates aren't bundled (they're a
# scaffold copied during onboarding, not runtime data). We try the
# checkout layout first; if absent, the script copies nothing and
# the vault still works — just without the schema READMEs.
def _templates_root() -> Path:
    # src/alice_cozylobe/cortex_cli.py → repo root is ../../..
    return Path(__file__).resolve().parents[2] / "templates" / "cozylobe-cortex"


TEMPLATES_ROOT = _templates_root()


# CozyHem entity-id filter: binary_sensor.*motion*
_MOTION_SENSOR_RE = re.compile(r"^binary_sensor\..*motion.*$", re.IGNORECASE)


ANSWERS_SCHEMA = """\
Test/automation answers file format (JSON):

{
  "floors": {
    "1": ["Kitchen", "Living Room", "Dining Room", "Office", "Bedroom"],
    "2": ["Master Bedroom", "Master Bathroom"]
  },
  "adjacency": {
    "Kitchen": ["Living Room", "Dining Room"],
    "Living Room": ["Kitchen", "Dining Room"]
  },
  "sensor_to_room": {
    "binary_sensor.hue_kitchen_motion": "Kitchen"
  },
  "extra_people": [
    {"name": "Sarah", "role": "visitor"}
  ]
}

Keys may be absent — defaults are a one-floor layout, empty adjacency,
no sensors, and the canonical Jason/Katie/Mike/unknown set.
"""


# ---------------------------------------------------------------------------
# Onboarding data shapes

@dataclass
class OnboardingAnswers:
    """Materialized answers from interactive prompts or --answers file."""

    floors: dict[str, list[str]] = field(default_factory=dict)
    adjacency: dict[str, list[str]] = field(default_factory=dict)
    sensor_to_room: dict[str, str] = field(default_factory=dict)
    extra_people: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CozyHem entity fetch

def fetch_cozyhem_sensors(
    base_url: str,
    *,
    timeout: float = 5.0,
) -> list[dict]:
    """Return CozyHem's ``binary_sensor.*motion*`` entities.

    Network failures raise — the caller decides whether to fall back to
    ``--sensors-from`` or abort. Returns a list of dicts as CozyHem
    returns them; at minimum ``entity_id``, usually also
    ``friendly_name`` / ``state`` / ``last_changed``.
    """
    import urllib.request

    url = f"{base_url.rstrip('/')}/api/v1/entities"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        data = json.load(resp)
    entries = data if isinstance(data, list) else data.get("entities", [])
    return [
        e
        for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("entity_id"), str)
        and _MOTION_SENSOR_RE.match(e["entity_id"])
    ]


def load_sensors_from_file(path: Path) -> list[dict]:
    """Load a JSON sensors fixture for offline / test runs."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else data.get("entities", [])
    return [
        e
        for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("entity_id"), str)
        and _MOTION_SENSOR_RE.match(e["entity_id"])
    ]


# ---------------------------------------------------------------------------
# Interactive prompts (replaceable for tests)

PromptFn = Callable[[str], str]


def _default_prompt(message: str) -> str:
    return input(message)


def _confirm(prompt: PromptFn, message: str, *, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    raw = prompt(message + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def interactive_answers(
    *,
    sensors: list[dict],
    prompt: PromptFn = _default_prompt,
) -> OnboardingAnswers:
    """Drive the operator through the five-step intake."""
    answers = OnboardingAnswers()

    floors_raw = prompt(
        "How many floors does the home have? (1-3, default 1) "
    ).strip() or "1"
    try:
        n_floors = max(1, min(3, int(floors_raw)))
    except ValueError:
        n_floors = 1

    for f in range(1, n_floors + 1):
        line = prompt(
            f"Floor {f}: room names, comma-separated "
            "(e.g. Kitchen, Living Room, Office): "
        ).strip()
        if line:
            rooms = [r.strip() for r in line.split(",") if r.strip()]
            answers.floors[str(f)] = rooms

    all_rooms = [r for rooms in answers.floors.values() for r in rooms]

    for room in all_rooms:
        line = prompt(
            f"  Rooms adjacent to {room!r}, comma-separated "
            "(blank for none): "
        ).strip()
        if line:
            adj = [r.strip() for r in line.split(",") if r.strip()]
            answers.adjacency[room] = adj

    if sensors:
        prompt(
            f"\nFound {len(sensors)} motion sensor(s) in CozyHem. Map each "
            "to a room (or leave blank to skip).\n"
            "Press Enter to continue: "
        )
        for sensor in sensors:
            entity_id = sensor["entity_id"]
            friendly = sensor.get("friendly_name", "")
            label = f"{entity_id}" + (f" ({friendly})" if friendly else "")
            line = prompt(f"  {label} → which room? ").strip()
            if line:
                answers.sensor_to_room[entity_id] = line

    if not _confirm(
        prompt,
        "\nResidents are Jason and Katie; visitor Mike is allowed in. "
        "Confirm?",
        default=True,
    ):
        prompt(
            "Edit the generated people/ notes by hand after onboarding. "
            "Press Enter to continue: "
        )
    extra = prompt(
        "Any other regular visitors? Comma-separated names (blank for none): "
    ).strip()
    if extra:
        for name in [n.strip() for n in extra.split(",") if n.strip()]:
            answers.extra_people.append({"name": name, "role": "visitor"})

    return answers


# ---------------------------------------------------------------------------
# Vault scaffolding

def _today_iso() -> str:
    return date.today().isoformat()


def _frontmatter_list(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return " []"
    return "\n" + "\n".join(f"  - \"{item}\"" for item in items)


def _wikilink(category: str, title: str) -> str:
    return f"[[{category}/{title}]]"


def _slugify(title: str) -> str:
    """Filesystem-safe basename — preserve spaces, strip path separators."""
    return title.replace("/", "-").replace("\\", "-").strip()


def ensure_subdirs(root: Path) -> None:
    """Create the six categorical subdirs + copy schema READMEs from
    the in-repo templates if present."""
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    src_root_readme = TEMPLATES_ROOT / "README.md"
    if src_root_readme.exists() and not (root / "README.md").exists():
        shutil.copy(src_root_readme, root / "README.md")
    for sub in SUBDIRS:
        src = TEMPLATES_ROOT / sub / "README.md"
        dst = root / sub / "README.md"
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)


def write_room_note(
    root: Path,
    name: str,
    *,
    floor: Optional[int],
    adjacent: list[str],
    sensors: list[str],
) -> Path:
    path = root / "rooms" / f"{_slugify(name)}.md"
    lines = [
        "---",
        f"title: {name}",
        "tags: [room, cozylobe-cortex]",
        f"created: {_today_iso()}",
        f"updated: {_today_iso()}",
    ]
    if floor is not None:
        lines.append(f"floor: {floor}")
    lines.append("adjacent:" + _frontmatter_list(
        _wikilink("rooms", a) for a in adjacent
    ))
    lines.append("sensors:" + _frontmatter_list(
        _wikilink("sensors", s) for s in sensors
    ))
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    if adjacent:
        adj_phrase = ", ".join(
            f"(IS-ADJACENT-TO:1.0){_wikilink('rooms', a)}" for a in adjacent
        )
        lines.append(f"Adjacent to {adj_phrase}.")
    if sensors:
        sensor_phrase = ", ".join(
            f"(COVERS:1.0){_wikilink('sensors', s)}" for s in sensors
        )
        lines.append("")
        lines.append(f"Motion coverage: {sensor_phrase}.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_sensor_note(
    root: Path,
    entity_id: str,
    *,
    room: str,
    kind: str = "PIR",
    install_date: Optional[str] = None,
) -> Path:
    path = root / "sensors" / f"{_slugify(entity_id)}.md"
    install = install_date or _today_iso()
    lines = [
        "---",
        f"title: {entity_id}",
        "tags: [sensor, motion, cozylobe-cortex]",
        f"created: {_today_iso()}",
        f"updated: {_today_iso()}",
        f"entity_id: {entity_id}",
        f"kind: {kind}",
        f"room: \"{_wikilink('rooms', room)}\"",
        f"install_date: {install}",
        "---",
        "",
        f"# {entity_id}",
        "",
        f"Motion sensor (COVERS:1.0){_wikilink('rooms', room)}. "
        f"Installed {install}.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_person_note(
    root: Path,
    name: str,
    *,
    role: str = "resident",
) -> Path:
    path = root / "people" / f"{_slugify(name)}.md"
    if role == "resident":
        tags = "[person, resident, cozylobe-cortex]"
    else:
        tags = f"[person, {role}, cozylobe-cortex]"
    lines = [
        "---",
        f"title: {name}",
        f"tags: {tags}",
        f"created: {_today_iso()}",
        f"updated: {_today_iso()}",
        f"role: {role}",
        "time_patterns: []",
        "---",
        "",
        f"# {name}",
        "",
        f"{role.capitalize()}. Time-of-day patterns will accumulate "
        "in destinations/ as Phase 3's guess lifecycle observes them.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Orchestration

CANONICAL_PEOPLE = [
    {"name": "Jason", "role": "resident"},
    {"name": "Katie", "role": "resident"},
    {"name": "Mike", "role": "visitor"},
    {"name": "unknown", "role": "unknown"},
]


def vault_has_notes(root: Path) -> bool:
    """True if any subdir already contains a non-README note."""
    if not root.is_dir():
        return False
    for sub in SUBDIRS:
        subdir = root / sub
        if not subdir.is_dir():
            continue
        for md in subdir.glob("*.md"):
            if md.name.lower() != "readme.md":
                return True
    return False


def materialize(
    root: Path,
    answers: OnboardingAnswers,
) -> dict[str, list[Path]]:
    """Write the vault from the given answers.

    Returns a per-category listing of paths created or updated.
    Idempotent on identical inputs (the write helpers regenerate the
    same bytes for the same arguments — only the ``updated:`` field
    might drift if the date rolls over mid-run, which is acceptable).
    """
    ensure_subdirs(root)
    written: dict[str, list[Path]] = {sub: [] for sub in SUBDIRS}

    room_to_sensors: dict[str, list[str]] = {}
    for entity_id, room in answers.sensor_to_room.items():
        room_to_sensors.setdefault(room, []).append(entity_id)

    floor_of: dict[str, int] = {}
    for floor_num, rooms in answers.floors.items():
        try:
            f = int(floor_num)
        except ValueError:
            continue
        for r in rooms:
            floor_of[r] = f

    for room in floor_of:
        path = write_room_note(
            root,
            room,
            floor=floor_of.get(room),
            adjacent=answers.adjacency.get(room, []),
            sensors=room_to_sensors.get(room, []),
        )
        written["rooms"].append(path)

    for entity_id, room in answers.sensor_to_room.items():
        path = write_sensor_note(root, entity_id, room=room)
        written["sensors"].append(path)

    for person in CANONICAL_PEOPLE:
        path = write_person_note(root, person["name"], role=person["role"])
        written["people"].append(path)
    for person in answers.extra_people:
        path = write_person_note(
            root, person["name"], role=person.get("role", "visitor")
        )
        written["people"].append(path)

    return written


def answers_from_json(data: dict) -> OnboardingAnswers:
    """Parse a ``--answers`` JSON blob into an :class:`OnboardingAnswers`."""
    return OnboardingAnswers(
        floors={
            str(k): list(v) for k, v in (data.get("floors") or {}).items()
        },
        adjacency={
            str(k): list(v) for k, v in (data.get("adjacency") or {}).items()
        },
        sensor_to_room={
            str(k): str(v)
            for k, v in (data.get("sensor_to_room") or {}).items()
        },
        extra_people=list(data.get("extra_people") or []),
    )


# ---------------------------------------------------------------------------
# Onboarding CLI

def _onboard_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cozylobe-cortex-onboard",
        description="Interactive onboarding for the cozylobe-cortex vault.",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help=f"Vault root (default: {DEFAULT_VAULT_ROOT})",
    )
    parser.add_argument(
        "--cozyhem-base-url",
        default=DEFAULT_COZYHEM_BASE_URL,
        help=f"CozyHem base URL for sensor inventory (default: "
        f"{DEFAULT_COZYHEM_BASE_URL})",
    )
    parser.add_argument(
        "--sensors-from",
        type=Path,
        help="JSON file with CozyHem entity list (offline / test mode). "
        "Skips the live HTTP fetch.",
    )
    parser.add_argument(
        "--answers",
        type=Path,
        help="JSON file with pre-canned answers (non-interactive mode).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an already-populated vault. Without this flag, "
        "the script refuses to clobber existing notes.",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the --answers JSON schema and exit.",
    )
    return parser


def onboard_main(argv: Optional[list[str]] = None) -> int:
    parser = _onboard_parser()
    args = parser.parse_args(argv)

    if args.print_schema:
        print(ANSWERS_SCHEMA)
        return 0

    if vault_has_notes(args.vault) and not args.force:
        print(
            f"cozylobe-cortex at {args.vault} already has notes — refusing "
            "to overwrite. Re-run with --force to clobber.",
            file=sys.stderr,
        )
        return 1

    if args.sensors_from is not None:
        sensors = load_sensors_from_file(args.sensors_from)
    else:
        try:
            sensors = fetch_cozyhem_sensors(args.cozyhem_base_url)
        except Exception as exc:
            print(
                f"warning: could not fetch sensors from "
                f"{args.cozyhem_base_url}: {exc}. Continuing with empty "
                "sensor list — pass --sensors-from FILE to seed manually.",
                file=sys.stderr,
            )
            sensors = []

    if args.answers is not None:
        data = json.loads(args.answers.read_text(encoding="utf-8"))
        answers = answers_from_json(data)
    else:
        answers = interactive_answers(sensors=sensors)

    written = materialize(args.vault, answers)
    total = sum(len(v) for v in written.values())
    print(
        f"cozylobe-cortex seeded at {args.vault}: {total} note(s) written"
    )
    for sub, paths in written.items():
        if paths:
            print(f"  {sub}/: {len(paths)}")
    return 0


# ---------------------------------------------------------------------------
# Lint

# Detect malformed annotations: ``(...)`` immediately followed by
# ``[[...]]`` where the inner content does NOT match the canonical
# ``VERB(:weight)`` form. The main edge regex tolerates these by simply
# not matching; this second pass surfaces them as errors.
_MALFORMED_ANNOTATION_RE = re.compile(r"\(([^)\n]*)\)\[\[")
_GOOD_ANNOTATION_BODY_RE = re.compile(
    r"^[A-Z][A-Z0-9\-]*(?::\d+(?:\.\d+)?)?$"
)


@dataclass(frozen=True)
class LintIssue:
    severity: str  # "error" | "warning"
    path: Optional[Path]
    line: Optional[int]
    message: str


def _resolve_target(vault: Vault, target: str) -> Optional[str]:
    note = vault.get(target)
    if note is not None:
        return note.slug
    if "/" in target:
        _, _, title = target.partition("/")
        note = vault.get(title)
        if note is not None:
            return note.slug
    return None


def lint_vault(root: Path) -> list[LintIssue]:
    """Return every issue found in the vault. Empty list = clean."""
    issues: list[LintIssue] = []

    if not root.is_dir():
        issues.append(
            LintIssue(
                severity="error",
                path=None,
                line=None,
                message=f"vault root does not exist: {root}",
            )
        )
        return issues

    # Orphan-notes check
    for md in root.rglob("*.md"):
        if md.name.lower() == "readme.md":
            continue
        try:
            rel = md.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2 or parts[0] not in SUBDIRS:
            issues.append(
                LintIssue(
                    severity="error",
                    path=md,
                    line=None,
                    message=(
                        "orphan note: not in one of "
                        f"{sorted(SUBDIRS)} subdirs"
                    ),
                )
            )

    vault = load_vault(root)

    # Sensor → room references resolve
    for sensor in vault.sensors.values():
        if sensor.room is None:
            issues.append(
                LintIssue(
                    severity="error",
                    path=sensor.path,
                    line=None,
                    message=(
                        f"sensor {sensor.title!r}: missing required "
                        "room: frontmatter"
                    ),
                )
            )
            continue
        if _resolve_target(vault, sensor.room) is None:
            issues.append(
                LintIssue(
                    severity="error",
                    path=sensor.path,
                    line=None,
                    message=(
                        f"sensor {sensor.title!r}: room: "
                        f"{sensor.room!r} does not resolve to an "
                        "existing room"
                    ),
                )
            )

    # Room adjacency references resolve
    for room in vault.rooms.values():
        for adj in room.adjacent:
            if _resolve_target(vault, adj) is None:
                issues.append(
                    LintIssue(
                        severity="error",
                        path=room.path,
                        line=None,
                        message=(
                            f"room {room.title!r}: adjacent: {adj!r} "
                            "does not resolve to an existing room"
                        ),
                    )
                )

    # Inline edge syntax — structural parse + controlled-verb warn
    for note in vault.all_notes():
        for line_no, raw in enumerate(note.body.splitlines(), start=1):
            for match in _MALFORMED_ANNOTATION_RE.finditer(raw):
                body = match.group(1)
                if not _GOOD_ANNOTATION_BODY_RE.match(body):
                    issues.append(
                        LintIssue(
                            severity="error",
                            path=note.path,
                            line=line_no,
                            message=(
                                f"malformed edge annotation: "
                                f"({body})[[…]]; expected "
                                "(VERB:weight)[[target]]"
                            ),
                        )
                    )
        for edge in parse_inline_edges(note.body):
            if edge.verb not in CONTROLLED_VERBS:
                issues.append(
                    LintIssue(
                        severity="warning",
                        path=note.path,
                        line=edge.source_line,
                        message=(
                            f"unknown verb {edge.verb!r} on edge → "
                            f"{edge.target!r}; controlled vocabulary: "
                            f"{sorted(CONTROLLED_VERBS)}"
                        ),
                    )
                )

    return issues


def _format(issue: LintIssue) -> str:
    parts = [issue.severity.upper()]
    if issue.path is not None:
        suffix = f":{issue.line}" if issue.line is not None else ""
        parts.append(f"{issue.path}{suffix}")
    parts.append(issue.message)
    return " — ".join(parts)


def _lint_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cozylobe-cortex-lint",
        description="Lint the cozylobe-cortex vault for schema violations.",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help=f"Vault root (default: {DEFAULT_VAULT_ROOT})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print 'clean' on success.",
    )
    return parser


def lint_main(argv: Optional[list[str]] = None) -> int:
    parser = _lint_parser()
    args = parser.parse_args(argv)

    try:
        issues = lint_vault(args.vault)
    except Exception as exc:
        print(f"lint failed: {exc}", file=sys.stderr)
        return 2

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in warnings:
        print(_format(issue), file=sys.stderr)
    for issue in errors:
        print(_format(issue), file=sys.stderr)

    if errors:
        return 1

    if args.verbose:
        print(
            f"cozylobe-cortex at {args.vault}: clean "
            f"({len(warnings)} warning(s))"
        )
    return 0
