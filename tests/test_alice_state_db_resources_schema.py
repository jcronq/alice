"""Issue #860 — resource_samples + token_usage schema contract."""

from __future__ import annotations

import sqlite3

from alice_state_db.schema import apply_schema


def test_schema_creates_resources_tables(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "resource_samples" in names
    assert "token_usage" in names


def test_schema_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    apply_schema(conn)  # must not raise


def test_token_usage_unique_constraint(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    conn.execute(
        "INSERT INTO token_usage (ts, bg_id, session_path, turn_index, model, "
        "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-11-07T00:00:00+00:00", "bg_a", "/x.jsonl", 0, "m", 1, 2, 0, 0),
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO token_usage (ts, bg_id, session_path, turn_index, model, "
            "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-11-07T00:00:00+00:00", "bg_a", "/x.jsonl", 0, "m", 99, 99, 0, 0),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "duplicate (session_path, turn_index) should be rejected"


def test_resource_samples_columns(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    cols = [
        r[1] for r in conn.execute("PRAGMA table_info(resource_samples)")
    ]
    assert cols == ["ts", "bg_id", "pid", "cmd_tag", "cpu_pct", "rss_mb", "source"]


def test_resource_samples_indexes(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    assert "resource_samples_ts" in idx
    assert "resource_samples_bg" in idx
    assert "token_usage_ts" in idx
    assert "token_usage_bg" in idx
