"""Tests for ``metrics.birth_signal``.

Coverage:

- Detection: zero-access notes >= N days, folder exclusions
  (dailies/, archive/, gh-state/, experiments/), special-file
  exclusions (index/README/daily-index TOC files)
- Burst-day computation: >= 5 notes in 24h
- Keyword classification (investigation vs operational, operational
  wins ties, no keywords -> ambiguous, token-split matching)
- Bucket A / B / Ambiguous classification with all combinations
- ``reference/`` folder is ALWAYS Bucket B
- Inbound-from-later detection (note linked by a younger note ->
  not Bucket A)
- Fixture-based integration test on a small mock vault
- Pattern library YAML loading and fallback
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from metrics.birth_signal import (
    BURST_SESSION_MIN_NOTES,
    birth_signal_event_exists_for_date,
    build_birth_signal_event,
    classify_keywords,
    classify_note,
    compute_birth_signal,
    compute_burst_days,
    compute_inbound_from_later,
    detect_zero_access_notes,
    load_pattern_library,
    main as birth_signal_main,
)


# Fixed reference date so the age-day filter is deterministic. All
# tests anchor against this; relative ``created`` dates are derived
# from ``_TODAY - timedelta(days=...)``.
_TODAY = datetime(2026, 6, 26)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip("\n"), encoding="utf-8")
    return path


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "cortex-memory"
    vault.mkdir()
    for sub in ("research", "reference", "projects", "dailies", "archive",
                "gh-state", "experiments", "decisions"):
        (vault / sub).mkdir()
    return vault


# ---------------------------------------------------------------------------
# Detection


def test_detect_zero_access_finds_old_zero_notes(tmp_path: Path) -> None:
    """A research/ note with access_count=0 and age>=30 is a candidate."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "old-zero.md",
        f"""
        ---
        slug: old-zero
        created: {(_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")}
        access_count: 0
        ---
        Body.
        """,
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    assert len(notes) == 1
    assert notes[0]["slug"] == "old-zero"
    assert notes[0]["folder"] == "research"


def test_detect_excludes_recent_zero_notes(tmp_path: Path) -> None:
    """A note younger than the age threshold is excluded."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "recent.md",
        f"""
        ---
        slug: recent
        created: {(_TODAY - timedelta(days=10)).strftime("%Y-%m-%d")}
        access_count: 0
        ---
        """,
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    assert notes == []


def test_detect_excludes_accessed_notes(tmp_path: Path) -> None:
    """A note with access_count > 0 is not zero-access."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "accessed.md",
        f"""
        ---
        slug: accessed
        created: {(_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")}
        access_count: 3
        ---
        """,
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    assert notes == []


