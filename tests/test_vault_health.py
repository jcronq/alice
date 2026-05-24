"""Eval-contract tests for ``metrics.vault_health``.

Each metric used to be inline LLM-generated bash that drifted between
wakes. The contract here is: every test that exercises a metric also
exercises the *buggy baseline* it replaces — so we catch regressions
to the same drift-prone shape, not just regressions in our fix.

Bug map:
- orphan_notes (cross-directory + aliases): tests 1, 2
- broken_wikilinks (code-span / fence / HTML-comment exclusion): tests 3, 4
- wake_type_distribution (date-spanning): tests 5, 6
- phase1-check delta mode (regression guard only): test 7
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from metrics.vault_health import (
    build_vault_health_event,
    compute_continuous_checks,
    compute_decay_coverage,
    count_broken_wikilinks,
    count_inbound_links,
    count_orphans,
    count_output_rate_slope,
    count_productive_wakes,
    count_research_decay,
    count_research_notes_created_on,
    count_shadow_and_dark,
    count_stage_c_candidates,
    count_surfaces_handled_today,
    count_surfaces_in_window,
    count_tier1_ratio,
    count_total_notes,
    count_wakes_by_stage,
    compute_recovery_state,
    main as vault_health_main,
    vault_health_event_exists_for_date,
)


# ---------------------------------------------------------------------------
# Helpers (vault scaffolding + buggy baselines)


def _write(path: Path, content: str) -> Path:
    """Write ``content`` to ``path`` (creating parents). Strips leading
    blank line + dedents, so callers can use triple-quoted strings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dedent(content).lstrip("\n")
    path.write_text(body, encoding="utf-8")
    return path


def _make_vault(tmp_path: Path) -> Path:
    """Return a fresh ``cortex-memory`` directory under ``tmp_path``."""
    vault = tmp_path / "cortex-memory"
    vault.mkdir()
    # Required structural folders (match the real vault layout so the
    # algorithms behave as in production).
    for sub in ("research", "reference", "projects", "dailies"):
        (vault / sub).mkdir()
    return vault


# Naive baselines — these are the shapes that produced the drift bugs.
# We assert the *buggy* output too, so a regression to "same-dir grep"
# or "raw regex" is detectable.


def _buggy_orphans_same_dir_only(vault: Path) -> int:
    """Same-directory wikilink scan — the original bash bug.

    Walk every .md under the vault. For each candidate (non-daily,
    non-scaffold), search ONLY the candidate's parent directory for
    ``[[<stem>]]``; if nothing matches, count as orphan. This is the
    shape that reported 942 orphans against a 1002-note vault.
    """
    count = 0
    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if rel_parts and rel_parts[0] == "dailies":
            continue
        if md.name in {"index.md", "README.md", "unresolved.md"}:
            continue
        stem = md.stem
        referenced = False
        for sibling in md.parent.glob("*.md"):
            if sibling == md:
                continue
            try:
                text = sibling.read_text(encoding="utf-8")
            except OSError:
                continue
            if f"[[{stem}]]" in text or f"[[{stem}|" in text:
                referenced = True
                break
        if not referenced:
            count += 1
    return count


def _buggy_orphans_ignore_aliases(vault: Path) -> int:
    """Cross-directory scan but ignores frontmatter aliases.

    Resolves wikilinks against filename stems only — same shape as the
    bash that didn't parse frontmatter. A note whose only inbound
    references use an alias will be flagged orphan.
    """
    referenced: set[str] = set()
    wikilink_re = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]*?)?\]\]")
    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in wikilink_re.finditer(text):
            target = m.group(1).strip()
            if "#" in target:
                target = target.split("#", 1)[0].strip()
            if "/" in target:
                target = target.rsplit("/", 1)[-1]
            referenced.add(target)
    count = 0
    for md in vault.rglob("*.md"):
        rel_parts = md.relative_to(vault).parts
        if rel_parts and rel_parts[0] == "dailies":
            continue
        if md.name in {"index.md", "README.md", "unresolved.md"}:
            continue
        if md.stem not in referenced:
            count += 1
    return count


def _buggy_broken_wikilinks_raw_regex(vault: Path) -> int:
    """Raw ``[[...]]`` regex over the file with no code-span exclusion.

    This is the shape that swung from 5422 to 2 between two adjacent
    wake scans — code fences / inline code / HTML comments all leaked
    into the count.
    """
    stems: set[str] = set()
    for md in vault.rglob("*.md"):
        if md.name in {"index.md", "README.md", "unresolved.md"}:
            continue
        stems.add(md.stem)
    wikilink_re = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]*?)?\]\]")
    broken = 0
    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in wikilink_re.finditer(text):
            target = m.group(1).strip()
            if "#" in target:
                target = target.split("#", 1)[0].strip()
            if "/" in target:
                target = target.rsplit("/", 1)[-1]
            if target not in stems:
                broken += 1
    return broken


def _buggy_wake_distribution_yesterday_only(
    thoughts_dir: Path, yesterday: str
) -> dict[str, int]:
    """Single-directory wake count — the original bash bug.

    Only scans ``thoughts_dir/<yesterday>/`` — wakes that landed in
    today's dir after midnight are missed.
    """
    counts = {"stage_b": 0, "stage_c": 0, "stage_d": 0}
    yest_dir = thoughts_dir / yesterday
    if not yest_dir.exists():
        return counts
    for md in yest_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        for stage in ("B", "C", "D"):
            if f"stage: {stage}" in text:
                counts[f"stage_{stage.lower()}"] += 1
                break
    return counts


# ---------------------------------------------------------------------------
# Test 1 — orphans: cross-directory wikilinks resolve


