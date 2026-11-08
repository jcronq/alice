"""Tests for the resource sampler (issue #860).

Pure parsers + a round-trip through sqlite using the real
``alice_state_db`` schema to catch UNIQUE / column-order mismatches.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alice_resources import sampler
from alice_resources.sampler import (
    PsRow,
    bg_id_for_args,
    bg_id_for_cwd,
    parse_ps_output,
    parse_transcript_turns,
    sample_processes,
    sample_tokens,
    tag_for_cmd,
)
from alice_state_db.schema import apply_schema


# ─── ps parser ─────────────────────────────────────────────────────


def test_parse_ps_output_tags_claude():
    text = (
        "  PID  PPID %CPU   RSS COMMAND COMMAND\n"
        "  101  1   12.5 51200 claude /usr/bin/claude --cwd /x/worktree\n"
        "  102  1    0.1  8192 bash /bin/bash somescript\n"
    )
    rows = parse_ps_output(text)
    assert len(rows) == 1
    assert rows[0].pid == 101
    assert rows[0].cmd_tag == "claude"
    assert rows[0].cpu_pct == 12.5
    assert rows[0].rss_kb == 51200


def test_parse_ps_output_matches_thinking_and_memory_worker():
    text = (
        "  201  1   8.0  20000 python3 /usr/bin/python3 -m alice_thinking.wake\n"
        "  202  1   2.0  10000 python3 /usr/bin/python3 -m alice_memory_worker\n"
    )
    rows = parse_ps_output(text)
    tags = sorted(r.cmd_tag for r in rows)
    assert tags == ["memory_worker", "thinking"]


def test_parse_ps_output_skips_malformed_rows():
    text = (
        "  not a row\n"
        "  bad int %CPU\n"
        "  301 1 3.1 1234 python3 -m alice_thinking.wake\n"
    )
    rows = parse_ps_output(text)
    assert len(rows) == 1 and rows[0].pid == 301


def test_tag_for_cmd_specificity_order():
    # Longer/more specific patterns match before the generic "claude".
    assert tag_for_cmd("python3 -m alice_memory_worker.something") == "memory_worker"
    assert tag_for_cmd("claude --help") == "claude"
    assert tag_for_cmd("unrelated process") is None


# ─── transcript parser ────────────────────────────────────────────


def test_parse_transcript_turns_extracts_usage():
    lines = [
        json.dumps({"type": "user", "message": {}}),
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-11-07T12:00:00Z",
            "cwd": "/home/alice/alice",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 10,
                },
            },
        }),
        json.dumps({"type": "system", "ignored": True}),
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-11-07T12:01:00Z",
            "message": {
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }),
    ]
    out = parse_transcript_turns("\n".join(lines))
    assert len(out) == 2
    first = out[0]
    assert first.turn_index == 1
    assert first.input_tokens == 100
    assert first.output_tokens == 50
    assert first.cache_creation_tokens == 5
    assert first.cache_read_tokens == 10
    assert first.cwd == "/home/alice/alice"
    assert out[1].turn_index == 3
    assert out[1].model == "claude-sonnet-4-5"


def test_parse_transcript_turns_skips_synthetic_zero():
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "usage": {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }),
    ]
    assert parse_transcript_turns("\n".join(lines)) == []


def test_parse_transcript_turns_respects_start_at():
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 1}},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"usage": {"input_tokens": 2}},
        }),
    ]
    out = parse_transcript_turns("\n".join(lines), start_at=1)
    assert len(out) == 1
    assert out[0].turn_index == 1
    assert out[0].input_tokens == 2


def test_parse_transcript_turns_tolerates_malformed_lines():
    text = "{not json\n" + json.dumps({
        "type": "assistant",
        "message": {"usage": {"input_tokens": 5, "output_tokens": 1}},
    })
    out = parse_transcript_turns(text)
    assert len(out) == 1 and out[0].input_tokens == 5


# ─── bg_id correlation ────────────────────────────────────────────


def test_bg_id_for_args_longest_path_wins():
    paths = {
        "bg_a": "/w/a",
        "bg_b": "/w/a/sub",
    }
    assert bg_id_for_args("claude --cwd /w/a/sub", paths) == "bg_b"
    assert bg_id_for_args("claude --cwd /w/a", paths) == "bg_a"


def test_bg_id_for_cwd_matches_prefix():
    paths = {"bg_x": "/home/alice/alice-worktrees/foo"}
    assert bg_id_for_cwd(
        "/home/alice/alice-worktrees/foo/src", paths,
    ) == "bg_x"
    assert bg_id_for_cwd("/unrelated", paths) is None
    assert bg_id_for_cwd(None, paths) is None


# ─── sqlite round-trip ────────────────────────────────────────────


def _mk_db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh DB with full schema applied."""
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    apply_schema(conn)
    return conn


def test_sample_processes_writes_rows(tmp_path):
    conn = _mk_db(tmp_path)
    ps_text = (
        "  PID  PPID %CPU   RSS COMMAND COMMAND\n"
        "  101  1   10.0 20000 claude claude --cwd /wt/a\n"
    )
    # Seed a worker row so bg_id correlation hits.
    conn.execute(
        "INSERT INTO workers (bg_id, worktree_path, dispatch_path, mtime_ns) "
        "VALUES ('bg_z', '/wt/a', '/dev/null', 0)"
    )
    conn.commit()
    n = sample_processes(conn, ps_runner=lambda: ps_text, now_iso="2026-11-07T00:00:00+00:00")
    assert n == 1
    rows = list(conn.execute("SELECT bg_id, pid, cmd_tag, cpu_pct, rss_mb FROM resource_samples"))
    assert rows == [("bg_z", 101, "claude", 10.0, 20000 / 1024.0)]


def test_sample_processes_ps_failure_is_noop(tmp_path):
    conn = _mk_db(tmp_path)

    def _boom() -> str:
        raise OSError("ps unavailable")

    n = sample_processes(conn, ps_runner=_boom, now_iso="2026-11-07T00:00:00+00:00")
    assert n == 0
    assert list(conn.execute("SELECT COUNT(*) FROM resource_samples")) == [(0,)]


def test_sample_tokens_idempotent(tmp_path):
    """Second call doesn't duplicate — UNIQUE(session_path, turn_index)."""
    conn = _mk_db(tmp_path)
    projects = tmp_path / "projects" / "-home-alice"
    projects.mkdir(parents=True)
    sess = projects / "abc.jsonl"
    sess.write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-11-07T00:00:00Z",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        }),
    ]))
    n1 = sample_tokens(conn, projects_dir=tmp_path / "projects")
    n2 = sample_tokens(conn, projects_dir=tmp_path / "projects")
    assert n1 == 1
    # Second call sees max turn_index=0 → start_at=1 → zero new rows.
    assert n2 == 0
    row = conn.execute(
        "SELECT session_path, turn_index, input_tokens, output_tokens FROM token_usage"
    ).fetchone()
    assert row[1] == 0 and row[2] == 10 and row[3] == 2


def test_sample_tokens_skips_old_files(tmp_path):
    """Files mtime'd beyond the scan window are ignored."""
    import os as _os
    conn = _mk_db(tmp_path)
    projects = tmp_path / "projects" / "x"
    projects.mkdir(parents=True)
    sess = projects / "old.jsonl"
    sess.write_text(json.dumps({
        "type": "assistant",
        "message": {"usage": {"input_tokens": 1}},
    }))
    # Push mtime 30 min into the past.
    stale = sampler.time.time() - (30 * 60)
    _os.utime(sess, (stale, stale))
    n = sample_tokens(conn, projects_dir=tmp_path / "projects")
    assert n == 0
