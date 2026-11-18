"""Slice-2 slash commands (issue #624).

Covers the /pause, /resume, /quiet, /interrupt, /cancel, /kill, /forget,
/help ACL-filtering, and Katie's narrow ACL. Dispatch-category verbs
(/go, /retry, /dispatch) shell out to the ``task`` CLI — covered here
with a monkeypatched subprocess.run so the test suite stays hermetic.
"""

from __future__ import annotations

import asyncio
import types

from alice_speaking import slash_commands, slash_state


def _ctx(cfg):
    ns = types.SimpleNamespace()
    ns.cfg = cfg
    ns.signal_transport = None
    ns._queue = None
    ns._current_turn_kind = None
    ns._surface_watcher = None
    return ns


# ---------------------------------------------------------------------------
# /pause and /resume


def test_pause_writes_sentinel_and_resume_clears_it(cfg):
    ctx = _ctx(cfg)
    assert not slash_state.is_paused(state_dir=cfg.state_dir)

    response = asyncio.run(slash_commands.handle("/pause maintenance", "jason", ctx))
    assert response is not None
    assert "paused" in response.reply_text.lower()
    assert slash_state.is_paused(state_dir=cfg.state_dir)
    record = slash_state.read_pause_record(state_dir=cfg.state_dir)
    assert record is not None
    assert record["reason"] == "maintenance"
    assert record["principal_id"] == "jason"

    response = asyncio.run(slash_commands.handle("/resume", "jason", ctx))
    assert response is not None
    assert "resumed" in response.reply_text.lower()
    assert not slash_state.is_paused(state_dir=cfg.state_dir)


