"""Tests for the speaking-benchmark assertion runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice_eval import assertions
from alice_eval.assertions import (
    AssertionFile,
    bleu4,
    evaluate_assertion,
    evaluate_instance,
    extract_arg_value,
    extract_tool_names,
    jaccard,
    load_assertion_file,
    normalised_levenshtein,
)


class TestExtractors:
    def test_extract_tool_names_python_call(self):
        out = 'send_message(recipient="jason", message="hello")'
        assert "send_message" in extract_tool_names(out)

    def test_extract_tool_names_mcp_token(self):
        out = "I'll use mcp__alice__send_message for this."
        names = extract_tool_names(out)
        assert any(n.startswith("mcp__") for n in names)

    def test_extract_tool_names_cli_token(self):
        out = "$ signal-cli -a +1555 send -m 'hi'"
        names = extract_tool_names(out)
        assert "signal-cli" in names

    def test_extract_arg_value_double_quoted(self):
        out = 'send_message(recipient="jason", message="hello world")'
        assert extract_arg_value(out, "send_message", "recipient") == "jason"
        assert (
            extract_arg_value(out, "send_message", "message") == "hello world"
        )

    def test_extract_arg_value_single_quoted(self):
        out = "send_message(recipient='katie', message='ok')"
        assert extract_arg_value(out, "send_message", "recipient") == "katie"

    def test_extract_arg_value_missing(self):
        out = "send_message(recipient='jason')"
        assert extract_arg_value(out, "send_message", "message") is None

    def test_extract_arg_value_no_call(self):
        assert extract_arg_value("nope", "send_message", "x") is None


class TestSimilarity:
    def test_jaccard_identical(self):
        assert jaccard("hello world", "hello world") == pytest.approx(1.0)

    def test_jaccard_disjoint(self):
        assert jaccard("foo bar", "baz qux") == 0.0

    def test_jaccard_partial(self):
        # "hello world" vs "hello there" → {hello,world} ∩ {hello,there} = 1; union = 3
        assert jaccard("hello world", "hello there") == pytest.approx(1 / 3)

    def test_levenshtein_identical(self):
        assert normalised_levenshtein("abc", "abc") == 0.0

    def test_levenshtein_one_sub(self):
        assert normalised_levenshtein("abc", "abd") == pytest.approx(1 / 3)

    def test_bleu_identical(self):
        # Should be 1.0 (or very close) on identical text.
        score = bleu4("the quick brown fox", "the quick brown fox")
        assert score >= 0.99

    def test_bleu_disjoint(self):
        score = bleu4("apples oranges bananas", "trucks cars planes")
        assert score <= 0.05

    def test_bleu_partial(self):
        score = bleu4(
            "the quick brown fox jumps over the lazy dog",
            "the quick brown fox vaulted the lazy dog",
        )
        assert 0.15 < score < 0.95


class TestAssertionTypes:
    def test_no_forbidden_tool_pass(self):
        result = evaluate_assertion(
            "send_message(recipient='jason', message='ok')",
            {"type": "no_forbidden_tool", "tool": "signal-cli"},
            bucket="pass_to_pass",
        )
        assert result.passed

    def test_no_forbidden_tool_fail(self):
        result = evaluate_assertion(
            "$ signal-cli -a +1 send -m 'x'",
            {"type": "no_forbidden_tool", "tool": "signal-cli"},
            bucket="pass_to_pass",
        )
        assert not result.passed

    def test_no_hallucinated_tool_pass(self):
        result = evaluate_assertion(
            "Used send_message(recipient='jason')",
            {
                "type": "no_hallucinated_tool",
                "allowed_tools": ["send_message"],
            },
            bucket="pass_to_pass",
        )
        assert result.passed

    def test_no_hallucinated_tool_fail(self):
        result = evaluate_assertion(
            "I called fake_made_up_tool(foo=1)",
            {
                "type": "no_hallucinated_tool",
                "allowed_tools": ["send_message"],
            },
            bucket="pass_to_pass",
        )
        assert not result.passed

    def test_channel_format_ok_signal_plain_passes(self):
        result = evaluate_assertion(
            "Plain text reply, no markdown",
            {"type": "channel_format_ok", "channel": "signal"},
            bucket="pass_to_pass",
        )
        assert result.passed

    def test_channel_format_ok_signal_markdown_fails(self):
        result = evaluate_assertion(
            "# Heading\n\nbody",
            {"type": "channel_format_ok", "channel": "signal"},
            bucket="pass_to_pass",
        )
        assert not result.passed

    def test_channel_format_ok_signal_fences_fail(self):
        result = evaluate_assertion(
            "Here:\n```bash\nls\n```",
            {"type": "channel_format_ok", "channel": "signal"},
            bucket="pass_to_pass",
        )
        assert not result.passed

    def test_channel_format_ok_cli_allows_markdown(self):
        result = evaluate_assertion(
            "# OK\n```sh\nls\n```",
            {"type": "channel_format_ok", "channel": "cli"},
            bucket="pass_to_pass",
        )
        assert result.passed

    def test_no_empty_reply(self):
        assert evaluate_assertion(
            "ok", {"type": "no_empty_reply"}, bucket="pass_to_pass"
        ).passed
        assert not evaluate_assertion(
            "   \n  ", {"type": "no_empty_reply"}, bucket="pass_to_pass"
        ).passed

    def test_tool_call_match_set_pass(self):
        out = "send_message(recipient='jason', message='ok')"
        result = evaluate_assertion(
            out,
            {"type": "tool_call_match", "expected_tools": ["send_message"]},
            bucket="fail_to_pass",
        )
        assert result.passed

    def test_tool_call_match_set_fail_missing(self):
        out = "nothing tool-like here"
        result = evaluate_assertion(
            out,
            {"type": "tool_call_match", "expected_tools": ["send_message"]},
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_arg_match_exact(self):
        out = "send_message(recipient='jason', message='hi')"
        ok = evaluate_assertion(
            out,
            {
                "type": "arg_match",
                "tool": "send_message",
                "arg": "recipient",
                "value": "jason",
                "strategy": "exact",
            },
            bucket="fail_to_pass",
        )
        assert ok.passed

    def test_arg_match_jaccard(self):
        out = (
            'send_message(recipient="jason", '
            'message="the deploy is done, all green")'
        )
        result = evaluate_assertion(
            out,
            {
                "type": "arg_match",
                "tool": "send_message",
                "arg": "message",
                "value": "deploy done all green",
                "strategy": "jaccard",
                "threshold": 0.3,
            },
            bucket="fail_to_pass",
        )
        assert result.passed

    def test_arg_match_levenshtein_fail(self):
        out = 'send_message(recipient="jasoooon")'
        result = evaluate_assertion(
            out,
            {
                "type": "arg_match",
                "tool": "send_message",
                "arg": "recipient",
                "value": "jason",
                "strategy": "levenshtein",
                "threshold": 0.1,
            },
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_bleu_threshold_above(self):
        result = evaluate_assertion(
            "the quick brown fox jumps over the lazy dog",
            {
                "type": "bleu_threshold",
                "reference": "the quick brown fox jumps over the lazy dog",
                "min_bleu": 0.15,
            },
            bucket="fail_to_pass",
        )
        assert result.passed

    def test_bleu_threshold_below(self):
        result = evaluate_assertion(
            "completely off-topic gibberish text here",
            {
                "type": "bleu_threshold",
                "reference": "the quick brown fox jumps over the lazy dog",
                "min_bleu": 0.15,
            },
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_entity_overlap_pass(self):
        result = evaluate_assertion(
            "I see Katie and the porch, plus a sunset.",
            {
                "type": "entity_overlap",
                "entities": ["katie", "porch", "sunset"],
                "min_overlap": 0.8,
            },
            bucket="fail_to_pass",
        )
        assert result.passed

    def test_entity_overlap_fail(self):
        result = evaluate_assertion(
            "Just a cat.",
            {
                "type": "entity_overlap",
                "entities": ["katie", "porch", "sunset"],
                "min_overlap": 0.8,
            },
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_routing_decision_dispatch(self):
        result = evaluate_assertion(
            "Agent(prompt='...') — worker dispatched",
            {"type": "routing_decision", "expected": "dispatch"},
            bucket="fail_to_pass",
        )
        assert result.passed

    def test_routing_decision_inline_when_dispatch_expected(self):
        result = evaluate_assertion(
            "Just a plain reply.",
            {"type": "routing_decision", "expected": "dispatch"},
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_skill_invocation_numeric_tolerance(self):
        out = "log-meal: chicken bowl, calories: 245, protein: 32g"
        result = evaluate_assertion(
            out,
            {
                "type": "skill_invocation",
                "skill": "log-meal",
                "required_fields": {"calories": 250},
            },
            bucket="fail_to_pass",
        )
        assert result.passed, result.detail

    def test_skill_invocation_numeric_too_far(self):
        out = "log-meal: chicken bowl, calories: 800"
        result = evaluate_assertion(
            out,
            {
                "type": "skill_invocation",
                "skill": "log-meal",
                "required_fields": {"calories": 250},
            },
            bucket="fail_to_pass",
        )
        assert not result.passed

    def test_unknown_assertion_type_fails(self):
        result = evaluate_assertion(
            "x", {"type": "wat"}, bucket="fail_to_pass"
        )
        assert not result.passed


class TestEvaluateInstance:
    def test_resolves_when_all_pass(self, tmp_path: Path):
        af = AssertionFile(
            turn_id="turn_1",
            category="tool-heavy",
            channel="signal",
            pass_to_pass=[
                {"type": "no_empty_reply"},
                {"type": "channel_format_ok", "channel": "signal"},
            ],
            fail_to_pass=[
                {
                    "type": "tool_call_match",
                    "expected_tools": ["send_message"],
                }
            ],
            historical_reply="ok",
        )
        result = evaluate_instance(
            af,
            "send_message(recipient='jason', message='ok')",
            candidate_id="opus",
        )
        assert result.resolved
        assert all(r.passed for r in result.results)
        assert result.candidate_id == "opus"

    def test_fails_on_any_assertion(self):
        af = AssertionFile(
            turn_id="turn_2",
            category="tactical",
            channel="signal",
            pass_to_pass=[{"type": "no_empty_reply"}],
            fail_to_pass=[
                {
                    "type": "tool_call_match",
                    "expected_tools": ["send_message"],
                }
            ],
            historical_reply="ok",
        )
        # Non-empty reply (P2P passes), but no send_message (F2P fails)
        result = evaluate_instance(af, "just text", candidate_id="qwen")
        assert not result.resolved

    def test_load_assertion_file_roundtrip(self, tmp_path: Path):
        af = AssertionFile(
            turn_id="turn_x",
            category="tactical",
            channel="signal",
            pass_to_pass=[{"type": "no_empty_reply"}],
            fail_to_pass=[{"type": "bleu_threshold", "reference": "ok"}],
            historical_reply="ok",
        )
        path = tmp_path / "turn_x.assert.json"
        path.write_text(json.dumps(af.to_dict()))
        loaded = load_assertion_file(path)
        assert loaded.turn_id == "turn_x"
        assert loaded.pass_to_pass == af.pass_to_pass
        assert loaded.fail_to_pass == af.fail_to_pass


def test_register_custom_assertion_type():
    def check(output: str, params):
        return ("alice" in output.lower(), "")

    assertions.register_assertion_type("contains_alice", check)
    result = evaluate_assertion(
        "Alice is here", {"type": "contains_alice"}, bucket="pass_to_pass"
    )
    assert result.passed
    # Cleanup so other tests aren't affected.
    assertions.ASSERTION_TYPES.pop("contains_alice", None)
