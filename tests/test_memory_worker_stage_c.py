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
from types import SimpleNamespace

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


# ---------- atomize: slug-uniqueness pre-flight ----------
#
# Regression guard for the 2026-06-07 shadow-orphan recurrence: atomize
# was creating new child notes whose slug already lived elsewhere in
# the vault, producing two files with the same stem. Wikilinks resolve
# by slug regardless of folder, so the loser became unreachable.


def _bloated_three_section_note(
    mind: pathlib.Path, rel: str, *, sections: tuple[str, str, str]
) -> pathlib.Path:
    """Helper: drop a bloated note with three named ``## `` sections."""
    body_lines = ["Intro prose.\n"]
    for section in sections:
        body_lines.append(f"\n## {section}\n")
        body_lines.extend(["body line\n"] * 100)
    return _write_note(
        mind,
        rel,
        title="Big Note",
        tags=["research"],
        body="".join(body_lines),
    )


def test_vault_has_slug_finds_match_anywhere_in_vault(mind: pathlib.Path):
    """`vault_has_slug` is folder-agnostic — a hit in any subdir counts."""
    _write_note(mind, "research/foo-bar.md", title="Foo Bar", body="body\n")
    assert stage_c.vault_has_slug(mind / "cortex-memory", "foo-bar") is True
    # Case-insensitive.
    assert stage_c.vault_has_slug(mind / "cortex-memory", "FOO-BAR") is True
    # Negative case.
    assert stage_c.vault_has_slug(mind / "cortex-memory", "no-such-slug") is False


def test_vault_has_slug_empty_vault_is_false(mind: pathlib.Path):
    """Empty vault: every slug check is a miss."""
    # `mind` fixture creates empty subdirs but no .md files.
    assert stage_c.vault_has_slug(mind / "cortex-memory", "anything") is False


