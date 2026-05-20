"""Tests for ``alice_thinking.workflow`` — the PR 1 harness module.

Covers schema round-trips, the concurrent-append flock invariant,
projection replay + atomic write, predicate evaluation, and completion
validation. All filesystem touches live under ``tmp_path``; no test
reaches the real ``~/alice-mind/``.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import threading
import time
from pathlib import Path

import pytest

from alice_thinking.workflow import (
    EventType,
    Harness,
    Projection,
    State,
    WorkflowEvent,
    WorkflowItem,
    append_event,
    build_from_events,
    event_from_dict,
    event_to_dict,
    is_fresh,
    is_legal_transition,
    load_projection,
    read_events,
    read_last_event_id,
    save_projection,
)
from alice_thinking.workflow import harness as harness_module
from alice_thinking.workflow import projection as projection_module


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 20, 13, 0, 0, tzinfo=_dt.timezone.utc)


def _open_event(item_id: str, *, priority: int = 1, opened_at: str | None = None,
                title: str = "t", item_type: str = "research") -> WorkflowEvent:
    return WorkflowEvent(
        event_id=0,
        ts="2026-05-20T13:00:00+00:00",
        event_type=EventType.OPEN,
        item_id=item_id,
        by="test",
        payload={
            "type": item_type,
            "title": title,
            "priority": priority,
            "opened_at": opened_at or "2026-05-20T13:00:00+00:00",
            "source": {"kind": "jason_direct"},
        },
    )


def _transition_event(
    item_id: str,
    from_state: State,
    to_state: State,
    *,
    forced: bool = False,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=0,
        ts="2026-05-20T13:05:00+00:00",
        event_type=(
            EventType.FORCE_TRANSITION if forced else EventType.TRANSITION
        ),
        item_id=item_id,
        from_state=from_state,
        to_state=to_state,
        by="test",
        bypassed_validation=forced,
    )


# ---------------------------------------------------------------------------
# Schema: legal-transition table
# ---------------------------------------------------------------------------


def test_legal_transitions_full_table() -> None:
    legal = {
        (State.OPENED, State.ACTIVE),
        (State.ACTIVE, State.BLOCKED),
        (State.ACTIVE, State.SHELVED),
        (State.ACTIVE, State.COMPLETED),
        (State.ACTIVE, State.ABANDONED),
        (State.BLOCKED, State.OPENED),
        (State.BLOCKED, State.ABANDONED),
        (State.SHELVED, State.OPENED),
        (State.SHELVED, State.ABANDONED),
    }
    for src in State:
        for dst in State:
            assert is_legal_transition(src, dst) is ((src, dst) in legal)


def test_terminal_states_have_no_outbound() -> None:
    for terminal in (State.COMPLETED, State.ABANDONED):
        for dst in State:
            assert not is_legal_transition(terminal, dst)


# ---------------------------------------------------------------------------
# Schema: dict round-trip
# ---------------------------------------------------------------------------


def test_workflow_event_dict_round_trip() -> None:
    original = WorkflowEvent(
        event_id=42,
        ts="2026-05-20T13:00:00+00:00",
        event_type=EventType.TRANSITION,
        item_id="tw-2026-05-20-001",
        from_state=State.OPENED,
        to_state=State.ACTIVE,
        by="speaking",
        reason="head of queue",
        evidence={"queue_head": True},
        bypassed_validation=False,
        payload={"note": "ignored"},
    )
    restored = event_from_dict(event_to_dict(original))
    assert restored == original


def test_workflow_event_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    e1 = _open_event("a")
    e2 = _transition_event("a", State.OPENED, State.ACTIVE)
    id1 = append_event(path, e1)
    id2 = append_event(path, e2)
    assert id1 == 1
    assert id2 == 2
    events = list(read_events(path))
    assert len(events) == 2
    assert events[0].event_type == EventType.OPEN
    assert events[0].event_id == 1
    assert events[1].event_type == EventType.TRANSITION
    assert events[1].event_id == 2


# ---------------------------------------------------------------------------
# events_log: concurrent append
# ---------------------------------------------------------------------------


def test_concurrent_append_monotonic_event_ids(tmp_path: Path) -> None:
    """Five threads, ten events each → 50 events with monotonic ids 1..50."""
    path = tmp_path / "events.jsonl"
    barrier = threading.Barrier(5)

    def worker(thread_idx: int) -> list[int]:
        barrier.wait()
        ids: list[int] = []
        for i in range(10):
            event = _open_event(f"t{thread_idx}-i{i}")
            ids.append(append_event(path, event))
            time.sleep(0.001)  # synthetic mid-write delay
        return ids

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(worker, t) for t in range(5)]
        all_ids: list[int] = []
        for fut in futures:
            all_ids.extend(fut.result())

    assert len(all_ids) == 50
    assert set(all_ids) == set(range(1, 51)), "must be the set {1..50}, no gaps or dupes"

    # Every line in the file must be valid JSON.
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            json.loads(line)  # raises on invalid

    # Independent recount: 50 events read back.
    events = list(read_events(path))
    assert len(events) == 50
    assert sorted(e.event_id for e in events) == list(range(1, 51))


# ---------------------------------------------------------------------------
# events_log: read_last_event_id
# ---------------------------------------------------------------------------


def test_read_last_event_id_missing(tmp_path: Path) -> None:
    assert read_last_event_id(tmp_path / "missing.jsonl") is None


def test_read_last_event_id_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.touch()
    assert read_last_event_id(path) is None


def test_read_last_event_id_populated(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    for i in range(3):
        append_event(path, _open_event(f"item-{i}"))
    assert read_last_event_id(path) == 3


# ---------------------------------------------------------------------------
# projection: replay
# ---------------------------------------------------------------------------


def test_build_from_events_happy_path() -> None:
    events = [
        _open_event("a"),
        _transition_event("a", State.OPENED, State.ACTIVE),
        _transition_event("a", State.ACTIVE, State.COMPLETED),
    ]
    # Assign sequential event_ids (the log layer does this in practice).
    for idx, ev in enumerate(events, start=1):
        ev.event_id = idx
    proj = build_from_events(events)
    assert proj.last_event_id == 3
    assert "a" in proj.items
    assert proj.items["a"].state == State.COMPLETED


def test_build_from_events_rejects_completed_to_active() -> None:
    events = [
        _open_event("a"),
        _transition_event("a", State.OPENED, State.ACTIVE),
        _transition_event("a", State.ACTIVE, State.COMPLETED),
        _transition_event("a", State.COMPLETED, State.ACTIVE),
    ]
    for idx, ev in enumerate(events, start=1):
        ev.event_id = idx
    with pytest.raises(ValueError, match="illegal transition"):
        build_from_events(events)


def test_force_transition_from_completed_to_active() -> None:
    events = [
        _open_event("a"),
        _transition_event("a", State.OPENED, State.ACTIVE),
        _transition_event("a", State.ACTIVE, State.COMPLETED),
        _transition_event("a", State.COMPLETED, State.ACTIVE, forced=True),
    ]
    for idx, ev in enumerate(events, start=1):
        ev.event_id = idx
    proj = build_from_events(events)
    assert proj.items["a"].state == State.ACTIVE
    # The forced event carries bypassed_validation=True in its serialization.
    assert events[-1].bypassed_validation is True


def test_update_event_mutates_non_state_fields() -> None:
    open_ev = _open_event("a", priority=1, title="old title")
    update_ev = WorkflowEvent(
        event_id=0,
        ts="2026-05-20T13:10:00+00:00",
        event_type=EventType.UPDATE,
        item_id="a",
        by="speaking",
        payload={"priority": 5, "title": "new title"},
    )
    open_ev.event_id = 1
    update_ev.event_id = 2
    proj = build_from_events([open_ev, update_ev])
    item = proj.items["a"]
    assert item.priority == 5
    assert item.title == "new title"
    assert item.state == State.OPENED  # untouched


def test_note_append_event_appends() -> None:
    open_ev = _open_event("a")
    note_ev = WorkflowEvent(
        event_id=0,
        ts="2026-05-20T13:11:00+00:00",
        event_type=EventType.NOTE_APPEND,
        item_id="a",
        by="speaking",
        payload={"note": "first note"},
    )
    open_ev.event_id = 1
    note_ev.event_id = 2
    proj = build_from_events([open_ev, note_ev])
    assert proj.items["a"].notes == ["first note"]


# ---------------------------------------------------------------------------
# projection: atomic write
# ---------------------------------------------------------------------------


def test_projection_save_atomic_under_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails after the tmp file is written, the existing
    projection.json is untouched (or absent, when there's no prior file)."""
    proj_path = tmp_path / "projection.json"
    # First, write a known-good projection.
    good = Projection(last_event_id=1, items={})
    save_projection(proj_path, good)
    original_bytes = proj_path.read_bytes()

    # Now sabotage os.replace and try to overwrite.
    def boom(src: str, dst: str) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(projection_module.os, "replace", boom)

    new_proj = Projection(last_event_id=2, items={})
    with pytest.raises(OSError, match="synthetic replace failure"):
        save_projection(proj_path, new_proj)

    # Final file is unchanged.
    assert proj_path.read_bytes() == original_bytes


