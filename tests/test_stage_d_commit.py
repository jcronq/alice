"""Tests for ``commit_stage_d_synthesis`` and the post-wake invariant.

The new single-call entry point that makes the dual-judge gate
structurally unskippable. Mocks judges entirely; no live LLM dispatch.

Coverage:

- shipped → vault note written, attempts.jsonl carries the slug, pairs
  log line with synthesis=slug
- dropped_agreement_reject → no vault note, pairs log line with
  synthesis=null
- dropped_disagreement_exhausted → same as agreement_reject
- fallback (judge raises) → vault note written from draft, no
  attempts.jsonl write, judge-failures.jsonl carries the reason, pairs
  log line with synthesis=slug
- pairs log path defaulting to today's file
- ``find_unaudited_stage_d_notes`` correctly flags a hand-written note
  with no log evidence and clears once an attempts.jsonl ship line exists
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib

from alice_thinking.stage_d_pipeline import (
    commit_stage_d_synthesis,
)
from alice_thinking.stage_d_invariant import find_unaudited_stage_d_notes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ship():
    return {"tier": "T1", "novel": True, "reason": "ship", "decision": "ship"}


def _reject():
    return {"tier": "T3", "novel": False, "reason": "reject", "decision": "reject"}


def _read_jsonl(p: pathlib.Path) -> list[dict]:
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _make_paths(tmp: pathlib.Path):
    return {
        "vault_root": tmp / "vault",
        "pairs_log_path": tmp / "pairs.jsonl",
        "attempts_log_path": tmp / "attempts.jsonl",
        "judge_failures_log_path": tmp / "judge-failures.jsonl",
    }


def _note_md(slug_a: str, slug_b: str, title: str = "Synthesis") -> str:
    today = _dt.date.today().isoformat()
    return (
        "---\n"
        f"title: {title}\n"
        f"source: stage-d\n"
        f"note_a: {slug_a}\n"
        f"note_b: {slug_b}\n"
        f"created: {today}\n"
        "tags: [research, stage-d]\n"
        "---\n"
        "# Body\n\n"
        "Some synthesis content.\n"
    )


# ---------------------------------------------------------------------------
# commit_stage_d_synthesis — shipped path
# ---------------------------------------------------------------------------


def test_commit_shipped_writes_vault_note_and_updates_attempts(tmp_path):
    p = _make_paths(tmp_path)
    note_md = _note_md("research/a", "research/b")

    res = commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="A bridges B because of foo.",
        output_slug="2026-05-11-a-x-b",
        note_content=note_md,
        judge_qwen_fn=lambda **kw: _ship(),
        judge_haiku_fn=lambda **kw: _ship(),
        **p,
    )

    assert res.outcome == "shipped"
    assert res.synthesis_slug == "research/2026-05-11-a-x-b"
    assert res.attempt_id is not None
    assert res.fallback_reason is None

    vault_file = p["vault_root"] / "research" / "2026-05-11-a-x-b.md"
    assert vault_file.is_file()
    assert "source: stage-d" in vault_file.read_text()

    attempts = _read_jsonl(p["attempts_log_path"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "shipped"
    assert attempts[0]["shipped_slug"] == "research/2026-05-11-a-x-b"

    pairs = _read_jsonl(p["pairs_log_path"])
    assert len(pairs) == 1
    assert pairs[0]["note_a"] == "research/a"
    assert pairs[0]["note_b"] == "research/b"
    assert pairs[0]["synthesis"] == "research/2026-05-11-a-x-b"

    assert not p["judge_failures_log_path"].exists()


# ---------------------------------------------------------------------------
# Drop outcomes — no vault note, pairs log with synthesis=null
# ---------------------------------------------------------------------------


def test_commit_agreement_reject_skips_vault_write(tmp_path):
    p = _make_paths(tmp_path)
    res = commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="weak",
        output_slug="2026-05-11-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        judge_qwen_fn=lambda **kw: _reject(),
        judge_haiku_fn=lambda **kw: _reject(),
        **p,
    )

    assert res.outcome == "dropped_agreement_reject"
    assert res.synthesis_slug is None
    vault_file = p["vault_root"] / "research" / "2026-05-11-a-x-b.md"
    assert not vault_file.exists()

    pairs = _read_jsonl(p["pairs_log_path"])
    assert len(pairs) == 1
    assert pairs[0]["synthesis"] is None


def test_commit_disagreement_exhausted_skips_vault_write(tmp_path):
    p = _make_paths(tmp_path)
    # Persistent disagreement: qwen ships, haiku rejects on every attempt.
    res = commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="mid",
        output_slug="2026-05-11-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        max_attempts=3,
        judge_qwen_fn=lambda **kw: _ship(),
        judge_haiku_fn=lambda **kw: _reject(),
        **p,
    )

    assert res.outcome == "dropped_disagreement_exhausted"
    assert res.synthesis_slug is None
    vault_file = p["vault_root"] / "research" / "2026-05-11-a-x-b.md"
    assert not vault_file.exists()

    attempts = _read_jsonl(p["attempts_log_path"])
    assert len(attempts) == 3  # one per attempt
    pairs = _read_jsonl(p["pairs_log_path"])
    assert len(pairs) == 1
    assert pairs[0]["synthesis"] is None


# ---------------------------------------------------------------------------
# Fallback — judges raise, vault note still gets written, failure logged
# ---------------------------------------------------------------------------


def test_commit_fallback_when_judges_raise(tmp_path):
    p = _make_paths(tmp_path)

    def boom(**kw):
        raise RuntimeError("Qwen LAN unreachable")

    res = commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="A bridges B because of foo.",
        output_slug="2026-05-11-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        judge_qwen_fn=boom,
        judge_haiku_fn=boom,
        **p,
    )

    assert res.outcome == "fallback"
    assert res.synthesis_slug == "research/2026-05-11-a-x-b"
    assert res.attempt_id is None
    assert "Qwen LAN unreachable" in (res.fallback_reason or "")

    vault_file = p["vault_root"] / "research" / "2026-05-11-a-x-b.md"
    assert vault_file.is_file()

    # No attempts log entry — judge never ran successfully.
    assert _read_jsonl(p["attempts_log_path"]) == []
    # Judge-failures log carries one entry.
    failures = _read_jsonl(p["judge_failures_log_path"])
    assert len(failures) == 1
    assert failures[0]["slug_a"] == "research/a"
    assert failures[0]["slug_b"] == "research/b"
    assert "RuntimeError" in failures[0]["reason"]

    pairs = _read_jsonl(p["pairs_log_path"])
    assert len(pairs) == 1
    assert pairs[0]["synthesis"] == "research/2026-05-11-a-x-b"


# ---------------------------------------------------------------------------
# Vault write — atomic temp+rename leaves no .tmp behind on success
# ---------------------------------------------------------------------------


def test_commit_atomic_write_leaves_no_tmp_file(tmp_path):
    p = _make_paths(tmp_path)
    commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="A bridges B because of foo.",
        output_slug="2026-05-11-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        judge_qwen_fn=lambda **kw: _ship(),
        judge_haiku_fn=lambda **kw: _ship(),
        **p,
    )
    research_dir = p["vault_root"] / "research"
    tmps = list(research_dir.glob("*.tmp"))
    assert tmps == []


# ---------------------------------------------------------------------------
# Invariant check
# ---------------------------------------------------------------------------


def test_invariant_flags_hand_written_stage_d_note(tmp_path):
    paths = _make_paths(tmp_path)
    research = paths["vault_root"] / "research"
    research.mkdir(parents=True)
    note = research / "2026-05-11-hand-written.md"
    today = _dt.date.today().isoformat()
    note.write_text(
        "---\n"
        "title: Hand-written\n"
        "source: stage-d\n"
        "note_a: research/a\n"
        "note_b: research/b\n"
        f"created: {today}\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    bad = find_unaudited_stage_d_notes(
        date=_dt.date.today(),
        vault_root=paths["vault_root"],
        attempts_log_path=paths["attempts_log_path"],
        judge_failures_log_path=paths["judge_failures_log_path"],
    )
    assert len(bad) == 1
    assert bad[0]["slug"] == "2026-05-11-hand-written"
    assert bad[0]["note_a"] == "research/a"
    assert bad[0]["note_b"] == "research/b"


def test_invariant_clears_after_attempts_log_ship_line(tmp_path):
    paths = _make_paths(tmp_path)
    # Write a note via the pipeline so attempts.jsonl carries the slug.
    commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="A bridges B because of foo.",
        output_slug=f"{_dt.date.today().isoformat()}-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        judge_qwen_fn=lambda **kw: _ship(),
        judge_haiku_fn=lambda **kw: _ship(),
        **paths,
    )

    bad = find_unaudited_stage_d_notes(
        date=_dt.date.today(),
        vault_root=paths["vault_root"],
        attempts_log_path=paths["attempts_log_path"],
        judge_failures_log_path=paths["judge_failures_log_path"],
    )
    assert bad == []


def test_invariant_clears_after_judge_failure_fallback(tmp_path):
    paths = _make_paths(tmp_path)

    def boom(**kw):
        raise RuntimeError("transient")

    commit_stage_d_synthesis(
        slug_a="research/a",
        slug_b="research/b",
        source_a_text="A",
        source_b_text="B",
        draft_synthesis="A bridges B because of foo.",
        output_slug=f"{_dt.date.today().isoformat()}-a-x-b",
        note_content=_note_md("research/a", "research/b"),
        judge_qwen_fn=boom,
        judge_haiku_fn=boom,
        **paths,
    )

    bad = find_unaudited_stage_d_notes(
        date=_dt.date.today(),
        vault_root=paths["vault_root"],
        attempts_log_path=paths["attempts_log_path"],
        judge_failures_log_path=paths["judge_failures_log_path"],
    )
    assert bad == []
