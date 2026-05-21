"""Tests for the dual-run JSONL logger."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

from alice_forge.sm.dual_run import log_entry, render_result
from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    EmitParseError,
    NoProgress,
    SideEffect,
    Transition,
)
from alice_forge.sm.states import SMState


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 21, 19, 30, tzinfo=dt.timezone.utc)


class TestRenderResult:
    def test_transition(self):
        kind, payload = render_result(
            Transition(
                target=SMState.NEEDS_STUDY,
                reason="route-to-study",
                art_swap="art:code",
            )
        )
        assert kind == "Transition"
        assert payload["target"] == "sm:needs_study"
        assert payload["art_swap"] == "art:code"

    def test_continue(self):
        kind, payload = render_result(
            Continue(reason="still investigating", findings=None)
        )
        assert kind == "Continue"
        assert payload["reason"] == "still investigating"

    def test_side_effect(self):
        kind, payload = render_result(
            SideEffect(name="triage-surface", body="x" * 500, ttl_seconds=3600)
        )
        assert kind == "SideEffect"
        # Bodies are truncated to keep the log readable.
        assert len(payload["body"]) <= 200

    def test_no_progress(self):
        kind, payload = render_result(
            NoProgress(
                duplicate_reason="still working",
                duplicate_of_emitted_at="2026-05-21T18:00:00+00:00",
            )
        )
        assert kind == "NoProgress"

    def test_blocked_by_ttl(self):
        kind, payload = render_result(BlockedByTTL(state_ttl_seconds=3600))
        assert kind == "BlockedByTTL"
        assert payload["state_ttl_seconds"] == 3600

    def test_emit_parse_error(self):
        kind, payload = render_result(
            EmitParseError(
                verb="route-to-study",
                reason="trailing field",
                reply_body="[SM] parse-error reason=...",
            )
        )
        assert kind == "EmitParseError"


class TestLogEntry:
    def test_writes_single_jsonl_line(self, tmp_path: pathlib.Path):
        path = tmp_path / "sm-v3-predicted.jsonl"
        log_entry(
            path,
            cycle_id="cyc-1",
            lane="v3-predicted",
            repo="jcronq/alice",
            issue_number=42,
            state=SMState.DRAFT,
            result=Transition(target=SMState.NEEDS_STUDY, reason="route-to-study"),
            now=_now(),
        )
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["cycle_id"] == "cyc-1"
        assert entry["lane"] == "v3-predicted"
        assert entry["issue_number"] == 42
        assert entry["action_kind"] == "Transition"

    def test_silent_no_op_when_result_none(self, tmp_path: pathlib.Path):
        path = tmp_path / "sm-v1-actual.jsonl"
        log_entry(
            path,
            cycle_id="cyc-2",
            lane="v1-actual",
            repo="jcronq/alice",
            issue_number=99,
            state=SMState.DRAFT,
            result=None,
            now=_now(),
        )
        entry = json.loads(path.read_text())
        assert entry["action_kind"] == "SilentNoOp"

    def test_appends_to_existing_log(self, tmp_path: pathlib.Path):
        path = tmp_path / "log.jsonl"
        for cyc in ["a", "b", "c"]:
            log_entry(
                path,
                cycle_id=cyc,
                lane="v3-predicted",
                repo="jcronq/alice",
                issue_number=1,
                state=SMState.DRAFT,
                result=Continue(reason="iter " + cyc),
                now=_now(),
            )
        lines = path.read_text().splitlines()
        assert len(lines) == 3
        ids = [json.loads(line)["cycle_id"] for line in lines]
        assert ids == ["a", "b", "c"]

    def test_extra_field_merged_into_payload(self, tmp_path: pathlib.Path):
        path = tmp_path / "log.jsonl"
        log_entry(
            path,
            cycle_id="cyc",
            lane="v3-predicted",
            repo="jcronq/alice",
            issue_number=1,
            state="sm:draft",  # string accepted
            result=Continue(reason="x"),
            extra={"comments_seen": 4},
            now=_now(),
        )
        entry = json.loads(path.read_text())
        assert entry["payload"]["_extra"] == {"comments_seen": 4}