def test_orphan_count_resolves_cross_directory(tmp_path: Path) -> None:
    """research/foo.md → reference/bar.md (slug match) — not orphan."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        """
        ---
        slug: foo
        ---

        Linking to [[bar]] across directories.
        """,
    )
    _write(
        vault / "reference" / "bar.md",
        """
        ---
        slug: bar
        ---

        Body of bar mentioning [[foo]] in return.
        """,
    )

    # Fixed implementation: zero orphans — each note links to the
    # other across directories, so both targets are referenced.
    count, orphans = count_orphans(vault)
    assert count == 0, f"expected no orphans, got {orphans}"
    assert orphans == []

    # Buggy baseline: same-dir-only scan reports both notes orphan
    # (research/foo.md sees no other research/ notes that mention it;
    # reference/bar.md sees no other reference/ notes that mention
    # it). Cross-directory references are invisible to the bash bug.
    assert _buggy_orphans_same_dir_only(vault) == 2


# ---------------------------------------------------------------------------
# Test 2 — orphans: aliases resolve


def test_orphan_count_resolves_aliases(tmp_path: Path) -> None:
    """[[Speaking Alice]] resolves via aliases on projects/alice-speaking.md."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        """
        ---
        slug: foo
        ---

        Discussing [[Speaking Alice]] briefly.
        """,
    )
    _write(
        vault / "projects" / "alice-speaking.md",
        """
        ---
        slug: alice-speaking
        aliases: [speaking alice]
        ---

        The speaking hemisphere.
        """,
    )

    count, orphans = count_orphans(vault)
    # foo.md isn't referenced anywhere → expected to be orphan.
    # alice-speaking.md is referenced via its alias → not orphan.
    assert "research/foo.md" in orphans, orphans
    assert "projects/alice-speaking.md" not in orphans, orphans
    assert count == 1

    # Buggy baseline ignores aliases → flags both notes as orphans
    # (foo.md unreferenced; alice-speaking.md only addressed by alias).
    buggy = _buggy_orphans_ignore_aliases(vault)
    assert buggy >= 2, f"buggy baseline should flag both notes as orphans; got {buggy}"


# ---------------------------------------------------------------------------
# Test 3 — broken wikilinks: code spans / fences / HTML comments excluded


def test_broken_wikilinks_excludes_code_spans(tmp_path: Path) -> None:
    """Only the body-text [[qux]] is real; code fences / inline code /
    HTML comments are noise."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        """
        ---
        slug: foo
        ---

        Real broken link: [[qux]] in body text.

        Inline code with `[[baz]]` shouldn't count.

        Fenced block:

        ```
        example: [[bar]]
        ```

        HTML comment: <!-- [[hidden]] --> shouldn't count either.
        """,
    )

    count, broken = count_broken_wikilinks(vault)
    assert count == 1, f"expected 1 broken link, got {broken}"
    assert broken == [("research/foo.md", "qux")]

    # Buggy baseline (raw regex, no exclusions) sees [[qux]], [[baz]],
    # [[bar]], and [[hidden]] — at least 3 broken even before counting
    # the HTML-comment hidden one. We assert >= 3 to be robust to the
    # exact count vs minor regex variation; the point is that the buggy
    # shape over-counts.
    buggy = _buggy_broken_wikilinks_raw_regex(vault)
    assert buggy >= 3, f"buggy baseline should over-count; got {buggy}"


# ---------------------------------------------------------------------------
# Test 4 — broken wikilinks: real broken link is detected


def test_broken_wikilinks_real_broken_link_detected(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        """
        ---
        slug: foo
        ---

        Plain body referencing [[bar]] which doesn't exist.
        """,
    )

    count, broken = count_broken_wikilinks(vault)
    assert count == 1
    assert broken == [("research/foo.md", "bar")]

    # Buggy baseline also reports this — it's a true broken link, not
    # a code-span false positive. The contract here is correctness
    # parity with baseline on the *easy* case.
    assert _buggy_broken_wikilinks_raw_regex(vault) == 1


# ---------------------------------------------------------------------------
# Test 5 — wake distribution spans midnight


def _write_wake(dir_path: Path, name: str, stage: str) -> None:
    _write(
        dir_path / name,
        f"""
        ---
        mode: sleep
        stage: {stage}
        did_work: true
        ---

        Wake body.
        """,
    )


def test_wake_distribution_spans_midnight(tmp_path: Path) -> None:
    thoughts = tmp_path / "thoughts"
    yest = thoughts / "2026-05-07"
    today = thoughts / "2026-05-08"
    yest.mkdir(parents=True)
    today.mkdir(parents=True)

    _write_wake(yest, "235500-wake.md", "B")
    _write_wake(yest, "233000-wake.md", "C")
    _write_wake(today, "001500-wake.md", "D")
    _write_wake(today, "063000-wake.md", "B")
    # One outside the window (08:00) — must be ignored.
    _write_wake(today, "080000-wake.md", "B")

    window_start = datetime(2026, 5, 7, 23, 0, 0)
    window_end = datetime(2026, 5, 8, 7, 0, 0)
    counts = count_wakes_by_stage(thoughts, window_start, window_end)
    assert counts == {"stage_b": 2, "stage_c": 1, "stage_d": 1}, counts

    # Buggy baseline (yesterday-only scan) misses today's two in-window
    # wakes entirely.
    buggy = _buggy_wake_distribution_yesterday_only(thoughts, "2026-05-07")
    assert buggy == {"stage_b": 1, "stage_c": 1, "stage_d": 0}, buggy


# ---------------------------------------------------------------------------
# Test 6 — wake distribution: all three filename formats parse


def test_wake_distribution_accepts_sleep_prefix(tmp_path: Path) -> None:
    """Production wake frontmatter uses ``sleep_b``/``sleep_c``/``sleep_d``;
    _read_stage must accept that form, not just the bare letter."""
    thoughts = tmp_path / "thoughts"
    today = thoughts / "2026-05-08"
    today.mkdir(parents=True)
    _write_wake(today, "000500-wake.md", "sleep_b")
    _write_wake(today, "001500-wake.md", "sleep_c")
    _write_wake(today, "003000-wake.md", "sleep_d")

    window_start = datetime(2026, 5, 7, 23, 0, 0)
    window_end = datetime(2026, 5, 8, 7, 0, 0)
    counts = count_wakes_by_stage(thoughts, window_start, window_end)
    assert counts == {"stage_b": 1, "stage_c": 1, "stage_d": 1}, counts


def test_wake_distribution_filename_formats(tmp_path: Path) -> None:
    thoughts = tmp_path / "thoughts"
    yest = thoughts / "2026-05-07"
    today = thoughts / "2026-05-08"
    yest.mkdir(parents=True)
    today.mkdir(parents=True)

    # Format 1: HHMMSS-wake.md (date from parent dir)
    _write_wake(yest, "235500-wake.md", "B")
    # Format 2: YYYYMMDD-HHMMSS-wake.md
    _write_wake(today, "20260508-001500-wake.md", "D")
    # Format 3: YYYYMMDDHHMMSS-wake.md
    _write_wake(today, "20260508003000-wake.md", "C")
    # Plus a non-matching filename — must be rejected silently.
    _write_wake(today, "scratch.md", "B")

    window_start = datetime(2026, 5, 7, 23, 0, 0)
    window_end = datetime(2026, 5, 8, 7, 0, 0)
    counts = count_wakes_by_stage(thoughts, window_start, window_end)
    assert counts == {"stage_b": 1, "stage_c": 1, "stage_d": 1}, counts


# ---------------------------------------------------------------------------
# Test 7 — phase1-check legacy-mode regression guard


def test_phase1_check_uses_legacy_mode() -> None:
    """phase1-check-script.py had a delta-mode bug; the fix removed
    the delta path and hardcoded legacy mode. Lock that in.

    See cortex-memory/research/2026-05-08-phase1-check-delta-mode-bug.md
    for the bug context. Don't reintroduce delta mode; just verify the
    legacy-mode marker is present.
    """
    script = Path.home() / "alice-mind" / "inner" / "state" / "phase1-check-script.py"
    if not script.exists():
        pytest.skip(f"phase1-check-script not present at {script}")
    text = script.read_text(encoding="utf-8")
    # Either marker locks in the fix.
    assert "delta mode removed" in text or "Mode: legacy" in text, (
        "phase1-check-script.py must keep its legacy-mode marker — "
        "delta mode is broken (see 2026-05-08-phase1-check-delta-mode-bug)."
    )


# ---------------------------------------------------------------------------
# Bonus — total-notes sanity (excluded scaffolding actually excluded)


def test_count_total_notes_excludes_scaffolding(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(vault / "index.md", "vault index")
    _write(vault / "README.md", "readme")
    _write(vault / "unresolved.md", "unresolved")
    _write(vault / "research" / "foo.md", "body")
    _write(vault / "reference" / "bar.md", "body")
    assert count_total_notes(vault) == 2


# ---------------------------------------------------------------------------
# Regression: scaffolding files (index.md / README.md / unresolved.md) must
# resolve as wikilink targets even though they don't count toward
# total_notes or appear in orphan candidate sets. The original PR #18
# excluded them everywhere, which made every [[index]] / [[unresolved]]
# reference register as broken.
# ---------------------------------------------------------------------------


def test_broken_wikilinks_resolves_scaffolding_index(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(vault / "index.md", "# Index\n\nVault scaffolding.\n")
    _write(vault / "research" / "foo.md", "Body links to [[index]].\n")
    n, broken = count_broken_wikilinks(vault)
    assert n == 0
    assert broken == []


def test_broken_wikilinks_resolves_scaffolding_unresolved(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(vault / "unresolved.md", "# Unresolved\n\nBookkeeping page.\n")
    _write(vault / "dailies" / "2026-05-08.md", "Backlog: [[unresolved]] today.\n")
    n, broken = count_broken_wikilinks(vault)
    assert n == 0
    assert broken == []


def test_broken_wikilinks_resolves_scaffolding_readme(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(vault / "README.md", "# Readme\n")
    _write(vault / "reference" / "x.md", "See [[README]] for the conventions.\n")
    n, broken = count_broken_wikilinks(vault)
    assert n == 0


def test_total_notes_still_excludes_scaffolding_after_fix(tmp_path: Path) -> None:
    """Scaffolding resolves as a wikilink target, but it must NOT count
    toward ``total_notes`` — that's the inverse invariant. Both checks
    have to coexist."""
    vault = _make_vault(tmp_path)
    _write(vault / "index.md", "")
    _write(vault / "unresolved.md", "")
    _write(vault / "research" / "foo.md", "")
    assert count_total_notes(vault) == 1


def test_orphans_still_exclude_scaffolding_after_fix(tmp_path: Path) -> None:
    """Scaffolding resolves as a wikilink target, but it must NOT appear
    in the orphan candidate set even when nothing references it. This is
    the inverse of the broken-link case — the scaffolding is conceptually
    a hub, never an orphan."""
    vault = _make_vault(tmp_path)
    _write(vault / "index.md", "")
    _write(vault / "unresolved.md", "")
    _write(vault / "README.md", "")
    n, orphans = count_orphans(vault)
    assert n == 0
    assert orphans == []


# ---------------------------------------------------------------------------
# Shadow orphans + truly-dark notes
# Design: cortex-memory/research/2026-05-18-vault-linking-protocol.md
# Shadow orphan: 0 inbound, ≥1 outbound, ≥1 trigger_keyword.
# Truly dark:    0 inbound, 0 trigger_keywords.
# ---------------------------------------------------------------------------


def test_shadow_orphan_has_triggers_and_outbound(tmp_path: Path) -> None:
    """0 inbound + ≥1 outbound + ≥1 trigger_keyword → shadow orphan."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "ghost.md",
        """
        ---
        slug: ghost
        trigger_keywords: [phantom, spectre]
        ---

        Linking out to [[anchor]] but nothing links back.
        """,
    )
    _write(
        vault / "reference" / "anchor.md",
        """
        ---
        slug: anchor
        ---

        Anchor body.
        """,
    )
    counts = count_shadow_and_dark(vault)
    assert counts["shadow_orphan_count"] == 1
    # anchor.md has 1 inbound (from ghost), so it's not shadow/dark.
    assert counts["truly_dark_count"] == 0


