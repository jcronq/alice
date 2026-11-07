"""Tests for ``item_dependencies`` + the dispatcher dep-gate (issue #833).

Covers:

* Schema — the ``item_dependencies`` table is created idempotently by
  :func:`alice_state_db.apply_schema`; the reverse-lookup index is
  present; primary key uniqueness holds.
* Helpers (:mod:`alice_state_db.dependencies`) — happy-path add/remove,
  cycle detection, unknown-item rejection, transitive
  :func:`is_ready` / :func:`blocked_by_count`.
* Task CLI — ``task deps add``/``list``/``remove`` round-trip.
* Dispatcher gate — a ``sm:selected`` item with a non-terminal
  ``blocks`` prerequisite is skipped by :func:`alice_forge.dispatcher.main.run`;
  once the prerequisite transitions to ``sm:done`` the next cadence
  picks the dependent up.

Fixtures intentionally stay minimal — only the DB rows the specific
test asserts against are seeded. ``items`` + ``state_machine`` rows
are inserted directly rather than going through the ``sm_seed``
derive pass; the gate only reads those two tables + the one under
test, so the full seeder's cost isn't warranted here.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alice_forge import task_cli
from alice_state_db import (
    CycleError,
    DependencyError,
    UnknownItemError,
    add_dependency,
    apply_schema,
    blocked_by_count,
    blockers_for,
    gh_issue_id,
    is_ready,
    non_terminal_blocker_detail,
    remove_dependency,
)
from alice_forge.dispatcher.dependency_gate import (
    _check_against_conn,
    build_default_check,
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Return an open RW connection pointed at a fresh tmp DB.

    ``apply_schema`` is run explicitly so every test starts against a
    fully-migrated schema. The connection is closed in a finalizer so
    the WAL files don't stick around in ``tmp_path``.
    """
    db_path = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


