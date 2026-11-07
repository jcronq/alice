"""Chat-tab working-status rows + working-group rendering (#656)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from alice_speaking._dispatch import _is_chat_surface
from alice_speaking.turn_runner import ChatStatusHandler
from status_site import chat_status as cs
from status_site.server import Config
from status_site.sources import chat as chat_source


def _cfg(tmp_path):
    return Config(
        status_dir=tmp_path / "status",
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key="k",
    )


def _write_chat(cfg, *rows):
    cfg.status_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.chat_log, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _rows(status_dir):
    return [json.loads(ln) for ln in cs.status_path(status_dir).read_text().splitlines()]


def _thread(cfg):
    out = chat_source.render(cfg)["a2ui"]
    (dm,) = [e for e in out if e["type"] == "updateDataModel"]
    return dm["value"]


# ─── labels ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,inp,significant,needle",
    [
        ("Read", {"file_path": "/a/server.py"}, False, "📖 Read /a/server.py"),
        ("Grep", {"pattern": "foo"}, False, "Grep foo"),
        ("Edit", {"file_path": "/a/b.py"}, True, "✏️ Edited /a/b.py"),
        ("Bash", {"command": "cd /x && gh pr merge 654"}, True, "🚀 gh pr merge 654"),
        ("Bash", {"command": "ssh box uptime"}, True, "ssh box uptime"),
        ("Bash", {"command": "status-chat 'hi'"}, True, "status-chat"),
        ("Bash", {"command": "git status"}, False, "git status"),
        ("Bash", {"command": "ls -la"}, False, "ls -la"),
        ("mcp__alice__send_message", {"recipient": "signal"}, True, "✉️ Sent message to signal"),
        ("mcp__alice__append_note", {}, True, "📝"),
        ("Task", {"description": "fix #1"}, True, "🚀 Dispatched worker: fix #1"),
    ],
)
def test_describe_tool(name, inp, significant, needle):
    label, _cat, sig = cs.describe_tool(name, inp)
    assert sig is significant
    assert needle in label


def test_label_is_clipped():
    label, _, _ = cs.describe_tool("Bash", {"command": "gh " + "x" * 500})
    assert len(label) <= cs.MAX_LABEL


# ─── writer ─────────────────────────────────────────────────────────


def test_writer_emits_wellformed_rows(tmp_path):
    w = cs.ChatStatusWriter(tmp_path, "turn1")
    w.turn_start()
    w.tool_start("Edit", {"file_path": "/a.py"}, "tu1")
    w.tool_end("tu1", False)
    w.turn_end()
    rows = _rows(tmp_path)
    assert [r["kind"] for r in rows] == ["turn_start", "tool_start", "tool_end", "turn_end"]
    assert all(r["turn_id"] == "turn1" and r["role"] == cs.ROLE and r["at"] for r in rows)
    start = rows[1]
    assert start["tool"] == "Edit" and start["tool_use_id"] == "tu1"
    assert start["text"] == "✏️ Edited /a.py" and start["significant"] is True


def test_writer_never_raises_on_unwritable_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cs.ChatStatusWriter(blocker / "sub", "t").turn_start()


# ─── grouping ───────────────────────────────────────────────────────


def test_build_groups_one_group_per_turn_with_aggregate(tmp_path):
    w = cs.ChatStatusWriter(tmp_path, "t1")
    w.turn_start()
    for i in range(3):
        w.tool_start("Read", {"file_path": f"/f{i}"}, f"r{i}")
        w.tool_end(f"r{i}")
    w.tool_start("Edit", {"file_path": "/x.py"}, "e1")
    w.tool_end("e1")
    w.tool_start("Bash", {"command": "gh pr merge 1"}, "b1")
    (g,) = cs.build_groups(cs.read_status_rows(tmp_path))
    assert g["variant"] == cs.VARIANT and g["turn_id"] == "t1"
    assert g["active"] is True and g["text"] == cs.WORKING_TEXT
    assert g["step_count"] == 5
    labels = [s["label"] for s in g["steps"]]
    assert labels[0] == "✏️ Edited /x.py" and labels[1] == "🚀 gh pr merge 1"
    assert labels[2] == "🔍 3 files read"
    assert [s["state"] for s in g["steps"]] == ["done", "running", "done"]


def test_turn_end_marks_done_and_error_state(tmp_path):
    w = cs.ChatStatusWriter(tmp_path, "t1")
    w.turn_start()
    w.tool_start("Edit", {"file_path": "/x"}, "e1")
    w.tool_end("e1", True)
    w.turn_end(error="boom")
    (g,) = cs.build_groups(cs.read_status_rows(tmp_path))
    assert g["active"] is False and g["text"] == cs.DONE_TEXT
    assert g["steps"][0]["state"] == "error" and g["error"] == "boom"


def test_stale_group_without_turn_end_is_not_active():
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    rows = [{"turn_id": "t", "kind": "turn_start", "at": old}]
    (g,) = cs.build_groups(rows)
    assert g["active"] is False


def test_read_status_rows_skips_garbage(tmp_path):
    p = cs.status_path(tmp_path)
    p.write_text('not json\n{"kind":"turn_start"}\n{"turn_id":"t","kind":"turn_start"}\n')
    assert [r["turn_id"] for r in cs.read_status_rows(tmp_path)] == ["t"]


# ─── merge + chat source ────────────────────────────────────────────


def test_merge_places_group_between_request_and_reply():
    msgs = [
        {"from": "jason", "at": "2026-11-06T10:00:00-0500", "text": "hi"},
        {"from": "alice", "at": "2026-11-06T10:00:30-0500", "text": "done"},
    ]
    group = {"variant": "working-group", "at": "2026-11-06T10:00:02-05:00"}
    merged = cs.merge_thread(msgs, [group])
    assert [m.get("variant") or m["from"] for m in merged] == ["jason", "working-group", "alice"]


def test_chat_render_without_status_is_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chat(cfg, {"from": "jason", "at": cs.now_iso(), "text": "hi"})
    assert [m["from"] for m in _thread(cfg)] == ["jason"]


def test_simulated_turn_with_three_tool_calls_end_to_end(tmp_path):
    """Handler -> sidecar -> chat A2UI source: one group, 3 steps, final reply."""
    cfg = _cfg(tmp_path)
    t0 = datetime.now().astimezone().replace(microsecond=0) - timedelta(seconds=10)
    _write_chat(cfg, {"from": "jason", "at": t0.isoformat(), "text": "ship it"})

    writer = cs.ChatStatusWriter(cfg.status_dir, "turnA")
    handler = ChatStatusHandler(writer)

    async def run_turn():
        writer.turn_start()
        calls = [
            ("Edit", {"file_path": "/repo/a.py"}, "u1"),
            ("Bash", {"command": "git push origin fix"}, "u2"),
            ("mcp__alice__send_message", {"recipient": "signal"}, "u3"),
        ]
        for name, inp, uid in calls:
            await handler.on_tool_use(name, inp, uid)
            await handler.on_tool_result(uid, "ok", False)

    asyncio.run(run_turn())

    # Mid-turn: group is active, no reply yet.
    thread = _thread(cfg)
    assert [m["from"] for m in thread] == ["jason", cs.ROLE]
    assert thread[1]["active"] is True and len(thread[1]["steps"]) == 3

    # Turn ends and Alice's reply lands.
    writer.turn_end()
    _write_chat(
        cfg,
        {"from": "alice", "at": (t0 + timedelta(seconds=60)).isoformat(), "text": "shipped"},
    )
    thread = _thread(cfg)
    assert [m.get("variant") or m["from"] for m in thread] == ["jason", "working-group", "alice"]
    group = thread[1]
    assert group["active"] is False and group["step_count"] == 3
    assert [s["label"] for s in group["steps"]] == [
        "✏️ Edited /repo/a.py",
        "🚀 git push origin fix",
        "✉️ Sent message to signal",
    ]
    assert all(s["state"] == "done" for s in group["steps"])


def test_status_rows_are_not_written_to_chat_log(tmp_path):
    cfg = _cfg(tmp_path)
    cs.ChatStatusWriter(cfg.status_dir, "t").turn_start()
    assert not cfg.chat_log.exists()


# ─── dispatch gating ────────────────────────────────────────────────


def test_is_chat_surface_only_for_status_chat_type():
    chat = "---\npriority: action\ntype: status-chat\npage: chat\n---\n\n# Jason sent...\n"
    reply = "---\ntype: status-reply\n---\n\nbody\n"
    body_mention = "---\ntype: note\n---\n\ntype: status-chat\n"
    assert _is_chat_surface(chat)
    assert not _is_chat_surface(reply)
    assert not _is_chat_surface(body_mention)
    assert not _is_chat_surface("no frontmatter")


# ─── handle_surface wiring (#688) ───────────────────────────────────


class _StubRunner:
    """Borrows the real begin/end methods without TurnRunner's heavy ctor."""

    def __init__(self, status_dir):
        from alice_speaking.turn_runner import TurnRunner

        self._chat_status_dir = status_dir
        self.chat_status = None
        self.begin_chat_status = TurnRunner.begin_chat_status.__get__(self)
        self.end_chat_status = TurnRunner.end_chat_status.__get__(self)


