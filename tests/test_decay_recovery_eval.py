"""Tests for ``metrics.decay_recovery_eval``.

Module provenance: salvaged from a thinking-side prototype that produced the
empirical threshold sweep behind PR #470 (cosine 0.40 → 0.45). The harness is
read-only against ``cortex-index.db`` — these tests pin the M5 calculation,
threshold-gate behavior, and read-only guarantee so the harness can be reused
for future decay-pair-quality evals without re-validating from scratch.

Test surface:
- ``tokenize`` / ``word_freq`` / ``cosine_sim`` — pairing primitives
- ``identify_decay_notes`` / ``identify_accessed_notes`` — filter helpers
- ``get_trigger_keywords`` / ``inject_keywords`` — Layer 2a keyword injection
- ``simulate_pairing``                     — cosine pairing under a threshold
- ``compute_m5``                           — recovery ratio
- ``run_eval``                             — end-to-end pipeline + DB I/O
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from metrics.decay_recovery_eval import (
    compute_m5,
    cosine_sim,
    get_trigger_keywords,
    identify_accessed_notes,
    identify_decay_notes,
    inject_keywords,
    load_db,
    run_eval,
    simulate_pairing,
    tokenize,
    word_freq,
)


# ---------------------------------------------------------------------------
# Synthetic cortex-index.db fixture
# ---------------------------------------------------------------------------

# Pared-down schema. Mirrors the columns the harness reads from
# ``indexer.build_index.SCHEMA_SQL`` — the FTS5 table + triggers + the unused
# meta/links tables are omitted to keep the fixture surface tight.
_SCHEMA_SQL = """
CREATE TABLE notes (
    rowid INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    title TEXT,
    note_type TEXT,
    status TEXT,
    tags_json TEXT,
    aliases_json TEXT,
    created TEXT,
    updated TEXT,
    body TEXT
);

CREATE TABLE note_metrics (
    slug TEXT PRIMARY KEY,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_queried TEXT,
    speaking_accessed_at TEXT
);
"""


def _make_db(path: Path) -> Path:
    """Create an empty cortex-index-shaped sqlite DB at ``path``."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return path


