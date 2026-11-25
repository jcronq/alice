"""Tests for the Phase 2 Agent UI backend port.

Covers:
* ``todos.build_todo`` backfill — title/excerpt/status/messageCount
  off a real on-disk ask record shape.
* ``todos.infer_status`` — resolved → ``done``; last-from-alice →
  ``in_progress``; otherwise ``waiting_on_you``.
* Snapshot event carries ``todos`` / ``workStatuses`` / ``agentStatus``
  alongside the existing ``sections`` block.
* Each POST route happy path + a bad-input rejection:
  - ``POST /api/todos/<id>/effort`` writes the ledger + fires
    ``todo.upsert`` with the refreshed Todo.
  - ``POST /api/todos/<id>/messages`` appends to the thread, fires
    ``thread.message``, and dedups a repeated ``messageId``.
  - ``POST /api/todos/<id>/resolve`` marks the ask done + fires
    ``todo.removed``.
  - ``POST /api/todos/<id>/reopen`` clears the resolve + fires
    ``todo.upsert``.
"""

from __future__ import annotations

import json
import queue
import threading

import pytest

from status_site import todo_events, todos
from status_site._sse import SectionBroadcaster
from status_site.server import (
    Config,
    build_todo_snapshot_fields,
    handle_todo_effort,
    handle_todo_messages,
    handle_todo_reopen,
    handle_todo_resolve,
    now_iso,
    read_threads,
)
from status_site.sources.common import asks_path


KEY = "p2-todo-test-key"


# ─── fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path):
    """Config with a broadcaster that captures published events.

    The fixture pins a SectionBroadcaster onto the Config but does NOT
    start its background tick — tests call ``publish_event`` / read
    the resulting queue directly so the assertions aren't timing-
    sensitive.
    """
    status = tmp_path / "status"
    status.mkdir()
    (status / "threads").mkdir()
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=KEY,
    )
    bc = SectionBroadcaster(renderers={}, tick_seconds=1.0)
    cfg.broadcaster = bc
    return cfg


@pytest.fixture
def subscribe(cfg):
    """Factory: subscribe to the broadcaster and return (sub, drain)."""

    def make():
        sub = cfg.broadcaster.subscribe()

        def drain(*, max_events: int = 10) -> list[dict]:
            out: list[dict] = []
            while True:
                try:
                    out.append(sub.q.get_nowait())
                except queue.Empty:
                    return out
                if len(out) >= max_events:
                    return out

        return sub, drain

    return make


