"""Tests for ``task reindex`` — #781.

The on-disk ``index.jsonl`` drifts from reality whenever a task.yaml
is hand-edited, migrated, or written by a crashed wake. #781 observed
608/912 — a ~33% miss rate. ``task reindex`` walks ``task-*/task.yaml``
and rewrites the index from scratch.

Coverage:

* :func:`test_reindex_rebuilds_from_task_yaml` — a freshly populated
  store with an intentionally stale index converges on the full set
  after reindex.
* :func:`test_reindex_dry_run_does_not_write` — dry-run only reports.
* :func:`test_reindex_flags_missing_yaml` — orphan task directory
  (no task.yaml) is reported, not silently dropped.
* :func:`test_reindex_flags_malformed_yaml` — malformed YAML surfaces
  as an error row with the reason.
* :func:`test_reindex_atomic_write` — a half-written tempfile cannot
  leave ``index.jsonl`` in a torn state (rename is atomic).
* :func:`test_reindex_cli_missing_yaml_exit_code` — the CLI exits
  non-zero when it finds malformed entries so cron / memory-worker
  catches regressions.
* :func:`test_reindex_cli_dry_run_stdout_shape` — dry-run human
  output includes the added/removed/changed preview.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_forge import task_cli
from alice_forge.task_store import TaskStore


def _run_cli(args: list[str], root: pathlib.Path) -> int:
    return task_cli.main(["--root", str(root), *args])


def _seed_store(root: pathlib.Path, *, n: int) -> TaskStore:
    """Populate ``root`` with ``n`` tasks via the real create path.

    Using :meth:`TaskStore.create` means the on-disk shape matches
    what the live store produces — tags, transitions, task.yaml,
    index.jsonl all consistent at the start.
    """
    store = TaskStore(root)
    for i in range(n):
        store.create(
            title=f"task {i}",
            tags=[f"tag{i % 3}"],
            actor="speaking",
        )
    return store


def test_reindex_rebuilds_from_task_yaml(tmp_path) -> None:
    store = _seed_store(tmp_path, n=5)

    # Simulate the #781 drift: truncate index.jsonl to the first entry
    # even though the on-disk task-NNNN/ directories cover all five.
    index_path = store.index_path
    first_line = index_path.read_text().splitlines()[0]
    index_path.write_text(first_line + "\n")

    assert len(list(store.iter_index())) == 1

    report = store.reindex()
    assert report.scanned == 5
    assert report.written == 5
    assert report.before_count == 1
    assert report.after_count == 5
    assert report.added == [f"task-{i+1:04d}" for i in range(1, 5)]
    assert report.errors == []
    assert report.dry_run is False

    # Index now carries all five tasks with the required #781 columns.
    rebuilt = list(store.iter_index())
    assert len(rebuilt) == 5
    first = rebuilt[0]
    for key in (
        "id",
        "title",
        "status",
        "actor",
        "artifact_type",
        "created",
        "updated",
        "tags",
    ):
        assert key in first, f"missing {key} in rebuilt entry"


def test_reindex_dry_run_does_not_write(tmp_path) -> None:
    store = _seed_store(tmp_path, n=3)

    # Corrupt the index so the dry-run has an obvious diff to report.
    store.index_path.write_text("")

    report = store.reindex(dry_run=True)
    assert report.dry_run is True
    assert report.scanned == 3
    assert report.written == 3
    assert set(report.added) == {"task-0001", "task-0002", "task-0003"}

    # Important: the on-disk file was NOT touched by a dry-run — the
    # truncated version we wrote is still there, unchanged.
    assert store.index_path.read_text() == ""

    # And a follow-up non-dry-run call does write.
    report2 = store.reindex()
    assert report2.dry_run is False
    assert len(list(store.iter_index())) == 3


def test_reindex_flags_missing_yaml(tmp_path) -> None:
    store = _seed_store(tmp_path, n=2)

    # An operator-created directory that looks like a task but has no
    # task.yaml — perhaps a `mkdir task-9999` from a shell typo, or a
    # worker that crashed before writing the canonical file.
    orphan = tmp_path / "task-9999"
    orphan.mkdir()

    report = store.reindex()
    assert report.scanned == 3  # task-0001, task-0002, task-9999
    assert report.written == 2  # the orphan does NOT contribute a row
    assert len(report.errors) == 1
    err = report.errors[0]
    assert err["id"] == "task-9999"
    assert "missing task.yaml" in err["reason"]

    # And critically: the orphan directory still exists. The reindex
    # does NOT clean up — operators decide.
    assert orphan.is_dir()


def test_reindex_flags_malformed_yaml(tmp_path) -> None:
    store = _seed_store(tmp_path, n=1)

    # Overwrite task-0001's canonical file with a non-mapping blob so
    # _parse_yaml_lenient raises. This is the "crashed mid-write"
    # scenario — the file exists but can't be parsed as a dict.
    yaml_path = tmp_path / "task-0001" / "task.yaml"
    yaml_path.write_text("- just\n- a\n- list\n")

    report = store.reindex()
    assert report.written == 0
    assert len(report.errors) == 1
    err = report.errors[0]
    assert err["id"] == "task-0001"
    assert "malformed task.yaml" in err["reason"]


def test_reindex_atomic_write(tmp_path, monkeypatch) -> None:
    """Simulate a crash inside ``_atomic_write_text`` and assert the
    pre-existing ``index.jsonl`` survives intact.

    The rename is the atomic step; if we raise AFTER the tempfile is
    written but BEFORE the rename lands, the original must still be
    readable. #781 explicitly flagged this as a hard rule.
    """
    store = _seed_store(tmp_path, n=4)
    original = store.index_path.read_text()
    assert original  # sanity

    # Force _atomic_write_text to raise just before os.rename by
    # monkeypatching the rename call used inside the helper.
    import alice_forge.task_store as mod

    calls: list[tuple[str, str]] = []

    def exploding_rename(src: str, dst: str) -> None:
        calls.append((src, dst))
        raise RuntimeError("simulated crash before rename")

    monkeypatch.setattr(mod.os, "rename", exploding_rename)

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.reindex()

    # Original index.jsonl is unchanged — the atomic-write contract
    # held even though the write path blew up.
    assert store.index_path.read_text() == original

    # And the tempfile cleanup fired — no stray hidden tmp lingering.
    tmp_files = [
        p for p in tmp_path.iterdir()
        if p.name.startswith(".index.jsonl.")
    ]
    assert tmp_files == [], f"orphan tempfile left behind: {tmp_files}"


def test_reindex_cli_missing_yaml_exit_code(tmp_path, capsys) -> None:
    _seed_store(tmp_path, n=1)
    (tmp_path / "task-0042").mkdir()

    code = _run_cli(["reindex"], tmp_path)
    captured = capsys.readouterr()

    assert code == 6, "reindex should exit 6 when errors are present"
    assert "task-0042" in captured.err
    assert "missing task.yaml" in captured.err
    assert "reindex:" in captured.out


def test_reindex_cli_dry_run_stdout_shape(tmp_path, capsys) -> None:
    store = _seed_store(tmp_path, n=2)
    # Blow away the index so dry-run has an "added" preview to render.
    store.index_path.write_text("")

    code = _run_cli(["reindex", "--dry-run"], tmp_path)
    assert code == 0  # no errors, so clean dry-run returns 0
    out = capsys.readouterr().out
    assert "scanned=2" in out
    assert "would write=2" in out
    # Added preview appears in the human-readable output.
    assert "added:" in out
    assert "task-0001" in out

    # Dry-run didn't touch the file.
    assert store.index_path.read_text() == ""


def test_reindex_cli_json_mode(tmp_path, capsys) -> None:
    _seed_store(tmp_path, n=2)
    code = _run_cli(["--json", "reindex", "--dry-run"], tmp_path)
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["scanned"] == 2
    assert payload["dry_run"] is True
    assert set(payload["added"]) == set()  # index is already in sync
    assert payload["errors"] == []