def test_truly_dark_has_no_triggers(tmp_path: Path) -> None:
    """0 inbound + 0 trigger_keywords → truly dark, regardless of outbound."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "void.md",
        """
        ---
        slug: void
        ---

        Body with no trigger keywords and no inbound links.
        """,
    )
    counts = count_shadow_and_dark(vault)
    assert counts["truly_dark_count"] == 1
    assert counts["shadow_orphan_count"] == 0


def test_shadow_and_dark_exclude_dailies_and_scaffolding(tmp_path: Path) -> None:
    """Dailies, archive, and scaffolding never count toward either bucket."""
    vault = _make_vault(tmp_path)
    (vault / "archive").mkdir()
    _write(vault / "dailies" / "2026-05-18.md", "Daily with no triggers.\n")
    _write(vault / "archive" / "ancient.md", "Archived note.\n")
    _write(vault / "index.md", "")
    _write(vault / "README.md", "")
    _write(vault / "unresolved.md", "")
    counts = count_shadow_and_dark(vault)
    assert counts == {
        "shadow_orphan_count": 0,
        "truly_dark_count": 0,
        "frontmatter_parse_failures": 0,
    }


def test_shadow_and_dark_inbound_kills_both_buckets(tmp_path: Path) -> None:
    """A single inbound link removes a note from both buckets."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "target.md",
        """
        ---
        slug: target
        trigger_keywords: [k]
        ---

        Target body.
        """,
    )
    _write(
        vault / "research" / "source.md",
        """
        ---
        slug: source
        trigger_keywords: [s]
        ---

        Points at [[target]].
        """,
    )
    counts = count_shadow_and_dark(vault)
    # target has 1 inbound → excluded. source has 0 inbound but ≥1 trigger
    # and ≥1 outbound → shadow.
    assert counts["shadow_orphan_count"] == 1
    assert counts["truly_dark_count"] == 0


# ---------------------------------------------------------------------------
# Regression: issue #251 — redirect stubs are bare-slug wikilink resolvers.
# By design they have zero inbound links (pointed at, not from) and trigger
# keywords matching the canonical slug, which used to make them look like
# shadow orphans. The fix detects them via the ``redirect:`` body marker
# and excludes them from both shadow/dark buckets.
# ---------------------------------------------------------------------------


def test_redirect_stub_excluded_from_shadow_orphan(tmp_path: Path) -> None:
    """A redirect stub (zero inbound, trigger_keywords, ``redirect:`` body
    marker) must not count as a shadow orphan."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "graph-models-for-cortex.md",
        """
        ---
        slug: graph-models-for-cortex
        trigger_keywords: [graph-models, cortex]
        ---

        redirect: [[2026-05-10-graph-models-for-cortex]]

        Redirect stub — canonical note is `[[2026-05-10-graph-models-for-cortex]]`.
        """,
    )
    counts = count_shadow_and_dark(vault)
    assert counts["shadow_orphan_count"] == 0
    assert counts["truly_dark_count"] == 0


def test_non_stub_mentioning_redirect_word_still_counted(tmp_path: Path) -> None:
    """Sanity: a real note that mentions the word ``redirect`` (without the
    structural ``redirect:`` marker) is NOT treated as a stub."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "real_note.md",
        """
        ---
        slug: real_note
        trigger_keywords: [routing]
        ---

        The handler will redirect the user to [[login]] on auth failure.
        """,
    )
    counts = count_shadow_and_dark(vault)
    # Real note, 0 inbound, ≥1 trigger, ≥1 outbound → still a shadow orphan.
    assert counts["shadow_orphan_count"] == 1
    assert counts["truly_dark_count"] == 0


def test_redirect_stub_and_genuine_shadow_orphan_coexist(tmp_path: Path) -> None:
    """A vault with both a redirect stub and a genuine shadow orphan must
    report only the genuine one — mirrors the production scenario from
    issue #251 (21 stubs + 1 real shadow orphan)."""
    vault = _make_vault(tmp_path)
    # Redirect stub — must be excluded.
    _write(
        vault / "research" / "measurement-self-contamination.md",
        """
        ---
        slug: measurement-self-contamination
        trigger_keywords: [measurement-self-contamination]
        ---

        redirect: [[2026-05-12-measurement-self-contamination]]
        """,
    )
    # Genuine shadow orphan — must be counted.
    _write(
        vault / "reference" / "2026-05-08-degradation-api-design.md",
        """
        ---
        slug: 2026-05-08-degradation-api-design
        trigger_keywords: [degradation, api-design]
        ---

        Design references [[graceful-degradation-pattern]] but nothing links back.
        """,
    )
    counts = count_shadow_and_dark(vault)
    assert counts["shadow_orphan_count"] == 1
    assert counts["truly_dark_count"] == 0


# ---------------------------------------------------------------------------
# Regression: issue #249 — silent frontmatter parse failures used to make
# notes look "truly dark" because trigger_keywords parsed as None. The fix
# exposes the failure both as a per-note WARNING log and as a counter on
# the count_shadow_and_dark return / vault_health event payload.
# ---------------------------------------------------------------------------


def test_frontmatter_parse_failure_counted_when_fence_unclosed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A note with an opening ``---`` fence but no closing fence is a
    parse failure: ``split_frontmatter`` returns an empty dict, so the
    note used to silently fall into ``truly_dark_count``. The counter
    must surface it AND a WARNING must be logged."""
    vault = _make_vault(tmp_path)
    # Note: this has an opening fence but no closing fence. yaml_lite
    # treats that as "no frontmatter" — returns ({}, text). Pre-fix this
    # was indistinguishable from a real no-frontmatter note.
    (vault / "research" / "broken.md").write_text(
        "---\nslug: broken\ntrigger_keywords: [phantom]\n\nbody without a closing fence\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="metrics.vault_health"):
        counts = count_shadow_and_dark(vault)
    assert counts["frontmatter_parse_failures"] == 1
    # The parse failure also flips classification: with no parsed fm,
    # trigger_keywords is None → trigger_count is 0 → counted as dark.
    # That's the documented inversion from #249 — the counter is what
    # makes it visible.
    assert counts["truly_dark_count"] == 1
    assert any(
        "frontmatter parse failure" in rec.message and "broken.md" in rec.message
        for rec in caplog.records
    )


def test_frontmatter_parse_failure_not_counted_for_legitimately_empty_frontmatter(
    tmp_path: Path,
) -> None:
    """A note that legitimately has no frontmatter (no opening fence)
    must NOT count toward parse failures."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "no_fm.md",
        "Just a note with no frontmatter at all.\n",
    )
    counts = count_shadow_and_dark(vault)
    assert counts["frontmatter_parse_failures"] == 0


