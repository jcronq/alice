"""Integration tests — dispatcher main loop + shadow/cutover wiring (#826).

Covers the three must-prove scenarios from #826's acceptance table:

* **Shadow mode divergence triggers a note.** Default env (shadow on)
  + a stubbed ``gh_list_sm_issues`` returning one id + an empty DB.
  ``sm.run()`` completes, the GH list wins (one issue polled), and a
  ``dispatcher-shadow-*.json`` lands in the notes dir with the
  gh-only number.
* **Cutover mode uses DB-only.** ``ALICE_DISPATCHER_SHADOW=0`` + the
  GH stub returning *other* numbers + seeded DB rows. ``sm.run()``
  polls the DB-authored numbers only.
* **ALICE_DISPATCHER_DRY still fully gates gh.** Verified by the
  autouse fixture in :mod:`tests.test_alice_forge_dispatcher`;
  re-asserted here against the new wrapper so a regression in the
  shim can't silently re-enable gh calls under the dry gate.

Fixtures intentionally override only the gh listing helpers — every
other gh ``_run_gh`` call is dry-gated by the autouse env fixture
below.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Iterator

import pytest

from alice_forge import dispatcher as sm
from alice_forge.dispatcher import db_source
from alice_state_db import apply_schema


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture(autouse=True)
def _dry_gh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every unmocked gh helper quiet across this test file.

    Mirrors :mod:`tests.test_alice_forge_dispatcher`'s autouse.
    """
    monkeypatch.setenv("ALICE_DISPATCHER_DRY", "1")
    yield


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "sm-dispatcher-state.json"


@pytest.fixture
def notes_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect the shadow-note sink to a tmp dir for the whole test.

    Patches the private resolver so ``compare_shadow_results`` writes
    into ``tmp_path/notes/`` instead of the real alice-mind tree.
    """
    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setattr(db_source, "_default_shadow_notes_dir", lambda: d)
    return d


@pytest.fixture
def db_path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Build a fresh state DB and point both reader helpers at it."""
    p = tmp_path / "alice-state.db"
    c = sqlite3.connect(str(p))
    try:
        apply_schema(c)
    finally:
        c.close()
    # Redirect the connect() default used by our DB-source helpers.
    monkeypatch.setattr(db_source, "DEFAULT_DB_PATH", p)
    # alice_state_db.read_api.DEFAULT_DB_PATH is the one connect()
    # actually resolves against; patch it too.
    from alice_state_db import read_api as _read_api
    monkeypatch.setattr(_read_api, "DEFAULT_DB_PATH", p)
    return p


def _seed(
    db_path: pathlib.Path,
    item_id: str,
    *,
    state: str,
    title: str = "",
    entered_at: str = "2026-11-02T00:00:00+00:00",
) -> None:
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(
            "INSERT OR REPLACE INTO items (item_id, item_type, title) "
            "VALUES (?, 'gh:issue', ?)",
            (item_id, title),
        )
        c.execute(
            "INSERT OR REPLACE INTO state_machine (item_id, state, entered_at) "
            "VALUES (?, ?, ?)",
            (item_id, state, entered_at),
        )
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Scenario 1 — shadow mode: GH wins, divergence note drops


