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
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from indexer.yaml_lite import extract_wikilinks, split_frontmatter

logger = logging.getLogger(__name__)

# Sanity-check threshold for the truly_dark_count field. With trigger-keyword
# backfill complete across the vault (1593/1593 notes as of 2026-05-19), a
# real truly_dark_count above this ceiling is statistically implausible and
# almost always indicates silent frontmatter parse failures upstream.
# See issue #249 / cortex-memory/research/2026-05-19-vault-health-shadow-dark-swap.md.
_TRULY_DARK_SUSPECT_THRESHOLD = 10

# Low-wake-count detector (issue #323 fix 3). Total of Stage B + C + D
# wakes in the morning window. The May-22 sleep cycle produced 3 wakes
# in 8 hours; a healthy 8h sleep should produce 15–19. Anything under 8
# is a strong signal that the backoff ladder ran away or the supervisor
# stalled. When this fires the morning vault-health event tags
# ``low_wake_count: true`` and an insight-tier surface is written to
# ``<vault>/inner/surface/<YYYY-MM-DD-HHMMSS>-low-wake-count.md``.
WAKE_COUNT_THRESHOLD = 8

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


def _extract_frontmatter_text(text: str) -> str:
    """Return raw frontmatter text between --- fences, or empty string.

    Used as a fallback when structured YAML parsing is unreliable
    (malformed indentation, non-standard values).
    """
    _FENCE = "---"
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return ""
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            return "\n".join(lines[1:i])
    return ""


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


