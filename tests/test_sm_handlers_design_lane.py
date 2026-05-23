"""Tests for the v3 design-lane handlers: designing, design_review, designed."""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.handlers.designed import handle as handle_designed
from alice_forge.sm.handlers.designing import handle as handle_designing
from alice_forge.sm.handlers.design_review import (
    DESIGN_REVISION_CAP,
    DESIGN_REVISION_NAME,
    handle as handle_design_review,
)
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    Continue,
    Transition,
)
from alice_forge.sm.services import HandlerServices
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 21, 0, tzinfo=dt.timezone.utc)


def _services(
    *,
    ledger: EmittedLedger | None = None,
    comments: list[dict[str, Any]] | None = None,
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
    )


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _issue(number: int = 400, labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": "design lane test",
        "labels": [{"name": name} for name in (labels or [])],
    }


class TestDesigning:
    def test_design_ready_transitions(self):
        result = handle_designing(
            _issue(),
            _services(
                comments=[
                    _comment("[SM] design-ready note=[[2026-05-21-design]]")
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.DESIGN_REVIEW
        assert "2026-05-21-design" in result.reason

    def test_continue_records_progress(self):
        result = handle_designing(
            _issue(),
            _services(comments=[_comment('[SM] continue reason="drafting §3"')]),
        )
        assert isinstance(result, Continue)
        assert "§3" in result.reason

    def test_no_design_ready_returns_none(self):
        assert handle_designing(_issue(), _services()) is None


class TestDesignReview:
    def test_design_approved_transitions_to_designed(self):
        result = handle_design_review(
            _issue(),
            _services(comments=[_comment("[SM] design-approved")]),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.DESIGNED
        assert result.metadata.get("clear_revision_counter") is True

    def test_design_rejected_transitions_to_rejected(self):
        result = handle_design_review(
            _issue(),
            _services(
                comments=[_comment('[SM] design-rejected reason="too risky"')]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED

    def test_design_revise_below_cap_bounces_to_designing(self):
        result = handle_design_review(
            _issue(),
            _services(
                comments=[
                    _comment('[SM] design-revise reason="add error handling"')
                ]
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.DESIGNING
        assert result.metadata["revision_count"] == 1

    def test_design_revise_at_cap_routes_to_rejected(self):
        # Pre-seed the ledger with cap revisions, so the next bump
        # would exceed.
        ledger = EmittedLedger()
        ledger.mark_emitted(
            400,
            DESIGN_REVISION_NAME,
            _now(),
            ttl_seconds=None,
            metadata={"count": DESIGN_REVISION_CAP},
        )
        result = handle_design_review(
            _issue(),
            _services(
                ledger=ledger,
                comments=[
                    _comment('[SM] design-revise reason="still not good"')
                ],
            ),
        )
        assert isinstance(result, Transition)
        assert result.target is SMState.REJECTED
        assert "capped" in result.reason

    def test_continue_records_progress(self):
        result = handle_design_review(
            _issue(),
            _services(
                comments=[_comment('[SM] continue reason="reviewing §4 now"')]
            ),
        )
        assert isinstance(result, Continue)

    def test_no_comments_returns_none(self):
        assert handle_design_review(_issue(), _services()) is None


class TestDesigned:
    """v3 DESIGNED handler — event-driven transitions stay in v1.

    Before #333 made v3 authoritative, v3's designed handler returned
    a *predicted* ``Transition(BUILDING)`` / ``Transition(COMPACTING)``
    for the dual-run logger to compare against v1-actual. Post-#333
    the dispatcher applies whatever v3 returns, so the predicted
    transition flipped the label without ever spawning a build worker
    — bug observed live on #294/#296/#297/#323 (transitioned to
    sm:building, no speaking-agent ever started, issues stuck).

    Fix: return ``None`` for the event case so v1's
    ``_process_designed`` runs and does the actual spawn + label
    flip atomically. v3 only owns verb-driven decisions
    (continue / parse-error) here.
    """

    def test_art_code_event_transition_returns_none(self):
        # v1's _process_designed owns build-spawn-dispatch.
        result = handle_designed(
            _issue(labels=["art:code"]),
            _services(),
        )
        assert result is None

    def test_other_art_event_transition_returns_none(self):
        # v1's _process_designed owns the legacy compact-signal lane.
        result = handle_designed(
            _issue(labels=["art:research_note"]),
            _services(),
        )
        assert result is None

    def test_no_art_label_returns_none(self):
        result = handle_designed(_issue(labels=[]), _services())
        assert result is None

    def test_continue_records_progress(self):
        result = handle_designed(
            _issue(labels=["art:code"]),
            _services(
                comments=[
                    _comment('[SM] continue reason="waiting for spawn slot"')
                ]
            ),
        )
        # Continue is found in the scan loop BEFORE the label-based
        # prediction. Both behaviors are arguably correct; the
        # continue is more informative.
        assert isinstance(result, Continue)
        assert "spawn slot" in result.reason
