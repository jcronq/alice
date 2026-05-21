"""Tests for the v3 ``sm:needs_study`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.needs_study import (
    STUDY_HINT_NAME,
    VAULT_AUTO_ADVANCE_NAME,
    handle,
)
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    SideEffect,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 20, 30, tzinfo=dt.timezone.utc)


def _services(
    *,
    ledger: EmittedLedger | None = None,
    comments: list[dict[str, Any]] | None = None,
    research_slug: str | None = None,
) -> HandlerServices:
    return HandlerServices(
        ledger=ledger or EmittedLedger(),
        repo="jcronq/alice",
        post_comment=lambda *a, **kw: None,
        list_comments=lambda repo, number: list(comments or []),
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda *a, **kw: None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        trusted_authors=frozenset({"jcronq", "alice"}),
        now=_now,
        log=lambda s: None,
        research_resolver=(lambda n: research_slug) if research_slug else None,
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 300) -> dict[str, Any]:
    return {"number": number, "title": "study test"}


class TestStudyComplete:
    def test_study_complete_transitions_to_selected(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        "[SM] study-complete art=art:code findings=[[my-slug]]"
                    )
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.SELECTED
        assert result.art_swap == "art:code"
        assert "my-slug" in result.reason


class TestStudyBlocked:
    def test_study_blocked_transitions_to_blocked(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        '[SM] study-blocked reason="dep #99 not in master"'
                    )
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.BLOCKED
        assert "dep #99" in result.reason


class TestStudyRejected:
    def test_study_rejected_transitions_to_rejected(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment('[SM] study-rejected reason="duplicate of #5"')
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED


class TestContinue:
    def test_continue_records_progress(self):
        result = handle(
            _issue(),
            _services(
                comments=[_comment('[SM] continue reason="reading prior art"')]
            ),
        )
        assert isinstance(result, Continue)
        assert "prior art" in result.reason


class TestParseError:
    def test_unknown_verb_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] random-verb")]),
        )
        assert isinstance(result, EmitParseError)


class TestVaultAutoAdvance:
    def test_research_resolver_synthesizes_study_complete(self):
        result = handle(
            _issue(),
            _services(comments=[], research_slug="2026-05-21-investigation"),
        )
        assert isinstance(result, SideEffect)
        assert result.name == VAULT_AUTO_ADVANCE_NAME
        assert "2026-05-21-investigation" in result.body
        assert "auto-posted=true" in result.body

    def test_research_resolver_none_falls_through_to_hint(self):
        # No vault match, no comments → emit study-hint instead.
        result = handle(
            _issue(),
            _services(comments=[], research_slug=None),
        )
        assert isinstance(result, SideEffect)
        assert result.name == STUDY_HINT_NAME


class TestHintEmission:
    def test_first_visit_emits_hint(self):
        result = handle(_issue(), _services())
        assert isinstance(result, SideEffect)
        assert result.name == STUDY_HINT_NAME
        assert result.metadata["hint_path"].endswith("issue300.md")

    def test_hint_dedup_via_ledger(self):
        ledger = EmittedLedger()
        ledger.mark_emitted(300, STUDY_HINT_NAME, _now(), ttl_seconds=None)
        result = handle(_issue(), _services(ledger=ledger))
        assert result is None  # already emitted

    def test_existing_transition_skips_hint(self):
        # If thinking already posted study-complete, the handler
        # transitions immediately — no hint wasted on an
        # already-resolving issue.
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        "[SM] study-complete art=art:code findings=[[done]]"
                    )
                ]
            ),
        )
        assert isinstance(result, Transition)