def _surface_ctx(status_dir, seen, fail=False):
    from types import SimpleNamespace

    class _Events:
        def emit(self, *a, **k):
            pass

    async def run_turn(prompt, turn_id, outbound_recipient):
        seen.append((turn_id, bool(ctx.turn_runner.chat_status)))
        if fail:
            raise RuntimeError("boom")

    ctx = SimpleNamespace(
        events=_Events(),
        _current_turn_kind=None,
        turn_runner=_StubRunner(status_dir),
        _run_turn=run_turn,
        _surface_watcher=SimpleNamespace(archive_unresolved=lambda p: None),
    )
    return ctx


def _drop_surface(tmp_path, kind):
    p = tmp_path / f"{kind}.md"
    p.write_text(f"---\ntype: {kind}\n---\n\nhello\n")
    return p


@pytest.mark.parametrize("fail", [False, True])
def test_handle_surface_brackets_chat_turn_with_status_rows(tmp_path, fail):
    from types import SimpleNamespace

    from alice_speaking._dispatch import handle_surface

    seen: list = []
    ctx = _surface_ctx(tmp_path / "status", seen, fail=fail)
    asyncio.run(handle_surface(ctx, SimpleNamespace(path=_drop_surface(tmp_path, "status-chat"))))

    (turn_id, writer_live), = seen
    assert writer_live, "writer must be active while the turn runs"
    assert ctx.turn_runner.chat_status is None, "writer cleared after the turn"
    rows = _rows(tmp_path / "status")
    assert [r["kind"] for r in rows] == ["turn_start", "turn_end"]
    assert {r["turn_id"] for r in rows} == {turn_id}
    assert bool(rows[-1].get("error")) is fail


