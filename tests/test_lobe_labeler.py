"""Unit tests for ``alice_viewer.lobe_labeler``.

The Qwen call is never exercised — every test passes a stub via
``llm_call`` so we don't depend on the LAN endpoint or google-adk
being installed.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from alice_viewer import lobe_labeler


# ---------------------------------------------------------------------------
# extract_first_chunk
# ---------------------------------------------------------------------------


def test_extract_first_chunk_strips_frontmatter(tmp_path: pathlib.Path) -> None:
    note = tmp_path / "n.md"
    note.write_text(
        "---\n"
        "title: ignored\n"
        "tags: [a, b]\n"
        "---\n"
        "Real body starts here."
    )
    out = lobe_labeler.extract_first_chunk(note)
    assert out == "Real body starts here."
    assert "title:" not in out


def test_extract_first_chunk_strips_headings_and_wikilinks(
    tmp_path: pathlib.Path,
) -> None:
    note = tmp_path / "n.md"
    note.write_text("# Header\n\nSee [[other-note]] for details.")
    out = lobe_labeler.extract_first_chunk(note)
    assert out.startswith("Header")
    # Wikilink brackets gone, target text preserved.
    assert "other-note" in out
    assert "[[" not in out


def test_extract_first_chunk_truncates_at_word_boundary(
    tmp_path: pathlib.Path,
) -> None:
    note = tmp_path / "n.md"
    note.write_text("alpha beta gamma delta epsilon zeta eta theta iota kappa")
    out = lobe_labeler.extract_first_chunk(note, max_chars=20)
    assert out.endswith("…")
    # Should have broken at a space, not mid-word — so the cut should
    # only contain whole words from the input.
    body = out.rstrip("…").strip()
    assert all(w in note.read_text().split() for w in body.split())


def test_extract_first_chunk_missing_file_returns_empty(
    tmp_path: pathlib.Path,
) -> None:
    out = lobe_labeler.extract_first_chunk(tmp_path / "no-such.md")
    assert out == ""


# ---------------------------------------------------------------------------
# format_lobe_prompt
# ---------------------------------------------------------------------------


def test_format_lobe_prompt_includes_titles_and_snippets() -> None:
    members = [
        {"label": "deload-week", "snippet": "Reduce intensity 40% for one week."},
        {"label": "rpe-target", "snippet": "Hit RPE 8 on top sets."},
    ]
    prompt = lobe_labeler.format_lobe_prompt(members)
    assert "deload-week" in prompt
    assert "Reduce intensity" in prompt
    assert "rpe-target" in prompt
    assert "Hit RPE 8" in prompt
    # Sanity: the trailing instruction is what tells the LLM to answer.
    assert prompt.rstrip().endswith("Phrase:")


def test_format_lobe_prompt_caps_at_max_members_fed() -> None:
    members = [
        {"label": f"note-{i:03d}", "snippet": f"snippet {i}"}
        for i in range(lobe_labeler.MAX_MEMBERS_FED + 25)
    ]
    prompt = lobe_labeler.format_lobe_prompt(members)
    # Last note that should be included.
    assert f"note-{lobe_labeler.MAX_MEMBERS_FED - 1:03d}" in prompt
    # First note that should be excluded.
    assert f"note-{lobe_labeler.MAX_MEMBERS_FED:03d}" not in prompt


def test_format_lobe_prompt_handles_empty_snippet() -> None:
    members = [{"label": "title", "snippet": ""}]
    prompt = lobe_labeler.format_lobe_prompt(members)
    assert "title" in prompt
    assert "(no excerpt)" in prompt


# ---------------------------------------------------------------------------
# sanitise_label
# ---------------------------------------------------------------------------


def test_sanitise_label_strips_quotes_and_lowercases() -> None:
    assert lobe_labeler.sanitise_label('"Fitness Deload Cycles."') == "fitness deload cycles"


def test_sanitise_label_collapses_whitespace() -> None:
    assert lobe_labeler.sanitise_label("  alpha   beta   gamma  ") == "alpha beta gamma"


def test_sanitise_label_caps_length() -> None:
    raw = "a-very-very-long-phrase " * 20
    out = lobe_labeler.sanitise_label(raw)
    assert len(out) <= lobe_labeler.MAX_LABEL_CHARS


def test_sanitise_label_empty_input_returns_empty() -> None:
    assert lobe_labeler.sanitise_label("") == ""
    assert lobe_labeler.sanitise_label("   ") == ""


# ---------------------------------------------------------------------------
# compute_label_async
# ---------------------------------------------------------------------------


def test_compute_label_async_passes_prompt_and_sanitises() -> None:
    seen: list[str] = []

    async def stub(prompt: str) -> str:
        seen.append(prompt)
        return '"Fitness Deload Cycles."'

    members = [{"label": "deload", "snippet": "intensity drop"}]
    result = asyncio.run(
        lobe_labeler.compute_label_async(members, llm_call=stub)
    )
    assert result == "fitness deload cycles"
    assert len(seen) == 1
    # Prompt must have included the member info.
    assert "deload" in seen[0]


def test_compute_label_async_returns_empty_on_exception() -> None:
    async def stub(prompt: str) -> str:
        raise RuntimeError("endpoint down")

    members = [{"label": "x", "snippet": "y"}]
    result = asyncio.run(
        lobe_labeler.compute_label_async(members, llm_call=stub)
    )
    assert result == ""


def test_compute_label_async_skips_when_no_members() -> None:
    async def stub(prompt: str) -> str:
        pytest.fail("should not be called when members is empty")

    result = asyncio.run(lobe_labeler.compute_label_async([], llm_call=stub))
    assert result == ""