def _iter_resolution_targets(vault_dir: Path) -> list[Path]:
    """Same walk as ``_iter_notes`` but INCLUDES the scaffolding files
    (``index.md``, ``README.md``, ``unresolved.md``). Those files exist
    on disk and are routinely linked to from dailies and other notes
    (e.g. ``[[index]]``, ``[[unresolved]]``); they should resolve as
    wikilink targets even though they don't count toward ``total_notes``
    or appear in the orphan candidate set.
    """
    if not vault_dir.exists():
        return []
    out: list[Path] = []
    for md in vault_dir.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(vault_dir).parts):
            continue
        out.append(md)
    out.sort()
    return out


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

    The walk uses :func:`_iter_resolution_targets` so scaffolding files
    (``index.md`` / ``README.md`` / ``unresolved.md``) are registered as
    resolvable targets too — they're not counted toward ``total_notes``
    or considered as orphan candidates, but ``[[index]]`` is a valid
    link that should resolve cleanly.
    """
    by_slug: dict[str, Path] = {}
    slugs_to_aliases: dict[str, list[str]] = {}
    alias_lower_to_slug: dict[str, str] = {}

    for md in _iter_resolution_targets(vault_dir):
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
    # Canvas files are valid wikilink targets too. They live alongside
    # markdown notes (e.g. cortex-memory/canvases/*.canvas) and dailies
    # routinely link to them as [[name.canvas]]. They have no frontmatter
    # and aren't a source for outbound wikilink scanning, so they only
    # need to land in by_slug — under both the bare stem and the
    # extension-qualified form to match either link style.
    if vault_dir.exists():
        for canvas in vault_dir.rglob("*.canvas"):
            if any(part.startswith(".") for part in canvas.relative_to(vault_dir).parts):
                continue
            by_slug.setdefault(canvas.stem, canvas)
            by_slug.setdefault(f"{canvas.stem}.canvas", canvas)
    return by_slug, slugs_to_aliases, alias_lower_to_slug


def _resolve_rel(
    target: str,
    vault_dir: Path,
    by_slug: dict[str, Path],
    alias_lower_to_slug: dict[str, str],
) -> str | None:
    """Resolve a wikilink target to its vault-relative path.

    Checks slug match first, then alias lookup. Returns the path
    relative to ``vault_dir``, or ``None`` if no match.

    Top-level analogue of the local ``_resolve`` inside
    ``count_inbound_links``, parameterized for reuse by the structural
    reachability extension in :func:`compute_decay_coverage`.
    """
    lower = target.lower()
    resolved = by_slug.get(lower)
    if resolved is None:
        resolved_slug = alias_lower_to_slug.get(lower)
        if resolved_slug:
            resolved = by_slug.get(resolved_slug)
    if resolved is None:
        return None
    return str(resolved.relative_to(vault_dir))


def _extract_targets(body: str, *, rescue_inline: bool = True) -> list[str]:
    """Extract ``[[target]]`` / ``[[target|alias]]`` wikilink targets,
    excluding code spans / fences / HTML comments. Returns normalized
    targets (anchors stripped, basename only).

    ``rescue_inline`` is forwarded to ``extract_wikilinks``. The default
    preserves orphan / inbound-link semantics (a slug-shaped wikilink in
    a backtick span still counts as a reference); the broken-link metric
    overrides it to ``False`` because documentation snippets must not
    contribute to the broken-link count.
    """
    body_no_comments = _strip_html_comments(body)
    raw = extract_wikilinks(body_no_comments, rescue_inline=rescue_inline)
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
    by_slug, _slugs_to_aliases, alias_lower_to_slug = _build_resolution_index(vault_dir)
    broken: list[tuple[str, str]] = []
    for md in _iter_notes(vault_dir):
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        # Strict mode: a slug-shaped wikilink that appears only inside a
        # backtick code span is documentation, not a real reference, so we
        # don't rescue it. Otherwise notes that document the wikilink
        # syntax (``[[example-target]]``) would inflate the broken-link
        # count with synthetic targets.
        targets = _extract_targets(body, rescue_inline=False)
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
    by_slug, slugs_to_aliases, _alias_lower_to_slug = _build_resolution_index(vault_dir)
    # Build the referenced set: every wikilink target seen anywhere,
    # normalized and lowercased so alias-vs-slug matching works.
    # Use _iter_resolution_targets so scaffold files (index.md, README.md,
    # unresolved.md) are scanned for incoming links — they are legitimate
    # reference sources even though they are excluded from the orphan
    # candidate set itself.
    referenced_lower: set[str] = set()
    for md in _iter_resolution_targets(vault_dir):
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        for target in _extract_targets(body):
            referenced_lower.add(target.lower())
        # Also scan frontmatter for wikilinks (e.g. in `related:` lists).
        # Body-only scanning missed notes that are only referenced from
        # frontmatter, inflating orphan counts. Extract from string values
        # in the frontmatter dict (handles lists like `related: [[foo]]`).
        for val in _fm.values():
            if isinstance(val, str):
                for target in _extract_targets(val):
                    referenced_lower.add(target.lower())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for target in _extract_targets(item):
                            referenced_lower.add(target.lower())
        # Fallback: raw frontmatter text catches malformed YAML that
        # structured parsing misses (e.g. broken indentation in `related:`
        # lists that causes the parser to drop items).
        _fm_raw = _extract_frontmatter_text(text)
        if _fm_raw:
            for target in _extract_targets(_fm_raw):
                referenced_lower.add(target.lower())

    orphans: list[str] = []
    for md in _iter_notes(vault_dir):
        rel_parts = md.relative_to(vault_dir).parts
        # Dailies, archive, and gh-state are always excluded — dailies and
        # archive are date-stamped activity logs that wouldn't normally be
        # linked to; gh-state holds GitHub issue/PR state-mirror notes,
        # which by design carry 0 wikilinks (see issue tracker context).
        if rel_parts and rel_parts[0] in {"dailies", "archive", "gh-state"}:
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
# Shadow orphans and truly-dark notes
# Design: [[vault-linking-protocol]]
# Shadow orphan: 0 inbound, ≥1 outbound, ≥1 trigger_keyword — reachable
# via the cue runner but invisible to vault navigation.
# Truly dark: 0 inbound, 0 trigger_keywords — no retrieval path at all.
# ---------------------------------------------------------------------------


def count_shadow_and_dark(vault_dir: Path) -> dict[str, int]:
    """Count shadow orphans and truly-dark notes.

    Returns ``{"shadow_orphan_count": int, "truly_dark_count": int,
    "frontmatter_parse_failures": int}``.
    Dailies, archive, and scaffolding (``index.md``, ``README.md``,
    ``unresolved.md``) are excluded from both categories. Inbound counts
    use the alias-resolved definition from ``count_inbound_links``.

    ``frontmatter_parse_failures`` counts notes whose first line is the
    ``---`` frontmatter fence but ``split_frontmatter`` returned an empty
    dict (unclosed fence or otherwise malformed). Without this counter,
    such notes silently fall into ``truly_dark_count`` because
    ``trigger_keywords`` parses as ``None``. See issue #249.
    """
    inbound = count_inbound_links(vault_dir)
    shadow = 0
    dark = 0
    parse_failures = 0
    for md in _iter_notes(vault_dir):
        rel_parts = md.relative_to(vault_dir).parts
        # gh-state holds GitHub issue/PR state-mirror notes — operational
        # records with 0 trigger_keywords and 0 wikilinks by design. They
        # are not knowledge notes and would otherwise dominate the
        # truly_dark_count signal.
        if rel_parts and rel_parts[0] in {"dailies", "archive", "gh-state"}:
            continue
        if md.name in EXCLUDED_NAMES:
            continue
        rel = str(md.relative_to(vault_dir))
        if inbound.get(rel, 0) > 0:
            continue
        text = _read_text(md)
        # Redirect stubs are bare-slug wikilink resolvers — by design they
        # carry zero inbound links (they are pointed *at*, not *from*). The
        # ``redirect: <canonical-slug>`` body marker is the structural
        # signature. Counting them as shadow orphans inflates the metric
        # past the real signal — see issue #251.
        if "redirect:" in text:
            continue
        fm, body = split_frontmatter(text)
        # Detect silent frontmatter parse failures: the note opens with a
        # ``---`` fence but parsing returned an empty dict. This is the
        # failure mode from #249 — a missing closing fence or a malformed
        # header silently produces an empty fm, which then makes the note
        # look "truly dark" (no trigger_keywords).
        if not fm and text.lstrip().startswith("---"):
            parse_failures += 1
            logger.warning(
                "frontmatter parse failure (note classified as truly dark "
                "by default): %s",
                rel,
            )
        triggers = fm.get("trigger_keywords")
        trigger_count = len(triggers) if isinstance(triggers, list) else 0
        outbound = _extract_targets(body)
        if trigger_count >= 1 and len(outbound) >= 1:
            shadow += 1
        elif trigger_count == 0:
            dark += 1
    return {
        "shadow_orphan_count": shadow,
        "truly_dark_count": dark,
        "frontmatter_parse_failures": parse_failures,
    }


# ---------------------------------------------------------------------------
# Continuous structural-health checks
# Design: [[2026-05-20-continuous-structural-health-check]] (Task 1).
# Two structural-absence detectors that surface grooming candidates with
# slug lists (the existing shadow/dark counters report only aggregates):
#   - zero_edge_notes: 0 inbound, ≥1 outbound, ≥1 trigger_keyword. The
#     shadow_orphan subset; reachable via the cue runner but invisible to
#     vault navigation.
#   - trigger_gap_notes: research/ notes older than 14 days with 0 trigger
#     keywords and ≥2 outbound wikilinks. Referenced by others but not
#     themselves discoverable via the cue runner — keyword-backfill
#     candidates.
# ---------------------------------------------------------------------------


CONTINUOUS_TRIGGER_GAP_AGE_DAYS = 14
CONTINUOUS_TRIGGER_GAP_MIN_OUTBOUND = 2


def compute_continuous_checks(
    vault_dir: Path,
    today: datetime | None = None,
    trigger_gap_age_days: int = CONTINUOUS_TRIGGER_GAP_AGE_DAYS,
    trigger_gap_min_outbound: int = CONTINUOUS_TRIGGER_GAP_MIN_OUTBOUND,
) -> dict[str, Any]:
    """Continuous structural-absence checks (Task 1 of the design note).

    Returns ``{"zero_edge_notes": {"count": int, "slugs": [str]},
    "trigger_gap_notes": {"count": int, "slugs": [str]}}``.

    Dailies, archive, scaffolding files, and redirect stubs are excluded
    from both buckets (same conventions as :func:`count_shadow_and_dark`).
    Reuses the inbound link graph from :func:`count_inbound_links` and
    the wikilink extractor from :func:`_extract_targets`, so the regex
    walk isn't duplicated.
    """
    if today is None:
        today = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    cutoff = today - timedelta(days=trigger_gap_age_days)

    inbound = count_inbound_links(vault_dir)
    zero_edge: list[str] = []
    trigger_gap: list[str] = []

    for md in _iter_notes(vault_dir):
        rel_parts = md.relative_to(vault_dir).parts
        # gh-state mirrors are excluded for the same reason as in
        # count_shadow_and_dark — operational records, not knowledge.
        if rel_parts and rel_parts[0] in {"dailies", "archive", "gh-state"}:
            continue
        rel = str(md.relative_to(vault_dir))
        text = _read_text(md)
        # Redirect stubs are bare-slug resolvers — by design they have 0
        # inbound + trigger_keywords. Excluding them matches the shadow/dark
        # contract (see issue #251).
        if "redirect:" in text:
            continue
        fm, body = split_frontmatter(text)
        triggers = fm.get("trigger_keywords")
        trigger_count = len(triggers) if isinstance(triggers, list) else 0
        outbound_count = len(_extract_targets(body))
        inbound_count = inbound.get(rel, 0)
        slug = _slug_from_fm(fm, md.stem)

        if inbound_count == 0 and trigger_count >= 1 and outbound_count >= 1:
            zero_edge.append(slug)

        if (
            rel_parts
            and rel_parts[0] == "research"
            and trigger_count == 0
            and outbound_count >= trigger_gap_min_outbound
        ):
            created = _parse_created_date(fm.get("created"))
            if created is not None and created < cutoff:
                trigger_gap.append(slug)

    zero_edge.sort()
    trigger_gap.sort()
    return {
        "zero_edge_notes": {"count": len(zero_edge), "slugs": zero_edge},
        "trigger_gap_notes": {"count": len(trigger_gap), "slugs": trigger_gap},
    }


# ---------------------------------------------------------------------------
# Research note decay
# Design: [[2026-05-09-research-note-decay-metric]]
# Count research/ notes older than 60 days with fewer than 2 inbound links.
# Age determined by the `created:` frontmatter field (immutable), not mtime.
# ---------------------------------------------------------------------------


def count_research_decay(
    vault_dir: Path,
    age_days: int = 60,
    link_threshold: int = 2,
) -> int:
    """Count research/ notes that have fallen out of the vault graph.

    A note *decays* when it is older than ``age_days`` (based on the
    ``created:`` frontmatter field) and has fewer than ``link_threshold``
    inbound wikilinks from other vault notes.

    Returns the count of decayed notes.

    Notes younger than the age threshold are ignored — they haven't had
    a chance to be referenced.  The threshold prevents flagging the normal
    one-shot nature of research notes before they've had time to
    mature into references.
    """
    research_dir = vault_dir / "research"
    if not research_dir.exists():
        return 0

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=age_days)

    # Build resolution index once.
    by_slug, slugs_to_aliases, alias_lower_to_slug = _build_resolution_index(vault_dir)

    # Count inbound links per research/ note: iterate each source note,
    # resolve its targets, and increment the counter for any research/
    # target that resolves. This counts unique *source notes* per target,
    # not raw link occurrences (a note that links to the same target
    # multiple times still counts as one source).
    research_rel_to_inbound: dict[str, int] = defaultdict(int)
    for md in _iter_resolution_targets(vault_dir):
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        # Scan body targets.
        targets_body = _extract_targets(body)
        # Scan frontmatter targets (related lists, etc.).
        targets_fm: list[str] = []
        for val in _fm.values():
            if isinstance(val, str):
                targets_fm.extend(_extract_targets(val))
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        targets_fm.extend(_extract_targets(item))
        for target in (*targets_body, *targets_fm):
            resolved = by_slug.get(target) or by_slug.get(target.lower())
            if resolved is None:
                resolved_slug = alias_lower_to_slug.get(target.lower())
                if resolved_slug:
                    resolved = by_slug.get(resolved_slug)
            if resolved is not None:
                rel = str(resolved.relative_to(vault_dir))
                if rel.startswith("research/"):
                    research_rel_to_inbound[rel] += 1

    # Now age each research note and check threshold.
    decay_count = 0
    for md in sorted(research_dir.rglob("*.md")):
        if md.name in EXCLUDED_NAMES:
            continue
        text = _read_text(md)
        fm, _body = split_frontmatter(text)
        # Age from the `created:` frontmatter field.
        created_raw = fm.get("created")
        if created_raw is None:
            # No `created:` field — skip to avoid false positives.
            continue
        created_str = str(created_raw).strip()
        # Parse: expected formats include "YYYY-MM-DD" or "YYYY-MM-DD HH:MM EDT".
        try:
            # Try full timestamp first.
            created_date = datetime.strptime(created_str, "%Y-%m-%d %H:%M %Z")
        except ValueError:
            try:
                created_date = datetime.strptime(created_str, "%Y-%m-%d %H:%M %z")
            except ValueError:
                try:
                    created_date = datetime.strptime(created_str, "%Y-%m-%d")
                except ValueError:
                    # Unparseable date — skip.
                    continue
        if created_date < cutoff:
            rel = str(md.relative_to(vault_dir))
            if research_rel_to_inbound.get(rel, 0) < link_threshold:
                decay_count += 1

    return decay_count


def count_access_decay(
    vault_dir: Path,
    cutoff_days: int = 5,
    access_threshold: int = 0,
) -> int:
    """Count notes with zero (or low) access_count older than cutoff_days.

    This is the **behavioral decay** detector — the dominant decay path
    (99.6% of decayed notes). Unlike ``count_research_decay`` which targets
    structural isolation, this catches notes that are structurally connected
    (have inbound links) but have never been retrieved by the cue runner.

    A note *decays* when:
    - ``created:`` is older than ``cutoff_days``
    - ``access_count:`` <= ``access_threshold`` (default 0)

    Returns the count of decayed notes. Notes younger than the cutoff are
    ignored — they haven't had time to be retrieved.

    Folders excluded: dailies, archive, gh-state (same as
    ``compute_decay_coverage``).
    """
    vault_dir = vault_dir.resolve()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=cutoff_days)

    decay_count = 0
    for md in vault_dir.rglob("*.md"):
        # Skip excluded top-level dirs.
        rel_parts = md.relative_to(vault_dir).parts
        if rel_parts and rel_parts[0] in {"dailies", "archive", "gh-state"}:
            continue
        if md.name in EXCLUDED_NAMES:
            continue

        text = _read_text(md)
        fm, _body = split_frontmatter(text)

        created_raw = fm.get("created")
        if created_raw is None:
            continue
        created_date = _parse_created_date(created_raw)
        if created_date is None:
            continue
        if created_date >= cutoff:
            continue

        access_count = _parse_access_count(fm.get("access_count"))
        if access_count is None:
            continue
        if access_count > access_threshold:
            continue

        decay_count += 1

    return decay_count


# ---------------------------------------------------------------------------
# Decay coverage (Layer 1 blind-spot detection)
# Design: [[2026-05-10-decay-blind-spot-detection-design]]
# Pool: notes that were `access_count == 0` and age >= 5 days at the cue
# runner activation date. Coverage: fraction of pool with last_accessed
# inside the window. A note that was in the decayed pool at activation
# and has since been touched (access_count > 0, last_accessed >= activation)
# still counts as part of the pool — that's the whole point: we want to
# see whether the pool is shrinking via access, not just freeze a snapshot.
# ---------------------------------------------------------------------------


# Cue runner went live 2026-05-06. Before that date, no access events
# were generated by the retrieval path, so any `access_count > 0` /
# `last_accessed >= 2026-05-06` is a real post-activation read.
DECAY_COVERAGE_ACTIVATION_DATE = "2026-05-06"

# Folders excluded from the decayed-pool candidate set. Dailies are
# activity logs (not retrievable knowledge); index/README/unresolved are
# vault scaffolding; archive/ is intentionally cold storage. gh-state
# holds GitHub issue/PR state-mirror notes — operational records with
# 0 trigger_keywords by design, so the cue runner is never expected to
# surface them. Counting them in the decayed pool would depress the
# coverage signal with notes that nobody is supposed to be reading.
_DECAY_EXCLUDED_FOLDERS = frozenset({"dailies", "archive", "gh-state"})


def _parse_access_count(raw: Any) -> int | None:
    """Best-effort parser for the ``access_count:`` frontmatter field."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_last_accessed(raw: Any) -> datetime | None:
    """Parse ``last_accessed`` to a naive midnight datetime, or None."""
    return _parse_created_date(raw)