def test_detect_excludes_folders(tmp_path: Path) -> None:
    """dailies/, archive/, gh-state/, experiments/ are skipped."""
    vault = _make_vault(tmp_path)
    old = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    for folder in ("dailies", "archive", "gh-state", "experiments"):
        _write(
            vault / folder / f"{folder}-zero.md",
            f"""
            ---
            slug: {folder}-zero
            created: {old}
            access_count: 0
            ---
            """,
        )
    # A real candidate so the scan isn't trivially empty.
    _write(
        vault / "research" / "real.md",
        f"""
        ---
        slug: real
        created: {old}
        access_count: 0
        ---
        """,
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    slugs = {n["slug"] for n in notes}
    assert slugs == {"real"}


def test_detect_excludes_special_files(tmp_path: Path) -> None:
    """index.md, README.md, *-index.md, and daily-tagged notes are skipped."""
    vault = _make_vault(tmp_path)
    old = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    # Scaffolding files at vault root.
    _write(vault / "index.md", f"---\nslug: index\ncreated: {old}\naccess_count: 0\n---\n")
    _write(vault / "README.md", f"---\nslug: readme\ncreated: {old}\naccess_count: 0\n---\n")
    # TOC files like decisions-index.md.
    _write(
        vault / "decisions-index.md",
        f"---\nslug: decisions-index\ncreated: {old}\naccess_count: 0\n---\n",
    )
    # _index.md under a folder.
    _write(
        vault / "projects" / "_index.md",
        f"---\nslug: projects-index\ncreated: {old}\naccess_count: 0\n---\n",
    )
    # A note explicitly tagged as a daily (in research/, edge case).
    _write(
        vault / "research" / "fake-daily.md",
        f"""
        ---
        slug: fake-daily
        note_type: daily
        created: {old}
        access_count: 0
        ---
        """,
    )
    # And a real candidate.
    _write(
        vault / "research" / "real.md",
        f"---\nslug: real\ncreated: {old}\naccess_count: 0\n---\n",
    )

    notes = detect_zero_access_notes(vault, today=_TODAY)
    slugs = {n["slug"] for n in notes}
    assert slugs == {"real"}


def test_detect_skips_unparseable_access_count(tmp_path: Path) -> None:
    """A note with no parseable access_count is data quality, not a signal."""
    vault = _make_vault(tmp_path)
    old = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    _write(
        vault / "research" / "no-ac.md",
        f"---\nslug: no-ac\ncreated: {old}\n---\nNo access_count field.\n",
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    assert notes == []


def test_detect_skips_archived_status(tmp_path: Path) -> None:
    """status: archived signals an already-handled note — skip it."""
    vault = _make_vault(tmp_path)
    old = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    _write(
        vault / "research" / "archived.md",
        f"---\nslug: archived\nstatus: archived\ncreated: {old}\naccess_count: 0\n---\n",
    )
    notes = detect_zero_access_notes(vault, today=_TODAY)
    assert notes == []


# ---------------------------------------------------------------------------
# Burst-day computation


def test_compute_burst_days_flags_busy_days(tmp_path: Path) -> None:
    """A calendar day with >= 5 candidates is a burst day."""
    candidates = []
    burst_day = (_TODAY - timedelta(days=60)).replace(hour=0, minute=0)
    quiet_day = (_TODAY - timedelta(days=45)).replace(hour=0, minute=0)
    for i in range(BURST_SESSION_MIN_NOTES):
        candidates.append({"created": burst_day, "slug": f"burst-{i}"})
    candidates.append({"created": quiet_day, "slug": "quiet"})
    burst_days = compute_burst_days(candidates)
    assert burst_day.strftime("%Y-%m-%d") in burst_days
    assert quiet_day.strftime("%Y-%m-%d") not in burst_days


def test_compute_burst_days_threshold_strict(tmp_path: Path) -> None:
    """Exactly 4 notes on a day is NOT a burst (floor is 5)."""
    day = (_TODAY - timedelta(days=60)).replace(hour=0, minute=0)
    candidates = [{"created": day, "slug": f"n-{i}"} for i in range(4)]
    assert compute_burst_days(candidates) == set()


# ---------------------------------------------------------------------------
# Keyword classification


def test_classify_keywords_investigation() -> None:
    inv = frozenset({"decay", "investigation"})
    op = frozenset({"protein"})
    assert classify_keywords(["decay-metrics"], inv, op) == "investigation"
    assert classify_keywords(["investigation"], inv, op) == "investigation"


def test_classify_keywords_operational() -> None:
    inv = frozenset({"decay"})
    op = frozenset({"protein", "workout"})
    assert classify_keywords(["protein tracking"], inv, op) == "operational"
    assert classify_keywords(["workout"], inv, op) == "operational"


def test_classify_keywords_operational_wins_ties() -> None:
    """When a note has both investigation and operational keywords,
    operational wins (a note that's both useful AND was investigated
    is still useful)."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    assert (
        classify_keywords(["decay analysis", "protein tracking"], inv, op)
        == "operational"
    )


def test_classify_keywords_no_keywords_ambiguous() -> None:
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    assert classify_keywords([], inv, op) == "ambiguous"


def test_classify_keywords_unrelated_keywords_ambiguous() -> None:
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    assert classify_keywords(["unrelated topic"], inv, op) == "ambiguous"


def test_classify_keywords_token_split_match() -> None:
    """A hyphenated keyword like 'decay-metrics' should match
    'decay' as a token. Same for underscores."""
    inv = frozenset({"decay", "investigation"})
    op = frozenset()
    assert classify_keywords(["decay_metric"], inv, op) == "investigation"
    assert classify_keywords(["decay-metric"], inv, op) == "investigation"


# ---------------------------------------------------------------------------
# Bucket classification


def _note(
    *,
    slug: str,
    folder: str = "research",
    created: datetime | None = None,
    keywords: list[str] | None = None,
) -> dict:
    return {
        "slug": slug,
        "folder": folder,
        "created": created or (_TODAY - timedelta(days=45)),
        "trigger_keywords": keywords or [],
        "fm": {},
        "rel": f"{folder}/{slug}.md",
        "path": Path(f"{folder}/{slug}.md"),
    }


def test_classify_bucket_a_burst_artifact() -> None:
    """Burst day + investigation keywords + no inbound => Bucket A."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    burst_day = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    note = _note(
        slug="ba",
        created=_TODAY - timedelta(days=45),
        keywords=["decay analysis"],
    )
    result = classify_note(
        note, burst_days={burst_day}, investigation=inv, operational=op,
        inbound_from_later={},
    )
    assert result == "burst_artifact"


def test_classify_bucket_b_operational_keywords() -> None:
    """Operational keywords => Bucket B regardless of burst origin."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    burst_day = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    note = _note(
        slug="bb",
        created=_TODAY - timedelta(days=45),
        keywords=["protein"],
    )
    assert classify_note(
        note, burst_days={burst_day}, investigation=inv, operational=op,
        inbound_from_later={},
    ) == "useful_poorly_linked"


def test_classify_bucket_b_has_inbound_from_later() -> None:
    """A note other notes link to later is useful → Bucket B."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    burst_day = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    note = _note(
        slug="bb-inbound",
        created=_TODAY - timedelta(days=45),
        keywords=["decay analysis"],
    )
    assert classify_note(
        note, burst_days={burst_day}, investigation=inv, operational=op,
        inbound_from_later={"bb-inbound": 3},
    ) == "useful_poorly_linked"


