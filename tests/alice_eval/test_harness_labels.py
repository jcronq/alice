"""Offline tests for the speaking-harness correctness eval.

Covers the three canonical spec-named failure-mode assertions and the
offline (historical-reconstruction) scoring path in
:mod:`eval.harness_replay`. Everything here runs without a network call:
the offline path reconstructs tool calls from the historical record, and
the assertions grade structured tool-call lists directly.
"""

from __future__ import annotations

from eval.assertions import evaluate_assertion
from eval.harness_replay import (
    assertions_for_label,
    label_category,
    offline_result,
    score_label_results,
)
from eval.score import score_results


SEND = {"name": "mcp__alice__send_message", "id": "s1", "input": {"message": "On it — pushed the fix."}}
EMOJI_SEND = {"name": "mcp__alice__send_message", "id": "s2", "input": {"message": "👍"}}
NOTE = {"name": "mcp__alice__append_note", "id": "n1", "input": {"text": "x"}}
AGENT = {"name": "Agent", "id": "a1", "input": {"prompt": "do work"}}


# ---------------------------------------------------------------------------
# action_taken_when_required (failure mode 1: bare-ack-no-action)


class TestActionTakenWhenRequired:
    def test_pass_with_substantive_send(self):
        r = evaluate_assertion(
            "On it.", {"type": "action_taken_when_required", "channel": "signal"},
            "fail_to_pass", tool_calls=[SEND],
        )
        assert r.passed

    def test_pass_with_nonsend_tool(self):
        r = evaluate_assertion(
            "filed it", {"type": "action_taken_when_required", "channel": "signal"},
            "fail_to_pass", tool_calls=[NOTE],
        )
        assert r.passed

    def test_fail_on_bare_emoji_send(self):
        # 👍 sent where action was required — the headline failure.
        r = evaluate_assertion(
            "👍", {"type": "action_taken_when_required", "channel": "signal"},
            "fail_to_pass", tool_calls=[EMOJI_SEND],
        )
        assert not r.passed

    def test_fail_on_no_tools_signal(self):
        r = evaluate_assertion(
            "👍", {"type": "action_taken_when_required", "channel": "signal"},
            "fail_to_pass", tool_calls=[],
        )
        assert not r.passed

    def test_cli_substantive_text_passes_without_tool(self):
        r = evaluate_assertion(
            "Yes — 20TB works, depends on your vdev layout.",
            {"type": "action_taken_when_required", "channel": "cli"},
            "fail_to_pass", tool_calls=[],
        )
        assert r.passed

    def test_cli_emoji_only_text_fails(self):
        r = evaluate_assertion(
            "👍", {"type": "action_taken_when_required", "channel": "cli"},
            "fail_to_pass", tool_calls=[],
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# claim_backed_by_tool (failure mode 2: false completion claim)


class TestClaimBackedByTool:
    def test_no_claim_passes(self):
        r = evaluate_assertion(
            "How can I help?", {"type": "claim_backed_by_tool"},
            "pass_to_pass", tool_calls=[],
        )
        assert r.passed

    def test_send_claim_backed_by_send(self):
        r = evaluate_assertion(
            "Sent it to Katie.", {"type": "claim_backed_by_tool"},
            "pass_to_pass", tool_calls=[SEND],
        )
        assert r.passed

    def test_send_claim_unbacked_fails(self):
        r = evaluate_assertion(
            "Sent it to Katie.", {"type": "claim_backed_by_tool"},
            "pass_to_pass", tool_calls=[],
        )
        assert not r.passed

    def test_implicit_ack_complete_emoji_unbacked_fails(self):
        # Sole emoji on a not-acceptable-ack turn = implicit "done" claim.
        r = evaluate_assertion(
            "👍",
            {"type": "claim_backed_by_tool", "acceptable_ack_only": False},
            "pass_to_pass", tool_calls=[],
        )
        assert not r.passed

    def test_emoji_acceptable_ack_passes(self):
        r = evaluate_assertion(
            "👍",
            {"type": "claim_backed_by_tool", "acceptable_ack_only": True},
            "pass_to_pass", tool_calls=[],
        )
        assert r.passed

    def test_dispatch_claim_backed_by_agent(self):
        r = evaluate_assertion(
            "Opened a draft PR for that.", {"type": "claim_backed_by_tool"},
            "pass_to_pass", tool_calls=[AGENT],
        )
        assert r.passed


# ---------------------------------------------------------------------------
# send_message_when_expected (failure mode 3: missing send)


class TestSendMessageWhenExpected:
    def test_pass_when_send_present(self):
        r = evaluate_assertion(
            "On it.", {"type": "send_message_when_expected", "channel": "signal"},
            "fail_to_pass", tool_calls=[SEND],
        )
        assert r.passed

    def test_fail_when_no_send(self):
        r = evaluate_assertion(
            "On it.", {"type": "send_message_when_expected", "channel": "signal"},
            "fail_to_pass", tool_calls=[NOTE],
        )
        assert not r.passed

    def test_fail_on_empty_tools(self):
        r = evaluate_assertion(
            "", {"type": "send_message_when_expected", "channel": "signal"},
            "fail_to_pass", tool_calls=[],
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# offline reconstruction


class TestOfflineResult:
    def test_signal_nonempty_synthesises_send(self):
        label = {
            "turn_id": "t1", "channel": "signal",
            "inbound": "fix the thing", "historical_outbound": "On it — done.",
        }
        res = offline_result(label)
        assert res.sent
        assert res.tool_calls[0]["name"] == "mcp__alice__send_message"
        assert res.tool_calls[0]["input"]["message"] == "On it — done."

    def test_signal_empty_outbound_no_send(self):
        label = {
            "turn_id": "t2", "channel": "signal",
            "inbound": "just fix things", "historical_outbound": "",
        }
        res = offline_result(label)
        assert not res.sent
        assert res.tool_calls == []

    def test_cli_no_send_synthesised(self):
        label = {
            "turn_id": "t3", "channel": "cli",
            "inbound": "review this", "historical_outbound": "Looks good, ship it.",
        }
        res = offline_result(label)
        assert not res.sent
        assert res.tool_calls == []
        assert res.outbound_text == "Looks good, ship it."


# ---------------------------------------------------------------------------
# label -> assertions wiring + category bucketing


class TestAssertionsForLabel:
    def test_signal_action_has_all_three(self):
        label = {
            "turn_id": "t", "channel": "signal", "action_required": True,
            "acceptable_ack_only": False, "expected_tools": ["send_message"],
        }
        af = assertions_for_label(label)
        types = {a["type"] for a in af.pass_to_pass + af.fail_to_pass}
        assert "claim_backed_by_tool" in types
        assert "action_taken_when_required" in types
        assert "send_message_when_expected" in types

    def test_cli_action_has_no_send_expectation(self):
        label = {
            "turn_id": "t", "channel": "cli", "action_required": True,
            "acceptable_ack_only": False, "expected_tools": [],
        }
        af = assertions_for_label(label)
        types = {a["type"] for a in af.pass_to_pass + af.fail_to_pass}
        assert "send_message_when_expected" not in types
        assert "action_taken_when_required" in types

    def test_ack_label_has_no_action_check(self):
        label = {
            "turn_id": "t", "channel": "signal", "action_required": False,
            "acceptable_ack_only": True, "expected_tools": [],
        }
        af = assertions_for_label(label)
        types = {a["type"] for a in af.fail_to_pass}
        assert "action_taken_when_required" not in types

    def test_category_buckets(self):
        assert label_category({"acceptable_ack_only": True}) == "ack"
        assert label_category({"action_required": False}) == "fyi"
        assert label_category(
            {"action_required": True, "channel": "cli"}
        ) == "action-cli"
        assert label_category(
            {"action_required": True, "channel": "signal"}
        ) == "action-signal"


# ---------------------------------------------------------------------------
# end-to-end offline scoring round-trip


class TestOfflineScoringRoundTrip:
    def test_bare_ack_action_signal_fails(self):
        # Action required on Signal, but Alice sent only 👍 — should NOT resolve.
        label = {
            "turn_id": "bad", "channel": "signal", "action_required": True,
            "acceptable_ack_only": False, "expected_tools": ["send_message"],
            "inbound": "just fix things and get it working",
            "historical_outbound": "👍",
        }
        rows = score_label_results([label], [offline_result(label)])
        assert rows[0]["resolved"] is False

    def test_substantive_signal_answer_resolves(self):
        label = {
            "turn_id": "good", "channel": "signal", "action_required": True,
            "acceptable_ack_only": False, "expected_tools": ["send_message"],
            "inbound": "is this going to work?",
            "historical_outbound": "Yes — the resilver finishes clean.",
        }
        rows = score_label_results([label], [offline_result(label)])
        assert rows[0]["resolved"] is True

    def test_score_results_breaks_down_by_assertion(self):
        labels = [
            {
                "turn_id": "bad", "channel": "signal", "action_required": True,
                "acceptable_ack_only": False, "expected_tools": ["send_message"],
                "inbound": "fix it", "historical_outbound": "👍",
            },
            {
                "turn_id": "good", "channel": "signal", "action_required": True,
                "acceptable_ack_only": False, "expected_tools": ["send_message"],
                "inbound": "is it ok?", "historical_outbound": "Yes, all good.",
            },
        ]
        results = [offline_result(x) for x in labels]
        rows = score_label_results(labels, results)
        report = score_results(rows)
        assert "action_taken_when_required" in report.by_assertion
        assert "send_message_when_expected" in report.by_assertion
        # The bad row trips action_taken; the good one passes.
        at = report.by_assertion["action_taken_when_required"]
        assert at.total == 2 and at.passed == 1

    def test_missing_result_marked_unresolved(self):
        label = {
            "turn_id": "ghost", "channel": "signal", "action_required": True,
            "acceptable_ack_only": False, "expected_tools": ["send_message"],
            "inbound": "x", "historical_outbound": "y",
        }
        rows = score_label_results([label], [])  # no matching HarnessResult
        assert rows[0]["resolved"] is False
        assert rows[0]["error"] == "no harness result"
