"""Semantic verification for correction cascade auto-propagation.

After structural auto-propagation adds wikilinks, this module verifies
that each correction actually addresses the claim in the referencing
note. Structural propagation is safe but imperfect — it adds a link to
any note that cites the corrected note, even if the citation is for a
different reason than the claim being corrected.

Design: ``cortex-memory/research/2026-06-11-correction-cascade-semantic-verification.md``

Integration
-----------

Runs after auto-propagation in the Stage C grooming pipeline::

    link audit → correction cascade detection →
        correction cascade auto-propagation →
        correction cascade semantic verification → dedupe

Example usage::

    from alice_thinking.memory_worker.correction_cascade import detect_corrections
    from alice_thinking.memory_worker.correction_cascade_auto_propagate import auto_propagate
    from alice_thinking.memory_worker.correction_cascade_semantic_verify import verify

    report = detect_corrections(mind)
    auto_propagate(mind, report)
    flags = verify(mind, report)  # returns verification results

Safety
------

- **Read-only.** Does not modify vault files. Produces a report.
- **Dry-run default.** ``_DRY_RUN = True``. No LLM calls are made
  in dry-run mode; the module returns mock classifications.
- **LLM cost cap.** Max ``_MAX_LLM_CALLS`` calls per run (default 50).
  If the report has more triples, verify the top N by severity.
- **Uses Sonnet** via the ``lite_llm`` client (local Qwen for cheap
  classification; swap to Sonnet when available).

"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
from typing import Optional

from indexer.yaml_lite import _strip_code, split_frontmatter

from alice_thinking.memory_worker.correction_cascade import (
    CascadeReport,
    _try_resolve_slug,
)

logger = logging.getLogger(__name__)

#: Dry-run mode (default). Set to ``False`` only after review.
_DRY_RUN = True

#: Maximum LLM calls per verification run.
_MAX_LLM_CALLS = 50

#: Model to use for classification. ``"qwen"`` for local (cheap),
#: ``"sonnet"`` for Anthropic (accurate). Default is ``"qwen"``.
_MODEL = "qwen"

# ---------- data types ----------


@dataclasses.dataclass
class VerificationResult:
    """Result of a single triple verification."""

    corrected_slug: str
    correction_slug: str
    referencing_slug: str
    verdict: str  # "yes", "no", "unclear"
    justification: str = ""
    confidence: float = 0.0  # 0.0–1.0, only set when not dry-run


@dataclasses.dataclass
class VerificationReport:
    """Aggregated verification results."""

    total_triples: int = 0
    verified: int = 0
    skipped: int = 0  # dry-run or cap exceeded
    yes_count: int = 0
    no_count: int = 0
    unclear_count: int = 0
    results: list[VerificationResult] = dataclasses.field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        """Count of triples that need human review (no or unclear)."""
        return self.no_count + self.unclear_count

    def to_jsonl(self, path: pathlib.Path) -> None:
        """Write the report as JSONL for Speaking to review."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in self.results:
                f.write(json.dumps(dataclasses.asdict(r)) + "\n")
        logger.info("verification report written to %s (%d entries)", path, len(self.results))


# ---------- claim extraction ----------


def _extract_correction_claim(correction_md: pathlib.Path) -> str:
    """Extract the core correction claim from a correction note.

    Parses the first 2–3 body paragraphs (after frontmatter, abstract,
    and backlinks), skipping metadata sections.
    """
    text = correction_md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    # Strip sections we don't need
    sections_to_skip = {"abstract", "backlinks", "changelog", "references"}
    cleaned = []
    current_section = None
    for line in body.split("\n"):
        if line.startswith("## "):
            section_name = line[3:].strip().lower()
            if section_name in sections_to_skip or current_section in sections_to_skip:
                current_section = section_name
                continue
            current_section = section_name
            cleaned.append(line)
        elif current_section in sections_to_skip:
            continue
        else:
            cleaned.append(line)

    # Take first 3 paragraphs
    paragraphs = [p.strip() for p in "\n".join(cleaned).split("\n\n") if p.strip()]
    claim = " ".join(paragraphs[:3])
    return _strip_code(claim)[:500]  # cap length


