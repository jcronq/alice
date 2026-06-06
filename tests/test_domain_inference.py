"""Tests for ``metrics.domain_inference``.

Module provenance: salvaged from a thinking-side prototype that landed in
the alice working tree without a PR. These tests pin the inference rules so
the read-only report stays stable and the ``--apply`` mode is exercised at
least once against a controlled vault before anyone points it at the real
cortex.

Test surface:
- ``collect_known_domains`` — distinct ``domain:`` values from vault notes
- ``infer_domain``           — tag → domain (exact + partial-match rules)
- ``find_unknown_domain_notes`` — notes lacking a ``domain:`` field
- ``analyze_notes``          — report shape + recovery counters
- ``apply_inference``        — writes ``domain:`` + ``status: inferred``
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent


from metrics.domain_inference import (
    EXCLUDED_TAGS,
    PARTIAL_MATCH_RULES,
    analyze_notes,
    apply_inference,
    collect_known_domains,
    collect_tag_domain_pairs,
    find_unknown_domain_notes,
    infer_domain,
)
from indexer.yaml_lite import split_frontmatter


# ---------------------------------------------------------------------------
# Helpers — mirror the test_vault_health.py shape so the suite reads uniformly.


def _write(path: Path, content: str) -> Path:
    """Write ``content`` to ``path`` (parents created, leading blank stripped,
    dedented)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dedent(content).lstrip("\n")
    path.write_text(body, encoding="utf-8")
    return path


def _make_vault(tmp_path: Path) -> Path:
    """Fresh empty ``cortex-memory`` directory under ``tmp_path``."""
    vault = tmp_path / "cortex-memory"
    vault.mkdir()
    for sub in ("research", "reference", "projects", "dailies"):
        (vault / sub).mkdir()
    return vault


def _seed_known_domain(vault: Path, domain: str, slug: str | None = None) -> Path:
    """Drop a note that declares ``domain:`` so the module's known-domain set
    contains ``domain``. Without at least one such anchor, no inference fires
    because ``known_domains`` is empty."""
    slug = slug or f"anchor-{domain.replace('/', '-')}"
    return _write(
        vault / "research" / f"{slug}.md",
        f"""
        ---
        slug: {slug}
        title: Anchor for {domain}
        domain: {domain}
        tags: [reference]
        ---
        Anchor note pinning ``{domain}`` into the known-domain index.
        """,
    )


# ---------------------------------------------------------------------------
# Empty / no-op vault


def test_empty_vault_produces_zero_counts(tmp_path):
    vault = _make_vault(tmp_path)
    report = analyze_notes(vault, set(), {})
    assert report["total_unknown"] == 0
    assert report["recovered_count"] == 0
    assert report["not_recovered_count"] == 0
    assert report["recovery_rate"] == 0
    assert report["recovered_notes"] == []
    assert report["not_recovered_notes"] == []


def test_empty_vault_known_domains_is_empty(tmp_path):
    vault = _make_vault(tmp_path)
    assert collect_known_domains(vault) == set()
    assert collect_tag_domain_pairs(vault) == {}


# ---------------------------------------------------------------------------
# Exact-match tag inference


def test_exact_tag_match_infers_domain(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "research" / "candidate.md",
        """
        ---
        slug: candidate
        title: Candidate
        tags: [memory-design]
        ---
        Body.
        """,
    )

    known = collect_known_domains(vault)
    assert "memory-design" in known

    report = analyze_notes(vault, known, collect_tag_domain_pairs(vault))
    assert report["recovered_count"] == 1
    assert report["not_recovered_count"] == 0
    entry = report["recovered_notes"][0]
    assert entry["slug"] == "candidate"
    assert entry["inferred_domain"] == "memory-design"
    assert entry["inferred_from_tag"] == "memory-design"


# ---------------------------------------------------------------------------
# Idempotency — notes that already declare ``domain:`` are skipped


def test_note_with_existing_domain_is_skipped(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "research" / "already-set.md",
        """
        ---
        slug: already-set
        title: Already Set
        domain: memory-design
        tags: [memory-design, reference]
        ---
        Has a domain, should not be touched.
        """,
    )

    unknown = find_unknown_domain_notes(vault)
    assert all(n["slug"] != "already-set" for n in unknown)

    report = analyze_notes(vault, collect_known_domains(vault), {})
    assert report["total_unknown"] == 0
    assert report["recovered_count"] == 0


# ---------------------------------------------------------------------------
# Partial-match rule (``cue-runner`` → ``cue-runner-evaluation``)


def test_partial_match_rule_for_cue_runner(tmp_path):
    """The ``PARTIAL_MATCH_RULES`` table must wire ``cue-runner`` to
    ``cue-runner-evaluation`` when the latter is a known domain."""
    assert PARTIAL_MATCH_RULES.get("cue-runner") == "cue-runner-evaluation"

    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "cue-runner-evaluation")
    _write(
        vault / "research" / "partial.md",
        """
        ---
        slug: partial
        title: Partial Match
        tags: [cue-runner]
        ---
        Tagged ``cue-runner`` only — should map via partial rule.
        """,
    )

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    assert report["recovered_count"] == 1
    entry = report["recovered_notes"][0]
    assert entry["inferred_domain"] == "cue-runner-evaluation"
    assert entry["inferred_from_tag"] == "cue-runner"