def _insert_note(
    db_path: Path,
    slug: str,
    title: str,
    tags: list[str] | None = None,
    body: str = "",
    access_count: int = 0,
) -> None:
    """Write one note + its metrics row into the synthetic DB."""
    tags = tags or []
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO notes (slug, title, tags_json, body) VALUES (?, ?, ?, ?)",
        (slug, title, json.dumps(tags), body),
    )
    conn.execute(
        "INSERT INTO note_metrics (slug, access_count) VALUES (?, ?)",
        (slug, access_count),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    return _make_db(tmp_path / "cortex-index.db")


# ---------------------------------------------------------------------------
# Tokenization + cosine primitives
# ---------------------------------------------------------------------------


def test_tokenize_drops_short_and_punctuation():
    # Min length is 3 — "AI" and "42" are dropped; "Hello"/"World" pass.
    assert tokenize("Hello, World! AI 42") == ["hello", "world"]
    # 3-digit tokens are kept (alphanumeric).
    assert tokenize("api/123 done") == ["api", "123", "done"]


def test_tokenize_lowercases():
    assert tokenize("Memory WORKER Phase") == ["memory", "worker", "phase"]


def test_word_freq_counts_repeats():
    assert word_freq(["a", "b", "a", "a"]) == {"a": 3.0, "b": 1.0}


def test_cosine_sim_identical_is_one():
    a = word_freq(tokenize("memory worker design"))
    assert cosine_sim(a, a) == pytest.approx(1.0)


def test_cosine_sim_disjoint_is_zero():
    a = word_freq(tokenize("memory worker"))
    b = word_freq(tokenize("retrieval policy"))
    assert cosine_sim(a, b) == 0.0


def test_cosine_sim_empty_vectors_is_zero():
    assert cosine_sim({}, {}) == 0.0
    assert cosine_sim({"a": 1.0}, {}) == 0.0


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def test_identify_decay_notes_picks_decay_tag_only():
    notes = {
        "a": {"tags_json": json.dumps(["decay"]), "title": "", "body": ""},
        "b": {"tags_json": json.dumps(["reference"]), "title": "", "body": ""},
        "c": {"tags_json": json.dumps(["decay", "research"]), "title": "", "body": ""},
        "d": {"tags_json": "", "title": "", "body": ""},
    }
    assert sorted(identify_decay_notes(notes)) == ["a", "c"]


def test_identify_accessed_notes_respects_min_access():
    metrics = {
        "a": {"access_count": 0},
        "b": {"access_count": 1},
        "c": {"access_count": 2},
        "d": {"access_count": 7},
    }
    assert sorted(identify_accessed_notes(metrics, min_access=2)) == ["c", "d"]
    assert sorted(identify_accessed_notes(metrics, min_access=1)) == ["b", "c", "d"]
    assert sorted(identify_accessed_notes(metrics, min_access=99)) == []


# ---------------------------------------------------------------------------
# Keyword injection (Layer 2a hypothesis)
# ---------------------------------------------------------------------------


def test_get_trigger_keywords_parses_frontmatter():
    body = (
        "---\n"
        "slug: x\n"
        "trigger_keywords: [alpha, \"beta gamma\", delta]\n"
        "---\nBody.\n"
    )
    notes = {"x": {"body": body, "title": "", "tags_json": "[]"}}
    keywords = get_trigger_keywords(notes, "x")
    assert keywords == ["alpha", "beta gamma", "delta"]


def test_get_trigger_keywords_missing_returns_empty():
    notes = {"x": {"body": "no frontmatter here", "title": "", "tags_json": "[]"}}
    assert get_trigger_keywords(notes, "x") == []
    assert get_trigger_keywords(notes, "absent") == []


def test_inject_keywords_appends_to_title():
    assert inject_keywords("memory design", ["worker", "phase"]) == (
        "memory design worker phase"
    )


def test_inject_keywords_empty_list_is_noop():
    assert inject_keywords("memory design", []) == "memory design"


# ---------------------------------------------------------------------------
# load_db round-trip
# ---------------------------------------------------------------------------


def test_load_db_empty(empty_db: Path):
    data = load_db(str(empty_db))
    assert data == {"notes": {}, "metrics": {}}


def test_load_db_roundtrip(empty_db: Path):
    _insert_note(
        empty_db, "alpha", "Memory worker design",
        tags=["decay"], body="frontmatter body", access_count=3,
    )
    data = load_db(str(empty_db))
    assert "alpha" in data["notes"]
    assert data["notes"]["alpha"]["title"] == "Memory worker design"
    assert json.loads(data["notes"]["alpha"]["tags_json"]) == ["decay"]
    assert data["metrics"]["alpha"]["access_count"] == 3


# ---------------------------------------------------------------------------
# simulate_pairing — threshold gating
# ---------------------------------------------------------------------------


def test_simulate_pairing_threshold_gate():
    """Lower threshold should never produce *fewer* inbound matches than a
    higher threshold — the gate is monotonic."""
    notes = {
        "decayed": {"title": "memory worker design"},
        "a": {"title": "memory worker phase"},
        "b": {"title": "memory architecture"},
        "c": {"title": "unrelated cooking recipe"},
    }
    counts_low = simulate_pairing(
        ["decayed"], ["a", "b", "c"], notes,
        thresholds=[0.1], keyword_injection=False,
    )
    counts_high = simulate_pairing(
        ["decayed"], ["a", "b", "c"], notes,
        thresholds=[0.99], keyword_injection=False,
    )
    assert counts_low[0.1]["decayed"] >= counts_high[0.99]["decayed"]
    # Concrete: at 0.99, no non-identical title clears the gate.
    assert counts_high[0.99]["decayed"] == 0
    # At 0.1, the two memory-* siblings should match.
    assert counts_low[0.1]["decayed"] >= 2


def test_simulate_pairing_keyword_injection_widens_pairing():
    """Injecting matching keywords into the accessed title should raise the
    inbound count vs the control run."""
    notes = {
        "decayed": {
            "title": "memory worker design",
            "body": "",
            "tags_json": "[]",
        },
        "a": {
            "title": "totally orthogonal note about cats",
            "body": "trigger_keywords: [memory, worker, design]",
            "tags_json": "[]",
        },
    }
    control = simulate_pairing(
        ["decayed"], ["a"], notes,
        thresholds=[0.5], keyword_injection=False,
    )
    injected = simulate_pairing(
        ["decayed"], ["a"], notes,
        thresholds=[0.5], keyword_injection=True,
    )
    # Without injection the titles share no tokens → no pair.
    assert control[0.5]["decayed"] == 0
    # With injection the keywords lift the cosine over 0.5.
    assert injected[0.5]["decayed"] >= 1


# ---------------------------------------------------------------------------
# compute_m5 — recovery ratio
# ---------------------------------------------------------------------------


def test_compute_m5_recovered_threshold_is_two():
    """``compute_m5`` treats inbound_count >= 2 as recovered."""
    decay_slugs = ["recovered_a", "recovered_b", "unrecovered_c"]
    inbound = {"recovered_a": 2, "recovered_b": 5, "unrecovered_c": 1}
    metrics = {
        "recovered_a": {"access_count": 10},
        "recovered_b": {"access_count": 6},
        "unrecovered_c": {"access_count": 2},
    }
    result = compute_m5(decay_slugs, inbound, metrics)
    assert result["recovered_count"] == 2
    assert result["unrecovered_count"] == 1
    assert result["recovered_mean"] == pytest.approx(8.0)
    assert result["unrecovered_mean"] == pytest.approx(2.0)
    assert result["m5"] == pytest.approx(4.0)


def test_compute_m5_no_unrecovered_returns_inf():
    decay_slugs = ["a"]
    inbound = {"a": 3}
    metrics = {"a": {"access_count": 5}}
    result = compute_m5(decay_slugs, inbound, metrics)
    assert result["m5"] == math.inf


def test_compute_m5_all_unrecovered_is_zero():
    decay_slugs = ["a"]
    inbound = {"a": 0}
    metrics = {"a": {"access_count": 4}}
    result = compute_m5(decay_slugs, inbound, metrics)
    # recovered_mean is 0, unrecovered_mean is 4 → ratio is 0.0.
    assert result["m5"] == 0.0
    assert result["recovered_count"] == 0


# ---------------------------------------------------------------------------
# End-to-end via run_eval
# ---------------------------------------------------------------------------


def test_run_eval_empty_db(empty_db: Path):
    """Empty vault → empty result without crashing.

    With zero decay notes there is nothing to evaluate — the harness returns
    one row per threshold with zero counts (and M5=inf since unrecovered_mean
    is 0)."""
    results = run_eval(str(empty_db), thresholds=[0.5])
    assert len(results) == 1
    row = results[0]
    assert row["threshold"] == 0.5
    assert row["recovered_count"] == 0
    assert row["unrecovered_count"] == 0
    assert row["total_pairs"] == 0


def test_run_eval_detects_recovered_pair(empty_db: Path):
    """One decayed note + two accessed notes that title-cosine above threshold
    → pair is detected and M5 numerator picks up the recovered note's
    access_count."""
    _insert_note(
        empty_db, "decayed",
        title="memory worker design",
        tags=["decay"],
        access_count=12,
    )
    _insert_note(
        empty_db, "sibling_a",
        title="memory worker phase",
        tags=["reference"],
        access_count=5,
    )
    _insert_note(
        empty_db, "sibling_b",
        title="memory worker architecture",
        tags=["reference"],
        access_count=5,
    )
    # Add an unrecovered decay note so unrecovered_mean is non-zero and M5
    # is finite — without this M5 would be inf and we can't compare numbers.
    _insert_note(
        empty_db, "lonely_decay",
        title="completely orthogonal topic",
        tags=["decay"],
        access_count=3,
    )

    results = run_eval(str(empty_db), thresholds=[0.3], min_access_count=2)
    row = results[0]
    assert row["recovered_count"] == 1
    assert row["unrecovered_count"] == 1
    assert row["total_pairs"] == 1
    # Recovered mean = 12, unrecovered mean = 3, M5 = 4.0.
    assert row["m5"] == pytest.approx(12.0 / 3.0)


def test_run_eval_threshold_gate_changes_pair_count(empty_db: Path):
    """Lower threshold → more pairs (monotonic). The harness's sweep output
    must reflect that as the threshold rises, fewer notes recover."""
    _insert_note(
        empty_db, "decayed",
        title="memory worker design",
        tags=["decay"], access_count=10,
    )
    # Three siblings with descending title similarity.
    _insert_note(
        empty_db, "near",
        title="memory worker phase",
        tags=["reference"], access_count=3,
    )
    _insert_note(
        empty_db, "mid",
        title="memory architecture",
        tags=["reference"], access_count=3,
    )
    _insert_note(
        empty_db, "far",
        title="design patterns",
        tags=["reference"], access_count=3,
    )

    results = run_eval(
        str(empty_db),
        thresholds=[0.1, 0.5, 0.99],
        min_access_count=2,
    )
    by_t = {r["threshold"]: r for r in results}
    # Total pairs is monotonically non-increasing in threshold.
    assert by_t[0.1]["total_pairs"] >= by_t[0.5]["total_pairs"]
    assert by_t[0.5]["total_pairs"] >= by_t[0.99]["total_pairs"]
    # At threshold 0.99, nothing non-identical can pair.
    assert by_t[0.99]["total_pairs"] == 0


def test_run_eval_is_read_only(empty_db: Path):
    """The harness must never mutate the DB or any note frontmatter.

    We don't have a real vault here, but the harness only touches the DB —
    so checking that the DB file is byte-identical after a run is sufficient
    to prove read-only behavior. Additionally, the in-memory notes dict
    returned by ``load_db`` is independent of the harness state and any
    mutation to it would not leak back into the DB.
    """
    _insert_note(
        empty_db, "decayed",
        title="memory worker design",
        tags=["decay"], access_count=4,
        body="trigger_keywords: [memory, worker]",
    )
    _insert_note(
        empty_db, "sibling",
        title="memory worker phase",
        tags=["reference"], access_count=3,
        body="trigger_keywords: [phase]",
    )

    before_bytes = empty_db.read_bytes()
    run_eval(str(empty_db), thresholds=[0.3], keyword_injection=True)
    after_bytes = empty_db.read_bytes()
    assert before_bytes == after_bytes


def test_run_eval_is_reproducible(empty_db: Path):
    """Two runs over the same DB at the same threshold must produce identical
    output. No nondeterministic ordering, hashing, or wall-clock leakage."""
    _insert_note(
        empty_db, "decayed",
        title="memory worker design",
        tags=["decay"], access_count=10,
    )
    _insert_note(
        empty_db, "a",
        title="memory worker phase",
        tags=["reference"], access_count=4,
    )
    _insert_note(
        empty_db, "b",
        title="memory worker architecture",
        tags=["reference"], access_count=4,
    )

    first = run_eval(str(empty_db), thresholds=[0.3, 0.6])
    second = run_eval(str(empty_db), thresholds=[0.3, 0.6])
    assert first == second
