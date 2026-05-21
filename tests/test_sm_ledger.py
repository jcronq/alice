"""Tests for ``alice_forge.sm.ledger``."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from alice_forge.sm.ledger import (
    EmittedLedger,
    EmittedRecord,
    SCHEMA_VERSION,
    load_ledger,
    load_or_migrate,
)


def _utc(s: str) -> dt.datetime:
    """Helper for fixed UTC timestamps in tests."""
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


class TestEmittedRecordLifecycle:
    def test_active_until_cleared(self):
        rec = EmittedRecord(
            issue_number=42,
            side_effect="spawn-started",
            emitted_at=_utc("2026-05-21T18:00:00"),
            ttl_seconds=None,
        )
        # No TTL, not cleared → active forever
        assert rec.is_active(_utc("2026-05-21T18:00:01"))
        assert rec.is_active(_utc("2030-01-01T00:00:00"))

    def test_ttl_expires(self):
        rec = EmittedRecord(
            issue_number=42,
            side_effect="hello",
            emitted_at=_utc("2026-05-21T18:00:00"),
            ttl_seconds=60,
        )
        assert rec.is_active(_utc("2026-05-21T18:00:30"))
        assert not rec.is_active(_utc("2026-05-21T18:01:01"))
        assert rec.is_expired(_utc("2026-05-21T18:01:01"))

    def test_cleared_record_inactive(self):
        rec = EmittedRecord(
            issue_number=42,
            side_effect="hello",
            emitted_at=_utc("2026-05-21T18:00:00"),
            ttl_seconds=None,
            cleared_at=_utc("2026-05-21T18:05:00"),
            cleared_by="completion-marker",
        )
        assert not rec.is_active(_utc("2026-05-21T18:06:00"))
        assert not rec.is_expired(_utc("2026-05-21T18:06:00"))


class TestEmittedLedger:
    def test_empty_ledger(self):
        ledger = EmittedLedger()
        assert ledger.records == []
        assert not ledger.is_emitted_active(42, "hello", _utc("2026-05-21T18:00:00"))

    def test_mark_emitted_records(self):
        ledger = EmittedLedger()
        rec = ledger.mark_emitted(
            issue_number=42,
            side_effect="hello",
            emitted_at=_utc("2026-05-21T18:00:00"),
            ttl_seconds=None,
        )
        assert rec.issue_number == 42
        assert rec.side_effect == "hello"
        assert ledger.is_emitted_active(42, "hello", _utc("2026-05-21T18:01:00"))

    def test_clear_emitted_marks_record(self):
        ledger = EmittedLedger()
        ledger.mark_emitted(
            issue_number=42,
            side_effect="spawn-started",
            emitted_at=_utc("2026-05-21T18:00:00"),
            ttl_seconds=None,
        )
        cleared = ledger.clear_emitted(
            42, "spawn-started", _utc("2026-05-21T18:30:00")
        )
        assert cleared is True
        assert not ledger.is_emitted_active(42, "spawn-started", _utc("2026-05-21T18:31:00"))

    def test_clear_returns_false_when_nothing_active(self):
        ledger = EmittedLedger()
        assert (
            ledger.clear_emitted(99, "nonexistent", _utc("2026-05-21T18:00:00"))
            is False
        )

    def test_re_emission_marks_prior_replaced(self):
        ledger = EmittedLedger()
        ledger.mark_emitted(
            42, "spawn-started", _utc("2026-05-21T18:00:00"), ttl_seconds=None
        )
        ledger.mark_emitted(
            42, "spawn-started", _utc("2026-05-21T19:00:00"), ttl_seconds=None
        )
        # Two records exist; the first is cleared (by replacement),
        # the second is active.
        all_recs = [r for r in ledger.records if r.issue_number == 42]
        assert len(all_recs) == 2
        first, second = all_recs
        assert first.cleared_by == "replaced"
        assert second.cleared_at is None

    def test_sweep_expired_clears_and_returns_them(self):
        ledger = EmittedLedger()
        ledger.mark_emitted(
            1, "hello", _utc("2026-05-21T18:00:00"), ttl_seconds=60
        )
        ledger.mark_emitted(
            2, "hello", _utc("2026-05-21T18:00:30"), ttl_seconds=60
        )
        ledger.mark_emitted(
            3, "hello", _utc("2026-05-21T18:01:00"), ttl_seconds=None
        )
        swept = ledger.sweep_expired(_utc("2026-05-21T18:02:00"))
        # Issues 1 and 2 have expired; issue 3 has no TTL.
        swept_numbers = sorted(r.issue_number for r in swept)
        assert swept_numbers == [1, 2]
        # Sweeping again is a no-op (idempotent).
        assert ledger.sweep_expired(_utc("2026-05-21T18:02:00")) == []


class TestPersistence:
    def test_round_trip(self, tmp_path: pathlib.Path):
        ledger = EmittedLedger()
        ledger.mark_emitted(
            42, "hello", _utc("2026-05-21T18:00:00"), ttl_seconds=None,
            metadata={"spawn_dir": "/some/path"},
        )
        ledger.mark_emitted(
            42, "study-hint", _utc("2026-05-21T18:00:30"), ttl_seconds=60,
        )
        path = tmp_path / "emit-ledger.json"
        ledger.save(path)

        loaded = load_ledger(path)
        assert loaded.version == SCHEMA_VERSION
        assert len(loaded.records) == 2
        assert loaded.records[0].metadata == {"spawn_dir": "/some/path"}

    def test_missing_file_returns_empty_ledger(self, tmp_path: pathlib.Path):
        ledger = load_ledger(tmp_path / "nonexistent.json")
        assert ledger.records == []

    def test_atomic_write_uses_tmp_file(self, tmp_path: pathlib.Path):
        # Verify the write doesn't leave a .tmp behind on success.
        ledger = EmittedLedger()
        ledger.mark_emitted(
            42, "hello", _utc("2026-05-21T18:00:00"), ttl_seconds=None
        )
        path = tmp_path / "ledger.json"
        ledger.save(path)
        assert path.exists()
        assert not (tmp_path / "ledger.json.tmp").exists()

    def test_version_mismatch_raises(self, tmp_path: pathlib.Path):
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({"version": 999, "records": []}))
        with pytest.raises(ValueError, match="schema mismatch"):
            load_ledger(path)


class TestV1Migration:
    def test_migrates_v1_lists_to_emit_records(self, tmp_path: pathlib.Path):
        v1_path = tmp_path / "v1-state.json"
        v1_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hello_commented": [1, 2, 3],
                    "needs_study_hinted": [4],
                    "triage_surfaced": [5, 6],
                    "design_revisions": {"7": 2, "8": 1},
                }
            )
        )
        v3_path = tmp_path / "v3-emit-ledger.json"  # missing; triggers migrate

        ledger = load_or_migrate(v3_path, v1_path)

        # Six issue-numbers across the flat lists + two from
        # design_revisions = 8 records total.
        assert len(ledger.records) == 8

        names = {r.side_effect for r in ledger.records}
        assert "hello" in names
        assert "study-hint" in names
        assert "triage-surface" in names
        assert "design-revision" in names

        # design-revision records carry the count in metadata.
        dr_records = [r for r in ledger.records if r.side_effect == "design-revision"]
        assert len(dr_records) == 2
        counts = sorted(r.metadata["count"] for r in dr_records)
        assert counts == [1, 2]

    def test_prefers_v3_file_when_both_exist(self, tmp_path: pathlib.Path):
        v3_path = tmp_path / "v3.json"
        v3_path.write_text(json.dumps({"version": SCHEMA_VERSION, "records": []}))
        v1_path = tmp_path / "v1.json"
        v1_path.write_text(json.dumps({"hello_commented": [1, 2, 3]}))

        ledger = load_or_migrate(v3_path, v1_path)
        # v3 was present and empty; v1 data NOT migrated.
        assert ledger.records == []

    def test_missing_v1_returns_empty(self, tmp_path: pathlib.Path):
        ledger = load_or_migrate(tmp_path / "v3.json", tmp_path / "no-v1.json")
        assert ledger.records == []
