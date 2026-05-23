"""Unit tests for :mod:`alice_forge.dispatcher.art_classifier`.

Covers every case from the design note's Tests section:
``cortex-memory/designs/2026-05-22-issue294-art-classifier.md`` (issue #294).
"""

from __future__ import annotations

from alice_forge.dispatcher.art_classifier import auto_label


def test_auto_label_bug_keyword_match() -> None:
    """A draft talking about a stall maps to ``art:bug``."""
    assert (
        auto_label(
            "EC-2: Auto-art-label", "draft issues stall silently...", []
        )
        == "art:bug"
    )


def test_auto_label_enhancement_keyword_match() -> None:
    """Title with ``feature`` + body with ``improve``/``ergonomics``."""
    assert (
        auto_label("Add feature X", "improve ergonomics", [])
        == "art:enhancement"
    )


def test_auto_label_research_note_keyword_match() -> None:
    """Research/investigate keywords map to ``art:research_note``."""
    assert (
        auto_label("Research: spike on X", "investigate X", [])
        == "art:research_note"
    )


def test_auto_label_design_keyword_match() -> None:
    """Design + architecture keywords map to ``art:design``."""
    assert (
        auto_label("Design: new protocol", "architecture matters", [])
        == "art:design"
    )


def test_auto_label_no_match_falls_back_to_pending() -> None:
    """Zero keyword matches → conservative ``art:pending`` fallback."""
    assert auto_label("Untitled", "", []) == "art:pending"


def test_auto_label_returns_none_when_already_labelled() -> None:
    """Pre-existing ``art:*`` short-circuits — never override."""
    assert auto_label("bug", "error", ["art:code"]) is None


def test_auto_label_returns_none_when_art_pending_already_set() -> None:
    """``art:pending`` is itself an ``art:*`` label — no re-classify."""
    assert auto_label("bug everywhere", "error", ["art:pending"]) is None


def test_auto_label_title_double_weight() -> None:
    """Title matches count 2× — body text alone can't out-score title."""
    # Title has "bug" (counts 2×: once in combined text, once in title
    # bonus). Body has no keywords from any other category, so bug wins.
    assert auto_label("bug in X", "unrelated text", []) == "art:bug"


def test_auto_label_tie_break_prefers_bug_over_enhancement() -> None:
    """Ties between equal-scoring categories fall back to dict
    insertion order (bug → enhancement → research_note → design)."""
    # Title carries one bug keyword ("fix") and one enhancement keyword
    # ("add") — equal title bonus, equal combined score. Insertion order
    # puts ``art:bug`` first.
    assert auto_label("fix and add", "", []) == "art:bug"


def test_auto_label_handles_multi_word_keyword() -> None:
    """``state machine`` is a phrase keyword in the design category."""
    assert (
        auto_label("State machine dispatcher", "", []) == "art:design"
    )


def test_auto_label_no_labels_arg_accepts_empty_list() -> None:
    """Sanity: empty existing_labels works as expected."""
    assert auto_label("crash", "", []) == "art:bug"
