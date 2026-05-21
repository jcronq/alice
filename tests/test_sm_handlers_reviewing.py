"""Tests for the v3 ``sm:reviewing`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.reviewing import handle
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 22, 0, tzinfo=dt.timezone.utc)


def _services(
    *,
    pr: dict[str, Any] | None = None,
    master_ci: Any = None,
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
        master_ci_status=lambda *a, **kw: master_ci,
        trusted_authors=frozenset({"jcronq", "alice"}),
        now=_now,
        log=lambda s: None,
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 600) -> dict[str, Any]:
    return {"number": number, "title": "reviewing test"}


class TestT2Done:
    def test_merged_pr_green_master_transitions_to_done(self):
        result = handle(
            _issue(),
            _services(
                pr={"state": "MERGED", "url": "https://..."},
                master_ci={"conclusion": "SUCCESS"},
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.DONE
        assert result.metadata["transition_class"] == "T2"

    def test_green_string_form_accepted(self):
        result = handle(
            _issue(),
            _services(
                pr={"state": "MERGED"},
                master_ci="green",
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.DONE


class TestT3Rollback:
    def test_merged_pr_red_master_transitions_to_building(self):
        result = handle(
            _issue(),
            _services(
                pr={"state": "MERGED"},
                master_ci={"conclusion": "FAILURE"},
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.BUILDING
        assert result.metadata["transition_class"] == "T3"


class TestPRClosed:
    def test_closed_unmerged_transitions_to_rejected(self):
        result = handle(
            _issue(),
            _services(pr={"state": "CLOSED"}),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED


class TestCIPending:
    def test_merged_pr_ci_pending_returns_none(self):
        # No green/red verdict yet → no transition. v1's verify/
        # rebase machinery may still act; v3 returns None.
        result = handle(
            _issue(),
            _services(
                pr={"state": "MERGED"},
                master_ci=None,
            ),
        )
        assert result is None

    def test_open_pr_returns_none(self):
        # Open PR is not yet a terminal-transition trigger.
        result = handle(
            _issue(),
            _services(pr={"state": "OPEN"}),
        )
        assert result is None


class TestContinue:
    def test_continue_records_progress_during_ci_pending(self):
        result = handle(
            _issue(),
            _services(
                pr={"state": "MERGED"},
                master_ci=None,  # pending
                comments=[
                    _comment(
                        '[SM] continue reason="waiting on flaky integration tests"'
                    )
                ],
            ),
        )
        assert isinstance(result, Continue)
        assert "flaky" in result.reason


class TestParseError:
    def test_unknown_verb_returns_parse_error(self):
        result = handle(
            _issue(),
            _services(
                pr={"state": "OPEN"},
                comments=[_comment("[SM] mystery-verb")],
            ),
        )
        assert isinstance(result, EmitParseError)


class TestNoPR:
    def test_no_pr_returns_none(self):
        # Edge case — sm:reviewing without a linked PR. v1 handles
        # this in its rebase machinery; v3 returns None.
        assert handle(_issue(), _services(pr=None)) is None
