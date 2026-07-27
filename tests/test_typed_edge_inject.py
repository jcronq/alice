"""Tests for the typed-edge injection executor (Guard 3, Phase 1).

Covers plan validation, dry-run semantics, DB write correctness, idempotency,
verification output, and the fail-loud gate for missing DB tables.

Design: ``~/alice-mind/cortex-memory/research/2026-07-27-typed-edge-injection-script-design.md``
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from alice_thinking.experiment import typed_edge_inject as tei

TYPED_EDGES_DDL = """
CREATE TABLE typed_edges (
    id             INTEGER PRIMARY KEY,
    from_slug      TEXT NOT NULL,
    to_slug        TEXT NOT NULL,
    link_type      TEXT NOT NULL,
    link_source    TEXT NOT NULL DEFAULT 'predicate',
    context        TEXT,
    confidence     TEXT NOT NULL DEFAULT 'high',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_slug, to_slug, link_type, link_source)
);
"""

NOTES_DDL = """
CREATE TABLE notes (
    rowid INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    folder TEXT NOT NULL,
    title TEXT,
    note_type TEXT,
    status TEXT,
    tags_json TEXT,
    aliases_json TEXT,
    created TEXT,
    updated TEXT,
    body TEXT
);
"""


def _make_db(path: Path, *, with_notes_table: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(TYPED_EDGES_DDL)
    if with_notes_table:
        conn.executescript(NOTES_DDL)
    conn.commit()
    conn.close()


def _seed_note_row(db: Path, slug: str, rel_path: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO notes (slug, path, folder, title) VALUES (?, ?, ?, ?)",
        (slug, rel_path, str(Path(rel_path).parent), slug),
    )
    conn.commit()
    conn.close()


def _sample_plan(n: int = 3) -> dict[str, list[dict[str, str]]]:
    return {
        "targets": [
            {
                "from_slug": f"hub-{i}",
                "to_slug": f"target-{i}",
                "link_type": "references",
                "context": "test injection",
                "confidence": "high",
            }
            for i in range(n)
        ]
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "cortex-index.db"
    _make_db(p)
    return p


@pytest.fixture
def plan_path(tmp_path: Path) -> Path:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(_sample_plan(3)))
    return p


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


# ----------------------------------------------------------------------------
# Plan validation
# ----------------------------------------------------------------------------


class TestLoadPlanValidatesShape:
    def test_missing_targets_key_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"items": []}))
        with pytest.raises(ValueError, match="missing top-level 'targets'"):
            tei.load_plan(p)

    def test_targets_not_a_list_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"targets": {"nope": True}}))
        with pytest.raises(ValueError, match="'targets' must be a list"):
            tei.load_plan(p)

    def test_target_missing_field_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(
            json.dumps(
                {"targets": [{"from_slug": "a", "to_slug": "b", "link_type": "c"}]}
            )
        )
        with pytest.raises(ValueError, match="missing required fields"):
            tei.load_plan(p)

    def test_top_level_not_object_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(["a", "b"]))
        with pytest.raises(ValueError, match="top-level must be a JSON object"):
            tei.load_plan(p)

    def test_valid_plan_returns_targets(self, plan_path: Path) -> None:
        targets = tei.load_plan(plan_path)
        assert len(targets) == 3
        assert targets[0]["from_slug"] == "hub-0"


# ----------------------------------------------------------------------------
# Dry-run
# ----------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    db_path: Path, plan_path: Path, vault_path: Path
) -> None:
    rc = tei.main(
        [
            "--db",
            str(db_path),
            "--targets",
            str(plan_path),
            "--vault",
            str(vault_path),
            "--dry-run",
            "--skip-frontmatter",
        ]
    )
    assert rc == 0
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM typed_edges").fetchone()[0]
    conn.close()
    assert count == 0


# ----------------------------------------------------------------------------
# Full run + verification
# ----------------------------------------------------------------------------


def test_inserts_all_targets(
    db_path: Path, plan_path: Path, vault_path: Path
) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = tei.main(
            [
                "--db",
                str(db_path),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
                "--skip-frontmatter",
            ]
        )
    assert rc == 0
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM typed_edges WHERE link_source = 'manual'"
    ).fetchone()[0]
    conn.close()
    assert count == 3

    results = json.loads(buf.getvalue())
    assert results["verification_passed"] is True
    assert results["parsed_counts"]["edges_written"] == 3
    assert results["parsed_counts"]["expected_edges"] == 3
    assert results["parsed_counts"]["inserted_now"] == 3
    assert results["parsed_counts"]["skipped_duplicates"] == 0
    assert results["parsed_counts"]["schema_valid"] is True
    assert "spot_check" in results["raw_verification_output"]
    assert "schema_check" in results["raw_verification_output"]


def test_idempotent_reinvocation(
    db_path: Path, plan_path: Path, vault_path: Path
) -> None:
    for _ in range(2):
        with redirect_stdout(io.StringIO()):
            rc = tei.main(
                [
                    "--db",
                    str(db_path),
                    "--targets",
                    str(plan_path),
                    "--vault",
                    str(vault_path),
                    "--skip-frontmatter",
                ]
            )
        assert rc == 0

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM typed_edges WHERE link_source = 'manual'"
    ).fetchone()[0]
    dup = conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT from_slug, to_slug, link_type, link_source, COUNT(*) c"
        " FROM typed_edges GROUP BY from_slug, to_slug, link_type, link_source"
        " HAVING c > 1"
        ")"
    ).fetchone()[0]
    conn.close()
    assert count == 3, "second run must not duplicate rows"
    assert dup == 0


def test_verification_query_counts_match_expected(
    db_path: Path, plan_path: Path, vault_path: Path
) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = tei.main(
            [
                "--db",
                str(db_path),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
                "--skip-frontmatter",
            ]
        )
    assert rc == 0
    results = json.loads(buf.getvalue())
    # Raw output must contain the manual edge total as a stringified integer.
    assert results["raw_verification_output"]["manual_edge_total"] == "3"
    # Parsed counts and raw text must agree — the LLM trust gap fix.
    assert results["parsed_counts"]["edges_written"] == 3
    assert results["experiment_slug"] == "typed-edge-hub-note-experiment"


def test_verification_fails_when_expected_mismatch(
    db_path: Path, tmp_path: Path
) -> None:
    """Directly exercise run_verification: pass an inflated expected count."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO typed_edges (from_slug, to_slug, link_type, link_source, context, confidence) "
        "VALUES ('a', 'b', 'references', 'manual', 'ctx', 'high')"
    )
    conn.commit()
    passed, block = tei.run_verification(conn, expected_total_manual=99)
    conn.close()
    assert passed is False
    assert "expected 99" in block["failure_reason"]


