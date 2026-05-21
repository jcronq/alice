"""Tests for the v3 ``sm:building`` handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.building import handle
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    EmitParseError,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 20, 0, tzinfo=dt.timezone.utc)


def _services(
    *,
    pr: dict[str, Any] | None = None,
    pr_raises: Exception | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> HandlerServices:
    def _find(repo, number):
        if pr_raises:
            raise pr_raises
        return pr

    return HandlerServices(
        ledger=EmittedLedger(),
        repo="jcronq/alice",
        post_comment=lambda *a, **kw: None,
        list_comments=lambda repo, number: list(comments or []),
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=_find,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        trusted_authors=frozenset({"jcronq", "alice"}),
        now=_now,
        log=lambda s: None,
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 200) -> dict[str, Any]:
    return {"number": number, "title": "building test"}


class TestLinkedPROpen:
    def test_open_pr_transitions_to_reviewing(self):
        pr = {"state": "OPEN", "url": "https://github.com/jcronq/alice/pull/123"}
        result = handle(_issue(), _services(pr=pr))
        assert isinstance(result, Transition)
        assert result.target is SMState.REVIEWING
        assert "pull/123" in result.reason
        assert result.metadata["pr_url"].endswith("/123")

    def test_state_case_insensitive(self):
        pr = {"state": "open", "url": "https://github.com/jcronq/alice/pull/124"}
        result = handle(_issue(), _services(pr=pr))
        assert isinstance(result, Transition)

    def test_pr_url_missing_falls_back(self):
        pr = {"state": "OPEN"}  # no url
        result = handle(_issue(), _services(pr=pr))
        assert isinstance(result, Transition)
        assert "<unknown>" in result.reason


class TestLinkedPRNotActionable:
    def test_no_pr_returns_none(self):
        # Worker is still producing the build; no PR yet, no action.
        assert handle(_issue(), _services(pr=None)) is None

    def test_closed_pr_returns_none(self):
        pr = {"state": "CLOSED", "url": "https://github.com/jcronq/alice/pull/125"}
        result = handle(_issue(), _services(pr=pr))
        # v1 also no-ops; PR exists but is closed.
        assert result is None

    def test_merged_pr_returns_none(self):
        pr = {"state": "MERGED", "url": "https://github.com/jcronq/alice/pull/126"}
        result = handle(_issue(), _services(pr=pr))
        assert result is None

    def test_find_linked_pr_raises_returns_none(self):
        result = handle(
            _issue(), _services(pr_raises=RuntimeError("rate-limited"))
        )
        # Transient error; handler logs + returns None so the
        # dispatcher retries next cadence.
        assert result is None


class TestContinueDuringBuild:
    def test_continue_records_progress(self):
        result = handle(
            _issue(),
            _services(
                pr=None,
                comments=[
                    _comment(
                        '[SM] continue reason="build at 70%, retrying flaky test"'
                    )
                ],
            ),
        )
        assert isinstance(result, Continue)
        assert "70" in result.reason

    def test_pr_open_overrides_continue(self):
        # If both signals are present (PR open AND continue in thread),
        # the PR transition wins — it's the dominant signal for this
        # state.
        pr = {"state": "OPEN", "url": "https://github.com/jcronq/alice/pull/127"}
        result = handle(
            _issue(),
            _services(
                pr=pr,
                comments=[_comment('[SM] continue reason="halfway through"')],
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REVIEWING


class TestParseError:
    def test_untrusted_author_surfaces_parse_error(self):
        result = handle(
            _issue(),
            _services(
                pr=None,
                comments=[_comment("[SM] build-started", author="rando")],
            ),
        )
        assert isinstance(result, EmitParseError)
