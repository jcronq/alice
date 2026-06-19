"""Tests for alice_thinking.memory_worker.correction_cascade_auto_propagate.

Focused on the events.jsonl observability emitted by auto_propagate():
the run writes a single `correction_cascade_auto_propagate` event with
the resolved counts, the dry-run/production mode, and severity breakdown.
"""

import json
import pathlib

from alice_thinking.memory_worker.correction_cascade import (
    CascadeReport,
    UnpropagatedCorrection,
)
from alice_thinking.memory_worker.correction_cascade_auto_propagate import (
    auto_propagate,
)


def _mk_mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """Build a minimal mind dir: a referencing note + a corrected note."""
    vault = tmp_path / "cortex-memory"
    vault.mkdir()
    (tmp_path / "memory").mkdir()

    # Referencing note cites the corrected note but not the correction.
    (vault / "ref-note.md").write_text(
        "---\nslug: ref-note\n---\n\n"
        "Body cites [[corrected-note]].\n\n"
        "## Backlinks\n\n",
        encoding="utf-8",
    )
    # Corrected note exists so corrected_by: can be updated.
    (vault / "corrected-note.md").write_text(
        "---\nslug: corrected-note\n---\n\nThe corrected claim.\n",
        encoding="utf-8",
    )
    return tmp_path


def _mk_report() -> CascadeReport:
    report = CascadeReport(correction_pairs_checked=1)
    report.unpropagated.append(
        UnpropagatedCorrection(
            corrected_slug="corrected-note",
            corrected_title="Corrected Note",
            correction_slug="correction-note",
            correction_title="Correction Note",
            referencing_slug="ref-note",
            referencing_title="Ref Note",
            severity="high",
            claim_changed="42% -> 73%",
        )
    )
    return report


def _read_events(mind: pathlib.Path) -> list[dict]:
    path = mind / "memory" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestPropagationEvent:
    def test_dry_run_writes_event(self, tmp_path):
        mind = _mk_mind(tmp_path)
        auto_propagate(mind, _mk_report(), dry_run=True)

        events = _read_events(mind)
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "correction_cascade_auto_propagate"
        assert ev["mode"] == "dry-run"
        # One high-severity correction would be added to one note.
        assert ev["total_resolved"] == 1
        assert ev["pairs_affected"] == 1
        assert ev["high_severity"] == 1
        assert ev["medium_severity"] == 0
        assert ev["low_severity"] == 0
        assert isinstance(ev["duration_seconds"], (int, float))
        assert "date" in ev and "time" in ev and "ts" in ev

    def test_production_mode_field(self, tmp_path):
        mind = _mk_mind(tmp_path)
        auto_propagate(mind, _mk_report(), dry_run=False)

        events = _read_events(mind)
        assert len(events) == 1
        assert events[0]["mode"] == "production"

    def test_empty_report_still_emits_event(self, tmp_path):
        mind = _mk_mind(tmp_path)
        auto_propagate(mind, CascadeReport(), dry_run=True)

        events = _read_events(mind)
        assert len(events) == 1
        ev = events[0]
        assert ev["total_resolved"] == 0
        assert ev["pairs_affected"] == 0
        assert ev["high_severity"] == 0
