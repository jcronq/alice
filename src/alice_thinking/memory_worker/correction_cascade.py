"""Correction cascade detection — mechanical detection of unpropagated
corrections during grooming.

When a research note corrects an earlier finding, the correction exists
in isolation — earlier notes that cited the original claim are not
updated. This module identifies correction cascades during grooming and
flags notes with unpropagated claims.

Design: ``cortex-memory/research/2026-06-11-correction-cascade-detection-design.md``
Pattern: ``cortex-memory/research/2026-06-11-correction-cascade-pattern.md``

Integration
-----------

Runs during the **link audit** phase (after atomize, before dedupe).
Results are added to the grooming report surface. High-severity
findings are surfaced to Speaking via ``inner/surface/``.

Algorithm
---------

1. Find all notes with ``note_type: correction`` in frontmatter or
   files named ``*-correction*.md``.
2. For each correction, find the corrected note via the
   ``corrected_by:`` field in the corrected note's frontmatter, or
   the ``references:`` field in the correction note.
3. Check for backlink propagation: for each pair
   (corrected_note → correction_note), find all notes that reference
   corrected_note and check if they also reference correction_note.
4. Classify severity (high/medium/low).
5. Output flagged pairs.

Limitations
-----------

- Detection is structural, not semantic. The algorithm detects missing
  backlinks but doesn't verify that the correction actually addresses
  the claim in the referencing note.
- Manual verification needed. The module flags pairs; it doesn't
  automatically update notes.
- Performance: O(n) per correction pair across the vault.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import time
from collections import defaultdict
from typing import Any, Optional

from indexer.yaml_lite import _strip_code, split_frontmatter



logger = logging.getLogger(__name__)

#: Vault-lock timeout for any single file read during backlink
#: scanning. We only read (never write) during detection, so a short
#: timeout is fine — if a thinking-side write blocks us, the next
#: cycle retries.
_CASCADE_LOCK_TIMEOUT = 3.0

#: Severity classification thresholds.
#:
#: High = quantitative claim changed (numbers, percentages, dates).
#: Medium = qualitative claim changed (interpretation, conclusion).
#: Low = nuance or edge case added.
#:
#: The classifier uses keyword heuristics since we can't do semantic
#: analysis without an LLM.
_HIGH_KEYWORDS = frozenset({
    "100%", "0%", "50%", "12%", "23%", "1000", "500", "100",
    "percent", "count", "number", "total", "all", "none",
    "every", "each", "always", "never", "zero",
    "n=", "sampled", "population", "genuine",
})
_MEDIUM_KEYWORDS = frozenset({
    "incorrect", "wrong", "misleading", "oversimplified",
    "overstated", "understated", "biased", "skewed",
    "artifact", "erroneous", "spurious", "false",
    "actually", "in fact", "however", "but", "contrary",
    "correction", "correction of", "supersedes",
})


# ---------- data types ----------


@dataclasses.dataclass
class UnpropagatedCorrection:
    """A single flagged correction cascade."""

    corrected_slug: str
    corrected_title: str
    correction_slug: str
    correction_title: str
    referencing_slug: str
    referencing_title: str
    severity: str  # "high" | "medium" | "low"
    claim_changed: str


@dataclasses.dataclass
class CascadeReport:
    """Aggregated results from a detection run."""

    correction_pairs_checked: int = 0
    unpropagated: list[UnpropagatedCorrection] = dataclasses.field(default_factory=list)

    @property
    def total_unpropagated(self) -> int:
        return len(self.unpropagated)

    @property
    def high_count(self) -> int:
        return sum(1 for u in self.unpropagated if u.severity == "high")

    @property
    def medium_count(self) -> int:
        return sum(1 for u in self.unpropagated if u.severity == "medium")

    @property
    def low_count(self) -> int:
        return sum(1 for u in self.unpropagated if u.severity == "low")

    def to_dict(self) -> dict[str, Any]:
        return {
            "correction_pairs_checked": self.correction_pairs_checked,
            "total_unpropagated": self.total_unpropagated,
            "high": self.high_count,
            "medium": self.medium_count,
            "low": self.low_count,
            "items": [
                {
                    "corrected": u.corrected_slug,
                    "correction": u.correction_slug,
                    "referencing": u.referencing_slug,
                    "severity": u.severity,
                    "claim_changed": u.claim_changed,
                }
                for u in self.unpropagated
            ],
        }

    def to_markdown_table(self) -> str:
        """Render as a markdown table for the grooming report surface."""
        if not self.unpropagated:
            return "No unpropagated corrections detected."
        lines = [
            "| Corrected Note | Correction Note | Referencing Note | Severity | Claim Changed |",
            "|---------------|----------------|-----------------|----------|--------------|",
        ]
        for u in self.unpropagated:
            lines.append(
                f"| [[{u.corrected_slug}]] "
                f"| [[{u.correction_slug}]] "
                f"| [[{u.referencing_slug}]] "
                f"| {u.severity} "
                f"| {u.claim_changed} "
            )
        return "\n".join(lines)


# ---------- helpers ----------


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _extract_wikilink_targets(body: str) -> list[str]:
    """Pull every ``[[target]]`` slug out of ``body``, ignoring code fences.

    Uses ``_strip_code`` from yaml_lite to remove fenced code blocks and
    inline code spans before matching — prevents false positives from bash
    ``[[ -d ]]`` tests, markdown examples like ``[[wikilinks]]``, and
    double-backtick spans.
    """
    cleaned = _strip_code(body)
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(cleaned)]


def _slug_of(md: pathlib.Path) -> str:
    """Slug used by wikilinks — explicit ``slug:`` frontmatter overrides
    if present; otherwise filename stem."""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return md.stem
    fm, _body = split_frontmatter(text)
    raw = fm.get("slug")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return md.stem


def _frontmatter_read(md: pathlib.Path) -> tuple[dict[str, Any], str]:
    """Read frontmatter and body from a vault note."""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    return split_frontmatter(text)


def _title_of(fm: dict[str, Any], slug: str) -> str:
    return str(fm.get("title") or slug)


def _extract_quantitative_claims(body: str) -> list[str]:
    """Extract quantitative claims (numbers, percentages) from body text.

    Returns a list of claim strings found in the body.
    """
    # Match percentages, numbers with context
    claims: list[str] = []
    for m in re.finditer(
        r"(\d+\.?\d*\s*(?:percent|%|of|in|from|to)\s*\w*(?:\s+\w*){0,3})",
        body,
        re.IGNORECASE,
    ):
        claims.append(m.group(1).strip())
    # Also match "n=..." patterns
    for m in re.finditer(r"n\s*=\s*(\d+)", body, re.IGNORECASE):
        claims.append(m.group(1))
    return claims


def _has_specific_quantitative_correction(correction_body: str) -> tuple[bool, str]:
    """Check if the correction body contains a specific quantitative
    claim change (as opposed to a generic qualitative statement).

    Returns (has_quantitative, claim_description).

    Specific quantitative claims include:
    - Ratios/percentages with counts: "98.1% (159/162)"
    - Multipliers: "0.1x as likely"
    - Exact counts: "n=100", "n=617"
    - Comparative statistics: "X% vs Y%"

    Avoids generic patterns like "100% domain classified" (coverage
    metric) by requiring the number to be part of a comparative or
    measurement claim.
    """
    # Specific ratio/percentage with count: "98.1% (159/162)"
    # Ratio format (X/Y) is the key signal — a single count in parens
    # (e.g. "100% (104 notes)") is just a coverage metric, not a
    # comparative measurement claim.
    for m in re.finditer(r'(\d+\.?\d*%\s*\(\d+/\d+\))', correction_body):
        return True, m.group(1).strip()

    # Multiplier claim: "0.1x as likely"
    for m in re.finditer(r'(\d+\.?\d*x\s+as\s+likely)', correction_body):
        return True, m.group(1).strip()

    # Ratio in opposite direction: "X/Y vs A/B"
    for m in re.finditer(r'(\d+/\d+\s*\(\d+/\d+\))', correction_body):
        return True, m.group(1).strip()

    # "n=X" patterns in context of measurement
    for m in re.finditer(r'n\s*=\s*(\d+)', correction_body):
        # Check if there's a "vs" or "vs" context nearby
        start = max(0, m.start() - 100)
        end = min(len(correction_body), m.end() + 100)
        context = correction_body[start:end].lower()
        if "vs" in context or "control" in context or "compared" in context:
            return True, f"n={m.group(1)}"

    # Comparative percentages: "X% vs Y%" or "X% compared to Y%"
    for m in re.finditer(r'(\d+\.?\d*%\s+(?:vs|compared to|versus)\s+\d+\.?\d*%)', correction_body):
        return True, m.group(1).strip()

    return False, ""


# ---------- core detection ----------


def _classify_severity(
    correction_body: str, corrected_body: str, ref_body: str
) -> tuple[str, str]:
    """Classify severity of an unpropagated correction.

    Returns (severity, claim_changed).

    High: the correction body contains a specific quantitative claim
    change (ratio like "98.1% (159/162)", multiplier like "0.1x").
    The presence of a specific quantitative correction inherently
    means the original claim was quantitative and is now wrong.

    Medium: the correction body contains qualitative changes
    (interpretation, conclusion changes) without specific numbers.

    Low: the correction adds a nuance or edge case.
    """
    has_quant, claim_desc = _has_specific_quantitative_correction(correction_body)
    if has_quant:
        return "high", claim_desc

    corr_lower = correction_body.lower()
    # Medium: qualitative correction keywords
    if any(kw in corr_lower for kw in _MEDIUM_KEYWORDS):
        return "medium", "qualitative claim corrected"

    # Low: everything else
    return "low", "nuance added"


def _find_correction_notes(vault: pathlib.Path) -> list[pathlib.Path]:
    """Find all notes that are corrections.

    A note is a correction if:
    1. It has ``note_type: correction`` in frontmatter, OR
    2. Its filename contains ``-correction-`` (case-insensitive).
    """
    corrections: list[pathlib.Path] = []
    if not vault.is_dir():
        return corrections

    for md in vault.rglob("*.md"):
        # Skip non-groomable paths
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

        note_type = str(fm.get("note_type") or "").strip().lower()
        if note_type == "correction":
            corrections.append(md)
            continue

        # Filename check (case-insensitive): stem contains "correction"
        # as a standalone word (preceded by hyphen or start of stem,
        # followed by hyphen or end of stem). This catches patterns
        # like "note-correction.md" and "note-correction-extra.md"
        # but not "miscorrection.md".
        stem_lower = md.stem.lower()
        if re.search(r"(^|-)correction(-|$)", stem_lower):
            corrections.append(md)

    return corrections


def _find_corrected_note(
    correction_md: pathlib.Path,
    vault: pathlib.Path,
) -> Optional[pathlib.Path]:
    """Given a correction note, find the note it corrects.

    Strategy order (fastest first):
    1. ``supersedes:`` field on the correction note — O(1), explicit target.
    2. ``corrected_note:`` field on the correction note — O(1), explicit target.
    3. Scan all vault notes for ``corrected_by:`` referencing this correction —
       canonical trace, but O(vault). Fallback when strategies 1-2 fail.
    4. ``references:`` field on the correction note — O(1), heuristic.
    5. Wikilink fallback from correction body — O(1), last resort.

    The ``supersedes:`` field is the highest-priority because it is written
    by the correction author and points directly at the target. The
    ``corrected_by:`` field is the canonical trace (written by the corrected
    note's author), but requires scanning all vault notes — hence it is
    strategy 3, not strategy 1.

    Returns None if no corrected note can be determined.
    """
    correction_slug = _slug_of(correction_md)
    _fm, corr_body = _frontmatter_read(correction_md)

    # Strategy 1: check ``supersedes:`` on the correction note.
    # Highest priority — explicit, O(1), written by correction author.
    supersedes = _fm.get("supersedes")
    if supersedes:
        target = _resolve_field_value(supersedes, vault)
        if target:
            return target

    # Strategy 2: check ``corrected_note:`` on the correction note.
    # Also explicit, O(1).
    corrected_note = _fm.get("corrected_note")
    if corrected_note:
        target = _resolve_field_value(corrected_note, vault)
        if target:
            return target

    # Strategy 3: scan all vault notes for ``corrected_by:`` referencing
    # this correction. Canonical trace — the corrected note explicitly
    # tracks who corrected it. O(vault), fallback when strategies 1-2 fail.
    if vault.is_dir():
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
            corrected_by = fm.get("corrected_by")
            if isinstance(corrected_by, list):
                if any(str(s).strip() == correction_slug for s in corrected_by):
                    return md
            elif isinstance(corrected_by, str):
                if corrected_by.strip() == correction_slug:
                    return md

    # Strategy 4: check ``references:`` on the correction note.
    # Heuristic — the first reference might be the corrected note.
    references = _fm.get("references")
    if references:
        target = _resolve_field_value(references, vault)
        if target:
            return target

    # Strategy 5: look in the correction body for wikilinks to other
    # notes. The first wikilink that isn't this note itself is likely
    # the corrected note.
    _, body = _frontmatter_read(correction_md)
    targets = _extract_wikilink_targets(body)
    for target_slug in targets:
        base = target_slug.rsplit("/", 1)[-1]
        target = vault / f"{base}.md"
        if target.is_file() and target != correction_md:
            return target
        for folder in ("projects", "reference", "research"):
            candidate = vault / folder / f"{base}.md"
            if candidate.is_file():
                return candidate

    return None


def _resolve_field_value(val, vault: pathlib.Path) -> Optional[pathlib.Path]:
    """Resolve a frontmatter field value (list or string) to a vault path.

    Handles bare slugs and wikilink syntax ``[[slug]]``.
    """
    if isinstance(val, list):
        for item in val:
            ref_slug = str(item).strip()
            target = _try_resolve_slug(ref_slug, vault)
            if target:
                return target
    elif isinstance(val, str) and val.strip():
        target = _try_resolve_slug(val.strip(), vault)
        if target:
            return target
    return None


def _try_resolve_slug(slug: str, vault: pathlib.Path) -> Optional[pathlib.Path]:
    """Try to resolve a slug (possibly wrapped in [[ ]]) to a vault path.

    Handles:
    - Bare slugs: ``bar``
    - Wikilink syntax: ``[[bar]]`` or ``[[bar|Title]]``
    - YAML-wrapped wikilinks: ``[bar]`` (YAML parses ``[[bar]]`` as a list
      containing the string ``[bar]``)

    Then tries:
    1. <slug>.md at vault root
    2. <folder>/<slug>.md for each known folder
    """
    clean = slug.strip()
    # Strip wikilink syntax [[slug]]
    if clean.startswith("[[") and clean.endswith("]]"):
        clean = clean[2:-2].strip()
    # Strip YAML-wrapped single brackets [slug] (from YAML list items
    # that were originally [[slug]] in the source)
    elif clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1].strip()
    # Strip anchor text after |
    if "|" in clean:
        clean = clean.split("|")[0].strip()

    # Try root-level
    target = vault / f"{clean}.md"
    if target.is_file():
        return target
    # Try known folders
    for folder in ("projects", "reference", "research"):
        candidate = vault / folder / f"{clean}.md"
        if candidate.is_file():
            return candidate
    return None


def _build_reference_index(vault: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """Build a mapping of ``slug -> [referencing-path]`` for all vault notes.

    Scans all groomable vault files once and extracts wikilink targets.
    This is O(vault) — called once per detection run instead of O(pairs × vault).

    Keys are lowercase slugs for case-insensitive lookup.
    """
    index: dict[str, list[pathlib.Path]] = defaultdict(list)
    if not vault.is_dir():
        return index

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

        for target in _extract_wikilink_targets(text):
            base = target.rsplit("/", 1)[-1].lower()
            if base:
                index[base].append(md)

    return index


def _find_notes_referencing(
    vault: pathlib.Path,
    target_slug: str,
    reference_index: dict[str, list[pathlib.Path]],
) -> list[pathlib.Path]:
    """Find all vault notes that reference ``target_slug`` via wikilinks.

    Uses the pre-built ``reference_index`` (from :func:`_build_reference_index`)
    for O(1) lookup instead of scanning all files.

    Returns notes sorted alphabetically by path.
    """
    result = reference_index.get(target_slug.lower(), [])
    return sorted(result)


def detect_corrections(
    mind: pathlib.Path,
) -> CascadeReport:
    """Run the correction cascade detection algorithm.

    Finds all correction notes, identifies which notes they correct,
    then checks whether all notes that reference the corrected note
    also reference the correction.

    Returns a :class:`CascadeReport` with all unpropagated corrections.
    """
    vault = mind / "cortex-memory"
    report = CascadeReport()

    corrections = _find_correction_notes(vault)
    logger.info(
        "correction_cascade: found %d correction notes", len(corrections)
    )

    # Build reference index once — O(vault) instead of O(pairs × vault)
    reference_index = _build_reference_index(vault)
    logger.info(
        "correction_cascade: reference index built, %d unique targets",
        len(reference_index),
    )

    for correction_md in corrections:
        corrected_md = _find_corrected_note(correction_md, vault)
        if corrected_md is None:
            logger.info(
                "correction_cascade: no corrected note found for %s, skipping",
                correction_md.stem,
            )
            continue

        correction_slug = _slug_of(correction_md)
        corrected_slug = _slug_of(corrected_md)
        report.correction_pairs_checked += 1

        logger.info(
            "correction_cascade: checking pair %s → %s",
            corrected_slug,
            correction_slug,
        )

        # Get bodies for severity classification
        _, corr_body = _frontmatter_read(correction_md)
        _, corrected_body = _frontmatter_read(corrected_md)

        # Find all notes that reference the corrected note
        referencing = _find_notes_referencing(vault, corrected_slug, reference_index)
        logger.info(
            "correction_cascade: %d notes reference %s",
            len(referencing),
            corrected_slug,
        )

        for ref_md in referencing:
            # Skip the correction note itself and the corrected note
            if ref_md == correction_md or ref_md == corrected_md:
                continue

            ref_slug = _slug_of(ref_md)
            _, ref_body = _frontmatter_read(ref_md)

            # Check if this note also references the correction
            if f"[[{correction_slug}" in ref_body:
                logger.info(
                    "correction_cascade: %s already references %s — OK",
                    ref_slug,
                    correction_slug,
                )
                continue

            # This note references the corrected note but NOT the correction
            # → unpropagated correction
            severity, claim_changed = _classify_severity(
                corr_body, corrected_body, ref_body
            )
            report.unpropagated.append(
                UnpropagatedCorrection(
                    corrected_slug=corrected_slug,
                    corrected_title=_title_of(
                        _frontmatter_read(corrected_md)[0], corrected_slug
                    ),
                    correction_slug=correction_slug,
                    correction_title=_title_of(
                        _frontmatter_read(correction_md)[0], correction_slug
                    ),
                    referencing_slug=ref_slug,
                    referencing_title=_title_of(
                        _frontmatter_read(ref_md)[0], ref_slug
                    ),
                    severity=severity,
                    claim_changed=claim_changed,
                )
            )

    return report


def run(
    mind: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
    surface_path: Optional[pathlib.Path] = None,
    daily_path: Optional[pathlib.Path] = None,
) -> CascadeReport:
    """Top-level entry point for correction cascade detection.

    Runs the detection algorithm and optionally writes results to
    a surface file and/or today's daily.

    Args:
        mind: The alice-mind root path.
        journal_path: Optional journal path for audit trail.
        surface_path: Optional path to write the grooming report surface.
        daily_path: Optional path to append to today's daily.

    Returns:
        A :class:`CascadeReport` with all findings.
    """
    started = time.monotonic()
    report = detect_corrections(mind)
    elapsed = time.monotonic() - started

    logger.info(
        "correction_cascade: checked %d pairs, found %d unpropagated "
        "(high=%d, medium=%d, low=%d) in %.1fs",
        report.correction_pairs_checked,
        report.total_unpropagated,
        report.high_count,
        report.medium_count,
        report.low_count,
        elapsed,
    )

    # Write grooming report surface if path provided
    if surface_path is not None:
        surface_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%d %H:%M EDT")
        surface_text = (
            "---\n"
            f"title: Correction cascade grooming report ({time.strftime('%Y-%m-%d')})\n"
            "tags: [grooming, correction-cascade, mechanical-detection]\n"
            f"created: {time.strftime('%Y-%m-%d')}\n"
            f"updated: {now}\n"
            "last_accessed: " + time.strftime("%Y-%m-%d") + "\n"
            "access_count: 0\n"
            "---\n"
            "\n"
            "# Correction Cascade Grooming Report\n"
            "\n"
            f"Generated at {now}. Detection time: {elapsed:.1f}s.\n"
            "\n"
            "## Summary\n"
            "\n"
            f"- Correction pairs checked: {report.correction_pairs_checked}\n"
            f"- Unpropagated corrections found: {report.total_unpropagated}\n"
            f"- High severity: {report.high_count}\n"
            f"- Medium severity: {report.medium_count}\n"
            f"- Low severity: {report.low_count}\n"
            "\n"
            "## Unpropagated Corrections\n"
            "\n"
            f"{report.to_markdown_table()}\n"
        )
        surface_path.write_text(surface_text, encoding="utf-8")
        logger.info("correction_cascade: wrote grooming report to %s", surface_path)

    # Append to daily if path provided
    if daily_path is not None and report.total_unpropagated > 0:
        try:
            existing = daily_path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            daily_path.write_text(
                existing
                + f"\n- **Correction cascade:** {report.total_unpropagated} unpropagated corrections detected "
                f"({report.high_count} high, {report.medium_count} medium, {report.low_count} low) "
                f"in {report.correction_pairs_checked} correction pairs.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("correction_cascade: daily append failed: %s", exc)

    # Surface high-severity findings to Speaking
    if report.high_count > 0 and surface_path is not None:
        high_items = [u for u in report.unpropagated if u.severity == "high"]
        high_desc = ", ".join(
            f"[[{u.referencing_slug}]] still cites [[{u.corrected_slug}]] "
            f"without referencing correction [[{u.correction_slug}]]"
            for u in high_items[:5]
        )
        # Write a surface for Speaking
        surface_dir = mind / "inner" / "surface"
        surface_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        surf_file = surface_dir / f"{stamp}-correction-cascade-high.md"
        surf_text = (
            "---\n"
            "priority: insight\n"
            "context: High-severity unpropagated corrections detected during Stage C grooming.\n"
            f"reply_expected: false\n"
            "---\n"
            "\n"
            f"**{report.high_count} high-severity unpropagated corrections** found in "
            f"{report.correction_pairs_checked} correction pairs.\n"
            "\n"
            f"{high_desc}\n"
            "\n"
            "Full report: [[correction-cascade-grooming-report]]\n"
        )
        surf_file.write_text(surf_text, encoding="utf-8")

    return report
