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
from alice_thinking.memory_worker import correction_cascade_auto_propagate as acp
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


class TestDryRunGate:
    """The write-gate must hold on the default-None path, not just when
    dry_run is passed explicitly.

    Regression for the gate-defeat bug: the helper call sites passed the
    raw local ``dry_run`` (``None`` on the default path) instead of the
    resolved module ``_DRY_RUN``. ``dry_run=None`` overrode each helper's
    ``= _DRY_RUN`` default and was falsy inside ``if dry_run:``, so files
    were written while the event still labeled the run "dry-run".
    Stage C (live since #504) calls ``auto_propagate`` with no ``dry_run``.
    """

    def test_default_no_kwarg_does_not_write(self, tmp_path, monkeypatch):
        # Pin the module default to True (its real default) so this test is
        # independent of ordering — other tests toggle the module global.
        monkeypatch.setattr(acp, "_DRY_RUN", True)
        mind = _mk_mind(tmp_path)
        ref_note = mind / "cortex-memory" / "ref-note.md"
        corrected_note = mind / "cortex-memory" / "corrected-note.md"
        before_ref = ref_note.read_bytes()
        before_corrected = corrected_note.read_bytes()

        # No dry_run kwarg — exactly how stage_c.run() calls it.
        auto_propagate(mind, _mk_report())

        # Byte-identical: the gate held, nothing was written.
        assert ref_note.read_bytes() == before_ref
        assert corrected_note.read_bytes() == before_corrected

        events = _read_events(mind)
        assert len(events) == 1
        assert events[0]["mode"] == "dry-run"

    def test_explicit_production_writes_backlink(self, tmp_path, monkeypatch):
        monkeypatch.setattr(acp, "_DRY_RUN", True)
        mind = _mk_mind(tmp_path)
        ref_note = mind / "cortex-memory" / "ref-note.md"
        before_ref = ref_note.read_bytes()

        auto_propagate(mind, _mk_report(), dry_run=False)

        after_ref = ref_note.read_bytes()
        assert after_ref != before_ref
        # The correction backlink was actually appended.
        assert "[[correction-note" in ref_note.read_text(encoding="utf-8")

        events = _read_events(mind)
        assert len(events) == 1
        assert events[0]["mode"] == "production"
