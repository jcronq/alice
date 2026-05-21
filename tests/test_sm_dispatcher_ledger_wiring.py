"""Phase 1 wiring tests — EmittedLedger loads + saves alongside DispatcherState.

These tests exercise the dispatcher's main ``run()`` path with all
external services stubbed so the only thing actually happening is
the state/ledger round-trip. No v1 handler reads or writes the
ledger yet (Phase 2 work); these tests confirm the persistence
infrastructure is in place for when handlers do.
"""

from __future__ import annotations

import json
import pathlib

from alice_forge.dispatcher.main import run
from alice_forge.sm.ledger import (
    EmittedLedger,
    SCHEMA_VERSION,
    load_ledger,
)


def _empty_run(state_path: pathlib.Path, ledger_path: pathlib.Path | None = None):
    """Drive ``run()`` with stubs that produce no issues + no API hits."""
    return run(
        repo="jcronq/alice",
        state_path=state_path,
        ledger_path=ledger_path,
        list_issues=lambda repo: [],
        list_stale_closed=lambda repo: [],
        list_open_done=lambda repo: [],
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        enable_rebase=False,
        dry_run=False,
        log=lambda s: None,
    )


class TestLedgerSavedOnEmptyRun:
    def test_creates_ledger_file_alongside_state(self, tmp_path: pathlib.Path):
        state_path = tmp_path / "sm-dispatcher-state.json"
        exit_code, _ = _empty_run(state_path)
        assert exit_code == 0
        # Default ledger location is alongside the state file.
        ledger_path = state_path.parent / "sm-emit-ledger.json"
        assert ledger_path.exists()
        data = json.loads(ledger_path.read_text())
        assert data["version"] == SCHEMA_VERSION
        assert data["records"] == []

    def test_respects_explicit_ledger_path(self, tmp_path: pathlib.Path):
        state_path = tmp_path / "state.json"
        custom = tmp_path / "subdir" / "custom-ledger.json"
        exit_code, _ = _empty_run(state_path, ledger_path=custom)
        assert exit_code == 0
        assert custom.exists()


class TestRoundTripPreservesLedgerContent:
    def test_existing_ledger_loaded_and_re_saved(self, tmp_path: pathlib.Path):
        # Pre-seed the ledger with a record; run() should load it,
        # leave it alone (no handler touches it in Phase 1), and re-
        # save without dropping the record.
        import datetime as dt

        ledger_path = tmp_path / "sm-emit-ledger.json"
        seeded = EmittedLedger()
        seeded.mark_emitted(
            issue_number=999,
            side_effect="hello",
            emitted_at=dt.datetime(2026, 5, 21, 18, 0, tzinfo=dt.timezone.utc),
            ttl_seconds=None,
            metadata={"sentinel": "phase-1-test"},
        )
        seeded.save(ledger_path)

        state_path = tmp_path / "state.json"
        exit_code, _ = _empty_run(state_path, ledger_path=ledger_path)
        assert exit_code == 0

        loaded = load_ledger(ledger_path)
        assert len(loaded.records) == 1
        assert loaded.records[0].issue_number == 999
        assert loaded.records[0].metadata == {"sentinel": "phase-1-test"}


class TestCorruptLedgerDoesNotKillRun:
    def test_unreadable_ledger_falls_back_to_empty(
        self, tmp_path: pathlib.Path
    ):
        # A v3 ledger with a wrong version should be logged and
        # treated as empty — the v1 state save must still happen.
        ledger_path = tmp_path / "sm-emit-ledger.json"
        ledger_path.write_text(json.dumps({"version": 999, "records": []}))

        state_path = tmp_path / "state.json"
        exit_code, _ = _empty_run(state_path, ledger_path=ledger_path)
        assert exit_code == 0
        # After run() the ledger is re-saved as v_current (empty).
        re_loaded = load_ledger(ledger_path)
        assert re_loaded.version == SCHEMA_VERSION
        assert re_loaded.records == []


class TestForwardMigrationFromV1State:
    def test_v1_state_only_migrates_to_ledger(self, tmp_path: pathlib.Path):
        # Phase 1 cutover scenario: v1 state file exists, no v3
        # ledger file. On run(), the dispatcher's load_or_migrate
        # picks up the v1 state and synthesizes ledger records.
        state_path = tmp_path / "sm-dispatcher-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hello_commented": [42, 43],
                    "needs_study_hinted": [44],
                    "triage_surfaced": [],
                    "design_revisions": {"50": 2},
                }
            )
        )
        exit_code, _ = _empty_run(state_path)
        assert exit_code == 0
        ledger_path = state_path.parent / "sm-emit-ledger.json"
        ledger = load_ledger(ledger_path)
        # Three flat-list entries + one design-revision entry = 4 records.
        assert len(ledger.records) == 4
        names = {r.side_effect for r in ledger.records}
        assert "hello" in names
        assert "study-hint" in names
        assert "design-revision" in names