def test_classify_bucket_b_non_burst_origin() -> None:
    """Non-burst origin => Bucket B even with investigation keywords."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    note = _note(
        slug="bb-non-burst",
        created=_TODAY - timedelta(days=45),
        keywords=["decay"],
    )
    # No burst days at all.
    assert classify_note(
        note, burst_days=set(), investigation=inv, operational=op,
        inbound_from_later={},
    ) == "useful_poorly_linked"


def test_classify_reference_always_bucket_b() -> None:
    """reference/ is ALWAYS Bucket B (evergreen, needs bridging)."""
    inv = frozenset({"decay"})
    op = frozenset()
    burst_day = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    # Even with investigation keywords + burst origin + no inbound,
    # reference/ wins as B.
    note = _note(
        slug="ref-note",
        folder="reference",
        created=_TODAY - timedelta(days=45),
        keywords=["decay"],
    )
    assert classify_note(
        note, burst_days={burst_day}, investigation=inv, operational=op,
        inbound_from_later={},
    ) == "useful_poorly_linked"


def test_classify_ambiguous() -> None:
    """Burst + no keywords + no inbound => ambiguous (the fall-through)."""
    inv = frozenset({"decay"})
    op = frozenset({"protein"})
    burst_day = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    note = _note(
        slug="amb",
        created=_TODAY - timedelta(days=45),
        keywords=[],  # no trigger keywords at all
    )
    assert classify_note(
        note, burst_days={burst_day}, investigation=inv, operational=op,
        inbound_from_later={},
    ) == "ambiguous"


# ---------------------------------------------------------------------------
# Inbound-from-later


def test_compute_inbound_from_later_counts_younger_links(tmp_path: Path) -> None:
    """An older candidate referenced by a younger note shows inbound > 0."""
    vault = _make_vault(tmp_path)
    older = (_TODAY - timedelta(days=60)).strftime("%Y-%m-%d")
    younger = (_TODAY - timedelta(days=30)).strftime("%Y-%m-%d")
    _write(
        vault / "research" / "target.md",
        f"---\nslug: target\ncreated: {older}\naccess_count: 0\n---\nTarget body.\n",
    )
    _write(
        vault / "research" / "younger.md",
        f"""
        ---
        slug: younger
        created: {younger}
        access_count: 1
        ---
        See [[target]] for details.
        """,
    )
    candidates = detect_zero_access_notes(vault, today=_TODAY)
    inbound = compute_inbound_from_later(vault, candidates)
    assert inbound.get("target", 0) == 1


def test_compute_inbound_ignores_older_links(tmp_path: Path) -> None:
    """A note linked by an OLDER note (predates candidate) doesn't count."""
    vault = _make_vault(tmp_path)
    older = (_TODAY - timedelta(days=90)).strftime("%Y-%m-%d")
    candidate = (_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")
    _write(
        vault / "research" / "target.md",
        f"---\nslug: target\ncreated: {candidate}\naccess_count: 0\n---\n",
    )
    _write(
        vault / "research" / "older-source.md",
        f"""
        ---
        slug: older-source
        created: {older}
        access_count: 1
        ---
        Pre-existing reference to [[target]].
        """,
    )
    candidates = detect_zero_access_notes(vault, today=_TODAY)
    inbound = compute_inbound_from_later(vault, candidates)
    # source predates target so inbound stays 0
    assert inbound.get("target", 0) == 0


def test_compute_inbound_via_frontmatter_references(tmp_path: Path) -> None:
    """Frontmatter ``references:`` lists count as inbound links."""
    vault = _make_vault(tmp_path)
    older = (_TODAY - timedelta(days=60)).strftime("%Y-%m-%d")
    younger = (_TODAY - timedelta(days=30)).strftime("%Y-%m-%d")
    _write(
        vault / "research" / "target.md",
        f"---\nslug: target\ncreated: {older}\naccess_count: 0\n---\n",
    )
    _write(
        vault / "research" / "younger.md",
        f"""
        ---
        slug: younger
        created: {younger}
        access_count: 1
        references:
          - [[target]] — useful prior work
        ---
        Body without a body wikilink.
        """,
    )
    candidates = detect_zero_access_notes(vault, today=_TODAY)
    inbound = compute_inbound_from_later(vault, candidates)
    assert inbound.get("target", 0) == 1


# ---------------------------------------------------------------------------
# Pattern library YAML loading


def test_load_pattern_library_defaults_when_missing(tmp_path: Path) -> None:
    """No path → in-module defaults from the spec."""
    inv, op = load_pattern_library(None)
    assert "decay" in inv
    assert "protein" in op


def test_load_pattern_library_yaml_parse(tmp_path: Path) -> None:
    """Read keyword sets from a simple YAML file."""
    yaml = tmp_path / "patterns.yaml"
    yaml.write_text(
        "investigation:\n  - foo\n  - bar\noperational:\n  - baz\n",
        encoding="utf-8",
    )
    inv, op = load_pattern_library(yaml)
    assert inv == frozenset({"foo", "bar"})
    assert op == frozenset({"baz"})


def test_load_pattern_library_empty_yaml_falls_back(tmp_path: Path) -> None:
    yaml = tmp_path / "empty.yaml"
    yaml.write_text("# just comments\n", encoding="utf-8")
    inv, op = load_pattern_library(yaml)
    assert "decay" in inv  # fell back to defaults
    assert "protein" in op


# ---------------------------------------------------------------------------
# Fixture-based integration


def test_compute_birth_signal_end_to_end(tmp_path: Path) -> None:
    """Small mock vault — assert the bucketed payload counts."""
    vault = _make_vault(tmp_path)
    # 5 burst-day investigation notes (Bucket A targets).
    burst_day = _TODAY - timedelta(days=60)
    for i in range(5):
        _write(
            vault / "research" / f"burst-{i}.md",
            f"""
            ---
            slug: burst-{i}
            created: {burst_day.strftime("%Y-%m-%d")}
            access_count: 0
            trigger_keywords: [decay-investigation, pilot]
            ---
            Burst body {i}.
            """,
        )
    # Operational note (Bucket B regardless of burst).
    _write(
        vault / "research" / "op-note.md",
        f"""
        ---
        slug: op-note
        created: {(_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")}
        access_count: 0
        trigger_keywords: [protein tracking]
        ---
        """,
    )
    # reference/ note (always Bucket B).
    _write(
        vault / "reference" / "evergreen.md",
        f"""
        ---
        slug: evergreen
        created: {(_TODAY - timedelta(days=80)).strftime("%Y-%m-%d")}
        access_count: 0
        trigger_keywords: [decay analysis]
        ---
        """,
    )
    # Ambiguous note — burst day, no keywords, no inbound.
    amb_day = _TODAY - timedelta(days=55)
    for i in range(5):
        _write(
            vault / "research" / f"amb-{i}.md",
            f"""
            ---
            slug: amb-{i}
            created: {amb_day.strftime("%Y-%m-%d")}
            access_count: 0
            ---
            """,
        )
    # A "younger linker" that should pull burst-0 into Bucket B by
    # giving it inbound-from-later.
    _write(
        vault / "research" / "linker.md",
        f"""
        ---
        slug: linker
        created: {(_TODAY - timedelta(days=30)).strftime("%Y-%m-%d")}
        access_count: 1
        ---
        See [[burst-0]] for context.
        """,
    )

    payload = compute_birth_signal(vault, today=_TODAY)

    # Sanity: ~ 11 zero-access candidates (5 burst, 5 amb, op-note,
    # evergreen). 'linker' has access_count=1 so it's not a candidate.
    assert payload["candidates"] == 12

    # Bucket A: 4 of the 5 burst notes (burst-0 was linked by 'linker',
    # so it moves to Bucket B).
    assert payload["burst_artifacts"] == 4
    # Bucket B includes: op-note, evergreen, burst-0 (now has inbound).
    assert payload["useful_poorly_linked"] == 3
    # Ambiguous: the 5 amb notes.
    assert payload["ambiguous"] == 5

    # Observability fields are always 0 for the detection-only module.
    assert payload["archived_last_cycle"] == 0
    assert payload["bridged_last_cycle"] == 0

    # Counts sum to candidates.
    assert (
        payload["burst_artifacts"]
        + payload["useful_poorly_linked"]
        + payload["ambiguous"]
        == payload["candidates"]
    )


def test_build_birth_signal_event_shape(tmp_path: Path) -> None:
    """The event dict has the spec's required fields."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "x.md",
        f"""
        ---
        slug: x
        created: {(_TODAY - timedelta(days=45)).strftime("%Y-%m-%d")}
        access_count: 0
        ---
        """,
    )
    event = build_birth_signal_event(vault, now=_TODAY)
    # Required fields per spec.
    for key in (
        "ts", "type", "date", "time",
        "total_zero_access_notes", "candidates",
        "burst_artifacts", "useful_poorly_linked", "ambiguous",
        "archived_last_cycle", "bridged_last_cycle",
    ):
        assert key in event, f"missing event field: {key}"
    assert event["type"] == "birth_signal"
    assert event["date"] == _TODAY.strftime("%Y-%m-%d")
    # JSON-serializable as a single line.
    line = json.dumps(event)
    assert json.loads(line) == event


# ---------------------------------------------------------------------------
# Event-file helpers


def test_event_dedup(tmp_path: Path) -> None:
    """The dedup helper matches the vault_health pattern."""
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "ts": "2026-06-26T07:00:00-04:00",
                "type": "birth_signal",
                "date": "2026-06-26",
            }
        ) + "\n",
        encoding="utf-8",
    )
    assert birth_signal_event_exists_for_date(events, "2026-06-26") is True
    assert birth_signal_event_exists_for_date(events, "2026-06-25") is False
    assert birth_signal_event_exists_for_date(tmp_path / "missing.jsonl", "x") is False


# ---------------------------------------------------------------------------
# CLI


def test_cli_dry_run(tmp_path: Path, capsys) -> None:
    """--dry-run prints the payload and writes nothing."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "x.md",
        f"---\nslug: x\ncreated: {(_TODAY - timedelta(days=45)).strftime('%Y-%m-%d')}\naccess_count: 0\n---\n",
    )
    events = tmp_path / "events.jsonl"
    rc = birth_signal_main(["--vault", str(vault), "--dry-run"])
    assert rc == 0
    # Nothing written.
    assert not events.exists()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "candidates" in parsed
    assert parsed["candidates"] == 1


