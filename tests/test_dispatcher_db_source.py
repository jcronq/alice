"""Tests for :mod:`alice_forge.dispatcher.db_source` (issue #826).

Three concerns, in order of fragility:

1. **DB-reader result shape.** Rows come back as GH-shaped dicts so
   downstream handlers see a type-compatible payload.
2. **Shadow mode divergence detection + logging.** When ``gh`` and
   ``db`` identity sets diverge, a tagged JSON note lands in the
   configured notes dir AND a stdout line names the gh-only /
   db-only numbers.
3. **Cutover-mode DB-only path.** With ``ALICE_DISPATCHER_SHADOW=0``
   the DB result is authoritative; a DB error falls back to the GH
   callable (fail-open).

Fixtures seed the ``items`` + ``state_machine`` tables directly and
skip the full ``sm_seed`` derive pass — only the two tables are
under test here and the dispatcher's read query only touches them
plus the ``items.author`` / ``items.summary`` fields.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Iterator

import pytest

from alice_forge.dispatcher.db_source import (
    _issue_numbers,
    _parse_gh_item_id,
    compare_shadow_results,
    db_list_open_done_sm_issues,
    db_list_sm_issues,
    db_list_stale_closed_sm_issues,
    shadow_enabled,
    shadow_resolve,
)
from alice_state_db import apply_schema


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a path to a freshly-initialised state DB."""
    p = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(p))
    try:
        apply_schema(c)
    finally:
        c.close()
    return p


def _seed(
    db_path: pathlib.Path,
    item_id: str,
    *,
    state: str,
    title: str = "",
    author: str = "",
    created_at: str = "2026-11-01T00:00:00+00:00",
    entered_at: str = "2026-11-02T00:00:00+00:00",
    summary: str = "",
) -> None:
    """Insert one items + state_machine row pair."""
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(
            """
            INSERT OR REPLACE INTO items
                (item_id, item_type, title, created_at, author, summary)
            VALUES (?, 'gh:issue', ?, ?, ?, ?)
            """,
            (item_id, title, created_at, author, summary),
        )
        c.execute(
            """
            INSERT OR REPLACE INTO state_machine
                (item_id, state, entered_at)
            VALUES (?, ?, ?)
            """,
            (item_id, state, entered_at),
        )
        c.commit()
    finally:
        c.close()


