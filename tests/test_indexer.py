"""Phase 1 of plan 08: indexer smoke tests.

The vault indexer was previously untested; the move from
``core/cortex_index/`` → ``indexer/`` is the right time
to add a small smoke. Three contracts:

1. ``yaml_lite.split_frontmatter`` parses a markdown body with a
   YAML frontmatter block into ``(metadata_dict, body)``.
2. ``build_index.build(vault, db_path)`` produces an SQLite DB
   containing the expected core tables (``notes``, ``links``,
   ``meta``, ``note_metrics``).
3. ``build_index.needs_rebuild`` returns False on a fresh-rebuilt
   DB and True when the DB is missing.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time

import pytest

from indexer.build_index import build, collect_notes, needs_rebuild, slug_for
from indexer.yaml_lite import extract_wikilinks, split_frontmatter


# ---------------------------------------------------------------------------
# yaml_lite


def test_split_frontmatter_extracts_metadata():
    body = "---\ntitle: My Note\ntags: [alpha, beta]\n---\n\nBody content here."
    meta, content = split_frontmatter(body)
    assert meta["title"] == "My Note"
    assert meta["tags"] == ["alpha", "beta"]
    assert content.strip() == "Body content here."


def test_split_frontmatter_no_frontmatter():
    """Plain markdown with no frontmatter returns an empty dict
    and the original body unchanged."""
    body = "# Heading\n\nJust prose, no metadata."
    meta, content = split_frontmatter(body)
    assert meta == {}
    assert content == body


def test_extract_wikilinks_finds_targets():
    body = "See [[foo-note]] and [[bar/baz|baz]] for details."
    links = extract_wikilinks(body)
    assert "foo-note" in links
    # Wikilinks with `|alias` strip the alias and keep the target.
    assert any("bar/baz" in link for link in links)


def test_extract_wikilinks_rescues_backtick_wrapped():
    """Slug-shaped wikilinks inside inline code spans should still count
    as references — daily entries commonly format them as
    `` `[[slug]]` `` and without rescue the target note would appear
    orphaned in vault_health metrics."""
    body = "Daily: see `[[2026-05-11-foo]]` and ``[[bar-note]]``."
    links = extract_wikilinks(body)
    assert "2026-05-11-foo" in links
    assert "bar-note" in links


def test_extract_wikilinks_still_suppresses_bash_expressions():
    """Bash test expressions like ``[[ -d "$x" ]]`` inside backticks
    must NOT trigger a wikilink match — they have spaces and ``$``,
    which the slug-like filter rejects. Same guard applies to fenced
    code blocks (multi-line)."""
    body = (
        'Inline: `if [[ -d "$x" ]]; then echo x; fi`.\n'
        'Fenced:\n```bash\nif [[ -z "$VAR" ]]; then echo no; fi\n```\n'
        "Real link: [[real-note]]."
    )
    links = extract_wikilinks(body)
    assert links == ["real-note"]


# ---------------------------------------------------------------------------
# build_index


def _write_note(path: pathlib.Path, *, title: str, body: str = "Hello.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ntype: reference\nstatus: open\ntags: []\n---\n\n{body}\n"
    )


def test_build_creates_expected_schema(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha", body="Linked: [[beta]].")
    _write_note(vault / "beta.md", title="Beta")

    db_path = tmp_path / "index.db"
    stats = build(vault, db_path)

    assert db_path.is_file()
    # ``build`` reports stats; the schema is the contract.
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    for required in ("notes", "links", "meta", "note_metrics"):
        assert required in tables, (
            f"missing core table {required!r}; stats={stats}, tables present: {tables}"
        )


def test_needs_rebuild_false_when_db_fresh(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    build(vault, db_path)
    # Just-built DB → fresh → no rebuild needed.
    assert needs_rebuild(vault, db_path) is False


def test_needs_rebuild_true_when_db_missing(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    # No build() call — DB doesn't exist.
    assert needs_rebuild(vault, db_path) is True


def test_build_raises_when_vault_missing(tmp_path: pathlib.Path):
    """The indexer surfaces a SystemExit (CLI-friendly) when the
    vault path doesn't exist. Same shape the ``--check`` flow
    relies on."""
    db_path = tmp_path / "index.db"
    with pytest.raises(SystemExit, match="vault not found"):
        build(tmp_path / "nonexistent", db_path)


def test_note_metrics_seeded_from_frontmatter_access_count(tmp_path: pathlib.Path):
    """Frontmatter is canonical for ``access_count``. The cue runner
    bumps both frontmatter and DB on each retrieval; on rebuild, the
    indexer must read access_count from frontmatter so accumulated
    counts survive. Previously the seed always wrote 0, making the
    recency boost inert."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "popular.md").write_text(
        "---\ntitle: Popular\naccess_count: 42\n---\n\nBody.\n"
    )
    (vault / "fresh.md").write_text("---\ntitle: Fresh\n---\n\nBody.\n")

    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute("SELECT slug, access_count FROM note_metrics"))
    finally:
        conn.close()

    assert rows["popular"] == 42, (
        f"expected 42 from frontmatter, got {rows.get('popular')}"
    )
    assert rows["fresh"] == 0, (
        f"missing access_count should default to 0, got {rows.get('fresh')}"
    )


