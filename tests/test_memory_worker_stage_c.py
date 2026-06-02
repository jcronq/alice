"""Tests for :mod:`alice_thinking.memory_worker.stage_c`.

Stage C is the deterministic vault-grooming layer. Each test seeds a
tmp-path vault, runs the operation, and asserts on the post-state of
the vault + journal + any side-effect files (orphans-pending.md,
events.jsonl).

Style mirrors :mod:`tests.test_memory_worker_stage_b` — real
filesystem ops, tmp_path fixture, no mocking the fs.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_thinking.memory_worker import journal as journal_mod
from alice_thinking.memory_worker import stage_c


# ---------- shared fixtures ----------


@pytest.fixture(autouse=True)
def _reset_journal_verifiers():
    """Keep the journal verifier registry clean between tests.

    Stage C closures from one test mustn't leak verifier state into
    another's journal replay assertions.
    """
    journal_mod.reset_verifiers_to_phase1_defaults()
    yield
    journal_mod.reset_verifiers_to_phase1_defaults()


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path alice-mind with the expected layout."""
    (tmp_path / "cortex-memory" / "dailies").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "research").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "projects").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "reference").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "people").mkdir(parents=True)
    (tmp_path / "inner").mkdir(parents=True)
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


def _write_note(
    mind: pathlib.Path,
    rel: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    created: str = "2026-05-01",
    body: str = "",
    extra_fm: dict | None = None,
) -> pathlib.Path:
    """Drop a note with a typical Stage-C-relevant frontmatter shape."""
    path = mind / "cortex-memory" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    if title:
        fm_lines.append(f"title: {title}")
    if tags is not None:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    fm_lines.append(f"created: {created}")
    if extra_fm:
        for k, v in extra_fm.items():
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    text = "\n".join(fm_lines) + body
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- trigger ----------


def test_trigger_fires_on_each_signal_independently():
    """UNION trigger: each of the 5 signals is enough on its own."""
    cfg = stage_c.StageCConfig(decay_threshold=50)
    base = stage_c.StageCState()
    assert stage_c.should_run_c(base, cfg) is False
    for field in ("bloated_notes", "stale_dailies", "orphans", "broken_wikilinks"):
        st = stage_c.StageCState()
        setattr(st, field, 1)
        assert stage_c.should_run_c(st, cfg) is True, f"{field}=1 should trigger"
    decay = stage_c.StageCState(decayed_notes_in_window=51)
    assert stage_c.should_run_c(decay, cfg) is True
    # Exactly at threshold does NOT trigger (strict >, matches design).
    at_threshold = stage_c.StageCState(decayed_notes_in_window=50)
    assert stage_c.should_run_c(at_threshold, cfg) is False


def test_trigger_quiet_when_all_clear():
    """All-zero state → no trigger."""
    assert (
        stage_c.should_run_c(stage_c.StageCState(), stage_c.StageCConfig()) is False
    )


# ---------- atomize ----------


def test_atomize_splits_on_top_level_headings(mind: pathlib.Path):
    """A bloated note with ``## `` sections splits cleanly."""
    body_lines = ["Intro prose.\n"]
    for section in ("Alpha", "Beta", "Gamma"):
        body_lines.append(f"\n## {section}\n")
        body_lines.extend(["body line\n"] * 100)
    _write_note(
        mind,
        "research/big-note.md",
        title="Big Note",
        tags=["research"],
        body="".join(body_lines),
    )

    n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 1

    research = mind / "cortex-memory" / "research"
    children = sorted(p.name for p in research.glob("big-note-*.md"))
    assert children == ["big-note-alpha.md", "big-note-beta.md", "big-note-gamma.md"]

    parent_text = (research / "big-note.md").read_text(encoding="utf-8")
    # Parent has wikilinks to the new children.
    assert "[[big-note-alpha]]" in parent_text
    assert "[[big-note-beta]]" in parent_text
    assert "[[big-note-gamma]]" in parent_text
    # Parent frontmatter preserved (title, tags) + updated bumped.
    assert "title: Big Note" in parent_text
    assert "tags: [research]" in parent_text
    assert "updated:" in parent_text

    # Children have derived_from + inherited tags.
    alpha = (research / "big-note-alpha.md").read_text(encoding="utf-8")
    assert "derived_from: big-note" in alpha
    assert "tags: [research]" in alpha
    assert "## Alpha" in alpha