@pytest.fixture
def no_shadow_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unset ``ALICE_DISPATCHER_SHADOW`` so default-on applies."""
    monkeypatch.delenv("ALICE_DISPATCHER_SHADOW", raising=False)
    yield


# ---------------------------------------------------------------------------
# DB-reader result shape (concern 1)


def test_parse_gh_item_id_happy_path() -> None:
    assert _parse_gh_item_id("gh:jcronq/alice#826") == ("jcronq/alice", 826)


def test_parse_gh_item_id_rejects_pr_items() -> None:
    # PR items (``gh:pr:<repo>#<N>``) must NOT parse — dispatcher
    # listings are issue-scoped.
    assert _parse_gh_item_id("gh:pr:jcronq/alice#826") is None


def test_parse_gh_item_id_rejects_local_tasks() -> None:
    assert _parse_gh_item_id("local:task-0826") is None


def test_db_list_sm_issues_returns_gh_shaped_dicts(db_path: pathlib.Path) -> None:
    _seed(
        db_path,
        "gh:jcronq/alice#1",
        state="sm:selected",
        title="a",
        author="jcronq",
        summary="first 500 chars",
    )
    _seed(
        db_path,
        "gh:jcronq/alice#2",
        state="sm:building",
        title="b",
        author="jcronq",
    )
    rows = db_list_sm_issues("jcronq/alice", db_path=db_path)
    numbers = {r["number"] for r in rows}
    assert numbers == {1, 2}
    # Shape checks on #1 — the handler dispatch relies on this.
    r1 = next(r for r in rows if r["number"] == 1)
    assert r1["title"] == "a"
    assert r1["labels"] == [{"name": "sm:selected"}]
    assert r1["author"] == {"login": "jcronq"}
    assert r1["body"] == "first 500 chars"
    assert r1["_source"] == "db"


def test_db_list_sm_issues_filters_by_repo(db_path: pathlib.Path) -> None:
    _seed(db_path, "gh:jcronq/alice#10", state="sm:selected", title="alice")
    _seed(db_path, "gh:jcronq/cozyhem-engine#20", state="sm:selected", title="cz")
    rows = db_list_sm_issues("jcronq/alice", db_path=db_path)
    assert {r["number"] for r in rows} == {10}


def test_db_list_sm_issues_excludes_terminal(db_path: pathlib.Path) -> None:
    _seed(db_path, "gh:jcronq/alice#30", state="sm:done", title="t")
    _seed(db_path, "gh:jcronq/alice#31", state="sm:rejected", title="r")
    rows = db_list_sm_issues("jcronq/alice", db_path=db_path)
    assert rows == []


def test_db_list_sm_issues_orders_by_entered_at(db_path: pathlib.Path) -> None:
    _seed(
        db_path,
        "gh:jcronq/alice#40",
        state="sm:selected",
        entered_at="2026-11-07T00:00:00+00:00",
    )
    _seed(
        db_path,
        "gh:jcronq/alice#41",
        state="sm:selected",
        entered_at="2026-11-05T00:00:00+00:00",
    )
    rows = db_list_sm_issues("jcronq/alice", db_path=db_path)
    assert [r["number"] for r in rows] == [41, 40]


def test_db_list_open_done_returns_done(db_path: pathlib.Path) -> None:
    _seed(db_path, "gh:jcronq/alice#50", state="sm:done", title="done")
    _seed(db_path, "gh:jcronq/alice#51", state="sm:selected", title="sel")
    rows = db_list_open_done_sm_issues("jcronq/alice", db_path=db_path)
    assert {r["number"] for r in rows} == {50}


def test_db_list_stale_closed_is_empty_by_design(db_path: pathlib.Path) -> None:
    _seed(db_path, "gh:jcronq/alice#60", state="sm:selected", title="s")
    # Even with data in the DB, the stale-closed helper returns [] —
    # the DB doesn't track GH open/closed yet. Shadow mode uses this
    # to log the expected divergence.
    assert db_list_stale_closed_sm_issues("jcronq/alice", db_path=db_path) == []


def test_db_list_sm_issues_raises_on_broken_path(
    tmp_path: pathlib.Path,
) -> None:
    # Pre-create a file where a directory should be to force OSError
    # in connect()'s path.parent.mkdir call.
    bogus = tmp_path / "cant" / "make" / "this.db"
    bogus.parent.parent.mkdir(parents=True, exist_ok=True)
    bogus.parent.write_text("not a dir", encoding="utf-8")
    # The helper must RAISE, not swallow, so shadow_resolve can
    # distinguish "DB broken" (fail-open to GH in cutover) from
    # "DB OK, no rows" (authoritative empty).
    with pytest.raises((sqlite3.Error, OSError)):
        db_list_sm_issues("jcronq/alice", db_path=bogus)


# ---------------------------------------------------------------------------
# Shadow mode divergence detection + logging (concern 2)


def test_compare_shadow_results_logs_when_identity_sets_diverge(
    tmp_path: pathlib.Path,
) -> None:
    logs: list[str] = []
    gh = [{"number": 1}, {"number": 2}]
    db = [{"number": 2}, {"number": 3}]
    divergent = compare_shadow_results(
        gh_result=gh,
        db_result=db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
    )
    assert divergent is True
    # stdout log line names the diff
    combined = "\n".join(logs)
    assert "shadow-diff" in combined
    assert "gh_only=[1]" in combined
    assert "db_only=[3]" in combined
    # Exactly one JSON note dropped
    notes = list(tmp_path.glob("dispatcher-shadow-*.json"))
    assert len(notes) == 1
    payload = json.loads(notes[0].read_text())
    assert payload["tag"] == "dispatcher-shadow"
    assert payload["source"] == "gh_list_sm_issues"
    assert payload["gh_only_numbers"] == [1]
    assert payload["db_only_numbers"] == [3]


def test_compare_shadow_results_silent_when_identity_sets_match(
    tmp_path: pathlib.Path,
) -> None:
    logs: list[str] = []
    gh = [{"number": 1}, {"number": 2}]
    db = [{"number": 2}, {"number": 1}]  # order-insensitive
    divergent = compare_shadow_results(
        gh_result=gh,
        db_result=db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
    )
    assert divergent is False
    assert logs == []
    assert list(tmp_path.glob("dispatcher-shadow-*.json")) == []


def test_issue_numbers_drops_malformed_rows() -> None:
    assert _issue_numbers(
        [{"number": 1}, {"number": "two"}, {"number": None}, {}]
    ) == {1}


# ---------------------------------------------------------------------------
# shadow_resolve — the single entry point (concerns 2 + 3)


def test_shadow_resolve_returns_gh_in_shadow_mode(
    tmp_path: pathlib.Path,
) -> None:
    """Shadow mode: GH is authoritative, even when DB disagrees."""
    logs: list[str] = []
    gh = [{"number": 1}, {"number": 2}]
    db = [{"number": 3}]
    result = shadow_resolve(
        gh_callable=lambda repo: gh,
        db_callable=lambda repo: db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
        shadow=True,
    )
    assert result == gh
    # Divergence note landed
    assert list(tmp_path.glob("dispatcher-shadow-*.json"))


def test_shadow_resolve_returns_db_in_cutover_mode(
    tmp_path: pathlib.Path,
) -> None:
    """Cutover mode: DB is authoritative."""
    logs: list[str] = []
    gh = [{"number": 1}]
    db = [{"number": 2}]
    result = shadow_resolve(
        gh_callable=lambda repo: gh,
        db_callable=lambda repo: db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
        shadow=False,
    )
    # DB wins — no shadow note either (we're not comparing)
    assert result == db


def test_shadow_resolve_cutover_fails_open_on_db_error(
    tmp_path: pathlib.Path,
) -> None:
    """Cutover mode: DB callable raising falls back to GH (fail-open)."""
    logs: list[str] = []
    gh = [{"number": 42}]

    def _db(_r: str) -> list[dict]:
        raise sqlite3.OperationalError("db is locked")

    result = shadow_resolve(
        gh_callable=lambda repo: gh,
        db_callable=_db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
        shadow=False,
    )
    assert result == gh
    assert any("fail-open" in m for m in logs)


def test_shadow_resolve_shadow_swallows_db_error(
    tmp_path: pathlib.Path,
) -> None:
    """Shadow mode: a broken DB never aborts the cadence."""
    gh = [{"number": 1}]

    def _db(_r: str) -> list[dict]:
        raise sqlite3.OperationalError("db is locked")

    logs: list[str] = []
    result = shadow_resolve(
        gh_callable=lambda repo: gh,
        db_callable=_db,
        source_name="gh_list_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=logs.append,
        shadow=True,
    )
    # GH is still authoritative; DB error is swallowed + logged as
    # "empty DB" so the compare proceeds without raising.
    assert result == gh
    assert any("treating as empty DB" in m for m in logs)


def test_shadow_resolve_stale_closed_cutover_falls_back_to_gh(
    tmp_path: pathlib.Path,
) -> None:
    """The stale_closed DB helper returns []; cutover must fall back to GH."""
    gh = [{"number": 7}]
    result = shadow_resolve(
        gh_callable=lambda repo: gh,
        db_callable=lambda repo: [],  # stale_closed helper always returns []
        source_name="gh_list_stale_closed_sm_issues",
        repo="jcronq/alice",
        notes_dir=tmp_path,
        log=lambda s: None,
        shadow=False,
    )
    assert result == gh


# ---------------------------------------------------------------------------
# Env-gate default (concern 3 — the rollout contract)


def test_shadow_enabled_default_on(no_shadow_env: None) -> None:
    """``ALICE_DISPATCHER_SHADOW`` unset → shadow mode (safe rollout)."""
    assert shadow_enabled() is True


def test_shadow_enabled_truthy_values_stay_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("1", "true", "yes", "on", "anything-else"):
        monkeypatch.setenv("ALICE_DISPATCHER_SHADOW", val)
        assert shadow_enabled() is True


def test_shadow_enabled_falsy_values_cut_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("ALICE_DISPATCHER_SHADOW", val)
        assert shadow_enabled() is False