# ----------------------------------------------------------------------------
# Fail-loud gates
# ----------------------------------------------------------------------------


def test_missing_table_fails_loud(
    tmp_path: Path, plan_path: Path, vault_path: Path
) -> None:
    bad_db = tmp_path / "empty.db"
    sqlite3.connect(bad_db).close()  # create an empty DB, no typed_edges table
    with pytest.raises(RuntimeError, match="typed_edges table not found"):
        tei.main(
            [
                "--db",
                str(bad_db),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
                "--skip-frontmatter",
            ]
        )


def test_schema_drift_fails_loud(
    tmp_path: Path, plan_path: Path, vault_path: Path
) -> None:
    bad_db = tmp_path / "drift.db"
    conn = sqlite3.connect(bad_db)
    conn.executescript(
        "CREATE TABLE typed_edges (from_slug TEXT, to_slug TEXT);"
    )
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="missing required columns"):
        tei.main(
            [
                "--db",
                str(bad_db),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
                "--skip-frontmatter",
            ]
        )


# ----------------------------------------------------------------------------
# Link-type override
# ----------------------------------------------------------------------------


def test_link_type_override_applied(
    db_path: Path, plan_path: Path, vault_path: Path
) -> None:
    with redirect_stdout(io.StringIO()):
        rc = tei.main(
            [
                "--db",
                str(db_path),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
                "--link-type",
                "supports",
                "--skip-frontmatter",
            ]
        )
    assert rc == 0
    conn = sqlite3.connect(db_path)
    types = {row[0] for row in conn.execute("SELECT DISTINCT link_type FROM typed_edges")}
    conn.close()
    assert types == {"supports"}