def test_atomize_skips_note_with_no_headings(
    mind: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """A bloated note that has no ``## `` headings logs and is skipped."""
    long_body = "\n".join([f"line {i}" for i in range(300)])
    path = _write_note(mind, "research/wall-of-text.md", body=long_body, title="W")
    with caplog.at_level("INFO"):
        n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 0
    # File unchanged.
    assert "line 0" in path.read_text(encoding="utf-8")
    # Skip message logged.
    assert any("atomize-skipped-no-headings" in r.message for r in caplog.records)


def test_atomize_respects_max_items(mind: pathlib.Path):
    """With more candidates than the cap, only ``max_items`` are
    processed in one cycle."""
    for i in range(3):
        body_lines = []
        for section in ("First", "Second"):
            body_lines.append(f"\n## {section}\n")
            body_lines.extend(["x\n"] * 150)
        _write_note(
            mind,
            f"research/big-{i}.md",
            title=f"Big {i}",
            body="".join(body_lines),
        )
    n = stage_c.atomize(mind, max_items=2, journal_path=None)
    assert n == 2


def test_atomize_journals_with_sha(mind: pathlib.Path):
    """Atomize writes a journal entry with the original SHA + children."""
    body_lines = []
    for section in ("Foo", "Bar"):
        body_lines.append(f"\n## {section}\n")
        body_lines.extend(["x\n"] * 150)
    _write_note(mind, "research/source.md", title="Src", body="".join(body_lines))
    journal_path = mind / "journal.jsonl"

    n = stage_c.atomize(mind, max_items=10, journal_path=journal_path)
    assert n == 1
    entries = journal_mod.load(journal_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.op == "atomize"
    assert entry.status == journal_mod.COMMITTED
    assert entry.detail["parent"] == "source"
    assert set(entry.detail["children"]) == {"source-foo", "source-bar"}
    assert isinstance(entry.detail["original_content_sha"], str)
    assert len(entry.detail["original_content_sha"]) == 64


# ---------- archive ----------


def test_archive_moves_stale_daily_to_year_folder(mind: pathlib.Path):
    """A daily older than 90 days moves to ``archive/dailies/<year>/``."""
    today = datetime.date(2026, 6, 2)
    # 91 days ago = 2026-03-03.
    stale_path = mind / "cortex-memory" / "dailies" / "2026-03-03.md"
    stale_path.write_text("---\ntitle: 2026-03-03\n---\n# Old\n", encoding="utf-8")
    fresh_path = mind / "cortex-memory" / "dailies" / "2026-06-01.md"
    fresh_path.write_text("---\ntitle: 2026-06-01\n---\n# Fresh\n", encoding="utf-8")

    n = stage_c.archive(mind, max_items=10, journal_path=None, today=today)
    assert n == 1
    assert not stale_path.exists()
    assert (
        mind / "cortex-memory" / "archive" / "dailies" / "2026" / "2026-03-03.md"
    ).is_file()
    # Fresh daily stays put.
    assert fresh_path.is_file()


def test_archive_creates_year_folder_if_missing(mind: pathlib.Path):
    """archive/dailies/<year>/ is mkdir-p'd."""
    today = datetime.date(2026, 6, 2)
    stale = mind / "cortex-memory" / "dailies" / "2024-01-15.md"
    stale.write_text("---\ntitle: 2024-01-15\n---\n", encoding="utf-8")
    assert not (mind / "cortex-memory" / "archive").exists()

    n = stage_c.archive(mind, max_items=10, journal_path=None, today=today)
    assert n == 1
    assert (
        mind / "cortex-memory" / "archive" / "dailies" / "2024" / "2024-01-15.md"
    ).is_file()


def test_archive_rewrites_wikilinks_pointing_at_moved_daily(mind: pathlib.Path):
    """References to the old slug get rewritten to the archive path."""
    today = datetime.date(2026, 6, 2)
    stale = mind / "cortex-memory" / "dailies" / "2025-12-01.md"
    stale.write_text("---\ntitle: 2025-12-01\n---\n", encoding="utf-8")
    ref = _write_note(
        mind,
        "research/refers-to-old-daily.md",
        title="Ref",
        body="See [[2025-12-01]] for the discussion.\n",
    )

    stage_c.archive(mind, max_items=10, journal_path=None, today=today)

    new_text = ref.read_text(encoding="utf-8")
    assert "[[2025-12-01]]" not in new_text
    assert "[[archive/dailies/2025/2025-12-01]]" in new_text


# ---------- dedupe-merge ----------


def test_dedupe_picks_older_canonical_and_merges(mind: pathlib.Path):
    """Two slug-equal notes merge with the older one as canonical."""
    older = _write_note(
        mind,
        "research/foo.md",
        title="Foo",
        tags=["research"],
        created="2026-01-01",
        body="Original content.\n",
    )
    # Drop the duplicate in a different subdir so both files exist
    # despite sharing the same stem.
    newer = _write_note(
        mind,
        "reference/foo.md",
        title="Foo",
        tags=["reference"],
        created="2026-05-01",
        body="Newer duplicate content.\n",
    )

    n = stage_c.dedupe_merge(mind, max_items=10, journal_path=None)
    assert n == 1
    # Older was canonical → reference/foo.md is deleted.
    assert older.is_file()
    assert not newer.is_file()
    canon_text = older.read_text(encoding="utf-8")
    assert "## Merged from foo (2026-05-01)" in canon_text
    assert "Newer duplicate content." in canon_text


def test_dedupe_tie_breaks_on_inbound_links(mind: pathlib.Path):
    """Same created date → more-inbound wins canonical."""
    a = _write_note(
        mind,
        "research/dup.md",
        title="Dup",
        created="2026-04-01",
        body="A body\n",
    )
    b = _write_note(
        mind,
        "reference/dup.md",
        title="Dup",
        created="2026-04-01",
        body="B body\n",
    )
    # Two referrers to `a`, one to `b` — but they share the same stem,
    # so we need to use a folder-qualified link to disambiguate. The
    # vault's actual convention is basename addressing — to test the
    # tie-break, we point at distinct slugs first then verify behavior
    # by writing referrers that use the bare stem (counts toward both,
    # which makes the tie-break degenerate). Easier: give the notes
    # distinct titles but distance < 3.
    a.unlink()
    b.unlink()
    a = _write_note(
        mind,
        "research/widget.md",
        title="Widget",
        created="2026-04-01",
        body="A body\n",
    )
    b = _write_note(
        mind,
        "reference/widgets.md",
        title="Widgets",
        created="2026-04-01",
        body="B body\n",
    )
    # Two inbound to widget, none to widgets → canonical = widget.
    _write_note(mind, "research/ref1.md", title="R1", body="See [[widget]].\n")
    _write_note(mind, "research/ref2.md", title="R2", body="Also [[widget]].\n")

    stage_c.dedupe_merge(mind, max_items=10, journal_path=None)
    # widget is canonical, widgets is merged.
    assert a.is_file()
    assert not b.is_file()


def test_dedupe_redirects_wikilinks_to_canonical(mind: pathlib.Path):
    """Links to the duplicate slug get redirected to the canonical."""
    canon = _write_note(
        mind,
        "research/canon.md",
        title="Canon",
        created="2026-01-01",
        body="C\n",
    )
    dup = _write_note(
        mind,
        "reference/canon.md",
        title="Canon",
        created="2026-05-01",
        body="D\n",
    )
    referrer = _write_note(
        mind,
        "research/elsewhere.md",
        title="Elsewhere",
        body="Originally pointed at [[canon]].\n",
    )
    stage_c.dedupe_merge(mind, max_items=10, journal_path=None)
    assert canon.is_file()
    assert not dup.is_file()
    # Redirect is a no-op (target stem unchanged) but the operation
    # must not corrupt the referrer.
    assert "[[canon]]" in referrer.read_text(encoding="utf-8")


# ---------- orphan-resolve ----------


def test_orphan_with_single_parent_match_gets_linked(mind: pathlib.Path):
    """Orphan tagged ``alpha`` and ``projects/alpha.md`` exists →
    auto-linked under the parent's ``## Linked notes`` section."""
    parent = _write_note(mind, "projects/alpha.md", title="Alpha", body="proj body\n")
    _write_note(
        mind,
        "research/standalone.md",
        title="Standalone",
        tags=["alpha"],
        body="research body\n",
    )

    n = stage_c.orphan_resolve(mind, max_items=10, journal_path=None)
    assert n >= 1
    parent_text = parent.read_text(encoding="utf-8")
    assert "## Linked notes" in parent_text
    assert "- [[standalone]]" in parent_text
    # Orphans-pending was not used (no ambiguity).
    pending = mind / "inner" / "orphans-pending.md"
    if pending.is_file():
        assert "standalone" not in pending.read_text(encoding="utf-8")


def test_orphan_with_ambiguous_parents_goes_to_pending(mind: pathlib.Path):
    """Two matching parents → orphan queued, not linked."""
    p1 = _write_note(mind, "projects/topic.md", title="Topic-proj", body="p\n")
    p2 = _write_note(mind, "reference/topic.md", title="Topic-ref", body="r\n")
    _write_note(
        mind,
        "research/ambiguous.md",
        title="Ambiguous",
        tags=["topic"],
        body="\n",
    )

    n = stage_c.orphan_resolve(mind, max_items=10, journal_path=None)
    assert n >= 1
    # Neither parent has a Linked notes section.
    assert "## Linked notes" not in p1.read_text(encoding="utf-8")
    assert "## Linked notes" not in p2.read_text(encoding="utf-8")
    pending = (mind / "inner" / "orphans-pending.md").read_text(encoding="utf-8")
    assert "- [[ambiguous]]" in pending
    assert "topic" in pending


def test_orphan_with_no_tags_goes_to_pending(mind: pathlib.Path):
    """Orphan with no parent match → pending queue with ``(none)``."""
    _write_note(mind, "research/loner.md", title="Loner", tags=[], body="\n")

    stage_c.orphan_resolve(mind, max_items=10, journal_path=None)
    pending = (mind / "inner" / "orphans-pending.md").read_text(encoding="utf-8")
    assert "- [[loner]]" in pending
    assert "(none)" in pending


# ---------- top-level run ----------


def test_run_short_circuits_when_trigger_quiet(mind: pathlib.Path):
    """Empty-ish vault → no trigger → no events written."""
    # No notes at all → no bloated/stale/decayed/orphans/broken.
    report = stage_c.run(mind, journal_path=None)
    assert report.ran is False
    assert report.atomize == 0
    assert report.archive == 0
    events_path = mind / "memory" / "events.jsonl"
    assert not events_path.is_file()


def test_run_emits_decay_recovery_rate_event(mind: pathlib.Path):
    """When Stage C runs, it appends a ``decay_recovery_rate`` event."""
    # Force a trigger via a stale daily.
    today = datetime.date.today()
    stale_day = today - datetime.timedelta(days=120)
    (mind / "cortex-memory" / "dailies" / f"{stale_day.isoformat()}.md").write_text(
        "---\ntitle: old\n---\n", encoding="utf-8"
    )
    report = stage_c.run(mind, journal_path=None)
    assert report.ran is True

    events_path = mind / "memory" / "events.jsonl"
    assert events_path.is_file()
    lines = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()]
    decay_events = [e for e in lines if e["type"] == "decay_recovery_rate"]
    assert len(decay_events) == 1
    rec = decay_events[0]
    assert rec["notes_recovered"] >= 1
    assert "cycle_duration_seconds" in rec
    assert "rate" in rec


def test_run_empty_vault_returns_zero(mind: pathlib.Path):
    """All ops on a brand-new vault → zero counts."""
    report = stage_c.run(
        mind,
        journal_path=None,
        config=stage_c.StageCConfig(decay_threshold=0),
    )
    # decay_threshold=0 still doesn't fire because we have 0 decayed notes
    # (no notes at all). Empty vault is a no-op.
    assert report.atomize == 0
    assert report.archive == 0
    assert report.dedupe_merge == 0
    assert report.orphan_resolve == 0


# ---------- journal replay (crash recovery) ----------


def test_replay_marks_atomize_committed_when_targets_present(mind: pathlib.Path):
    """A pending atomize entry whose children + parent wikilinks exist
    is re-marked COMMITTED on replay."""
    # Seed the post-mutation vault state manually.
    research = mind / "cortex-memory" / "research"
    research.mkdir(parents=True, exist_ok=True)
    parent = research / "noted.md"
    parent.write_text(
        "---\ntitle: Noted\n---\n\n## Sections\n\n- See [[noted-a]]\n- See [[noted-b]]\n",
        encoding="utf-8",
    )
    (research / "noted-a.md").write_text("---\ntitle: A\n---\n", encoding="utf-8")
    (research / "noted-b.md").write_text("---\ntitle: B\n---\n", encoding="utf-8")

    journal_path = mind / "journal.jsonl"
    journal_mod.append(
        journal_path,
        op="atomize",
        source="research/noted.md",
        targets=["research/noted-a.md", "research/noted-b.md"],
        detail={
            "parent": "noted",
            "children": ["noted-a", "noted-b"],
            "original_content_sha": "deadbeef" * 8,
        },
        journal_id="crashed-atomize",
    )

    stage_c.register_verifiers(mind)
    report = journal_mod.replay(journal_path)
    assert report.committed == 1
    assert report.skipped == 0
    after = {e.journal_id: e.status for e in journal_mod.load(journal_path)}
    assert after["crashed-atomize"] == journal_mod.COMMITTED


def test_replay_marks_atomize_skipped_when_children_missing(mind: pathlib.Path):
    """A crashed atomize with no children on disk → SKIPPED."""
    research = mind / "cortex-memory" / "research"
    research.mkdir(parents=True, exist_ok=True)
    # Parent still has the OLD body (pre-split), children don't exist.
    parent = research / "noted.md"
    parent.write_text(
        "---\ntitle: Noted\n---\n\n## Original-Section\nblob\n",
        encoding="utf-8",
    )

    journal_path = mind / "journal.jsonl"
    journal_mod.append(
        journal_path,
        op="atomize",
        source="research/noted.md",
        targets=["research/noted-a.md", "research/noted-b.md"],
        detail={
            "parent": "noted",
            "children": ["noted-a", "noted-b"],
            "original_content_sha": "deadbeef" * 8,
        },
        journal_id="partial-atomize",
    )

    stage_c.register_verifiers(mind)
    report = journal_mod.replay(journal_path)
    assert report.skipped == 1
    after = {e.journal_id: e.status for e in journal_mod.load(journal_path)}
    assert after["partial-atomize"] == journal_mod.SKIPPED


def test_replay_archive_verifier_checks_src_gone_and_dst_present(mind: pathlib.Path):
    """archive verifier: src must NOT exist, dst MUST exist."""
    dailies = mind / "cortex-memory" / "dailies"
    archive_dir = mind / "cortex-memory" / "archive" / "dailies" / "2025"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "2025-01-01.md").write_text("---\n---\n", encoding="utf-8")
    # src absent (already moved).

    journal_path = mind / "journal.jsonl"
    journal_mod.append(
        journal_path,
        op="archive",
        source="dailies/2025-01-01.md",
        targets=["archive/dailies/2025/2025-01-01.md"],
        detail={
            "src": "dailies/2025-01-01.md",
            "dst": "archive/dailies/2025/2025-01-01.md",
            "wikilink_updates": [],
        },
        journal_id="arch-1",
    )

    stage_c.register_verifiers(mind)
    report = journal_mod.replay(journal_path)
    assert report.committed == 1

    # Failure case: bring src back, replay should now SKIP.
    journal_mod.append(
        journal_path,
        op="archive",
        source="dailies/2025-02-02.md",
        targets=["archive/dailies/2025/2025-02-02.md"],
        detail={
            "src": "dailies/2025-02-02.md",
            "dst": "archive/dailies/2025/2025-02-02.md",
        },
        journal_id="arch-2",
    )
    dailies.mkdir(parents=True, exist_ok=True)
    (dailies / "2025-02-02.md").write_text(
        "---\n---\nstill here\n", encoding="utf-8"
    )
    report = journal_mod.replay(journal_path)
    after = {e.journal_id: e.status for e in journal_mod.load(journal_path)}
    assert after["arch-2"] == journal_mod.SKIPPED