def test_atomize_skips_section_on_slug_collision_default(
    mind: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """Default config: a section whose slug already exists somewhere
    in the vault is SKIPPED — no new child file is created for it,
    a warning is logged, and the section content is preserved inline
    in the parent (no data loss)."""
    # Pre-existing slug in a DIFFERENT folder — exactly the shadow-orphan
    # case wikilinks can't disambiguate.
    _write_note(
        mind,
        "projects/big-note-beta.md",
        title="Existing Beta",
        body="pre-existing content\n",
    )

    parent = _bloated_three_section_note(
        mind, "research/big-note.md", sections=("Alpha", "Beta", "Gamma")
    )

    with caplog.at_level("WARNING"):
        n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 1

    research = mind / "cortex-memory" / "research"
    # Alpha and Gamma children created; Beta SKIPPED.
    assert (research / "big-note-alpha.md").exists()
    assert (research / "big-note-gamma.md").exists()
    assert not (research / "big-note-beta.md").exists()

    # Pre-existing collision target untouched.
    existing = mind / "cortex-memory" / "projects" / "big-note-beta.md"
    assert "pre-existing content" in existing.read_text(encoding="utf-8")

    # Parent rewrite: wikilinks only to created children; Beta content
    # preserved INLINE (the bytes have to go somewhere).
    parent_text = parent.read_text(encoding="utf-8")
    assert "[[big-note-alpha]]" in parent_text
    assert "[[big-note-gamma]]" in parent_text
    assert "[[big-note-beta]]" not in parent_text
    assert "## Beta" in parent_text  # inline-preserved section heading
    assert "body line" in parent_text  # inline-preserved section content

    # Warning logged with the existing path so a human can diagnose.
    msgs = [r.message for r in caplog.records]
    assert any(
        "atomize-skipped-slug-collision" in m
        and "big-note-beta" in m
        and "projects/big-note-beta.md" in m
        for m in msgs
    ), f"no slug-collision warning found in {msgs!r}"


def test_atomize_disambiguates_on_collision_when_enabled(
    mind: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """Opt-in flag: instead of skipping, generate `{slug}-2` until
    vault-unique and force-create the child."""
    _write_note(
        mind,
        "projects/big-note-beta.md",
        title="Existing Beta",
        body="pre-existing\n",
    )

    parent = _bloated_three_section_note(
        mind, "research/big-note.md", sections=("Alpha", "Beta", "Gamma")
    )

    with caplog.at_level("INFO"):
        n = stage_c.atomize(
            mind,
            max_items=10,
            journal_path=None,
            disambiguate_on_collision=True,
        )
    assert n == 1

    research = mind / "cortex-memory" / "research"
    # Beta got the `-2` suffix; the others land at their natural slugs.
    assert (research / "big-note-alpha.md").exists()
    assert (research / "big-note-beta-2.md").exists()
    assert (research / "big-note-gamma.md").exists()
    # No `-beta` child in research (would have shadowed the projects one).
    assert not (research / "big-note-beta.md").exists()

    # Parent wikilinks point at the disambiguated slug.
    parent_text = parent.read_text(encoding="utf-8")
    assert "[[big-note-beta-2]]" in parent_text

    # An INFO-level disambiguation log was emitted.
    msgs = [r.message for r in caplog.records]
    assert any(
        "atomize-slug-disambiguated" in m and "big-note-beta-2" in m for m in msgs
    ), f"no disambiguation log found in {msgs!r}"


def test_atomize_empty_vault_no_false_collisions(mind: pathlib.Path):
    """No pre-existing notes anywhere in the vault → atomize behaves
    exactly as the no-collision case: all sub-notes created."""
    _bloated_three_section_note(
        mind, "research/big-note.md", sections=("Alpha", "Beta", "Gamma")
    )
    n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 1
    research = mind / "cortex-memory" / "research"
    children = sorted(p.name for p in research.glob("big-note-*.md"))
    assert children == [
        "big-note-alpha.md",
        "big-note-beta.md",
        "big-note-gamma.md",
    ]


def test_atomize_aborts_when_every_section_collides(
    mind: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """If every proposed child slug collides AND we're in SKIP mode,
    the source is left untouched and abort is logged."""
    for section in ("alpha", "beta", "gamma"):
        _write_note(
            mind,
            f"projects/big-note-{section}.md",
            title=f"Existing {section}",
            body="pre-existing\n",
        )
    parent = _bloated_three_section_note(
        mind, "research/big-note.md", sections=("Alpha", "Beta", "Gamma")
    )
    parent_before = parent.read_text(encoding="utf-8")

    with caplog.at_level("WARNING"):
        n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 0
    # Source byte-for-byte unchanged.
    assert parent.read_text(encoding="utf-8") == parent_before
    msgs = [r.message for r in caplog.records]
    assert any(
        "atomize-aborted-all-collisions" in m for m in msgs
    ), f"no abort log found in {msgs!r}"


def test_atomize_idempotent_under_collision(
    mind: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """Running atomize twice in a row with a pre-existing collision
    produces the SAME warning count, not a doubling: the first run
    rewrites the parent (skipping the colliding section inline), the
    second run sees the parent under threshold and short-circuits."""
    _write_note(
        mind,
        "projects/big-note-beta.md",
        title="Existing Beta",
        body="pre-existing\n",
    )
    _bloated_three_section_note(
        mind, "research/big-note.md", sections=("Alpha", "Beta", "Gamma")
    )

    with caplog.at_level("WARNING"):
        first = stage_c.atomize(mind, max_items=10, journal_path=None)
        warnings_after_first = sum(
            1
            for r in caplog.records
            if "atomize-skipped-slug-collision" in r.message
        )
        second = stage_c.atomize(mind, max_items=10, journal_path=None)
        warnings_after_second = sum(
            1
            for r in caplog.records
            if "atomize-skipped-slug-collision" in r.message
        )

    assert first == 1
    # Second pass: the parent is now a pointer-list + small inline
    # remainder, well under the bloated threshold. No more atomization,
    # no new collision warnings.
    assert second == 0
    assert warnings_after_first == 1
    assert warnings_after_second == 1


def test_atomize_sibling_children_share_collision_set(mind: pathlib.Path):
    """When two source notes processed in the same tick would both
    create the same child slug, the second one should detect the
    in-flight collision (slug_index is mutated as children are
    written).

    Phase 2 update: ``created`` set to today so the continuous decay
    formula registers these (fresh, zero-access research) notes as
    decay candidates → relaxed atomize threshold (0.8 × bloated_lines)
    activates. The legacy ``created=2026-05-01`` would age-discount
    the decay boost and require the full bloated threshold, which the
    contrived 205-line bodies don't reach."""
    body_a = (
        "intro\n\n## Beta\n" + ("body\n" * 100) + "\n## Done\n" + ("body\n" * 100)
    )
    body_b = (
        "intro\n\n## Beta\n" + ("body\n" * 100) + "\n## Done\n" + ("body\n" * 100)
    )
    today_iso = datetime.date.today().isoformat()
    _write_note(
        mind, "research/parent-a.md", title="A", body=body_a, created=today_iso
    )
    _write_note(
        mind, "research/parent-b.md", title="B", body=body_b, created=today_iso
    )

    n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 2
    research = mind / "cortex-memory" / "research"
    assert (research / "parent-a-beta.md").exists()
    assert (research / "parent-b-beta.md").exists()
    assert (research / "parent-a-done.md").exists()
    assert (research / "parent-b-done.md").exists()


def test_load_stage_c_config_parses_disambiguate_flag(mind: pathlib.Path):
    """`alice.config.json` can flip the atomize disambiguation flag."""
    cfg_dir = mind / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "alice.config.json").write_text(
        json.dumps(
            {
                "memory_worker": {
                    "stage_c": {"atomize_disambiguate_on_collision": True}
                }
            }
        )
    )
    cfg = stage_c._load_stage_c_config(mind)
    assert cfg.atomize_disambiguate_on_collision is True


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


# ---------- archive-stale-open ----------


def _read_status(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().lower()
    return ""


def test_archive_stale_open_matches_open_zero_access_aged(mind: pathlib.Path):
    """status: open + access_count: 0 + created > 30 days ago = candidate."""
    today = datetime.date(2026, 6, 6)
    # 60 days ago = 2026-04-07.
    target = _write_note(
        mind,
        "projects/stale-thing.md",
        title="Stale thing",
        created="2026-04-07",
        extra_fm={"status": "open", "access_count": 0},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 1
    assert _read_status(target) == "archived"


def test_archive_stale_open_skips_nonzero_access_count(mind: pathlib.Path):
    """access_count > 0 disqualifies even if status: open and old."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/has-been-read.md",
        title="Has been read",
        created="2026-04-07",
        extra_fm={"status": "open", "access_count": 5},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 0
    assert _read_status(target) == "open"


def test_archive_stale_open_skips_recent_creation(mind: pathlib.Path):
    """A 10-day-old open note is too fresh to archive."""
    today = datetime.date(2026, 6, 6)
    # 10 days ago = 2026-05-27.
    target = _write_note(
        mind,
        "projects/fresh-open.md",
        title="Fresh open",
        created="2026-05-27",
        extra_fm={"status": "open", "access_count": 0},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 0
    assert _read_status(target) == "open"


def test_archive_stale_open_skips_non_open_status(mind: pathlib.Path):
    """status: complete (or anything ≠ open) is ignored even when old."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/done-already.md",
        title="Done already",
        created="2026-04-07",
        extra_fm={"status": "complete", "access_count": 0},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 0
    assert _read_status(target) == "complete"


def test_archive_stale_open_dry_run_does_not_mutate(mind: pathlib.Path):
    """With dry_run=True (the default), candidates are counted but the
    vault and today's daily are untouched."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/dry-run-candidate.md",
        title="Dry run candidate",
        created="2026-04-07",
        extra_fm={"status": "open", "access_count": 0},
    )
    daily_path = (
        mind / "cortex-memory" / "dailies" / f"{today.isoformat()}.md"
    )
    assert not daily_path.exists()

    # Default is dry_run=True — verify by omitting the kwarg.
    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today
    )
    assert n == 1
    # Frontmatter untouched.
    assert _read_status(target) == "open"
    # No daily created by the dry-run pass.
    assert not daily_path.exists()


def test_archive_stale_open_apply_mutates_and_logs_to_daily(mind: pathlib.Path):
    """With dry_run=False, frontmatter flips and today's daily gets a
    one-line entry."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/apply-candidate.md",
        title="Apply candidate",
        created="2026-04-07",
        extra_fm={"status": "open", "access_count": 0},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 1
    assert _read_status(target) == "archived"

    daily_path = (
        mind / "cortex-memory" / "dailies" / f"{today.isoformat()}.md"
    )
    assert daily_path.is_file()
    daily_text = daily_path.read_text(encoding="utf-8")
    assert "Archived apply-candidate" in daily_text
    assert "status: open" in daily_text
    assert "access_count: 0" in daily_text
    assert "created: 2026-04-07" in daily_text
    assert "stale-open-cleanup" in daily_text


def test_archive_stale_open_is_idempotent(mind: pathlib.Path):
    """Running twice with dry_run=False archives once; the second run
    finds nothing to do because status: archived falls out of the gate."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/idempotent-target.md",
        title="Idempotent target",
        created="2026-04-07",
        extra_fm={"status": "open", "access_count": 0},
    )

    first = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    second = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert first == 1
    assert second == 0
    assert _read_status(target) == "archived"


def test_archive_stale_open_treats_missing_access_count_as_zero(
    mind: pathlib.Path,
):
    """A note with no ``access_count`` field is treated as zero (matches)."""
    today = datetime.date(2026, 6, 6)
    target = _write_note(
        mind,
        "projects/no-ac-field.md",
        title="No access_count field",
        created="2026-04-07",
        extra_fm={"status": "open"},
    )

    n = stage_c.archive_stale_open(
        mind, max_items=10, journal_path=None, today=today, dry_run=False
    )
    assert n == 1
    assert _read_status(target) == "archived"


def test_archive_stale_open_respects_max_items(mind: pathlib.Path):
    """max_items caps a large candidate set on a single tick."""
    today = datetime.date(2026, 6, 6)
    for i in range(5):
        _write_note(
            mind,
            f"projects/stale-batch-{i:02d}.md",
            title=f"Stale batch {i}",
            created="2026-04-07",
            extra_fm={"status": "open", "access_count": 0},
        )

    n = stage_c.archive_stale_open(
        mind, max_items=3, journal_path=None, today=today, dry_run=False
    )
    assert n == 3


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


# ---------- auto-propagate wiring ----------


def _force_trigger_stale_daily(mind: pathlib.Path) -> None:
    """Drop an aged daily so ``should_run_c`` fires."""
    today = datetime.date.today()
    stale_day = today - datetime.timedelta(days=120)
    (mind / "cortex-memory" / "dailies" / f"{stale_day.isoformat()}.md").write_text(
        "---\ntitle: old\n---\n", encoding="utf-8"
    )


def test_run_invokes_auto_propagate_when_unpropagated(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """When cascade detection finds unpropagated corrections, ``run`` calls
    ``auto_propagate`` and records its return value on the report."""
    _force_trigger_stale_daily(mind)

    fake_cascade = SimpleNamespace(correction_pairs_checked=3, total_unpropagated=2)
    monkeypatch.setattr(stage_c.cascade_mod, "run", lambda *a, **k: fake_cascade)

    calls: list[tuple] = []

    def _spy(passed_mind, passed_report):
        calls.append((passed_mind, passed_report))
        return {"slug-a": 2, "slug-b": 1}

    monkeypatch.setattr(stage_c.autoprop_mod, "auto_propagate", _spy)

    report = stage_c.run(mind, journal_path=None)

    # Called exactly once, with the cascade report (dry-run default untouched).
    assert len(calls) == 1
    assert calls[0][0] == mind
    assert calls[0][1] is fake_cascade
    # added = sum(changes.values()); pairs = len(changes)
    assert report.auto_propagate_added == 3
    assert report.auto_propagate_pairs == 2

    d = report.to_dict()
    assert d["auto_propagate_added"] == 3
    assert d["auto_propagate_pairs"] == 2


def test_run_skips_auto_propagate_when_nothing_to_propagate(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """No unpropagated corrections → ``auto_propagate`` is not called and the
    report fields stay zero (but are still present in ``to_dict``)."""
    _force_trigger_stale_daily(mind)

    fake_cascade = SimpleNamespace(correction_pairs_checked=4, total_unpropagated=0)
    monkeypatch.setattr(stage_c.cascade_mod, "run", lambda *a, **k: fake_cascade)

    def _fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("auto_propagate should not run with nothing to propagate")

    monkeypatch.setattr(stage_c.autoprop_mod, "auto_propagate", _fail)

    report = stage_c.run(mind, journal_path=None)

    assert report.auto_propagate_added == 0
    assert report.auto_propagate_pairs == 0
    d = report.to_dict()
    assert d["auto_propagate_added"] == 0
    assert d["auto_propagate_pairs"] == 0


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


# ---------- vault_lock retrofit (phase 4) ----------
#
# The phase 3 worker shipped stage_c.py without using vault_lock from
# phase 1. The phase 4 retrofit wires :func:`vault_lock.acquire` around
# every vault mutation in stage_c.py. These tests prove the wiring by
# (a) running the operations end-to-end (they still produce correct
# output — same as the tests above) and (b) showing that an externally
# held lock makes the operation skip the locked file.
#
# The lock is process-scoped; we use ``os.fork`` to hold the sidecar
# from a child process so the parent's acquire actually contends.


def _hold_lock_in_child(target_path: pathlib.Path) -> int:
    """Fork a child that grabs ``target_path``'s sidecar and sleeps.
    Returns the child PID. Parent must SIGKILL + waitpid in finally."""
    import os
    import time as _time

    from alice_thinking import vault_lock as _vl

    pid = os.fork()
    if pid == 0:
        try:
            with _vl.acquire(target_path, mode=_vl.LockMode.EXCLUSIVE):
                _time.sleep(10.0)
        finally:
            os._exit(0)
    # Brief pause so the child has time to acquire before parent contends.
    _time.sleep(0.1)
    return pid


def _kill_child(pid: int) -> None:
    import os
    import signal as _signal

    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


def test_atomize_skips_when_source_lock_held(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """An externally held EXCLUSIVE lock on the bloated source makes
    atomize log + skip rather than rewrite."""
    monkeypatch.setattr(stage_c, "_LOCK_TIMEOUT_SECONDS", 0.2)
    body_lines = []
    for section in ("Foo", "Bar"):
        body_lines.append(f"\n## {section}\n")
        body_lines.extend(["x\n"] * 150)
    src = _write_note(
        mind, "research/locked-src.md", title="Locked", body="".join(body_lines)
    )

    pid = _hold_lock_in_child(src)
    try:
        n = stage_c.atomize(mind, max_items=10, journal_path=None)
    finally:
        _kill_child(pid)
    assert n == 0
    # Source remains intact (no children produced).
    assert "## Foo" in src.read_text(encoding="utf-8")
    assert not list((mind / "cortex-memory" / "research").glob("locked-src-*.md"))


def test_archive_skips_when_destination_lock_held(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Atomic lock on the archive destination makes archive log + skip."""
    monkeypatch.setattr(stage_c, "_LOCK_TIMEOUT_SECONDS", 0.2)
    today = datetime.date(2026, 6, 10)
    stale = mind / "cortex-memory" / "dailies" / "2025-01-01.md"
    stale.write_text("---\ntitle: 2025-01-01\n---\n", encoding="utf-8")
    # Pre-create dest directory and lock the destination path.
    dst = (
        mind
        / "cortex-memory"
        / "archive"
        / "dailies"
        / "2025"
        / "2025-01-01.md"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)

    pid = _hold_lock_in_child(dst)
    try:
        n = stage_c.archive(mind, max_items=10, journal_path=None, today=today)
    finally:
        _kill_child(pid)
    assert n == 0
    # Source not moved.
    assert stale.is_file()


def test_orphan_link_skips_when_parent_lock_held(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A locked parent makes the orphan-link append return False — but
    the orphan still gets counted as "resolved" by the cycle (idempotent
    semantics — we'll retry next tick)."""
    monkeypatch.setattr(stage_c, "_LOCK_TIMEOUT_SECONDS", 0.2)
    parent = _write_note(mind, "projects/alpha.md", title="Alpha", body="\n")
    _write_note(
        mind,
        "research/orphan-1.md",
        title="Orphan-1",
        tags=["alpha"],
        body="\n",
    )

    pid = _hold_lock_in_child(parent)
    try:
        stage_c.orphan_resolve(mind, max_items=10, journal_path=None)
    finally:
        _kill_child(pid)
    # Parent unchanged — no Linked notes section appended.
    assert "## Linked notes" not in parent.read_text(encoding="utf-8")


def test_dedupe_skips_group_when_lock_held(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """A locked duplicate file makes the whole group skip — the canonical
    remains unchanged and the duplicate stays on disk."""
    monkeypatch.setattr(stage_c, "_LOCK_TIMEOUT_SECONDS", 0.2)
    canon = _write_note(
        mind,
        "research/widget.md",
        title="Widget",
        created="2026-01-01",
        body="canonical body\n",
    )
    dup = _write_note(
        mind,
        "reference/widget.md",
        title="Widget",
        created="2026-05-01",
        body="dup body\n",
    )

    pid = _hold_lock_in_child(dup)
    try:
        n = stage_c.dedupe_merge(mind, max_items=10, journal_path=None)
    finally:
        _kill_child(pid)
    assert n == 0
    # Duplicate not deleted, canonical not merged.
    assert dup.is_file()
    canon_text = canon.read_text(encoding="utf-8")
    assert "## Merged from" not in canon_text


def test_atomize_still_works_under_normal_lock_path(mind: pathlib.Path):
    """Sanity check: with no external contention, atomize still produces
    the expected children + parent rewrite under vault_lock."""
    body_lines = []
    for section in ("First", "Second"):
        body_lines.append(f"\n## {section}\n")
        body_lines.extend(["x\n"] * 150)
    src = _write_note(
        mind, "research/unlocked.md", title="Unlocked", body="".join(body_lines)
    )
    n = stage_c.atomize(mind, max_items=10, journal_path=None)
    assert n == 1
    research = mind / "cortex-memory" / "research"
    children = sorted(p.name for p in research.glob("unlocked-*.md"))
    assert children == ["unlocked-first.md", "unlocked-second.md"]
    # Parent rewritten under the held source lock.
    assert "[[unlocked-first]]" in src.read_text(encoding="utf-8")
