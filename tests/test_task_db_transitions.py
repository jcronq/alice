"""Tests for ``task db transitions`` / ``db history`` / ``db deps`` (issue #832).

Covers the CLI shape conflict resolution documented in the design:

* ``db transitions`` → cross-item, filterable, newest-first, tabular
  default. ``--item`` is optional; the old positional shape is gone.
* ``db history <item_id>`` → the "everything for one item" path
  that the old ``db transitions <item_id>`` used to serve.
* ``db deps <item_id>`` → read-only dependency dump; must degrade
  gracefully when the ``item_dependencies`` table doesn't exist yet
  (older DB from before PR-10 lands).

Fixture style mirrors ``tests/test_alice_state_db_sm_seed.py`` —
build a tmp mind dir, seed rows directly via ``apply_schema`` +
raw INSERTs, and exercise :func:`alice_forge.task_cli.main` end-to-end
with ``capsys`` capturing stdout / stderr.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from alice_forge import task_cli
from alice_state_db import apply_schema


def _db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Canonical ``alice-state.db`` location for the tmp mind tree."""
    p = tmp_path / "inner" / "state" / "alice-state.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def seeded_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a path to an alice-state.db seeded with two items + several
    transitions. Shape mirrors the SM-v2 seeder's output.
    """
    path = _db_path(tmp_path)
    conn = sqlite3.connect(str(path))
    apply_schema(conn)

    conn.execute(
        """
        INSERT INTO items (item_id, item_type, title, created_at, updated_at, author)
        VALUES
            ('gh:jcronq/alice#832', 'gh:issue', 'Status-site history drawer',
             '2026-11-07T10:00:00Z', '2026-11-07T12:00:00Z', 'jcronq'),
            ('gh:jcronq/alice#823', 'gh:issue', 'Deprecate SM-on-GitHub',
             '2026-11-06T00:00:00Z', '2026-11-07T11:00:00Z', 'jcronq')
        """
    )
    conn.execute(
        """
        INSERT INTO state_machine (item_id, state, entered_at)
        VALUES
            ('gh:jcronq/alice#832', 'sm:building',
             '2026-11-07T12:00:00Z'),
            ('gh:jcronq/alice#823', 'sm:done',
             '2026-11-07T11:00:00Z')
        """
    )
    # Six transitions spread across the two items. Insert in chronological
    # order so transition_id ascends with ts (the test asserts both).
    conn.executemany(
        """
        INSERT INTO transitions
            (item_id, from_state, to_state, actor, reason, ts, source, source_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # item #832 timeline
            ("gh:jcronq/alice#832", None, "sm:selected", "jason",
             "picked up", "2026-11-07T09:00:00Z", "gh-comment", "r1"),
            ("gh:jcronq/alice#832", "sm:selected", "sm:designing", "alice",
             "design", "2026-11-07T10:00:00Z", "gh-comment", "r2"),
            ("gh:jcronq/alice#832", "sm:designing", "sm:building", "alice",
             "spawn", "2026-11-07T12:00:00Z", "gh-comment", "r3"),
            # item #823 timeline
            ("gh:jcronq/alice#823", None, "sm:selected", "jason",
             "picked up", "2026-11-06T00:00:00Z", "gh-comment", "r4"),
            ("gh:jcronq/alice#823", "sm:selected", "sm:building", "alice",
             "spawn", "2026-11-06T10:00:00Z", "gh-comment", "r5"),
            ("gh:jcronq/alice#823", "sm:building", "sm:done", "alice",
             "merged", "2026-11-07T11:00:00Z", "gh-comment", "r6"),
        ],
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# db transitions
# ---------------------------------------------------------------------------


def test_db_transitions_default_is_tabular_and_newest_first(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Issue #832 — new shape: tabular, newest-first, default ``--limit 20``."""
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions", "--limit", "5",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    # Tabular default — should not be JSON.
    assert not out.strip().startswith("[")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 5
    # Newest-first ⇒ the youngest transition (building→done on #823
    # at 11:00) is first; the oldest of the 6 seeded is clipped by limit.
    assert "sm:building → sm:done" in lines[0]
    assert "gh:jcronq/alice#823" in lines[0]


def test_db_transitions_json_flag_preserves_json(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions", "--limit", "3",
        ]
    )
    assert exit_code == 0
    rows = json.loads(capsys.readouterr().out.strip())
    assert isinstance(rows, list) and len(rows) == 3
    # ORDER BY transition_id DESC
    assert rows[0]["transition_id"] > rows[1]["transition_id"] > rows[2]["transition_id"]


def test_db_transitions_filters_item_and_state(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``--item X --state sm:done`` returns only #823's landing-on-done row."""
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions",
            "--item", "gh:jcronq/alice#823",
            "--state", "sm:done",
        ]
    )
    assert exit_code == 0
    rows = json.loads(capsys.readouterr().out.strip())
    assert len(rows) == 1
    assert rows[0]["item_id"] == "gh:jcronq/alice#823"
    assert rows[0]["to_state"] == "sm:done"


