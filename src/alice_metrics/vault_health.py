"""Stable vault-health metrics — pure functions, no side effects.

Replaces three LLM-generated bash blocks in
``alice_thinking.prompts.active`` (and the wider thinking prompt set)
that drifted between wakes and produced order-of-magnitude wrong
numbers:

- ``orphan_notes`` — same-directory wikilink scan missed cross-directory
  references and ignored aliases. Reported ~942 orphans of 1002 notes
  when the real number was ~11 (all dailies).
- ``broken_wikilinks`` — regex over-matched code spans / fences / HTML
  comments, so the count exploded unpredictably (5422 vs 2 between two
  consecutive wakes).
- ``wake_type_distribution`` — ``find -newermt`` window only scanned
  yesterday's thoughts dir and missed wakes that wrote into today's
  dir after midnight.

The fourth bug (phase1-check delta mode) is fixed in
``inner/state/phase1-check-script.py`` directly; this module only
contributes a regression test that locks in the legacy-mode marker.

Algorithms here are intentionally exposed as pure functions: they
take filesystem paths in, return values out, and never write. The
``__main__`` entrypoint glues them together into the JSON shape the
``vault_health`` event consumes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from alice_indexer.yaml_lite import extract_wikilinks, split_frontmatter

# ---------------------------------------------------------------------------
# Constants

# Files that aren't real notes — exclude from totals, orphan checks, and
# resolution maps. ``unresolved.md`` is the indexer's bookkeeping page;
# ``index.md`` / ``README.md`` are vault scaffolding.
EXCLUDED_NAMES = frozenset({"index.md", "README.md", "unresolved.md"})

# HTML comments can hide wikilink-shaped tokens (``<!-- [[foo]] -->``)
# that shouldn't count as broken links. ``yaml_lite._strip_code``
# already strips fenced blocks and inline code spans; HTML comments are
# the third hiding place we have to handle here.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


# ---------------------------------------------------------------------------
# Helpers


def _strip_html_comments(body: str) -> str:
    """Remove ``<!-- ... -->`` blocks (multi-line) from a markdown body.

    ``yaml_lite.extract_wikilinks`` already strips fenced code blocks
    and inline code spans, but HTML comments are the third place a
    ``[[foo]]`` token can hide. The metrics here are stricter than the
    indexer (we want zero false positives in the broken-link count),
    so we strip comments before calling ``extract_wikilinks``.
    """
    return _HTML_COMMENT_RE.sub("", body)


def _iter_notes(vault_dir: Path) -> list[Path]:
    """Every ``*.md`` under ``vault_dir`` excluding scaffolding files
    and dotfile directories. Ordered for deterministic output."""
    if not vault_dir.exists():
        return []
    out: list[Path] = []
    for md in vault_dir.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(vault_dir).parts):
            continue
        if md.name in EXCLUDED_NAMES:
            continue
        out.append(md)
    out.sort()
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _normalize_target(raw: str) -> str:
    """Strip section anchors and folder qualifiers; lowercase for alias matching."""
    target = raw.strip()
    if "#" in target:
        target = target.split("#", 1)[0].strip()
    # Folder-qualified targets (``research/foo``) — keep the basename
    # since vaults usually address by basename.
    if "/" in target:
        target = target.rsplit("/", 1)[-1]
    return target


def _aliases_from_fm(fm: dict[str, Any]) -> list[str]:
    """Frontmatter ``aliases:`` is either a string or a YAML-ish list."""
    raw = fm.get("aliases") or []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(a) for a in raw if a]
    return []


def _slug_from_fm(fm: dict[str, Any], filename_stem: str) -> str:
    slug = fm.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return filename_stem


# ---------------------------------------------------------------------------
# count_total_notes


def count_total_notes(vault_dir: Path) -> int:
    """Recursively count ``*.md`` under ``vault_dir`` excluding
    ``index.md`` / ``README.md`` / ``unresolved.md``."""
    return len(_iter_notes(vault_dir))


# ---------------------------------------------------------------------------
# Resolution maps + wikilink extraction


def _build_resolution_index(
    vault_dir: Path,
) -> tuple[dict[str, Path], dict[str, list[str]], dict[str, str]]:
    """Return ``(by_slug, slugs_to_aliases, alias_lower_to_slug)``.

    ``by_slug`` maps slug → file path. Slugs come from frontmatter
    ``slug:`` if present, otherwise the filename stem. We also register
    the bare filename stem as a slug if it differs (so ``[[bar]]``
    resolves whether the target file is named ``bar.md`` or has
    ``slug: bar``).

    ``alias_lower_to_slug`` maps lowercased alias → slug for
    case-insensitive alias resolution.
    """
    by_slug: dict[str, Path] = {}
    slugs_to_aliases: dict[str, list[str]] = {}
    alias_lower_to_slug: dict[str, str] = {}

    for md in _iter_notes(vault_dir):
        text = _read_text(md)
        fm, _body = split_frontmatter(text)
        slug = _slug_from_fm(fm, md.stem)
        # Register the explicit slug.
        by_slug.setdefault(slug, md)
        # Always register the bare filename stem too — a wikilink may
        # address either form.
        by_slug.setdefault(md.stem, md)
        aliases = _aliases_from_fm(fm)
        slugs_to_aliases[slug] = aliases
        for alias in aliases:
            alias_lower_to_slug.setdefault(alias.lower(), slug)
    return by_slug, slugs_to_aliases, alias_lower_to_slug


def _extract_targets(body: str) -> list[str]:
    """Extract ``[[target]]`` / ``[[target|alias]]`` wikilink targets,
    excluding code spans / fences / HTML comments. Returns normalized
    targets (anchors stripped, basename only)."""
    body_no_comments = _strip_html_comments(body)
    raw = extract_wikilinks(body_no_comments)
    return [_normalize_target(t) for t in raw if _normalize_target(t)]


def _resolve(
    target: str,
    by_slug: dict[str, Path],
    alias_lower_to_slug: dict[str, str],
) -> bool:
    """Does ``target`` resolve to any note in the vault?"""
    if target in by_slug:
        return True
    if target.lower() in alias_lower_to_slug:
        return True
    return False


# ---------------------------------------------------------------------------
# count_broken_wikilinks


def count_broken_wikilinks(
    vault_dir: Path,
) -> tuple[int, list[tuple[str, str]]]:
    """Count wikilinks that don't resolve to any note in the vault.

    Returns ``(count, [(source_relpath, target_slug), ...])``.
    Code spans, fenced code blocks, and HTML comment blocks are
    excluded. Resolution checks slug, filename stem, and aliases (case
    insensitive).
    """
    by_slug, _slugs_to_aliases, alias_lower_to_slug = _build_resolution_index(
        vault_dir
    )
    broken: list[tuple[str, str]] = []
    for md in _iter_notes(vault_dir):
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        targets = _extract_targets(body)
        rel = str(md.relative_to(vault_dir))
        for target in targets:
            if not _resolve(target, by_slug, alias_lower_to_slug):
                broken.append((rel, target))
    return len(broken), broken


# ---------------------------------------------------------------------------
# count_orphans


def count_orphans(vault_dir: Path) -> tuple[int, list[str]]:
    """Count notes that aren't referenced by any other note's wikilinks.

    Returns ``(count, [relpath, ...])``. Dailies, ``index.md``,
    ``README.md``, and ``unresolved.md`` are excluded from the orphan
    candidate set. A note is orphan iff none of (slug, aliases,
    filename-stem) appears among the union of referenced targets
    across the whole vault.
    """
    by_slug, slugs_to_aliases, _alias_lower_to_slug = _build_resolution_index(
        vault_dir
    )
    # Build the referenced set: every wikilink target seen anywhere,
    # normalized and lowercased so alias-vs-slug matching works.
    referenced_lower: set[str] = set()
    for md in _iter_notes(vault_dir):
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        for target in _extract_targets(body):
            referenced_lower.add(target.lower())

    orphans: list[str] = []
    for md in _iter_notes(vault_dir):
        rel_parts = md.relative_to(vault_dir).parts
        # Dailies are always excluded — they're date-stamped activity
        # logs that wouldn't normally be linked to.
        if rel_parts and rel_parts[0] == "dailies":
            continue
        text = _read_text(md)
        fm, _body = split_frontmatter(text)
        slug = _slug_from_fm(fm, md.stem)
        aliases = _aliases_from_fm(fm)
        # The set of names that, if referenced anywhere, would
        # disqualify this note from being an orphan.
        identities = {slug.lower(), md.stem.lower()}
        for alias in aliases:
            identities.add(alias.lower())
        # Stable check: also register all known aliases for the slug
        # (covers the rare case where a note's slug differs from its
        # stem and aliases were attached to the slug entry).
        for alias in slugs_to_aliases.get(slug, []):
            identities.add(alias.lower())

        if not (identities & referenced_lower):
            orphans.append(str(md.relative_to(vault_dir)))
    return len(orphans), orphans


# ---------------------------------------------------------------------------
# count_wakes_by_stage

# Filename formats accepted, in order of preference.
# 1. ``HHMMSS-wake.md``
# 2. ``YYYYMMDD-HHMMSS-wake.md``
# 3. ``YYYYMMDDHHMMSS-wake.md``
_WAKE_FILENAME_FORMATS: list[re.Pattern[str]] = [
    re.compile(r"^(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})-wake\.md$"),
    re.compile(
        r"^(?P<Y>\d{4})(?P<MO>\d{2})(?P<D>\d{2})-"
        r"(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})-wake\.md$"
    ),
    re.compile(
        r"^(?P<Y>\d{4})(?P<MO>\d{2})(?P<D>\d{2})"
        r"(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})-wake\.md$"
    ),
]


def _parse_wake_filename(
    name: str, dir_date: datetime | None
) -> datetime | None:
    """Parse a wake-file name; return a naive ``datetime`` or ``None``.

    The HHMMSS-only format takes its date from the parent directory.
    The longer formats embed their own date and ignore the directory
    date.
    """
    for idx, pat in enumerate(_WAKE_FILENAME_FORMATS):
        m = pat.match(name)
        if not m:
            continue
        gd = m.groupdict()
        try:
            if idx == 0:
                if dir_date is None:
                    return None
                return dir_date.replace(
                    hour=int(gd["H"]),
                    minute=int(gd["M"]),
                    second=int(gd["S"]),
                    microsecond=0,
                )
            return datetime(
                year=int(gd["Y"]),
                month=int(gd["MO"]),
                day=int(gd["D"]),
                hour=int(gd["H"]),
                minute=int(gd["M"]),
                second=int(gd["S"]),
            )
        except ValueError:
            return None
    return None


def _intersecting_date_dirs(
    thoughts_dir: Path, window_start: datetime, window_end: datetime
) -> list[Path]:
    """Subdirectories of ``thoughts_dir`` whose date intersects the window.

    Date dir names are ``YYYY-MM-DD``. A date dir D intersects the
    window if D's [00:00, 24:00) span overlaps [window_start,
    window_end). We over-include rather than under-include — a wake
    inside the window is dropped if its parsed timestamp falls outside
    the window anyway.
    """
    if not thoughts_dir.exists():
        return []
    out: list[Path] = []
    for child in sorted(thoughts_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            dir_date = datetime.strptime(child.name, "%Y-%m-%d")
        except ValueError:
            continue
        dir_start = dir_date
        dir_end = dir_date.replace(hour=23, minute=59, second=59)
        # Intersection check on naive datetimes (window_*'s tz is
        # whatever the caller used — we compare like-for-like).
        if dir_end < _strip_tz(window_start):
            continue
        if dir_start > _strip_tz(window_end):
            continue
        out.append(child)
    return out


def _strip_tz(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _read_stage(path: Path) -> str | None:
    text = _read_text(path)
    fm, _body = split_frontmatter(text)
    stage = fm.get("stage")
    if isinstance(stage, str):
        s = stage.strip().upper()
        if s in {"B", "C", "D"}:
            return s
    return None


def count_wakes_by_stage(
    thoughts_dir: Path, window_start: datetime, window_end: datetime
) -> dict[str, int]:
    """Count wake files whose parsed start time falls in the window,
    bucketed by stage frontmatter.

    The window is half-open: ``[window_start, window_end)``. The
    function scans every date subdirectory whose calendar date
    intersects the window — this is the bug fix for wakes that landed
    in tomorrow's dir after midnight.
    """
    counts = {"stage_b": 0, "stage_c": 0, "stage_d": 0}
    ws = _strip_tz(window_start)
    we = _strip_tz(window_end)
    for date_dir in _intersecting_date_dirs(thoughts_dir, ws, we):
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        for md in sorted(date_dir.glob("*.md")):
            ts = _parse_wake_filename(md.name, dir_date)
            if ts is None:
                continue
            if not (ws <= ts < we):
                continue
            stage = _read_stage(md)
            if stage is None:
                continue
            counts[f"stage_{stage.lower()}"] += 1
    return counts


# ---------------------------------------------------------------------------
# CLI


def _parse_iso(s: str) -> datetime:
    # ``datetime.fromisoformat`` handles offsets in 3.11+. Fall back to
    # naive parse if the string is already naive.
    return datetime.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compute the four vault-health metrics covered by "
            "alice_metrics.vault_health and emit JSON."
        )
    )
    parser.add_argument(
        "--vault",
        required=True,
        type=Path,
        help="Path to the cortex-memory vault directory.",
    )
    parser.add_argument(
        "--thoughts",
        type=Path,
        default=None,
        help="Path to inner/thoughts. Required if --window-start/end given.",
    )
    parser.add_argument(
        "--window-start",
        type=_parse_iso,
        default=None,
        help="ISO timestamp; start of the wake-counting window.",
    )
    parser.add_argument(
        "--window-end",
        type=_parse_iso,
        default=None,
        help="ISO timestamp; end of the wake-counting window.",
    )
    args = parser.parse_args(argv)

    out: dict[str, Any] = {
        "total_notes": count_total_notes(args.vault),
    }
    broken_count, _broken = count_broken_wikilinks(args.vault)
    out["broken_wikilinks"] = broken_count
    orphan_count, _orphans = count_orphans(args.vault)
    out["orphan_notes"] = orphan_count

    if args.thoughts and args.window_start and args.window_end:
        out["wake_type_distribution"] = count_wakes_by_stage(
            args.thoughts, args.window_start, args.window_end
        )
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