def test_partial_match_silent_when_target_domain_missing(tmp_path):
    """``cue-runner`` tag without a ``cue-runner-evaluation`` anchor must NOT
    invent a domain that doesn't exist in the vault."""
    vault = _make_vault(tmp_path)
    # No cue-runner-evaluation anchor — just an unrelated domain.
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "research" / "orphan.md",
        """
        ---
        slug: orphan
        title: Orphan
        tags: [cue-runner]
        ---
        Body.
        """,
    )

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    assert report["recovered_count"] == 0
    assert report["not_recovered_count"] == 1


# ---------------------------------------------------------------------------
# Unrecoverable — note with no useful tags


def test_unrecoverable_note_with_no_matching_tags(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "research" / "nope.md",
        """
        ---
        slug: nope
        title: Not Recoverable
        tags: [reference, research, design]
        ---
        All tags are in EXCLUDED_TAGS, so nothing matches.
        """,
    )

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    assert report["recovered_count"] == 0
    assert report["not_recovered_count"] == 1
    assert report["not_recovered_notes"][0]["slug"] == "nope"


def test_excluded_tags_are_filtered_out(tmp_path):
    """A note carrying both an excluded tag (``research``) AND a recoverable
    one (``memory-design``) must infer from the recoverable one — the
    excluded tag must not short-circuit the search."""
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "research" / "mixed.md",
        """
        ---
        slug: mixed
        title: Mixed Tags
        tags: [research, memory-design, design]
        ---
        Body.
        """,
    )
    # Sanity: ``research`` must actually be in the excluded set, otherwise
    # this test asserts nothing interesting.
    assert "research" in EXCLUDED_TAGS

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    assert report["recovered_count"] == 1
    assert report["recovered_notes"][0]["inferred_domain"] == "memory-design"
    assert report["recovered_notes"][0]["inferred_from_tag"] == "memory-design"


# ---------------------------------------------------------------------------
# Dailies / scaffold files are skipped


def test_daily_notes_are_excluded(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    _write(
        vault / "dailies" / "2026-06-06.md",
        """
        ---
        slug: 2026-06-06
        title: Daily
        tags: [memory-design]
        ---
        Daily logs are deliberately skipped by the scanner.
        """,
    )

    unknown = find_unknown_domain_notes(vault)
    assert all("/dailies/" not in n["path"] for n in unknown)


# ---------------------------------------------------------------------------
# ``infer_domain`` unit cases


def test_infer_domain_exact_match():
    assert infer_domain("retrieval", {"retrieval", "memory-design"}) == "retrieval"


def test_infer_domain_partial_match():
    known = {"cue-runner-evaluation"}
    assert infer_domain("cue-runner", known) == "cue-runner-evaluation"


def test_infer_domain_no_match():
    assert infer_domain("totally-unknown", {"retrieval"}) is None


def test_infer_domain_partial_without_target_returns_none():
    # cue-runner is in PARTIAL_MATCH_RULES but mapped domain not in vault.
    assert infer_domain("cue-runner", {"retrieval"}) is None


# ---------------------------------------------------------------------------
# ``--apply`` mode writes the domain + status: inferred


def test_apply_writes_domain_and_status_inferred(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    target = _write(
        vault / "research" / "candidate.md",
        """
        ---
        slug: candidate
        title: Candidate
        tags: [memory-design]
        ---
        Body content stays put.
        """,
    )

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    assert report["recovered_count"] == 1

    result = apply_inference(report, vault, dry_run=False)
    assert result["changes_applied"] == 1
    assert result["errors"] == []

    fm, body = split_frontmatter(target.read_text(encoding="utf-8"))
    assert fm["domain"] == "memory-design"
    assert fm["status"] == "inferred"
    # Body content is preserved.
    assert "Body content stays put." in body


def test_apply_dry_run_does_not_mutate_disk(tmp_path):
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    target = _write(
        vault / "research" / "candidate.md",
        """
        ---
        slug: candidate
        title: Candidate
        tags: [memory-design]
        ---
        Body.
        """,
    )
    before = target.read_text(encoding="utf-8")

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    result = apply_inference(report, vault, dry_run=True)
    assert result["changes_applied"] == 1

    # File on disk is byte-identical — dry-run only formats the change list.
    assert target.read_text(encoding="utf-8") == before


def test_apply_preserves_existing_non_inferred_status(tmp_path):
    """A note that already has e.g. ``status: stable`` must get the inferred
    flag appended, not overwritten."""
    vault = _make_vault(tmp_path)
    _seed_known_domain(vault, "memory-design")
    target = _write(
        vault / "research" / "with-status.md",
        """
        ---
        slug: with-status
        title: With Status
        status: stable
        tags: [memory-design]
        ---
        Body.
        """,
    )

    report = analyze_notes(
        vault,
        collect_known_domains(vault),
        collect_tag_domain_pairs(vault),
    )
    apply_inference(report, vault, dry_run=False)

    fm, _ = split_frontmatter(target.read_text(encoding="utf-8"))
    assert fm["domain"] == "memory-design"
    # status is the concatenated form — order-tolerant assertion.
    assert "stable" in fm["status"]
    assert "inferred" in fm["status"]
