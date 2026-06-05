"""Tests for the decay-intervention Phase 3 passes.

Phase 3 adds three sequential passes to the memory worker pipeline:

  1. **Archive** (Stage D) — moves low-signal decayed notes to
     ``cortex-memory/archive/``.
  2. **Extraction** (Stage D) — intentional no-op stub; the dry-run
     validation showed filename-keyword grouping produces zero matches
     between decayed and accessed cohorts. See
     ``cortex-memory/research/2026-06-04-decay-extraction-pass-breakdown``.
  3. **Pairing** (Stage D) — directory + keyword grouping; emits a
     ``decay_event`` to ``memory/events.jsonl`` for Stage C consumption.

Plus a Stage C integration point: ``atomize()`` consults
``_decay_priority_score()`` to reorder its candidates so decayed notes
get atomization priority within one tick's cap.

Style mirrors :mod:`tests.test_memory_worker_stage_c` —  real
filesystem ops on a ``tmp_path`` mind, no fs mocking.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_thinking.memory_worker import journal as journal_mod
from alice_thinking.memory_worker import stage_c, stage_d


# ---------- shared fixtures ----------


@pytest.fixture(autouse=True)
def _reset_journal_verifiers():
    journal_mod.reset_verifiers_to_phase1_defaults()
    yield
    journal_mod.reset_verifiers_to_phase1_defaults()


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "cortex-memory" / "research").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "reference").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "projects").mkdir(parents=True)
    (tmp_path / "inner" / "state").mkdir(parents=True)
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


def _write_note(
    mind: pathlib.Path,
    rel: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    created: str = "2026-04-01",
    last_accessed: str = "2026-05-01",
    access_count: int = 0,
    status: str | None = None,
    note_type: str | None = None,
    body: str = "Body content.\n",
    extra_fm: dict | None = None,
) -> pathlib.Path:
    path = mind / "cortex-memory" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = ["---"]
    if title:
        fm.append(f"title: {title}")
    if tags is not None:
        fm.append(f"tags: [{', '.join(tags)}]")
    fm.append(f"created: {created}")
    fm.append(f"last_accessed: {last_accessed}")
    fm.append(f"access_count: {access_count}")
    if status:
        fm.append(f"status: {status}")
    if note_type:
        fm.append(f"note_type: {note_type}")
    if extra_fm:
        for k, v in extra_fm.items():
            fm.append(f"{k}: {v}")
    fm.append("---")
    fm.append("")
    text = "\n".join(fm) + body
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- title keyword extraction ----------


def test_extract_title_keywords_strips_date_prefix_and_stopwords():
    """Date prefix and stop words drop out; meaningful tokens survive."""
    out = stage_d._extract_title_keywords(
        "2026-06-04-decay-phase3-dry-run-validation"
    )
    assert out == ["decay", "phase3", "dry", "run", "validation"]


def test_extract_title_keywords_drops_short_tokens():
    """Tokens of length <=2 don't carry signal — filter them."""
    out = stage_d._extract_title_keywords("an-x-of-the-cat")
    # 'cat' survives; 'an'/'x'/'of'/'the' do not.
    assert out == ["cat"]


def test_extract_major_topic_picks_longest_specific_keyword():
    """Major topic = longest token >4 chars."""
    assert (
        stage_d._extract_major_topic(["decay", "phase", "validation"])
        == "validation"
    )


def test_extract_major_topic_falls_back_to_first_keyword():
    """No keyword exceeds 4 chars → fall back to the first."""
    assert stage_d._extract_major_topic(["abc", "def"]) == "abc"


def test_extract_major_topic_empty_input_returns_empty():
    assert stage_d._extract_major_topic([]) == ""


# ---------- archive pass eligibility ----------


def test_is_archive_eligible_status_superseded():
    assert stage_d._is_archive_eligible({"status": "superseded"}, "anything") is True


def test_is_archive_eligible_status_resolved():
    assert stage_d._is_archive_eligible({"status": "resolved"}, "anything") is True


def test_is_archive_eligible_investigation_with_no_action():
    fm = {"note_type": "investigation"}
    assert stage_d._is_archive_eligible(fm, "Just observations.\n") is True


def test_not_archive_eligible_investigation_with_next_step():
    fm = {"note_type": "investigation"}
    body = "Findings.\n\n## Next steps\n- follow up.\n"
    assert stage_d._is_archive_eligible(fm, body) is False


def test_is_archive_eligible_redirect_stub():
    assert stage_d._is_archive_eligible({}, "[[somewhere-else]]\n") is True


def test_not_archive_eligible_live_note():
    assert stage_d._is_archive_eligible({}, "Real prose here.\n") is False