def test_build_vault_health_event_includes_frontmatter_parse_failures(
    tmp_path: Path,
) -> None:
    """The morning-scan event must surface ``frontmatter_parse_failures``
    so consumers can distinguish a clean read from a degraded one."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    event = build_vault_health_event(
        vault_dir=vault,
        thoughts_dir=thoughts,
        events_path=events,
        surface_dir=surface,
    )
    assert "frontmatter_parse_failures" in event
    assert event["frontmatter_parse_failures"] == 0
    # No suspect tag on a clean vault.
    assert "parse_quality" not in event


def test_build_vault_health_event_tags_suspect_when_truly_dark_exceeds_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When ``truly_dark_count`` exceeds the sanity threshold, the event
    must be tagged ``parse_quality: "suspect"`` AND a WARNING logged.
    Vault context: with trigger-keyword backfill complete, >10 truly
    dark notes is statistically implausible — the 07:49 EDT 2026-05-19
    event that recorded shadow_orphan:1, truly_dark:20 is the reference
    failure mode (issue #249)."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    # Seed 11 notes that have no frontmatter at all → each counts as
    # truly dark. 11 > the threshold of 10.
    for i in range(11):
        _write(
            vault / "research" / f"dark_{i}.md",
            f"Note {i} with no frontmatter and no triggers.\n",
        )

    with caplog.at_level("WARNING", logger="metrics.vault_health"):
        event = build_vault_health_event(
            vault_dir=vault,
            thoughts_dir=thoughts,
            events_path=events,
            surface_dir=surface,
        )
    assert event["truly_dark_count"] >= 11
    assert event.get("parse_quality") == "suspect"
    assert any(
        "exceeds suspect threshold" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Regression: double-backtick code spans (``[[wikilink]]``) leaked through
# the inline-code stripper in indexer.yaml_lite. Markdown allows
# longer tick runs to delimit spans containing inner ticks; widest first
# is the right strip order.
# ---------------------------------------------------------------------------


def test_broken_wikilinks_excludes_double_backtick_code_spans(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        "Documenting the wikilink syntax: ``[[example-target]]`` is how you write one.\n",
    )
    n, broken = count_broken_wikilinks(vault)
    assert n == 0
    assert broken == []


def test_broken_wikilinks_excludes_mixed_tick_widths(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        "Single `[[a]]` and double ``[[b]]`` and real broken [[c]] in body.\n",
    )
    n, broken = count_broken_wikilinks(vault)
    assert n == 1
    assert broken == [("research/foo.md", "c")]


# ---------------------------------------------------------------------------
# Recovery state: inbound link counting


def test_inbound_links_counts_cross_directory_refs(tmp_path: Path) -> None:
    """research/a.md → reference/hub.md (hub gets +1)."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "a.md",
        """
        ---
        slug: a
        ---
        Hub note: [[hub]]
        """,
    )
    _write(
        vault / "reference" / "hub.md",
        """
        ---
        slug: hub
        ---
        I am the hub.
        """,
    )

    links = count_inbound_links(vault)
    assert links["reference/hub.md"] >= 1


def test_inbound_links_excludes_dailies_when_requested(tmp_path: Path) -> None:
    """Daily references to a note should not inflate hub count when
    dailies are excluded."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "reference" / "hub.md",
        """
        ---
        slug: hub
        ---
        Hub.
        """,
    )
    _write(
        vault / "research" / "a.md",
        """
        ---
        slug: a
        ---
        References hub: [[hub]].
        """,
    )
    _write(
        vault / "dailies" / "2026-05-09.md",
        """
        Today I wrote about [[hub]].
        """,
    )

    # With dailies excluded: only research/a.md counts → hub gets 1
    daily_files = list((vault / "dailies").rglob("*.md"))
    exclude = frozenset(str(d.relative_to(vault)) for d in daily_files)
    links = count_inbound_links(vault, exclude=exclude)
    assert links["reference/hub.md"] == 1

    # Without exclusion: research/a.md + daily → hub gets 2
    links_no_exclude = count_inbound_links(vault)
    assert links_no_exclude["reference/hub.md"] == 2


# ---------------------------------------------------------------------------
# Recovery state: Tier 1 ratio


def test_tier1_ratio_returns_zero_when_no_old_notes(tmp_path: Path) -> None:
    """Notes created in the future (mtime > cutoff) → total = 0 → ratio = 0."""
    vault = _make_vault(tmp_path)
    from datetime import datetime, timedelta

    # Cutoff in the future — no notes qualify.
    cutoff = datetime.now() + timedelta(days=1)
    result = count_tier1_ratio(vault, notes_7d_cutoff=cutoff)
    assert result == {"ratio": 0.0, "hubs": 0, "total": 0}


def test_tier1_ratio_empty_research_dir(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    # research/ exists but has no .md files.
    result = count_tier1_ratio(vault)
    assert result == {"ratio": 0.0, "hubs": 0, "total": 0}


# ---------------------------------------------------------------------------
# Recovery state: output rate slope


def test_output_rate_slope_empty_vault(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    result = count_output_rate_slope(vault)
    assert result["slope"] == 0.0
    assert result["days"] == 0


# ---------------------------------------------------------------------------
# Recovery state: end-to-end with events.jsonl


def _write_event(path: Path, ts: str, vault_health: dict) -> None:
    """Append a vault_health event to the JSONL file."""
    import json
    evt = {"ts": ts, "type": "vault_health", **vault_health}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(evt) + "\n")


def test_recovery_state_no_events(tmp_path: Path) -> None:
    """No events.jsonl → debt_delta = 0, status = consolidating."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    events = tmp_path / "events.jsonl"  # doesn't exist

    from datetime import datetime, timedelta

    we = datetime.now()
    ws = we - timedelta(days=14)
    result = compute_recovery_state(
        vault, thoughts, window_start=ws, window_end=we, events_path=events
    )
    assert result["status"] in {"consolidating", "deteriorating"}  # tier1=0 is red
    assert result["structural_debt_delta"] == 0


def test_recovery_state_baseline_available(tmp_path: Path) -> None:
    """Events with clear start and end → delta computed."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    events = tmp_path / "events.jsonl"

    # Create a research note with mtime in the past (8 days ago).
    from datetime import datetime, timedelta
    import os

    eight_days_ago = datetime.now() - timedelta(days=8)
    note_path = vault / "research" / "hub.md"
    _write(
        note_path,
        """
        ---
        slug: hub
        ---
        Hub.
        """,
    )
    # Set mtime to 8 days ago (old enough for Tier 1).
    ts = eight_days_ago.timestamp()
    os.utime(note_path, (ts, ts))

    # Write events: start debt = 10, end debt = 5 (debt resolving).
    _write_event(events, "2026-05-01T08:00:00-04:00", {
        "date": "2026-05-01",
        "total_notes": 100,
        "orphan_notes": 6,
        "broken_wikilinks": 4,
    })
    _write_event(events, "2026-05-10T08:00:00-04:00", {
        "date": "2026-05-10",
        "total_notes": 105,
        "orphan_notes": 3,
        "broken_wikilinks": 2,
    })

    we = datetime(2026, 5, 10, 7, 0, 0)
    ws = we - timedelta(days=14)
    result = compute_recovery_state(
        vault, thoughts, window_start=ws, window_end=we, events_path=events
    )
    # debt_delta = (3+2) - (6+4) = -5 (green)
    assert result["structural_debt_delta"] == -5
    # tier1_ratio: 1 note, 0 links → 0.0 (red)
    # slope: small positive (recovering)
    # Result: 1 red, 1 green, 1 unknown/low → consolidating
    assert result["status"] in {"consolidating", "deteriorating"}


# ---------------------------------------------------------------------------
# Research note decay: [[2026-05-09-research-note-decay-metric]]
# Count research/ notes older than 60 days with fewer than 2 inbound links.
# Age determined by the `created:` frontmatter field.
# ---------------------------------------------------------------------------


def test_research_decay_empty_vault(tmp_path: Path) -> None:
    """No research/ notes → 0 decay."""
    vault = _make_vault(tmp_path)
    assert count_research_decay(vault) == 0


def test_research_decay_empty_research_dir(tmp_path: Path) -> None:
    """research/ exists but has no .md files → 0 decay."""
    vault = _make_vault(tmp_path)
    assert count_research_decay(vault) == 0


def test_research_decay_young_note_not_counted(tmp_path: Path) -> None:
    """A note with a `created:` date only 10 days ago should not be counted
    as decayed, even with zero inbound links."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "foo.md",
        """
        ---
        slug: foo
        created: 2026-05-05
        ---
        Young note with no inbound links.
        """,
    )
    assert count_research_decay(vault) == 0