# ---------------------------------------------------------------------------
# Regression: slug collisions on deep folders + meta-based staleness check.
# Both bugs combined to silently drop 423 notes from FTS over 6 days in
# June 2026 — the slug collision crashed the rebuild mid-flight, and the
# mtime-based staleness check never noticed because external opens kept
# bumping the DB file's mtime so it always looked "newer" than the vault.


def test_slug_for_uses_full_parent_path_on_deep_collision(tmp_path: pathlib.Path):
    """Two notes with the same stem nested under DIFFERENT subpaths sharing
    a common top-level folder must get distinct slugs. Previously
    ``slug_for`` used only the top-level folder, so
    archive/dispatched-inflight/README.md and archive/refactor-plans/README.md
    both became ``archive/README`` — UNIQUE constraint crash on insert.
    """
    vault = tmp_path / "vault"
    a = vault / "sub" / "a" / "README.md"
    b = vault / "sub" / "b" / "README.md"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_text("body")
    b.write_text("body")
    colliding = frozenset({"README"})

    assert slug_for(a, vault, colliding) == "sub/a/README"
    assert slug_for(b, vault, colliding) == "sub/b/README"


def test_slug_for_root_collision_falls_through_to_bare_stem(tmp_path: pathlib.Path):
    """A root-level file (parent == ".") with a colliding stem keeps its
    bare stem — there is no folder qualifier to apply at the vault root,
    and the file system already enforces uniqueness at that level."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root_file = vault / "README.md"
    root_file.write_text("body")

    assert slug_for(root_file, vault, frozenset({"README"})) == "README"


def test_build_survives_deep_stem_collision(tmp_path: pathlib.Path):
    """Integration: two README.md files under different deep subpaths in
    the same top-level folder must both make it into the DB. This is the
    case that crashed the live rebuild."""
    vault = tmp_path / "vault"
    _write_note(
        vault / "archive" / "dispatched-inflight" / "README.md", title="Dispatched"
    )
    _write_note(vault / "archive" / "refactor-plans" / "README.md", title="Refactor")

    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        slugs = {row[0] for row in conn.execute("SELECT slug FROM notes")}
    finally:
        conn.close()
    assert "archive/dispatched-inflight/README" in slugs
    assert "archive/refactor-plans/README" in slugs


def test_needs_rebuild_uses_meta_timestamp_not_db_mtime(tmp_path: pathlib.Path):
    """``needs_rebuild`` must source the build time from ``meta.built_at``,
    not from the DB file's filesystem mtime. SQLite WAL mode, query
    side-effects, and external opens all bump the file mtime without the
    index actually being rebuilt — that drift hid 6 days of vault changes
    in June 2026 because the file always looked "newer" than the vault.

    Setup: a DB written with an old ``built_at`` in the meta row, but the
    file's mtime touched to the present. If needs_rebuild were still using
    file mtime, it would say "fresh" and skip the rebuild. The correct
    behavior is to see the old meta timestamp and rebuild.
    """
    import os  # noqa: PLC0415 — keep the test self-contained

    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    # Rewrite meta.built_at to a date in the distant past.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE meta SET built_at = ?",
            ("2020-01-01 00:00:00 UTC",),
        )
        conn.commit()
    finally:
        conn.close()

    # Touch the DB file mtime to now — the old mtime-based check would say "fresh".
    now = time.time()
    os.utime(db_path, (now, now))

    assert needs_rebuild(vault, db_path) is True


def test_needs_rebuild_triggers_on_note_count_mismatch(tmp_path: pathlib.Path):
    """Belt-and-suspenders check: if ``meta.note_count`` doesn't match the
    actual vault file count, force a rebuild. This catches the case where
    a partial/crashed rebuild left an old DB in place — the meta timestamp
    might be recent, but the vault has notes that aren't in the index.
    """
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    # Fresh-built DB → no rebuild needed.
    assert needs_rebuild(vault, db_path) is False

    # Add 5 new notes to the vault that the index doesn't know about.
    for i in range(5):
        _write_note(vault / f"new-{i}.md", title=f"New {i}")

    # Force meta.built_at to "now" so the timestamp check alone wouldn't trip.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE meta SET built_at = ?",
            (time.strftime("%Y-%m-%d %H:%M:%S %Z"),),
        )
        conn.commit()
    finally:
        conn.close()

    # vault_mtime may also have moved, but the count mismatch is the
    # belt-and-suspenders check we care about here.
    assert needs_rebuild(vault, db_path) is True


def test_wikilink_resolves_via_frontmatter_slug(tmp_path: pathlib.Path):
    """A note may carry a frontmatter ``slug:`` that differs from its
    filename stem (e.g. dailies/research notes named with a date prefix).
    Wikilinks frequently address the note by that frontmatter slug. The
    indexer previously keyed resolution only on the filename stem, so such
    links were wrongly marked ``resolved=0`` — ~1,000 false positives in the
    live vault. The resolution map must register the frontmatter slug too.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    # Target file is named with a date prefix but has a bare slug.
    (vault / "2026-06-21-decay-structural-linking.md").write_text(
        "---\ntitle: Decay Structural Linking\n"
        "slug: decay-structural-linking\n---\n\nTarget body.\n"
    )
    # Source links to the target by its frontmatter slug, not the stem.
    (vault / "source.md").write_text(
        "---\ntitle: Source\n---\n\nSee [[decay-structural-linking]].\n"
    )

    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(
            conn.execute(
                "SELECT target_slug, resolved FROM links "
                "WHERE target_raw = 'decay-structural-linking'"
            )
        )
    finally:
        conn.close()

    assert rows, "expected the frontmatter-slug wikilink to be indexed"
    assert all(r[1] == 1 for r in rows), (
        f"frontmatter-slug wikilink should resolve, got {rows}"
    )
    # Resolves to the canonical filename-stem slug of the target note.
    assert rows[0][0] == "2026-06-21-decay-structural-linking", (
        f"expected resolution to the filename-stem slug, got {rows[0][0]}"
    )