def test_cli_writes_event(tmp_path: Path) -> None:
    """Without --dry-run, the event is appended to --events."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "x.md",
        f"---\nslug: x\ncreated: {(_TODAY - timedelta(days=45)).strftime('%Y-%m-%d')}\naccess_count: 0\n---\n",
    )
    events = tmp_path / "events.jsonl"
    rc = birth_signal_main(
        ["--vault", str(vault), "--events", str(events)],
    )
    assert rc == 0
    line = events.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["type"] == "birth_signal"
    assert parsed["candidates"] == 1


def test_cli_check_existing_skips(tmp_path: Path) -> None:
    """--check-existing skips the write when today's event is on disk."""
    vault = _make_vault(tmp_path)
    today_str = datetime.now().strftime("%Y-%m-%d")
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "ts": "2026-06-26T07:00:00-04:00",
                "type": "birth_signal",
                "date": today_str,
            }
        ) + "\n",
        encoding="utf-8",
    )
    before = events.read_text(encoding="utf-8")
    rc = birth_signal_main(
        ["--vault", str(vault), "--events", str(events), "--check-existing"],
    )
    assert rc == 0
    # No additional event line written.
    assert events.read_text(encoding="utf-8") == before


def test_cli_missing_events_errors(tmp_path: Path) -> None:
    """Without --dry-run AND without --events, the CLI errors."""
    vault = _make_vault(tmp_path)
    with pytest.raises(SystemExit):
        birth_signal_main(["--vault", str(vault)])