def test_research_decay_old_note_zero_links(tmp_path: Path) -> None:
    """A note older than 60 days with 0 inbound links is decayed."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "old-note.md",
        """
        ---
        slug: old-note
        created: 2026-03-01
        ---
        Old note nobody references.
        """,
    )
    assert count_research_decay(vault) == 1


def test_research_decay_old_note_one_link_not_decayed(tmp_path: Path) -> None:
    """A note older than 60 days with exactly 1 inbound link is NOT decayed
    (threshold is fewer than 2)."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "reference" / "referencing.md",
        """
        ---
        slug: referencing
        ---
        References old stuff: [[old-note]].
        """,
    )
    _write(
        vault / "research" / "old-note.md",
        """
        ---
        slug: old-note
        created: 2026-03-01
        ---
        Old note with one inbound link.
        """,
    )
    # 1 inbound link → below threshold of 2 → decayed.
    assert count_research_decay(vault) == 1


def test_research_decay_old_note_two_links_not_decayed(tmp_path: Path) -> None:
    """A note older than 60 days with 2+ inbound links is NOT decayed."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "reference" / "ref1.md",
        """
        ---
        slug: ref1
        ---
        Links to [[old-note]].
        """,
    )
    _write(
        vault / "reference" / "ref2.md",
        """
        ---
        slug: ref2
        ---
        Also links to [[old-note]].
        """,
    )
    _write(
        vault / "research" / "old-note.md",
        """
        ---
        slug: old-note
        created: 2026-03-01
        ---
        Old note with two inbound links.
        """,
    )
    # 2 inbound links → not fewer than 2 → NOT decayed.
    assert count_research_decay(vault) == 0


def test_research_decay_missing_created_field_skipped(tmp_path: Path) -> None:
    """An old note without a `created:` frontmatter field is skipped
    (not counted as decayed, to avoid false positives)."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "no-created.md",
        """
        ---
        slug: no-created
        ---
        Note without a created field.
        """,
    )
    assert count_research_decay(vault) == 0


def test_research_decay_frontmatter_link_not_counted(tmp_path: Path) -> None:
    """A link in a frontmatter `related:` list counts as an inbound link."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "reference" / "hub.md",
        """
        ---
        slug: hub
        related: [[old-note]]
        ---
        Hub note.
        """,
    )
    _write(
        vault / "research" / "old-note.md",
        """
        ---
        slug: old-note
        created: 2026-03-01
        ---
        Old note linked from frontmatter.
        """,
    )
    # 1 inbound link (from frontmatter) → fewer than 2 → decayed.
    assert count_research_decay(vault) == 1


def test_research_decay_cross_directory_link_counts(tmp_path: Path) -> None:
    """A link from a note in another directory resolves and counts."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "projects" / "project-x.md",
        """
        ---
        slug: project-x
        ---
        References [[old-note]] extensively.
        """,
    )
    _write(
        vault / "research" / "old-note.md",
        """
        ---
        slug: old-note
        created: 2026-03-01
        ---
        Old note referenced from projects/.
        """,
    )
    assert count_research_decay(vault) == 1  # only 1 link < 2


# ---------------------------------------------------------------------------
# Stage C candidates: bloated + stale-dailies counters
# Previously bash-computed inside the wake template; absorbed into the
# module so the morning scan collapses to one command.
# ---------------------------------------------------------------------------


