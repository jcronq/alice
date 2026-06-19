"""Tests for the real-TurnRunner correctness harness (fake/deterministic
mode).

These prove the capture seam end-to-end without a live model: a fake SDK
``query`` yields a turn that emits a ``send_message`` tool call, and the
harness reads it back off ``TurnRunner.last_tool_calls`` — exactly the
structured signal the legacy benchmark could not recover.
"""

from __future__ import annotations

import pytest

from eval import harness_replay as hr


@pytest.mark.asyncio
async def test_harness_captures_send_message_tool_call(tmp_path):
    messages = hr.make_fake_messages(
        text="Done — sent it.",
        tool_calls=[
            {"name": "mcp__alice__send_message", "input": {"recipient": "jason"}}
        ],
    )
    result = await hr.run_case(
        {"turn_id": "t1", "inbound": "tell katie I'm running late"},
        tmp_dir=tmp_path,
        fake_messages=messages,
    )
    assert result.error is None
    assert result.outbound_text == "Done — sent it."
    assert any(tc["name"].endswith("send_message") for tc in result.tool_calls)
    assert result.sent is True


@pytest.mark.asyncio
async def test_harness_bare_ack_has_no_tool_calls(tmp_path):
    # A turn that only replies "👍" and calls nothing.
    messages = hr.make_fake_messages(text="👍", tool_calls=[])
    result = await hr.run_case(
        {"turn_id": "t2", "inbound": "log my bench 100x8"},
        tmp_dir=tmp_path,
        fake_messages=messages,
    )
    assert result.tool_calls == []
    assert result.sent is False


@pytest.mark.asyncio
async def test_harness_captures_non_send_tool(tmp_path):
    messages = hr.make_fake_messages(
        text="Logged.",
        tool_calls=[{"name": "mcp__alice__log_event", "input": {}}],
    )
    result = await hr.run_case(
        {"turn_id": "t3", "inbound": "x"},
        tmp_dir=tmp_path,
        fake_messages=messages,
    )
    assert [tc["name"] for tc in result.tool_calls] == ["mcp__alice__log_event"]
    assert result.sent is False


def test_score_harness_results_action_required_pass():
    cases = [
        {
            "turn_id": "t1",
            "inbound": "tell katie hi",
            "expected_action_required": True,
            "expected_tools": ["send_message"],
            "category": "tactical",
        }
    ]
    results = [
        hr.HarnessResult(
            turn_id="t1",
            inbound="tell katie hi",
            outbound_text="On it.",
            tool_calls=[{"name": "mcp__alice__send_message", "id": "s1"}],
            sent=True,
        )
    ]
    rows = hr.score_harness_results(cases, results)
    assert len(rows) == 1
    assert rows[0]["resolved"] is True


def test_score_harness_results_bare_ack_fails():
    cases = [
        {
            "turn_id": "t2",
            "inbound": "log my bench",
            "expected_action_required": True,
            "category": "tactical",
        }
    ]
    results = [
        hr.HarnessResult(
            turn_id="t2",
            inbound="log my bench",
            outbound_text="👍 done!",
            tool_calls=[],
            sent=False,
        )
    ]
    rows = hr.score_harness_results(cases, results)
    # Fails BOTH action_requires_send AND no_unbacked_completion_claim.
    assert rows[0]["resolved"] is False


def test_assertions_for_case_shape():
    af = hr.assertions_for_case(
        {
            "turn_id": "t1",
            "expected_action_required": True,
            "expected_tools": ["send_message", "log_event"],
        }
    )
    ptp = {a["type"] for a in af.pass_to_pass}
    ftp = {a["type"] for a in af.fail_to_pass}
    assert "no_unbacked_completion_claim" in ptp
    assert "action_requires_send" in ftp
    assert "tool_call_match" in ftp