def test_db_transitions_filters_by_actor(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions", "--actor", "jason",
        ]
    )
    assert exit_code == 0
    rows = json.loads(capsys.readouterr().out.strip())
    assert len(rows) == 2
    assert {r["item_id"] for r in rows} == {
        "gh:jcronq/alice#823", "gh:jcronq/alice#832",
    }
    assert all(r["actor"] == "jason" for r in rows)


def test_db_transitions_filters_by_since(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions", "--since", "2026-11-07T00:00:00Z",
        ]
    )
    assert exit_code == 0
    rows = json.loads(capsys.readouterr().out.strip())
    # Three transitions seeded on 2026-11-07 (sm:selected#832, designing,
    # building, and done#823 — four total).
    assert len(rows) == 4
    assert all(r["ts"] >= "2026-11-07T00:00:00Z" for r in rows)


def test_db_transitions_tabular_empty(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Empty result still exits 0; prints a human marker, not a crash."""
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "transitions", "--actor", "nobody-such",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(no transitions match)" in out


# ---------------------------------------------------------------------------
# db history — the "everything for one item" subcommand
# ---------------------------------------------------------------------------


def test_db_history_prints_state_line_and_full_tail(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Design: ``db history`` absorbs the old ``db transitions <item>`` shape.

    Prints the current state_machine row at the top, then the full
    oldest-first transition tail (NOT capped at 5 like ``db item``).
    """
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "history", "gh:jcronq/alice#832",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    # Header line + current state line are both present.
    assert "# gh:jcronq/alice#832" in out
    assert "Status-site history drawer" in out
    assert "state: sm:building" in out
    # All three transitions for #832 land in the body (full tail, not
    # the 5-cap ``db item`` applies).
    assert "sm:selected" in out and "sm:designing" in out and "sm:building" in out
    # Oldest-first: ``None → sm:selected`` must appear before
    # ``sm:selected → sm:designing``.
    idx_a = out.index("— → sm:selected")
    idx_b = out.index("sm:selected → sm:designing")
    assert idx_a < idx_b


def test_db_history_not_found_exits_2(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "history", "gh:nope/nope#1",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_db_history_json_payload(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "history", "gh:jcronq/alice#823",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["item"]["item_id"] == "gh:jcronq/alice#823"
    assert payload["state"]["state"] == "sm:done"
    # Oldest-first full tail — three transitions for #823.
    tos = [t["to_state"] for t in payload["transitions"]]
    assert tos == ["sm:selected", "sm:building", "sm:done"]


# ---------------------------------------------------------------------------
# db deps — read-only, graceful when the table doesn't exist
# ---------------------------------------------------------------------------


def test_db_deps_prints_none_when_empty(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Design: ``(none)`` + exit 0 even though the table exists but has no
    edges for this item. Matches the "prints `(none)` otherwise"
    requirement in the issue."""
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "deps", "gh:jcronq/alice#832",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(none)" in out


def test_db_deps_lists_edges(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When the ``item_dependencies`` table has rows, each edge prints."""
    conn = sqlite3.connect(str(seeded_db))
    conn.execute(
        "INSERT INTO item_dependencies "
        "(item_id, depends_on_item_id, kind, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            "gh:jcronq/alice#832",
            "gh:jcronq/alice#823",
            "blocks",
            "2026-11-07T10:00:00Z",
        ),
    )
    conn.commit()
    conn.close()

    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "deps", "gh:jcronq/alice#832",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "blocks" in out
    assert "gh:jcronq/alice#823" in out


def test_db_deps_degrades_gracefully_without_table(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A DB that predates the ``item_dependencies`` table (PR-10 lands it)
    must return ``(none)`` + exit 0, not raise ``no such table``.

    We can't call ``apply_schema`` because it idempotently creates the
    table. Build a minimal DB by hand with just the ``items`` /
    ``state_machine`` substrate and verify the CLI survives.
    """
    path = tmp_path / "inner" / "state" / "alice-state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    # Just enough schema for the CLI's RO ``connect`` to succeed —
    # ``items`` has to exist so ``item_history`` lookups don't blow up
    # elsewhere. We deliberately DO NOT create ``item_dependencies``.
    conn.execute(
        "CREATE TABLE items (item_id TEXT PRIMARY KEY, item_type TEXT, "
        "title TEXT, created_at TEXT, updated_at TEXT, author TEXT, "
        "summary TEXT, source_path TEXT)"
    )
    conn.execute(
        "INSERT INTO items (item_id, item_type, title) VALUES (?, ?, ?)",
        ("gh:jcronq/alice#832", "gh:issue", "x"),
    )
    conn.commit()
    conn.close()

    # Open via the CLI, which will run its own ``apply_schema`` on a
    # RW connect — but the CLI reads RO, so the schema stays as-is.
    # (The RO ``connect`` path never calls ``apply_schema``, see
    # ``alice_state_db.read_api.connect``.)
    exit_code = task_cli.main(
        [
            "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(path),
            "deps", "gh:jcronq/alice#832",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "(none)" in out


def test_db_deps_json_flag(
    tmp_path: pathlib.Path,
    seeded_db: pathlib.Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = task_cli.main(
        [
            "--json", "--root", str(tmp_path / "inner" / "tasks"),
            "db", "--db-path", str(seeded_db),
            "deps", "gh:jcronq/alice#832",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == []
