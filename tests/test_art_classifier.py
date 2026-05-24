"""Unit tests for :mod:`alice_forge.dispatcher.art_classifier`.

Covers every case from the design note's Tests section:
``cortex-memory/designs/2026-05-22-issue294-art-classifier.md`` (issue #294).

The classifier curates keywords under internal *categories* (bug /
enhancement / research / design) but only ever emits a label from
``ART_LABEL_WHITELIST`` — bug/enhancement/design collapse to ``art:code``,
research to ``art:research_note``. Emitting a non-whitelisted label (the
original bug) made ``edit_labels`` fail every poll cycle because the label
neither existed in the repo nor passed the dispatcher's trust filter.
"""

from __future__ import annotations

from alice_forge.dispatcher.art_classifier import (
    _CATEGORY_LABEL,
    auto_label,
)
from alice_forge.dispatcher.constants import ART_LABEL_WHITELIST


def test_auto_label_bug_keyword_match() -> None:
    """A draft talking about a stall (bug category) maps to ``art:code``."""
    assert (
        auto_label(
            "EC-2: Auto-art-label", "draft issues stall silently...", []
        )
        == "art:code"
    )


def test_auto_label_enhancement_keyword_match() -> None:
    """Title with ``feature`` + body with ``improve``/``ergonomics``
    (enhancement category) maps to ``art:code``."""
    assert (
        auto_label("Add feature X", "improve ergonomics", []) == "art:code"
    )


def test_auto_label_research_note_keyword_match() -> None:
    """Research/investigate keywords map to ``art:research_note``."""
    assert (
        auto_label("Research: spike on X", "investigate X", [])
        == "art:research_note"
    )


def test_auto_label_design_keyword_match() -> None:
    """Design + architecture keywords (design category) map to ``art:code``
    — design work produces code artifacts; there is no ``art:design``."""
    assert (
        auto_label("Design: new protocol", "architecture matters", [])
        == "art:code"
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
    """Title matches count 2×, so a single title keyword out-scores a
    single body keyword from a different category. Title carries
    ``research`` (score 2: combined + title bonus); body carries one
    code keyword ``fix`` (score 1) — research wins despite ``art:code``
    sorting earlier, proving the 2× weighting is live."""
    assert (
        auto_label("research plan", "fix the thing", [])
        == "art:research_note"
    )


def test_auto_label_tie_break_prefers_code_over_research() -> None:
    """On an exact score tie, dict insertion order wins. ``fix`` (bug →
    code) and ``audit`` (research) each score 2 here; bug is inserted
    before research, so the emitted label is ``art:code``."""
    assert auto_label("fix and audit", "", []) == "art:code"


def test_auto_label_handles_multi_word_keyword() -> None:
    """``state machine`` is a phrase keyword in the design category →
    ``art:code``."""
    assert auto_label("State machine dispatcher", "", []) == "art:code"


def test_auto_label_no_labels_arg_accepts_empty_list() -> None:
    """Sanity: empty existing_labels works as expected (bug → code)."""
    assert auto_label("crash", "", []) == "art:code"


def test_every_emitted_label_is_whitelisted() -> None:
    """Guard against regressing the original bug: every category must map
    to a label the dispatcher can actually apply, and the ``art:pending``
    fallback must itself be whitelisted."""
    for category, label in _CATEGORY_LABEL.items():
        assert label in ART_LABEL_WHITELIST, (
            f"category {category!r} maps to non-whitelisted {label!r}"
        )
    assert "art:pending" in ART_LABEL_WHITELIST
