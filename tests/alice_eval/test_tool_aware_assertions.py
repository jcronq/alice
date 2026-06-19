"""Tests for the tool-aware correctness assertions + harness wiring.

These cover the two failure modes the legacy regex-over-prose benchmark
cannot catch:

- ``action_requires_send`` — a bare acknowledgement ("👍") sent where an
  action was actually required (no structured ``send_message`` call).
- ``no_unbacked_completion_claim`` — a hallucinated "done / logged / sent"
  claim with no tool call behind it.
"""

from __future__ import annotations

import pytest

from eval.assertions import (
    COMPLETION_CLAIM_KEYWORDS,
    AssertionFile,
    evaluate_assertion,
    evaluate_instance,
    text_claims_completion,
    tool_calls_contain_send,
)


SEND = {"name": "send_message", "id": "s1"}
MCP_SEND = {"name": "mcp__alice__send_message", "id": "s2"}
LOG = {"name": "mcp__alice__log_event", "id": "l1"}


class TestSendDetection:
    def test_bare_send_name(self):
        assert tool_calls_contain_send([SEND])

    def test_mcp_qualified_send_name(self):
        assert tool_calls_contain_send([MCP_SEND])

    def test_renamed_mcp_server_still_counts(self):
        assert tool_calls_contain_send([{"name": "mcp__other__send_message"}])

    def test_no_send(self):
        assert not tool_calls_contain_send([LOG])

    def test_empty(self):
        assert not tool_calls_contain_send([])
        assert not tool_calls_contain_send(None)

    def test_bare_string_entries(self):
        assert tool_calls_contain_send(["send_message"])


class TestCompletionClaimDetector:
    @pytest.mark.parametrize(
        "text",
        ["done!", "logged it", "I sent that", "updated the sheet", "queued."],
    )
    def test_positive(self, text):
        assert text_claims_completion(text)

    @pytest.mark.parametrize(
        "text",
        ["how are you?", "sure thing", "", "let me think about it"],
    )
    def test_negative(self, text):
        assert not text_claims_completion(text)

    def test_whole_word_only(self):
        # "doner" should not trip "done"; "outposted" should not trip "posted".
        assert not text_claims_completion("doner kebab")
        assert not text_claims_completion("the outposts")

    def test_custom_keywords(self):
        assert text_claims_completion("frobbed it", ["frobbed"])
        assert not text_claims_completion("frobbed it", COMPLETION_CLAIM_KEYWORDS)


class TestActionRequiresSend:
    def test_pass_when_send_present(self):
        r = evaluate_assertion(
            "Sent it.", {"type": "action_requires_send"}, "fail_to_pass",
            tool_calls=[SEND],
        )
        assert r.passed

    def test_pass_with_mcp_send(self):
        r = evaluate_assertion(
            "ok", {"type": "action_requires_send"}, "fail_to_pass",
            tool_calls=[MCP_SEND, LOG],
        )
        assert r.passed

    def test_fail_on_bare_ack_no_send(self):
        # The headline failure: "👍" with zero tool calls.
        r = evaluate_assertion(
            "👍", {"type": "action_requires_send"}, "fail_to_pass",
            tool_calls=[],
        )
        assert not r.passed

    def test_fail_when_only_other_tools(self):
        r = evaluate_assertion(
            "Looked it up.", {"type": "action_requires_send"}, "fail_to_pass",
            tool_calls=[LOG],
        )
        assert not r.passed

    def test_legacy_fallback_uses_prose(self):
        # No structured tool_calls → regex over prose.
        r = evaluate_assertion(
            'send_message(recipient="jason", message="hi")',
            {"type": "action_requires_send"},
            "fail_to_pass",
            tool_calls=None,
        )
        assert r.passed


class TestNoUnbackedCompletionClaim:
    def test_pass_when_no_claim(self):
        r = evaluate_assertion(
            "how can I help?", {"type": "no_unbacked_completion_claim"},
            "pass_to_pass", tool_calls=[],
        )
        assert r.passed

    def test_pass_when_claim_is_backed(self):
        r = evaluate_assertion(
            "Logged it.", {"type": "no_unbacked_completion_claim"},
            "pass_to_pass", tool_calls=[LOG],
        )
        assert r.passed

    def test_fail_on_hallucinated_claim(self):
        # The headline failure: "done, logged it" with zero tool calls.
        r = evaluate_assertion(
            "Done — logged it for you.",
            {"type": "no_unbacked_completion_claim"},
            "pass_to_pass", tool_calls=[],
        )
        assert not r.passed

    def test_custom_keywords_param(self):
        r = evaluate_assertion(
            "frobbed it",
            {"type": "no_unbacked_completion_claim", "claim_keywords": ["frobbed"]},
            "pass_to_pass", tool_calls=[],
        )
        assert not r.passed


class TestEvaluateInstanceThreadsToolCalls:
    def test_resolved_with_send(self):
        af = AssertionFile(
            turn_id="t1",
            category="tactical",
            channel="signal",
            pass_to_pass=[{"type": "no_unbacked_completion_claim"}],
            fail_to_pass=[{"type": "action_requires_send"}],
        )
        res = evaluate_instance(af, "On it.", tool_calls=[SEND])
        assert res.resolved

    def test_unresolved_bare_ack(self):
        af = AssertionFile(
            turn_id="t2",
            category="tactical",
            channel="signal",
            pass_to_pass=[{"type": "no_unbacked_completion_claim"}],
            fail_to_pass=[{"type": "action_requires_send"}],
        )
        res = evaluate_instance(af, "👍", tool_calls=[])
        assert not res.resolved
