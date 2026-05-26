"""Unit tests for :mod:`alice_forge.task_store`.

Covers the SM v2 task store contract:

* ``create`` allocates ``task-NNNN`` zero-padded, writes task.yaml +
  transitions.jsonl, appends to index.jsonl.
* ``update`` validates SM v2 transitions, rewrites task.yaml
  atomically, appends transitions.jsonl, refreshes index.jsonl.
* Invalid transitions are rejected.
* ``list`` honours status / tag / open_only filters.
* ``find_by_tag`` returns the most-recently-updated open match.
* ``close`` is sugar over ``update --status done``.
* Legacy ``review`` state is tolerated on read.
* Sidecar-field requirements (``unblocked_by`` for blocked,
  ``validation_evidence`` for validating → done, ``merge_ref`` for
  the building → done self-merge shortcut) are enforced.
"""

from __future__ import annotations

import pathlib

import pytest

from alice_forge.task_store import (
    InvalidState,
    InvalidTransition,
    TaskNotFound,
    TaskStore,
    TaskStoreError,
    default_root,
)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks")


# ---------------------------------------------------------------------------
# create


def test_create_allocates_task_0001_in_empty_store(store: TaskStore) -> None:
    record = store.create(title="First task", tags=["bootstrap"])
    assert record.id == "task-0001"
    assert record.status == "draft"
    assert record.tags == ["bootstrap"]
    # task.yaml exists
    yaml_path = store.root / "task-0001" / "task.yaml"
    assert yaml_path.is_file()
    # transitions.jsonl has exactly one row (null → draft)
    trans = store.transitions("task-0001")
    assert len(trans) == 1
    assert trans[0]["from"] is None
    assert trans[0]["to"] == "draft"


def test_create_zero_pads_id_and_increments(store: TaskStore) -> None:
    store.create(title="a")
    store.create(title="b")
    record = store.create(title="c")
    assert record.id == "task-0003"
    # index has three lines
    entries = list(store.iter_index())
    assert [e["id"] for e in entries] == ["task-0001", "task-0002", "task-0003"]


def test_create_with_invalid_actor_raises(store: TaskStore) -> None:
    with pytest.raises(TaskStoreError):
        store.create(title="x", actor="bogus")


# ---------------------------------------------------------------------------
# update — valid transitions


def test_update_draft_to_selected(store: TaskStore) -> None:
    record = store.create(title="x")
    updated = store.update(record.id, status="selected", reason="auto-select")
    assert updated.status == "selected"
    # transitions.jsonl now has two rows
    trans = store.transitions(record.id)
    assert len(trans) == 2
    assert trans[1]["from"] == "draft"
    assert trans[1]["to"] == "selected"
    # index reflects new status
    entry = next(e for e in store.iter_index() if e["id"] == record.id)
    assert entry["status"] == "selected"


