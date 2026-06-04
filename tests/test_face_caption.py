"""Smoke tests for :mod:`alice_speaking.infra.face_caption`.

Covers just the deterministic bits — the caption truncator, the YAML
frontmatter strip, and the wake-file picker. The Anthropic POST and
the LCD POST are network paths and stay untested here per the worker
spec (skip non-trivial tests).
"""

from __future__ import annotations

import pathlib

from alice_speaking.infra.face_caption import (
    MAX_CAPTION_CHARS,
    _latest_wake_path,
    _strip_frontmatter,
    _truncate_caption,
)


def test_truncate_clamps_long_input() -> None:
    long_input = "x" * 200
    out = _truncate_caption(long_input)
    assert len(out) <= MAX_CAPTION_CHARS
    assert out == "x" * MAX_CAPTION_CHARS


def test_truncate_strips_trailing_punctuation_and_whitespace() -> None:
    assert _truncate_caption("  reviewing cozyhem bugs.   ") == "reviewing cozyhem bugs"
    assert _truncate_caption("idle; queue blocked on Jason!") == (
        "idle; queue blocked on Jason"
    )


def test_truncate_collapses_internal_whitespace() -> None:
    assert _truncate_caption("foo   bar\n\n   baz") == "foo bar baz"


def test_strip_frontmatter_removes_yaml_block() -> None:
    text = "---\nmode: active\ndid_work: true\n---\n\nbody here\nmore body\n"
    assert _strip_frontmatter(text) == "body here\nmore body\n"


def test_strip_frontmatter_passes_through_when_absent() -> None:
    text = "no frontmatter at all\nsecond line\n"
    assert _strip_frontmatter(text) == text


def test_latest_wake_path_returns_most_recent_today(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    day_dir = tmp_path / now.date().isoformat()
    day_dir.mkdir()
    older = day_dir / "100000-wake.md"
    newer = day_dir / "110000-wake.md"
    older.write_text("old")
    newer.write_text("new")
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (1000, 1000))
    assert _latest_wake_path(tmp_path, now=now) == newer


def test_latest_wake_path_falls_back_to_yesterday(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    # Today directory exists but empty.
    (tmp_path / now.date().isoformat()).mkdir()
    yesterday_dir = tmp_path / (now.date() - dt.timedelta(days=1)).isoformat()
    yesterday_dir.mkdir()
    f = yesterday_dir / "230000-wake.md"
    f.write_text("from yesterday")
    assert _latest_wake_path(tmp_path, now=now) == f


def test_latest_wake_path_returns_none_when_empty(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    assert _latest_wake_path(tmp_path, now=now) is None
