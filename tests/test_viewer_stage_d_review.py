"""Tests for ``alice_viewer.stage_d_store`` — focused on the
pairs-log read path that backs ``/stage-d-review``.

Regression target: #134 — the viewer used to read only the rich
``stage-d-attempts.jsonl`` firehose, which the wake prompt never
populates. Operationally the wake writes the simpler per-night
``stage-d-pairs-YYYY-MM-DD.jsonl`` files, so the tab rendered empty
despite live data being present. These tests pin the new behaviour
where the viewer also synthesises attempt-shaped rows from the
pairs logs and joins them with the labels sidecar.
"""

from __future__ import annotations

import json
import pathlib

from alice_viewer import stage_d_store


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _state_dir(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "state"


def test_read_pairs_returns_empty_when_no_logs(tmp_path):
    mind = tmp_path / "mind"
    assert stage_d_store.read_pairs(mind) == []


def test_read_pairs_concatenates_all_per_night_logs(tmp_path):
    mind = tmp_path / "mind"
    state = _state_dir(mind)
    _write_jsonl(
        state / "stage-d-pairs-2026-05-11.jsonl",
        [
            {
                "ts": "2026-05-11T03:00:00Z",
                "note_a": "alpha",
                "note_b": "beta",
                "synthesis": "2026-05-11-alpha-x-beta",
            }
        ],
    )
    _write_jsonl(
        state / "stage-d-pairs-2026-05-12.jsonl",
        [
            {
                "ts": "2026-05-12T04:00:00Z",
                "note_a": "gamma",
                "note_b": "delta",
                "synthesis": None,
            }
        ],
    )

    rows = stage_d_store.read_pairs(mind)
    assert len(rows) == 2
    assert rows[0]["note_a"] == "alpha"
    assert rows[1]["synthesis"] is None


def test_pair_to_attempt_shipped_shape():
    pair = {
        "ts": "2026-05-12T04:00:00Z",
        "note_a": "alpha",
        "note_b": "beta",
        "synthesis": "2026-05-12-alpha-x-beta",
    }
    rec = stage_d_store.pair_to_attempt(pair)
    assert rec["id"].startswith(stage_d_store.PAIR_ID_PREFIX)
    assert rec["pair"] == {"slug_a": "alpha", "slug_b": "beta"}
    assert rec["outcome"] == "shipped"
    assert rec["shipped_slug"] == "research/2026-05-12-alpha-x-beta"
    assert rec["created_at"] == "2026-05-12T04:00:00Z"
    assert rec["qwen_verdict"] is None
    assert rec["haiku_verdict"] is None
    assert rec["draft_attempt_n"] == 1
    assert rec["retry_history"] == []


def test_pair_to_attempt_null_result_shape():
    pair = {
        "ts": "2026-05-12T04:00:00Z",
        "note_a": "alpha",
        "note_b": "beta",
        "synthesis": None,
    }
    rec = stage_d_store.pair_to_attempt(pair)
    assert rec["outcome"] == stage_d_store.PAIR_NULL_OUTCOME == "null_result"
    assert rec["shipped_slug"] is None


def test_pair_to_attempt_id_is_stable():
    pair = {
        "ts": "2026-05-12T04:00:00Z",
        "note_a": "alpha",
        "note_b": "beta",
        "synthesis": "x",
    }
    a = stage_d_store.pair_to_attempt(pair)
    b = stage_d_store.pair_to_attempt(pair)
    assert a["id"] == b["id"]


def test_pair_to_attempt_id_distinguishes_pairs():
    p1 = {"ts": "T", "note_a": "a", "note_b": "b", "synthesis": "s"}
    p2 = {"ts": "T", "note_a": "a", "note_b": "c", "synthesis": "s"}
    assert (
        stage_d_store.pair_to_attempt(p1)["id"]
        != stage_d_store.pair_to_attempt(p2)["id"]
    )


def test_load_review_rows_returns_empty_with_no_files(tmp_path):
    mind = tmp_path / "mind"
    rows = stage_d_store.load_review_rows(mind)
    assert rows == []


def test_load_review_rows_includes_pair_log_entries(tmp_path):
    """Regression for #134 — the /stage-d-review tab must surface
    rows that exist only in the per-night pairs logs."""
    mind = tmp_path / "mind"
    state = _state_dir(mind)
    _write_jsonl(
        state / "stage-d-pairs-2026-05-12.jsonl",
        [
            {
                "ts": "2026-05-12T03:26:00Z",
                "note_a": "alpha",
                "note_b": "beta",
                "synthesis": "2026-05-12-alpha-x-beta",
            },
            {
                "ts": "2026-05-12T04:00:00Z",
                "note_a": "gamma",
                "note_b": "delta",
                "synthesis": None,
            },
        ],
    )

    rows = stage_d_store.load_review_rows(mind)
    assert len(rows) == 2
    by_outcome = {r["outcome"] for r in rows}
    assert by_outcome == {"shipped", "null_result"}
    assert all("label_record" in r for r in rows)
    assert all(r["label_record"] is None for r in rows)


def test_load_review_rows_joins_labels_against_pair_rows(tmp_path):
    mind = tmp_path / "mind"
    state = _state_dir(mind)
    _write_jsonl(
        state / "stage-d-pairs-2026-05-12.jsonl",
        [
            {
                "ts": "2026-05-12T03:26:00Z",
                "note_a": "alpha",
                "note_b": "beta",
                "synthesis": "2026-05-12-alpha-x-beta",
            }
        ],
    )
    pair_id = stage_d_store._pair_record_id(
        "2026-05-12T03:26:00Z", "alpha", "beta"
    )
    _write_jsonl(
        state / "stage-d-labels.jsonl",
        [{"attempt_id": pair_id, "label": "T1", "labeled_at": "2026-05-12T08:00:00Z"}],
    )

    rows = stage_d_store.load_review_rows(mind)
    assert len(rows) == 1
    assert rows[0]["label_record"]["label"] == "T1"


def test_load_review_rows_merges_firehose_and_pairs(tmp_path):
    """If both the firehose attempts log AND pairs logs exist, rows
    from both surface in the joined view."""
    mind = tmp_path / "mind"
    state = _state_dir(mind)
    _write_jsonl(
        state / "stage-d-attempts.jsonl",
        [
            {
                "id": "att-real-001",
                "pair": {"slug_a": "x", "slug_b": "y"},
                "synthesis_text": "live attempt",
                "draft_attempt_n": 1,
                "qwen_verdict": {"tier": "T1", "novel": "yes", "reason": "r", "decision": "ship"},
                "haiku_verdict": {"tier": "T1", "novel": "yes", "reason": "r", "decision": "ship"},
                "outcome": "shipped",
                "retry_history": [],
                "created_at": "2026-05-12T05:00:00-04:00",
                "shipped_slug": "research/x-y",
            }
        ],
    )
    _write_jsonl(
        state / "stage-d-pairs-2026-05-12.jsonl",
        [
            {
                "ts": "2026-05-12T03:26:00Z",
                "note_a": "alpha",
                "note_b": "beta",
                "synthesis": "2026-05-12-alpha-x-beta",
            }
        ],
    )

    rows = stage_d_store.load_review_rows(mind)
    ids = {r["id"] for r in rows}
    assert "att-real-001" in ids
    pair_id = stage_d_store._pair_record_id(
        "2026-05-12T03:26:00Z", "alpha", "beta"
    )
    assert pair_id in ids


def test_summarize_counts_null_results_in_dropped_bucket():
    rows = [
        stage_d_store.pair_to_attempt(
            {"ts": "T", "note_a": "a", "note_b": "b", "synthesis": "s"}
        ),
        stage_d_store.pair_to_attempt(
            {"ts": "T2", "note_a": "c", "note_b": "d", "synthesis": None}
        ),
    ]
    summary = stage_d_store.summarize(rows)
    assert summary["total"] == 2
    assert summary["shipped"] == 1
    assert summary["dropped"] == 1


def test_filter_dropped_includes_null_result():
    rows = [
        stage_d_store.pair_to_attempt(
            {"ts": "T", "note_a": "a", "note_b": "b", "synthesis": None}
        ),
    ]
    out = stage_d_store.filter_attempts(rows, status="dropped")
    assert len(out) == 1


def test_filter_since_works_on_pair_derived_rows():
    rows = [
        stage_d_store.pair_to_attempt(
            {"ts": "2026-05-10T03:00:00Z", "note_a": "a", "note_b": "b", "synthesis": "s1"}
        ),
        stage_d_store.pair_to_attempt(
            {"ts": "2026-05-12T03:00:00Z", "note_a": "c", "note_b": "d", "synthesis": "s2"}
        ),
    ]
    out = stage_d_store.filter_attempts(rows, since="2026-05-11")
    assert len(out) == 1
    assert out[0]["pair"]["slug_a"] == "c"