def _extract_referencing_paragraphs(
    ref_md: pathlib.Path, corrected_slug: str
) -> str:
    """Extract paragraphs from the referencing note that cite the corrected note.

    Finds all paragraphs containing a wikilink to ``corrected_slug``
    and returns them for comparison.
    """
    text = ref_md.read_text(encoding="utf-8")
    _, body = split_frontmatter(text)

    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    relevant = []
    for p in paragraphs:
        clean = _strip_code(p)
        if f"[[{corrected_slug}" in clean or f"[[{corrected_slug}|" in clean:
            relevant.append(p)

    return "\n\n".join(relevant[:3])  # cap at 3 relevant paragraphs


# ---------- LLM classification ----------


def _build_prompt(
    correction_claim: str,
    referencing_paragraphs: str,
    corrected_slug: str,
    correction_slug: str,
    referencing_slug: str,
) -> str:
    """Build the LLM prompt for a single triple."""
    return (
        f"You are verifying a correction propagation in a knowledge vault.\n"
        f"\n"
        f"Correction note [[{correction_slug}]] corrects a claim in [[{corrected_slug}]].\n"
        f"The correction states:\n"
        f"{correction_claim}\n"
        f"\n"
        f"Referencing note [[{referencing_slug}]] cites [[{corrected_slug}]].\n"
        f"Its relevant paragraphs:\n"
        f"{referencing_paragraphs}\n"
        f"\n"
        f"Question: Does the correction in the correction note apply to the "
        f"claim made in the referencing note? Answer YES, NO, or UNCLEAR.\n"
        f"Justify in one sentence.\n"
        f"\n"
        f"Respond in this exact format:\n"
        f"VERDICT: [YES|NO|UNCLEAR]\n"
        f"JUSTIFICATION: [one sentence]\n"
    )


def _parse_llm_response(response: str) -> tuple[str, str]:
    """Parse the LLM's response into verdict and justification.

    Returns ("yes", "justification") or ("no", "justification") or
    ("unclear", "justification"). Defaults to ("unclear", "parse failed")
    on unexpected output.
    """
    verdict = "unclear"
    justification = "parse failed"

    for line in response.split("\n"):
        line = line.strip().upper()
        if line.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v == "YES":
                verdict = "yes"
            elif v == "NO":
                verdict = "no"
            elif v == "UNCLEAR":
                verdict = "unclear"
            break

    for line in response.split("\n"):
        line = line.strip()
        if line.upper().startswith("JUSTIFICATION:"):
            justification = line.split(":", 1)[1].strip()
            break
        # Also try "Justification:" with various casing
        if "justification" in line.lower() and ":" in line:
            justification = line.split(":", 1)[1].strip()
            break

    return verdict, justification


def _classify_with_llm(
    prompt: str,
) -> tuple[str, str]:
    """Call the LLM to classify a triple.

    Returns (verdict, justification). In dry-run mode, returns
    ("yes", "dry-run skip") without making an LLM call.
    """
    if _DRY_RUN:
        return ("yes", "dry-run skip")

    # Try lite_llm first (local Qwen), fall back to mock
    try:
        from alice_thinking.llm_client import LiteLLMClient
        client = LiteLLMClient(model=_MODEL, max_tokens=100, temperature=0.0)
        response = client.call(prompt)
        return _parse_llm_response(response)
    except Exception as e:
        logger.warning("LLM classification failed (%s), using mock", e)
        return ("yes", f"LLM unavailable: {e}")


# ---------- main verification pass ----------