def test_identify_archive_candidates_filters_population(mind: pathlib.Path):
    """Out of a mixed-decay batch, only the eligible ones come back."""
    a = _write_note(mind, "research/old-design.md", status="superseded")
    b = _write_note(mind, "research/active-design.md", body="Live notes.\n")
    c = _write_note(
        mind,
        "research/old-redirect.md",
        body="[[active-design]]\n",
    )
    out = stage_d._identify_archive_candidates([a, b, c])
    assert set(out) == {a, c}


# ---------- archive pass execution ----------


def test_archive_decayed_note_moves_to_archive_dir(mind: pathlib.Path):
    """A no-inbound, archive-eligible note moves under archive/."""
    note = _write_note(
        mind,
        "research/superseded-thing.md",
        status="superseded",
        body="Old content.\n",
    )
    moved = stage_d._archive_decayed_note(mind, note, journal_path=None)
    assert moved is True
    assert not note.exists()
    archived = mind / "cortex-memory" / "archive" / "superseded-thing.md"
    assert archived.is_file()
    text = archived.read_text(encoding="utf-8")
    assert "archived:" in text
    assert "Old content." in text


def test_archive_decayed_note_skips_if_inbound_links_exist(mind: pathlib.Path):
    """A referenced note stays in place — decay archive is for the
    orphaned-and-superseded tail only."""
    referenced = _write_note(
        mind, "research/cited-note.md", status="superseded"
    )
    _write_note(
        mind,
        "research/referrer.md",
        body="See [[cited-note]] for the original.\n",
    )
    moved = stage_d._archive_decayed_note(mind, referenced, journal_path=None)
    assert moved is False
    assert referenced.is_file()


def test_archive_decayed_note_journals(mind: pathlib.Path):
    """Archive-decay produces a committed journal entry."""
    note = _write_note(
        mind, "research/done-thing.md", status="resolved", body="x\n"
    )
    journal_path = mind / "journal.jsonl"
    assert stage_d._archive_decayed_note(mind, note, journal_path=journal_path) is True
    entries = journal_mod.load(journal_path)
    assert len(entries) == 1
    assert entries[0].op == "archive-decay"
    assert entries[0].status == journal_mod.COMMITTED


# ---------- extraction pass (no-op) ----------


def test_extraction_candidates_is_noop(mind: pathlib.Path):
    """Extraction is intentionally a stub — see
    ``decay-extraction-pass-breakdown``. It must return an empty list
    regardless of input."""
    a = _write_note(mind, "research/foo-validation.md")
    b = _write_note(mind, "research/bar-validation.md", access_count=12)
    vault = mind / "cortex-memory"
    assert stage_d._identify_extraction_candidates([a, b], vault) == []


# ---------- pairing pass ----------


def test_select_decay_pair_same_topic_overlap(mind: pathlib.Path):
    """Two decayed notes in the same directory + same major topic +
    shared keywords pair up."""
    vault = mind / "cortex-memory"
    a = _write_note(mind, "research/2026-04-01-decay-phase3-validation.md")
    b = _write_note(mind, "research/2026-04-02-decay-phase3-experiment-validation.md")
    pair = stage_d._select_decay_pair([a, b], vault)
    assert pair is not None
    paired_a, paired_b, score = pair
    assert {paired_a, paired_b} == {a, b}
    assert score > 0


def test_select_decay_pair_cross_topic_within_directory(mind: pathlib.Path):
    """No same-topic match → fall back to ≥2 shared keywords within dir."""
    vault = mind / "cortex-memory"
    a = _write_note(mind, "research/2026-04-01-alpha-beta-gamma-validation.md")
    b = _write_note(mind, "research/2026-04-02-alpha-beta-experiment.md")
    pair = stage_d._select_decay_pair([a, b], vault)
    # alpha + beta is 2 shared, and they don't share a major topic
    # because 'validation' and 'experiment' are different majors.
    assert pair is not None
    paired_a, paired_b, _score = pair
    assert {paired_a, paired_b} == {a, b}


def test_select_decay_pair_none_when_no_overlap(mind: pathlib.Path):
    vault = mind / "cortex-memory"
    a = _write_note(mind, "research/2026-04-01-alpha.md")
    b = _write_note(mind, "research/2026-04-02-omega.md")
    assert stage_d._select_decay_pair([a, b], vault) is None


def test_select_decay_pair_excludes_fitness_domain(mind: pathlib.Path):
    """Fitness-tagged notes are exempt from decay pairing."""
    vault = mind / "cortex-memory"
    a = _write_note(
        mind,
        "research/2026-04-01-bench-press-progression.md",
        tags=["fitness"],
    )
    b = _write_note(
        mind,
        "research/2026-04-02-bench-press-variations.md",
        tags=["workout"],
    )
    assert stage_d._select_decay_pair([a, b], vault) is None