def test_filename_stem_slug_wins_over_colliding_frontmatter_slug(
    tmp_path: pathlib.Path,
):
    """The frontmatter slug is registered with ``setdefault`` so the
    canonical filename-stem slug always wins a collision. A note whose
    filename stem equals another note's frontmatter slug must not be
    shadowed."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # Canonical: filename stem == "alpha".
    (vault / "alpha.md").write_text("---\ntitle: Canonical Alpha\n---\n\nBody.\n")
    # A different note declares slug: alpha in frontmatter.
    (vault / "other.md").write_text(
        "---\ntitle: Other\nslug: alpha\n---\n\nBody.\n"
    )
    (vault / "source.md").write_text(
        "---\ntitle: Source\n---\n\nLink [[alpha]].\n"
    )

    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        target_slug = list(
            conn.execute(
                "SELECT target_slug FROM links "
                "WHERE target_raw = 'alpha' AND resolved = 1"
            )
        )
    finally:
        conn.close()

    assert target_slug, "the [[alpha]] link should resolve"
    assert target_slug[0][0] == "alpha", (
        f"filename-stem slug must win the collision, got {target_slug[0][0]}"
    )


def test_needs_rebuild_force_rebuild_on_unparseable_built_at(tmp_path: pathlib.Path):
    """Legacy/corrupt DBs without a parseable ``built_at`` should force a
    rebuild rather than silently stay stale — fail-safe direction."""
    vault = tmp_path / "vault"
    _write_note(vault / "alpha.md", title="Alpha")
    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE meta SET built_at = ?", ("totally not a date",))
        conn.commit()
    finally:
        conn.close()

    assert needs_rebuild(vault, db_path) is True


# ---------------------------------------------------------------------------
# Regression: frontmatter `references:` were silently dropped from the link
# graph. ~930 vault notes carry frontmatter references in the form
# ``"[[slug]] — description"``; the indexer only mined the body, losing
# ~2,500-3,000 declared links and inflating isolated-note counts.
# Fix reuses ``extract_wikilinks`` over each reference entry.


def _by_slug(records: list[dict], slug: str) -> dict:
    for r in records:
        if r["slug"] == slug:
            return r
    raise AssertionError(f"no record with slug={slug!r}")


def test_collect_notes_extracts_frontmatter_references_list(tmp_path: pathlib.Path):
    """Frontmatter ``references:`` declared as a list of
    ``"[[slug]] — description"`` strings (the live-vault format) must
    surface every wikilink target in ``_fm_references``."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "source.md").write_text(
        "---\n"
        "title: Source\n"
        "references:\n"
        '  - "[[foo]] — project goal"\n'
        '  - "[[bar|alias]] — desc"\n'
        "---\n\n"
        "Body without links.\n"
    )

    records = collect_notes(vault)
    src = _by_slug(records, "source")
    assert "foo" in src["_fm_references"]
    assert "bar" in src["_fm_references"]


