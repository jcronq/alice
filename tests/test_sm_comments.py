"""Tests for ``alice_forge.sm.comments``."""

from __future__ import annotations


from alice_forge.sm.comments import (
    Continue,
    ParseError,
    ParsedVerb,
    parse_comment,
)
from alice_forge.sm.transitions import Verbs


class TestNonSMComments:
    def test_human_prose_returns_none(self):
        assert parse_comment("Looks good to me!", "jcronq") is None
        assert parse_comment("LGTM", "jcronq") is None
        assert parse_comment("", "jcronq") is None

    def test_sm_in_middle_of_text_returns_none(self):
        # Only ``[SM] `` at the start triggers parsing.
        assert (
            parse_comment("Talking about [SM] route-to-study briefly", "jcronq")
            is None
        )


class TestAuditPrefixFilter:
    """Dispatcher self-audit prefixes return None — not a parse error.

    These comments are protocol-internal announcements (spawn started,
    transition recorded, etc.) posted by the dispatcher itself. Treating
    them as transition verbs would post a fresh ``[SM] parse-error``
    reply every poll cycle. Issue #295 demonstrated the noise: 228
    ``thinking-spawn-started`` audit comments would have triggered
    228 parse-error replies without this filter.
    """

    def test_thinking_spawn_started_returns_none(self):
        body = (
            "[SM] thinking-spawn-started task=#295 artifact=art:code "
            "phase=per_issue_design runtime=claude-agent-sdk:opus "
            "spawn_id=spawn-295-1779459000 ts=2026-05-22T14:10:00+00:00"
        )
        assert parse_comment(body, "jcronq") is None

    def test_transition_audit_returns_none(self):
        body = '[SM] transition from=selected to=designing reason="..."'
        assert parse_comment(body, "jcronq") is None

    def test_design_ready_audit_returns_none(self):
        # NOTE: this is the dispatcher's *echo* of a design-ready, not
        # the agent's design-ready verb itself. The verb form (without
        # ``-audit``) still parses as Verbs.DESIGN_READY.
        body = "[SM] design-ready-audit task=#42 note=[[design-note]] author=alice"
        assert parse_comment(body, "jcronq") is None

    def test_design_ready_verb_still_parses(self):
        # Ensure the audit-prefix filter doesn't accidentally swallow
        # the actual ``[SM] design-ready`` verb (which is a real
        # transition trigger from sm:designing).
        body = "[SM] design-ready note=[[2026-05-22-design]]"
        result = parse_comment(body, "alice")
        assert isinstance(result, ParsedVerb)
        assert result.verb is Verbs.DESIGN_READY

    def test_other_audit_prefixes_return_none(self):
        for body in (
            "[SM] spawn-started task=#1",
            "[SM] speaking-spawn-started task=#1",
            "[SM] dispatcher-hello task=#1 state=sm:selected",
            "[SM] study-hint-written task=#1 path=...",
            '[SM] parse-error reason="..."',
            "[SM] rebase-needed pr=#1",
            "[SM] rebase-pushed pr=#1",
            "[SM] rebase-escalated pr=#1",
            "[SM] verify status=pass pr=#1",
            "[SM] exit-transition-required",
            "[SM] design-revisions-capped",
            "[SM] auto-study-complete",
            # Legacy patterns still on old issues.
            '[SM] blocked reason="manual operator block"',
            "[SM] speaking-build-complete pr=#1",
        ):
            assert parse_comment(body, "jcronq") is None, body


class TestSuccessfulParse:
    def test_bare_route_to_study(self):
        result = parse_comment("[SM] route-to-study", "jcronq")
        assert isinstance(result, ParsedVerb)
        assert result.verb is Verbs.ROUTE_TO_STUDY
        assert result.fields == {}

    def test_route_to_study_with_art_swap(self):
        result = parse_comment(
            "[SM] route-to-study art=art:research_note", "jcronq"
        )
        assert isinstance(result, ParsedVerb)
        assert result.verb is Verbs.ROUTE_TO_STUDY
        assert result.art_label == "art:research_note"

    def test_continue_returns_continue_subtype(self):
        result = parse_comment(
            "[SM] continue reason=\"investigating retrieval ranking\"", "alice"
        )
        assert isinstance(result, Continue)
        assert result.reason == "investigating retrieval ranking"

    def test_study_complete_with_findings_and_art(self):
        result = parse_comment(
            "[SM] study-complete art=art:research_note findings=[[some-slug]]",
            "jcronq",
        )
        assert isinstance(result, ParsedVerb)
        assert result.verb is Verbs.STUDY_COMPLETE
        assert result.art_label == "art:research_note"
        assert result.findings == "[[some-slug]]"

    def test_quoted_reason_keeps_whitespace(self):
        result = parse_comment(
            '[SM] reject reason="not in scope for this milestone"', "jcronq"
        )
        assert isinstance(result, ParsedVerb)
        assert result.reason == "not in scope for this milestone"


class TestTrailingProseRelaxation:
    """Today's #300 — v1 rejected route-to-study with trailing prose.
    v3 accepts the prose silently; key=value tokens still parsed."""

    def test_trailing_prose_does_not_block_transition(self):
        body = (
            "[SM] route-to-study\n\n"
            "Designer: audit §EC-8 already specifies the fix — "
            "check inner/surface/ before writing."
        )
        result = parse_comment(body, "jcronq")
        assert isinstance(result, ParsedVerb)
        assert result.verb is Verbs.ROUTE_TO_STUDY

    def test_trailing_prose_with_art_swap_still_parses(self):
        body = (
            "[SM] route-to-study art=art:research_note\n"
            "Note: this is a design note, not code."
        )
        result = parse_comment(body, "jcronq")
        assert isinstance(result, ParsedVerb)
        assert result.art_label == "art:research_note"


class TestParseErrors:
    def test_empty_verb_returns_parse_error(self):
        result = parse_comment("[SM] ", "jcronq")
        assert isinstance(result, ParseError)
        assert "empty" in result.reason.lower()
        assert "[SM] parse-error" in result.reply_body

    def test_unknown_verb_returns_parse_error(self):
        result = parse_comment("[SM] totally-fake-verb", "jcronq")
        assert isinstance(result, ParseError)
        assert "unknown verb" in result.reason
        assert "totally-fake-verb" in result.reason

    def test_untrusted_author_returns_parse_error(self):
        result = parse_comment(
            "[SM] route-to-study", "random-internet-person"
        )
        assert isinstance(result, ParseError)
        assert "untrusted author" in result.reason

    def test_missing_author_returns_parse_error(self):
        result = parse_comment("[SM] route-to-study", None)
        assert isinstance(result, ParseError)
        assert "untrusted author" in result.reason

    def test_parse_error_reply_body_includes_original(self):
        result = parse_comment("[SM] fake-verb args=here", "jcronq")
        assert isinstance(result, ParseError)
        assert "[SM] fake-verb args=here" in result.reply_body


class TestTrustedAuthorsOverride:
    def test_custom_trust_set(self):
        result = parse_comment(
            "[SM] route-to-study",
            "bob",
            trusted_authors=frozenset({"bob"}),
        )
        assert isinstance(result, ParsedVerb)

    def test_jcronq_not_in_custom_set_rejected(self):
        result = parse_comment(
            "[SM] route-to-study",
            "jcronq",
            trusted_authors=frozenset({"alice"}),
        )
        assert isinstance(result, ParseError)