def _note_domain(fm: dict[str, Any], rel: str) -> str:
    """Domain bucket for a note: frontmatter ``domain:`` or folder name."""
    raw = fm.get("domain")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    parts = rel.split("/", 1)
    if len(parts) > 1:
        return parts[0]
    return "uncategorized"


def compute_decay_coverage(
    vault_dir: Path,
    window_days: int = 7,
    activation_date: str = DECAY_COVERAGE_ACTIVATION_DATE,
    cutoff_days: int = 5,
    today: datetime | None = None,
) -> dict[str, Any]:
    """Compute Layer 1 decay coverage — see design note for the contract.

    **Decayed pool** (at activation):
    - ``created`` <= ``activation_date - cutoff_days`` (note was already
      ``cutoff_days`` old at activation; the 5-day cliff had already passed
      so it was eligible to decay)
    - At activation, either still untouched (currently ``access_count == 0``)
      OR touched only post-activation (``last_accessed >= activation_date``).
      That gives us the set of notes that *were* in the decayed pool when
      the cue runner came online, regardless of whether they've since been
      recovered.
    - Excludes ``dailies/`` and ``archive/`` (and the standard scaffolding
      files via :func:`_iter_notes`).

    **Accessed in window**: pool members whose ``last_accessed`` is on or
    after ``max(today - window_days, activation_date)``. The activation
    floor matters in the first weeks post-activation, when ``today -
    window_days`` predates the cue runner and would inflate the window.

    Returns the payload shape documented in the design note:

    ``{total_decayed_notes, decayed_accessed_in_window, decay_coverage_pct,
       window_days, activation_date, by_domain: {domain: {decayed,
       accessed, coverage_pct}}}``.

    When the pool is empty, ``decay_coverage_pct`` is ``1.0`` (vacuously
    healthy — nothing to recover means nothing is stuck).
    """
    if today is None:
        today = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today = today.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    activation = _parse_created_date(activation_date)
    if activation is None:
        raise ValueError(f"unparseable activation_date: {activation_date!r}")
    # Notes had to be at least cutoff_days old at activation to count as
    # "decayed at activation". A note created on activation-day is fresh,
    # not decayed.
    age_cutoff = activation - timedelta(days=cutoff_days)

    window_floor_by_days = today - timedelta(days=window_days)
    # Cap the window at the activation date — pre-activation access events
    # didn't exist, so a 7-day window in week 1 should still floor at
    # activation. As the activation date recedes into the past, the
    # window_days bound takes over naturally.
    window_floor = max(window_floor_by_days, activation)

    by_domain: dict[str, dict[str, int]] = defaultdict(
        lambda: {"decayed": 0, "accessed": 0}
    )
    total_decayed = 0
    total_accessed = 0

    # Structural reachability: pre-build resolution index + tracking sets.
    # Indexed once so the post-loop pass can resolve wikilink targets from
    # recently-accessed notes back to decayed-pool slugs.
    by_slug, _slugs_to_aliases, alias_lower_to_slug = _build_resolution_index(
        vault_dir
    )
    decayed_by_slug: dict[str, str] = {}       # slug_lower → domain
    decayed_rel_to_slug: dict[str, str] = {}   # rel_lower → slug_lower
    accessed_decay_slugs: set[str] = set()     # directly-accessed decayed slugs

    for md in _iter_notes(vault_dir):
        rel_parts = md.relative_to(vault_dir).parts
        if rel_parts and rel_parts[0] in _DECAY_EXCLUDED_FOLDERS:
            continue
        text = _read_text(md)
        fm, _body = split_frontmatter(text)

        created = _parse_created_date(fm.get("created"))
        if created is None or created > age_cutoff:
            continue

        access_count = _parse_access_count(fm.get("access_count"))
        if access_count is None:
            continue

        last_accessed = _parse_last_accessed(fm.get("last_accessed"))

        # Was this note in the decayed pool at activation?
        # Yes if either: never accessed (count == 0), or every access has
        # happened on/after activation (count > 0 AND last_accessed >=
        # activation). A note with count > 0 and last_accessed < activation
        # was already in active circulation pre-cue and isn't a decay case.
        if access_count == 0:
            in_pool = True
        elif last_accessed is not None and last_accessed >= activation:
            in_pool = True
        else:
            in_pool = False
        if not in_pool:
            continue

        rel = str(md.relative_to(vault_dir))
        domain = _note_domain(fm, rel)
        total_decayed += 1
        by_domain[domain]["decayed"] += 1

        slug = _slug_from_fm(fm, md.stem) or md.stem
        slug_lower = slug.lower()
        decayed_by_slug[slug_lower] = domain
        decayed_rel_to_slug[rel.lower()] = slug_lower

        if last_accessed is not None and last_accessed >= window_floor:
            total_accessed += 1
            by_domain[domain]["accessed"] += 1
            accessed_decay_slugs.add(slug_lower)

    # ── Structural reachability ──────────────────────────────────
    # A decayed note is "structurally covered" if it was directly
    # accessed in the window OR has ≥ 1 inbound link from a
    # recently-accessed note. Union of direct + structural support.
    #
    # Pass: for each recently-accessed note, follow its wikilink
    # targets to find decayed notes it links to.
    struct_links_slugs: set[str] = set()
    for md in _iter_notes(vault_dir):
        text = _read_text(md)
        fm, body = split_frontmatter(text)
        last_accessed = _parse_last_accessed(fm.get("last_accessed"))
        if last_accessed is None or last_accessed < window_floor:
            continue
        targets: list[str] = list(_extract_targets(body))
        for val in fm.values():
            if isinstance(val, str):
                targets.extend(_extract_targets(val))
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        targets.extend(_extract_targets(item))
        for target in targets:
            target_lower = target.lower()
            if target_lower in decayed_by_slug:
                struct_links_slugs.add(target_lower)
                continue
            resolved = _resolve_rel(
                target, vault_dir, by_slug, alias_lower_to_slug,
            )
            if resolved is not None:
                if resolved.lower() in decayed_rel_to_slug:
                    struct_links_slugs.add(
                        decayed_rel_to_slug[resolved.lower()],
                    )

    # Union of direct access + structural support
    struct_recovered_set = accessed_decay_slugs | struct_links_slugs
    struct_coverage = (
        round(100.0 * len(struct_recovered_set) / total_decayed, 2)
        if total_decayed > 0
        else 100.0
    )

    # Per-domain structural counts
    struct_by_domain: dict[str, int] = defaultdict(int)
    for slug_lower in struct_recovered_set:
        struct_by_domain[decayed_by_slug[slug_lower]] += 1

    coverage_pct = (
        round(100.0 * total_accessed / total_decayed, 2)
        if total_decayed > 0
        else 100.0
    )
    domain_payload: dict[str, dict[str, float | int]] = {}
    for domain, counts in sorted(by_domain.items()):
        d_total = counts["decayed"]
        d_acc = counts["accessed"]
        d_pct = round(100.0 * d_acc / d_total, 2) if d_total > 0 else 0.0
        d_struct = struct_by_domain.get(domain, 0)
        d_struct_pct = (
            round(100.0 * d_struct / d_total, 2) if d_total > 0 else 0.0
        )
        domain_payload[domain] = {
            "decayed": d_total,
            "accessed": d_acc,
            "coverage_pct": d_pct,
            "struct_recovered": d_struct,
            "structural_coverage_pct": d_struct_pct,
        }

    return {
        "total_decayed_notes": total_decayed,
        "decayed_accessed_in_window": total_accessed,
        "decay_coverage_pct": coverage_pct,
        "decay_coverage_structural_pct": struct_coverage,
        "window_days": window_days,
        "activation_date": activation_date,
        "by_domain": domain_payload,
    }