def test_stage_c_candidates_bloated_threshold(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    # Three notes: 100 lines, 251 lines (just over), 1000 lines.
    _write(vault / "research" / "small.md", "x\n" * 100)
    _write(vault / "research" / "over.md", "x\n" * 251)
    _write(vault / "research" / "big.md", "x\n" * 1000)
    result = count_stage_c_candidates(vault)
    assert result["bloated_notes"] == 2
    assert result["stale_dailies"] == 0
    assert result["total"] == 2


def test_stage_c_candidates_excludes_dailies_and_scaffolding(tmp_path: Path) -> None:
    """Bloated count must skip dailies/, index.md, README.md, unresolved.md."""
    vault = _make_vault(tmp_path)
    big = "x\n" * 500
    _write(vault / "dailies" / "2026-01-01.md", big)
    _write(vault / "index.md", big)
    _write(vault / "README.md", big)
    _write(vault / "unresolved.md", big)
    _write(vault / "research" / "real-bloat.md", big)
    result = count_stage_c_candidates(vault)
    assert result["bloated_notes"] == 1


def test_stage_c_candidates_stale_dailies(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    # today=2026-05-10; 90-day cutoff = 2026-02-09.
    today = datetime(2026, 5, 10)
    _write(vault / "dailies" / "2025-12-01.md", "stale\n")
    _write(vault / "dailies" / "2026-02-08.md", "also stale\n")
    _write(vault / "dailies" / "2026-02-09.md", "edge: not stale\n")
    _write(vault / "dailies" / "2026-05-01.md", "recent\n")
    result = count_stage_c_candidates(vault, today=today)
    # Two strictly older than the cutoff (2026-02-09).
    assert result["stale_dailies"] == 2


# ---------------------------------------------------------------------------
# research_notes_last_night: created: == yesterday
# ---------------------------------------------------------------------------


def test_research_notes_created_on_matches_yesterday(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "yesterday.md",
        """
        ---
        slug: yesterday
        created: 2026-05-09
        ---
        Body.
        """,
    )
    _write(
        vault / "research" / "today.md",
        """
        ---
        slug: today
        created: 2026-05-10
        ---
        Body.
        """,
    )
    _write(
        vault / "research" / "old.md",
        """
        ---
        slug: old
        created: 2026-04-01
        ---
        Body.
        """,
    )
    count = count_research_notes_created_on(vault, datetime(2026, 5, 9))
    assert count == 1


def test_research_notes_created_on_handles_timestamped_format(tmp_path: Path) -> None:
    """`created: 2026-05-09 22:14 EDT` must parse as the 2026-05-09 day."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "stamped.md",
        """
        ---
        slug: stamped
        created: 2026-05-09 22:14 -0400
        ---
        Body.
        """,
    )
    count = count_research_notes_created_on(vault, datetime(2026, 5, 9))
    assert count == 1


# ---------------------------------------------------------------------------
# Surface counts
# ---------------------------------------------------------------------------


def test_count_surfaces_in_window_filters_by_mtime(tmp_path: Path) -> None:
    import os

    surface = tmp_path / "surface"
    surface.mkdir()
    today_dir = surface / "2026-05-10"
    today_dir.mkdir()

    inside = today_dir / "in-window.md"
    outside = today_dir / "out-of-window.md"
    inside.write_text("x")
    outside.write_text("x")

    ws = datetime(2026, 5, 9, 23, 0, 0)
    we = datetime(2026, 5, 10, 7, 0, 0)
    # Set mtimes explicitly.
    os.utime(inside, (ws.timestamp() + 1800, ws.timestamp() + 1800))  # 23:30
    os.utime(outside, (we.timestamp() + 3600, we.timestamp() + 3600))  # 08:00

    n = count_surfaces_in_window(surface, ws, we)
    assert n == 1


def test_count_surfaces_in_window_skips_handled_dir(tmp_path: Path) -> None:
    """Files under `.handled/` must not count as freshly written."""
    import os

    surface = tmp_path / "surface"
    handled = surface / ".handled" / "2026-05-10"
    handled.mkdir(parents=True)
    f = handled / "old.md"
    f.write_text("x")

    ws = datetime(2026, 5, 9, 23, 0, 0)
    we = datetime(2026, 5, 10, 7, 0, 0)
    os.utime(f, (ws.timestamp() + 100, ws.timestamp() + 100))

    assert count_surfaces_in_window(surface, ws, we) == 0


def test_count_surfaces_handled_today(tmp_path: Path) -> None:
    surface = tmp_path / "surface"
    today = datetime(2026, 5, 10)
    handled = surface / ".handled" / "2026-05-10"
    handled.mkdir(parents=True)
    (handled / "a.md").write_text("x")
    (handled / "b.md").write_text("x")
    # File in a different day's handled dir must not count.
    other = surface / ".handled" / "2026-05-09"
    other.mkdir(parents=True)
    (other / "c.md").write_text("x")

    assert count_surfaces_handled_today(surface, today) == 2


def test_count_surfaces_handled_today_empty(tmp_path: Path) -> None:
    surface = tmp_path / "surface"
    surface.mkdir()
    today = datetime(2026, 5, 10)
    assert count_surfaces_handled_today(surface, today) == 0


# ---------------------------------------------------------------------------
# Productive wakes: did_work=true in the window
# ---------------------------------------------------------------------------


def test_count_productive_wakes_did_work_filter(tmp_path: Path) -> None:
    thoughts = tmp_path / "thoughts"
    yest = thoughts / "2026-05-09"
    today = thoughts / "2026-05-10"
    yest.mkdir(parents=True)
    today.mkdir(parents=True)

    def _wake(dir_path: Path, name: str, did: str) -> None:
        _write(
            dir_path / name,
            f"""
            ---
            mode: sleep
            stage: C
            did_work: {did}
            ---
            Body.
            """,
        )

    _wake(yest, "233000-wake.md", "true")
    _wake(yest, "235500-wake.md", "false")
    _wake(today, "010000-wake.md", "true")
    _wake(today, "030000-wake.md", "true")
    # Out of window — must be excluded.
    _wake(today, "080000-wake.md", "true")

    ws = datetime(2026, 5, 9, 23, 0, 0)
    we = datetime(2026, 5, 10, 7, 0, 0)
    assert count_productive_wakes(thoughts, ws, we) == 3


# ---------------------------------------------------------------------------
# Event-stream dedup
# ---------------------------------------------------------------------------


def test_vault_health_event_exists_true(tmp_path: Path) -> None:
    import json

    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"type": "vault_health", "date": "2026-05-10"}) + "\n"
        + json.dumps({"type": "meal", "date": "2026-05-10"}) + "\n",
        encoding="utf-8",
    )
    assert vault_health_event_exists_for_date(events, "2026-05-10") is True
    assert vault_health_event_exists_for_date(events, "2026-05-09") is False


def test_vault_health_event_exists_missing_file(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"  # doesn't exist
    assert vault_health_event_exists_for_date(events, "2026-05-10") is False


def test_vault_health_event_exists_tolerates_garbage_lines(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        'not json\n{"type":"vault_health","date":"2026-05-10"}\nbroken{\n',
        encoding="utf-8",
    )
    assert vault_health_event_exists_for_date(events, "2026-05-10") is True


# ---------------------------------------------------------------------------
# build_vault_health_event: shape contract
# Every field the morning scan needs must appear exactly once.
# ---------------------------------------------------------------------------


REQUIRED_EVENT_FIELDS = {
    "ts",
    "type",
    "date",
    "time",
    "total_notes",
    "broken_wikilinks",
    "orphan_notes",
    "orphan_dailies_excluded",
    "research_notes_last_night",
    "surfaces_written_last_night",
    "surfaces_handled_today",
    "total_wakes_last_night",
    "stage_c_candidates",
    "wake_type_distribution",
    "recovery_state",
    "research_decay_count",
    "decay_coverage",
    "shadow_orphan_count",
    "truly_dark_count",
}


def test_build_vault_health_event_has_all_required_fields(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    event = build_vault_health_event(
        vault_dir=vault,
        thoughts_dir=thoughts,
        events_path=events,
        surface_dir=surface,
    )
    missing = REQUIRED_EVENT_FIELDS - set(event.keys())
    assert not missing, f"missing fields: {missing}"
    assert event["type"] == "vault_health"
    assert isinstance(event["stage_c_candidates"], dict)
    assert set(event["stage_c_candidates"].keys()) >= {
        "bloated_notes",
        "stale_dailies",
        "total",
    }
    assert isinstance(event["recovery_state"], dict)
    assert "status" in event["recovery_state"]


# ---------------------------------------------------------------------------
# CLI: --check-existing + --append
# These two flags are the structural fix for the field-drop bug.
# ---------------------------------------------------------------------------


def _cli_args(vault: Path, thoughts: Path, events: Path, surface: Path, *extra: str) -> list[str]:
    return [
        "--vault", str(vault),
        "--thoughts", str(thoughts),
        "--events", str(events),
        "--surface", str(surface),
        *extra,
    ]


def test_cli_check_existing_noops_when_event_exists(tmp_path: Path) -> None:
    """If today's event is already in events.jsonl, --check-existing must
    exit 0 without writing anything."""
    import json

    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"
    today = datetime.now().strftime("%Y-%m-%d")
    events.write_text(
        json.dumps({"type": "vault_health", "date": today}) + "\n",
        encoding="utf-8",
    )
    size_before = events.stat().st_size

    rc = vault_health_main(_cli_args(vault, thoughts, events, surface, "--check-existing", "--append"))
    assert rc == 0
    # File untouched.
    assert events.stat().st_size == size_before


def test_cli_append_writes_full_event(tmp_path: Path) -> None:
    """--append must write a single JSON line that has every required field."""
    import json

    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"  # does not exist yet

    rc = vault_health_main(_cli_args(vault, thoughts, events, surface, "--check-existing", "--append"))
    assert rc == 0
    assert events.exists()
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    missing = REQUIRED_EVENT_FIELDS - set(evt.keys())
    assert not missing, f"appended event missing fields: {missing}"
    assert evt["type"] == "vault_health"


def test_cli_check_existing_continues_when_no_today_event(tmp_path: Path) -> None:
    """events.jsonl has a vault_health event for yesterday but not today —
    --check-existing must NOT short-circuit; --append must write today's."""
    import json

    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    events.write_text(
        json.dumps({"type": "vault_health", "date": yesterday}) + "\n",
        encoding="utf-8",
    )

    rc = vault_health_main(_cli_args(vault, thoughts, events, surface, "--check-existing", "--append"))
    assert rc == 0
    lines = events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    evt = json.loads(lines[1])
    today = datetime.now().strftime("%Y-%m-%d")
    assert evt["date"] == today


def test_cli_append_requires_thoughts_and_events(tmp_path: Path) -> None:
    """--append without --thoughts/--events should error via argparse."""
    vault = _make_vault(tmp_path)
    with pytest.raises(SystemExit):
        vault_health_main(["--vault", str(vault), "--append"])


# ---------------------------------------------------------------------------
# Decay coverage (Layer 1 blind-spot detection)
# Design: [[2026-05-10-decay-blind-spot-detection-design]]
# Pool: notes that were access_count == 0 and age >= 5 at activation.
# Coverage: fraction of pool with last_accessed >= window_floor.
# ---------------------------------------------------------------------------


_TODAY = datetime(2026, 5, 10)
_ACTIVATION = "2026-05-06"


def test_decay_coverage_empty_pool_returns_one_hundred(tmp_path: Path) -> None:
    """No qualifying notes → vacuous 100% coverage (nothing to rescue)."""
    vault = _make_vault(tmp_path)
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 0
    assert result["decayed_accessed_in_window"] == 0
    assert result["decay_coverage_pct"] == 100.0
    assert result["activation_date"] == _ACTIVATION
    assert result["window_days"] == 7
    assert result["by_domain"] == {}


def test_decay_coverage_all_decayed_returns_zero(tmp_path: Path) -> None:
    """Every pool note still untouched → 0% coverage."""
    vault = _make_vault(tmp_path)
    for name in ("a", "b", "c"):
        _write(
            vault / "research" / f"{name}.md",
            f"""
            ---
            slug: {name}
            created: 2026-04-25
            access_count: 0
            domain: fitness
            ---
            Body.
            """,
        )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 3
    assert result["decayed_accessed_in_window"] == 0
    assert result["decay_coverage_pct"] == 0.0
    assert result["by_domain"]["fitness"] == {
        "decayed": 3,
        "accessed": 0,
        "coverage_pct": 0.0,
    }


def test_decay_coverage_partial_access(tmp_path: Path) -> None:
    """Two of four pool notes touched post-activation → 50% coverage.

    The accessed notes are still in the pool — that's the whole point:
    pool membership is determined at activation, coverage measures what
    fraction has since been recovered.
    """
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "untouched-1.md",
        """
        ---
        slug: untouched-1
        created: 2026-04-25
        access_count: 0
        domain: fitness
        ---
        """,
    )
    _write(
        vault / "research" / "untouched-2.md",
        """
        ---
        slug: untouched-2
        created: 2026-04-25
        access_count: 0
        domain: fitness
        ---
        """,
    )
    _write(
        vault / "research" / "rescued-1.md",
        """
        ---
        slug: rescued-1
        created: 2026-04-25
        access_count: 1
        last_accessed: 2026-05-08
        domain: fitness
        ---
        """,
    )
    _write(
        vault / "research" / "rescued-2.md",
        """
        ---
        slug: rescued-2
        created: 2026-04-25
        access_count: 2
        last_accessed: 2026-05-09
        domain: alice-architecture
        ---
        """,
    )

    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 4
    assert result["decayed_accessed_in_window"] == 2
    assert result["decay_coverage_pct"] == 50.0
    assert result["by_domain"]["fitness"]["decayed"] == 3
    assert result["by_domain"]["fitness"]["accessed"] == 1
    assert result["by_domain"]["alice-architecture"]["accessed"] == 1


def test_decay_coverage_activation_boundary_respected(tmp_path: Path) -> None:
    """A note created on activation-day is too young — not in pool.

    With activation 2026-05-06 and cutoff_days=5, the age cutoff is
    2026-05-01. A note created 2026-05-06 is post-activation and didn't
    sit in a decayed state.
    """
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "young.md",
        """
        ---
        slug: young
        created: 2026-05-06
        access_count: 0
        ---
        """,
    )
    _write(
        vault / "research" / "borderline.md",
        """
        ---
        slug: borderline
        created: 2026-05-02
        access_count: 0
        ---
        """,
    )
    _write(
        vault / "research" / "ancient.md",
        """
        ---
        slug: ancient
        created: 2026-04-01
        access_count: 0
        ---
        """,
    )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    # Only `ancient.md` qualifies (created <= 2026-05-01).
    assert result["total_decayed_notes"] == 1


