"""Tests for the Phase 4 v3 ``apply_result`` function.

Phase 4 of the SM v3 rollout (issue #301) flipped v3 from dry-run /
dual-run shadow to authoritative for transition decisions.
:func:`alice_forge.sm.apply.apply_result` is the single point where
a :class:`HandlerResult` is turned into a real GitHub side-effect
plus a ledger record. These tests confirm each variant applies the
expected calls and returns the right transition-or-not signal so
the dispatcher knows whether to skip the legacy v1 handler.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from alice_forge.sm.apply import apply_result
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    EmitParseError,
    NoProgress,
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
    posted: list[tuple[str, int, str]] | None = None,
    labels_edits: list[tuple[str, int, dict[str, Any]]] | None = None,
) -> tuple[HandlerServices, list[tuple[str, int, str]], list[tuple[str, int, dict[str, Any]]]]:
    """Build a HandlerServices that records every write call.

    Returns ``(services, posted_comments, label_edits)`` so the caller
    can assert against the recorded calls. Both record lists are
    fresh empty lists if not provided.
    """
    posted = posted if posted is not None else []
    labels_edits = labels_edits if labels_edits is not None else []
    comments_list = list(comments or [])

    def _post(repo: str, number: int, body: str) -> None:
        posted.append((repo, number, body))

    def _edit_labels(repo: str, number: int, **kwargs: Any) -> None:
        labels_edits.append((repo, number, dict(kwargs)))

    services = HandlerServices(
        ledger=ledger or EmittedLedger(),
        repo="jcronq/alice",
        post_comment=_post,
        list_comments=lambda repo, number: comments_list,
        edit_labels=_edit_labels,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda *a, **kw: None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        trusted_authors=frozenset({"jcronq", "alice"}),
        now=_now,
        log=lambda s: None,
    )
    return services, posted, labels_edits


def _issue(
    number: int = 42, art_label: str | None = None
) -> dict[str, Any]:
    labels: list[dict[str, Any]] = [{"name": "sm:draft"}]
    if art_label is not None:
        labels.append({"name": art_label})
    return {
        "number": number,
        "title": "Test issue",
        "url": f"https://github.com/jcronq/alice/issues/{number}",
        "labels": labels,
    }


class TestApplyTransition:
    def test_basic_transition_emits_label_edit_and_audit(self):
        services, posted, label_edits = _services()
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=Transition(
                target=SMState.NEEDS_STUDY, reason="route-to-study"
            ),
            services=services,
        )
        assert transitioned is True
        assert len(label_edits) == 1
        repo, number, kwargs = label_edits[0]
        assert repo == "jcronq/alice"
        assert number == 42
        assert "sm:needs_study" in kwargs["add"]
        assert "sm:draft" in kwargs["remove"]
        # Audit comment shape: matches the spec example from the v3
        # design doc — ``[SM] transition from=... to=... reason=...``.
        assert len(posted) == 1
        body = posted[0][2]
        assert body.startswith("[SM] transition")
        assert "from=sm:draft" in body
        assert "to=sm:needs_study" in body
        assert "route-to-study" in body

    def test_art_swap_added_when_missing(self):
        services, _, label_edits = _services()
        apply_result(
            issue=_issue(),  # no art:* label currently set
            current_state=SMState.DRAFT,
            result=Transition(
                target=SMState.NEEDS_STUDY,
                reason="route-to-study",
                art_swap="art:code",
            ),
            services=services,
        )
        _, _, kwargs = label_edits[0]
        assert "art:code" in kwargs["add"]
        assert "art:code" not in kwargs["remove"]

    def test_art_swap_replaces_existing(self):
        services, _, label_edits = _services()
        apply_result(
            issue=_issue(art_label="art:code"),
            current_state=SMState.DRAFT,
            result=Transition(
                target=SMState.NEEDS_STUDY,
                reason="route-to-study",
                art_swap="art:research_note",
            ),
            services=services,
        )
        _, _, kwargs = label_edits[0]
        assert "art:research_note" in kwargs["add"]
        assert "art:code" in kwargs["remove"]

    def test_art_swap_noop_when_already_correct(self):
        services, _, label_edits = _services()
        apply_result(
            issue=_issue(art_label="art:code"),
            current_state=SMState.DRAFT,
            result=Transition(
                target=SMState.NEEDS_STUDY,
                reason="route-to-study",
                art_swap="art:code",
            ),
            services=services,
        )
        _, _, kwargs = label_edits[0]
        # art:code is already on the issue — no add, no remove for it.
        assert "art:code" not in kwargs["add"]
        assert "art:code" not in kwargs["remove"]

    def test_transition_records_ledger_entry(self):
        ledger = EmittedLedger()
        services, _, _ = _services(ledger=ledger)
        apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=Transition(
                target=SMState.NEEDS_STUDY, reason="route-to-study"
            ),
            services=services,
        )
        # One transition record per move.
        rec = ledger.find(42, "transition:sm:draft->sm:needs_study")
        assert rec is not None
        assert rec.metadata.get("reason") == "route-to-study"


class TestApplyContinue:
    def test_continue_records_ledger_no_post(self):
        ledger = EmittedLedger()
        services, posted, label_edits = _services(ledger=ledger)
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=Continue(reason="still investigating", findings=None),
            services=services,
        )
        assert transitioned is False
        # Continue is the agent's own comment; the dispatcher only
        # records the audit, no extra post.
        assert posted == []
        assert label_edits == []
        rec = ledger.find(42, "continue:sm:draft")
        assert rec is not None
        assert rec.metadata.get("reason") == "still investigating"


class TestApplySideEffect:
    def test_side_effect_posts_and_records(self):
        ledger = EmittedLedger()
        services, posted, _ = _services(ledger=ledger)
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=SideEffect(
                name="triage-surface",
                body="[SM] triage-surface number=42 title='Test issue'",
                ttl_seconds=None,
                metadata={"issue_url": "https://github.com/jcronq/alice/issues/42"},
            ),
            services=services,
        )
        assert transitioned is False
        assert len(posted) == 1
        assert "triage-surface" in posted[0][2]
        rec = ledger.find(42, "triage-surface")
        assert rec is not None
        assert rec.metadata.get("issue_url") == "https://github.com/jcronq/alice/issues/42"


class TestApplyNoProgress:
    def test_no_progress_posts_polite_ping(self):
        ledger = EmittedLedger()
        services, posted, _ = _services(ledger=ledger)
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.NEEDS_STUDY,
            result=NoProgress(
                duplicate_reason="still working",
                duplicate_of_emitted_at="2026-05-21T18:00:00+00:00",
            ),
            services=services,
        )
        assert transitioned is False
        # Polite ping body mentions the no-progress wording.
        assert len(posted) == 1
        body = posted[0][2]
        assert "[SM] no-progress" in body
        rec = ledger.find(42, "no-progress:sm:needs_study")
        assert rec is not None


class TestApplyBlockedByTTL:
    def test_blocked_by_ttl_transitions_to_blocked(self):
        ledger = EmittedLedger()
        services, posted, label_edits = _services(ledger=ledger)
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.NEEDS_STUDY,
            result=BlockedByTTL(state_ttl_seconds=86400),
            services=services,
        )
        assert transitioned is True
        # Routed through the transition path — label edit + audit
        # comment land on the issue.
        assert len(label_edits) == 1
        _, _, kwargs = label_edits[0]
        assert "sm:blocked" in kwargs["add"]
        assert "sm:needs_study" in kwargs["remove"]
        assert len(posted) == 1
        assert "TTL elapsed" in posted[0][2]
        rec = ledger.find(42, "transition:sm:needs_study->sm:blocked")
        assert rec is not None


class TestApplyParseError:
    def test_parse_error_posts_loud_reply(self):
        ledger = EmittedLedger()
        services, posted, _ = _services(ledger=ledger)
        transitioned = apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=EmitParseError(
                verb="route-to-study",
                reason="unknown field",
                reply_body=(
                    "[SM] parse-error reason=\"unknown field\"\n"
                    "Original body: ..."
                ),
            ),
            services=services,
        )
        assert transitioned is False
        assert len(posted) == 1
        assert "[SM] parse-error" in posted[0][2]
        rec = ledger.find(42, "parse-error-reply")
        assert rec is not None
        assert rec.ttl_seconds is not None
        # 1-hour TTL per design I-6.
        assert rec.ttl_seconds == 60 * 60

    def test_parse_error_dedup_within_ttl(self):
        ledger = EmittedLedger()
        services, posted, _ = _services(ledger=ledger)

        # First parse error lands.
        apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=EmitParseError(
                verb="route-to-study",
                reason="unknown field",
                reply_body="[SM] parse-error reason=\"unknown field\"",
            ),
            services=services,
        )
        assert len(posted) == 1

        # Second parse error within the TTL: dispatcher must not
        # re-post, to avoid spamming the issue thread.
        apply_result(
            issue=_issue(),
            current_state=SMState.DRAFT,
            result=EmitParseError(
                verb="route-to-study",
                reason="unknown field",
                reply_body="[SM] parse-error reason=\"unknown field\"",
            ),
            services=services,
        )
        assert len(posted) == 1
