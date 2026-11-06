"""Tests for the per-task effort-level dial (issue #642).

Slice-1 coverage:

  * :func:`alice_forge.dispatcher.effort.read_effort` label precedence,
    verb override, default fallback.
  * :func:`alice_forge.dispatcher.spawn.compose_spawn_prompt` rendering
    per level — high includes the regression-suite phrase, low includes
    the ship-and-iterate phrase, medium adds no block.

The status-site pill-cycle UI and worker self-escalation ride on later
slices (see #642).
"""

from __future__ import annotations

from typing import Any

import pytest

from alice_forge.dispatcher import constants as sm_constants
from alice_forge.dispatcher.effort import (
    DEFAULT,
    LEVELS,
    Effort,
    effort_from_comments,
    effort_from_labels,
    effort_rules_block,
    read_effort,
)
from alice_forge.dispatcher.spawn import compose_spawn_prompt


# ---------------------------------------------------------------------------
# read_effort — resolution order tests
# ---------------------------------------------------------------------------


REPO = "jcronq/alice"


def _issue(labels: list[str]) -> dict[str, Any]:
    """Build a minimal ``gh issue view --json labels`` payload."""
    return {"number": 642, "labels": [{"name": name} for name in labels]}


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    """Build a minimal ``gh issue view --json comments`` entry."""
    return {"body": body, "author": {"login": author}}


class TestReadEffortLabelPrecedence:
    """``effort:<level>`` label alone → level."""

    @pytest.mark.parametrize("level", list(LEVELS))
    def test_single_label_wins(self, level: Effort) -> None:
        issue = _issue([f"effort:{level}", "art:code"])

        def get_issue(_repo: str, _n: int) -> dict:
            return issue

        def list_comments(_repo: str, _n: int) -> list:
            return []

        assert (
            read_effort(
                642,
                REPO,
                get_issue=get_issue,
                list_comments=list_comments,
            )
            == level
        )

    def test_effort_high_label_yields_high(self) -> None:
        """Explicit spec case: ``effort:high`` label → ``"high"``."""
        issue = _issue(["effort:high", "sm:selected"])

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: [],
            )
            == "high"
        )

    def test_multiple_labels_strongest_wins(self) -> None:
        """When somebody stacks ``effort:low`` + ``effort:high`` the
        strongest level wins — otherwise an operator could silently
        drop effort just by adding a second label."""
        issue = _issue(["effort:low", "effort:high", "effort:medium"])

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: [],
            )
            == "high"
        )

    def test_unknown_effort_suffix_is_ignored(self) -> None:
        """A bogus ``effort:banana`` label does not resolve — fall
        through to the next resolution rung."""
        issue = _issue(["effort:banana", "effort:high"])

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: [],
            )
            == "high"
        )


class TestReadEffortVerbOverride:
    """``[SM] effort=<level>`` comment beats the label."""

    def test_verb_overrides_label(self) -> None:
        """Explicit spec case: ``effort:low`` label +
        ``[SM] effort=high`` in latest comment → ``"high"``."""
        issue = _issue(["effort:low"])
        comments = [_comment("[SM] effort=high reason=\"going deep\"")]

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: comments,
            )
            == "high"
        )

    def test_latest_verb_wins_over_earlier_verb(self) -> None:
        """When multiple ``[SM] effort=X`` amendments stack, the most
        recent (last in the chronological list) wins."""
        issue = _issue([])  # no label — isolate the verb-scan
        comments = [
            _comment("[SM] effort=high"),
            _comment("[SM] effort=max"),
            _comment("[SM] effort=low"),
        ]

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: comments,
            )
            == "low"
        )

    def test_untrusted_author_verb_ignored(self) -> None:
        """A ``[SM] effort=max`` comment from an untrusted login must
        not flip the dial — otherwise any GitHub drive-by could crank
        the effort on an open task."""
        issue = _issue(["effort:medium"])
        comments = [_comment("[SM] effort=max", author="stranger")]

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: comments,
                trusted_authors=frozenset({"jcronq"}),
            )
            == "medium"
        )

    def test_verb_inline_in_prose_does_not_match(self) -> None:
        """``[SM] effort=high`` has to be at the start of the comment.
        A casual mention inside a longer paragraph (``I think [SM]
        effort=high would help``) must NOT trigger a flip — the anchor
        protects against accidental activation via quoting."""
        issue = _issue([])
        comments = [
            _comment(
                "I think we might want [SM] effort=max here eventually."
            )
        ]

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: comments,
            )
            == DEFAULT
        )


