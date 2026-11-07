"""Tests for ``alice_speaking.cron_hook`` — the PostToolUse bridge that
mirrors CronCreate / CronDelete / ScheduleWakeup outcomes into the
status-site Schedules panel's on-disk snapshot (issue #815).

Each tool-call completion shape is tested in isolation with its own
tmp-path :class:`ScheduleState`, so parallel runs don't trample each
other.
"""

from __future__ import annotations

import json
from pathlib import Path

from alice_speaking import cron_hook as ch
from alice_speaking import schedule_state as ss


def _fresh_state(tmp_path: Path) -> ss.ScheduleState:
    return ss.ScheduleState(path=tmp_path / "active.json")


def _snapshot(state: ss.ScheduleState) -> dict:
    return json.loads(state.path.read_text())


# ─── CronCreate ───────────────────────────────────────────────────────


def test_cron_create_persists_id_cron_and_human_schedule(tmp_path):
    state = _fresh_state(tmp_path)
    jid = ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={
            "cron": "*/5 * * * *",
            "prompt": "babysit PRs — secret note",
            "recurring": True,
        },
        tool_response={
            "id": "job-abc",
            "humanSchedule": "every 5 min",
            "recurring": True,
        },
        state=state,
    )
    assert jid == "job-abc"
    snap = _snapshot(state)
    assert len(snap["jobs"]) == 1
    row = snap["jobs"][0]
    assert row["id"] == "job-abc"
    assert row["cron"] == "*/5 * * * *"
    assert row["description"] == "every 5 min"


def test_cron_create_strips_prompt_from_snapshot(tmp_path):
    """Defense in depth — even if someone passes the prompt into the
    tool_input, the writer's allowlist plus our deliberate narrow
    extraction means the prompt text can't reach disk."""
    state = _fresh_state(tmp_path)
    secret = "DO NOT LEAK — internal credential context"
    ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={"cron": "0 * * * *", "prompt": secret},
        tool_response={"id": "job-1", "humanSchedule": "hourly"},
        state=state,
    )
    raw = state.path.read_text()
    assert secret not in raw
    # And the row has no 'prompt' key.
    row = _snapshot(state)["jobs"][0]
    assert "prompt" not in row
    assert "prompt_preview" not in row


def test_cron_create_accepts_nested_data_response(tmp_path):
    """If the hook ever surfaces the full ``{data: {...}}`` envelope
    rather than the unwrapped data object, we still extract the id."""
    state = _fresh_state(tmp_path)
    jid = ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={"cron": "0 9 * * 1-5", "prompt": "x"},
        tool_response={"data": {"id": "job-nested", "humanSchedule": "weekdays at 09:00"}},
        state=state,
    )
    assert jid == "job-nested"
    row = _snapshot(state)["jobs"][0]
    assert row["id"] == "job-nested"
    assert row["description"] == "weekdays at 09:00"


def test_cron_create_missing_cron_is_noop(tmp_path):
    state = _fresh_state(tmp_path)
    result = ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={"prompt": "x"},
        tool_response={"id": "job-1"},
        state=state,
    )
    assert result is None
    # Nothing written.
    assert not state.path.exists()


def test_cron_create_missing_id_is_noop(tmp_path):
    state = _fresh_state(tmp_path)
    result = ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={"cron": "*/5 * * * *", "prompt": "x"},
        tool_response={"humanSchedule": "every 5 min"},
        state=state,
    )
    assert result is None
    assert not state.path.exists()


# ─── CronDelete ───────────────────────────────────────────────────────


def test_cron_delete_removes_known_job(tmp_path):
    state = _fresh_state(tmp_path)
    state.record_cron(id="job-abc", cron="*/5 * * * *", description="x")
    jid = ch.record_cron_tool_outcome(
        tool_name="CronDelete",
        tool_input={"id": "job-abc"},
        tool_response={"id": "job-abc"},
        state=state,
    )
    assert jid == "job-abc"
    assert _snapshot(state)["jobs"] == []