def test_select_decay_pair_singleton_returns_none(mind: pathlib.Path):
    a = _write_note(mind, "research/lonely.md")
    assert stage_d._select_decay_pair([a], mind / "cortex-memory") is None


# ---------- iter decayed notes ----------


def test_iter_decayed_notes_filters_by_access_and_window(mind: pathlib.Path):
    """Decayed = stale ``last_accessed`` AND ``access_count`` <=1."""
    today = datetime.date(2026, 6, 1)
    decayed = _write_note(
        mind,
        "research/decayed.md",
        last_accessed="2026-04-01",
        access_count=0,
    )
    hot = _write_note(
        mind,
        "research/hot.md",
        last_accessed="2026-04-01",
        access_count=15,
    )
    recent = _write_note(
        mind,
        "research/recent.md",
        last_accessed="2026-05-30",
        access_count=0,
    )
    out = stage_d._iter_decayed_notes(
        mind / "cortex-memory", today, window_days=7
    )
    assert decayed in out
    assert hot not in out
    assert recent not in out


def test_iter_decayed_notes_excludes_archive_and_dailies(mind: pathlib.Path):
    """``dailies/`` and ``archive/`` are never decay candidates."""
    today = datetime.date(2026, 6, 1)
    (mind / "cortex-memory" / "dailies").mkdir(exist_ok=True)
    (mind / "cortex-memory" / "archive").mkdir(exist_ok=True)
    _write_note(
        mind,
        "dailies/2026-04-01.md",
        last_accessed="2026-04-01",
        access_count=0,
    )
    _write_note(
        mind,
        "archive/old.md",
        last_accessed="2026-04-01",
        access_count=0,
    )
    out = stage_d._iter_decayed_notes(
        mind / "cortex-memory", today, window_days=7
    )
    assert out == []


# ---------- decay event emission ----------


def test_emit_decay_event_appends_to_events_jsonl(mind: pathlib.Path):
    """One decay_event record per call lands in memory/events.jsonl."""
    a = _write_note(mind, "research/a.md")
    b = _write_note(mind, "research/b.md")
    stage_d._emit_decay_event(mind, a, b, score=3.0)
    events_path = mind / "memory" / "events.jsonl"
    assert events_path.is_file()
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["type"] == "decay_event"
    assert rec["note"] == "research/a.md"
    assert rec["partner"] == "research/b.md"
    assert rec["score"] == 3.0


# ---------- pre-pass driver ----------


def test_run_decay_prepass_archives_and_pairs(mind: pathlib.Path):
    """End-to-end: archive-eligible note moves; pair emits an event."""
    today = datetime.date(2026, 6, 1)
    # Archive-eligible (no inbound links, superseded).
    _write_note(
        mind,
        "research/old-spec.md",
        status="superseded",
        last_accessed="2026-04-01",
        access_count=0,
    )
    # Two decayed notes that should pair on same-topic.
    _write_note(
        mind,
        "research/2026-04-01-cue-runner-validation.md",
        last_accessed="2026-04-01",
        access_count=0,
    )
    _write_note(
        mind,
        "research/2026-04-02-cue-runner-experiment-validation.md",
        last_accessed="2026-04-02",
        access_count=0,
    )

    result = stage_d.run_decay_prepass(mind, today=today)
    assert result.archived == 1
    assert result.extracted == 0  # no-op stub
    assert result.paired == 1
    assert len(result.notes) == 1

    archived = mind / "cortex-memory" / "archive" / "old-spec.md"
    assert archived.is_file()
    events = (mind / "memory" / "events.jsonl").read_text(encoding="utf-8")
    assert "\"type\": \"decay_event\"" in events


def test_run_decay_prepass_empty_vault_returns_empty_result(mind: pathlib.Path):
    """No decayed notes → no archive, no pairs, no events."""
    today = datetime.date(2026, 6, 1)
    result = stage_d.run_decay_prepass(mind, today=today)
    assert result.archived == 0
    assert result.paired == 0
    assert result.notes == []
    assert not (mind / "memory" / "events.jsonl").exists()


# ---------- stage_d.run() integration ----------


def test_run_populates_decay_pairing_field(mind: pathlib.Path):
    """The full Stage D tick should populate StageDReport.decay_pairing."""
    today = datetime.date(2026, 6, 1)
    # Pair of decayed notes for the pre-pass.
    _write_note(
        mind,
        "research/2026-04-01-foo-validation.md",
        last_accessed="2026-04-01",
        access_count=0,
    )
    _write_note(
        mind,
        "research/2026-04-02-foo-experiment-validation.md",
        last_accessed="2026-04-02",
        access_count=0,
    )

    # Stub synthesizer so the recombination flow doesn't crash on
    # finding fewer than 2 recent notes.
    def synth(slug_a, body_a, slug_b, body_b):
        return stage_d.SynthesizerOutput(null=True, reason="no")

    report = stage_d.run(mind, synthesizer=synth, today=today)
    assert report.decay_pairing is not None
    assert report.decay_pairing.paired == 1


