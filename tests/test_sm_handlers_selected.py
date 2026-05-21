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