def test_resume_without_pause_reports_was_not_paused(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/resume", "jason", ctx))
    assert response is not None
    assert "was not paused" in response.reply_text.lower()


# ---------------------------------------------------------------------------
# /quiet


def test_quiet_default_duration_writes_sentinel(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/quiet", "jason", ctx))
    assert response is not None
    assert "suppressing" in response.reply_text.lower()
    assert slash_state.quiet_until(state_dir=cfg.state_dir) is not None


def test_quiet_duration_parsed(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/quiet 10m", "jason", ctx))
    assert response is not None
    assert slash_state.quiet_until(state_dir=cfg.state_dir) is not None


def test_quiet_off_clears_sentinel(cfg):
    ctx = _ctx(cfg)
    asyncio.run(slash_commands.handle("/quiet 5m", "jason", ctx))
    response = asyncio.run(slash_commands.handle("/quiet off", "jason", ctx))
    assert response is not None
    assert "cleared" in response.reply_text.lower()
    assert slash_state.quiet_until(state_dir=cfg.state_dir) is None


def test_quiet_bad_duration_returns_usage(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/quiet banana", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


# ---------------------------------------------------------------------------
# /interrupt and /cancel


def test_interrupt_records_sentinel_line(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(
        slash_commands.handle("/interrupt forget the previous task, do X instead", "jason", ctx)
    )
    assert response is not None
    assert "interrupt" in response.reply_text.lower()
    sentinel = cfg.state_dir / slash_state.INTERRUPT_SENTINEL
    assert sentinel.is_file()
    lines = sentinel.read_text().splitlines()
    assert any("do X instead" in line for line in lines)


def test_interrupt_without_message_still_recorded(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/interrupt", "jason", ctx))
    assert response is not None
    sentinel = cfg.state_dir / slash_state.INTERRUPT_SENTINEL
    assert sentinel.is_file()


def test_interrupt_drains_pending_outbound_when_transport_exposes_it(cfg):
    ctx = _ctx(cfg)
    class _FakeTransport:
        async def cancel_pending(self):
            return 3

    ctx.signal_transport = _FakeTransport()
    response = asyncio.run(slash_commands.handle("/interrupt stop", "jason", ctx))
    assert response is not None
    assert "cancelled: 3" in response.reply_text.lower()


def test_cancel_records_target(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/cancel 1234", "jason", ctx))
    assert response is not None
    assert "1234" in response.reply_text
    sentinel = cfg.state_dir / slash_state.CANCEL_SENTINEL
    assert sentinel.is_file()
    assert "1234" in sentinel.read_text()


def test_cancel_defaults_to_current(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/cancel", "jason", ctx))
    assert response is not None
    assert "current" in response.reply_text


# ---------------------------------------------------------------------------
# /kill


def test_kill_requires_target(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/kill", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


def test_kill_records_cancel_sentinel(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/kill 920", "jason", ctx))
    assert response is not None
    assert "920" in response.reply_text
    assert "920" in (cfg.state_dir / slash_state.CANCEL_SENTINEL).read_text()


# ---------------------------------------------------------------------------
# /forget


def test_forget_writes_tombstone_note(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(
        slash_commands.handle("/forget outdated-vault-slug", "jason", ctx)
    )
    assert response is not None
    assert "outdated-vault-slug" in response.reply_text
    notes = list((cfg.mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "tag: correction" in body
    assert "target: outdated-vault-slug" in body
    assert "op: tombstone" in body


def test_forget_requires_slug(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/forget", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


# ---------------------------------------------------------------------------
# /go, /retry, /dispatch — task-CLI bridge


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_go_invokes_task_cli(monkeypatch, cfg):
    ctx = _ctx(cfg)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ARG001
        captured.append(list(cmd))
        return _FakeCompletedProcess(0, stdout="selected #42\n")

    monkeypatch.setattr("alice_speaking.slash_commands.subprocess.run", fake_run)
    response = asyncio.run(slash_commands.handle("/go 42", "jason", ctx))
    assert response is not None
    assert "#42" in response.reply_text
    assert captured[0] == ["task", "sm", "select", "--gh", "42"]


def test_go_surface_failure_from_cli(monkeypatch, cfg):
    ctx = _ctx(cfg)
    def fake_run(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompletedProcess(2, stderr="not found\n")

    monkeypatch.setattr("alice_speaking.slash_commands.subprocess.run", fake_run)
    response = asyncio.run(slash_commands.handle("/go 9999", "jason", ctx))
    assert response is not None
    assert "failed" in response.reply_text.lower()


def test_retry_requires_issue(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/retry", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


def test_dispatch_invokes_task_cli(monkeypatch, cfg):
    ctx = _ctx(cfg)

    def fake_run(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompletedProcess(0, stdout="created task-1234\n")

    monkeypatch.setattr("alice_speaking.slash_commands.subprocess.run", fake_run)
    response = asyncio.run(
        slash_commands.handle("/dispatch fix the thing", "jason", ctx)
    )
    assert response is not None
    assert "task-1234" in response.reply_text


# ---------------------------------------------------------------------------
# Katie ACL — issue #624 decision


def test_katie_can_run_ack(cfg):
    ctx = _ctx(cfg)
    surface_dir = cfg.mind_dir / "inner" / "surface"
    surface_dir.mkdir(parents=True)
    (surface_dir / "2026-11-17-stub.md").write_text("---\nkind: test\n---\n")

    response = asyncio.run(slash_commands.handle("/ack", "katie", ctx))
    assert response is not None
    assert response.reply_text is not None
    assert "resolved" in response.reply_text.lower()


def test_katie_can_run_food_shortcut(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/food salad", "katie", ctx))
    assert response is not None
    assert response.rewritten_text == "Use the log-meal skill: salad"


def test_katie_can_run_log_shortcut(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/log feeling great", "katie", ctx))
    assert response is not None
    assert response.rewritten_text == "Use the log-journal skill: feeling great"


def test_katie_denied_kill(cfg):
    """Katie invoking /kill must be ACL-denied with a visible reply."""
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/kill 42", "katie", ctx))
    assert response is not None
    assert response.reply_text is not None
    assert "not available" in response.reply_text.lower()


def test_katie_denied_pause(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/pause", "katie", ctx))
    assert response is not None
    assert "not available" in response.reply_text.lower()
    assert not slash_state.is_paused(state_dir=cfg.state_dir)


def test_katie_help_shows_only_her_commands(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/help", "katie", ctx))
    assert response is not None
    assert "/ack" in response.reply_text
    assert "/food" in response.reply_text
    assert "/log" in response.reply_text
    # Katie should NOT see Jason-only verbs in her help output.
    assert "/kill" not in response.reply_text
    assert "/pause" not in response.reply_text
    assert "/go" not in response.reply_text


def test_jason_help_shows_full_registry(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/help", "jason", ctx))
    assert response is not None
    for verb in (
        "/interrupt", "/cancel", "/pause", "/resume", "/status",
        "/quiet", "/ack", "/q",
        "/remember", "/forget", "/recall",
        "/go", "/kill", "/retry", "/dispatch",
        "/food", "/workout", "/weight", "/log",
    ):
        assert verb in response.reply_text


def test_unknown_principal_falls_through(cfg):
    """Non-registered principals (e.g. ``friend``) must fall through
    silently so the normal pipeline still handles their message as
    literal content — don't leak the slash-command inventory to them."""
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/help", "friend", ctx))
    assert response is None


# ---------------------------------------------------------------------------
# Status includes paused and quiet labels


def test_status_reports_paused(cfg):
    ctx = _ctx(cfg)
    slash_state.set_paused(principal_id="jason", state_dir=cfg.state_dir)
    response = asyncio.run(slash_commands.handle("/status", "jason", ctx))
    assert response is not None
    assert "paused" in response.reply_text.lower()


# ---------------------------------------------------------------------------
# Registry invariants


def test_registry_covers_full_command_set():
    """Issue #624 enumerates 20 verbs across six categories — assert
    the registry hasn't silently dropped any."""
    verbs = {cmd.verb for cmd in slash_commands.SLASH_COMMANDS}
    expected = {
        "interrupt", "cancel", "pause", "resume", "status",
        "quiet", "ack", "q",
        "remember", "forget", "recall",
        "go", "kill", "retry", "dispatch",
        "food", "workout", "weight", "log",
        "help",
    }
    assert expected <= verbs


def test_registry_acl_matches_decision():
    by_verb = {cmd.verb: cmd for cmd in slash_commands.SLASH_COMMANDS}
    # Every verb admits Jason (jason or jcronq principal id).
    for cmd in slash_commands.SLASH_COMMANDS:
        assert cmd.principal_allowed("jason"), f"Jason denied for /{cmd.verb}"
    # Katie's narrow subset per issue #624.
    for verb in ("ack", "food", "log", "help"):
        assert by_verb[verb].principal_allowed("katie"), f"Katie denied /{verb}"
    # Verify Katie denied on the Jason-only subset.
    for verb in ("kill", "pause", "go", "dispatch", "interrupt", "cancel", "recall", "forget"):
        assert not by_verb[verb].principal_allowed("katie"), (
            f"Katie should not have access to /{verb}"
        )