def test_collect_notes_extracts_frontmatter_references_scalar_string(
    tmp_path: pathlib.Path,
):
    """A scalar ``references:`` string (single entry, not a list) is
    still parsed. Some live-vault notes use this shape."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "source.md").write_text(
        "---\n"
        "title: Source\n"
        'references: "[[single-string]] — desc"\n'
        "---\n\n"
        "Body.\n"
    )

    records = collect_notes(vault)
    src = _by_slug(records, "source")
    assert src["_fm_references"] == ["single-string"]


def test_collect_notes_no_references_field(tmp_path: pathlib.Path):
    """A note without a ``references:`` field gets an empty
    ``_fm_references`` list and doesn't crash collect_notes."""
    vault = tmp_path / "vault"
    _write_note(vault / "plain.md", title="Plain", body="Just prose.")

    records = collect_notes(vault)
    src = _by_slug(records, "plain")
    assert src["_fm_references"] == []


def test_collect_notes_references_non_wikilink_string(tmp_path: pathlib.Path):
    """A ``references:`` entry that contains no wikilink syntax should
    produce zero targets — extract_wikilinks naturally returns []."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "source.md").write_text(
        "---\n"
        "title: Source\n"
        "references:\n"
        '  - "plain text, not a wikilink"\n'
        "---\n\n"
        "Body.\n"
    )

    records = collect_notes(vault)
    src = _by_slug(records, "source")
    assert src["_fm_references"] == []


def test_build_inserts_link_rows_for_frontmatter_references(tmp_path: pathlib.Path):
    """End-to-end: frontmatter references must land in the ``links`` table
    alongside body-extracted wikilinks. This is the contract that drives
    PageRank / isolated-note metrics — body-only extraction was the bug."""
    vault = tmp_path / "vault"
    _write_note(vault / "foo.md", title="Foo")
    _write_note(vault / "bar.md", title="Bar")
    (vault / "source.md").write_text(
        "---\n"
        "title: Source\n"
        "references:\n"
        '  - "[[foo]] — project goal"\n'
        '  - "[[bar|alias]] — desc"\n'
        "---\n\n"
        "Body without inline links.\n"
    )

    db_path = tmp_path / "index.db"
    build(vault, db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        targets = {
            row[0]
            for row in conn.execute(
                "SELECT target_slug FROM links WHERE source_slug = 'source'"
            )
        }
    finally:
        conn.close()
    assert "foo" in targets, f"frontmatter ref 'foo' not in links table: {targets}"
    assert "bar" in targets, f"frontmatter ref 'bar' not in links table: {targets}"