def test_shadow_mode_default_logs_divergence_note(
    state_path: pathlib.Path,
    notes_dir: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALICE_DISPATCHER_SHADOW", raising=False)

    # GH says [42]; DB is empty — divergence on #42 (gh-only).
    gh_payload = [
        {
            "number": 42,
            "title": "shadow case",
            "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
            "author": {"login": "jcronq"},
            "createdAt": "2026-11-02T00:00:00Z",
        }
    ]
    monkeypatch.setattr(sm, "gh_list_sm_issues", lambda _r: gh_payload)
    monkeypatch.setattr(
        sm, "gh_list_stale_closed_sm_issues", lambda _r: []
    )
    monkeypatch.setattr(sm, "gh_list_open_done_sm_issues", lambda _r: [])

    logs: list[str] = []
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        post_comment=lambda *a, **k: None,
        edit_labels=lambda *a, **k: None,
        close_issue=lambda *a, **k: None,
        find_linked_pr=lambda *a, **k: None,
        find_open_linked_prs=lambda *a, **k: [],
        pr_merge_status=lambda *a, **k: None,
        master_ci_status=lambda *a, **k: None,
        enable_dependency_gate=False,
        log=logs.append,
    )

    assert exit_code == 0
    # GH was authoritative → the dispatcher polled the GH-only issue.
    assert report.polled == 1
    # Shadow divergence note was dropped
    notes = list(notes_dir.glob("dispatcher-shadow-*.json"))
    assert notes, (
        "expected a dispatcher-shadow note in "
        f"{notes_dir} — got {sorted(p.name for p in notes_dir.iterdir())}"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — cutover mode: DB wins


def test_cutover_mode_uses_db_result_not_gh(
    state_path: pathlib.Path,
    notes_dir: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALICE_DISPATCHER_SHADOW", "0")

    # Seed DB with #77 at sm:selected.
    _seed(
        db_path,
        "gh:jcronq/alice#77",
        state="sm:selected",
        title="db-only",
    )
    # GH says nothing — if the dispatcher ever reads from GH in
    # cutover mode it'd see polled == 0.
    monkeypatch.setattr(sm, "gh_list_sm_issues", lambda _r: [])
    monkeypatch.setattr(sm, "gh_list_stale_closed_sm_issues", lambda _r: [])
    monkeypatch.setattr(sm, "gh_list_open_done_sm_issues", lambda _r: [])

    logs: list[str] = []
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        post_comment=lambda *a, **k: None,
        edit_labels=lambda *a, **k: None,
        close_issue=lambda *a, **k: None,
        find_linked_pr=lambda *a, **k: None,
        find_open_linked_prs=lambda *a, **k: [],
        pr_merge_status=lambda *a, **k: None,
        master_ci_status=lambda *a, **k: None,
        enable_dependency_gate=False,
        log=logs.append,
    )

    assert exit_code == 0
    # DB authoritative → one issue surfaced, even though GH returned [].
    assert report.polled == 1


# ---------------------------------------------------------------------------
# Scenario 3 — cutover mode with DB broken falls back to GH (fail-open)


def test_cutover_mode_db_broken_falls_back_to_gh(
    state_path: pathlib.Path,
    notes_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALICE_DISPATCHER_SHADOW", "0")

    # Point the DB readers at a path that can't be opened (parent of
    # parent is a FILE, not a dir).
    bogus_parent = tmp_path / "broken-parent"
    bogus_parent.write_text("not a dir", encoding="utf-8")
    bogus_db = bogus_parent / "subdir" / "alice-state.db"
    monkeypatch.setattr(db_source, "DEFAULT_DB_PATH", bogus_db)
    from alice_state_db import read_api as _read_api
    monkeypatch.setattr(_read_api, "DEFAULT_DB_PATH", bogus_db)

    # GH provides the fallback.
    gh_payload = [
        {
            "number": 88,
            "title": "fail-open fallback",
            "labels": [{"name": "sm:selected"}, {"name": "art:code"}],
            "author": {"login": "jcronq"},
            "createdAt": "2026-11-02T00:00:00Z",
        }
    ]
    monkeypatch.setattr(sm, "gh_list_sm_issues", lambda _r: gh_payload)
    monkeypatch.setattr(sm, "gh_list_stale_closed_sm_issues", lambda _r: [])
    monkeypatch.setattr(sm, "gh_list_open_done_sm_issues", lambda _r: [])

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        post_comment=lambda *a, **k: None,
        edit_labels=lambda *a, **k: None,
        close_issue=lambda *a, **k: None,
        find_linked_pr=lambda *a, **k: None,
        find_open_linked_prs=lambda *a, **k: [],
        pr_merge_status=lambda *a, **k: None,
        master_ci_status=lambda *a, **k: None,
        enable_dependency_gate=False,
        log=lambda s: None,
    )

    assert exit_code == 0
    # GH fell back in cutover → one issue polled.
    assert report.polled == 1


# ---------------------------------------------------------------------------
# Scenario 4 — dry gate still trumps the shim


def test_dry_gate_still_prevents_gh_calls_through_wrapper(
    state_path: pathlib.Path,
    notes_dir: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALICE_DISPATCHER_DRY=1 → the gh fallback _run_gh returns ''.

    Even in cutover mode, if the DB is empty AND the gh call would
    produce ``""`` (dry gate), the dispatcher sees zero issues and
    the run is a no-op. This is the safety contract from #803
    carried across the new wrapper.
    """
    monkeypatch.setenv("ALICE_DISPATCHER_SHADOW", "0")
    # Leave ALICE_DISPATCHER_DRY=1 from autouse → gh helpers short-
    # circuit to empty. DB also empty (no seeds).

    # We do NOT patch sm.gh_list_sm_issues — so the real helper runs
    # against the dry gate and returns [].

    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        post_comment=lambda *a, **k: None,
        edit_labels=lambda *a, **k: None,
        close_issue=lambda *a, **k: None,
        find_linked_pr=lambda *a, **k: None,
        find_open_linked_prs=lambda *a, **k: [],
        pr_merge_status=lambda *a, **k: None,
        master_ci_status=lambda *a, **k: None,
        enable_dependency_gate=False,
        log=lambda s: None,
    )

    assert exit_code == 0
    assert report.polled == 0