def test_handle_surface_skips_status_for_non_chat_surface(tmp_path):
    from types import SimpleNamespace

    from alice_speaking._dispatch import handle_surface

    seen: list = []
    ctx = _surface_ctx(tmp_path / "status", seen)
    asyncio.run(handle_surface(ctx, SimpleNamespace(path=_drop_surface(tmp_path, "note"))))

    assert seen and seen[0][1] is False
    assert not cs.status_path(tmp_path / "status").exists()


def test_handle_surface_emits_read_row_and_groups_ignore_it(tmp_path):
    from types import SimpleNamespace

    from alice_speaking._dispatch import handle_surface

    p = tmp_path / "c.md"
    p.write_text("---\ntype: status-chat\nmessage_at: 2026-11-06T13:00:00-05:00\n---\n\nhi\n")
    ctx = _surface_ctx(tmp_path / "status", [])
    asyncio.run(handle_surface(ctx, SimpleNamespace(path=p)))

    rows = _rows(tmp_path / "status")
    assert [r["kind"] for r in rows] == ["turn_start", "read", "turn_end"]
    assert rows[1]["event"] == "read" and rows[1]["read_at"]
    assert cs.read_message_ats(rows) == {"2026-11-06T13:00:00-05:00"}
    assert len(cs.build_groups(rows)) == 1