class TestReadEffortDefault:
    """No label and no verb → :data:`DEFAULT` (``"medium"``)."""

    def test_no_label_no_verb_is_medium(self) -> None:
        """Explicit spec case: no label, no verb → ``"medium"``."""
        issue = _issue([])

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: [],
            )
            == "medium"
        )

    def test_non_effort_labels_do_not_affect_default(self) -> None:
        issue = _issue(["sm:selected", "art:code", "priority:high"])

        assert (
            read_effort(
                642,
                REPO,
                get_issue=lambda _r, _n: issue,
                list_comments=lambda _r, _n: [],
            )
            == "medium"
        )

    def test_get_issue_failure_falls_back_to_default(self) -> None:
        """A gh transport failure degrades to ``DEFAULT`` rather than
        propagating — so a transient 404 / rate-limit can't block
        spawns in the live dispatcher path."""

        def boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("gh went splat")

        assert (
            read_effort(
                642,
                REPO,
                get_issue=boom,
                list_comments=boom,
            )
            == DEFAULT
        )


# ---------------------------------------------------------------------------
# Low-level readers — separately testable
# ---------------------------------------------------------------------------


class TestEffortFromLabels:
    def test_malformed_label_entries_are_skipped(self) -> None:
        labels = [
            {},  # no name key
            {"name": None},  # non-string
            "not-a-dict",
            {"name": "effort:high"},
        ]
        # labels is intentionally heterogeneous to exercise the
        # defensive type checks — only the one valid entry wins.
        assert effort_from_labels(labels) == "high"  # type: ignore[arg-type]

    def test_empty_labels_returns_none(self) -> None:
        assert effort_from_labels([]) is None


class TestEffortFromComments:
    def test_trailing_fields_do_not_block_match(self) -> None:
        """``[SM] effort=high reason="..."`` is still an amendment."""
        assert (
            effort_from_comments(
                [_comment("[SM] effort=high reason=\"deep dive\"")]
            )
            == "high"
        )

    def test_leading_whitespace_tolerated(self) -> None:
        """Comment bodies occasionally round-trip through markdown
        renderers that leading-pad the first line. ``.lstrip()`` in the
        matcher keeps the amendment intact."""
        assert (
            effort_from_comments([_comment("  [SM] effort=max")]) == "max"
        )


# ---------------------------------------------------------------------------
# Prompt rendering per level
# ---------------------------------------------------------------------------


# Canonical issue + spawn-config row — reuse the config_change
# worker row from the dispatcher constants rather than re-building
# an AgentSpec locally, so the test tracks real production wiring.
_SPAWN_ROW = sm_constants.SPAWN_MAP[("sm:selected", "art:config_change")]


def _canonical_issue() -> dict[str, Any]:
    return {
        "number": 642,
        "title": "Example task title",
        "body": "Example body.",
        "labels": [{"name": "art:config_change"}],
        "author": {"login": "jcronq"},
    }


class TestComposeSpawnPromptEffort:
    def test_high_includes_regression_suite_phrase(self) -> None:
        prompt = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW, effort="high"
        )
        assert "regression suite" in prompt
        assert "## Effort: high" in prompt

    def test_low_includes_ship_and_iterate_phrase(self) -> None:
        prompt = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW, effort="low"
        )
        assert "ship-and-iterate" in prompt
        assert "## Effort: low" in prompt

    def test_medium_adds_no_effort_block(self) -> None:
        """Medium is the baseline — the prompt must stay byte-identical
        to the no-effort-arg render so the existing golden fixtures
        continue to match without regeneration."""
        prompt_default = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW
        )
        prompt_medium = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW, effort="medium"
        )
        assert prompt_default == prompt_medium
        assert "## Effort:" not in prompt_medium

    def test_max_includes_go_deep_phrase(self) -> None:
        prompt = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW, effort="max"
        )
        assert "go-deep-until-done" in prompt
        assert "## Effort: max" in prompt
        assert "code-review max" in prompt

    def test_effort_block_sits_between_trailer_and_outro(self) -> None:
        """The "## Effort" block must sit AFTER the behavioral trailer
        and BEFORE the final "Operate as a real engineer" outro — so
        the effort rules read as an added expectation layered on top
        of the standard framing, not a replacement."""
        prompt = compose_spawn_prompt(
            _canonical_issue(), _SPAWN_ROW, effort="high"
        )
        trailer_idx = prompt.index("## Constraint:")
        effort_idx = prompt.index("## Effort: high")
        outro_idx = prompt.index("Operate as a real engineer")
        assert trailer_idx < effort_idx < outro_idx


class TestEffortRulesBlock:
    """The lower-level block helper matches the per-level contract."""

    def test_medium_is_empty(self) -> None:
        assert effort_rules_block("medium") == ""

    def test_low_mentions_draft_pr(self) -> None:
        assert "draft PR" in effort_rules_block("low")

    def test_high_mentions_full_suite(self) -> None:
        assert "full" in effort_rules_block("high").lower()

    def test_max_mentions_sub_reviewers(self) -> None:
        assert "sub-reviewers" in effort_rules_block("max")