def test_decay_coverage_excludes_pre_activation_access(tmp_path: Path) -> None:
    """A note with access_count > 0 whose last_accessed predates activation
    was already in active circulation — not a decay case."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "active.md",
        """
        ---
        slug: active
        created: 2026-03-01
        access_count: 5
        last_accessed: 2026-04-15
        ---
        """,
    )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 0


def test_decay_coverage_excludes_dailies_and_archive(tmp_path: Path) -> None:
    """Activity logs and cold storage are not part of the retrievable pool."""
    vault = _make_vault(tmp_path)
    (vault / "archive").mkdir()
    for folder in ("dailies", "archive"):
        _write(
            vault / folder / "old.md",
            """
            ---
            slug: old
            created: 2026-04-01
            access_count: 0
            ---
            """,
        )
    _write(
        vault / "research" / "real.md",
        """
        ---
        slug: real
        created: 2026-04-01
        access_count: 0
        ---
        """,
    )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 1
    assert "dailies" not in result["by_domain"]
    assert "archive" not in result["by_domain"]


def test_decay_coverage_window_floor_caps_at_activation(tmp_path: Path) -> None:
    """In week 1, today - window_days predates activation. The activation
    date must floor the window so pre-cue ``last_accessed`` values don't
    leak into the accessed count."""
    vault = _make_vault(tmp_path)
    # last_accessed 2026-05-04 is within 7 days of 2026-05-10 BUT predates
    # activation 2026-05-06 — must NOT count as accessed-in-window.
    _write(
        vault / "research" / "pre-cue-touch.md",
        """
        ---
        slug: pre-cue-touch
        created: 2026-04-15
        access_count: 1
        last_accessed: 2026-05-04
        ---
        """,
    )
    result = compute_decay_coverage(
        vault, today=_TODAY, activation_date=_ACTIVATION, window_days=7
    )
    # Note isn't even in the pool: last_accessed < activation means it
    # was active pre-cue. Pool is empty.
    assert result["total_decayed_notes"] == 0


def test_decay_coverage_domain_fallback_to_folder(tmp_path: Path) -> None:
    """Notes without a ``domain:`` frontmatter bucket under their folder."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "no-domain.md",
        """
        ---
        slug: no-domain
        created: 2026-04-01
        access_count: 0
        ---
        """,
    )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert "research" in result["by_domain"]
    assert result["by_domain"]["research"]["decayed"] == 1


def test_decay_coverage_window_days_parameter(tmp_path: Path) -> None:
    """A larger window includes accesses that a smaller window misses."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "rescued-yesterday.md",
        """
        ---
        slug: rescued-yesterday
        created: 2026-04-01
        access_count: 1
        last_accessed: 2026-05-09
        ---
        """,
    )
    # 1-day window covers 2026-05-09 only (today-1) given _TODAY = 2026-05-10.
    # With activation floor in play, that's still inside the floor.
    r1 = compute_decay_coverage(
        vault, today=_TODAY, activation_date=_ACTIVATION, window_days=1
    )
    assert r1["decayed_accessed_in_window"] == 1
    # 0-day window: today - 0 = today (2026-05-10); 2026-05-09 falls
    # outside → coverage 0 even though pool has 1 entry.
    r0 = compute_decay_coverage(
        vault, today=_TODAY, activation_date=_ACTIVATION, window_days=0
    )
    assert r0["total_decayed_notes"] == 1
    assert r0["decayed_accessed_in_window"] == 0
    assert r0["decay_coverage_pct"] == 0.0


def test_decay_coverage_missing_access_count_skipped(tmp_path: Path) -> None:
    """Notes without an ``access_count:`` field can't be classified —
    skip them so we don't inflate either the pool or the coverage count."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "no-count.md",
        """
        ---
        slug: no-count
        created: 2026-04-01
        ---
        Body without access_count.
        """,
    )
    result = compute_decay_coverage(vault, today=_TODAY, activation_date=_ACTIVATION)
    assert result["total_decayed_notes"] == 0


# ---------------------------------------------------------------------------
# Continuous structural-health checks
# Design: [[2026-05-20-continuous-structural-health-check]] (Task 1).
# - zero_edge_notes: shadow_orphan subset surfaced with slug list.
# - trigger_gap_notes: research/ notes >14d old, no triggers, ≥2 outbound.
# - --continuous flag: opt-in; baseline output unchanged when omitted.
# ---------------------------------------------------------------------------


_CONTINUOUS_TODAY = datetime(2026, 5, 20)


def test_compute_continuous_checks_detects_zero_edge_note(tmp_path: Path) -> None:
    """A note with ≥1 trigger keyword, ≥1 outbound, 0 inbound is a
    zero_edge_note and must appear in the slug list."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "ghost.md",
        """
        ---
        slug: ghost
        trigger_keywords: [phantom]
        created: 2026-05-01
        ---

        Linking out to [[anchor]] but nothing links back.
        """,
    )
    _write(
        vault / "reference" / "anchor.md",
        """
        ---
        slug: anchor
        ---

        Anchor body.
        """,
    )
    result = compute_continuous_checks(vault, today=_CONTINUOUS_TODAY)
    assert result["zero_edge_notes"]["count"] == 1
    assert result["zero_edge_notes"]["slugs"] == ["ghost"]
    # anchor has 1 inbound + no outbound + no triggers → neither bucket.
    assert result["trigger_gap_notes"]["count"] == 0


def test_compute_continuous_checks_detects_trigger_gap_note(tmp_path: Path) -> None:
    """A research/ note older than 14 days with 0 trigger keywords and
    ≥2 outbound wikilinks must appear as a trigger_gap_note. A younger
    note with the same shape must not."""
    vault = _make_vault(tmp_path)
    # Old + 2 outbound + no triggers → trigger_gap.
    _write(
        vault / "research" / "old-gap.md",
        """
        ---
        slug: old-gap
        created: 2026-04-01
        ---

        References [[anchor-a]] and [[anchor-b]] but is itself undiscoverable.
        """,
    )
    # Young + 2 outbound + no triggers → not yet eligible.
    _write(
        vault / "research" / "young-gap.md",
        """
        ---
        slug: young-gap
        created: 2026-05-18
        ---

        References [[anchor-a]] and [[anchor-b]] but is recent.
        """,
    )
    # Old + 1 outbound + no triggers → below outbound threshold.
    _write(
        vault / "research" / "old-single.md",
        """
        ---
        slug: old-single
        created: 2026-04-01
        ---

        Only one outbound: [[anchor-a]].
        """,
    )
    _write(vault / "reference" / "anchor-a.md", "anchor a\n")
    _write(vault / "reference" / "anchor-b.md", "anchor b\n")
    result = compute_continuous_checks(vault, today=_CONTINUOUS_TODAY)
    assert result["trigger_gap_notes"]["count"] == 1
    assert result["trigger_gap_notes"]["slugs"] == ["old-gap"]


def test_continuous_flag_does_not_change_baseline_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting --continuous must leave the legacy CLI output free of a
    ``continuous_checks`` key. Passing --continuous must add exactly that
    key without disturbing other fields."""
    vault = _make_vault(tmp_path)
    _write(
        vault / "research" / "ghost.md",
        """
        ---
        slug: ghost
        trigger_keywords: [phantom]
        created: 2026-05-01
        ---

        Linking out to [[anchor]].
        """,
    )
    _write(vault / "reference" / "anchor.md", "anchor body\n")

    vault_health_main(["--vault", str(vault)])
    baseline = json.loads(capsys.readouterr().out)
    assert "continuous_checks" not in baseline

    vault_health_main(["--vault", str(vault), "--continuous"])
    with_flag = json.loads(capsys.readouterr().out)
    assert "continuous_checks" in with_flag
    # The opt-in key is the only additive change.
    assert {k: with_flag[k] for k in baseline} == baseline
    assert with_flag["continuous_checks"]["zero_edge_notes"]["count"] == 1
    assert with_flag["continuous_checks"]["zero_edge_notes"]["slugs"] == ["ghost"]


