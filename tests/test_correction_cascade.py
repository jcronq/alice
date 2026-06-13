"""Tests for alice_thinking.memory_worker.correction_cascade.

Covers:
- Wikilink extraction (basic, aliased, anchored, multiple, empty)
- Slug resolution (explicit frontmatter slug vs filename stem)
- Quantitative correction detection (ratio, multiplier, n=, comparative %)
- Qualitative-only and nuance classification
- Severity classification
- Correction note discovery (note_type, filename pattern, exclusions)
- Corrected note resolution (corrected_by field, references/supersedes fields, wikilink fallback)
- Reference index building
- Full detection run (single correction, no unpropagated, no corrections)
- Excluded folders (dailies, archive, gh-state, hidden)
"""

import pathlib


from alice_thinking.memory_worker.correction_cascade import (
    CascadeReport,
    UnpropagatedCorrection,
    _build_reference_index,
    _classify_severity,
    _extract_quantitative_claims,
    _extract_wikilink_targets,
    _find_corrected_note,
    _find_correction_notes,
    _find_notes_referencing,
    _frontmatter_read,
    _has_specific_quantitative_correction,
    _resolve_field_value,
    _slug_of,
    _try_resolve_slug,
    detect_corrections,
)


# ── Wikilink extraction ──────────────────────────────────────────────


class TestExtractWikilinkTargets:
    def test_basic(self):
        body = "See [[foo-bar]] for details."
        assert _extract_wikilink_targets(body) == ["foo-bar"]

    def test_aliased(self):
        body = "See [[foo-bar|bar]] for details."
        assert _extract_wikilink_targets(body) == ["foo-bar"]

    def test_anchored(self):
        body = "See [[foo-bar#section]] for details."
        assert _extract_wikilink_targets(body) == ["foo-bar"]

    def test_multiple(self):
        body = "See [[a]] and [[b]] and [[c]]."
        assert _extract_wikilink_targets(body) == ["a", "b", "c"]

    def test_empty(self):
        assert _extract_wikilink_targets("") == []

    def test_no_wikilinks(self):
        body = "No wikilinks here, just prose."
        assert _extract_wikilink_targets(body) == []

    def test_nested_brackets_ignored(self):
        body = "See [[foo|[[bar]]]] for details."
        assert _extract_wikilink_targets(body) == ["foo"]

    def test_foldered_slugs(self):
        body = "See [[projects/foo]] and [[reference/bar]]"
        assert _extract_wikilink_targets(body) == ["projects/foo", "reference/bar"]

    def test_anchored_with_alias(self):
        body = "See [[foo-bar#section|bar]] for details."
        assert _extract_wikilink_targets(body) == ["foo-bar"]


# ── Slug resolution ─────────────────────────────────────────────────


class TestSlugOf:
    def test_explicit_slug(self, tmp_path):
        md = tmp_path / "foo.md"
        md.write_text("---\nslug: bar\n---\nbody", encoding="utf-8")
        assert _slug_of(md) == "bar"

    def test_filename_stem(self, tmp_path):
        md = tmp_path / "foo.md"
        md.write_text("no frontmatter\n", encoding="utf-8")
        assert _slug_of(md) == "foo"

    def test_empty_slug_falls_back(self, tmp_path):
        md = tmp_path / "foo.md"
        md.write_text("---\nslug: \n---\nbody", encoding="utf-8")
        assert _slug_of(md) == "foo"

    def test_whitespace_slug_falls_back(self, tmp_path):
        md = tmp_path / "foo.md"
        md.write_text("---\nslug:   \n---\nbody", encoding="utf-8")
        assert _slug_of(md) == "foo"


# ── Frontmatter read ────────────────────────────────────────────────