def test_cron_delete_falls_back_to_cancelled_string_content(tmp_path):
    """If ``tool_response`` surfaces as the model-visible content
    block, we can still parse the id out of ``"Cancelled job <id>."``."""
    state = _fresh_state(tmp_path)
    state.record_cron(id="job-xyz", cron="0 * * * *")
    jid = ch.record_cron_tool_outcome(
        tool_name="CronDelete",
        tool_input={},  # input id missing — exercise the fallback
        tool_response="Cancelled job job-xyz.",
        state=state,
    )
    assert jid == "job-xyz"
    assert _snapshot(state)["jobs"] == []


def test_cron_delete_unknown_id_is_harmless(tmp_path):
    state = _fresh_state(tmp_path)
    state.record_cron(id="other", cron="*/5 * * * *")
    jid = ch.record_cron_tool_outcome(
        tool_name="CronDelete",
        tool_input={"id": "ghost"},
        tool_response={"id": "ghost"},
        state=state,
    )
    assert jid == "ghost"
    # Existing jobs untouched — forget_cron returns False but we
    # don't blow up.
    ids = [j["id"] for j in _snapshot(state)["jobs"]]
    assert ids == ["other"]


def test_cron_delete_missing_id_everywhere_is_noop(tmp_path):
    state = _fresh_state(tmp_path)
    state.record_cron(id="job-1", cron="*/5 * * * *")
    result = ch.record_cron_tool_outcome(
        tool_name="CronDelete",
        tool_input={},
        tool_response={"content": "something else"},
        state=state,
    )
    assert result is None
    # Existing jobs untouched (one row remained).
    ids = [j["id"] for j in _snapshot(state)["jobs"]]
    assert ids == ["job-1"]


# ─── ScheduleWakeup ───────────────────────────────────────────────────


def test_schedule_wakeup_upserts_singleton_row(tmp_path):
    state = _fresh_state(tmp_path)
    future_epoch_ms = 1800000000000  # 2027-01-15
    jid = ch.record_cron_tool_outcome(
        tool_name="ScheduleWakeup",
        tool_input={
            "delaySeconds": 270,
            "reason": "checking long build",
            "prompt": "do not persist me",
        },
        tool_response={
            "scheduledFor": future_epoch_ms,
            "clampedDelaySeconds": 270,
            "wasClamped": False,
        },
        state=state,
    )
    assert jid == ch.WAKEUP_JOB_ID
    snap = _snapshot(state)
    assert len(snap["jobs"]) == 1
    row = snap["jobs"][0]
    assert row["id"] == ch.WAKEUP_JOB_ID
    assert row["cron"] == ch.WAKEUP_CRON_SENTINEL
    assert "checking long build" in row["description"]
    assert "+270s" in row["description"]
    assert row["next_run_at"]  # ISO string present
    # Prompt never touches disk.
    assert "do not persist me" not in state.path.read_text()


def test_schedule_wakeup_overwrites_previous_row(tmp_path):
    state = _fresh_state(tmp_path)
    ch.record_cron_tool_outcome(
        tool_name="ScheduleWakeup",
        tool_input={"delaySeconds": 60, "reason": "first"},
        tool_response={
            "scheduledFor": 1800000000000,
            "clampedDelaySeconds": 60,
            "wasClamped": False,
        },
        state=state,
    )
    ch.record_cron_tool_outcome(
        tool_name="ScheduleWakeup",
        tool_input={"delaySeconds": 1200, "reason": "second"},
        tool_response={
            "scheduledFor": 1800003600000,
            "clampedDelaySeconds": 1200,
            "wasClamped": False,
        },
        state=state,
    )
    snap = _snapshot(state)
    assert len(snap["jobs"]) == 1
    assert "second" in snap["jobs"][0]["description"]
    assert "first" not in snap["jobs"][0]["description"]


def test_schedule_wakeup_zero_scheduled_for_is_noop(tmp_path):
    """``scheduledFor == 0`` means the SDK didn't schedule (gate off /
    loop exhausted). Don't write a bogus row."""
    state = _fresh_state(tmp_path)
    result = ch.record_cron_tool_outcome(
        tool_name="ScheduleWakeup",
        tool_input={"delaySeconds": 60, "reason": "never fires"},
        tool_response={
            "scheduledFor": 0,
            "clampedDelaySeconds": 0,
            "wasClamped": False,
        },
        state=state,
    )
    assert result is None
    assert not state.path.exists()


