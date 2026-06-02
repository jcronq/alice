"""Tests for :mod:`alice_thinking.memory_worker.journal`.

The journal is the crash-recovery backbone — pending entries on
disk must be inspected and resolved before the next wake mutates
the vault. Tests cover the append/load roundtrip, idempotent
status transitions, verifier dispatch (committed / skipped /
unknown-op), and resilience to malformed lines.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_thinking.memory_worker.journal import (
    COMMITTED,
    PENDING,
    SKIPPED,
    JournalEntry,
    ReplayReport,
    append,
    commit,
    load,
    register_verifier,
    replay,
    reset_verifiers_to_phase1_defaults,
)


@pytest.fixture(autouse=True)
def _reset_verifiers():
    """Each test starts with the Phase 1 logging-stub registry.

    Without this, a test that registers a real verifier would
    contaminate the next test's replay behavior because the
    registry is module-level state.
    """
    reset_verifiers_to_phase1_defaults()
    yield
    reset_verifiers_to_phase1_defaults()


# ---------- append + load roundtrip ----------


def test_append_writes_jsonl_line(tmp_path: pathlib.Path) -> None:
    """One append → one JSON line on disk with the expected fields."""
    journal_path = tmp_path / "journal.jsonl"
    entry = append(
        journal_path,
        op="atomize",
        source="cortex-memory/research/foo.md",
        targets=["cortex-memory/research/foo-1.md", "cortex-memory/research/foo-2.md"],
        detail={"split_at_heading": "## Body"},
        journal_id="test-id-1",
    )

    assert entry.journal_id == "test-id-1"
    assert entry.status == PENDING

    lines = journal_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["journal_id"] == "test-id-1"
    assert parsed["op"] == "atomize"
    assert parsed["source"] == "cortex-memory/research/foo.md"
    assert parsed["targets"] == [
        "cortex-memory/research/foo-1.md",
        "cortex-memory/research/foo-2.md",
    ]
    assert parsed["detail"] == {"split_at_heading": "## Body"}
    assert parsed["status"] == PENDING


def test_load_returns_empty_for_missing_file(tmp_path: pathlib.Path) -> None:
    """A missing journal is not an error — first-ever startup hits this."""
    assert load(tmp_path / "absent.jsonl") == []


def test_load_collapses_status_history(tmp_path: pathlib.Path) -> None:
    """Multiple status records for one id collapse to the latest status."""
    journal_path = tmp_path / "journal.jsonl"
    entry = append(
        journal_path,
        op="archive",
        source="cortex-memory/dailies/2025-01-01.md",
        journal_id="archive-1",
    )
    commit(journal_path, entry.journal_id)

    loaded = load(journal_path)
    assert len(loaded) == 1
    assert loaded[0].journal_id == "archive-1"
    assert loaded[0].status == COMMITTED
    # Op metadata survives the collapse — only status is updated.
    assert loaded[0].source == "cortex-memory/dailies/2025-01-01.md"


def test_status_update_for_unknown_id_is_ignored(tmp_path: pathlib.Path) -> None:
    """A torn-record orphan can't resurrect an id we never appended."""
    journal_path = tmp_path / "journal.jsonl"
    commit(journal_path, "ghost-id")
    assert load(journal_path) == []


