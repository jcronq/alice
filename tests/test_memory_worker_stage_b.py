"""Tests for :mod:`alice_thinking.memory_worker.stage_b`.

Stage B is the deterministic inbox-drain routing layer. Each test
seeds a tmp-path vault (``inner/notes/`` + ``cortex-memory/``),
drops one or more notes into the inbox, runs :func:`stage_b.run`,
and asserts on the routing outcome + the post-tick state of the
inbox / vault.

The mind/vault root is the tmp_path itself — Stage B doesn't care
about absolute paths, only the relative ``inner/notes/`` /
``cortex-memory/`` / ``memory/events.jsonl`` layout.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_thinking.memory_worker import stage_b


# ---------- vault fixture ----------


@pytest.fixture
def vault(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path "alice-mind" with the expected directory shape."""
    (tmp_path / "inner" / "notes").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "dailies").mkdir(parents=True)
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


def _drop_note(vault: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = vault / "inner" / "notes" / name
    path.write_text(content, encoding="utf-8")
    return path


def _read_daily(vault: pathlib.Path, day: datetime.date | None = None) -> str:
    day = day or datetime.date.today()
    return (vault / "cortex-memory" / "dailies" / f"{day.isoformat()}.md").read_text(
        encoding="utf-8"
    )


def _consumed_path(vault: pathlib.Path, name: str) -> pathlib.Path:
    today = datetime.date.today().isoformat()
    return vault / "inner" / "notes" / ".consumed" / today / name


def _failed_path(vault: pathlib.Path, name: str) -> pathlib.Path:
    today = datetime.date.today().isoformat()
    return vault / "inner" / "notes" / ".failed" / today / name


# ---------- empty-inbox no-op ----------


def test_empty_inbox_is_noop(vault: pathlib.Path) -> None:
    """No notes in the inbox → report zeros, no files created."""
    report = stage_b.run(vault)
    assert report.scanned == 0
    assert report.routed_activity == 0
    assert report.routed_event == 0
    assert report.unclassified == 0
    # Daily file is NOT auto-created when there's no work — Stage
    # B only creates it on first append.
    assert not (
        vault
        / "cortex-memory"
        / "dailies"
        / f"{datetime.date.today().isoformat()}.md"
    ).exists()


# ---------- activity route ----------


def test_activity_by_tag_appends_to_daily(vault: pathlib.Path) -> None:
    """``tag: activity`` → daily entry."""
    _drop_note(
        vault,
        "2026-06-01-active-wake-notes.md",
        "---\ntag: activity\n---\n\nGenerative work — read three notes, no surface.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_activity == 1
    assert report.scanned == 1

    body = _read_daily(vault)
    assert "Generative work — read three notes" in body
    assert _consumed_path(vault, "2026-06-01-active-wake-notes.md").exists()
    assert not (vault / "inner" / "notes" / "2026-06-01-active-wake-notes.md").exists()


def test_activity_by_body_hhmm_prefix_appends_verbatim(vault: pathlib.Path) -> None:
    """A body line opening ``HH:MM ...`` triggers the activity rule
    even when no tag is set, and the line is used verbatim."""
    _drop_note(
        vault,
        "00-misc-note.md",
        "---\ntag: misc\n---\n\n13:45 EDT — Drained 4 motion batches from inbox.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_activity == 1
    body = _read_daily(vault)
    assert "13:45 EDT — Drained 4 motion batches from inbox." in body


# ---------- event route ----------


def test_event_meal_writes_events_jsonl_and_daily(vault: pathlib.Path) -> None:
    """``tag: meal`` → one JSONL line + one daily one-liner."""
    _drop_note(
        vault,
        "2026-06-01-breakfast.md",
        "---\ntag: meal\nmeal: breakfast\nkcal: 470\nprotein_g: 44\n---\n\nYogurt, granola, protein shake.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_event == 1

    events_path = vault / "memory" / "events.jsonl"
    assert events_path.is_file()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "meal"
    assert record["subject"] == "jason"
    # Data block carries the frontmatter scalars we coerced.
    assert record["data"]["meal"] == "breakfast"
    assert record["data"]["kcal"] == 470
    assert record["data"]["protein_g"] == 44

    body = _read_daily(vault)
    assert "meal: " in body
    assert _consumed_path(vault, "2026-06-01-breakfast.md").exists()


def test_event_by_body_marker_routes_to_workout(vault: pathlib.Path) -> None:
    """Body opener ``workout: ...`` triggers the event rule even
    when frontmatter has no event tag."""
    _drop_note(
        vault,
        "0-workout.md",
        "---\n---\n\nworkout: upper, 52 min, bench 100x8.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_event == 1
    record = json.loads(
        (vault / "memory" / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["type"] == "workout"


# ---------- concept route ----------


def test_new_concept_creates_atomic_note(vault: pathlib.Path) -> None:
    """``tag: project`` → new note under ``cortex-memory/projects/``."""
    _drop_note(
        vault,
        "alice-body.md",
        "---\ntag: project\ntitle: Alice Body\n---\n\nVoice-first, output-only face. Pi 5 + ReSpeaker + 2.1\" round LCD.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_concept == 1

    target = vault / "cortex-memory" / "projects" / "alice-body.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "title: Alice Body" in text
    assert "access_count: 0" in text
    assert "Voice-first, output-only face." in text


def test_concept_duplicate_slug_merges_dated_section(vault: pathlib.Path) -> None:
    """Re-dropping a note with the same slug appends, doesn't overwrite."""
    target_dir = vault / "cortex-memory" / "people"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "mike.md"
    target.write_text(
        "---\ntitle: Mike\ntags: [person]\ncreated: 2026-04-01\n---\n\n# Mike\n\nJason's younger brother.\n",
        encoding="utf-8",
    )
    _drop_note(
        vault,
        "mike.md",
        "---\ntag: person\n---\n\nLives in Utah. Helps with Plex questions.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_concept == 1

    text = target.read_text(encoding="utf-8")
    # Original content preserved.
    assert "Jason's younger brother." in text
    # Merge section appended with today's date.
    today = datetime.date.today().isoformat()
    assert f"## Update {today}" in text
    assert "Lives in Utah." in text


def test_reference_candidate_routes_to_reference_folder(vault: pathlib.Path) -> None:
    """``tag: reference-candidate`` → new note under ``cortex-memory/reference/``.

    Speaking emits ``reference-candidate`` heavily; it should route to the same
    folder as the base ``reference`` tag rather than bouncing to ``.failed/``.
    """
    _drop_note(
        vault,
        "ssh-universal-password.md",
        "---\ntag: reference-candidate\ntitle: Universal SSH Password\n---\n\nBounce sshpass through aimax1 when alice container key is rejected.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_concept == 1

    target = vault / "cortex-memory" / "reference" / "ssh-universal-password.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "title: Universal SSH Password" in text
    assert "Bounce sshpass through aimax1" in text


def test_feedback_self_routes_to_feedback_folder(vault: pathlib.Path) -> None:
    """``tag: feedback-self`` → new note under ``cortex-memory/feedback/``.

    Speaking emits ``feedback-self`` heavily; it should route to the same
    folder as the base ``feedback`` tag rather than bouncing to ``.failed/``.
    """
    _drop_note(
        vault,
        "feedback-no-walls-of-text.md",
        "---\ntag: feedback-self\ntitle: No Walls Of Text\n---\n\nSignal replies stay 1-3 sentences. Long narratives go in thinking notes.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_concept == 1

    target = vault / "cortex-memory" / "feedback" / "feedback-no-walls-of-text.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "title: No Walls Of Text" in text
    assert "Signal replies stay 1-3 sentences." in text


# ---------- conflict-candidate route ----------


def test_conflict_candidate_writes_to_conflicts(vault: pathlib.Path) -> None:
    """``tag: conflict-candidate`` → conflicts/ entry."""
    _drop_note(
        vault,
        "stale-cache-conflict.md",
        "---\ntag: conflict-candidate\ntitle: Stale cache vs realtime API\n---\n\n"
        "Vault says cache is authoritative; API returns fresher state.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_conflict == 1

    today = datetime.date.today().isoformat()
    target = vault / "cortex-memory" / "conflicts" / f"{today}-stale-cache-conflict.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "status: open" in text
    assert "Vault says cache is authoritative" in text


# ---------- noise route ----------


def test_noise_drops_to_daily_oneliner(vault: pathlib.Path) -> None:
    """``route: noise`` → daily one-liner, no events.jsonl write."""
    _drop_note(
        vault,
        "2026-06-01-motion-batch-1.md",
        "---\nroute: noise\ntags: [motion-pipeline, lobe-observation]\n---\n\n"
        "motion-pipeline — batch of 7 motion event(s) in Master Closet.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_noise == 1

    body = _read_daily(vault)
    assert "noise:" in body
    # Noise does NOT touch events.jsonl.
    assert not (vault / "memory" / "events.jsonl").exists()


# ---------- unclassified route ----------


def test_unclassified_writes_surface_and_moves_to_failed(vault: pathlib.Path) -> None:
    """No matching rule → conflict-candidate surface in ``inner/surface/``
    AND the note moves to ``.failed/`` so the surface doesn't re-emit
    on the next tick."""
    _drop_note(
        vault,
        "weird-thought.md",
        "---\ntag: musings\n---\n\nWonder if the vault should grow organically or be pruned aggressively.\n",
    )
    report = stage_b.run(vault)
    assert report.unclassified == 1

    surface_dir = vault / "inner" / "surface"
    assert surface_dir.is_dir()
    surfaces = list(surface_dir.glob("*-stage-b-unclassified-*.md"))
    assert len(surfaces) == 1
    surface_text = surfaces[0].read_text(encoding="utf-8")
    assert "type: conflict-candidate" in surface_text
    assert "source: memory-worker" in surface_text
    assert "stage: B" in surface_text
    assert "weird-thought" in surface_text

    # Note moved to .failed/, not .consumed/.
    assert _failed_path(vault, "weird-thought.md").exists()
    assert not _consumed_path(vault, "weird-thought.md").exists()


def test_no_tag_at_all_is_unclassified(vault: pathlib.Path) -> None:
    """Per the design: a note with no ``tag`` and no body markers
    is unclassified — don't infer concept routing from body text."""
    _drop_note(
        vault,
        "untagged.md",
        "Just a free-form note with no frontmatter.\n",
    )
    report = stage_b.run(vault)
    assert report.unclassified == 1


# ---------- malformed frontmatter ----------


def test_malformed_frontmatter_moves_to_failed(
    vault: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A frontmatter-parser exception moves the note to ``.failed/``
    rather than crashing the drain."""

    # Force the parser to raise so we exercise the malformed branch
    # without depending on yaml_lite's tolerance (yaml_lite is
    # intentionally permissive).
    def boom(text: str):  # noqa: ANN001
        raise ValueError("simulated parser crash")

    monkeypatch.setattr(stage_b, "split_frontmatter", boom)

    _drop_note(
        vault,
        "broken.md",
        "---\nnot: real: yaml: maybe\n---\n\nContent\n",
    )
    with caplog.at_level("WARNING"):
        report = stage_b.run(vault)

    assert report.malformed == 1
    assert _failed_path(vault, "broken.md").exists()
    assert not (vault / "inner" / "notes" / "broken.md").exists()


# ---------- mid-routing failure leaves note in inbox ----------


def test_writer_failure_leaves_note_in_inbox(
    vault: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the per-route writer raises after classification, the note
    stays in the inbox and is NOT renamed into ``.consumed/``. The
    next tick will retry."""
    _drop_note(
        vault,
        "2026-06-01-active.md",
        "---\ntag: activity\n---\n\nGenerative work, no surface.\n",
    )

    def explode(*_a, **_kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr(stage_b, "_write_activity", explode)

    report = stage_b.run(vault)
    assert report.errors == 1
    assert report.routed_activity == 0
    # Note still in inbox.
    assert (vault / "inner" / "notes" / "2026-06-01-active.md").is_file()
    # Nothing in .consumed/.
    assert not _consumed_path(vault, "2026-06-01-active.md").exists()


# ---------- atomic rename idempotency ----------


def test_consume_overwrites_pre_existing_target(vault: pathlib.Path) -> None:
    """A leftover ``.consumed/<today>/<name>`` from a partial prior
    run gets overwritten by :func:`os.replace`. The newer routing
    output wins; the older consumed copy is no more authoritative."""
    today = datetime.date.today().isoformat()
    stale_consumed_dir = vault / "inner" / "notes" / ".consumed" / today
    stale_consumed_dir.mkdir(parents=True, exist_ok=True)
    (stale_consumed_dir / "2026-06-01-active.md").write_text(
        "stale leftover content\n", encoding="utf-8"
    )

    _drop_note(
        vault,
        "2026-06-01-active.md",
        "---\ntag: activity\n---\n\nFresh activity body, latest content wins.\n",
    )

    report = stage_b.run(vault)
    assert report.routed_activity == 1
    # The leftover got overwritten with the freshly-consumed copy.
    consumed = _consumed_path(vault, "2026-06-01-active.md")
    assert consumed.is_file()
    text = consumed.read_text(encoding="utf-8")
    assert "Fresh activity body" in text
    assert "stale leftover content" not in text


# ---------- daily auto-creation ----------


def test_daily_is_created_on_first_append(vault: pathlib.Path) -> None:
    """No daily exists for today → Stage B creates it on first
    write, with frontmatter matching the vault convention."""
    today = datetime.date.today().isoformat()
    daily = vault / "cortex-memory" / "dailies" / f"{today}.md"
    assert not daily.exists()

    _drop_note(
        vault,
        "0-activity.md",
        "---\ntag: activity\n---\n\n14:00 EDT — Inbox drain.\n",
    )
    report = stage_b.run(vault)
    assert report.routed_activity == 1
    assert daily.is_file()
    text = daily.read_text(encoding="utf-8")
    # Frontmatter present.
    assert text.startswith("---\n")
    assert f"title: {today}" in text
    assert "tags: [daily]" in text
    assert f"created: {today}" in text
    # Body present.
    assert f"# {today}" in text
    assert "Inbox drain." in text


# ---------- scan skips ----------


def test_scan_skips_dotfiles_and_subdirs(vault: pathlib.Path) -> None:
    """The inbox scanner ignores dotfiles, ``.consumed/``,
    ``.failed/``, non-``.md`` files, and subdirectories."""
    inbox = vault / "inner" / "notes"
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    (inbox / "not-markdown.txt").write_text("hi", encoding="utf-8")
    (inbox / "noise").mkdir()
    (inbox / "noise" / "should-be-ignored.md").write_text(
        "---\ntag: activity\n---\n\nNested.\n", encoding="utf-8"
    )

    # One legitimate inbox note alongside the noise.
    _drop_note(
        vault,
        "good.md",
        "---\ntag: activity\n---\n\nReal entry.\n",
    )

    report = stage_b.run(vault)
    assert report.scanned == 1
    assert report.routed_activity == 1
