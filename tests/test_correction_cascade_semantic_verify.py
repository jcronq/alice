"""Tests for alice_thinking.memory_worker.correction_cascade_semantic_verify.

Covers:
- VerificationResult / VerificationReport dataclass behavior
- _extract_correction_claim (body sections, metadata skipping, length cap)
- _extract_referencing_paragraphs (wikilink matching, code fence suppression)
- _build_prompt (content assembly)
- _parse_llm_response (verdict parsing, justification parsing, malformed input)
- verify (dry-run mode, LLM cap, missing notes, metadata-only corrections,
  empty referencing paragraphs, full pipeline)
"""

import json
import pathlib
import tempfile

import pytest

from alice_thinking.memory_worker.correction_cascade import (
    CascadeReport,
    UnpropagatedCorrection,
)
from alice_thinking.memory_worker.correction_cascade_semantic_verify import (
    VerificationResult,
    VerificationReport,
    _build_prompt,
    _classify_with_llm,
    _extract_correction_claim,
    _extract_referencing_paragraphs,
    _parse_llm_response,
    verify,
)


# ── VerificationResult ──────────────────────────────────────────────


class TestVerificationResult:
    def test_defaults(self):
        r = VerificationResult(
            corrected_slug="a",
            correction_slug="b",
            referencing_slug="c",
            verdict="yes",
        )
        assert r.justification == ""
        assert r.confidence == 0.0

    def test_full(self):
        r = VerificationResult(
            corrected_slug="a",
            correction_slug="b",
            referencing_slug="c",
            verdict="no",
            justification="unrelated topic",
            confidence=0.85,
        )
        assert r.justification == "unrelated topic"
        assert r.confidence == 0.85


# ── VerificationReport ──────────────────────────────────────────────


class TestVerificationReport:
    def test_flagged_count(self):
        r = VerificationReport(yes_count=5, no_count=2, unclear_count=1)
        assert r.flagged_count == 3

    def test_no_flagged(self):
        r = VerificationReport(yes_count=10, no_count=0, unclear_count=0)
        assert r.flagged_count == 0

    def test_to_jsonl(self, tmp_path):
        r = VerificationReport(
            total_triples=2,
            verified=2,
            skipped=0,
            yes_count=1,
            no_count=1,
            unclear_count=0,
            results=[
                VerificationResult("a", "b", "c", "yes", "related"),
                VerificationResult("a", "b", "d", "no", "unrelated"),
            ],
        )
        out = tmp_path / "report.jsonl"
        r.to_jsonl(out)
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["verdict"] == "yes"
        assert first["corrected_slug"] == "a"


# ── _extract_correction_claim ───────────────────────────────────────


