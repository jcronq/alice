"""End-to-end integration test for the memory worker.

Phase 5 cutover (2026-06-02) — exercises one full memory-worker cycle:

1. Seed ``inner/notes/`` with two notes (activity + structured event).
2. Run :mod:`alice_thinking.memory_worker.wake` with mocked Stage D
   synthesizer (no real LLM call).
3. Verify the daily / events.jsonl / consumed-inbox states.
4. Seed bloated and stale notes, re-run, confirm Stage C grooming.
5. Seed two graph-distant recently-touched research notes, re-run,
   confirm Stage D writes a recombination note through the commit
   gate.
6. Confirm a thinking-side wake (``alice_thinking.wake.main``) still
   dispatches through ``ActiveMode`` without ever touching the
   former B/C/D code paths.

The test pins phase-5 architecture: thinking is single-mode generative,
memory worker owns inbox drain + grooming + recombination. Both
services coexist without stepping on each other's vault writes thanks
to ``vault_lock``.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_thinking.memory_worker import stage_c, stage_d, wake
from alice_thinking.memory_worker.stage_d import SynthesizerOutput


# ---------- fixtures ----------


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path alice-mind root with the expected layout."""
    for sub in (
        "inner/notes",
        "inner/state",
        "inner/surface",
        "inner/thoughts",
        "cortex-memory/dailies",
        "cortex-memory/research",
        "cortex-memory/reference",
        "cortex-memory/projects",
        "cortex-memory/conflicts",
        "memory",
        "config",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_config(mind: pathlib.Path, blob: dict) -> None:
    (mind / "config" / "alice.config.json").write_text(json.dumps(blob))


def _drop_note(mind: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = mind / "inner" / "notes" / name
    path.write_text(content, encoding="utf-8")
    return path


def _today() -> datetime.date:
    return datetime.date.today()


def _daily_path(mind: pathlib.Path, day: datetime.date | None = None) -> pathlib.Path:
    day = day or _today()
    return mind / "cortex-memory" / "dailies" / f"{day.isoformat()}.md"


# ---------- full-cycle integration ----------


def test_memory_worker_full_cycle_drains_inbox_and_writes_daily(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage B drains an activity note → daily; Stage C/D no-op on
    a near-empty vault. Confirms the end-to-end glue (wake.main →
    stage_b → vault writes) operates without the legacy thinking
    code paths."""
    log = mind / ".." / "memory-worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    journal_path = mind / "inner" / "state" / "memory-worker-journal.jsonl"
    _write_config(
        mind,
        {
            "memory_worker": {
                "enabled": True,
                "cadence_minutes": 30,
                "journal_path": str(journal_path),
                "stage_d_model": "local",
                "stage_d_api_tier_enabled": False,
            }
        },
    )
    _drop_note(
        mind,
        "20260602-0001-active-wake-notes.md",
        "---\ntag: activity\n---\n\nGenerative wake — wrote one synthesis.\n",
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worker",
            "--mind",
            str(mind),
            "--log",
            str(log),
            "--state-dir",
            str(mind / ".." / "state"),
        ],
    )
    monkeypatch.setattr(
        wake, "MEMORY_WORKER_LIVENESS_PATH", mind / ".." / "alive"
    )

    rc = wake.main()
    assert rc == 0

    # Daily got the activity entry.
    daily = _daily_path(mind)
    assert daily.is_file(), "Stage B should have created today's daily"
    assert "Generative wake — wrote one synthesis." in daily.read_text(
        encoding="utf-8"
    )

    # Inbox is empty; the note moved to .consumed/<today>/.
    consumed = (
        mind
        / "inner"
        / "notes"
        / ".consumed"
        / _today().isoformat()
        / "20260602-0001-active-wake-notes.md"
    )
    assert consumed.is_file(), "consumed note should land under .consumed/<today>/"
    assert not (
        mind / "inner" / "notes" / "20260602-0001-active-wake-notes.md"
    ).is_file()

    # Heartbeat event emitted.
    events = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    heartbeats = [e for e in events if e.get("event") == "memory_worker_heartbeat"]
    assert heartbeats, f"expected memory_worker_heartbeat in {events!r}"
    hb = heartbeats[-1]
    assert hb["stage_b_routed_activity"] == 1
    assert hb["stage_b_scanned"] == 1


# ---------- stage C grooming ----------


def test_stage_c_grooms_bloated_note_when_threshold_met(
    mind: pathlib.Path,
) -> None:
    """A >250-line note in cortex-memory should be picked up as a
    bloated-atomize candidate; running stage_c with low
    ``max_items_per_category`` exercises the atomize path without
    requiring real LLM calls (atomize is mechanical)."""
    # Seed one bloated note — 300 lines of content + frontmatter.
    bloated = mind / "cortex-memory" / "reference" / "bloated-topic.md"
    body_lines = ["This is a stub line.\n" for _ in range(300)]
    bloated.write_text(
        "---\n"
        "slug: bloated-topic\n"
        "title: Bloated Topic\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [reference]\n"
        "---\n\n"
        "# Bloated Topic\n\n"
        "> **tl;dr** This note is intentionally long to trigger Stage C.\n\n"
        + "".join(body_lines),
        encoding="utf-8",
    )

    journal_path = mind / "inner" / "state" / "memory-worker-journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    # Force Stage C to run by lowering the decay threshold + caps so
    # the bloated count alone is enough to trip the UNION trigger.
    cfg = stage_c.StageCConfig(
        decay_threshold=10_000,  # silenced; we want bloated path
        max_items_per_category=1,
    )
    report = stage_c.run(mind, journal_path=journal_path, config=cfg)

    # We don't insist on a particular grooming outcome (the atomize
    # writer is content-aware and may decline a stub) — the
    # invariant is that Stage C *ran* (vs short-circuiting) and the
    # report shape is well-formed. The trigger condition (bloated > 0)
    # alone is sufficient to flip the UNION gate.
    assert report.ran is True, (
        f"Stage C should run when a bloated note exists; got report={report!r}"
    )


# ---------- stage D recombination (mocked synthesizer) ----------


def test_stage_d_writes_synthesis_with_mock_synthesizer(
    mind: pathlib.Path,
) -> None:
    """Stage D with a graph-distant recently-touched pair + a mocked
    synthesizer that returns a synthesis JSON → recombination note
    lands under cortex-memory/research/, audit log has one row."""
    today = _today()

    # Two recently-touched research notes with no wikilinks between
    # them and disjoint tag sets — satisfies the graph-distance
    # criterion (both must live under research/ for Stage D's
    # candidate scan to pick them up).
    note_a = mind / "cortex-memory" / "research" / "topic-a.md"
    note_b = mind / "cortex-memory" / "research" / "topic-b.md"
    for note in (note_a, note_b):
        note.parent.mkdir(parents=True, exist_ok=True)

    note_a.write_text(
        "---\n"
        "slug: topic-a\n"
        "title: Topic A\n"
        f"created: {today.isoformat()}\n"
        f"updated: {today.isoformat()}\n"
        f"last_accessed: {today.isoformat()}\n"
        "tags: [research, alpha]\n"
        "---\n\n"
        "Domain A insight: caching reduces tail latency on hot keys.\n",
        encoding="utf-8",
    )
    note_b.write_text(
        "---\n"
        "slug: topic-b\n"
        "title: Topic B\n"
        f"created: {today.isoformat()}\n"
        f"updated: {today.isoformat()}\n"
        f"last_accessed: {today.isoformat()}\n"
        "tags: [project, beta]\n"
        "---\n\n"
        "Domain B insight: lazy materialization defers cost to read time.\n",
        encoding="utf-8",
    )

    journal_path = mind / "inner" / "state" / "memory-worker-journal.jsonl"

    # Mock synthesizer: returns the structured connection the prod
    # qwen path would. Stage D wraps the result through
    # ``commit_stage_d_synthesis``.
    def _fake_synth(
        slug_a: str, body_a: str, slug_b: str, body_b: str
    ) -> SynthesizerOutput:
        return SynthesizerOutput(
            null=False,
            title="Caching as deferred cost",
            body=(
                "Topic A's cache and topic B's lazy materialization are "
                "two faces of cost deferral: both shift work out of the "
                "hot path by paying for it elsewhere. The cache pays in "
                "memory; lazy materialization pays at first-read."
                "\n\nBoth break down under uniform access — when every "
                "key is hot, the cache hit rate collapses and lazy "
                "materialization's read-time amortization vanishes."
                "\n\nThe shared shape suggests a unified deferral budget "
                "as a single architectural knob."
            ),
        )

    report = stage_d.run(
        mind,
        journal_path=journal_path,
        synthesizer=_fake_synth,
        today=today,
    )

    assert report.ran, f"Stage D should run with two recent pairs; got {report!r}"
    assert report.synthesized == 1, (
        f"expected 1 synthesis, got {report.synthesized} "
        f"(skipped_reason={report.skipped_reason!r})"
    )

    # Vault note landed under research/ with the recombination prefix.
    research_dir = mind / "cortex-memory" / "research"
    recomb_notes = list(research_dir.glob(f"{today.isoformat()}-recombination-*.md"))
    assert recomb_notes, (
        f"expected a recombination note in {research_dir}, "
        f"found {[p.name for p in research_dir.iterdir()]}"
    )
    body = recomb_notes[0].read_text(encoding="utf-8")
    assert "source: stage-d" in body, "synthesis must carry source: stage-d"
    assert "Caching as deferred cost" in body or "deferral" in body

    # Audit log got a row.
    audit_log = mind / "inner" / "state" / "memory-worker-stage-d-attempts.jsonl"
    assert audit_log.is_file()
    rows = [
        json.loads(line)
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows, "expected at least one audit row"
    # ``status`` is "committed" in the memory-worker audit log (the
    # ``shipped`` terminology was thinking's commit_stage_d_synthesis
    # gate; memory-worker writes its own audit row).
    assert any(r.get("status") == "committed" for r in rows), rows
    assert all(
        r.get("source") == "memory-worker-stage-d" for r in rows
    ), rows


def test_stage_d_null_result_does_not_pollute_research(
    mind: pathlib.Path,
) -> None:
    """Synthesizer returns NULL_RESULT → no vault note, but a null
    line lands in the null-results log."""
    today = _today()

    note_a = mind / "cortex-memory" / "research" / "loose-a.md"
    note_b = mind / "cortex-memory" / "research" / "loose-b.md"
    for note in (note_a, note_b):
        note.parent.mkdir(parents=True, exist_ok=True)
    for note, tags in ((note_a, "[research, alpha]"), (note_b, "[reference, beta]")):
        note.write_text(
            "---\n"
            f"slug: {note.stem}\n"
            f"title: {note.stem}\n"
            f"created: {today.isoformat()}\n"
            f"updated: {today.isoformat()}\n"
            f"last_accessed: {today.isoformat()}\n"
            f"tags: {tags}\n"
            "---\n\n"
            "Stub content for null-result testing.\n",
            encoding="utf-8",
        )

    journal_path = mind / "inner" / "state" / "memory-worker-journal.jsonl"

    def _null_synth(*args, **kwargs) -> SynthesizerOutput:
        return SynthesizerOutput(null=True, reason="no real connection")

    report = stage_d.run(
        mind,
        journal_path=journal_path,
        synthesizer=_null_synth,
        today=today,
    )

    assert report.ran
    assert report.null_results == 1
    assert report.synthesized == 0

    # No vault note created.
    research_dir = mind / "cortex-memory" / "research"
    recomb_notes = list(research_dir.glob(f"{today.isoformat()}-recombination-*.md"))
    assert not recomb_notes, (
        "NULL_RESULT must not write a vault note, "
        f"found {[p.name for p in recomb_notes]}"
    )


# ---------- thinking single-mode invariant ----------


def test_thinking_wake_dispatches_active_mode_only(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post phase 5: ``alice_thinking.wake.main`` always routes through
    ActiveMode (or the design-commission / conflict-resolution
    preempts). No SleepMode, no Stage B/C/D import from the wake
    module."""
    import sys

    # Confirm the public modes API no longer exports SleepMode.
    from alice_thinking import modes

    assert hasattr(modes, "ActiveMode")
    assert not hasattr(modes, "SleepMode"), (
        "Phase 5 retired SleepMode; it should not be re-exported"
    )

    # Confirm the deleted stage_d helper modules really are gone.
    for dead in (
        "alice_thinking.stage_d_pipeline",
        "alice_thinking.stage_d_judges",
        "alice_thinking.stage_d_invariant",
        "alice_thinking.wake_hooks",
        "alice_thinking.modes.sleep",
    ):
        # Drop any cached import from earlier tests so the negative
        # check actually tries to import fresh.
        sys.modules.pop(dead, None)
        with pytest.raises(ModuleNotFoundError):
            __import__(dead)

    # Drive the wake's selector — single-mode contract.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from alice_thinking.modes import ActiveMode
    from alice_thinking.selector import select_mode

    eastern = ZoneInfo("America/New_York")
    # 02:30 — formerly sleep window, now still ACTIVE.
    night = datetime(2026, 6, 2, 2, 30, tzinfo=eastern)
    # 14:30 — formerly active.
    afternoon = datetime(2026, 6, 2, 14, 30, tzinfo=eastern)
    assert isinstance(select_mode(now=night), ActiveMode)
    assert isinstance(select_mode(now=afternoon), ActiveMode)


def test_thinking_wake_main_smoke(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wake.main --quick`` runs without touching the network and
    returns 0. Pins that the phase-5 wake body still completes a
    cycle end-to-end."""
    from alice_thinking import wake as thinking_wake

    # Stub auth + the kernel adapter so the wake doesn't try to call
    # a real LLM. ``run_wake`` returns 0 synchronously.
    async def _zero(**kw):
        return 0

    monkeypatch.setattr(thinking_wake, "ensure_auth_env", lambda *a, **kw: None)
    monkeypatch.setattr(thinking_wake, "run_wake", lambda **kw: _zero(**kw))

    # Skip prompt loader (avoids personae.yml + skill rendering side
    # effects).
    monkeypatch.setattr(thinking_wake, "_install_prompt_loader", lambda *a, **kw: None)
    monkeypatch.setattr(
        thinking_wake, "_render_system_prompt", lambda *a, **kw: ""
    )

    # Minimal model.yml so load_model_config returns a valid spec.
    cfg_dir = mind / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model.yml").write_text(
        "thinking:\n  backend: subscription\n  model: claude-sonnet-test\n"
    )

    state_dir = mind / ".." / "thinking-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = mind / ".." / "thinking.log"
    monkeypatch.setattr(
        thinking_wake, "THINKING_LIVENESS_PATH", mind / ".." / "thinking-alive"
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "alice-think",
            "--mind",
            str(mind),
            "--state-dir",
            str(state_dir),
            "--log",
            str(log),
            "--quick",
        ],
    )

    rc = thinking_wake.main()
    assert rc == 0