# ---------------------------------------------------------------------------
# Recovery state: post-burst recovery tracking
# Design: [[2026-05-09-post-burst-recovery-tracking]]
# Three signals over a 14-day rolling window:
#   1. Tier 1 ratio (% of research notes with ≥ 10 inbound links)
#   2. Output rate trend (slope of daily research note creation)
#   3. Structural debt delta (change in orphan+broken over window)
# ---------------------------------------------------------------------------


def count_inbound_links(
    vault_dir: Path,
    exclude: frozenset[str] | None = None,
) -> dict[str, int]:
    """Count how many inbound links each note receives from the vault.

    Returns ``{relpath: count}`` where *relpath* is relative to ``vault_dir``
    and *count* is the number of other notes that reference this note via
    a wikilink (in body or frontmatter).

    ``exclude`` is an optional set of relpaths to ignore as both sources
    and targets (useful for ignoring dailies when computing hub ratios).
    """
    if exclude is None:
        exclude = frozenset()

    by_slug, slugs_to_aliases, alias_lower_to_slug = _build_resolution_index(
        vault_dir,
    )

    def _resolve(target: str) -> str | None:
        lower = target.lower()
        resolved = by_slug.get(lower)
        if resolved is None:
            resolved_slug = alias_lower_to_slug.get(lower)
            if resolved_slug:
                resolved = by_slug.get(resolved_slug)
        if resolved is None:
            return None
        return str(resolved.relative_to(vault_dir))

    # Count one inbound per wikilink occurrence per source file. Two links
    # from different sources to the same target → 2 inbound. Multiple links
    # from a single source to the same target collapse to 1 (a note that
    # references hub three times shouldn't triple-count itself as a hub fan).
    inbound: dict[str, int] = defaultdict(int)
    for md in _iter_resolution_targets(vault_dir):
        if str(md.relative_to(vault_dir)) in exclude:
            continue
        text = _read_text(md)
        _fm, body = split_frontmatter(text)
        per_source_targets: set[str] = set()
        for target in _extract_targets(body):
            per_source_targets.add(target.lower())
        for val in _fm.values():
            if isinstance(val, str):
                for target in _extract_targets(val):
                    per_source_targets.add(target.lower())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        for target in _extract_targets(item):
                            per_source_targets.add(target.lower())
        for target in per_source_targets:
            rel = _resolve(target)
            if rel is not None and rel not in exclude:
                inbound[rel] += 1
    return dict(inbound)


def count_tier1_ratio(
    vault_dir: Path,
    notes_7d_cutoff: datetime | None = None,
) -> dict[str, float | int]:
    """Compute the Tier 1 ratio for research/ notes ≥ 7 days old.

    **Tier 1 ratio** = fraction of 7-day-old research/ notes that have
    ≥ 10 inbound links from other vault notes.

    Only notes in the ``research/`` subdirectory count toward the metric.
    Notes younger than 7 days are excluded (they haven't had time to
    accumulate inbound links).

    Returns ``{"ratio": float, "hubs": int, "total": int}`` where
    *hubs* is the count of notes with ≥ 10 inbound links and *total*
    is the count of research/ notes ≥ 7 days old.
    """
    if notes_7d_cutoff is None:
        notes_7d_cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Collect research/ notes created ≥ 7 days ago. Uses `created:` frontmatter,
    # not mtime — bulk backfills (e.g. trigger_keyword) rewrite every file and
    # would zero out the mtime-based age signal.
    old_notes: list[Path] = []
    research_dir = vault_dir / "research"
    if not research_dir.exists():
        return {"ratio": 0.0, "hubs": 0, "total": 0}
    for md in sorted(research_dir.rglob("*.md")):
        if md.name in EXCLUDED_NAMES:
            continue
        text = _read_text(md)
        fm, _body = split_frontmatter(text)
        created = _parse_created_date(fm.get("created"))
        if created is None or created >= notes_7d_cutoff:
            continue  # too recent or no created: field
        old_notes.append(md)

    if not old_notes:
        return {"ratio": 0.0, "hubs": 0, "total": 0}

    # Count inbound links (excluding dailies as sources to avoid
    # inflating counts with activity-log references).
    inbound = count_inbound_links(vault_dir, exclude=frozenset(
        str(d.relative_to(vault_dir))
        for d in (vault_dir / "dailies").rglob("*.md")
        if d.is_file()
    ) if (vault_dir / "dailies").exists() else frozenset())

    hubs = 0
    for md in old_notes:
        rel = str(md.relative_to(vault_dir))
        count = inbound.get(rel, 0)
        if count >= 10:
            hubs += 1

    ratio = hubs / len(old_notes) if old_notes else 0.0
    return {"ratio": round(ratio, 4), "hubs": hubs, "total": len(old_notes)}


