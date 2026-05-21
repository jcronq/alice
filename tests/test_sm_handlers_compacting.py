"""Tests for the v3 ``sm:compacting`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.compacting import handle
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 19, 30, tzinfo=dt.timezone.utc)


def _services(
    comments: list[dict[str, Any]] | None = None,
) -> HandlerServices:
    return HandlerServices(
        ledger=EmittedLedger(),
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
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 100) -> dict[str, Any]:
    return {"number": number, "title": "compacting test"}


class TestBuildStarted:
    def test_build_started_transitions_to_building(self):
        result = handle(
            _issue(), _services(comments=[_comment("[SM] build-started")])
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.BUILDING
        assert result.reason == "build-started"


class TestContinue:
    def test_continue_records_progress(self):
        result = handle(
            _issue(),
            _services(
                comments=[
                    _comment(
                        '[SM] continue reason="compaction at 60%"'
                    )
                ]
            ),
        )
        assert isinstance(result, Continue)
        assert "60" in result.reason


class TestNoActionableInput:
    def test_no_comments_returns_none(self):
        # sm:compacting doesn't emit a triage-surface; the agent is
        # actively running. No comments = still working = None.
        assert handle(_issue(), _services()) is None

    def test_human_prose_only_returns_none(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("LGTM", author="random")]),
        )
        assert result is None


class TestParseError:
    def test_untrusted_author_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(
                comments=[_comment("[SM] build-started", author="rando")]
            ),
        )
        assert isinstance(result, EmitParseError)
        assert "untrusted" in result.reason

    def test_unknown_verb_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] some-fake-verb")]),
        )
        assert isinstance(result, EmitParseError)


class TestUnrelatedVerb:
    def test_route_to_study_from_compacting_is_ignored(self):
        # route-to-study is parseable + trusted but not legal from
        # sm:compacting — the handler skips it and keeps scanning.
        result = handle(
            _issue(),
            _services(comments=[_comment("[SM] route-to-study")]),
        )
        # No build-started AND route-to-study isn't applicable here,
        # so the handler returns None.
        assert result is None