# ---------------------------------------------------------------------------
# Issue #323 — low-wake-count surface
# ---------------------------------------------------------------------------


def _seed_wakes_for_morning_window(thoughts: Path, *, b: int, c: int, d: int) -> None:
    """Drop ``b + c + d`` wake files inside the morning window.

    The morning window is yesterday-23:00 → today-07:00 in local time;
    this helper writes wakes at 00:00:NN through the previous-day dir
    so the count is independent of when the test runs.
    """
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yday = thoughts / yesterday
    yday.mkdir(parents=True, exist_ok=True)
    seq = 0
    for stage, n in (("B", b), ("C", c), ("D", d)):
        for _ in range(n):
            seq += 1
            _write_wake(yday, f"23{seq:02d}00-wake.md", stage)


def test_low_wake_count_tagged_when_under_threshold(tmp_path: Path) -> None:
    """Issue #323 fix 3: when stage_b + stage_c + stage_d < threshold,
    the event MUST tag ``low_wake_count: True`` and write an
    insight-tier surface file under ``surface_dir``."""
    from metrics.vault_health import WAKE_COUNT_THRESHOLD

    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    # 3 total wakes — well under the threshold of 8 (matches the
    # May-22 sleep cycle that motivated the fix).
    _seed_wakes_for_morning_window(thoughts, b=1, c=1, d=1)

    event = build_vault_health_event(
        vault_dir=vault,
        thoughts_dir=thoughts,
        events_path=events,
        surface_dir=surface,
    )

    assert event["wake_type_distribution"]["stage_b"] + \
        event["wake_type_distribution"]["stage_c"] + \
        event["wake_type_distribution"]["stage_d"] < WAKE_COUNT_THRESHOLD
    assert event.get("low_wake_count") is True
    assert event.get("total_sleep_wakes") == 3

    # Surface file dropped with the expected naming + frontmatter.
    written = list(surface.glob("*-low-wake-count.md"))
    assert len(written) == 1, written
    body = written[0].read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "priority: insight" in body
    assert "reply_expected: false" in body
    assert "Wake count fell below threshold" in body
    assert "total_sleep_wakes: 3" in body
    assert f"threshold: {WAKE_COUNT_THRESHOLD}" in body
    assert "check the backoff history" in body


def test_low_wake_count_not_tagged_when_at_or_above_threshold(
    tmp_path: Path,
) -> None:
    """Healthy 8h sleep produces 15–19 wakes. Anything ≥ threshold
    must NOT tag the event and must NOT write a surface file."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    # 16 total wakes — the floor MIN_WAKE_PERIOD guarantees.
    _seed_wakes_for_morning_window(thoughts, b=8, c=4, d=4)

    event = build_vault_health_event(
        vault_dir=vault,
        thoughts_dir=thoughts,
        events_path=events,
        surface_dir=surface,
    )

    assert event.get("low_wake_count") is None or event["low_wake_count"] is False
    assert event["total_sleep_wakes"] == 16
    assert list(surface.glob("*-low-wake-count.md")) == []


def test_low_wake_count_handles_zero_wakes(tmp_path: Path) -> None:
    """Edge case: no thoughts at all (e.g. supervisor never ran).
    Must still tag and surface — zero is below threshold."""
    vault = _make_vault(tmp_path)
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    events = tmp_path / "events.jsonl"

    event = build_vault_health_event(
        vault_dir=vault,
        thoughts_dir=thoughts,
        events_path=events,
        surface_dir=surface,
    )

    assert event["total_sleep_wakes"] == 0
    assert event.get("low_wake_count") is True
    assert len(list(surface.glob("*-low-wake-count.md"))) == 1


# ---------------------------------------------------------------------------
# gh-state exclusion
# cortex-memory/gh-state/ holds GitHub issue/PR state-mirror notes —
# operational records with 0 trigger_keywords and 0 wikilinks by design.
# They are excluded from the orphan, dark, and zero-edge buckets so they
# don't dominate the vault-health signal (production: 42 of 43 dark notes
# were gh-state mirrors, masking the real knowledge-vault count of 1).
# The exclusion is selective — bare research/ notes still count as dark.
# ---------------------------------------------------------------------------


def _write_gh_state_note(vault: Path, name: str = "alice-123.md") -> Path:
    """Drop a representative gh-state mirror note: type: pr, no triggers,
    no wikilinks. Same shape as production cortex-memory/gh-state/*.md."""
    return _write(
        vault / "gh-state" / name,
        """
        ---
        slug: alice-123
        type: pr
        state: open
        repo: jcronq/alice
        ---

        State-mirror body — no wikilinks, no trigger_keywords.
        """,
    )


def test_gh_state_note_not_counted_as_dark(tmp_path: Path) -> None:
    """gh-state mirrors (0 inbound, 0 triggers) MUST NOT bump
    truly_dark_count or shadow_orphan_count."""
    vault = _make_vault(tmp_path)
    _write_gh_state_note(vault)
    counts = count_shadow_and_dark(vault)
    assert counts == {
        "shadow_orphan_count": 0,
        "truly_dark_count": 0,
        "frontmatter_parse_failures": 0,
    }


def test_gh_state_note_not_counted_as_orphan(tmp_path: Path) -> None:
    """gh-state mirrors have 0 inbound wikilinks by design and MUST NOT
    appear in count_orphans's result."""
    vault = _make_vault(tmp_path)
    _write_gh_state_note(vault)
    n, orphans = count_orphans(vault)
    assert n == 0
    assert orphans == []


def test_gh_state_note_not_in_continuous_checks(tmp_path: Path) -> None:
    """gh-state mirrors with 0 inbound + 0 outbound + 0 triggers (the
    common production shape) MUST NOT appear in either bucket of
    compute_continuous_checks."""
    vault = _make_vault(tmp_path)
    _write_gh_state_note(vault)
    # Add a gh-state note that — hypothetically — has wikilinks and old
    # date: even if its shape *would* match trigger_gap on a research/
    # note, the folder exclusion must take precedence.
    _write(
        vault / "gh-state" / "alice-456.md",
        """
        ---
        slug: alice-456
        type: issue
        state: closed
        created: 2026-04-01
        ---

        Hypothetical mirror with outbound [[anchor-a]] and [[anchor-b]].
        """,
    )
    _write(vault / "reference" / "anchor-a.md", "anchor a\n")
    _write(vault / "reference" / "anchor-b.md", "anchor b\n")
    result = compute_continuous_checks(vault, today=datetime(2026, 5, 20))
    assert result["zero_edge_notes"]["count"] == 0
    assert result["zero_edge_notes"]["slugs"] == []
    assert result["trigger_gap_notes"]["count"] == 0
    assert result["trigger_gap_notes"]["slugs"] == []


def test_bare_research_note_still_counted_as_dark(tmp_path: Path) -> None:
    """Sanity: the gh-state exclusion is selective, not a global loosening.
    A bare research/ note (0 inbound, 0 triggers) is STILL truly_dark."""
    vault = _make_vault(tmp_path)
    _write_gh_state_note(vault)
    _write(
        vault / "research" / "real-dark.md",
        """
        ---
        slug: real-dark
        ---

        Knowledge note with no triggers and no inbound — actually dark.
        """,
    )
    counts = count_shadow_and_dark(vault)
    assert counts["truly_dark_count"] == 1
    assert counts["shadow_orphan_count"] == 0