def test_projection_save_atomic_when_no_prior_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj_path = tmp_path / "projection.json"
    assert not proj_path.exists()

    def boom(src: str, dst: str) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(projection_module.os, "replace", boom)

    with pytest.raises(OSError):
        save_projection(proj_path, Projection(last_event_id=1, items={}))

    # No final file was ever created.
    assert not proj_path.exists()


def test_projection_round_trip_on_disk(tmp_path: Path) -> None:
    proj_path = tmp_path / "projection.json"
    item = WorkflowItem(
        id="a",
        state=State.ACTIVE,
        type="research",
        title="t",
        priority=3,
        opened_at="2026-05-20T13:00:00+00:00",
        source={"kind": "jason_direct"},
        notes=["n1", "n2"],
        wakes_active=2,
    )
    original = Projection(last_event_id=7, items={"a": item})
    save_projection(proj_path, original)
    restored = load_projection(proj_path)
    assert restored.last_event_id == 7
    assert restored.items["a"].state == State.ACTIVE
    assert restored.items["a"].notes == ["n1", "n2"]
    assert restored.items["a"].wakes_active == 2


# ---------------------------------------------------------------------------
# projection: is_fresh
# ---------------------------------------------------------------------------


def test_is_fresh_true_when_matches(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    append_event(log_path, _open_event("a"))
    append_event(log_path, _open_event("b"))
    proj = Projection(last_event_id=2, items={})
    assert is_fresh(proj, log_path) is True


def test_is_fresh_both_empty(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    proj = Projection(last_event_id=None, items={})
    assert is_fresh(proj, log_path) is True


def test_is_fresh_false_when_projection_trails(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    for _ in range(3):
        append_event(log_path, _open_event("x"))
    proj = Projection(last_event_id=1, items={})
    assert is_fresh(proj, log_path) is False


# ---------------------------------------------------------------------------
# harness: select_next_active
# ---------------------------------------------------------------------------


def _populated_projection(items: list[WorkflowItem]) -> Projection:
    return Projection(
        last_event_id=len(items),
        items={i.id: i for i in items},
    )


def test_select_next_active_priority_winner(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    items = [
        WorkflowItem(
            id="low", state=State.OPENED, type="r", title="", priority=1,
            opened_at="2026-05-20T10:00:00+00:00", source={},
        ),
        WorkflowItem(
            id="high", state=State.OPENED, type="r", title="", priority=3,
            opened_at="2026-05-20T11:00:00+00:00", source={},
        ),
        WorkflowItem(
            id="mid", state=State.OPENED, type="r", title="", priority=2,
            opened_at="2026-05-20T12:00:00+00:00", source={},
        ),
    ]
    proj = _populated_projection(items)
    winner = harness.select_next_active(proj)
    assert winner is not None
    assert winner.id == "high"


def test_select_next_active_fifo_tiebreak(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    items = [
        WorkflowItem(
            id="younger", state=State.OPENED, type="r", title="", priority=2,
            opened_at="2026-05-20T12:00:00+00:00", source={},
        ),
        WorkflowItem(
            id="older", state=State.OPENED, type="r", title="", priority=2,
            opened_at="2026-05-20T10:00:00+00:00", source={},
        ),
    ]
    proj = _populated_projection(items)
    winner = harness.select_next_active(proj)
    assert winner is not None
    assert winner.id == "older"


def test_select_next_active_empty_queue(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    proj = _populated_projection([])
    assert harness.select_next_active(proj) is None


# ---------------------------------------------------------------------------
# harness: propose_active_transition
# ---------------------------------------------------------------------------


def test_propose_active_transition_returns_none_when_active_exists(
    tmp_path: Path,
) -> None:
    harness = Harness(mind_dir=tmp_path)
    items = [
        WorkflowItem(
            id="busy", state=State.ACTIVE, type="r", title="", priority=1,
            opened_at="2026-05-20T10:00:00+00:00", source={},
        ),
        WorkflowItem(
            id="waiting", state=State.OPENED, type="r", title="", priority=5,
            opened_at="2026-05-20T11:00:00+00:00", source={},
        ),
    ]
    proj = _populated_projection(items)
    assert harness.propose_active_transition(proj, _now()) is None


def test_propose_active_transition_picks_head(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    items = [
        WorkflowItem(
            id="a", state=State.OPENED, type="r", title="", priority=2,
            opened_at="2026-05-20T11:00:00+00:00", source={},
        ),
        WorkflowItem(
            id="b", state=State.OPENED, type="r", title="", priority=5,
            opened_at="2026-05-20T12:00:00+00:00", source={},
        ),
    ]
    proj = _populated_projection(items)
    proposed = harness.propose_active_transition(proj, _now())
    assert proposed is not None
    assert proposed.item_id == "b"
    assert proposed.event_type == EventType.TRANSITION
    assert proposed.from_state == State.OPENED
    assert proposed.to_state == State.ACTIVE
    assert proposed.event_id == 0  # caller assigns
    assert proposed.by == "harness:auto-select"


# ---------------------------------------------------------------------------
# harness: evaluate_predicates (time_elapsed)
# ---------------------------------------------------------------------------


def _seed_log_with_blocked_item(
    log_path: Path, item_id: str, unblock_when: dict
) -> None:
    open_ev = _open_event(item_id)
    append_event(log_path, open_ev)
    append_event(
        log_path, _transition_event(item_id, State.OPENED, State.ACTIVE)
    )
    # Apply an update to set unblock_when before flipping to blocked.
    append_event(
        log_path,
        WorkflowEvent(
            event_id=0,
            ts="2026-05-20T13:01:00+00:00",
            event_type=EventType.UPDATE,
            item_id=item_id,
            by="speaking",
            payload={"unblock_when": unblock_when},
        ),
    )
    append_event(
        log_path, _transition_event(item_id, State.ACTIVE, State.BLOCKED)
    )


def test_evaluate_predicates_time_elapsed_past(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    log_path = harness.events_log_path
    _seed_log_with_blocked_item(
        log_path,
        "blocked-1",
        {"type": "time_elapsed", "after": "2026-05-19T00:00:00+00:00"},
    )
    proposed = harness.evaluate_predicates(_now())
    assert len(proposed) == 1
    ev = proposed[0]
    assert ev.event_type == EventType.TRANSITION
    assert ev.item_id == "blocked-1"
    assert ev.from_state == State.BLOCKED
    assert ev.to_state == State.OPENED
    assert ev.by == "harness:predicate"


def test_evaluate_predicates_time_elapsed_future(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    log_path = harness.events_log_path
    _seed_log_with_blocked_item(
        log_path,
        "blocked-2",
        {"type": "time_elapsed", "after": "2099-01-01T00:00:00+00:00"},
    )
    proposed = harness.evaluate_predicates(_now())
    assert proposed == []


# ---------------------------------------------------------------------------
# harness: evaluate_predicates (github_pr_merged via monkeypatched subprocess)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_github_pr_merged_true(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompletedProcess(0, stdout=json.dumps({"state": "MERGED"}))

    monkeypatch.setattr(harness_module.subprocess, "run", fake_run)
    assert (
        harness_module.evaluate_predicate(
            {"type": "github_pr_merged", "pr": "owner/repo#7"}, _now()
        )
        is True
    )


def test_github_pr_merged_false_on_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompletedProcess(1, stdout="")

    monkeypatch.setattr(harness_module.subprocess, "run", fake_run)
    assert (
        harness_module.evaluate_predicate(
            {"type": "github_pr_merged", "pr": "owner/repo#7"}, _now()
        )
        is False
    )


def test_github_pr_merged_false_on_missing_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(harness_module.subprocess, "run", fake_run)
    assert (
        harness_module.evaluate_predicate(
            {"type": "github_pr_merged", "pr": "owner/repo#7"}, _now()
        )
        is False
    )


# ---------------------------------------------------------------------------
# harness: validate_completion (research_note_resolves)
# ---------------------------------------------------------------------------


def test_validate_completion_research_note_resolves_true(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    research_dir = tmp_path / "cortex-memory" / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "2026-05-20-foo.md").write_text(
        "---\n"
        "title: foo\n"
        "resolves_workflow_item: ITEM-1\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )
    item = WorkflowItem(
        id="ITEM-1",
        state=State.ACTIVE,
        type="research",
        title="",
        priority=1,
        opened_at="",
        source={},
        completion_criterion={"type": "research_note_resolves"},
    )
    assert harness.validate_completion(item) is True


def test_validate_completion_research_note_resolves_false_without_frontmatter(
    tmp_path: Path,
) -> None:
    harness = Harness(mind_dir=tmp_path)
    research_dir = tmp_path / "cortex-memory" / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "no-frontmatter.md").write_text(
        "just some prose, no yaml header at all\n",
        encoding="utf-8",
    )
    item = WorkflowItem(
        id="ITEM-1",
        state=State.ACTIVE,
        type="research",
        title="",
        priority=1,
        opened_at="",
        source={},
        completion_criterion={"type": "research_note_resolves"},
    )
    assert harness.validate_completion(item) is False


def test_validate_completion_research_note_resolves_wrong_id(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    research_dir = tmp_path / "cortex-memory" / "research"
    research_dir.mkdir(parents=True)
    (research_dir / "other.md").write_text(
        "---\nresolves_workflow_item: OTHER-ID\n---\nbody\n",
        encoding="utf-8",
    )
    item = WorkflowItem(
        id="ITEM-1",
        state=State.ACTIVE,
        type="research",
        title="",
        priority=1,
        opened_at="",
        source={},
        completion_criterion={"type": "research_note_resolves"},
    )
    assert harness.validate_completion(item) is False


def test_validate_completion_no_criterion(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    item = WorkflowItem(
        id="ITEM-1",
        state=State.ACTIVE,
        type="research",
        title="",
        priority=1,
        opened_at="",
        source={},
        completion_criterion=None,
    )
    assert harness.validate_completion(item) is False


# ---------------------------------------------------------------------------
# harness: refresh rebuilds when stale
# ---------------------------------------------------------------------------


def test_harness_refresh_rebuilds_when_stale(tmp_path: Path) -> None:
    harness = Harness(mind_dir=tmp_path)
    # Append a couple of events directly to the log; no projection exists.
    append_event(harness.events_log_path, _open_event("a"))
    append_event(
        harness.events_log_path,
        _transition_event("a", State.OPENED, State.ACTIVE),
    )
    proj = harness.refresh()
    assert proj.last_event_id == 2
    assert proj.items["a"].state == State.ACTIVE
    # Projection.json now exists on disk.
    assert harness.projection_path.is_file()
    # Subsequent refresh returns the cached projection (no rebuild needed).
    proj2 = harness.refresh()
    assert proj2.last_event_id == 2