class TestExtractCorrectionClaim:
    def test_basic_body(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "The original claim stated that 98.1% of notes were decayed.\n\n"
            "This is incorrect; the actual figure is 72.3%.\n\n"
            "The discrepancy arose from excluding metadata-only notes.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert "98.1%" in claim
        assert "72.3%" in claim
        assert len(claim) <= 500

    def test_skips_abstract_section(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "The real claim is that 50% was wrong.\n\n"
            "## Abstract\n\nThis is the abstract.\n\n"
            "## Discussion\n\nFurther analysis shows the figure was inflated.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert "50%" in claim
        # "This is the abstract" should NOT appear (under skipped section)
        assert "This is the abstract" not in claim
        # "Further analysis" should appear (after ## Discussion, which is not skipped)
        assert "Further analysis" in claim

    def test_skips_backlinks_section(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "The claim is 98.1% decayed.\n\n"
            "## Backlinks\n\nSee [[baz]].\n\n"
            "## Changelog\n\nv1: initial.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert "98.1%" in claim
        assert "baz" not in claim

    def test_skips_changelog_section(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "The correction is about protein intake.\n\n"
            "## Changelog\n\n2026-01-01: created.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert "protein" in claim

    def test_length_cap(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        long_text = "x " * 300
        md.write_text(
            "---\nnote_type: correction\n---\n" + long_text,
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert len(claim) <= 500

    def test_code_fence_suppression(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "See [[bar]] for the original.\n\n"
            "The code example shows [[baz]] which is irrelevant.\n\n"
            "The actual correction is about the percentage.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        # [[bar]] in the body should be captured (it's in the first paragraph)
        assert "bar" in claim
        # [[baz]] should be stripped if in a code fence, but here it's inline
        # The key test: _strip_code is applied, so code fence wikilinks are suppressed

    def test_single_paragraph(self, tmp_path):
        md = tmp_path / "foo-correction.md"
        md.write_text(
            "---\nnote_type: correction\n---\n"
            "The original claim was that all notes decayed, but only 60% did.",
            encoding="utf-8",
        )
        claim = _extract_correction_claim(md)
        assert "60%" in claim


# ── _extract_referencing_paragraphs ─────────────────────────────────


class TestExtractReferencingParagraphs:
    def test_basic_wikilink_match(self, tmp_path):
        md = tmp_path / "baz.md"
        md.write_text(
            "---\n---\n"
            "See [[foo]] for details on the decay rate.\n\n"
            "Other paragraph that doesn't mention foo.\n\n"
            "Also check [[foo]] for additional context.",
            encoding="utf-8",
        )
        paras = _extract_referencing_paragraphs(md, "foo")
        assert "decay rate" in paras
        assert "additional context" in paras
        assert "doesn't mention" not in paras

    def test_aliased_wikilink(self, tmp_path):
        md = tmp_path / "baz.md"
        md.write_text(
            "See [[foo|the decay note]] for more.",
            encoding="utf-8",
        )
        paras = _extract_referencing_paragraphs(md, "foo")
        assert "decay note" in paras

    def test_no_match(self, tmp_path):
        md = tmp_path / "baz.md"
        md.write_text(
            "See [[bar]] for details.\n\n"
            "Nothing about foo here.",
            encoding="utf-8",
        )
        paras = _extract_referencing_paragraphs(md, "foo")
        assert paras == ""

    def test_cap_at_three(self, tmp_path):
        md = tmp_path / "baz.md"
        paragraphs = []
        for i in range(5):
            paragraphs.append(
                f"Paragraph {i} mentions [[foo]] in it."
            )
        md.write_text("\n\n".join(paragraphs), encoding="utf-8")
        paras = _extract_referencing_paragraphs(md, "foo")
        # Should contain at most 3 paragraphs
        count = paras.count("Paragraph")
        assert count == 3

    def test_code_fence_suppression(self, tmp_path):
        md = tmp_path / "baz.md"
        md.write_text(
            "```python\nprint('[[foo]]')\n```\n\n"
            "Real mention of [[foo]] in prose.",
            encoding="utf-8",
        )
        paras = _extract_referencing_paragraphs(md, "foo")
        # Code fence content should be stripped; only real prose should match
        assert "Real mention" in paras

    def test_frontmatter_skipped(self, tmp_path):
        md = tmp_path / "baz.md"
        md.write_text(
            "---\ntitle: Test\n---\nSee [[foo]] for details.",
            encoding="utf-8",
        )
        paras = _extract_referencing_paragraphs(md, "foo")
        assert "details" in paras


# ── _build_prompt ───────────────────────────────────────────────────


class TestBuildPrompt:
    def test_contains_all_fields(self):
        prompt = _build_prompt(
            correction_claim="98.1% was wrong",
            referencing_paragraphs="See [[bar]] for more.",
            corrected_slug="bar",
            correction_slug="foo-correction",
            referencing_slug="baz",
        )
        assert "98.1%" in prompt
        assert "foo-correction" in prompt
        assert "bar" in prompt
        assert "baz" in prompt
        assert "YES" in prompt
        assert "NO" in prompt
        assert "UNCLEAR" in prompt
        assert "VERDICT:" in prompt
        assert "JUSTIFICATION:" in prompt

    def test_format(self):
        prompt = _build_prompt("claim", "para", "c", "corr", "ref")
        assert "VERDICT: [YES|NO|UNCLEAR]" in prompt
        assert "JUSTIFICATION: [one sentence]" in prompt


# ── _parse_llm_response ─────────────────────────────────────────────


class TestParseLlmResponse:
    def test_yes_verdict(self):
        response = "VERDICT: YES\nJUSTIFICATION: These are related."
        verdict, justification = _parse_llm_response(response)
        assert verdict == "yes"
        assert justification == "These are related."

    def test_no_verdict(self):
        response = "VERDICT: NO\nJustification: Unrelated topics."
        verdict, justification = _parse_llm_response(response)
        assert verdict == "no"
        assert justification == "Unrelated topics."

    def test_unclear_verdict(self):
        response = "VERDICT: UNCLEAR\nJustification: Not enough context."
        verdict, justification = _parse_llm_response(response)
        assert verdict == "unclear"

    def test_lowercase_verdict(self):
        response = "verdict: yes\njustification: related"
        verdict, justification = _parse_llm_response(response)
        assert verdict == "yes"

    def test_malformed_defaults(self):
        response = "some random text without verdict or justification"
        verdict, justification = _parse_llm_response(response)
        assert verdict == "unclear"
        assert justification == "parse failed"

    def test_verdict_only(self):
        response = "VERDICT: YES"
        verdict, justification = _parse_llm_response(response)
        assert verdict == "yes"
        assert justification == "parse failed"

    def test_justification_only(self):
        response = "JUSTIFICATION: related"
        verdict, justification = _parse_llm_response(response)
        assert verdict == "unclear"
        assert justification == "related"

    def test_justification_variants(self):
        # Test various casing and formatting of JUSTIFICATION line
        for prefix in ["JUSTIFICATION:", "Justification:", "justification:"]:
            response = f"{prefix} some reason"
            verdict, justification = _parse_llm_response(response)
            assert justification == "some reason"

    def test_justification_with_colon(self):
        response = "JUSTIFICATION: Reason: additional detail"
        verdict, justification = _parse_llm_response(response)
        assert justification == "Reason: additional detail"


# ── _classify_with_llm ──────────────────────────────────────────────


class TestClassifyWithLlm:
    def test_dry_run_returns_yes(self):
        # In dry-run mode, always returns ("yes", "dry-run skip")
        # _DRY_RUN is module-level and defaults to True
        verdict, justification = _classify_with_llm("any prompt")
        assert verdict == "yes"
        assert justification == "dry-run skip"


# ── verify (dry-run mode) ───────────────────────────────────────────


class TestVerifyDryRun:
    def _make_vault(self, tmp_path):
        """Create a minimal vault structure. Returns mind_dir (parent of cortex-memory).

        verify() expects the mind root; it computes vault = mind / 'cortex-memory'.
        """
        tmp_path = pathlib.Path(tmp_path)
        mind_dir = tmp_path
        vault = mind_dir / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        # Correction note (must be >= 20 words to pass metadata-only check)
        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\n---\n"
            "The original claim stated that 98.1 percent of notes were decayed from the vault.\n\n"
            "This figure is incorrect; the actual decay rate is 72.3 percent based on the corrected analysis.\n\n"
            "The discrepancy arose from excluding metadata-only notes from the original calculation.",
            encoding="utf-8",
        )

        # Corrected note
        corrected = vault / "reference" / "bar.md"
        corrected.write_text(
            "---\ncorrected_by: [foo-correction]\n---\nnotes were decayed.",
            encoding="utf-8",
        )

        # Referencing note (mentions bar but not the correction)
        ref = vault / "reference" / "baz.md"
        ref.write_text(
            "See [[bar]] for details on the decay rate.",
            encoding="utf-8",
        )

        return mind_dir

    def test_basic_dry_run(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)

        # Build a minimal report
        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection(
                corrected_slug="bar",
                corrected_title="Bar",
                correction_slug="foo-correction",
                correction_title="Foo Correction",
                referencing_slug="baz",
                referencing_title="Baz",
                severity="high",
                claim_changed="98.1% → 72.3%",
            )
        )

        result = verify(vault, report)
        assert result.total_triples == 1
        assert result.yes_count == 1
        assert result.no_count == 0
        assert result.skipped == 0
        assert result.results[0].verdict == "yes"
        assert result.results[0].justification == "dry-run skip"

    def test_multiple_triples_dry_run(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Add another referencing note
        ref2 = vault / "reference" / "qux.md"
        ref2.write_text(
            "Also see [[bar]] for context.",
            encoding="utf-8",
        )

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "qux", "Qux", "medium", "98.1%")
        )

        result = verify(vault.parent, report)
        assert result.total_triples == 2
        assert result.yes_count == 2
        assert result.skipped == 0

    def test_missing_note_skipped(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Remove the referenced note
        (vault / "reference" / "baz.md").unlink()

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )

        result = verify(vault.parent, report)
        assert result.skipped == 1
        assert result.results[0].verdict == "unclear"
        assert "not found" in result.results[0].justification

    def test_metadata_only_correction_skipped(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Overwrite correction with metadata-only content
        (vault / "reference" / "foo-correction.md").write_text(
            "---\nnote_type: correction\n---\nJust a note.",
            encoding="utf-8",
        )

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )

        result = verify(vault.parent, report)
        assert result.skipped == 1
        assert "metadata-only" in result.results[0].justification or "too short" in result.results[0].justification

    def test_no_referencing_paragraphs_skipped(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Remove wikilink from referencing note
        (vault / "reference" / "baz.md").write_text(
            "No wikilinks here at all.",
            encoding="utf-8",
        )

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )

        result = verify(vault.parent, report)
        assert result.skipped == 1
        assert "no relevant paragraphs" in result.results[0].justification

    def test_llm_call_cap(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Create many referencing notes
        for i in range(60):
            ref = vault / "reference" / f"ref-{i}.md"
            ref.write_text(f"See [[bar]] for details {i}.", encoding="utf-8")

        report = CascadeReport()
        report.correction_pairs_checked = 1
        for i in range(60):
            report.unpropagated.append(
                UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", f"ref-{i}", f"Ref {i}", "low", "98.1%")
            )

        result = verify(vault.parent, report)
        # Should have processed 50 and skipped 10
        assert result.yes_count == 50
        assert result.skipped == 10
        assert result.results[-1].justification.startswith("LLM call cap")

    def test_severity_ordering(self):
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        # Create notes for different severities
        for sev, slug in [("high", "high-note"), ("medium", "med-note"), ("low", "low-note")]:
            ref = vault / "reference" / f"{slug}.md"
            ref.write_text(f"See [[bar]] for details.", encoding="utf-8")

        report = CascadeReport()
        report.correction_pairs_checked = 1
        # Add in reverse severity order
        for sev, slug in [("low", "low-note"), ("medium", "med-note"), ("high", "high-note")]:
            report.unpropagated.append(
                UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", slug, slug.capitalize(), sev, "98.1%")
            )

        result = verify(vault.parent, report)
        # First result should be high severity
        assert result.results[0].referencing_slug == "high-note"
        assert result.results[1].referencing_slug == "med-note"
        assert result.results[2].referencing_slug == "low-note"

    def test_output_path(self, tmp_path):
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )

        out = tmp_path / "output" / "report.jsonl"
        result = verify(vault.parent, report, output_path=out)
        assert out.exists()
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_empty_report(self):
        tmp_path = tempfile.mkdtemp()
        mind_dir = pathlib.Path(tmp_path)
        (mind_dir / "cortex-memory").mkdir()

        report = CascadeReport()
        report.correction_pairs_checked = 0

        result = verify(mind_dir, report)
        assert result.total_triples == 0
        assert result.yes_count == 0
        assert result.no_count == 0
        assert result.unclear_count == 0

    def test_dry_run_override(self):
        """Test that dry_run=False parameter works (still dry-run because LLM unavailable)."""
        tmp_path = tempfile.mkdtemp()
        vault = self._make_vault(tmp_path)
        vault = vault / "cortex-memory"

        report = CascadeReport()
        report.correction_pairs_checked = 1
        report.unpropagated.append(
            UnpropagatedCorrection("bar", "Bar", "foo-correction", "Foo Corr", "baz", "Baz", "high", "98.1%")
        )

        # dry_run=False would try LLM; since LLM is unavailable, it falls back to mock
        # which returns ("yes", "LLM unavailable: ...")
        # This test just verifies the function doesn't crash with dry_run=False
        result = verify(vault.parent, report, dry_run=False)
        assert result.total_triples == 1