def test_malformed_lines_are_skipped(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A torn write or hand-edit corruption shouldn't break replay."""
    journal_path = tmp_path / "journal.jsonl"
    good = append(journal_path, op="archive", source="x", journal_id="good-1")
    # Inject garbage between two good records.
    with journal_path.open("a") as fh:
        fh.write("this is not json\n")
        fh.write('{"missing": "journal_id"}\n')
    append(journal_path, op="archive", source="y", journal_id="good-2")

    with caplog.at_level("WARNING"):
        loaded = {e.journal_id: e for e in load(journal_path)}
    assert set(loaded.keys()) == {good.journal_id, "good-2"}


# ---------- replay dispatch ----------


def test_replay_marks_unknown_op_skipped(tmp_path: pathlib.Path) -> None:
    """An op with no verifier transitions to SKIPPED so we don't
    re-inspect it forever."""
    journal_path = tmp_path / "journal.jsonl"
    append(
        journal_path,
        op="invented-op",
        source="x",
        journal_id="weird-1",
    )

    report = replay(journal_path)
    assert isinstance(report, ReplayReport)
    assert report.inspected == 1
    assert report.unknown_op == 1
    assert report.skipped == 1
    assert report.committed == 0

    after = {e.journal_id: e.status for e in load(journal_path)}
    assert after["weird-1"] == SKIPPED


def test_replay_commits_when_verifier_confirms(tmp_path: pathlib.Path) -> None:
    """A verifier returning True transitions the entry to COMMITTED."""
    journal_path = tmp_path / "journal.jsonl"
    append(
        journal_path,
        op="atomize",
        source="cortex-memory/research/foo.md",
        targets=["cortex-memory/research/foo-1.md"],
        journal_id="atomize-1",
    )

    seen: list[JournalEntry] = []

    def yes_verifier(entry: JournalEntry) -> bool:
        seen.append(entry)
        return True

    register_verifier("atomize", yes_verifier)

    report = replay(journal_path)
    assert report.inspected == 1
    assert report.committed == 1
    assert report.skipped == 0
    assert len(seen) == 1
    assert seen[0].journal_id == "atomize-1"

    after = {e.journal_id: e.status for e in load(journal_path)}
    assert after["atomize-1"] == COMMITTED


def test_replay_skips_when_verifier_denies(tmp_path: pathlib.Path) -> None:
    """Phase 1 stubs return False — entry is SKIPPED, not COMMITTED."""
    journal_path = tmp_path / "journal.jsonl"
    append(
        journal_path,
        op="atomize",
        source="x",
        journal_id="atomize-2",
    )

    # The Phase 1 default stub returns False; rely on that here.
    report = replay(journal_path)
    assert report.inspected == 1
    assert report.committed == 0
    assert report.skipped == 1
    assert report.unknown_op == 0

    after = {e.journal_id: e.status for e in load(journal_path)}
    assert after["atomize-2"] == SKIPPED


def test_replay_swallows_verifier_exception(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A verifier raising must not crash replay; entry is SKIPPED."""
    journal_path = tmp_path / "journal.jsonl"
    append(
        journal_path,
        op="archive",
        source="x",
        journal_id="bang-1",
    )

    def boom(entry: JournalEntry) -> bool:
        raise RuntimeError("verifier blew up")

    register_verifier("archive", boom)

    with caplog.at_level("WARNING"):
        report = replay(journal_path)
    assert report.skipped == 1
    assert report.committed == 0
    after = {e.journal_id: e.status for e in load(journal_path)}
    assert after["bang-1"] == SKIPPED


def test_replay_skips_already_done_entries(tmp_path: pathlib.Path) -> None:
    """Entries already in COMMITTED or SKIPPED don't get re-verified."""
    journal_path = tmp_path / "journal.jsonl"
    append(
        journal_path,
        op="atomize",
        source="x",
        journal_id="done-1",
    )
    commit(journal_path, "done-1")

    call_count = {"n": 0}

    def counting_verifier(entry: JournalEntry) -> bool:
        call_count["n"] += 1
        return True

    register_verifier("atomize", counting_verifier)

    report = replay(journal_path)
    assert report.inspected == 1
    assert report.already_done == 1
    assert report.committed == 0
    assert call_count["n"] == 0


def test_replay_is_idempotent_across_runs(tmp_path: pathlib.Path) -> None:
    """Running replay twice on the same journal yields the same end state.

    Important invariant: a worker that crashes after replay but
    before its next wake must not double-process anything on restart.
    """
    journal_path = tmp_path / "journal.jsonl"
    append(journal_path, op="atomize", source="x", journal_id="idem-1")

    register_verifier("atomize", lambda e: True)

    first = replay(journal_path)
    assert first.committed == 1
    assert first.skipped == 0

    second = replay(journal_path)
    assert second.inspected == 1
    assert second.already_done == 1
    assert second.committed == 0


def test_journal_entry_from_dict_roundtrip() -> None:
    """Schema validation via the dataclass converters."""
    raw = {
        "journal_id": "rt-1",
        "ts": "2026-06-01T20:58:00Z",
        "op": "frontmatter-update",
        "source": "cortex-memory/people/jason.md",
        "targets": [],
        "detail": {"set": {"updated": "2026-06-01"}},
        "status": PENDING,
    }
    entry = JournalEntry.from_dict(raw)
    assert entry.to_dict() == raw
