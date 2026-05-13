"""Tests for :mod:`alice_sm.comments` — the ``[SM]`` comment-shape parsers.

Each parser gets a happy-path test and a malformed-comment matrix
(missing fields, malformed wikilink, unknown verdict, untrusted author,
trailing junk). Verdict resolution is exercised via the
:func:`alice_sm.comments.parse_comment` dispatch wrapper.

Malformed comments must return ``None`` AND log a defensive warning; the
log assertions use an injectable ``log`` callable so the test doesn't
have to capture stderr.
"""

from __future__ import annotations

import pytest

from alice_sm import comments as cm
from alice_sm.dispatcher import ART_LABEL_WHITELIST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)

    def joined(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def log() -> LogCapture:
    return LogCapture()


# ---------------------------------------------------------------------------
# design-ready
# ---------------------------------------------------------------------------


def test_design_ready_happy(log: LogCapture) -> None:
    body = "[SM] design-ready note=[[2026-05-13-design-thing]] author=alice"
    out = cm.parse_design_ready(body, "jcronq", log=log)
    assert out == cm.DesignReady(note="2026-05-13-design-thing", author="alice")
    assert log.lines == []


def test_design_ready_order_independent(log: LogCapture) -> None:
    body = "[SM] design-ready author=alice note=[[note-x]]"
    out = cm.parse_design_ready(body, "jcronq", log=log)
    assert out == cm.DesignReady(note="note-x", author="alice")


def test_design_ready_missing_note(log: LogCapture) -> None:
    body = "[SM] design-ready author=alice"
    assert cm.parse_design_ready(body, "jcronq", log=log) is None
    assert "missing required fields" in log.joined()


def test_design_ready_missing_author(log: LogCapture) -> None:
    body = "[SM] design-ready note=[[x]]"
    assert cm.parse_design_ready(body, "jcronq", log=log) is None
    assert "missing required fields" in log.joined()


def test_design_ready_bad_wikilink(log: LogCapture) -> None:
    body = "[SM] design-ready note=plain-text author=alice"
    assert cm.parse_design_ready(body, "jcronq", log=log) is None
    assert "not a wikilink" in log.joined()


def test_design_ready_wrong_prefix(log: LogCapture) -> None:
    # Different verb — must not match.
    body = "[SM] design-approved note=[[x]] author=alice"
    assert cm.parse_design_ready(body, "jcronq", log=log) is None


def test_design_ready_untrusted_author(log: LogCapture) -> None:
    body = "[SM] design-ready note=[[x]] author=alice"
    assert cm.parse_design_ready(body, "random-drive-by", log=log) is None
    assert "untrusted" in log.joined()


def test_design_ready_no_author(log: LogCapture) -> None:
    body = "[SM] design-ready note=[[x]] author=alice"
    assert cm.parse_design_ready(body, None, log=log) is None
    assert "untrusted" in log.joined()


# ---------------------------------------------------------------------------
# design-approved
# ---------------------------------------------------------------------------


def test_design_approved_happy(log: LogCapture) -> None:
    body = "[SM] design-approved"
    out = cm.parse_design_approved(body, "jcronq", log=log)
    assert out == cm.DesignApproved()


def test_design_approved_trailing_junk(log: LogCapture) -> None:
    body = "[SM] design-approved with extra prose"
    assert cm.parse_design_approved(body, "jcronq", log=log) is None
    assert "trailing content" in log.joined()


def test_design_approved_untrusted(log: LogCapture) -> None:
    assert cm.parse_design_approved("[SM] design-approved", "mallory", log=log) is None
    assert "untrusted" in log.joined()


def test_design_approved_must_not_match_substring_verb(log: LogCapture) -> None:
    # `design-approved-extra` must not match `design-approved`.
    assert (
        cm.parse_design_approved("[SM] design-approved-extra", "jcronq", log=log)
        is None
    )


# ---------------------------------------------------------------------------
# design-revise
# ---------------------------------------------------------------------------


def test_design_revise_happy(log: LogCapture) -> None:
    body = '[SM] design-revise reason="needs clearer state diagram" feedback=[[fb-1]]'
    out = cm.parse_design_revise(body, "jcronq", log=log)
    assert out == cm.DesignRevise(reason="needs clearer state diagram", feedback="fb-1")


def test_design_revise_bareword_reason(log: LogCapture) -> None:
    body = "[SM] design-revise reason=stub feedback=[[fb]]"
    out = cm.parse_design_revise(body, "jcronq", log=log)
    assert out == cm.DesignRevise(reason="stub", feedback="fb")


def test_design_revise_missing_feedback(log: LogCapture) -> None:
    body = '[SM] design-revise reason="x"'
    assert cm.parse_design_revise(body, "jcronq", log=log) is None
    assert "missing required fields" in log.joined()


def test_design_revise_bad_wikilink(log: LogCapture) -> None:
    body = '[SM] design-revise reason="x" feedback=raw'
    assert cm.parse_design_revise(body, "jcronq", log=log) is None
    assert "not a wikilink" in log.joined()


# ---------------------------------------------------------------------------
# design-rejected
# ---------------------------------------------------------------------------


def test_design_rejected_happy(log: LogCapture) -> None:
    body = '[SM] design-rejected reason="scope creep"'
    out = cm.parse_design_rejected(body, "jcronq", log=log)
    assert out == cm.DesignRejected(reason="scope creep")


def test_design_rejected_missing_reason(log: LogCapture) -> None:
    body = "[SM] design-rejected"
    assert cm.parse_design_rejected(body, "jcronq", log=log) is None
    assert "missing reason" in log.joined()


# ---------------------------------------------------------------------------
# code-review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", sorted(cm.CODE_REVIEW_VERDICTS))
def test_code_review_verdict_resolution(verdict: str, log: LogCapture) -> None:
    body = f"[SM] code-review verdict={verdict} findings=[[review-1]]"
    out = cm.parse_code_review(body, "jcronq", log=log)
    assert out == cm.CodeReview(verdict=verdict, findings="review-1")


def test_code_review_unknown_verdict(log: LogCapture) -> None:
    body = "[SM] code-review verdict=lgtm findings=[[r]]"
    assert cm.parse_code_review(body, "jcronq", log=log) is None
    assert "unknown verdict" in log.joined()


def test_code_review_missing_findings(log: LogCapture) -> None:
    body = "[SM] code-review verdict=approved"
    assert cm.parse_code_review(body, "jcronq", log=log) is None
    assert "missing required fields" in log.joined()


def test_code_review_findings_not_wikilink(log: LogCapture) -> None:
    body = "[SM] code-review verdict=approved findings=plain"
    assert cm.parse_code_review(body, "jcronq", log=log) is None
    assert "not a wikilink" in log.joined()


def test_code_review_does_not_match_override(log: LogCapture) -> None:
    # The override verb is a different shape; the prefix-guard must
    # refuse to consume it via the code-review parser.
    body = '[SM] code-review-override reason="manual approval"'
    assert cm.parse_code_review(body, "jcronq", log=log) is None


# ---------------------------------------------------------------------------
# code-review-override
# ---------------------------------------------------------------------------


def test_code_review_override_happy(log: LogCapture) -> None:
    body = '[SM] code-review-override reason="manual approval — emergency hotfix"'
    out = cm.parse_code_review_override(body, "jcronq", log=log)
    assert out == cm.CodeReviewOverride(reason="manual approval — emergency hotfix")


def test_code_review_override_missing_reason(log: LogCapture) -> None:
    body = "[SM] code-review-override"
    assert cm.parse_code_review_override(body, "jcronq", log=log) is None
    assert "missing reason" in log.joined()


# ---------------------------------------------------------------------------
# study-complete
# ---------------------------------------------------------------------------


def test_study_complete_happy(log: LogCapture) -> None:
    body = "[SM] study-complete art=art:code findings=[[notes/study-1]]"
    out = cm.parse_study_complete(body, "jcronq", log=log)
    assert out == cm.StudyComplete(art_label="art:code", findings="notes/study-1")


def test_study_complete_allows_art_swap(log: LogCapture) -> None:
    # The art label may differ from what's on the issue today — the
    # whole point is to let thinking swap it on exit.
    assert "art:research_note" in ART_LABEL_WHITELIST
    body = "[SM] study-complete art=art:research_note findings=[[x]]"
    out = cm.parse_study_complete(body, "jcronq", log=log)
    assert out is not None
    assert out.art_label == "art:research_note"


def test_study_complete_unknown_art(log: LogCapture) -> None:
    body = "[SM] study-complete art=art:imaginary findings=[[x]]"
    assert cm.parse_study_complete(body, "jcronq", log=log) is None
    assert "not in whitelist" in log.joined()


def test_study_complete_missing_findings(log: LogCapture) -> None:
    body = "[SM] study-complete art=art:code"
    assert cm.parse_study_complete(body, "jcronq", log=log) is None
    assert "missing required fields" in log.joined()


# ---------------------------------------------------------------------------
# study-blocked / study-progress / study-rejected
# ---------------------------------------------------------------------------


def test_study_blocked_happy(log: LogCapture) -> None:
    body = '[SM] study-blocked reason="external API offline"'
    out = cm.parse_study_blocked(body, "jcronq", log=log)
    assert out == cm.StudyBlocked(reason="external API offline")


def test_study_blocked_missing_reason(log: LogCapture) -> None:
    assert cm.parse_study_blocked("[SM] study-blocked", "jcronq", log=log) is None
    assert "missing reason" in log.joined()


def test_study_progress_happy(log: LogCapture) -> None:
    body = "[SM] study-progress note=[[inner/checkpoint-2]]"
    out = cm.parse_study_progress(body, "jcronq", log=log)
    assert out == cm.StudyProgress(note="inner/checkpoint-2")


def test_study_progress_bad_wikilink(log: LogCapture) -> None:
    body = "[SM] study-progress note=raw"
    assert cm.parse_study_progress(body, "jcronq", log=log) is None
    assert "not a wikilink" in log.joined()


def test_study_progress_missing_note(log: LogCapture) -> None:
    assert cm.parse_study_progress("[SM] study-progress", "jcronq", log=log) is None
    assert "missing note" in log.joined()


def test_study_rejected_happy(log: LogCapture) -> None:
    body = '[SM] study-rejected reason="wrong shape — refile"'
    out = cm.parse_study_rejected(body, "jcronq", log=log)
    assert out == cm.StudyRejected(reason="wrong shape — refile")


# ---------------------------------------------------------------------------
# route-to-study / return-to-study
# ---------------------------------------------------------------------------


def test_route_to_study_happy_bare(log: LogCapture) -> None:
    """Bare form — issue keeps its current art:* label across the transition."""
    assert (
        cm.parse_route_to_study("[SM] route-to-study", "jcronq", log=log)
        == cm.RouteToStudy(art_label=None)
    )


def test_route_to_study_with_art_swap(log: LogCapture) -> None:
    body = "[SM] route-to-study art=art:research_note"
    out = cm.parse_route_to_study(body, "jcronq", log=log)
    assert out == cm.RouteToStudy(art_label="art:research_note")


def test_route_to_study_art_must_be_whitelisted(log: LogCapture) -> None:
    body = "[SM] route-to-study art=art:imaginary"
    assert cm.parse_route_to_study(body, "jcronq", log=log) is None
    assert "not in whitelist" in log.joined()


def test_route_to_study_trailing_junk_rejected(log: LogCapture) -> None:
    body = "[SM] route-to-study oh-hi"
    assert cm.parse_route_to_study(body, "jcronq", log=log) is None
    assert "unexpected trailing content" in log.joined()


def test_route_to_study_unknown_field_rejected(log: LogCapture) -> None:
    body = "[SM] route-to-study reason=foo"
    assert cm.parse_route_to_study(body, "jcronq", log=log) is None
    assert "unexpected trailing content" in log.joined()


def test_route_to_study_untrusted(log: LogCapture) -> None:
    assert (
        cm.parse_route_to_study("[SM] route-to-study", "drive-by", log=log) is None
    )
    assert "untrusted" in log.joined()


def test_return_to_study_happy(log: LogCapture) -> None:
    body = '[SM] return-to-study reason="need design clarification"'
    out = cm.parse_return_to_study(body, "jcronq", log=log)
    assert out == cm.ReturnToStudy(reason="need design clarification")


def test_return_to_study_bareword_reason(log: LogCapture) -> None:
    body = "[SM] return-to-study reason=stub"
    out = cm.parse_return_to_study(body, "jcronq", log=log)
    assert out == cm.ReturnToStudy(reason="stub")


def test_return_to_study_missing_reason(log: LogCapture) -> None:
    assert (
        cm.parse_return_to_study("[SM] return-to-study", "jcronq", log=log) is None
    )
    assert "missing reason" in log.joined()


def test_return_to_study_untrusted(log: LogCapture) -> None:
    body = '[SM] return-to-study reason="x"'
    assert cm.parse_return_to_study(body, "drive-by", log=log) is None
    assert "untrusted" in log.joined()


# ---------------------------------------------------------------------------
# parse_comment dispatch
# ---------------------------------------------------------------------------


def test_parse_comment_dispatch_design_ready(log: LogCapture) -> None:
    body = "[SM] design-ready note=[[x]] author=alice"
    out = cm.parse_comment(body, "jcronq", log=log)
    assert isinstance(out, cm.DesignReady)


def test_parse_comment_dispatch_code_review_vs_override(log: LogCapture) -> None:
    """``code-review-override`` must dispatch to the override parser, not
    the ``code-review`` one — order in ``_PARSERS`` ensures the longer
    prefix wins."""
    out = cm.parse_comment(
        '[SM] code-review-override reason="hotfix"',
        "jcronq",
        log=log,
    )
    assert isinstance(out, cm.CodeReviewOverride)
    assert out.reason == "hotfix"


def test_parse_comment_dispatch_unknown_verb(log: LogCapture) -> None:
    # ``[SM] foo`` is an unrecognized verb. Returns None silently — we
    # only warn when a verb prefix matches but the body fails to
    # validate, not when a brand-new verb shows up that we don't yet
    # parse (forward-compat).
    assert cm.parse_comment("[SM] foo bar baz", "jcronq", log=log) is None
    assert log.lines == []


def test_parse_comment_non_sm_comment(log: LogCapture) -> None:
    # Plain human prose — most issue comments. Silent None.
    assert cm.parse_comment("looks good to me", "jcronq", log=log) is None
    assert log.lines == []


def test_parse_comment_dispatcher_audit_comment_not_parsed(log: LogCapture) -> None:
    # The dispatcher's own ``[SM] dispatcher-hello`` / ``[SM] transition``
    # comments are emitted by this module's host; they aren't consumed
    # by the design/study parsers and must return None silently.
    body = "[SM] dispatcher-hello task=#42 state=sm:selected art=art:code ts=x v=0"
    assert cm.parse_comment(body, "jcronq", log=log) is None
    assert log.lines == []


def test_parse_comment_malformed_known_verb_logs(log: LogCapture) -> None:
    # Verb matches but body is malformed — defensive warning expected.
    body = "[SM] design-ready missing-fields"
    out = cm.parse_comment(body, "jcronq", log=log)
    assert out is None
    assert log.lines, "malformed known-verb comment must produce a warning"


def test_parse_comment_untrusted_logs(log: LogCapture) -> None:
    body = "[SM] design-approved"
    assert cm.parse_comment(body, "drive-by", log=log) is None
    assert "untrusted" in log.joined()


# ---------------------------------------------------------------------------
# build-started (issue #164)
# ---------------------------------------------------------------------------


def test_build_started_happy(log: LogCapture) -> None:
    body = "[SM] build-started"
    out = cm.parse_build_started(body, "jcronq", log=log)
    assert out == cm.BuildStarted()
    assert log.lines == []


def test_build_started_with_trailing_audit_fields_accepted(log: LogCapture) -> None:
    # The agent may render an audit-style payload — we tolerate
    # ``task=#N ts=...`` trailing fields rather than require a bare verb.
    body = "[SM] build-started task=#42 ts=2026-05-13T12:00:00+00:00"
    out = cm.parse_build_started(body, "jcronq", log=log)
    assert out == cm.BuildStarted()


def test_build_started_untrusted_returns_none(log: LogCapture) -> None:
    body = "[SM] build-started"
    assert cm.parse_build_started(body, "drive-by", log=log) is None
    assert "untrusted" in log.joined()


def test_build_started_dispatch_resolves(log: LogCapture) -> None:
    body = "[SM] build-started"
    assert cm.parse_comment(body, "jcronq", log=log) == cm.BuildStarted()


def test_build_started_does_not_match_other_verbs(log: LogCapture) -> None:
    # Verb-prefix guard: ``build-started-something`` must NOT trip the
    # ``build-started`` parser.
    body = "[SM] build-started-other"
    assert cm.parse_build_started(body, "jcronq", log=log) is None


# ---------------------------------------------------------------------------
# exit-transition (issue #174)
# ---------------------------------------------------------------------------


def test_exit_transition_wild_form_disseminate(log: LogCapture) -> None:
    # ``exit-transition=<value>`` is the form used in the wild on the
    # retroactive #136/#148/#149/#170 closes.
    body = "[SM] exit-transition=disseminate"
    out = cm.parse_exit_transition(body, "jcronq", log=log)
    assert out == cm.ExitTransition(value="disseminate", findings=None, spawned=None)
    assert log.lines == []


def test_exit_transition_wild_form_with_findings_and_spawned(log: LogCapture) -> None:
    body = (
        "[SM] exit-transition=both findings=[[2026-05-12-eks-phase-2]] "
        "spawned=spawned #152 #155"
    )
    out = cm.parse_exit_transition(body, "jcronq", log=log)
    assert out is not None
    assert out.value == "both"
    assert out.findings == "2026-05-12-eks-phase-2"
    # The ``spawned=`` bareword + trailing-token recovery preserves the
    # full ``#N #N`` list rather than just the first token.
    assert out.spawned == "spawned #152 #155"


def test_exit_transition_canonical_bareword_form(log: LogCapture) -> None:
    body = "[SM] exit-transition spawn-code findings=[[my-note]]"
    out = cm.parse_exit_transition(body, "jcronq", log=log)
    assert out == cm.ExitTransition(
        value="spawn-code", findings="my-note", spawned=None
    )


def test_exit_transition_unknown_value_rejected(log: LogCapture) -> None:
    body = "[SM] exit-transition=archive"
    assert cm.parse_exit_transition(body, "jcronq", log=log) is None
    assert "unknown value" in log.joined()


def test_exit_transition_missing_value(log: LogCapture) -> None:
    body = "[SM] exit-transition"
    assert cm.parse_exit_transition(body, "jcronq", log=log) is None


def test_exit_transition_untrusted_returns_none(log: LogCapture) -> None:
    body = "[SM] exit-transition=both"
    assert cm.parse_exit_transition(body, "drive-by", log=log) is None
    assert "untrusted" in log.joined()


def test_exit_transition_bad_findings_wikilink(log: LogCapture) -> None:
    body = "[SM] exit-transition=both findings=plain-text"
    assert cm.parse_exit_transition(body, "jcronq", log=log) is None
    assert "not a wikilink" in log.joined()


def test_exit_transition_does_not_match_required_reminder(log: LogCapture) -> None:
    # ``exit-transition-required`` is the dispatcher's reminder prefix.
    # The worker-emitted parser must NOT match it (otherwise the
    # reminder itself would be misread as a worker exit-transition and
    # we'd close the issue based on our own nag).
    body = (
        '[SM] exit-transition-required task=#136 '
        'expected=one-of="disseminate|spawn-code|both" ts=2026-05-13T12:00:00+00:00'
    )
    assert cm.parse_exit_transition(body, "jcronq", log=log) is None
    # Same body via dispatch wrapper must also stay None.
    assert cm.parse_comment(body, "jcronq", log=log) is None


def test_exit_transition_dispatch_wild_form(log: LogCapture) -> None:
    # The dispatch wrapper must route ``exit-transition=both`` to the
    # parser despite the ``=`` (rather than space) between verb and
    # value — both shapes appear in the wild.
    body = "[SM] exit-transition=both findings=[[note-x]]"
    out = cm.parse_comment(body, "jcronq", log=log)
    assert isinstance(out, cm.ExitTransition)
    assert out.value == "both"
    assert out.findings == "note-x"


def test_exit_transition_dispatch_bareword_form(log: LogCapture) -> None:
    body = "[SM] exit-transition disseminate"
    out = cm.parse_comment(body, "jcronq", log=log)
    assert out == cm.ExitTransition(
        value="disseminate", findings=None, spawned=None
    )


# ---------------------------------------------------------------------------
# Issue #176 — dependency parser (Depends on #N / Blocked by #N / etc.)
# ---------------------------------------------------------------------------


def test_parse_dependencies_empty_text() -> None:
    assert cm.parse_dependencies("") == cm.IssueDependencies(hard=(), soft=())
    assert cm.parse_dependencies(None) == cm.IssueDependencies(hard=(), soft=())


def test_parse_dependencies_depends_on() -> None:
    out = cm.parse_dependencies("Depends on #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_case_insensitive() -> None:
    out = cm.parse_dependencies("DEPENDS ON #5\ndepends on #6\nDepends On #7")
    assert out == cm.IssueDependencies(hard=(5, 6, 7), soft=())


def test_parse_dependencies_blocked_by_synonym() -> None:
    out = cm.parse_dependencies("Blocked by #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_requires_synonym() -> None:
    out = cm.parse_dependencies("Requires #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_waits_for_synonym() -> None:
    out = cm.parse_dependencies("Waits for #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_comma_separated() -> None:
    out = cm.parse_dependencies("Depends on #5, #6, #7")
    assert out == cm.IssueDependencies(hard=(5, 6, 7), soft=())


def test_parse_dependencies_mixed_separators() -> None:
    out = cm.parse_dependencies("Depends on #5, #6 and #7")
    assert out == cm.IssueDependencies(hard=(5, 6, 7), soft=())


def test_parse_dependencies_soft_depends_on() -> None:
    out = cm.parse_dependencies("Soft depends on #5\nPrefers #6")
    assert out == cm.IssueDependencies(hard=(), soft=(5, 6))


def test_parse_dependencies_soft_does_not_eat_hard() -> None:
    # "Soft depends on" must match the longer alternation, NOT the
    # "depends on" branch starting partway into the line.
    out = cm.parse_dependencies("Soft depends on #5")
    assert out == cm.IssueDependencies(hard=(), soft=(5,))


def test_parse_dependencies_hard_overrides_soft_for_same_ref() -> None:
    # A dep that's both soft and hard is treated as hard. Order in body
    # shouldn't matter — hard wins regardless of which line came first.
    soft_first = cm.parse_dependencies("Soft depends on #5\nDepends on #5")
    assert soft_first == cm.IssueDependencies(hard=(5,), soft=())
    hard_first = cm.parse_dependencies("Depends on #5\nSoft depends on #5")
    assert hard_first == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_inline_mention_is_not_a_dep() -> None:
    # The verb must start the (lstripped) line. Prose mid-sentence
    # must not produce a false positive.
    text = "This thing requires #5 to land before we can ship."
    assert cm.parse_dependencies(text) == cm.IssueDependencies(hard=(), soft=())


def test_parse_dependencies_leading_whitespace_ok() -> None:
    # List-marker style ("  - Depends on #5") should still parse, since
    # we lstrip before anchoring. (We don't strip the leading '- '
    # marker though — that's prose, not whitespace.)
    out = cm.parse_dependencies("  Depends on #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_deduplicates_within_same_text() -> None:
    out = cm.parse_dependencies("Depends on #5\nDepends on #5\nBlocked by #5")
    assert out == cm.IssueDependencies(hard=(5,), soft=())


def test_parse_dependencies_preserves_order() -> None:
    out = cm.parse_dependencies(
        "Depends on #9\nBlocked by #3\nRequires #11, #1"
    )
    assert out == cm.IssueDependencies(hard=(9, 3, 11, 1), soft=())


def test_parse_dependencies_multiline_body() -> None:
    body = """\
This is a sub-issue scoped to wiring up the new routing layer.

Depends on #150
Blocked by #151, #152

Some prose explaining the design considerations.

Soft depends on #149

End of body.
"""
    assert cm.parse_dependencies(body) == cm.IssueDependencies(
        hard=(150, 151, 152), soft=(149,)
    )


def test_parse_dependencies_ignores_non_verb_lines_with_hashrefs() -> None:
    body = """\
See #5 for context.
This change relates to #6.
Depends on #7
"""
    # Only the explicit verb line counts; the bare "#5" mentions don't
    # become deps.
    assert cm.parse_dependencies(body) == cm.IssueDependencies(hard=(7,), soft=())
