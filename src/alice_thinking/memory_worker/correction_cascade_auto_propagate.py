"""Auto-propagate corrections to referencing notes.

Runs after correction cascade detection during Stage C grooming.
Adds correction wikilinks to notes that cite the corrected note
but not the correction itself.

Design: ``cortex-memory/research/2026-06-11-correction-cascade-auto-propagation-design.md``

This is a **structural propagation**, not a semantic one. The algorithm
adds the link; it does not verify that the correction actually addresses
the claim in the referencing note.

Integration
-----------

Runs after detection in the Stage C grooming pipeline::

    link audit → correction cascade detection →
        correction cascade auto-propagation → dedupe

Example usage::

    from alice_thinking.memory_worker.correction_cascade import detect_corrections
    from alice_thinking.memory_worker.correction_cascade_auto_propagate import auto_propagate

    report = detect_corrections(mind)
    if report.total_unpropagated > 0:
        auto_propagate(mind, report)

Safety
------

- Dry-run mode by default (``_DRY_RUN = True``). First 3 runs should
  stay dry. Toggle to ``False`` only after manual review of dry-run output.
- Per-note limit: max 10 correction links added per note per run.
- Idempotent: checks for existing ``[[correction-slug]]`` in the body
  before writing. Running twice produces no second changes.
- Uses ``vault_lock.acquire()`` for per-file write serialization.
- All writes within a single ``auto_propagate()`` call are independent
  (no transaction — if one write fails, other notes are still updated).
"""

from __future__ import annotations

import logging
import pathlib
import re
import time
from collections import defaultdict
from typing import Optional

from indexer.yaml_lite import split_frontmatter

from alice_thinking import vault_lock
from alice_thinking.memory_worker.correction_cascade import (
    CascadeReport,
    _frontmatter_read,
    _slug_of,
    _try_resolve_slug,
)

# Lazy cache: slug -> pathlib.Path, built on first miss from _try_resolve_slug
_slug_cache: dict[str, pathlib.Path] = {}


def _resolve_slug_by_frontmatter(slug: str, vault: pathlib.Path) -> Optional[pathlib.Path]:
    """Fallback: search all vault notes for one whose frontmatter ``slug:`` matches.

    Called only when the fast-path (filename match) fails. O(vault) but
    cached after first lookup.
    """
    global _slug_cache
    if slug in _slug_cache:
        return _slug_cache[slug]

    clean = slug.strip()
    # Strip wikilink / YAML syntax (defensive)
    if clean.startswith("[[") and clean.endswith("]]"):
        clean = clean[2:-2].strip()
    elif clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1].strip()

    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if rel_parts and rel_parts[0] in ("dailies", "archive", "gh-state"):
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        if md.name in ("index.md", "README.md", "unresolved.md"):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        raw = fm.get("slug")
        if isinstance(raw, str) and raw.strip().lower() == clean.lower():
            _slug_cache[slug] = md
            return md

    _slug_cache[slug] = None  # memoize miss
    return None

logger = logging.getLogger(__name__)

#: Max correction links to add per note in a single run.
_MAX_CORRECTIONS_PER_NOTE = 10

#: Dry-run mode (default for first 3 runs). Set to ``False`` after
#: manual review of dry-run output.
_DRY_RUN = True

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _already_references(md: pathlib.Path, correction_slug: str) -> bool:
    """Check if the note body already contains a wikilink to *correction_slug*.

    Idempotency guard: if the link is already present, skip.
    """
    _, body = _frontmatter_read(md)
    return f"[[{correction_slug}" in body


def _write_updated_frontmatter(fm: dict, body: str) -> str:
    """Render frontmatter + body back to text, bumping ``updated:``."""
    fm["updated"] = time.strftime("%Y-%m-%d %H:%M EDT")
    out = "---\n"
    for k, v in fm.items():
        if isinstance(v, list):
            out += f"{k}:\n"
            for item in v:
                out += f"  - {item}\n"
        else:
            out += f"{k}: {v}\n"
    out += "---\n\n"
    out += body
    return out


def _add_backlink(
    md: pathlib.Path,
    correction_slug: str,
    correction_title: str,
    *,
    dry_run: bool = _DRY_RUN,
) -> bool:
    """Append a correction wikilink to the note's backlinks section.

    Returns ``True`` if the note would be (or was) changed, ``False``
    if the note was unchanged (already referenced).

    In dry-run mode the note is NOT written; the return value indicates
    what *would* change.
    """
    text = md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    # Idempotency: already has the link?
    if f"[[{correction_slug}" in body:
        return False

    # Try to find the Backlinks section
    backlinks_match = re.search(r"^(## Backlinks\s*\n)", text, re.MULTILINE)
    if backlinks_match:
        # Append to existing Backlinks section
        pos = backlinks_match.end()
        new_text = (
            text[:pos]
            + f"- [[{correction_slug}|{correction_title}]]\n"
            + text[pos:]
        )
    else:
        # No Backlinks section — create one
        new_backlinks = (
            f"\n## Backlinks\n\n"
            f"- [[{correction_slug}|{correction_title}]]\n"
        )
        changelog_match = re.search(
            r"^(## Changelog\s*\n.*?)(?=\n##|\Z)", text, re.MULTILINE | re.DOTALL
        )
        if changelog_match:
            # Insert before Changelog
            pos = changelog_match.start()
            new_text = text[:pos] + new_backlinks + text[pos:]
        else:
            # Append at end
            suffix = text.rstrip()
            if not suffix.endswith("\n"):
                suffix += "\n"
            new_text = suffix + new_backlinks

    # Bump updated timestamp
    new_fm, new_body = split_frontmatter(new_text)
    new_text = _write_updated_frontmatter(new_fm, new_body)

    if dry_run:
        logger.info(
            "auto-propagate (dry-run): would add [[%s]] to [[%s]]",
            correction_slug,
            _slug_of(md),
        )
        return True  # would change

    with vault_lock.acquire(md, mode=vault_lock.LockMode.EXCLUSIVE):
        md.write_text(new_text, encoding="utf-8")
        logger.info(
            "auto-propagate: added [[%s]] to [[%s]]",
            correction_slug,
            _slug_of(md),
        )

    return True