def test_schedule_wakeup_omits_delay_hint_when_zero_clamp(tmp_path):
    state = _fresh_state(tmp_path)
    ch.record_cron_tool_outcome(
        tool_name="ScheduleWakeup",
        tool_input={"delaySeconds": 0, "reason": "edge"},
        tool_response={
            "scheduledFor": 1800000000000,
            "clampedDelaySeconds": 0,
            "wasClamped": True,
        },
        state=state,
    )
    row = _snapshot(state)["jobs"][0]
    # No "(+0s)" tail when there's nothing to say
    assert "+0s" not in row["description"]
    assert row["description"] == "edge"


# ─── pass-through / defensive behaviour ───────────────────────────────


def test_non_cron_tool_is_noop(tmp_path):
    state = _fresh_state(tmp_path)
    result = ch.record_cron_tool_outcome(
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_response="",
        state=state,
    )
    assert result is None
    assert not state.path.exists()


def test_cron_tool_names_contains_expected_three():
    assert ch.CRON_TOOL_NAMES == frozenset(
        {"CronCreate", "CronDelete", "ScheduleWakeup"}
    )


def test_failure_in_state_write_is_swallowed(tmp_path, monkeypatch):
    """A broken sidecar writer must NOT propagate — the daemon's
    hook chain would lose mid-turn drain + Task interception."""
    state = _fresh_state(tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state, "record_cron", boom)
    # Should NOT raise.
    result = ch.record_cron_tool_outcome(
        tool_name="CronCreate",
        tool_input={"cron": "*/5 * * * *", "prompt": "x"},
        tool_response={"id": "job-1", "humanSchedule": "every 5 min"},
        state=state,
    )
    assert result is None


def test_atomic_write_no_torn_json(tmp_path):
    """Snapshots must always be well-formed JSON — the reader
    tolerates missing but not torn."""
    state = _fresh_state(tmp_path)
    for i in range(10):
        ch.record_cron_tool_outcome(
            tool_name="CronCreate",
            tool_input={"cron": f"*/{i+1} * * * *", "prompt": "x"},
            tool_response={"id": f"job-{i}", "humanSchedule": f"every {i+1} min"},
            state=state,
        )
    # Any read must parse cleanly.
    payload = json.loads(state.path.read_text())
    assert isinstance(payload["jobs"], list)
    assert len(payload["jobs"]) == 10


# ─── daemon hook integration (shape only — no full boot) ──────────────


def test_daemon_posttooluse_dispatches_cron_tools(tmp_path, monkeypatch):
    """When the daemon's PostToolUse hook fires with a cron tool
    name, it must route through :func:`record_cron_tool_outcome`.

    We stub the module-level recorder and verify the bind-site in
    daemon.py calls it with the right args. Mid-turn drain path is
    tested elsewhere; here we just verify the sidecar bridge wiring."""
    import asyncio

    from alice_speaking.daemon import SpeakingDaemon

    captured: list[dict] = []

    def fake_record(**kwargs):
        captured.append(kwargs)
        return "job-fake"

    from alice_speaking import cron_hook as ch_mod

    monkeypatch.setattr(ch_mod, "record_cron_tool_outcome", fake_record)

    class _StubEvents:
        def emit(self, *a, **kw):
            pass

    class _Stub:
        _channel_key = staticmethod(SpeakingDaemon._channel_key)

        def __init__(self):
            self.events = _StubEvents()
            self._mid_turn_inbox = {}
            self._current_reply_channel = None
            self._current_principal_display_name = None
            self._current_turn_replied = False

    stub = _Stub()
    hook = SpeakingDaemon._posttooluse_hook.__get__(stub, _Stub)

    # Cron-adjacent tool — should hit the recorder.
    asyncio.run(
        hook(
            {
                "tool_name": "CronCreate",
                "tool_input": {"cron": "*/5 * * * *", "prompt": "x"},
                "tool_response": {"id": "job-1", "humanSchedule": "every 5 min"},
            },
            "toolu_1",
            None,
        )
    )
    assert len(captured) == 1
    assert captured[0]["tool_name"] == "CronCreate"
    assert captured[0]["tool_input"]["cron"] == "*/5 * * * *"

    # Non-cron tool — recorder must NOT fire (filtered by name).
    asyncio.run(
        hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": ""},
            "toolu_2",
            None,
        )
    )
    assert len(captured) == 1  # unchanged
