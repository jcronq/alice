"""Tests for :mod:`alice_thinking.memory_worker.stage_d`.

Stage D is the LLM-driven recombination layer. Each test seeds a
tmp-path vault with recently-touched research notes, injects a fake
synthesizer (so we don't hit a real model), and asserts on the vault +
state files after the cycle.

Style mirrors :mod:`tests.test_memory_worker_stage_c`: real filesystem
ops, ``tmp_path`` fixture, no fs mocking. The synthesizer is the only
seam we inject.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from alice_thinking import vault_lock
from alice_thinking.memory_worker import journal as journal_mod
from alice_thinking.memory_worker import stage_d


# ---------- shared fixtures ----------


@pytest.fixture(autouse=True)
def _reset_journal_verifiers():
    """Reset the journal verifier registry between tests so a
    Stage D-registered closure from one test can't leak into another."""
    journal_mod.reset_verifiers_to_phase1_defaults()
    yield
    journal_mod.reset_verifiers_to_phase1_defaults()


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp-path alice-mind with the directories Stage D touches."""
    (tmp_path / "cortex-memory" / "research").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "projects").mkdir(parents=True)
    (tmp_path / "inner" / "state").mkdir(parents=True)
    return tmp_path


def _write_research_note(
    mind: pathlib.Path,
    slug: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    updated: str = "2026-06-09",
    last_accessed: str = "2026-06-09",
    body: str = "Body of note.\n",
    folder: str = "research",
    extra_fm: dict | None = None,
) -> pathlib.Path:
    """Drop a research note with the frontmatter Stage D inspects."""
    path = mind / "cortex-memory" / folder / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    if title:
        fm_lines.append(f"title: {title}")
    fm_lines.append("source: research")
    if tags is not None:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    fm_lines.append("created: 2026-04-01")
    fm_lines.append(f"updated: {updated}")
    fm_lines.append(f"last_accessed: {last_accessed}")
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


def _make_synth(*, null: bool = False, title: str = "Bridges", body_md: str = "P1.\n\nP2.\n\nP3.\n", reason: str = ""):
    """Build a synthesizer callable returning a fixed output.

    Mirrors the public :data:`stage_d.Synthesizer` signature so tests
    don't have to import the protocol; just pass the lambda directly.
    """
    def synth(slug_a, body_a, slug_b, body_b):
        if null:
            return stage_d.SynthesizerOutput(null=True, reason=reason or "no real connection")
        return stage_d.SynthesizerOutput(null=False, title=title, body=body_md)
    return synth


# ---------- selection: recency window ----------


def test_recently_touched_skips_stale_notes(mind: pathlib.Path):
    """A note last touched outside the window is excluded."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(
        mind, "fresh", updated="2026-06-08", last_accessed="2026-06-08"
    )
    _write_research_note(
        mind, "stale", updated="2026-05-01", last_accessed="2026-05-01"
    )
    out = list(
        stage_d.iter_recently_touched(mind, window_days=7, today=today)
    )
    names = [p.stem for p in out]
    assert "fresh" in names
    assert "stale" not in names


def test_recently_touched_accepts_either_field(mind: pathlib.Path):
    """``updated`` OR ``last_accessed`` within the window is enough."""
    today = datetime.date(2026, 6, 10)
    # Only ``last_accessed`` is recent; ``updated`` is old.
    _write_research_note(
        mind,
        "via-access",
        updated="2026-04-01",
        last_accessed="2026-06-09",
    )
    out = list(
        stage_d.iter_recently_touched(mind, window_days=7, today=today)
    )
    assert [p.stem for p in out] == ["via-access"]


# ---------- selection: graph distance ----------


def test_runs_when_two_recent_distant_notes_exist(mind: pathlib.Path):
    """Two recently-touched notes with no link / no shared neighbor →
    Stage D picks them and produces a synthesis via the injected synth."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(
        mind, "alpha", tags=["compilers"], body="Alpha content about parsers.\n"
    )
    _write_research_note(
        mind, "beta", tags=["sleep"], body="Beta content about chronotypes.\n"
    )

    report = stage_d.run(
        mind, synthesizer=_make_synth(title="Parsers and chronotypes"), today=today
    )
    assert report.ran is True
    assert report.synthesized == 1
    assert report.null_results == 0
    assert report.synthesis_path is not None
    # Synthesis file landed under research/.
    out_file = mind / "cortex-memory" / report.synthesis_path
    assert out_file.is_file()


def test_skips_when_pair_directly_links(mind: pathlib.Path):
    """A note that directly links to the only other candidate fails the
    graph-distance check — Stage D reports skipped."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(
        mind, "alpha", body="See [[beta]] for more.\n"
    )
    _write_research_note(mind, "beta", body="Standalone.\n")
    report = stage_d.run(mind, synthesizer=_make_synth(), today=today)
    assert report.ran is False
    assert report.synthesized == 0
    assert report.skipped_reason is not None
    assert "no graph-distant pair" in report.skipped_reason