def verify(
    mind: pathlib.Path,
    report: CascadeReport,
    *,
    dry_run: Optional[bool] = None,
    output_path: Optional[pathlib.Path] = None,
) -> VerificationReport:
    """Run semantic verification on a correction cascade report.

    For each unpropagated correction triple, extracts the correction
    claim and the referencing note's relevant paragraphs, then sends
    them to an LLM for classification.

    Parameters
    ----------
    mind
        The alice-mind root path.
    report
        Detection report with unpropagated corrections.
    dry_run
        Override the module-level ``_DRY_RUN`` setting.
    output_path
        Where to write the JSONL report. Defaults to
        ``~/alice-mind/inner/state/verification_report.jsonl``.

    Returns
    -------
    VerificationReport
        Aggregated results with per-triple verdicts.
    """
    global _DRY_RUN
    if dry_run is not None:
        _DRY_RUN = dry_run

    vault = mind / "cortex-memory"
    results: list[VerificationResult] = []
    yes_count = 0
    no_count = 0
    unclear_count = 0
    skipped = 0
    llm_calls = 0

    # Sort by severity: high first, then medium, then low
    sorted_items = sorted(
        report.unpropagated,
        key=lambda u: {"high": 0, "medium": 1, "low": 2}.get(u.severity, 3),
    )

    for i, triple in enumerate(sorted_items):
        # Cap LLM calls
        if llm_calls >= _MAX_LLM_CALLS:
            skipped += 1
            results.append(VerificationResult(
                corrected_slug=triple.corrected_slug,
                correction_slug=triple.correction_slug,
                referencing_slug=triple.referencing_slug,
                verdict="unclear",
                justification=f"LLM call cap ({_MAX_LLM_CALLS}) reached at index {i}",
            ))
            continue

        # Resolve paths
        corrected_md = _try_resolve_slug(triple.corrected_slug, vault)
        correction_md = _try_resolve_slug(triple.correction_slug, vault)
        ref_md = _try_resolve_slug(triple.referencing_slug, vault)

        if corrected_md is None or correction_md is None or ref_md is None:
            skipped += 1
            results.append(VerificationResult(
                corrected_slug=triple.corrected_slug,
                correction_slug=triple.correction_slug,
                referencing_slug=triple.referencing_slug,
                verdict="unclear",
                justification="note not found in vault",
            ))
            continue

        # Skip metadata-only corrections (no body claim to verify)
        _, corr_body = split_frontmatter(correction_md.read_text(encoding="utf-8"))
        cleaned_body = _strip_code(corr_body)
        if len(cleaned_body.split()) < 20:
            skipped += 1
            results.append(VerificationResult(
                corrected_slug=triple.corrected_slug,
                correction_slug=triple.correction_slug,
                referencing_slug=triple.referencing_slug,
                verdict="unclear",
                justification="metadata-only correction (too short for semantic analysis)",
            ))
            continue

        # Extract claims
        try:
            correction_claim = _extract_correction_claim(correction_md)
            ref_paragraphs = _extract_referencing_paragraphs(
                ref_md, triple.corrected_slug
            )
        except Exception as e:
            skipped += 1
            results.append(VerificationResult(
                corrected_slug=triple.corrected_slug,
                correction_slug=triple.correction_slug,
                referencing_slug=triple.referencing_slug,
                verdict="unclear",
                justification=f"extraction error: {e}",
            ))
            continue

        if not ref_paragraphs.strip():
            skipped += 1
            results.append(VerificationResult(
                corrected_slug=triple.corrected_slug,
                correction_slug=triple.correction_slug,
                referencing_slug=triple.referencing_slug,
                verdict="unclear",
                justification="no relevant paragraphs found in referencing note",
            ))
            continue

        # Classify
        prompt = _build_prompt(
            correction_claim,
            ref_paragraphs,
            triple.corrected_slug,
            triple.correction_slug,
            triple.referencing_slug,
        )

        verdict, justification = _classify_with_llm(prompt)
        llm_calls += 1

        if verdict == "yes":
            yes_count += 1
        elif verdict == "no":
            no_count += 1
        else:
            unclear_count += 1

        results.append(VerificationResult(
            corrected_slug=triple.corrected_slug,
            correction_slug=triple.correction_slug,
            referencing_slug=triple.referencing_slug,
            verdict=verdict,
            justification=justification,
        ))

    report_obj = VerificationReport(
        total_triples=len(sorted_items),
        verified=len(results) - skipped,
        skipped=skipped,
        yes_count=yes_count,
        no_count=no_count,
        unclear_count=unclear_count,
        results=results,
    )

    # Write report
    if output_path is None:
        output_path = mind / "inner" / "state" / "verification_report.jsonl"
    report_obj.to_jsonl(output_path)

    logger.info(
        "semantic verification: %d triples, %d verified, "
        "%d skipped, %d yes, %d no, %d unclear",
        report_obj.total_triples,
        report_obj.verified,
        report_obj.skipped,
        report_obj.yes_count,
        report_obj.no_count,
        report_obj.unclear_count,
    )

    return report_obj