def _linear_regression_slope(x: list[float], y: list[float]) -> float:
    """Ordinary least-squares slope for paired (x, y) data.

    Returns 0.0 if fewer than 2 points or all x values are identical.
    """
    n = len(x)
    if n < 2:
        return 0.0
    mean_x = sum(x) / n
    # Check for zero variance in x.
    if all(xi == mean_x for xi in x):
        return 0.0
    numerator = sum((xi - mean_x) * (yi - sum(y) / n) for xi, yi in zip(x, y))
    denominator = sum((xi - mean_x) ** 2 for xi in x)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def count_output_rate_slope(
    vault_dir: Path,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, float | int]:
    """Compute the slope of daily research/ note creation over a window.

    Counts notes in ``research/*.md`` by `created:` frontmatter date (in
    calendar-day buckets), fits an OLS line, and returns the slope
    (notes/day/day). Frontmatter is used rather than mtime so bulk
    backfills don't masquerade as a creation burst.

    Positive slope = output accelerating (burst).  Negative = output
    declining (recovery).  Near-zero = stable.

    Returns ``{"slope": float, "days": int, "counts": list[int]}``.
    """
    if window_start is None:
        window_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
    if window_end is None:
        window_end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    ws = window_start.replace(tzinfo=None)
    we = window_end.replace(tzinfo=None)

    # Bucket notes by calendar day of `created:` frontmatter date.
    daily: dict[datetime, int] = defaultdict(int)
    research_dir = vault_dir / "research"
    if research_dir.exists():
        for md in research_dir.rglob("*.md"):
            if md.name in EXCLUDED_NAMES:
                continue
            text = _read_text(md)
            fm, _body = split_frontmatter(text)
            created = _parse_created_date(fm.get("created"))
            if created is None:
                continue
            day = created.replace(hour=0, minute=0, second=0, microsecond=0)
            if ws <= day < we:
                daily[day] += 1

    if not daily:
        return {"slope": 0.0, "days": 0, "counts": []}

    # Create a full day sequence so gaps (zero-creation days) are
    # counted — this matters for the slope.
    days: list[datetime] = []
    counts: list[int] = []
    current = ws.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= we:
        days.append(current)
        counts.append(daily.get(current, 0))
        current += timedelta(days=1)

    x_vals = [float(i) for i in range(len(days))]
    y_vals = [float(c) for c in counts]
    slope = _linear_regression_slope(x_vals, y_vals)
    return {"slope": round(slope, 4), "days": len(days), "counts": counts}


def _read_events_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read and return all events from a JSONL event file."""
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def _event_structural_debt(event: dict[str, Any]) -> int:
    """Structural debt from a single vault_health event: orphans + broken."""
    return event.get("orphan_notes", 0) + event.get("broken_wikilinks", 0)


def compute_recovery_state(
    vault_dir: Path,
    thoughts_dir: Path,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    events_path: Path | None = None,
) -> dict[str, Any]:
    """Compute the full recovery_state sub-object for a vault_health event.

    The 14-day rolling window metric determines whether the thinking
    system is in recovery, stable, or deteriorating state after a
    research burst.

    **Signals:**
    1. Tier 1 ratio (% of 7-day-old research/ notes with ≥ 10 inbound links)
    2. Output rate slope (OLS slope of daily research note creation)
    3. Structural debt delta (change in orphan+broken over window)

    **Decision rules:**
    - 2+ green signals → ``recovering`` (can accept more work)
    - 2+ yellow signals → ``consolidating`` (self-correcting)
    - 2+ red signals   → ``deteriorating`` (trigger consolidation)

    **Burst detection:** If ``research_notes_last_night`` > 20 for
    2+ consecutive days in the events log, status is ``active_burst``.

    Default when no burst is active and no window data available:
    ``{"status": "baseline", "window": "N/A"}``.
    """
    if window_start is None:
        window_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=14)
    if window_end is None:
        window_end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    ws = window_start.replace(tzinfo=None)
    we = window_end.replace(tzinfo=None)

    # --- Check for active burst via events.jsonl ---
    if events_path and events_path.exists():
        events = _read_events_jsonl(events_path)
        last_night_counts: list[int] = []
        for evt in reversed(events):
            if evt.get("type") != "vault_health":
                continue
            rnl = evt.get("research_notes_last_night", 0)
            if isinstance(rnl, (int, float)) and rnl > 20:
                last_night_counts.append(int(rnl))
            elif len(last_night_counts) > 0:
                break  # once we hit a non-burst, stop looking back
        if len(last_night_counts) >= 2:
            return {
                "status": "active_burst",
                "tier_1_ratio": None,
                "output_rate_slope": None,
                "structural_debt_delta": None,
                "estimated_recovery_tier": "R0",
                "burst_start_date": None,
                "day_in_window": None,
            }

    # --- Compute three signals ---
    # 1. Tier 1 ratio
    tier1 = count_tier1_ratio(vault_dir, notes_7d_cutoff=ws)
    tier1_ratio = tier1.get("ratio", 0.0)

    # 2. Output rate slope
    output = count_output_rate_slope(vault_dir, window_start=ws, window_end=we)
    slope = output.get("slope", 0.0)
    slope_total_notes = sum(output.get("counts", []) or [])

    # 3. Structural debt delta
    debt_delta = 0
    debt_has_data = bool(events_path and events_path.exists())
    if events_path and events_path.exists():
        events = _read_events_jsonl(events_path)
        # Find vault_health events near window boundaries.
        # Start debt: most recent event at or before window_start, or
        #             oldest event inside the window if none precedes it.
        # End debt: most recent event overall.
        debt_at_start_pre = None
        debt_at_start_in = None
        debt_at_end = None
        for evt in events:
            if evt.get("type") != "vault_health":
                continue
            evt_date_str = evt.get("date")
            if not evt_date_str:
                continue
            try:
                evt_date = datetime.strptime(evt_date_str, "%Y-%m-%d")
            except ValueError:
                continue
            evt_naive = evt_date.replace(hour=0, minute=0, second=0, microsecond=0)
            # Pre-window candidate: most recent event <= window_start.
            if evt_naive <= ws and (debt_at_start_pre is None or evt_date > debt_at_start_pre):
                debt_at_start_pre = evt_date
            # In-window candidate: oldest event > window_start.
            if evt_naive > ws and (debt_at_start_in is None or evt_date < debt_at_start_in):
                debt_at_start_in = evt_date
            # End debt: most recent event overall.
            if debt_at_end is None or evt_date > debt_at_end:
                debt_at_end = evt_date
        debt_at_start = debt_at_start_pre or debt_at_start_in
        # Compute delta using the event dicts directly.
        # Need at least two distinct events (start ≠ end) for a meaningful delta.
        if (
            debt_at_start is not None
            and debt_at_end is not None
            and debt_at_start != debt_at_end
        ):
            start_evt = next(
                (e for e in events if e.get("date") == debt_at_start.strftime("%Y-%m-%d")),
                None,
            )
            end_evt = next(
                (e for e in events if e.get("date") == debt_at_end.strftime("%Y-%m-%d")),
                None,
            )
            if start_evt and end_evt:
                debt_delta = _event_structural_debt(end_evt) - _event_structural_debt(
                    start_evt
                )
        # Single event in/around window → can't compute a delta. debt_has_data
        # stays True (events.jsonl exists), but classifier treats delta=0 as
        # green which is fine here since we genuinely have no signal of decline.

    # --- Classify each signal ---
    def _tier1_color(ratio: float) -> str:
        if ratio >= 0.15:
            return "green"
        if ratio >= 0.05:
            return "yellow"
        return "red"

    def _slope_color(s: float, total_notes: int) -> str:
        # Positive slope = recovering (output declining from burst)
        # Negative slope = deteriorating (output accelerating)
        # Fewer than 3 notes total in the window → yellow (cautious unknown):
        # OLS over a series of zeros gives a small slope that the green band
        # would swallow, masking a genuine "no data" state.
        if total_notes < 3:
            return "yellow"
        if s <= 15:
            return "green"
        if s <= 25:
            return "yellow"
        return "red"

    def _debt_color(delta: int, has_data: bool) -> str:
        # No events.jsonl available → yellow (cautious unknown).
        if not has_data:
            return "yellow"
        if delta <= 5:
            return "green"
        if delta <= 20:
            return "yellow"
        return "red"

    colors = {
        "tier_1_ratio": _tier1_color(tier1_ratio),
        "output_rate_slope": _slope_color(slope, slope_total_notes),
        "structural_debt_delta": _debt_color(debt_delta, debt_has_data),
    }

    # --- Aggregate: 2+ green → recovering, 2+ yellow → consolidating,
    #    2+ red → deteriorating ---
    color_counts = defaultdict(int)
    for c in colors.values():
        color_counts[c] += 1

    if color_counts["green"] >= 2:
        status = "recovering"
    elif color_counts["yellow"] >= 2:
        status = "consolidating"
    elif color_counts["red"] >= 2:
        status = "deteriorating"
    else:
        # Mixed signals — default to consolidating (cautious).
        status = "consolidating"

    # --- Estimated recovery tier ---
    if status == "recovering":
        if tier1_ratio >= 0.25 and debt_delta <= 5 and slope <= 10:
            tier_label = "R5-R6"
        else:
            tier_label = "R3-R4"
    elif status == "consolidating":
        if tier1_ratio >= 0.05:
            tier_label = "R3-R4"
        else:
            tier_label = "R1-R2"
    elif status == "deteriorating":
        tier_label = "R0"
    else:
        tier_label = "N/A"

    return {
        "status": status,
        "tier_1_ratio": tier1_ratio,
        "output_rate_slope": slope,
        "structural_debt_delta": debt_delta,
        "estimated_recovery_tier": tier_label,
        "burst_start_date": None,
        "day_in_window": None,
    }


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


def _parse_wake_filename(name: str, dir_date: datetime | None) -> datetime | None:
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
        # Production wake frontmatter uses ``sleep_b``/``sleep_c``/
        # ``sleep_d``; older tests/fixtures use the bare ``B``/``C``/
        # ``D``. Accept both so the metric reflects reality.
        if s.startswith("SLEEP_"):
            s = s[len("SLEEP_"):]
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
# Stage C candidates: bloated notes + stale dailies
# Previously computed inline in the wake template via `find | wc -l` bash.
# Moved into Python so the morning vault scan collapses to a single command
# (the manual JSON-assembly step kept dropping fields).
# ---------------------------------------------------------------------------


def count_stage_c_candidates(
    vault_dir: Path,
    bloated_min_lines: int = 250,
    stale_days: int = 90,
    today: datetime | None = None,
) -> dict[str, int]:
    """Stage C workload snapshot.

    - ``bloated_notes``: vault ``.md`` files with > ``bloated_min_lines``
      lines, excluding ``dailies/``, ``index.md``, ``README.md``,
      ``unresolved.md``. Atomization candidates.
    - ``stale_dailies``: dailies whose filename date is older than
      ``stale_days``. Archive-eligible.
    - ``total``: sum.
    """
    bloated = 0
    if vault_dir.exists():
        for md in vault_dir.rglob("*.md"):
            rel_parts = md.relative_to(vault_dir).parts
            if rel_parts and rel_parts[0] in {"dailies", "archive"}:
                continue
            if md.name in EXCLUDED_NAMES:
                continue
            try:
                with md.open("r", encoding="utf-8", errors="ignore") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                continue
            if line_count > bloated_min_lines:
                bloated += 1

    if today is None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=stale_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    stale = 0
    dailies_dir = vault_dir / "dailies"
    if dailies_dir.exists():
        for md in dailies_dir.glob("*.md"):
            stem = md.stem
            # Filename date: dailies are named YYYY-MM-DD.md.
            # Lexicographic compare works because of ISO format.
            if len(stem) >= 10 and stem[:10] < cutoff_str:
                stale += 1

    return {"bloated_notes": bloated, "stale_dailies": stale, "total": bloated + stale}


# ---------------------------------------------------------------------------
# research_notes_last_night: research/ notes whose `created:` frontmatter
# date equals yesterday. (mtime-based variants drift when notes are touched.)
# ---------------------------------------------------------------------------


def _parse_created_date(raw: Any) -> datetime | None:
    """Best-effort parser for the ``created:`` frontmatter field.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM TZ``, or a ``datetime``
    object already parsed by the YAML loader. Returns a naive datetime
    at midnight on the created day, or ``None`` if unparseable.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M %Z", "%Y-%m-%d %H:%M %z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    # Last resort: take first 10 chars and try ISO.
    if len(s) >= 10:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
        return dt
    return None


