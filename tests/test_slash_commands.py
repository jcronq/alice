"""Parser + per-verb tests for the Signal slash-command dispatcher.

Covers the slice-1 verb set: /help, /status, /ack, /q, /recall,
/remember. The ACL and escape paths are covered in sibling test
modules so each file stays focused on one axis.
"""

from __future__ import annotations

import asyncio
import types


from alice_speaking import slash_commands
from alice_speaking.slash_commands import SLICE1_VERBS


def _ctx(cfg):
    """Minimal context stub for slash handlers.

    The handlers reach into ctx.cfg, ctx.signal_transport, ctx._queue,
    ctx._current_turn_kind, and ctx.events. We provide just enough for
    the slice-1 verbs — tests that need more wire it explicitly.
    """
    ns = types.SimpleNamespace()
    ns.cfg = cfg
    ns.signal_transport = None
    ns._queue = None
    ns._current_turn_kind = None
    ns._surface_watcher = None
    return ns


# ---------------------------------------------------------------------------
# Parser


def test_help_lists_every_slice1_verb(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/help", "jason", ctx))
    assert response is not None
    assert response.reply_text is not None
    for verb, _ in SLICE1_VERBS:
        assert f"/{verb}" in response.reply_text


def test_unknown_verb_falls_through(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/notaverb arg", "jason", ctx))
    assert response is None


def test_non_slash_message_falls_through(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("hello", "jason", ctx))
    assert response is None


def test_verb_case_insensitive(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/HELP", "jason", ctx))
    assert response is not None
    assert response.reply_text is not None


# ---------------------------------------------------------------------------
# /status


def test_status_includes_queue_and_current(cfg):
    ctx = _ctx(cfg)
    ctx._current_turn_kind = "signal"
    response = asyncio.run(slash_commands.handle("/status", "jason", ctx))
    assert response is not None
    assert "current:" in response.reply_text
    assert "queue:" in response.reply_text


def test_status_reads_tail_of_event_log(cfg, tmp_path):
    ctx = _ctx(cfg)
    # Hand-craft two JSONL event lines: a turn_end + a send. The status
    # formatter should surface both.
    cfg.event_log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.event_log_path.write_text(
        '{"ts": 1, "event": "signal_turn_start", "turn_id": "a"}\n'
        '{"ts": 2, "event": "signal_send", "text": "hello from alice"}\n'
        '{"ts": 3, "event": "signal_turn_end", "turn_id": "a", "duration_ms": 420}\n'
    )
    response = asyncio.run(slash_commands.handle("/status", "jason", ctx))
    assert response is not None
    assert "signal" in response.reply_text
    assert "hello from alice" in response.reply_text


# ---------------------------------------------------------------------------
# /ack


def test_ack_moves_pending_surfaces(cfg):
    ctx = _ctx(cfg)
    surface_dir = cfg.mind_dir / "inner" / "surface"
    surface_dir.mkdir(parents=True)
    (surface_dir / "2026-11-06-123456-foo.md").write_text("---\nsurface_type: test\n---\nbody\n")
    (surface_dir / "2026-11-06-123457-bar.md").write_text("---\nsurface_type: test\n---\nbody\n")

    response = asyncio.run(slash_commands.handle("/ack", "jason", ctx))
    assert response is not None
    assert "2" in response.reply_text
    assert not (surface_dir / "2026-11-06-123456-foo.md").exists()
    assert not (surface_dir / "2026-11-06-123457-bar.md").exists()
    handled = list((surface_dir / ".handled").rglob("*.md"))
    assert len(handled) == 2
    for path in handled:
        assert "acked by Jason" in path.read_text()


def test_ack_reports_empty_when_no_pending(cfg):
    ctx = _ctx(cfg)
    # Dir doesn't exist yet — handler should still reply, not crash.
    response = asyncio.run(slash_commands.handle("/ack", "jason", ctx))
    assert response is not None
    assert response.reply_text is not None


# ---------------------------------------------------------------------------
# /q and /remember


def test_q_writes_low_priority_note(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(
        slash_commands.handle("/q don't preempt — think about this later", "jason", ctx)
    )
    assert response is not None
    assert "queued" in response.reply_text.lower()
    notes = list((cfg.mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "priority: low" in body
    assert "tag: idea" in body
    assert "don't preempt" in body


def test_remember_writes_idea_note_without_priority(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(
        slash_commands.handle("/remember the gym rower creaks in the cold", "jason", ctx)
    )
    assert response is not None
    assert response.reply_text.startswith("/remember:")
    notes = list((cfg.mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "priority:" not in body
    assert "tag: idea" in body
    assert "rower creaks" in body


def test_q_empty_argument_returns_usage(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/q", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()
    # No note was written.
    notes_dir = cfg.mind_dir / "inner" / "notes"
    assert not notes_dir.exists() or not list(notes_dir.glob("*.md"))


def test_remember_empty_argument_returns_usage(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/remember   ", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


# ---------------------------------------------------------------------------
# /recall


def test_recall_empty_argument_returns_usage(cfg):
    ctx = _ctx(cfg)
    response = asyncio.run(slash_commands.handle("/recall", "jason", ctx))
    assert response is not None
    assert "usage" in response.reply_text.lower()


def test_recall_handles_cue_runner_error(monkeypatch, cfg):
    """The cue runner is best-effort — a failure must produce a reply,
    not propagate."""

    ctx = _ctx(cfg)

    async def boom(*args, **kwargs):
        raise RuntimeError("fts missing")

    monkeypatch.setattr(
        "alice_speaking.retrieval.cue_runner.build_cue_context", boom
    )
    response = asyncio.run(slash_commands.handle("/recall foo", "jason", ctx))
    assert response is not None
    assert "errored" in response.reply_text.lower() or "no vault matches" in response.reply_text.lower()


def test_recall_formats_top_three(monkeypatch, cfg):
    """When the cue runner returns candidates, /recall renders up to 3."""

    ctx = _ctx(cfg)

    class _Candidate:
        def __init__(self, slug: str, title: str = "") -> None:
            self.slug = slug
            self.title = title

    class _CueContext:
        def __init__(self, candidates):
            self.candidates = candidates
            self.text = ""

    async def stub(*args, **kwargs):
        return _CueContext([
            _Candidate("note-one", "Note One"),
            _Candidate("note-two", "Note Two"),
            _Candidate("note-three", "Note Three"),
            _Candidate("note-four", "Note Four"),
        ])

    monkeypatch.setattr(
        "alice_speaking.retrieval.cue_runner.build_cue_context", stub
    )
    response = asyncio.run(slash_commands.handle("/recall anything", "jason", ctx))
    assert response is not None
    assert "note-one" in response.reply_text
    assert "note-three" in response.reply_text
    assert "note-four" not in response.reply_text  # capped at 3
