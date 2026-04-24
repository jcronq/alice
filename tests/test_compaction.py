"""Tests for the compaction helper module."""

from __future__ import annotations

import pathlib

from alice_speaking import compaction
from alice_speaking.turn_log import Turn


def _turn(inbound: str = "hi", outbound: str = "hello") -> Turn:
    return Turn(
        ts=0.0,
        sender_number="+1",
        sender_name="Owner",
        inbound=inbound,
        outbound=outbound,
    )


# ---------------------------------------------------------------- should_compact


def test_should_compact_fires_above_threshold() -> None:
    assert compaction.should_compact({"input_tokens": 200_000}, 150_000) is True


def test_should_compact_false_below_threshold() -> None:
    assert compaction.should_compact({"input_tokens": 50_000}, 150_000) is False


def test_should_compact_false_at_threshold() -> None:
    # Strict >, not >=, per the design prose.
    assert compaction.should_compact({"input_tokens": 150_000}, 150_000) is False


def test_should_compact_missing_usage() -> None:
    assert compaction.should_compact(None, 150_000) is False
    assert compaction.should_compact({}, 150_000) is False


def test_should_compact_malformed_usage() -> None:
    assert compaction.should_compact({"input_tokens": "lots"}, 150_000) is False
    assert compaction.should_compact({"input_tokens": None}, 150_000) is False
    assert compaction.should_compact("not a dict", 150_000) is False


# -------------------------------------------------------- preamble builders


def test_bootstrap_preamble_with_turns() -> None:
    result = compaction.build_bootstrap_preamble([_turn("hi", "hello")])
    assert "Recent conversation" in result
    assert "[Owner] hi" in result
    assert "[alice] hello" in result


def test_bootstrap_preamble_empty() -> None:
    assert compaction.build_bootstrap_preamble([]) == ""


def test_summary_preamble_includes_summary_and_tail() -> None:
    result = compaction.build_summary_preamble(
        "four-part summary body",
        [_turn("i ate breakfast", "logged")],
    )
    assert "Context summary" in result
    assert "four-part summary body" in result
    assert "Recent turns:" in result
    assert "[alice] logged" in result


def test_summary_preamble_without_recent_turns() -> None:
    result = compaction.build_summary_preamble("summary only", [])
    assert "summary only" in result
    # With no turn tail we skip the "Recent turns:" divider entirely.
    assert "Recent turns:" not in result


# -------------------------------------------------------------- read/write summary


def test_read_summary_missing(tmp_path: pathlib.Path) -> None:
    assert compaction.read_summary_if_any(tmp_path / "nope.md") is None


def test_write_then_read_summary(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "summary.md"
    compaction.write_summary(path, "body text")
    assert compaction.read_summary_if_any(path) == "body text"


def test_write_summary_creates_parent(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "a" / "b" / "summary.md"
    compaction.write_summary(path, "hi")
    assert path.is_file()


def test_read_empty_summary_returns_none(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "summary.md"
    path.write_text("   \n\n")
    assert compaction.read_summary_if_any(path) is None