def count_research_notes_created_on(vault_dir: Path, day: datetime) -> int:
    """Count research/ notes whose `created:` frontmatter equals ``day``."""
    research_dir = vault_dir / "research"
    if not research_dir.exists():
        return 0
    target = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    count = 0
    for md in research_dir.rglob("*.md"):
        if md.name in EXCLUDED_NAMES:
            continue
        text = _read_text(md)
        fm, _body = split_frontmatter(text)
        created = _parse_created_date(fm.get("created"))
        if created is not None and created == target:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Surface counts (written / handled)
# ---------------------------------------------------------------------------


def count_surfaces_in_window(
    surface_dir: Path,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Count files under ``surface_dir`` (non-recursive into ``.handled``)
    with mtime in ``[window_start, window_end)``.

    Surface dir layout: ``inner/surface/<date>/<file>.md`` plus the
    ``.handled/`` archive. We scan every ``YYYY-MM-DD`` date dir whose
    name could plausibly contain a file in the window, and filter by
    mtime.
    """
    if not surface_dir.exists():
        return 0
    ws_ts = _strip_tz(window_start).timestamp()
    we_ts = _strip_tz(window_end).timestamp()
    count = 0
    for child in surface_dir.iterdir():
        if not child.is_dir():
            continue
        # Skip the .handled archive and any other dotfile dirs.
        if child.name.startswith("."):
            continue
        # Only YYYY-MM-DD date dirs are valid sources.
        try:
            datetime.strptime(child.name, "%Y-%m-%d")
        except ValueError:
            continue
        for f in child.rglob("*"):
            if not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if ws_ts <= mtime < we_ts:
                count += 1
    return count


def count_surfaces_handled_today(surface_dir: Path, today: datetime) -> int:
    """Count files in ``surface_dir/.handled/<today>/``."""
    handled = surface_dir / ".handled" / today.strftime("%Y-%m-%d")
    if not handled.exists():
        return 0
    return sum(1 for f in handled.rglob("*") if f.is_file())


# ---------------------------------------------------------------------------
# Productive wakes last night
# A wake file is "productive" when its frontmatter has did_work: true.
# Filename-based timestamp puts the file in the [23:00, 07:00) window;
# we reuse the wake-filename parser from count_wakes_by_stage.
# ---------------------------------------------------------------------------


def _read_did_work(path: Path) -> bool:
    text = _read_text(path)
    fm, _body = split_frontmatter(text)
    val = fm.get("did_work")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "1"}
    return False


# Retained for backward compat; build_vault_health_event no longer calls this.
def count_productive_wakes(
    thoughts_dir: Path, window_start: datetime, window_end: datetime
) -> int:
    """Count wake files in the window whose frontmatter has ``did_work: true``."""
    if not thoughts_dir.exists():
        return 0
    ws = _strip_tz(window_start)
    we = _strip_tz(window_end)
    count = 0
    for date_dir in _intersecting_date_dirs(thoughts_dir, ws, we):
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        for md in date_dir.glob("*.md"):
            ts = _parse_wake_filename(md.name, dir_date)
            if ts is None:
                continue
            if not (ws <= ts < we):
                continue
            if _read_did_work(md):
                count += 1
    return count


def count_all_wakes_in_window(
    thoughts_dir: Path, window_start: datetime, window_end: datetime
) -> int:
    """Count every wake file whose parsed start time falls in the window.

    Same scanning logic as :func:`count_productive_wakes` but without the
    ``did_work`` filter — useful for "how many wakes fired last night"
    regardless of whether each wake produced work.
    """
    if not thoughts_dir.exists():
        return 0
    ws = _strip_tz(window_start)
    we = _strip_tz(window_end)
    count = 0
    for date_dir in _intersecting_date_dirs(thoughts_dir, ws, we):
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        for md in date_dir.glob("*.md"):
            ts = _parse_wake_filename(md.name, dir_date)
            if ts is None:
                continue
            if not (ws <= ts < we):
                continue
            count += 1
    return count


# ---------------------------------------------------------------------------
# Event-stream helpers: dedup + atomic append
# ---------------------------------------------------------------------------


def vault_health_event_exists_for_date(events_path: Path, date_str: str) -> bool:
    """True iff ``events.jsonl`` already has a ``vault_health`` event whose
    ``date`` field equals ``date_str``."""
    if not events_path.exists():
        return False
    try:
        with events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "vault_health" and evt.get("date") == date_str:
                    return True
    except OSError:
        return False
    return False


def _append_event(events_path: Path, event: dict[str, Any]) -> None:
    """Append ``event`` as a single JSON line. Creates parent dir if needed."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event)
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Local-time helpers for the morning window
# ---------------------------------------------------------------------------


# ``America/New_York`` resolves to EDT or EST depending on the calendar
# date — zoneinfo handles DST without us tracking the schedule. Used by
# ``_local_now`` and ``_sleep_window_closed`` so the wall-clock semantics
# are correct regardless of the container's system timezone (the alice
# container runs in UTC; the sleep schedule is Eastern).
_EASTERN = ZoneInfo("America/New_York")


def _local_now() -> datetime:
    """Naive Eastern wall-clock datetime.

    The morning scan owns Eastern semantics (sleep window 23:00→07:00
    Eastern), and the alice container runs in UTC — so a bare
    ``datetime.now()`` would read the UTC wall clock and silently
    mis-align every comparison against the ``today_07`` boundary by 4–5
    hours. Constructing through ``zoneinfo`` gives us the Eastern wall
    clock regardless of the container's system timezone, then stripping
    ``tzinfo`` preserves the naive-datetime contract every downstream
    caller (``_morning_window``, ``count_*_in_window``) already expects.
    """
    return datetime.now(_EASTERN).replace(tzinfo=None)


def _morning_window(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """Return ``(yesterday_23, today_07, today_midnight)`` as naive datetimes.

    The wake scan window is yesterday 23:00 through today 07:00. Both
    endpoints are needed for surface counts and productive-wake counts.
    """
    if now is None:
        now = _local_now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_23 = today_midnight - timedelta(hours=1)
    today_07 = today_midnight + timedelta(hours=7)
    return yesterday_23, today_07, today_midnight


def _sleep_window_closed(scan_dt: datetime) -> bool:
    """Return True iff ``scan_dt``'s wall time in America/New_York is >= 07:00.

    The overnight sleep window runs 23:00 → 07:00 Eastern. A vault_health
    scan that fires inside that window sees a *partial* wake distribution
    — Stage D wakes cluster 00:27–02:20 Eastern, so an early-morning scan
    at, say, 00:14 reports ``stage_d: 0`` even though six Stage D wakes
    are queued for later in the night. The downstream thinker prompt
    treats ``stage_d == 0`` as evidence of a Stage D drought and raises
    ``stage_d_drought: true`` on the event; firing that flag against
    partial-window data produces a false positive every night.

    This helper is the single source of truth for "is it safe to draw
    drought conclusions from the wake distribution yet?". The check is
    explicitly timezone-aware (the alice container runs in UTC; the
    sleep schedule is Eastern) and DST-correct.

    ``scan_dt`` may be naive (interpreted as Eastern wall-clock — this
    matches the ``_local_now()`` contract and the legacy ``_morning_window``
    semantics, both of which represent time in Eastern naive form) or
    timezone-aware (converted to Eastern explicitly).

    Originally surfaced 2026-05-08, re-surfaced 2026-06-04 and 2026-06-05.
    """
    if scan_dt.tzinfo is None:
        # Naive: caller has already committed to Eastern wall-clock
        # semantics (see ``_local_now``). Attach the zone without
        # shifting the clock so ``.hour`` reads the Eastern hour.
        eastern = scan_dt.replace(tzinfo=_EASTERN)
    else:
        eastern = scan_dt.astimezone(_EASTERN)
    return eastern.hour >= 7


# ---------------------------------------------------------------------------
# Full-event assembly
# ---------------------------------------------------------------------------


def build_vault_health_event(
    vault_dir: Path,
    thoughts_dir: Path,
    events_path: Path | None,
    surface_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the complete vault_health event dict.

    All fields the morning scan needs are computed here. The caller can
    either print this (--append off) or hand it to ``_append_event``.
    """
    if now is None:
        now = _local_now()
    yesterday_23, today_07, today_midnight = _morning_window(now)
    yesterday_midnight = today_midnight - timedelta(days=1)

    if surface_dir is None:
        # Default: sibling of thoughts_dir (inner/thoughts -> inner/surface).
        surface_dir = thoughts_dir.parent / "surface"

    # ts: ISO8601 with local offset. Use the system's local offset.
    try:
        local_offset = datetime.now(timezone.utc).astimezone().strftime("%z")
        # Re-format from +HHMM to +HH:MM
        if len(local_offset) == 5:
            local_offset = f"{local_offset[:3]}:{local_offset[3:]}"
    except Exception:
        local_offset = ""
    ts = now.strftime("%Y-%m-%dT%H:%M:%S") + local_offset
    tzname = datetime.now().astimezone().tzname() or ""
    time_str = f"{now.strftime('%H:%M')} {tzname}".strip()

    broken_count, _ = count_broken_wikilinks(vault_dir)
    orphan_count, _ = count_orphans(vault_dir)
    shadow_dark = count_shadow_and_dark(vault_dir)

    # Drought-flag guard: the 23:00 → 07:00 Eastern sleep window must be
    # closed before the wake distribution is safe to draw conclusions
    # from. See ``_sleep_window_closed`` for the full rationale. The
    # field rides along on every event so the thinker prompt
    # (active.md:107) can gate the ``stage_d_drought`` emission without
    # re-deriving the time check.
    window_closed = _sleep_window_closed(now)

    event: dict[str, Any] = {
        "ts": ts,
        "type": "vault_health",
        "date": now.strftime("%Y-%m-%d"),
        "time": time_str,
        "sleep_window_closed": window_closed,
        "total_notes": count_total_notes(vault_dir),
        "broken_wikilinks": broken_count,
        "orphan_notes": orphan_count,
        "orphan_dailies_excluded": True,
        "shadow_orphan_count": shadow_dark["shadow_orphan_count"],
        "truly_dark_count": shadow_dark["truly_dark_count"],
        "frontmatter_parse_failures": shadow_dark["frontmatter_parse_failures"],
        "research_notes_last_night": count_research_notes_created_on(
            vault_dir, yesterday_midnight
        ),
        "surfaces_written_last_night": count_surfaces_in_window(
            surface_dir, yesterday_23, today_07
        ),
        "surfaces_handled_today": count_surfaces_handled_today(surface_dir, today_midnight),
        "total_wakes_last_night": count_all_wakes_in_window(
            thoughts_dir, yesterday_23, today_07
        ),
        "stage_c_candidates": count_stage_c_candidates(vault_dir, today=today_midnight),
        "wake_type_distribution": count_wakes_by_stage(
            thoughts_dir, yesterday_23, today_07
        ),
        "research_decay_count": count_research_decay(vault_dir),
        "decay_coverage": compute_decay_coverage(vault_dir, today=today_midnight),
        "access_decay": count_access_decay(vault_dir),
    }

    # Recovery state uses a 14-day rolling window ending today.
    recovery_ws = today_midnight - timedelta(days=14)
    event["recovery_state"] = compute_recovery_state(
        vault_dir,
        thoughts_dir,
        window_start=recovery_ws,
        window_end=today_midnight,
        events_path=events_path,
    )

    # Sanity-check: with trigger-keyword backfill complete, a real
    # truly_dark_count above 10 is statistically implausible. Tag the
    # event so downstream consumers (vault_linking_protocol, dashboards)
    # can distinguish a suspect reading from a clean one. See #249.
    if event["truly_dark_count"] > _TRULY_DARK_SUSPECT_THRESHOLD:
        logger.warning(
            "vault_health: truly_dark_count=%d exceeds suspect threshold (%d); "
            "tagging event parse_quality=suspect (likely silent frontmatter "
            "parse failures upstream — see issue #249)",
            event["truly_dark_count"],
            _TRULY_DARK_SUSPECT_THRESHOLD,
        )
        event["parse_quality"] = "suspect"

    # Low-wake-count detector (issue #323 fix 3). Sum Stage B+C+D wakes
    # from the morning window; if under WAKE_COUNT_THRESHOLD, tag the
    # event AND write an insight-tier surface so Speaking notices on
    # the next morning surface scan. Background: May-22 sleep cycle
    # had 3 wakes in 8 hours; healthy is 15–19.
    dist = event["wake_type_distribution"]
    total_sleep_wakes = (
        int(dist.get("stage_b", 0))
        + int(dist.get("stage_c", 0))
        + int(dist.get("stage_d", 0))
    )
    event["total_sleep_wakes"] = total_sleep_wakes
    # Same partial-window concern as the drought flag — a low count
    # before 07:00 Eastern is expected because not all sleep-phase wakes
    # have written their note yet. Only fire the low-wake surface once
    # the window has closed.
    if window_closed and total_sleep_wakes < WAKE_COUNT_THRESHOLD:
        event["low_wake_count"] = True
        try:
            _write_low_wake_count_surface(
                surface_dir=surface_dir,
                now=now,
                total_sleep_wakes=total_sleep_wakes,
                distribution=dist,
            )
        except OSError as exc:
            logger.warning(
                "vault_health: failed to write low-wake-count surface to %s: %s",
                surface_dir,
                exc,
            )

    return event


def _write_low_wake_count_surface(
    *,
    surface_dir: Path,
    now: datetime,
    total_sleep_wakes: int,
    distribution: dict[str, int],
) -> Path:
    """Drop an insight-tier surface flagging low overnight wake count.

    Insight-tier (not flash) — the symptom is structural, not urgent;
    Speaking's morning surface sweep will pick it up alongside the rest
    of the vault_health-driven nudges.

    Filename: ``<YYYY-MM-DD-HHMMSS>-low-wake-count.md`` (matches the
    surface naming convention used by ``design_pipeline.write_surface``
    and the rest of the inner/surface ecosystem).
    """
    surface_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    path = surface_dir / f"{stamp}-low-wake-count.md"

    body = (
        f"Stage B + C + D wakes during the morning window summed to "
        f"`{total_sleep_wakes}`, below the threshold of "
        f"`{WAKE_COUNT_THRESHOLD}`. A healthy 8h sleep cycle produces "
        f"15–19 wakes; persistently low counts indicate the wake-cadence "
        f"backoff ladder ran away or the s6 supervisor stalled.\n\n"
        f"Per-stage distribution: "
        f"B={int(distribution.get('stage_b', 0))}, "
        f"C={int(distribution.get('stage_c', 0))}, "
        f"D={int(distribution.get('stage_d', 0))}.\n\n"
        f"**Suggested next step**: check the backoff history in "
        f"`memory/events.jsonl` (filter for `type: wake_start` / "
        f"`type: vault_health`) and the supervisor logs under "
        f"`/state/worker/`. If the ladder spent the night pegged at "
        f"`MAX_INTERVAL_SECONDS` despite Stage B/D writes, the "
        f"`did_work` signal probably isn't being emitted — see "
        f"issue #323 fix 0.\n"
    )

    lines = [
        "---",
        "priority: insight",
        (
            "context: Wake count fell below threshold, indicating "
            "possible backoff runaway or scheduler issue"
        ),
        "reply_expected: false",
        "type: low-wake-count",
        f"total_sleep_wakes: {total_sleep_wakes}",
        f"threshold: {WAKE_COUNT_THRESHOLD}",
        "---",
        "",
        body,
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI


def _parse_iso(s: str) -> datetime:
    # ``datetime.fromisoformat`` handles offsets in 3.11+. Fall back to
    # naive parse if the string is already naive.
    return datetime.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compute vault-health metrics and (optionally) append a "
            "vault_health event to memory/events.jsonl."
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
        "--surface",
        type=Path,
        default=None,
        help=(
            "Path to inner/surface. Defaults to <thoughts>/../surface. "
            "Used for surfaces_written_last_night and surfaces_handled_today."
        ),
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
    parser.add_argument(
        "--events",
        type=Path,
        default=None,
        help="Path to memory/events.jsonl. Required for recovery_state.",
    )
    parser.add_argument(
        "--check-existing",
        action="store_true",
        help=(
            "Read --events and exit 0 (no-op) if a vault_health event for "
            "today already exists. Authoritative dedup; replaces the bash "
            "grep workaround the morning scan used to run."
        ),
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help=(
            "Append the assembled event as one JSON line to --events. "
            "Combined with --check-existing, this is the entire morning "
            "vault_health write path — no shell-side JSON assembly."
        ),
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Emit continuous structural-health checks (zero_edge_notes, "
            "trigger_gap_notes) under a top-level `continuous_checks` key. "
            "See design note [[2026-05-20-continuous-structural-health-check]]."
        ),
    )
    args = parser.parse_args(argv)

    # Single-command morning-scan mode: when --check-existing or --append
    # is set, the module owns the full event assembly + dedup + write.
    if args.check_existing or args.append:
        if args.thoughts is None:
            parser.error("--check-existing/--append require --thoughts")
        if args.events is None:
            parser.error("--check-existing/--append require --events")

        now = _local_now()
        today_str = now.strftime("%Y-%m-%d")

        # Window-not-closed gate. The wake-counting window is
        # yesterday-23:00 → today-07:00 Eastern. If the scan fires before
        # 07:00 (the first sleep-mode thinker wake of the morning, or a
        # mid-night wake that happens to hit the active.md preamble),
        # the Stage B/C/D wake files for the night don't all exist on
        # disk yet — Stage D writes cluster 00:27–02:20. The undercount
        # produced false ``stage_d_drought: true`` flags and bad totals
        # in every wake_type_distribution. Silent skip so the next wake
        # after 07:00 (active-mode 5-min cadence) runs the scan with a
        # closed, complete window. Pairs with the dedup check below so
        # exactly one event lands per day.
        #
        # The check goes through ``_sleep_window_closed`` so it's
        # timezone-aware: the alice container runs in UTC, but the
        # sleep schedule is Eastern. A naive ``now < today_07`` compare
        # used to let the scan through at, e.g., 03:30 EDT (= 07:30 UTC)
        # because the wall-clock arithmetic was UTC-relative.
        #
        # The gate is unconditional on the single-command-mode path:
        # any pre-window invocation (``--check-existing``, ``--append``,
        # or both) silent-skips. A bare ``--append`` call from an active
        # thinking wake at 00:14 had previously bypassed the gate and
        # written a partial-window event with the wrong
        # ``wake_type_distribution``.
        if not _sleep_window_closed(now):
            return 0

        if args.check_existing and vault_health_event_exists_for_date(
            args.events, today_str
        ):
            # Today's event already on disk. Silent no-op so the morning
            # scan can call this unconditionally.
            return 0

        event = build_vault_health_event(
            vault_dir=args.vault,
            thoughts_dir=args.thoughts,
            events_path=args.events,
            surface_dir=args.surface,
            now=now,
        )
        if args.continuous:
            event["continuous_checks"] = compute_continuous_checks(
                args.vault, today=now,
            )

        if args.append:
            _append_event(args.events, event)
        else:
            print(json.dumps(event))
        return 0

    # Legacy mode: emit partial-metric JSON for ad-hoc inspection.
    out: dict[str, Any] = {
        "total_notes": count_total_notes(args.vault),
    }
    broken_count, _broken = count_broken_wikilinks(args.vault)
    out["broken_wikilinks"] = broken_count
    orphan_count, _orphans = count_orphans(args.vault)
    out["orphan_notes"] = orphan_count

    shadow_dark = count_shadow_and_dark(args.vault)
    out["shadow_orphan_count"] = shadow_dark["shadow_orphan_count"]
    out["truly_dark_count"] = shadow_dark["truly_dark_count"]
    out["frontmatter_parse_failures"] = shadow_dark["frontmatter_parse_failures"]

    # Research note decay: notes older than 60 days with < 2 inbound links.
    out["research_decay_count"] = count_research_decay(args.vault)

    # Decay coverage (Layer 1 blind-spot detection): % of activation-era
    # decayed pool that has been accessed since the cue runner came online.
    out["decay_coverage"] = compute_decay_coverage(args.vault)

    # Behavioral decay: notes with zero access older than 5 days.
    out["access_decay"] = count_access_decay(args.vault)

    if args.thoughts and args.window_start and args.window_end:
        out["wake_type_distribution"] = count_wakes_by_stage(
            args.thoughts, args.window_start, args.window_end
        )
        # Recovery state always uses a full 14-day rolling window,
        # regardless of the short wake window used for wake counting.
        _we = _strip_tz(args.window_end)
        _recovery_ws = _we - timedelta(days=14)
        _recovery_we = _we
        out["recovery_state"] = compute_recovery_state(
            args.vault,
            args.thoughts,
            window_start=_recovery_ws,
            window_end=_recovery_we,
            events_path=args.events,
        )
    if args.continuous:
        out["continuous_checks"] = compute_continuous_checks(args.vault)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