def _write_ask(cfg: Config, record: dict) -> None:
    """Append one ask record to ``asks.jsonl`` (test helper)."""
    p = asks_path(cfg.status_dir)
    with open(p, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


# ─── schema + backfill ──────────────────────────────────────────────


def test_title_and_excerpt_splits_first_line():
    title, excerpt = todos.title_and_excerpt(
        "decide SM v2 cutover date\n\nbackground — thinking landed the design"
    )
    assert title == "decide SM v2 cutover date"
    assert excerpt == "background — thinking landed the design"


def test_title_and_excerpt_empty():
    assert todos.title_and_excerpt("") == ("", "")
    assert todos.title_and_excerpt("\n\n") == ("", "")


def test_infer_status_resolved_is_done():
    rec = {"resolved": True}
    assert todos.infer_status(rec) == "done"
    # Thread content is ignored when resolved.
    assert (
        todos.infer_status(
            rec, thread=[{"from": "alice", "text": "k", "at": ""}]
        )
        == "done"
    )


def test_infer_status_default_waiting_on_you():
    assert todos.infer_status({"resolved": False}) == "waiting_on_you"
    assert todos.infer_status({}) == "waiting_on_you"


def test_infer_status_alice_last_is_in_progress():
    rec = {"resolved": False}
    thread = [
        {"from": "jason", "text": "ping", "at": "2026-11-25T10:00:00+00:00"},
        {"from": "alice", "text": "on it", "at": "2026-11-25T10:01:00+00:00"},
    ]
    assert todos.infer_status(rec, thread=thread) == "in_progress"


def test_build_todo_backfills_from_ask_record():
    rec = {
        "id": "abc12345",
        "text": "decide SM v2 cutover date\n\nthinking landed the design",
        "created": "2026-11-20T00:00:00+00:00",
        "resolved": False,
    }
    todo = todos.build_todo(rec)
    assert todo["id"] == "abc12345"
    assert todo["title"] == "decide SM v2 cutover date"
    assert todo["detailsExcerpt"] == "thinking landed the design"
    assert todo["status"] == "waiting_on_you"
    assert todo["createdAt"] == rec["created"]
    assert todo["updatedAt"] == rec["created"]
    assert todo["unread"] is False
    assert todo["messageCount"] == 0
    assert todo["lastMessage"] is None


def test_build_todo_resolved_updates_updatedAt_from_resolved_at():
    rec = {
        "id": "x",
        "text": "already done",
        "created": "2026-11-01T00:00:00+00:00",
        "resolved": True,
        "resolved_at": "2026-11-02T00:00:00+00:00",
    }
    todo = todos.build_todo(rec)
    assert todo["status"] == "done"
    assert todo["updatedAt"] == rec["resolved_at"]


def test_build_todo_translates_role_from_alice_to_claude():
    rec = {"id": "x", "text": "t", "created": "2026-11-20T00:00:00+00:00"}
    thread = [{"from": "alice", "text": "on it", "at": "2026-11-20T00:01:00+00:00"}]
    todo = todos.build_todo(rec, thread=thread)
    assert todo["lastMessage"] is not None
    assert todo["lastMessage"]["role"] == "claude"


def test_effort_level_word_and_number():
    assert todos.effort_level("spike") == 1
    assert todos.effort_level("Prototype") == 2
    assert todos.effort_level(3) == 3
    assert todos.effort_level("4") == 4
    assert todos.effort_level("unknown") is None
    assert todos.effort_level(None) is None
    assert todos.effort_level(True) is None  # bool passthrough blocked


# ─── snapshot shape ─────────────────────────────────────────────────


def test_build_todo_snapshot_fields_empty_site(cfg):
    snap = build_todo_snapshot_fields(cfg)
    assert snap["todos"] == []
    assert snap["workStatuses"] == []
    assert snap["agentStatus"] == "idle"


def test_snapshot_includes_todos_from_asks(cfg):
    _write_ask(
        cfg,
        {
            "id": "ab",
            "text": "review the new persona rules",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    snap = build_todo_snapshot_fields(cfg)
    assert len(snap["todos"]) == 1
    todo = snap["todos"][0]
    assert todo["id"] == "ab"
    assert todo["title"] == "review the new persona rules"
    assert todo["status"] == "waiting_on_you"


def test_snapshot_merges_into_broadcaster_snapshot(cfg):
    """SectionBroadcaster.snapshot() merges augmentor output alongside sections."""
    _write_ask(
        cfg,
        {
            "id": "merge",
            "text": "hello",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    bc = SectionBroadcaster(
        renderers={"demo": lambda: {"markdown": "hi"}},
        tick_seconds=60.0,
        snapshot_augmentor=lambda: build_todo_snapshot_fields(cfg),
    )
    frame = bc.snapshot()
    assert frame["type"] == "snapshot"
    assert "sections" in frame
    assert frame["sections"]["demo"] == {"markdown": "hi"}
    # Agent UI fields merged in.
    assert frame["agentStatus"] == "idle"
    assert frame["workStatuses"] == []
    ids = [t["id"] for t in frame["todos"]]
    assert ids == ["merge"]


def test_snapshot_augmentor_cannot_overwrite_core_keys(cfg):
    """A buggy augmentor can't crash the broadcaster or clobber ``sections``."""
    bc = SectionBroadcaster(
        renderers={"demo": lambda: {"markdown": "hi"}},
        tick_seconds=60.0,
        snapshot_augmentor=lambda: {
            "type": "nope",
            "sections": {"evil": "x"},
            "todos": [{"id": "t"}],
        },
    )
    frame = bc.snapshot()
    assert frame["type"] == "snapshot"
    assert frame["sections"]["demo"] == {"markdown": "hi"}
    # Extra non-core key still merged.
    assert frame["todos"] == [{"id": "t"}]


# ─── POST /api/todos/<id>/effort ────────────────────────────────────


def test_post_effort_sets_level_and_fires_upsert(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "e1",
            "text": "ship the thing",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    sub, drain = subscribe()
    resp = handle_todo_effort(cfg, "e1", {"key": KEY, "effort": 3})
    assert resp["ok"] is True
    assert resp["todo"]["effort"] == 3
    assert resp["todo"]["effortBy"] == "user"
    evs = drain()
    assert len(evs) == 1
    assert evs[0]["type"] == "todo.upsert"
    assert evs[0]["todo"]["id"] == "e1"
    assert evs[0]["todo"]["effort"] == 3


def test_post_effort_null_clears(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "e2",
            "text": "ship",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    # First set a level, then clear it.
    handle_todo_effort(cfg, "e2", {"key": KEY, "effort": 2})
    sub, drain = subscribe()
    resp = handle_todo_effort(cfg, "e2", {"key": KEY, "effort": None})
    assert resp["ok"] is True
    assert "effort" not in resp["todo"]
    evs = drain()
    assert evs[0]["type"] == "todo.upsert"
    assert "effort" not in evs[0]["todo"]


def test_post_effort_bad_level(cfg):
    _write_ask(
        cfg,
        {
            "id": "bad",
            "text": "x",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_effort(cfg, "bad", {"key": KEY, "effort": 7})
    assert ei.value.code == 400


def test_post_effort_bad_key(cfg):
    _write_ask(
        cfg,
        {"id": "k", "text": "x", "created": "2026-11-20T00:00:00+00:00"},
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_effort(cfg, "k", {"key": "wrong", "effort": 2})
    assert ei.value.code == 403


def test_post_effort_unknown_todo(cfg):
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_effort(cfg, "nope", {"key": KEY, "effort": 2})
    assert ei.value.code == 404


# ─── POST /api/todos/<id>/messages ──────────────────────────────────


def test_post_messages_appends_and_fires_thread_message(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "m1",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    sub, drain = subscribe()
    resp = handle_todo_messages(
        cfg,
        "m1",
        {"key": KEY, "text": "actually yes", "messageId": "msg-aaa"},
    )
    assert resp["ok"] is True
    assert resp["messageId"] == "msg-aaa"

    # Thread file has the message.
    thread = read_threads(cfg).get("m1") or []
    assert len(thread) == 1
    assert thread[0]["text"] == "actually yes"

    # Event emitted on the subscriber's mailbox.
    evs = drain()
    types = [e["type"] for e in evs]
    assert "thread.message" in types
    tm = next(e for e in evs if e["type"] == "thread.message")
    assert tm["todoId"] == "m1"
    assert tm["message"]["role"] == "user"
    assert tm["message"]["text"] == "actually yes"


def test_post_messages_idempotent_on_messageId(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "dup",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    handle_todo_messages(
        cfg, "dup", {"key": KEY, "text": "hi", "messageId": "same"}
    )
    sub, drain = subscribe()
    resp = handle_todo_messages(
        cfg, "dup", {"key": KEY, "text": "hi (retry)", "messageId": "same"}
    )
    assert resp.get("deduped") is True
    thread = read_threads(cfg).get("dup") or []
    assert len(thread) == 1  # not double-written
    # No event emitted on dedup.
    assert drain() == []


def test_post_messages_empty_text_400(cfg):
    _write_ask(
        cfg,
        {
            "id": "empty",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_messages(cfg, "empty", {"key": KEY, "text": "   "})
    assert ei.value.code == 400


def test_post_messages_bad_images_type(cfg):
    _write_ask(
        cfg,
        {"id": "img", "text": "x", "created": "2026-11-20T00:00:00+00:00"},
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_messages(
            cfg, "img", {"key": KEY, "text": "hi", "images": "not a list"}
        )
    assert ei.value.code == 400


# ─── POST /api/todos/<id>/resolve ───────────────────────────────────


def test_post_resolve_marks_and_fires_removed(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "r1",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    sub, drain = subscribe()
    resp = handle_todo_resolve(
        cfg, "r1", {"key": KEY, "resolution": "resolved"}
    )
    assert resp["ok"] is True
    # On-disk ask is now resolved.
    rows = [
        json.loads(line)
        for line in asks_path(cfg.status_dir).read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["resolved"] is True
    assert "resolved_at" in rows[0]
    assert rows[0]["resolution"] == "resolved"
    evs = drain()
    assert evs == [{"type": "todo.removed", "id": "r1"}]


def test_post_resolve_dismiss_also_fires_removed(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "d1",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    sub, drain = subscribe()
    handle_todo_resolve(cfg, "d1", {"key": KEY, "resolution": "dismissed"})
    evs = drain()
    assert evs == [{"type": "todo.removed", "id": "d1"}]


def test_post_resolve_bad_resolution(cfg):
    _write_ask(
        cfg,
        {"id": "b1", "text": "x", "created": "2026-11-20T00:00:00+00:00"},
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_resolve(cfg, "b1", {"key": KEY, "resolution": "maybe"})
    assert ei.value.code == 400


def test_post_resolve_unknown_todo(cfg):
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_resolve(
            cfg, "nope", {"key": KEY, "resolution": "resolved"}
        )
    assert ei.value.code == 404


# ─── POST /api/todos/<id>/reopen ────────────────────────────────────


def test_post_reopen_clears_resolve_and_fires_upsert(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "ro",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": True,
            "resolved_at": "2026-11-21T00:00:00+00:00",
            "resolution": "resolved",
        },
    )
    sub, drain = subscribe()
    resp = handle_todo_reopen(cfg, "ro", {"key": KEY})
    assert resp["ok"] is True
    assert resp["todo"]["status"] == "waiting_on_you"
    rows = [
        json.loads(line)
        for line in asks_path(cfg.status_dir).read_text().splitlines()
        if line.strip()
    ]
    assert rows[0]["resolved"] is False
    assert "resolved_at" not in rows[0]
    evs = drain()
    assert len(evs) == 1
    assert evs[0]["type"] == "todo.upsert"
    assert evs[0]["todo"]["id"] == "ro"


def test_post_reopen_idempotent_on_open_record(cfg, subscribe):
    _write_ask(
        cfg,
        {
            "id": "opn",
            "text": "ping",
            "created": "2026-11-20T00:00:00+00:00",
            "resolved": False,
        },
    )
    sub, drain = subscribe()
    resp = handle_todo_reopen(cfg, "opn", {"key": KEY})
    assert resp["ok"] is True
    # Still fires a ``todo.upsert`` so a UI that replayed this through
    # the Undo button gets a fresh Todo frame to draw against.
    evs = drain()
    assert len(evs) == 1
    assert evs[0]["type"] == "todo.upsert"


def test_post_reopen_bad_key(cfg):
    _write_ask(
        cfg,
        {"id": "k", "text": "x", "created": "2026-11-20T00:00:00+00:00"},
    )
    from status_site.server import ReplyError

    with pytest.raises(ReplyError) as ei:
        handle_todo_reopen(cfg, "k", {"key": "wrong"})
    assert ei.value.code == 403


# ─── event builders — exact shapes ──────────────────────────────────


def test_event_builders_mirror_ts_shapes():
    """One sanity assertion per event helper so a rename breaks here."""
    assert todo_events.todo_upsert_event({"id": "a"})["type"] == "todo.upsert"
    assert todo_events.todo_upsert_event({"id": "a"})["todo"] == {"id": "a"}
    assert todo_events.todo_removed_event("b")["type"] == "todo.removed"
    assert todo_events.todo_removed_event("b")["id"] == "b"
    tm = todo_events.thread_message_event("c", {"role": "user", "text": "hi", "at": ""})
    assert tm["type"] == "thread.message"
    assert tm["todoId"] == "c"
    assert tm["thread"] == {"kind": "todo", "id": "c"}
    assert tm["message"]["role"] == "user"
    td = todo_events.thread_delta_event("d", "chunk", "sid", index=4)
    assert td["type"] == "thread.delta"
    assert td["streamId"] == "sid"
    assert td["messageId"] == "sid"
    assert td["index"] == 4
    assert td["text"] == "chunk"
    assert td["delta"] == "chunk"
    wu = todo_events.work_update_event([{"text": "running"}])
    assert wu["type"] == "work.update"
    assert wu["items"] == [{"text": "running"}]
    assert wu["workStatuses"] == [{"text": "running"}]
    ag = todo_events.agent_status_event("working")
    assert ag["type"] == "agent.status"
    assert ag["agentStatus"] == "working"
    assert ag["state"] == "working"
    snap = todo_events.snapshot_fields([{"id": "a"}])
    assert snap["todos"] == [{"id": "a"}]
    assert snap["workStatuses"] == []
    assert snap["agentStatus"] == "idle"


# ─── broadcaster integration — publish_event fans to subs ───────────


def test_publish_event_delivers_to_subscriber():
    bc = SectionBroadcaster(renderers={}, tick_seconds=1.0)
    sub = bc.subscribe()
    bc.publish_event({"type": "todo.upsert", "todo": {"id": "x"}})
    ev = sub.q.get(timeout=1.0)
    assert ev["type"] == "todo.upsert"


def test_publish_event_rejects_bad_payload():
    bc = SectionBroadcaster(renderers={}, tick_seconds=1.0)
    sub = bc.subscribe()
    bc.publish_event({})  # missing type
    bc.publish_event("not a dict")  # type: ignore[arg-type]
    with pytest.raises(queue.Empty):
        sub.q.get_nowait()
