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

- Production mode (``_DRY_RUN = False``) as of 2026-06-24, after dry-run
  review and clean validation runs on 2026-06-23.
- Per-note limit: max 10 correction links added per note per run.
- Idempotent: checks for existing ``[[correction-slug]]`` in the body
  before writing. Running twice produces no second changes.
- Uses ``vault_lock.acquire()`` for per-file write serialization.
- All writes within a single ``auto_propagate()`` call are independent
  (no transaction — if one write fails, other notes are still updated).
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

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
        if rel_parts and rel_parts[0] in ("dailies", "archive", "gh-state", "experiments"):
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

#: Dry-run mode. Set to ``False`` after manual review of dry-run output;
#: validated by clean production runs on 2026-06-23 (severity filter correct,
#: 0 low-severity propagated).
_DRY_RUN = False

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

#: Timezone for event timestamps (events.jsonl convention).
_EDT = ZoneInfo("America/New_York")


def _write_propagation_event(
    mind: pathlib.Path,
    *,
    total_resolved: int,
    pairs_affected: int,
    mode: str,
    duration_seconds: float,
    high_severity: int,
    medium_severity: int,
    low_severity: int,
    trigger: str = "stage_c",
) -> None:
    """Write a ``correction_cascade_auto_propagate`` event to events.jsonl.

    Called at the end of :func:`auto_propagate` to log the run. The write
    is non-fatal: the propagation has already happened, so a failed event
    write is logged and swallowed rather than raised.

    Severity counts reflect what was *actually propagated* (not what was
    detected). Low-severity entries are filtered out before propagation
    and do not appear in the propagated count.

    ``trigger`` debuggability tag: defaults to ``"stage_c"`` (the original
    nightly call site). Per-wake hook invocations pass
    ``trigger="periodic_wake"`` so the source is identifiable in
    events.jsonl.
    """
    now = datetime.now(_EDT)
    event = {
        "ts": now.isoformat(),
        "type": "correction_cascade_auto_propagate",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M EDT"),
        "total_resolved": total_resolved,
        "pairs_affected": pairs_affected,
        "mode": mode,
        "duration_seconds": round(duration_seconds, 2),
        "high_severity": high_severity,
        "medium_severity": medium_severity,
        "low_severity": low_severity,
        "trigger": trigger,
    }
    events_path = mind / "memory" / "events.jsonl"
    try:
        with open(events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.warning(
            "auto-propagate: event write failed for %s: %s",
            event["type"],
            exc,
        )


def _already_references(
    md: pathlib.Path, correction_slug: str, correction_stem: str | None = None
) -> bool:
    """Check if the note body already contains a wikilink to *correction_slug*.

    Idempotency guard: if the link is already present by either the
    frontmatter slug or the filename stem form, skip. This mirrors the
    dual-form resolution in ``correction_cascade.detect_corrections``
    so that notes referencing a correction by stem are not falsely
    flagged as unpropagated.
    """
    _, body = _frontmatter_read(md)
    if f"[[{correction_slug}" in body:
        return True
    if correction_stem and correction_stem != correction_slug:
        return f"[[{correction_stem}" in body
    return False


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
    correction_stem: str | None = None,
    *,
    severity: str = "high",
    dry_run: bool = _DRY_RUN,
) -> bool:
    """Append a correction wikilink to the note's backlinks section.

    Returns ``True`` if the note would be (or was) changed, ``False``
    if the note was unchanged (already referenced).

    In dry-run mode the note is NOT written; the return value indicates
    what *would* change.

    Dual-form resolution: checks both the frontmatter slug and the
    filename stem form to avoid adding a duplicate wikilink when a
    note references the correction by the stem form.

    Severity markers: medium-severity corrections receive a ``[medium]``
    tag after the wikilink so the reader knows the correction is
    qualitative rather than quantitative. High-severity corrections
    have no marker (they are quantitative and unambiguous).
    """
    text = md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    # Idempotency: already has the link (slug or stem form)?
    if f"[[{correction_slug}" in body:
        return False
    if correction_stem and correction_stem != correction_slug:
        if f"[[{correction_stem}" in body:
            return False

    # Build the backlink line, with optional severity marker.
    severity_tag = f" [{severity}]" if severity == "medium" else ""
    backlink_line = f"- [[{correction_slug}|{correction_title}]]{severity_tag}\n"

    # Try to find the Backlinks section
    backlinks_match = re.search(r"^(## Backlinks\s*\n)", text, re.MULTILINE)
    if backlinks_match:
        # Append to existing Backlinks section
        pos = backlinks_match.end()
        new_text = text[:pos] + backlink_line + text[pos:]
    else:
        # No Backlinks section — create one
        new_backlinks = f"\n## Backlinks\n\n{backlink_line}"
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
    trigger: str = "stage_c",
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
    trigger
        Debuggability tag forwarded into the events.jsonl event so the
        call site is identifiable. Defaults to ``"stage_c"`` (the
        original nightly call site). The per-wake hook passes
        ``"periodic_wake"``.

    Returns
    -------
    dict[str, int]
        Mapping of ``referencing_slug → corrections_added``.
    """
    vault = mind / "cortex-memory"
    global _DRY_RUN
    if dry_run is not None:
        _DRY_RUN = dry_run

    wall_start = time.time()

    changes: dict[str, int] = {}
    total_added = 0
    skipped = 0
    corrected_by_updates = 0
    propagated_high = 0
    propagated_medium = 0

    # Group by referencing note. Only propagate high and medium severity;
    # low-severity corrections are not auto-propagated (design spec:
    # low = nuance added, edge-case corrections may not apply to all
    # referencing notes — let Speaking decide case-by-case).
    by_ref: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    # Track which corrected notes we need to update corrected_by for
    corrected_targets: dict[str, str] = {}  # corrected_slug -> correction_slug
    for u in report.unpropagated:
        if u.severity == "low":
            skipped += 1
            continue
        by_ref[u.referencing_slug].append(
            (u.correction_slug, u.correction_title, u.corrected_slug, u.severity)
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
        for corr_slug, corr_title, _corrected_slug, corr_severity in corrections:
            # Resolve the correction note to get its filename stem,
            # matching the dual-form pattern from detect_corrections.
            corr_md = _try_resolve_slug(corr_slug, vault)
            if corr_md is None:
                corr_md = _resolve_slug_by_frontmatter(corr_slug, vault)
            corr_stem: str | None = None
            if corr_md is not None:
                stem = corr_md.stem
                if stem != corr_slug:
                    corr_stem = stem
            if _already_references(ref_md, corr_slug, corr_stem):
                continue
            if _add_backlink(
                ref_md, corr_slug, corr_title, corr_stem,
                severity=corr_severity, dry_run=_DRY_RUN,
            ):
                added += 1
                if corr_severity == "high":
                    propagated_high += 1
                else:
                    propagated_medium += 1

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
        if _update_corrected_by(corrected_md, correction_slug, dry_run=_DRY_RUN):
            corrected_by_updates += 1

    wall_elapsed = time.time() - wall_start

    logger.info(
        "auto-propagate: added %d correction links to %d notes, "
        "updated corrected_by: on %d notes, skipped %d (low-severity)",
        total_added,
        len(changes),
        corrected_by_updates,
        skipped,
    )

    _write_propagation_event(
        mind,
        total_resolved=total_added,
        pairs_affected=len(changes),
        mode="dry-run" if _DRY_RUN else "production",
        duration_seconds=wall_elapsed,
        high_severity=propagated_high,
        medium_severity=propagated_medium,
        low_severity=skipped,
        trigger=trigger,
    )

    return changes