# ----------------------------------------------------------------------------
# Frontmatter update
# ----------------------------------------------------------------------------


def test_append_references_creates_field_when_missing() -> None:
    original = "---\ntitle: Hub Note\n---\nBody here.\n"
    new_text, changed = tei._append_references(original, ["target-x"])
    assert changed is True
    assert "references:" in new_text
    assert "[[target-x]]" in new_text
    assert "Body here." in new_text


def test_append_references_merges_with_existing() -> None:
    original = (
        "---\n"
        "title: Hub Note\n"
        "references:\n"
        "  - [[already-here]]\n"
        "---\n"
        "Body.\n"
    )
    new_text, changed = tei._append_references(original, ["new-one", "already-here"])
    assert changed is True
    # Existing entry preserved exactly once, new one appended.
    assert new_text.count("[[already-here]]") == 1
    assert new_text.count("[[new-one]]") == 1


def test_append_references_is_idempotent_when_all_present() -> None:
    original = (
        "---\n"
        "title: Hub\n"
        "references:\n"
        "  - [[a]]\n"
        "  - [[b]]\n"
        "---\n"
        "Body.\n"
    )
    _, changed = tei._append_references(original, ["a", "b"])
    assert changed is False


def test_append_references_creates_frontmatter_when_absent() -> None:
    original = "# Hub Note\n\nJust markdown, no frontmatter.\n"
    new_text, changed = tei._append_references(original, ["x"])
    assert changed is True
    assert new_text.startswith("---\n")
    assert "[[x]]" in new_text
    assert "# Hub Note" in new_text


def test_update_frontmatter_end_to_end(
    tmp_path: Path, db_path: Path, vault_path: Path
) -> None:
    hub_rel = "projects/hub-0.md"
    (vault_path / "projects").mkdir()
    (vault_path / hub_rel).write_text(
        "---\ntitle: Hub 0\nreferences:\n  - [[existing]]\n---\nBody.\n"
    )
    _seed_note_row(db_path, "hub-0", hub_rel)

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "from_slug": "hub-0",
                        "to_slug": "new-target",
                        "link_type": "references",
                        "context": "test",
                        "confidence": "high",
                    }
                ]
            }
        )
    )

    with redirect_stdout(io.StringIO()):
        rc = tei.main(
            [
                "--db",
                str(db_path),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
            ]
        )
    assert rc == 0
    updated_text = (vault_path / hub_rel).read_text()
    assert "[[existing]]" in updated_text
    assert "[[new-target]]" in updated_text


def test_missing_note_warns_but_continues(
    tmp_path: Path, db_path: Path, vault_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Plan references hub-missing which is not in the notes table.
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "from_slug": "hub-missing",
                        "to_slug": "target",
                        "link_type": "references",
                        "context": "test",
                        "confidence": "high",
                    }
                ]
            }
        )
    )

    with redirect_stdout(io.StringIO()):
        rc = tei.main(
            [
                "--db",
                str(db_path),
                "--targets",
                str(plan_path),
                "--vault",
                str(vault_path),
            ]
        )
    assert rc == 0
    captured = capsys.readouterr()
    assert "note not found for slug=hub-missing" in captured.err
    # DB edge still written despite the missing note.
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM typed_edges WHERE link_source = 'manual'"
    ).fetchone()[0]
    conn.close()
    assert count == 1
