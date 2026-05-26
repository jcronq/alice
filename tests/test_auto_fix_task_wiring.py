"""Integration tests for the issue #375 SM v2 task wiring in
:mod:`alice_speaking.auto_fix`.

Exercises the full dispatcher lifecycle: intake creates a task that
cascades to ``building``; completion transitions ``building → done``
(with PR URL) or ``→ blocked`` (on error / no PR URL).

The wiring is plain function calls — no daemon boot required — so the
test uses a tmp-path task store and asserts on disk-state after each
step.
"""

from __future__ import annotations

import pathlib

from alice_forge.task_store import TaskStore
from alice_speaking import auto_fix


_PROMPT = (
    "You are an auto-fix worker for issue #375 in jcronq/alice "
    "from @jcronq.\n"
    "\n"
    "Issue body:\n(omitted)\n"
)


def test_intake_cascades_to_building(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    task_id = auto_fix.record_auto_fix_task_intake(
        _PROMPT, "bg-deadbeef", tasks_root=root
    )
    assert task_id == "task-0001"
    store = TaskStore(root)
    record = store.load(task_id)
    assert record.status == "building"
    assert record.actor == "speaking"
    assert record.artifact_type == "code"
    assert record.source == "jcronq/alice#375"
    assert "auto-fix" in record.tags
    assert "jcronq/alice#375" in record.tags
    assert "worker:bg-deadbeef" in record.tags
    # transitions.jsonl recorded the full cascade
    trans = store.transitions(task_id)
    assert [t["to"] for t in trans] == ["draft", "selected", "building"]


def test_intake_returns_none_for_non_auto_fix(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    result = auto_fix.record_auto_fix_task_intake(
        "research the foo bar", "bg-1234", tasks_root=root
    )
    assert result is None
    # No task directory created
    assert not (root / "task-0001").exists()


def test_completion_transitions_to_done_with_pr_url(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    task_id = auto_fix.record_auto_fix_task_intake(
        _PROMPT, "bg-deadbeef", tasks_root=root
    )
    assert task_id is not None
    result_text = (
        "Implementation complete.\n\n"
        "PR: https://github.com/jcronq/alice/pull/376\n"
    )
    closed = auto_fix.record_auto_fix_task_complete(
        "bg-deadbeef", result_text, is_error=False, tasks_root=root
    )
    assert closed == task_id
    store = TaskStore(root)
    record = store.load(task_id)
    assert record.status == "done"
    assert record.merge_ref == "https://github.com/jcronq/alice/pull/376"


def test_completion_blocks_on_error(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    task_id = auto_fix.record_auto_fix_task_intake(
        _PROMPT, "bg-deadbeef", tasks_root=root
    )
    assert task_id is not None
    auto_fix.record_auto_fix_task_complete(
        "bg-deadbeef",
        "Worker crashed: missing dependency",
        is_error=True,
        tasks_root=root,
    )
    store = TaskStore(root)
    record = store.load(task_id)
    assert record.status == "blocked"


def test_completion_blocks_when_no_pr_url(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    task_id = auto_fix.record_auto_fix_task_intake(
        _PROMPT, "bg-deadbeef", tasks_root=root
    )
    assert task_id is not None
    auto_fix.record_auto_fix_task_complete(
        "bg-deadbeef",
        "Investigated but didn't open a PR",
        is_error=False,
        tasks_root=root,
    )
    store = TaskStore(root)
    record = store.load(task_id)
    assert record.status == "blocked"


def test_completion_noop_for_unknown_worker(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tasks"
    # No intake for this worker; completion should be a no-op.
    result = auto_fix.record_auto_fix_task_complete(
        "bg-unknown",
        "https://github.com/jcronq/alice/pull/1",
        is_error=False,
        tasks_root=root,
    )
    assert result is None


def test_lookup_by_repo_issue_tag(tmp_path: pathlib.Path) -> None:
    """The `<repo>#<N>` tag is the documented lookup key for the
    dispatcher to find a task between dispatch and merge events."""
    root = tmp_path / "tasks"
    auto_fix.record_auto_fix_task_intake(
        _PROMPT, "bg-deadbeef", tasks_root=root
    )
    store = TaskStore(root)
    found = store.find_by_tag("jcronq/alice#375")
    assert found is not None
    assert found["id"] == "task-0001"