# ---------- stage_c decay-priority score ----------


def test_decay_priority_score_zero_access_gets_base_boost(mind: pathlib.Path):
    """A note with access_count == 0 earns the base 10.0 boost."""
    note = _write_note(
        mind,
        "research/decayed.md",
        access_count=0,
        created="2026-05-30",
    )
    score = stage_c._decay_priority_score(note)
    assert score >= 10.0


def test_decay_priority_score_accessed_note_gets_zero(mind: pathlib.Path):
    """Any access at all → no decay boost."""
    note = _write_note(mind, "research/touched.md", access_count=3)
    assert stage_c._decay_priority_score(note) == 0.0


def test_decay_priority_score_fitness_exempt(mind: pathlib.Path):
    """Fitness-domain notes are fixed-schedule, not behavioral decay."""
    note = _write_note(
        mind,
        "research/bench-progression.md",
        tags=["fitness"],
        access_count=0,
    )
    assert stage_c._decay_priority_score(note) == 0.0


def test_decay_priority_score_age_bonus(mind: pathlib.Path):
    """Older zero-access notes score higher than newer ones."""
    older = _write_note(
        mind, "research/older.md", access_count=0, created="2024-01-01"
    )
    newer = _write_note(
        mind, "research/newer.md", access_count=0, created="2026-05-30"
    )
    assert stage_c._decay_priority_score(older) > stage_c._decay_priority_score(newer)


# ---------- stage_c.atomize() decay priority ----------


def _build_bloated_note(
    mind: pathlib.Path, slug: str, *, sections: int, access_count: int,
    section_lines: int = 100,
) -> pathlib.Path:
    """A multi-section note above the bloated threshold."""
    body_lines = ["Intro prose.\n"]
    for i in range(sections):
        body_lines.append(f"\n## Section {i}\n")
        body_lines.extend(["body line\n"] * section_lines)
    return _write_note(
        mind,
        f"research/{slug}.md",
        title=slug.replace("-", " ").title(),
        tags=["research"],
        access_count=access_count,
        body="".join(body_lines),
    )


def test_atomize_prefers_decayed_notes_within_cap(mind: pathlib.Path):
    """With max_items=1, a decayed bloated note wins over an accessed one."""
    decayed = _build_bloated_note(mind, "decayed-big", sections=2, access_count=0)
    accessed = _build_bloated_note(mind, "accessed-big", sections=2, access_count=20)

    n = stage_c.atomize(mind, max_items=1, journal_path=None)
    assert n == 1
    # Decayed should be split (parent now has wikilinks); accessed should still be a wall.
    decayed_text = decayed.read_text(encoding="utf-8")
    accessed_text = accessed.read_text(encoding="utf-8")
    assert "[[decayed-big-section-0]]" in decayed_text
    assert "## Section 0" in accessed_text  # untouched
    assert "[[accessed-big-section-0]]" not in accessed_text


def test_atomize_processes_decayed_at_relaxed_threshold(mind: pathlib.Path):
    """A decayed note between the 0.8 × threshold and the full threshold
    qualifies for atomization (where an accessed note would not)."""
    # ~210 lines: above 250 * 0.8 = 200, below 250.
    body_lines = ["Intro\n"]
    for i in range(2):
        body_lines.append(f"\n## Sec {i}\n")
        body_lines.extend(["x\n"] * 100)
    decayed = _write_note(
        mind,
        "research/mid-decayed.md",
        title="Mid",
        access_count=0,
        body="".join(body_lines),
    )

    n = stage_c.atomize(mind, max_items=5, journal_path=None)
    assert n == 1
    assert "[[mid-decayed-sec-0]]" in decayed.read_text(encoding="utf-8")


def test_atomize_skips_accessed_note_under_threshold(mind: pathlib.Path):
    """An accessed note at the same line count is NOT atomized — only
    decayed notes get the relaxed bar."""
    body_lines = ["Intro\n"]
    for i in range(2):
        body_lines.append(f"\n## Sec {i}\n")
        body_lines.extend(["x\n"] * 100)
    accessed = _write_note(
        mind,
        "research/mid-accessed.md",
        title="Mid",
        access_count=15,
        body="".join(body_lines),
    )

    n = stage_c.atomize(mind, max_items=5, journal_path=None)
    assert n == 0
    # Body still intact.
    assert "## Sec 0" in accessed.read_text(encoding="utf-8")
