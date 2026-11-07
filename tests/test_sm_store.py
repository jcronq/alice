"""Tests for ``alice_forge.sm.store`` (issue #824, PR-1 of #823).

Five scenarios:

1. Legal transition writes a transition row + updates state_machine,
   and a simulated step-5 UPDATE failure rolls back the step-4 INSERT.
2. Untrusted actor raises before any SQL fires.
3. Illegal edge (e.g. DONE -> BUILDING) raises and writes nothing.
4. Unknown item (no state_machine row) raises ``ItemNotFoundError``.
5. ``amend()`` duplicate ``(item_id, source, source_ref)`` propagates
   ``sqlite3.IntegrityError`` from the ``uniq_amendments_source_ref``
   partial index -- it is NOT swallowed like the seed replay path.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alice_forge.sm import store
from alice_forge.sm.states import SMState
from alice_state_db.schema import apply_schema


ITEM = "gh:jcronq/alice#824"
ITEM2 = "gh:jcronq/alice#8242"


class _FailOnStateUpdate(sqlite3.Connection):
    """Raise on the state_machine UPDATE so we can observe rollback."""

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        if sql.strip().upper().startswith("UPDATE STATE_MACHINE"):
            raise sqlite3.OperationalError("simulated IO failure")
        return super().execute(sql, *args, **kwargs)


def _open(db_path: pathlib.Path, *, factory=None) -> sqlite3.Connection:
    """Open a connection against the seeded DB with the harness defaults."""
    if factory is None:
        conn = sqlite3.connect(str(db_path))
    else:
        conn = sqlite3.connect(str(db_path), factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _seed_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    state: str,
) -> None:
    """Write the items row + the state_machine row the tests need."""
    conn.execute(
        """
        INSERT INTO items (item_id, item_type, title, created_at, updated_at)
        VALUES (?, 'gh:issue', ?, ?, ?)
        """,
        (item_id, f"title for {item_id}", "2026-11-07T00:00:00+00:00",
         "2026-11-07T00:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO state_machine (item_id, state, entered_at)
        VALUES (?, ?, ?)
        """,
        (item_id, state, "2026-11-07T00:00:00+00:00"),
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Schema-applied DB file with no content."""
    db = tmp_path / "alice-state.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return db


def test_transition_legal_and_updates_state_machine(
    db_path: pathlib.Path,
) -> None:
    conn = _open(db_path)
    try:
        _seed_item(conn, ITEM, state=SMState.DRAFT.label)

        tid = store.transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.NEEDS_STUDY,
            actor="dispatcher",
        )
    finally:
        conn.close()

    # Verify from a fresh connection -- commit must have landed.
    verify = _open(db_path)
    try:
        rows = verify.execute(
            "SELECT transition_id, from_state, to_state, actor, source "
            "FROM transitions WHERE item_id = ?",
            (ITEM,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["transition_id"] == tid
        assert rows[0]["from_state"] == SMState.DRAFT.label
        assert rows[0]["to_state"] == SMState.NEEDS_STUDY.label
        assert rows[0]["actor"] == "dispatcher"
        assert rows[0]["source"] == "sm-store"

        sm = verify.execute(
            "SELECT state, last_transition_id FROM state_machine "
            "WHERE item_id = ?",
            (ITEM,),
        ).fetchone()
        assert sm["state"] == SMState.NEEDS_STUDY.label
        assert sm["last_transition_id"] == tid
    finally:
        verify.close()


def test_transition_rolls_back_on_step_5_failure(
    db_path: pathlib.Path,
) -> None:
    # Seed via a plain connection so the fixture state_machine row is real.
    seed = _open(db_path)
    try:
        _seed_item(seed, ITEM2, state=SMState.DRAFT.label)
    finally:
        seed.close()

    # Now open with the hostile factory and prove the step-4 insert
    # unwinds when the step-5 UPDATE blows up.
    conn = _open(db_path, factory=_FailOnStateUpdate)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.transition(
                conn=conn,
                item_id=ITEM2,
                to=SMState.NEEDS_STUDY,
                actor="dispatcher",
            )
    finally:
        conn.close()

    # Fresh connection — must observe zero transitions rows.
    verify = _open(db_path)
    try:
        count = verify.execute(
            "SELECT COUNT(*) AS n FROM transitions WHERE item_id = ?",
            (ITEM2,),
        ).fetchone()["n"]
        assert count == 0

        state = verify.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (ITEM2,),
        ).fetchone()["state"]
        assert state == SMState.DRAFT.label
    finally:
        verify.close()


def test_transition_rejects_untrusted_actor(db_path: pathlib.Path) -> None:
    conn = _open(db_path)
    try:
        _seed_item(conn, ITEM, state=SMState.DRAFT.label)

        with pytest.raises(store.ActorNotTrustedError):
            store.transition(
                conn=conn,
                item_id=ITEM,
                to=SMState.NEEDS_STUDY,
                actor="random-stranger",
            )

        # No row written, state unchanged.
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM transitions WHERE item_id = ?",
            (ITEM,),
        ).fetchone()["n"]
        assert count == 0
        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (ITEM,),
        ).fetchone()["state"]
        assert state == SMState.DRAFT.label
    finally:
        conn.close()


def test_transition_rejects_illegal_edge(db_path: pathlib.Path) -> None:
    conn = _open(db_path)
    try:
        _seed_item(conn, ITEM, state=SMState.DONE.label)

        with pytest.raises(store.IllegalTransitionError):
            store.transition(
                conn=conn,
                item_id=ITEM,
                to=SMState.BUILDING,
                actor="dispatcher",
            )

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM transitions WHERE item_id = ?",
            (ITEM,),
        ).fetchone()["n"]
        assert count == 0
        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (ITEM,),
        ).fetchone()["state"]
        assert state == SMState.DONE.label
    finally:
        conn.close()


def test_transition_unknown_item(db_path: pathlib.Path) -> None:
    conn = _open(db_path)
    try:
        with pytest.raises(store.ItemNotFoundError):
            store.transition(
                conn=conn,
                item_id="gh:jcronq/alice#999999",
                to=SMState.NEEDS_STUDY,
                actor="dispatcher",
            )
    finally:
        conn.close()


def test_amend_source_ref_uniqueness(db_path: pathlib.Path) -> None:
    conn = _open(db_path)
    try:
        _seed_item(conn, ITEM, state=SMState.DRAFT.label)

        first = store.amend(
            conn=conn,
            item_id=ITEM,
            body="first amendment",
            author="jason",
            source_ref="dup-1",
        )
        assert first > 0

        with pytest.raises(sqlite3.IntegrityError):
            store.amend(
                conn=conn,
                item_id=ITEM,
                body="duplicate amendment",
                author="jason",
                source_ref="dup-1",
            )
    finally:
        conn.close()
