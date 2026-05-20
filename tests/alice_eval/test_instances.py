"""Tests for the instance-synthesis pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from eval.assertions import load_assertion_file
from eval.instances import (
    DEFAULT_KNOWN_TOOLS,
    derive_assertion_file,
    main_instances,
    write_assertion_files,
)


def _sample(**overrides):
    base = {
        "turn_id": "turn_42",
        "sampled_category": "tactical",
        "sender_number": "+15553334444",
        "inbound": "what's the deploy status",
        "outbound": "All green.",
    }
    base.update(overrides)
    return base


class TestDeriveAssertionFile:
    def test_basic_tactical_has_p2p_guards(self):
        af = derive_assertion_file(_sample())
        types = {a["type"] for a in af.pass_to_pass}
        assert "no_empty_reply" in types
        assert "channel_format_ok" in types
        assert "no_hallucinated_tool" in types
        # Signal channel adds the signal-cli forbidden-tool guard
        assert "no_forbidden_tool" in types

    def test_cli_channel_skips_forbidden_signal_cli(self):
        # No sender_number → cli channel → no signal-cli guard
        af = derive_assertion_file(_sample(sender_number=None, outbound="$ ls"))
        types = {a["type"] for a in af.pass_to_pass}
        assert "no_forbidden_tool" not in types

    def test_skill_fire_produces_skill_invocation(self):
        af = derive_assertion_file(
            _sample(
                sampled_category="tactical",
                outbound="log-meal: oatmeal, calories: 320, protein: 12g",
            )
        )
        f2p_types = {a["type"] for a in af.fail_to_pass}
        assert "skill_invocation" in f2p_types
        sk = [a for a in af.fail_to_pass if a["type"] == "skill_invocation"][0]
        assert sk["skill"] == "log-meal"
        assert sk["required_fields"].get("calories") == 320.0
        assert sk["required_fields"].get("protein") == 12.0

    def test_send_message_recipient_extracted(self):
        af = derive_assertion_file(
            _sample(
                sampled_category="tactical",
                outbound='send_message(recipient="jason", message="hi")',
            )
        )
        f2p = af.fail_to_pass
        arg_match = [a for a in f2p if a["type"] == "arg_match"]
        assert any(
            a["arg"] == "recipient" and a["value"] == "jason"
            for a in arg_match
        )
        # And a tool_call_match for send_message
        tc = [a for a in f2p if a["type"] == "tool_call_match"]
        assert any("send_message" in a["expected_tools"] for a in tc)

    def test_image_category_adds_entity_overlap(self):
        af = derive_assertion_file(
            _sample(
                sampled_category="image",
                outbound="Looks like Katie on the porch at sunset.",
            )
        )
        f2p_types = {a["type"] for a in af.fail_to_pass}
        assert "entity_overlap" in f2p_types

    def test_prose_fallback_to_bleu(self):
        af = derive_assertion_file(
            _sample(
                sampled_category="conversational",
                outbound="Just a thought — Tuesday seems fine.",
            )
        )
        f2p_types = {a["type"] for a in af.fail_to_pass}
        assert "bleu_threshold" in f2p_types

    def test_dispatch_signal_emits_routing_assertion(self):
        af = derive_assertion_file(
            _sample(
                sampled_category="design",
                outbound="Agent(prompt='...') — worker dispatched",
            )
        )
        f2p_types = {a["type"] for a in af.fail_to_pass}
        assert "routing_decision" in f2p_types

    def test_no_hallucinated_tool_allowlist_includes_defaults(self):
        af = derive_assertion_file(_sample())
        nh = [
            a for a in af.pass_to_pass if a["type"] == "no_hallucinated_tool"
        ][0]
        assert "send_message" in nh["allowed_tools"]
        # Ensure we pass through the full DEFAULT_KNOWN_TOOLS list
        assert set(DEFAULT_KNOWN_TOOLS).issubset(set(nh["allowed_tools"]))


class TestWriteAssertionFiles:
    def test_writes_one_file_per_sample(self, tmp_path: Path):
        samples = [
            _sample(turn_id="turn_1"),
            _sample(turn_id="turn_2", outbound="log-meal: salad, calories: 200"),
        ]
        paths = write_assertion_files(samples, out_dir=tmp_path)
        assert len(paths) == 2
        # Files exist and are valid AssertionFile JSON
        for path in paths:
            assert path.is_file()
            af = load_assertion_file(path)
            assert af.turn_id in {"turn_1", "turn_2"}

    def test_main_instances_reads_sample_jsonl(self, tmp_path: Path):
        sample_path = tmp_path / "eval_sample.jsonl"
        sample_path.write_text(
            "\n".join(
                json.dumps(s)
                for s in [
                    _sample(turn_id="turn_a"),
                    _sample(
                        turn_id="turn_b",
                        outbound=(
                            'send_message(recipient="katie", '
                            'message="ok")'
                        ),
                    ),
                ]
            )
        )
        out_dir = tmp_path / "instances"
        paths = main_instances(sample_path=sample_path, out_dir=out_dir)
        assert len(paths) == 2
        # turn_b should have a recipient arg_match
        af_b = load_assertion_file(out_dir / "turn_b.assert.json")
        assert any(
            a["type"] == "arg_match"
            and a.get("arg") == "recipient"
            and a.get("value") == "katie"
            for a in af_b.fail_to_pass
        )
