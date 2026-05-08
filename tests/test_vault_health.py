"""Eval-contract tests for ``alice_metrics.vault_health``.

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

import re
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from alice_metrics.vault_health import (
    count_broken_wikilinks,
    count_orphans,
    count_total_notes,
    count_wakes_by_stage,
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
# Regression: double-backtick code spans (``[[wikilink]]``) leaked through
# the inline-code stripper in alice_indexer.yaml_lite. Markdown allows
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