class TestFrontmatterRead:
    def test_valid_frontmatter(self):
        # _frontmatter_read reads from a file, not text — coverage via
        # test_file_read below; this slot is kept for symmetry.
        fm, body = _frontmatter_read(pathlib.Path())
        assert fm == {} and body == ""

    def test_file_read(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("---\nslug: foo\nnote_type: correction\n---\nbody text", encoding="utf-8")
        fm, body = _frontmatter_read(md)
        assert fm["slug"] == "foo"
        assert fm["note_type"] == "correction"
        assert body == "body text"

    def test_no_frontmatter(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("just body text", encoding="utf-8")
        fm, body = _frontmatter_read(md)
        assert fm == {}
        assert body == "just body text"

    def test_os_error(self, tmp_path):
        # Read a non-existent file
        fm, body = _frontmatter_read(tmp_path / "nonexistent.md")
        assert fm == {}
        assert body == ""


# ── Quantitative claim extraction ────────────────────────────────────


class TestExtractQuantitativeClaims:
    def test_percentage(self):
        body = "98.1% of notes were decayed."
        claims = _extract_quantitative_claims(body)
        # The regex captures the full match; the claim string contains the percentage
        assert any("98.1%" in c for c in claims)

    def test_n_equals(self):
        body = "We sampled n=100 notes."
        claims = _extract_quantitative_claims(body)
        assert "100" in claims

    def test_multiple(self):
        body = "100% coverage with n=1000 samples."
        claims = _extract_quantitative_claims(body)
        assert any("100" in c for c in claims)
        assert any("1000" in c for c in claims)

    def test_no_numbers(self):
        body = "All notes were reviewed carefully."
        claims = _extract_quantitative_claims(body)
        # Should find "All" doesn't match the pattern
        assert claims == []


# ── Specific quantitative correction detection ──────────────────────


class TestHasSpecificQuantitativeCorrection:
    def test_ratio_with_count(self):
        body = "The correction was 98.1% (159/162)."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is True
        assert "98.1%" in desc

    def test_multiplier(self):
        body = "They are 0.1x as likely to be accessed."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is True
        assert "0.1x" in desc

    def test_n_equals_with_vs(self):
        body = "Treatment group n=100 vs control n=50."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is True

    def test_comparative_percentages(self):
        body = "Recovery was 12% vs 5% baseline."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is True

    def test_generic_percentage_no_count(self):
        body = "100% domain classified."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is False

    def test_coverage_metric(self):
        body = "Coverage 100% (104 notes)."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is False

    def test_no_quantitative(self):
        body = "The correction clarifies the original finding."
        found, desc = _has_specific_quantitative_correction(body)
        assert found is False

    def test_empty(self):
        found, desc = _has_specific_quantitative_correction("")
        assert found is False


# ── Severity classification ─────────────────────────────────────────


class TestClassifySeverity:
    def test_high_quantitative(self):
        severity, claim = _classify_severity(
            "98.1% (159/162) were decayed.",
            "notes were decayed.",
            "some notes were decayed.",
        )
        assert severity == "high"
        assert "98.1%" in claim

    def test_medium_qualitative(self):
        severity, claim = _classify_severity(
            "The original claim was incorrect.",
            "notes were decayed.",
            "some notes were decayed.",
        )
        assert severity == "medium"
        assert "qualitative" in claim.lower()

    def test_low_nuance(self):
        severity, claim = _classify_severity(
            "This adds an edge case to the original.",
            "notes were decayed.",
            "some notes were decayed.",
        )
        assert severity == "low"
        assert "nuance" in claim.lower()

    def test_medium_keyword_actually(self):
        severity, claim = _classify_severity(
            "Actually, the original was wrong.",
            "notes were decayed.",
            "some notes were decayed.",
        )
        assert severity == "medium"

    def test_medium_keyword_misleading(self):
        severity, claim = _classify_severity(
            "The prior conclusion was misleading.",
            "notes were decayed.",
            "some notes were decayed.",
        )
        assert severity == "medium"


# ── Correction note discovery ───────────────────────────────────────


class TestFindCorrectionNotes:
    def test_note_type_correction(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        md = vault / "reference" / "foo-correction.md"
        md.write_text("---\nnote_type: correction\n---\ncorrected something", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 1
        assert notes[0] == md

    def test_filename_pattern(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        md = vault / "reference" / "foo-correction.md"
        md.write_text("---\nnote_type: analysis\n---\nnot a correction", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 1
        assert notes[0] == md

    def test_excluded_dailies(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "dailies").mkdir()
        md = vault / "dailies" / "2026-01-01-correction.md"
        md.write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_excluded_archive(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "archive").mkdir()
        md = vault / "archive" / "old-correction.md"
        md.write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_excluded_gh_state(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "gh-state").mkdir()
        md = vault / "gh-state" / "issue-correction.md"
        md.write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_excluded_index_readme(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        vault.joinpath("index.md").write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        vault.joinpath("README.md").write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        vault.joinpath("unresolved.md").write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_not_a_correction(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        md = vault / "reference" / "normal-note.md"
        md.write_text("---\nnote_type: analysis\n---\njust a note", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_hidden_folder_excluded(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        hidden = vault / ".hidden"
        hidden.mkdir()
        md = hidden / "foo-correction.md"
        md.write_text("---\nnote_type: correction\n---\n", encoding="utf-8")
        notes = _find_correction_notes(vault)
        assert len(notes) == 0

    def test_miscorrection_not_matched(self, tmp_path):
        """Filename 'miscorrection.md' should NOT match the -correction- pattern."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        md = vault / "reference" / "miscorrection.md"
        md.write_text("---\nnote_type: analysis\n---\nnot a correction", encoding="utf-8")
        notes = _find_correction_notes(vault)
        # Only matches note_type, not filename
        assert len(notes) == 0


# ── Corrected note resolution ───────────────────────────────────────


class TestFindCorrectedNote:
    def test_corrected_by_field(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        # Create correction note
        correction = vault / "reference" / "foo-correction.md"
        correction.write_text("---\nnote_type: correction\n---\nfixed something", encoding="utf-8")

        # Create corrected note that references the correction
        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\ncorrected_by: [foo-correction]\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_supersedes_field(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nsupersedes: [bar]\n---\nfixed something",
            encoding="utf-8",
        )

        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_references_field(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nreferences: [bar]\n---\nfixed something",
            encoding="utf-8",
        )

        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_wikilink_fallback(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\n---\nsee [[bar]] for the original",
            encoding="utf-8",
        )

        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_no_corrected_note(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text("---\nnote_type: correction\n---\nno target", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result is None

    def test_corrected_by_list_field(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text("---\nnote_type: correction\n---\n", encoding="utf-8")

        corrected = vault / "reference" / "bar.md"
        corrected.write_text(
            "---\ncorrected_by: [foo-correction, other-correction]\n---\nwas wrong",
            encoding="utf-8",
        )

        result = _find_corrected_note(correction, vault)
        assert result == corrected


# ── Reference index ─────────────────────────────────────────────────


class TestBuildReferenceIndex:
    def test_basic_indexing(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        md = vault / "reference" / "a.md"
        md.write_text("See [[foo]] and [[bar]].", encoding="utf-8")

        idx = _build_reference_index(vault)
        assert "foo" in idx
        assert "bar" in idx
        assert len(idx["foo"]) == 1
        assert len(idx["bar"]) == 1

    def test_excluded_folders(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "dailies").mkdir()
        (vault / "reference").mkdir()

        daily = vault / "dailies" / "2026-01-01.md"
        daily.write_text("See [[foo]].", encoding="utf-8")

        ref = vault / "reference" / "a.md"
        ref.write_text("See [[foo]].", encoding="utf-8")

        idx = _build_reference_index(vault)
        assert len(idx["foo"]) == 1  # Only the reference note, not the daily

    def test_case_insensitive(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        md1 = vault / "reference" / "a.md"
        md1.write_text("See [[FOO]].", encoding="utf-8")

        md2 = vault / "reference" / "b.md"
        md2.write_text("See [[foo]].", encoding="utf-8")

        idx = _build_reference_index(vault)
        assert len(idx["foo"]) == 2


# ── Notes referencing ───────────────────────────────────────────────


class TestFindNotesReferencing:
    def test_finds_references(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        md = vault / "reference" / "a.md"
        md.write_text("See [[foo]].", encoding="utf-8")

        idx = _build_reference_index(vault)
        refs = _find_notes_referencing(vault, "foo", idx)
        assert len(refs) == 1
        assert refs[0] == md

    def test_no_references(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        idx = _build_reference_index(vault)
        refs = _find_notes_referencing(vault, "nonexistent", idx)
        assert refs == []


# ── Full detection run ──────────────────────────────────────────────


class TestDetectCorrections:
    def test_single_correction_unpropagated(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        # Create correction note
        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\n---\n98.1% (159/162) were decayed.",
            encoding="utf-8",
        )

        # Create corrected note
        corrected = vault / "reference" / "bar.md"
        corrected.write_text(
            "---\ncorrected_by: [foo-correction]\n---\nnotes were decayed.",
            encoding="utf-8",
        )

        # Create referencing note (references bar but not foo)
        ref = vault / "reference" / "baz.md"
        ref.write_text("See [[bar]].", encoding="utf-8")

        report = detect_corrections(tmp_path)
        assert report.correction_pairs_checked == 1
        assert report.total_unpropagated == 1
        assert report.high_count == 1
        assert report.unpropagated[0].severity == "high"

    def test_no_unpropagated(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\n---\n98.1% (159/162) were decayed.",
            encoding="utf-8",
        )

        corrected = vault / "reference" / "bar.md"
        corrected.write_text(
            "---\ncorrected_by: [foo-correction]\n---\nnotes were decayed.",
            encoding="utf-8",
        )

        # Referencing note that also references the correction
        ref = vault / "reference" / "baz.md"
        ref.write_text("See [[bar]] and [[foo-correction]].", encoding="utf-8")

        report = detect_corrections(tmp_path)
        assert report.correction_pairs_checked == 1
        assert report.total_unpropagated == 0

    def test_no_corrections_found(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        md = vault / "reference" / "normal.md"
        md.write_text("---\nnote_type: analysis\n---\njust a note", encoding="utf-8")

        report = detect_corrections(tmp_path)
        assert report.correction_pairs_checked == 0
        assert report.total_unpropagated == 0

    def test_multiple_pairs(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        # Pair 1
        c1 = vault / "reference" / "a-correction.md"
        c1.write_text("---\nnote_type: correction\n---\n100% wrong.", encoding="utf-8")
        corr1 = vault / "reference" / "a.md"
        corr1.write_text("---\ncorrected_by: [a-correction]\n---\nwas wrong.", encoding="utf-8")

        # Pair 2
        c2 = vault / "reference" / "b-correction.md"
        c2.write_text("---\nnote_type: correction\n---\n50% wrong.", encoding="utf-8")
        corr2 = vault / "reference" / "b.md"
        corr2.write_text("---\ncorrected_by: [b-correction]\n---\nwas wrong.", encoding="utf-8")

        # Referencing both
        ref = vault / "reference" / "c.md"
        ref.write_text("See [[a]] and [[b]].", encoding="utf-8")

        report = detect_corrections(tmp_path)
        assert report.correction_pairs_checked == 2
        assert report.total_unpropagated == 2


# ── CascadeReport serialization ─────────────────────────────────────


class TestCascadeReport:
    def test_to_dict_empty(self):
        report = CascadeReport()
        d = report.to_dict()
        assert d["correction_pairs_checked"] == 0
        assert d["total_unpropagated"] == 0
        assert d["items"] == []

    def test_to_dict_with_items(self):
        report = CascadeReport()
        report.correction_pairs_checked = 2
        report.unpropagated.append(
            UnpropagatedCorrection(
                corrected_slug="a",
                corrected_title="A",
                correction_slug="b",
                correction_title="B",
                referencing_slug="c",
                referencing_title="C",
                severity="high",
                claim_changed="98.1%",
            )
        )
        d = report.to_dict()
        assert d["correction_pairs_checked"] == 2
        assert d["total_unpropagated"] == 1
        assert d["high"] == 1
        assert len(d["items"]) == 1
        assert d["items"][0]["corrected"] == "a"

    def test_to_markdown_table_empty(self):
        report = CascadeReport()
        assert "No unpropagated corrections" in report.to_markdown_table()

    def test_to_markdown_table_with_items(self):
        report = CascadeReport()
        report.unpropagated.append(
            UnpropagatedCorrection(
                corrected_slug="a",
                corrected_title="A",
                correction_slug="b",
                correction_title="B",
                referencing_slug="c",
                referencing_title="C",
                severity="high",
                claim_changed="98.1%",
            )
        )
        table = report.to_markdown_table()
        assert "| [[a]]" in table
        assert "| [[b]]" in table
        assert "| [[c]]" in table
        assert "| high" in table

    def test_count_properties(self):
        report = CascadeReport()
        report.unpropagated.append(
            UnpropagatedCorrection("a", "A", "b", "B", "c", "C", "high", "100%")
        )
        report.unpropagated.append(
            UnpropagatedCorrection("a", "A", "b", "B", "d", "D", "medium", "qual")
        )
        report.unpropagated.append(
            UnpropagatedCorrection("a", "A", "b", "B", "e", "E", "low", "nuance")
        )
        assert report.high_count == 1
        assert report.medium_count == 1
        assert report.low_count == 1
        assert report.total_unpropagated == 3


# ── Field resolution helpers ────────────────────────────────────────


class TestResolveFieldValue:
    """Tests for _resolve_field_value helper."""

    def test_bare_slug(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value("bar", vault)
        assert result == vault / "reference" / "bar.md"

    def test_bare_slug_list(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value(["bar", "baz"], vault)
        assert result == vault / "reference" / "bar.md"

    def test_wikilink_syntax(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value("[[bar]]", vault)
        assert result == vault / "reference" / "bar.md"

    def test_wikilink_syntax_list(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value(["[[bar]]"], vault)
        assert result == vault / "reference" / "bar.md"

    def test_wikilink_with_alias(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value("[[bar|Bar Title]]", vault)
        assert result == vault / "reference" / "bar.md"

    def test_foldered_slug(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value("[[reference/bar]]", vault)
        assert result == vault / "reference" / "bar.md"

    def test_no_match(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        result = _resolve_field_value("nonexistent", vault)
        assert result is None

    def test_empty_string(self, tmp_path):
        result = _resolve_field_value("", tmp_path)
        assert result is None

    def test_empty_list(self, tmp_path):
        result = _resolve_field_value([], tmp_path)
        assert result is None

    def test_whitespace_slug(self, tmp_path):
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        (vault / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _resolve_field_value("  bar  ", vault)
        assert result == vault / "reference" / "bar.md"


class TestTryResolveSlug:
    """Tests for _try_resolve_slug helper."""

    def test_root_slug(self, tmp_path):
        (tmp_path / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _try_resolve_slug("bar", tmp_path)
        assert result == tmp_path / "bar.md"

    def test_root_wikilink(self, tmp_path):
        (tmp_path / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _try_resolve_slug("[[bar]]", tmp_path)
        assert result == tmp_path / "bar.md"

    def test_root_with_alias(self, tmp_path):
        (tmp_path / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _try_resolve_slug("[[bar|Bar]]", tmp_path)
        assert result == tmp_path / "bar.md"

    def test_foldered_slug(self, tmp_path):
        (tmp_path / "reference").mkdir(parents=True)
        (tmp_path / "reference" / "bar.md").write_text("---\n---\nbody", encoding="utf-8")
        result = _try_resolve_slug("reference/bar", tmp_path)
        assert result == tmp_path / "reference" / "bar.md"

    def test_not_found(self, tmp_path):
        result = _try_resolve_slug("nonexistent", tmp_path)
        assert result is None

    def test_not_found_any_folder(self, tmp_path):
        result = _try_resolve_slug("projects/bar", tmp_path)
        assert result is None


# ── Priority ordering ───────────────────────────────────────────────


class TestFindCorrectedNotePriority:
    """Verify supersedes: resolves before corrected_by: scan."""

    def test_supersedes_beats_corrected_by(self, tmp_path):
        """When both supersedes: and corrected_by: exist, supersedes: wins."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        # correction points via supersedes to bar.md
        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nsupersedes: [bar]\n---\nfixed something",
            encoding="utf-8",
        )

        # bar.md has corrected_by pointing to this correction
        bar = vault / "reference" / "bar.md"
        bar.write_text(
            "---\ncorrected_by: [foo-correction]\n---\nwas wrong",
            encoding="utf-8",
        )

        # baz.md also has corrected_by pointing to this correction
        # (but supersedes should resolve to bar, not require scanning)
        baz = vault / "reference" / "baz.md"
        baz.write_text(
            "---\ncorrected_by: [foo-correction]\n---\nalso wrong",
            encoding="utf-8",
        )

        result = _find_corrected_note(correction, vault)
        assert result == bar

    def test_supersedes_wikilink_syntax(self, tmp_path):
        """supersedes: with [[wikilink]] syntax resolves correctly."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nsupersedes: [[bar]]\n---\nfixed",
            encoding="utf-8",
        )

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_supersedes_with_alias(self, tmp_path):
        """supersedes: with [[bar|Title]] alias strips correctly."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()
        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nsupersedes: [[bar|Original Note]]\n---\nfixed",
            encoding="utf-8",
        )

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_corrected_note_field(self, tmp_path):
        """corrected_note: field resolves before corrected_by: scan."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\ncorrected_note: [bar]\n---\nfixed",
            encoding="utf-8",
        )

        corrected = vault / "reference" / "bar.md"
        corrected.write_text("---\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == corrected

    def test_supersedes_list_resolves_first(self, tmp_path):
        """supersedes: as a list resolves the first matching entry."""
        vault = tmp_path / "cortex-memory"
        vault.mkdir()
        (vault / "reference").mkdir()

        correction = vault / "reference" / "foo-correction.md"
        correction.write_text(
            "---\nnote_type: correction\nsupersedes: [bar, baz]\n---\nfixed",
            encoding="utf-8",
        )

        bar = vault / "reference" / "bar.md"
        bar.write_text("---\n---\nwas wrong", encoding="utf-8")

        result = _find_corrected_note(correction, vault)
        assert result == bar