def test_update_cascade_to_building(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    assert store.load(record.id).status == "building"


def test_building_to_done_requires_merge_ref(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    with pytest.raises(InvalidTransition):
        store.update(record.id, status="done")


def test_building_to_done_with_merge_ref_succeeds(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    updated = store.update(
        record.id,
        status="done",
        merge_ref="https://github.com/jcronq/alice/pull/123",
    )
    assert updated.status == "done"
    assert updated.merge_ref == "https://github.com/jcronq/alice/pull/123"


# ---------------------------------------------------------------------------
# update — invalid transitions


def test_draft_to_building_is_invalid(store: TaskStore) -> None:
    record = store.create(title="x")
    with pytest.raises(InvalidTransition):
        store.update(record.id, status="building")


def test_done_is_terminal(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    store.update(
        record.id,
        status="done",
        merge_ref="https://github.com/jcronq/alice/pull/1",
    )
    with pytest.raises(InvalidTransition):
        store.update(record.id, status="building")


def test_unknown_status_rejected(store: TaskStore) -> None:
    record = store.create(title="x")
    with pytest.raises(InvalidState):
        store.update(record.id, status="bogus-state")


# ---------------------------------------------------------------------------
# update — sidecar field requirements


def test_blocked_requires_unblocked_by(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    with pytest.raises(TaskStoreError):
        store.update(record.id, status="blocked", reason="hit a wall")
    # With unblocked_by, succeeds
    store.update(
        record.id,
        status="blocked",
        reason="hit a wall",
        unblocked_by="Jason to decide on schema",
    )


def test_validating_to_done_requires_validation_evidence(store: TaskStore) -> None:
    record = store.create(title="x", artifact_type="code")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    store.update(record.id, status="reviewing")
    store.update(
        record.id,
        status="validating",
        merge_ref="https://github.com/jcronq/alice/pull/1",
    )
    with pytest.raises(TaskStoreError):
        store.update(record.id, status="done")
    store.update(
        record.id,
        status="done",
        validation_evidence="CI green on merge commit deadbeef",
    )


# ---------------------------------------------------------------------------
# list


def test_list_filters_by_status(store: TaskStore) -> None:
    a = store.create(title="a")
    b = store.create(title="b")
    store.update(a.id, status="selected")
    selected = store.list(status="selected")
    drafts = store.list(status="draft")
    assert [e["id"] for e in selected] == [a.id]
    assert [e["id"] for e in drafts] == [b.id]


def test_list_open_only_excludes_done_and_rejected(store: TaskStore) -> None:
    a = store.create(title="a")
    b = store.create(title="b")
    store.update(a.id, status="selected")
    store.update(a.id, status="building")
    store.update(
        a.id,
        status="done",
        merge_ref="https://github.com/jcronq/alice/pull/1",
    )
    # b is still draft, a is done
    open_entries = store.list(open_only=True)
    assert [e["id"] for e in open_entries] == [b.id]


def test_list_filters_by_tag(store: TaskStore) -> None:
    a = store.create(title="a", tags=["auto-fix", "jcronq/alice"])
    store.create(title="b", tags=["other"])
    hits = store.list(tag="auto-fix")
    assert [e["id"] for e in hits] == [a.id]


# ---------------------------------------------------------------------------
# find_by_tag


def test_find_by_tag_returns_open_entry(store: TaskStore) -> None:
    a = store.create(title="a", tags=["worker:bg-1234"])
    entry = store.find_by_tag("worker:bg-1234")
    assert entry is not None
    assert entry["id"] == a.id


def test_find_by_tag_skips_terminal_entries(store: TaskStore) -> None:
    a = store.create(title="a", tags=["worker:bg-1234"])
    store.update(a.id, status="selected")
    store.update(a.id, status="building")
    store.update(
        a.id,
        status="done",
        merge_ref="https://github.com/jcronq/alice/pull/1",
    )
    # Terminal — find_by_tag should skip
    assert store.find_by_tag("worker:bg-1234") is None


# ---------------------------------------------------------------------------
# close


def test_close_is_sugar_for_done(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    store.update(record.id, status="building")
    updated = store.close(
        record.id,
        merge_ref="https://github.com/jcronq/alice/pull/1",
        reason="self-merge",
    )
    assert updated.status == "done"


# ---------------------------------------------------------------------------
# view + missing


def test_load_raises_for_missing_task(store: TaskStore) -> None:
    with pytest.raises(TaskNotFound):
        store.load("task-9999")


# ---------------------------------------------------------------------------
# Legacy ``review`` state acceptance


def test_legacy_review_state_can_be_advanced(store: TaskStore) -> None:
    """task-0001 has 'review' in its transitions — we shouldn't lock that
    out when an older record is loaded."""
    record = store.create(title="legacy")
    # Hand-write a task.yaml in the legacy 'review' state to simulate
    # an old record. Bypassing the CLI is fine for this fixture
    # because the test exercises load+update, not create.
    yaml_path = store.root / record.id / "task.yaml"
    text = yaml_path.read_text().replace("status: draft", "status: review")
    yaml_path.write_text(text)
    # The CLI rejects writing 'review' status, but advancing OUT of it
    # to 'reviewing' is permitted by the legacy edge.
    updated = store.update(record.id, status="reviewing", reason="legacy migration")
    assert updated.status == "reviewing"


# ---------------------------------------------------------------------------
# Atomicity smoke test


def test_atomic_write_leaves_no_tempfile(store: TaskStore) -> None:
    record = store.create(title="x")
    store.update(record.id, status="selected")
    # No ``.task.yaml.*.tmp`` siblings should be left behind.
    leftovers = list((store.root / record.id).glob(".task.yaml.*"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# default_root resolution


def test_default_root_honours_tasks_dir(monkeypatch, tmp_path) -> None:
    target = tmp_path / "override"
    monkeypatch.setenv("TASKS_DIR", str(target))
    assert default_root() == target


def test_default_root_honours_alice_mind_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TASKS_DIR", raising=False)
    monkeypatch.setenv("ALICE_MIND_DIR", str(tmp_path / "mind"))
    assert default_root() == tmp_path / "mind" / "inner" / "tasks"