def _seed_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    state: str | None = None,
    title: str | None = None,
) -> None:
    """Minimal ``items`` + optional ``state_machine`` seed.

    The dep helpers only need ``items.item_id`` + optionally
    ``state_machine.state``; everything else stays NULL. Keeps the
    fixture concise.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO items (item_id, item_type, title)
        VALUES (?, 'gh:issue', ?)
        """,
        (item_id, title or f"title-{item_id}"),
    )
    if state is not None:
        conn.execute(
            """
            INSERT OR REPLACE INTO state_machine (item_id, state, entered_at)
            VALUES (?, ?, '2026-11-07T00:00:00+00:00')
            """,
            (item_id, state),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Schema tests


def test_schema_creates_item_dependencies_table(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='item_dependencies'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_creates_reverse_lookup_index(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='item_dependencies'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_item_deps_depends_on" in names


def test_schema_apply_is_idempotent(tmp_path: pathlib.Path) -> None:
    """Running ``apply_schema`` twice must not raise."""
    db_path = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        apply_schema(c)  # second call is the real assertion
    finally:
        c.close()


def test_schema_primary_key_prevents_duplicates(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    conn.execute(
        """
        INSERT INTO item_dependencies
            (item_id, depends_on_item_id, kind, created_at)
        VALUES ('gh:org/repo#1', 'gh:org/repo#2', 'blocks', '2026-01-01T00:00:00Z')
        """
    )
    # Inserting the same composite key must fail with IntegrityError.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO item_dependencies
                (item_id, depends_on_item_id, kind, created_at)
            VALUES ('gh:org/repo#1', 'gh:org/repo#2', 'blocks', '2026-01-02T00:00:00Z')
            """
        )


# ---------------------------------------------------------------------------
# add_dependency / remove_dependency


def test_add_dependency_happy_path(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")

    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2", kind="blocks")

    rows = conn.execute(
        """
        SELECT item_id, depends_on_item_id, kind FROM item_dependencies
        """
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "gh:org/repo#1"
    assert rows[0]["depends_on_item_id"] == "gh:org/repo#2"
    assert rows[0]["kind"] == "blocks"


def test_add_dependency_is_idempotent(conn: sqlite3.Connection) -> None:
    """A second identical ``add`` must not raise and must not double-write.

    This is the PR-1 stub behaviour the issue body asks for —
    ``INSERT OR IGNORE`` semantics.
    """
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")

    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM item_dependencies"
    ).fetchone()
    assert rows["n"] == 1


def test_add_dependency_rejects_invalid_kind(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    with pytest.raises(DependencyError):
        add_dependency(
            conn,
            "gh:org/repo#1",
            "gh:org/repo#2",
            kind="depends_sortof",  # not in VALID_KINDS
        )


def test_add_dependency_rejects_unknown_dependent(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#2")
    with pytest.raises(UnknownItemError):
        add_dependency(conn, "gh:org/repo#99", "gh:org/repo#2")


def test_add_dependency_rejects_unknown_prerequisite(
    conn: sqlite3.Connection,
) -> None:
    _seed_item(conn, "gh:org/repo#1")
    with pytest.raises(UnknownItemError):
        add_dependency(conn, "gh:org/repo#1", "gh:org/repo#99")


def test_add_dependency_rejects_self_edge(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    with pytest.raises(CycleError):
        add_dependency(conn, "gh:org/repo#1", "gh:org/repo#1")


def test_add_dependency_rejects_direct_cycle(conn: sqlite3.Connection) -> None:
    """``A -> B`` followed by ``B -> A`` must raise ``CycleError``."""
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    with pytest.raises(CycleError):
        add_dependency(conn, "gh:org/repo#2", "gh:org/repo#1")


def test_add_dependency_rejects_transitive_cycle(conn: sqlite3.Connection) -> None:
    """``A->B``, ``B->C``, then adding ``C->A`` cycles via two hops."""
    for n in (1, 2, 3):
        _seed_item(conn, f"gh:org/repo#{n}")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#2", "gh:org/repo#3")
    with pytest.raises(CycleError):
        add_dependency(conn, "gh:org/repo#3", "gh:org/repo#1")


def test_remove_dependency_happy_path(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert blockers_for(conn, "gh:org/repo#1") == ["gh:org/repo#2"]

    remove_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert blockers_for(conn, "gh:org/repo#1") == []


def test_remove_dependency_is_idempotent(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    # Not previously added — remove should be a clean no-op.
    remove_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert blockers_for(conn, "gh:org/repo#1") == []


# ---------------------------------------------------------------------------
# is_ready / blocked_by_count / blockers_for


def test_is_ready_true_when_no_deps(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    assert is_ready(conn, "gh:org/repo#1") is True
    assert blocked_by_count(conn, "gh:org/repo#1") == 0


def test_is_ready_false_when_prereq_in_building(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2", state="sm:building")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert is_ready(conn, "gh:org/repo#1") is False
    assert blocked_by_count(conn, "gh:org/repo#1") == 1


def test_is_ready_true_when_prereq_done(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2", state="sm:done")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert is_ready(conn, "gh:org/repo#1") is True


def test_is_ready_true_when_prereq_rejected(conn: sqlite3.Connection) -> None:
    """``sm:rejected`` is terminal — the dependent must unblock too."""
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2", state="sm:rejected")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert is_ready(conn, "gh:org/repo#1") is True


def test_is_ready_transitive_chain(conn: sqlite3.Connection) -> None:
    """A depends on B depends on C — C non-terminal gates A too."""
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2", state="sm:selected")
    _seed_item(conn, "gh:org/repo#3", state="sm:building")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#2", "gh:org/repo#3")
    assert is_ready(conn, "gh:org/repo#1") is False
    # Count reflects transitive blockers (both #2 and #3).
    assert blocked_by_count(conn, "gh:org/repo#1") == 2


def test_is_ready_treats_missing_state_as_blocking(
    conn: sqlite3.Connection,
) -> None:
    """Missing ``state_machine`` row counts as "still blocking" — fail safe."""
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2")  # no state yet
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    assert is_ready(conn, "gh:org/repo#1") is False


def test_blockers_for_returns_only_direct_prereqs(
    conn: sqlite3.Connection,
) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    _seed_item(conn, "gh:org/repo#3")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#2", "gh:org/repo#3")
    # Direct blockers of #1 is only #2 — #3 shows up in transitive
    # walks but not in the plain lookup.
    assert blockers_for(conn, "gh:org/repo#1") == ["gh:org/repo#2"]


def test_blockers_for_filters_by_kind(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:org/repo#1")
    _seed_item(conn, "gh:org/repo#2")
    _seed_item(conn, "gh:org/repo#3")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2", kind="blocks")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#3", kind="follows")

    assert blockers_for(conn, "gh:org/repo#1", kind="blocks") == [
        "gh:org/repo#2"
    ]
    assert blockers_for(conn, "gh:org/repo#1", kind="follows") == [
        "gh:org/repo#3"
    ]


def test_follows_kind_does_not_gate(conn: sqlite3.Connection) -> None:
    """``follows`` is an ordering hint — dispatcher must NOT gate on it."""
    _seed_item(conn, "gh:org/repo#1", state="sm:selected")
    _seed_item(conn, "gh:org/repo#2", state="sm:building")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2", kind="follows")
    assert is_ready(conn, "gh:org/repo#1") is True


# ---------------------------------------------------------------------------
# Gate log formatting


def test_gate_check_returns_none_when_ready(conn: sqlite3.Connection) -> None:
    _seed_item(conn, "gh:jcronq/alice#833", state="sm:selected")
    assert _check_against_conn(conn, "gh:jcronq/alice#833") is None


def test_gate_check_returns_log_line_when_blocked(
    conn: sqlite3.Connection,
) -> None:
    _seed_item(conn, "gh:jcronq/alice#833", state="sm:selected")
    _seed_item(conn, "gh:jcronq/alice#824", state="sm:building")
    add_dependency(
        conn, "gh:jcronq/alice#833", "gh:jcronq/alice#824"
    )
    log = _check_against_conn(conn, "gh:jcronq/alice#833")
    assert log is not None
    assert "gh:jcronq/alice#833" in log
    assert "gh:jcronq/alice#824" in log
    assert "sm:building" in log
    assert log.startswith("[sm-dispatcher]")


def test_gate_default_fail_open_on_missing_db(tmp_path: pathlib.Path) -> None:
    """A broken/missing DB must not lock the dispatcher out.

    The default check captures ``db_path`` at bind time; if the file
    can't be opened read-only the closure returns ``None`` (ready)
    rather than raising. We simulate a non-existent dir to force the
    open-fail path — :func:`alice_state_db.connect` creates the file
    on first read-only access, so point at a path whose PARENT is
    missing to trigger a real ``sqlite3.Error``.
    """
    # connect() creates the parent on-the-fly for a missing file, but
    # passing a path where the parent is a REGULAR FILE triggers the
    # error path cleanly.
    sentinel = tmp_path / "not-a-dir"
    sentinel.write_text("x")
    db_path = sentinel / "alice-state.db"
    check = build_default_check(db_path=db_path, log=lambda _s: None)
    # Even though the DB can't be opened, fail-open returns None:
    assert check("jcronq/alice", 833) is None


# ---------------------------------------------------------------------------
# CLI round-trip


def test_cli_deps_add_and_list(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        _seed_item(c, "gh:org/repo#1")
        _seed_item(c, "gh:org/repo#2")
    finally:
        c.close()

    rc = task_cli.main(
        [
            "--root",
            str(tmp_path),
            "deps",
            "--db-path",
            str(db_path),
            "add",
            "gh:org/repo#1",
            "gh:org/repo#2",
        ]
    )
    assert rc == 0

    rc = task_cli.main(
        [
            "--root",
            str(tmp_path),
            "--json",
            "deps",
            "--db-path",
            str(db_path),
            "list",
            "gh:org/repo#1",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    # Last line is the list JSON (add's output was already consumed).
    import json as _json

    payload = _json.loads(out[-1])
    assert payload["depends_on"] == ["gh:org/repo#2"]


def test_cli_deps_remove(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        _seed_item(c, "gh:org/repo#1")
        _seed_item(c, "gh:org/repo#2")
    finally:
        c.close()
    task_cli.main(
        [
            "--root",
            str(tmp_path),
            "deps",
            "--db-path",
            str(db_path),
            "add",
            "gh:org/repo#1",
            "gh:org/repo#2",
        ]
    )
    capsys.readouterr()  # flush
    rc = task_cli.main(
        [
            "--root",
            str(tmp_path),
            "deps",
            "--db-path",
            str(db_path),
            "remove",
            "gh:org/repo#1",
            "gh:org/repo#2",
        ]
    )
    assert rc == 0
    # Verify the row is gone.
    c = sqlite3.connect(str(db_path))
    try:
        rows = c.execute(
            "SELECT COUNT(*) FROM item_dependencies"
        ).fetchone()
        assert rows[0] == 0
    finally:
        c.close()


def test_cli_deps_add_rejects_cycle(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        _seed_item(c, "gh:org/repo#1")
        _seed_item(c, "gh:org/repo#2")
    finally:
        c.close()
    task_cli.main(
        [
            "--root",
            str(tmp_path),
            "deps",
            "--db-path",
            str(db_path),
            "add",
            "gh:org/repo#1",
            "gh:org/repo#2",
        ]
    )
    capsys.readouterr()
    rc = task_cli.main(
        [
            "--root",
            str(tmp_path),
            "deps",
            "--db-path",
            str(db_path),
            "add",
            "gh:org/repo#2",
            "gh:org/repo#1",
        ]
    )
    assert rc == 7
    err = capsys.readouterr().err
    assert "cycle" in err.lower()


# ---------------------------------------------------------------------------
# Dispatcher integration — the acceptance test from issue #833


def _dispatcher_run_once(
    *,
    state_path: pathlib.Path,
    db_path: pathlib.Path,
    issues: list[dict],
    log_lines: list[str],
):
    """Invoke the dispatcher once against the test DB.

    Returns ``(exit_code, report)``. All transport callables are
    stubs so no network traffic is attempted; spawn/verify/cleanup
    are disabled with ``enable_*=False``.
    """
    from alice_forge import dispatcher as _sm
    from alice_forge.dispatcher.dependency_gate import build_default_check

    gate = build_default_check(db_path=db_path, log=log_lines.append)

    return _sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        enable_rebase=False,
        list_issues=lambda _repo: issues,
        list_stale_closed=lambda _repo: [],
        list_open_done=lambda _repo: [],
        post_comment=lambda *a, **kw: None,
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda *a, **kw: None,
        find_open_linked_prs=None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        list_comments=lambda *a, **kw: [],
        dependency_gate=gate,
        dry_run=True,
        log=log_lines.append,
    )


def _make_selected_issue(number: int) -> dict:
    """Minimal issue dict shaped like gh CLI output.

    The dispatcher inspects ``number``, ``labels``, and
    ``createdAt``/``updatedAt``; everything else is unused for this
    test's code path.
    """
    return {
        "number": number,
        "author": {"login": "jcronq"},
        "labels": [
            {"name": "sm:selected"},
            {"name": "art:code"},
        ],
        "title": f"item-{number}",
        "body": "",
        "createdAt": "2026-11-07T00:00:00Z",
        "updatedAt": "2026-11-07T00:00:00Z",
    }


def test_dispatcher_skips_gated_selected_item(tmp_path: pathlib.Path) -> None:
    """Issue #833 acceptance test — gated item stays, prereq-done unblocks.

    Setup:
      * item-A = gh:jcronq/alice#1000 at sm:selected
      * item-B = gh:jcronq/alice#1001 at sm:building
      * item-A ``blocks`` depends on item-B

    First cadence: dispatcher sees issue #1000 at sm:selected but
    the gate returns a log line → the issue is skipped (no hello,
    no spawn, no transition). Log line matches the #833 format.

    After transitioning item-B to sm:done, a second cadence picks
    item-A up — the gate returns None. The dispatcher's regular
    sm:selected handler runs (and, with spawn disabled in this
    test, posts the hello comment without transitioning).
    """
    db_path = tmp_path / "alice-state.db"
    state_path = tmp_path / "sm-state.json"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        _seed_item(c, gh_issue_id("jcronq/alice", 1000), state="sm:selected")
        _seed_item(c, gh_issue_id("jcronq/alice", 1001), state="sm:building")
        add_dependency(
            c,
            gh_issue_id("jcronq/alice", 1000),
            gh_issue_id("jcronq/alice", 1001),
            kind="blocks",
        )
    finally:
        c.close()

    log_lines: list[str] = []
    exit_code, _report = _dispatcher_run_once(
        state_path=state_path,
        db_path=db_path,
        issues=[_make_selected_issue(1000)],
        log_lines=log_lines,
    )
    assert exit_code == 0
    # The gate log line went through. Exact format: "item-X gated by item-Y".
    gated_lines = [
        line
        for line in log_lines
        if "gated by" in line and "gh:jcronq/alice#1000" in line
    ]
    assert gated_lines, (
        f"expected a 'gated by' log line; saw: {log_lines!r}"
    )
    assert "gh:jcronq/alice#1001" in gated_lines[0]
    assert "sm:building" in gated_lines[0]

    # Second cadence: flip item-B to sm:done; item-A's gate clears.
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(
            """
            UPDATE state_machine SET state = 'sm:done'
            WHERE item_id = ?
            """,
            (gh_issue_id("jcronq/alice", 1001),),
        )
        c.commit()
    finally:
        c.close()

    log_lines_2: list[str] = []
    exit_code, _report = _dispatcher_run_once(
        state_path=state_path,
        db_path=db_path,
        issues=[_make_selected_issue(1000)],
        log_lines=log_lines_2,
    )
    assert exit_code == 0
    # No "gated by" line this time.
    assert not any(
        "gated by" in line and "gh:jcronq/alice#1000" in line
        for line in log_lines_2
    )


def test_dispatcher_runs_when_gate_disabled(tmp_path: pathlib.Path) -> None:
    """Passing ``enable_dependency_gate=False`` restores pre-#833 behaviour.

    Even with a blocking row seeded, the dispatcher proceeds as if
    the gate didn't exist. This guards callers (tests, legacy
    operators) who want to opt out explicitly.
    """
    from alice_forge import dispatcher as _sm

    db_path = tmp_path / "alice-state.db"
    state_path = tmp_path / "sm-state.json"
    c = sqlite3.connect(str(db_path))
    try:
        apply_schema(c)
        _seed_item(c, gh_issue_id("jcronq/alice", 2000), state="sm:selected")
        _seed_item(c, gh_issue_id("jcronq/alice", 2001), state="sm:building")
        add_dependency(
            c,
            gh_issue_id("jcronq/alice", 2000),
            gh_issue_id("jcronq/alice", 2001),
            kind="blocks",
        )
    finally:
        c.close()

    log_lines: list[str] = []
    exit_code, _report = _sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        enable_rebase=False,
        enable_dependency_gate=False,  # the opt-out
        list_issues=lambda _repo: [_make_selected_issue(2000)],
        list_stale_closed=lambda _repo: [],
        list_open_done=lambda _repo: [],
        post_comment=lambda *a, **kw: None,
        edit_labels=lambda *a, **kw: None,
        close_issue=lambda *a, **kw: None,
        find_linked_pr=lambda *a, **kw: None,
        find_open_linked_prs=None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        list_comments=lambda *a, **kw: [],
        dry_run=True,
        log=log_lines.append,
    )
    assert exit_code == 0
    # Gate didn't fire: no "gated by" line.
    assert not any("gated by" in line for line in log_lines)


# ---------------------------------------------------------------------------
# Detail helper used by the gate's log formatting


def test_non_terminal_blocker_detail_returns_only_blocking_prereqs(
    conn: sqlite3.Connection,
) -> None:
    for n, state in (
        (1, "sm:selected"),
        (2, "sm:done"),
        (3, "sm:building"),
    ):
        _seed_item(conn, f"gh:org/repo#{n}", state=state)
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#2")
    add_dependency(conn, "gh:org/repo#1", "gh:org/repo#3")
    detail = non_terminal_blocker_detail(conn, "gh:org/repo#1")
    # Only #3 is still blocking (#2 is terminal).
    assert detail == [("gh:org/repo#3", "sm:building")]
