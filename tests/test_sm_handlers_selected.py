"""Tests for the v3 ``sm:selected`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.selected import handle
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 21, 30, tzinfo=dt.timezone.utc)


def _services(
    *,
    pr: dict[str, Any] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> HandlerServices:
    return HandlerServices(
        ledger=EmittedLedger(),
        repo="jcronq/alice",
        post_comment=lambda *a, **kw: None,
        list_comments=lambda repo, number: list(comments or []),
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda repo, number: pr,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        trusted_authors=frozenset({"jcronq", "alice"}),
        now=_now,
        log=lambda s: None,
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 500) -> dict[str, Any]:
    return {"number": number, "title": "selected test"}


class TestT1LinkedPR:
    def test_open_pr_transitions_to_reviewing(self):
        pr = {"state": "OPEN", "url": "https://github.com/jcronq/alice/pull/501"}
        result = handle(_issue(), _services(pr=pr))
        assert isinstance(result, Transition)
        assert result.target is SMState.REVIEWING
        assert result.metadata["transition_class"] == "T1"

    def test_closed_pr_returns_none(self):
        pr = {"state": "CLOSED", "url": "https://github.com/jcronq/alice/pull/502"}
        result = handle(_issue(), _services(pr=pr))
        assert result is None


class TestReturnToStudy:
    def test_return_to_study_transitions_to_needs_study(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        '[SM] return-to-study reason="more vault prior art needed"'
                    )
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.NEEDS_STUDY


class TestContinue:
    def test_continue_records_progress(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment('[SM] continue reason="spawn queued, awaiting slot"')
                ]
            ),
        )
        assert isinstance(result, Continue)


class TestParseError:
    def test_unknown_verb_returns_parse_error(self):
        result = handle(
            _issue(), _services(comments=[_comment("[SM] fake-verb")])
        )
        assert isinstance(result, EmitParseError)


class TestNoActionableInput:
    def test_no_pr_no_verbs_returns_none(self):
        # v1's spawn / hello / dep-check would fire here. v3
        # returns None — the divergence is expected and recorded by
        # the diff job until Phase 3.
        assert handle(_issue(), _services()) is None


class TestOrphanDesignReadyRecovery:
    """Recovery for the missing ``spawn-dispatch-art-code`` EventTransition.

    The v3 transition table declares SELECTED → DESIGNING on the
    ``spawn-dispatch-art-code`` event, but v1's spawn dispatch never
    swaps the label, so design-ready arrives while the issue is still
    ``sm:selected``. Issue #295 hit this — 228 thinking-spawn-started
    comments in one day. v3 SELECTED now detects the orphan and
    transitions to DESIGN_REVIEW.
    """

    def test_spawn_started_plus_design_ready_transitions_to_design_review(self):
        comments = [
            _comment(
                "[SM] thinking-spawn-started task=#295 artifact=art:code "
                "phase=per_issue_design runtime=claude-agent-sdk:opus "
                "spawn_id=spawn-295-1779459000 ts=2026-05-22T14:10:00+00:00"
            ),
            _comment(
                "[SM] design-ready note=[[2026-05-22-issue295-surface-dedup-layer2]] "
                "author=alice",
                author="alice",
            ),
        ]
        result = handle(_issue(295), _services(comments=comments))
        assert isinstance(result, Transition)
        assert result.target is SMState.DESIGN_REVIEW
        # parse_comment preserves the wikilink brackets in field
        # values (the KV regex captures everything non-whitespace
        # after ``=``); the DESIGNING handler does the same in its
        # metadata.
        assert result.metadata["design_note"] == (
            "[[2026-05-22-issue295-surface-dedup-layer2]]"
        )
        assert result.metadata["recovery"] == (
            "selected-to-designing-event-skipped"
        )

    def test_spawn_started_without_design_ready_returns_none(self):
        # v1's silent-spawn guard handles this case (transitions to
        # sm:blocked). v3 stays out of the way.
        comments = [
            _comment(
                "[SM] thinking-spawn-started task=#295 artifact=art:code "
                "phase=per_issue_design"
            ),
        ]
        assert handle(_issue(295), _services(comments=comments)) is None

    def test_design_ready_without_spawn_started_returns_none(self):
        # Defensive — a design-ready posted before any spawn-started
        # is malformed protocol; don't auto-transition on it alone.
        comments = [
            _comment(
                "[SM] design-ready note=[[some-design]]",
                author="alice",
            ),
        ]
        assert handle(_issue(295), _services(comments=comments)) is None

    def test_untrusted_author_does_not_trigger_recovery(self):
        comments = [
            _comment(
                "[SM] thinking-spawn-started task=#295 artifact=art:code",
                author="random-user",
            ),
            _comment(
                "[SM] design-ready note=[[evil]]",
                author="random-user",
            ),
        ]
        result = handle(_issue(295), _services(comments=comments))
        # design-ready from untrusted author is rejected by parse_comment
        # as a parse error before our recovery check; that's fine — the
        # important thing is no Transition to DESIGN_REVIEW slips through.
        assert not isinstance(result, Transition)