def _update_corrected_by(
    corrected_md: pathlib.Path,
    correction_slug: str,
    *,
    dry_run: bool = _DRY_RUN,
) -> bool:
    """Ensure *correction_slug* appears in the corrected note's ``corrected_by:`` list.

    Returns ``True`` if the note would be (or was) changed, ``False``
    if already present.
    """
    text = corrected_md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    corrected_by = fm.get("corrected_by")
    if isinstance(corrected_by, list):
        if any(str(s).strip() == correction_slug for s in corrected_by):
            return False  # already present
        corrected_by.append(correction_slug)
        fm["corrected_by"] = corrected_by
    elif isinstance(corrected_by, str):
        if corrected_by.strip() == correction_slug:
            return False
        fm["corrected_by"] = [corrected_by.strip(), correction_slug]
    else:
        fm["corrected_by"] = [correction_slug]

    new_text = _write_updated_frontmatter(fm, body)

    if dry_run:
        logger.info(
            "auto-propagate (dry-run): would add %s to corrected_by: of [[%s]]",
            correction_slug,
            _slug_of(corrected_md),
        )
        return True  # would change

    with vault_lock.acquire(corrected_md, mode=vault_lock.LockMode.EXCLUSIVE):
        corrected_md.write_text(new_text, encoding="utf-8")
        logger.info(
            "auto-propagate: added %s to corrected_by: of [[%s]]",
            correction_slug,
            _slug_of(corrected_md),
        )

    return True


def auto_propagate(
    mind: pathlib.Path,
    report: CascadeReport,
    *,
    dry_run: Optional[bool] = None,
) -> dict[str, int]:
    """Auto-propagate corrections from a detection report.

    For each unpropagated correction in *report*, adds a wikilink to
    the referencing note's backlinks section (if not already present)
    and ensures the correction slug appears in the corrected note's
    ``corrected_by:`` field.

    Parameters
    ----------
    mind
        The alice-mind root path.
    report
        Detection report with unpropagated corrections.
    dry_run
        Override the module-level ``_DRY_RUN`` setting. ``None`` uses
        the default.

    Returns
    -------
    dict[str, int]
        Mapping of ``referencing_slug → corrections_added``.
    """
    vault = mind / "cortex-memory"
    global _DRY_RUN
    if dry_run is not None:
        _DRY_RUN = dry_run

    changes: dict[str, int] = {}
    total_added = 0
    skipped = 0
    corrected_by_updates = 0

    # Group by referencing note
    by_ref: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    # Track which corrected notes we need to update corrected_by for
    corrected_targets: dict[str, str] = {}  # corrected_slug -> correction_slug
    for u in report.unpropagated:
        by_ref[u.referencing_slug].append(
            (u.correction_slug, u.correction_title, u.corrected_slug)
        )
        corrected_targets[u.corrected_slug] = u.correction_slug

    for ref_slug, corrections in by_ref.items():
        # Apply per-note limit
        if len(corrections) > _MAX_CORRECTIONS_PER_NOTE:
            logger.warning(
                "auto-propagate: %s has %d pending corrections (limit %d), skipping",
                ref_slug,
                len(corrections),
                _MAX_CORRECTIONS_PER_NOTE,
            )
            skipped += 1
            continue

        # Resolve path: fast path (filename) then frontmatter fallback
        ref_md = _try_resolve_slug(ref_slug, vault)
        if ref_md is None:
            ref_md = _resolve_slug_by_frontmatter(ref_slug, vault)
        if ref_md is None:
            logger.warning("auto-propagate: %s not found, skipping", ref_slug)
            skipped += 1
            continue

        added = 0
        for corr_slug, corr_title, _corrected_slug in corrections:
            if _already_references(ref_md, corr_slug):
                continue
            if _add_backlink(ref_md, corr_slug, corr_title, dry_run=dry_run):
                added += 1

        if added > 0:
            changes[ref_slug] = added
            total_added += added

    # Update corrected_by: on corrected notes
    for corrected_slug, correction_slug in corrected_targets.items():
        corrected_md = _try_resolve_slug(corrected_slug, vault)
        if corrected_md is None:
            logger.warning(
                "auto-propagate: corrected note %s not found, skipping corrected_by update",
                corrected_slug,
            )
            continue
        if _update_corrected_by(corrected_md, correction_slug, dry_run=dry_run):
            corrected_by_updates += 1

    logger.info(
        "auto-propagate: added %d correction links to %d notes, "
        "updated corrected_by: on %d notes, skipped %d",
        total_added,
        len(changes),
        corrected_by_updates,
        skipped,
    )

    return changes
