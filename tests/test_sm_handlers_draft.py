"""Tests for the v3 ``sm:draft`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any


from alice_forge.sm.handlers.draft import handle
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
    return dt.datetime(2026, 5, 21, 19, 0, tzinfo=dt.timezone.utc)


def _services(
    *,
    ledger: EmittedLedger | None = None,
    comments: list[dict[str, Any]] | None = None,
    trusted_authors: frozenset[str] = frozenset({"jcronq", "alice"}),
) -> HandlerServices:
    """Build a HandlerServices with all GitHub IO stubbed."""
    comments_list = list(comments or [])
    return HandlerServices(
        ledger=ledger or EmittedLedger(),
        repo="jcronq/alice",
        post_comment=lambda *a, **kw: None,
        list_comments=lambda repo, number: comments_list,
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda *a, **kw: None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        trusted_authors=trusted_authors,
        now=_now,
        log=lambda s: None,
    )


def _issue(number: int = 42, title: str = "Test issue") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/jcronq/alice/issues/{number}",
    }


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


class TestRouteToStudy:
    def test_bare_route_to_study(self):
        result = handle(
            _issue(), _services(comments=[_comment("[SM] route-to-study")])
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.NEEDS_STUDY
        assert result.art_swap is None

    def test_with_art_swap(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] route-to-study art=art:research_note")]),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.NEEDS_STUDY
        assert result.art_swap == "art:research_note"

    def test_trailing_prose_does_not_block(self):
        # The #300 bug we explicitly fixed in v3 parser — confirm
        # end-to-end through the handler.
        body = (
            "[SM] route-to-study\n"
            "Designer: see audit doc for the proposed fix."
        )
        result = handle(_issue(), _services(comments=[_comment(body)]))
        assert isinstance(result, Transition)
        assert result.target is SMState.NEEDS_STUDY


class TestSelect:
    def test_bare_select(self):
        result = handle(
            _issue(), _services(comments=[_comment("[SM] select")])
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.SELECTED
        assert result.art_swap is None

    def test_with_art_swap(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] select art=art:code")]),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.SELECTED
        assert result.art_swap == "art:code"

    def test_trailing_prose_does_not_block(self):
        body = (
            "[SM] select art=art:config_change\n"
            "Triager: trivial 3-line config fix, no study needed."
        )
        result = handle(_issue(), _services(comments=[_comment(body)]))
        assert isinstance(result, Transition)
        assert result.target is SMState.SELECTED
        assert result.art_swap == "art:config_change"


class TestReject:
    def test_reject_with_reason(self):
        result = handle(
            _issue(),
            _services(
                comments=[_comment('[SM] reject reason="duplicate of #5"')]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED
        assert "duplicate" in result.reason


class TestContinue:
    def test_continue_returns_continue_result(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment('[SM] continue reason="investigating overlap with #7"')
                ]
            ),
        )
        assert isinstance(result, Continue)
        assert "overlap" in result.reason

    def test_continue_with_findings(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        '[SM] continue reason="found candidate fix" '
                        'findings=[[2026-05-21-fix-candidate]]'
                    )
                ]
            ),
        )
        assert isinstance(result, Continue)
        assert result.findings == "[[2026-05-21-fix-candidate]]"


class TestParseError:
    def test_untrusted_author_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(
                comments=[_comment("[SM] route-to-study", author="rando")]
            ),
        )
        assert isinstance(result, EmitParseError)
        assert "untrusted author" in result.reason

    def test_unknown_verb_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] totally-not-a-verb")]),
        )
        assert isinstance(result, EmitParseError)
        assert "unknown verb" in result.reason


class TestTriageSurface:
    def test_no_comments_emits_triage_surface(self):
        result = handle(_issue(), _services(comments=[]))
        assert isinstance(result, SideEffect)
        assert result.name == "triage-surface"
        assert result.ttl_seconds is None  # cleared on label exit, not by time

    def test_human_prose_only_emits_triage_surface(self):
        # Non-[SM] comments don't drive transitions; should still
        # trigger triage on first visit.
        result = handle(
            _issue(),
            _services(comments=[_comment("LGTM, ship it", author="random")]),
        )
        assert isinstance(result, SideEffect)
        assert result.name == "triage-surface"

    def test_existing_triage_in_ledger_returns_none(self):
        # Dedup: if the ledger already has an active triage-surface
        # entry, the handler returns None.
        ledger = EmittedLedger()
        ledger.mark_emitted(
            42, "triage-surface", _now(), ttl_seconds=None
        )
        result = handle(_issue(), _services(ledger=ledger, comments=[]))
        assert result is None


class TestNewestFirstScan:
    def test_more_recent_verb_wins(self):
        # If multiple [SM] verbs are in the thread, the newest one
        # is the actionable one. v1 mirrors this behavior.
        comments = [
            _comment("[SM] route-to-study"),  # old (lower in chronological order)
            _comment('[SM] reject reason="changed mind"'),  # newest
        ]
        result = handle(_issue(), _services(comments=comments))
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED


class TestListCommentsFailure:
    def test_returns_none_on_list_comments_exception(self):
        # Transient GitHub error → return None and let the
        # dispatcher retry next cadence. NOT a parse error.
        def raise_(repo, number):
            raise RuntimeError("rate limited")

        services = HandlerServices(
            ledger=EmittedLedger(),
            repo="jcronq/alice",
            post_comment=lambda *a, **kw: None,
            list_comments=raise_,
            edit_labels=lambda *a, **kw: None,
            close_issue=lambda *a, **kw: None,
            find_linked_pr=lambda *a, **kw: None,
            pr_merge_status=lambda *a, **kw: None,
            master_ci_status=lambda *a, **kw: None,
            trusted_authors=frozenset({"jcronq"}),
            now=_now,
            log=lambda s: None,
        )
        result = handle(_issue(), services)
        assert result is None