def test_picks_smallest_tag_overlap_among_distant_pairs(mind: pathlib.Path):
    """When multiple pairs satisfy graph-distance, the pair with the
    smallest tag intersection wins."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["shared", "x"])
    # ``beta`` overlaps ``alpha`` by one tag.
    _write_research_note(mind, "beta", tags=["shared", "y"])
    # ``gamma`` doesn't overlap ``alpha`` at all — best partner for it.
    _write_research_note(mind, "gamma", tags=["z"])
    captured: dict[str, tuple[str, str]] = {}

    def capture(slug_a, body_a, slug_b, body_b):
        captured["pair"] = (slug_a, slug_b)
        return stage_d.SynthesizerOutput(null=False, title="t", body="b1\n\nb2\n\nb3\n")

    stage_d.run(mind, synthesizer=capture, today=today)
    assert captured["pair"] in (("alpha", "gamma"), ("gamma", "alpha"))


# ---------- selection: pair dedup ----------


def test_skips_pair_present_in_processed_log(mind: pathlib.Path):
    """A pair already logged within the dedup window is not picked."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["a"])
    _write_research_note(mind, "beta", tags=["b"])
    # Pre-populate the pairs log with (alpha, beta).
    pairs_path = mind / "inner" / "state" / "stage-d-pairs.jsonl"
    pairs_path.write_text(
        json.dumps(
            {
                "note_a": "alpha",
                "note_b": "beta",
                "timestamp": "2026-06-08T00:00:00Z",
                "synthesis_path": None,
                "outcome": "synthesized",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = stage_d.run(mind, synthesizer=_make_synth(), today=today)
    assert report.ran is False
    assert report.synthesized == 0


def test_picks_pair_outside_dedup_window(mind: pathlib.Path):
    """A pair logged longer than the lookback window IS eligible
    again."""
    today = datetime.date(2026, 7, 10)
    _write_research_note(mind, "alpha", tags=["a"], updated="2026-07-09", last_accessed="2026-07-09")
    _write_research_note(mind, "beta", tags=["b"], updated="2026-07-09", last_accessed="2026-07-09")
    pairs_path = mind / "inner" / "state" / "stage-d-pairs.jsonl"
    # 60 days ago — outside the 30-day default lookback.
    pairs_path.write_text(
        json.dumps(
            {
                "note_a": "alpha",
                "note_b": "beta",
                "timestamp": "2026-05-08T00:00:00Z",
                "outcome": "synthesized",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = stage_d.run(mind, synthesizer=_make_synth(), today=today)
    assert report.ran is True
    assert report.synthesized == 1


# ---------- output: NULL_RESULT path ----------


def test_null_result_does_not_write_vault_note(mind: pathlib.Path):
    """When synthesizer emits NULL_RESULT, no synthesis file is created,
    a null-results log line lands, and the pair is recorded for dedup."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["a"])
    _write_research_note(mind, "beta", tags=["b"])
    report = stage_d.run(
        mind, synthesizer=_make_synth(null=True, reason="forced abstract"), today=today
    )
    assert report.ran is True
    assert report.null_results == 1
    assert report.synthesized == 0

    research = mind / "cortex-memory" / "research"
    # No synthesis file produced.
    assert not list(research.glob("*recombination*.md"))

    nulls_path = mind / "inner" / "state" / "stage-d-null-results.jsonl"
    assert nulls_path.is_file()
    lines = [json.loads(ln) for ln in nulls_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["note_a"] == "alpha"
    assert lines[0]["note_b"] == "beta"
    assert "forced abstract" in lines[0]["reason"]

    # Pair recorded with outcome=null_result so dedup catches it.
    pairs_path = mind / "inner" / "state" / "stage-d-pairs.jsonl"
    pair_lines = [json.loads(ln) for ln in pairs_path.read_text().splitlines() if ln.strip()]
    assert pair_lines[-1]["outcome"] == "null_result"


# ---------- output: SYNTHESIS path + commit gate ----------


def test_synthesis_writes_vault_note_with_expected_frontmatter(
    mind: pathlib.Path,
):
    """A shipped synthesis writes a file under research/ with
    ``source: stage-d`` and inherited tags."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["compilers"])
    _write_research_note(mind, "beta", tags=["sleep"])
    report = stage_d.run(
        mind,
        synthesizer=_make_synth(title="Compilers and sleep"),
        today=today,
    )
    assert report.synthesized == 1

    out_file = mind / "cortex-memory" / report.synthesis_path
    text = out_file.read_text(encoding="utf-8")
    assert "source: stage-d" in text
    assert "note_a: alpha" in text
    assert "note_b: beta" in text
    # Inherited tags include "recombination" + both source tags.
    assert "recombination" in text
    assert "compilers" in text
    assert "sleep" in text
    # access_count starts at 0 so a future Stage C decay pass sees it
    # as fresh.
    assert "access_count: 0" in text
    # File name follows YYYY-MM-DD-recombination-<slug>.md.
    assert out_file.name.startswith("2026-06-10-recombination-")
    assert out_file.suffix == ".md"


def test_commit_gate_audit_log_invariant(mind: pathlib.Path):
    """Every synthesis write produces exactly one audit row whose
    ``audit_hash`` matches the file's SHA-256."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["a"])
    _write_research_note(mind, "beta", tags=["b"])
    report = stage_d.run(
        mind, synthesizer=_make_synth(title="Bridges"), today=today
    )
    assert report.synthesized == 1

    audit_path = mind / "inner" / "state" / "memory-worker-stage-d-attempts.jsonl"
    assert audit_path.is_file()
    audit_lines = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    assert len(audit_lines) == 1
    audit = audit_lines[0]
    assert audit["source"] == "memory-worker-stage-d"
    assert audit["status"] == "committed"
    assert audit["note_a"] == "alpha"
    assert audit["note_b"] == "beta"

    # The audit_hash matches the file's content sha.
    out_file = mind / "cortex-memory" / report.synthesis_path
    import hashlib

    expected = hashlib.sha256(out_file.read_bytes()).hexdigest()
    assert audit["audit_hash"] == expected


def test_commit_gate_called_even_via_direct_helper(mind: pathlib.Path):
    """commit_stage_d_synthesis is the structural gate — calling it
    directly still produces the audit row (no LLM-prompt advisory)."""
    note_a = _write_research_note(mind, "alpha", tags=["a"])
    note_b = _write_research_note(mind, "beta", tags=["b"])
    synthesis_path = (
        mind / "cortex-memory" / "research" / "2026-06-10-recombination-direct.md"
    )
    content = "---\nsource: stage-d\nnote_a: alpha\nnote_b: beta\n---\n\nBody.\n"

    rec = stage_d.commit_stage_d_synthesis(
        mind,
        note_a=note_a,
        note_b=note_b,
        synthesis_path=synthesis_path,
        note_content=content,
    )
    # Audit + pairs both got their lines.
    assert rec["status"] == "committed"
    assert (mind / "inner" / "state" / "memory-worker-stage-d-attempts.jsonl").is_file()
    pairs_lines = (
        (mind / "inner" / "state" / "stage-d-pairs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert any(json.loads(ln).get("outcome") == "synthesized" for ln in pairs_lines)
    assert synthesis_path.is_file()


def test_run_journal_committed_on_success(mind: pathlib.Path):
    """A successful Stage D cycle leaves a COMMITTED ``recombination``
    journal entry."""
    today = datetime.date(2026, 6, 10)
    _write_research_note(mind, "alpha", tags=["a"])
    _write_research_note(mind, "beta", tags=["b"])
    journal_path = mind / "journal.jsonl"
    report = stage_d.run(
        mind,
        synthesizer=_make_synth(),
        today=today,
        journal_path=journal_path,
    )
    assert report.synthesized == 1
    entries = journal_mod.load(journal_path)
    assert len(entries) == 1
    assert entries[0].op == "recombination"
    assert entries[0].status == journal_mod.COMMITTED


# ---------- crash recovery ----------


def test_replay_marks_recombination_committed_when_vault_and_audit_match(
    mind: pathlib.Path,
):
    """A pending ``recombination`` entry whose synthesis file + audit
    row both exist is re-marked COMMITTED on replay."""
    research = mind / "cortex-memory" / "research"
    research.mkdir(parents=True, exist_ok=True)
    syn = research / "2026-06-10-recombination-test.md"
    syn.write_text("---\nsource: stage-d\n---\nbody\n", encoding="utf-8")
    audit_hash = "abc123" * 10
    # Audit row pre-seeded.
    audit_path = mind / "inner" / "state" / "memory-worker-stage-d-attempts.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-06-10T00:00:00Z",
                "note_a": "alpha",
                "note_b": "beta",
                "synthesis_path": "research/2026-06-10-recombination-test.md",
                "audit_hash": audit_hash,
                "status": "committed",
                "source": "memory-worker-stage-d",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal_path = mind / "journal.jsonl"
    journal_mod.append(
        journal_path,
        op="recombination",
        source="research/alpha",
        targets=["research/2026-06-10-recombination-test.md"],
        detail={
            "note_a": "alpha",
            "note_b": "beta",
            "synthesis_path": "research/2026-06-10-recombination-test.md",
            "audit_hash": audit_hash,
        },
        journal_id="crashed-recomb",
    )

    stage_d.register_verifiers(mind)
    report = journal_mod.replay(journal_path)
    assert report.committed == 1

    after = {e.journal_id: e.status for e in journal_mod.load(journal_path)}
    assert after["crashed-recomb"] == journal_mod.COMMITTED


def test_replay_marks_recombination_skipped_when_audit_missing(
    mind: pathlib.Path,
):
    """If the synthesis file is on disk but the audit row never
    landed, the entry is marked SKIPPED — the invariant didn't hold."""
    research = mind / "cortex-memory" / "research"
    research.mkdir(parents=True, exist_ok=True)
    syn = research / "2026-06-10-recombination-test.md"
    syn.write_text("---\nsource: stage-d\n---\nbody\n", encoding="utf-8")
    # No audit row.

    journal_path = mind / "journal.jsonl"
    journal_mod.append(
        journal_path,
        op="recombination",
        source="research/alpha",
        targets=["research/2026-06-10-recombination-test.md"],
        detail={
            "note_a": "alpha",
            "note_b": "beta",
            "synthesis_path": "research/2026-06-10-recombination-test.md",
            "audit_hash": "deadbeef" * 8,
        },
        journal_id="partial-recomb",
    )

    stage_d.register_verifiers(mind)
    report = journal_mod.replay(journal_path)
    assert report.skipped == 1

    after = {e.journal_id: e.status for e in journal_mod.load(journal_path)}
    assert after["partial-recomb"] == journal_mod.SKIPPED


# ---------- vault_lock retrofit ----------


def test_synthesis_write_acquires_vault_lock(
    mind: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Pre-acquiring the synthesis target's sidecar in a separate
    process-equivalent (here: a child process via ``multiprocessing``
    would be ideal, but ``flock`` is per-fd within a process and a
    second-fd-on-the-same-process re-acquires successfully — so we use
    a subprocess-style lock-holding by opening the sidecar manually in
    THIS process and never releasing, then call commit through a
    monkeypatched short timeout). Proves the lock path is wired."""
    _write_research_note(mind, "alpha", tags=["a"])
    _write_research_note(mind, "beta", tags=["b"])
    target = (
        mind / "cortex-memory" / "research" / "2026-06-10-recombination-x.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    # Shrink the timeout so the test finishes in well under a second.
    monkeypatch.setattr(stage_d, "_LOCK_TIMEOUT_SECONDS", 0.2)

    # Hold the sidecar from a daemon thread. ``fcntl.flock`` is
    # advisory + process-scoped, but a SECOND ``acquire`` in the SAME
    # process re-acquires successfully (no thread-local fallback in the
    # phase-1 design). To prove the lock is wired we use ``subprocess``
    # via ``os.fork`` if available, falling back to the structural
    # check below.
    import os

    pid = os.fork()
    if pid == 0:
        # Child: acquire the lock and sleep. Parent kills us in
        # ``finally``.
        try:
            with vault_lock.acquire(target, mode=vault_lock.LockMode.EXCLUSIVE):
                import time as _time

                _time.sleep(5.0)
        finally:
            os._exit(0)

    try:
        # Give the child a moment to grab the lock.
        import time as _time

        _time.sleep(0.1)

        with pytest.raises(vault_lock.VaultLockTimeout):
            stage_d.commit_stage_d_synthesis(
                mind,
                note_a=mind / "cortex-memory" / "research" / "alpha.md",
                note_b=mind / "cortex-memory" / "research" / "beta.md",
                synthesis_path=target,
                note_content="---\nsource: stage-d\n---\nbody\n",
            )
    finally:
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
