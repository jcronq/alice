"""Tests for :mod:`alice_forge.sm.store` — SM write choke point (#824).

Covers the acceptance spec in the issue body:

1. ``transition`` with a legal edge writes exactly one
   ``transitions`` row and updates ``state_machine`` atomically;
   a failure in the UPDATE rolls back the INSERT.
2. ``transition`` with an untrusted actor raises
   :class:`ActorNotTrustedError` and writes nothing.
3. ``transition`` with an illegal edge (``done → building``)
   raises :class:`IllegalTransitionError` and writes nothing.
4. ``transition`` with an unknown ``item_id`` raises
   :class:`ItemNotFoundError`.
5. ``amend`` writes one ``amendments`` row; ``source_ref``
   uniqueness is enforced — a second insert with the same
   ``(item_id, source, source_ref)`` raises.

Plus the extras this module adds on top of the spec:

* :data:`LEGAL_EDGES` is derived from :data:`TRANSITIONS` (not a
  hand-rolled list) — a representative edge sample is present.
* :func:`transition` replays idempotently when a ``source_ref``
  row already exists (returns the prior ``transition_id``,
  writes nothing new).
* :func:`amend` also actor-trust-gates on ``author``.
* :func:`record_dependency` is importable but raises
  :class:`NotImplementedError` until PR-10 lands the table.
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest

from alice_forge.sm.states import SMState
from alice_forge.sm.store import (
    ALLOWED_ACTORS,
    LEGAL_EDGES,
    ActorNotTrustedError,
    IllegalTransitionError,
    ItemNotFoundError,
    amend,
    record_dependency,
    transition,
)
from alice_state_db.schema import apply_schema


ITEM = "gh:jcronq/alice#824"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Schema-applied ``alice-state.db`` file, no content."""
    db = tmp_path / "alice-state.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return db


@pytest.fixture
def conn(db_path: pathlib.Path):
    """Fresh connection against the seeded DB. Caller is expected
    to NOT care about ``row_factory`` — the module reads positional
    columns.
    """
    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA foreign_keys = ON;")
    yield c
    c.close()


def _seed_item(
    conn: sqlite3.Connection,
    *,
    item_id: str = ITEM,
    state: SMState = SMState.SELECTED,
) -> None:
    """Insert a bare ``items`` row + ``state_machine`` row so
    :func:`transition` has something to mutate."""
    conn.execute(
        """
        INSERT INTO items (item_id, item_type, title, created_at, updated_at)
        VALUES (?, 'gh:issue', ?, ?, ?)
        """,
        (
            item_id,
            f"test item {item_id}",
            "2026-11-07T00:00:00+00:00",
            "2026-11-07T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO state_machine (item_id, state, entered_at)
        VALUES (?, ?, ?)
        """,
        (item_id, state.label, "2026-11-07T00:00:00+00:00"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# LEGAL_EDGES — derived, not hardcoded
# ---------------------------------------------------------------------------


class TestLegalEdges:
    """``LEGAL_EDGES`` is built once from :data:`TRANSITIONS`."""

    def test_includes_comment_verb_edge(self) -> None:
        """``draft → selected`` is a canonical comment-verb edge."""
        assert (SMState.DRAFT, SMState.SELECTED) in LEGAL_EDGES

    def test_includes_event_transition_edge(self) -> None:
        """``selected → designing`` is an EventTransition (via
        the ``spawn-dispatch-art-code`` trigger) — these count."""
        assert (SMState.SELECTED, SMState.DESIGNING) in LEGAL_EDGES

    def test_includes_self_loop(self) -> None:
        """SelfLoop entries contribute a ``(s, s)`` edge so
        explicit self-transitions for the ``[SM] continue`` audit
        trail are legal."""
        # DRAFT has Verbs.CONTINUE: SelfLoop() in TRANSITIONS.
        assert (SMState.DRAFT, SMState.DRAFT) in LEGAL_EDGES

    def test_excludes_illegal_edges(self) -> None:
        """Representative illegal edges stay out."""
        # Can't go from DONE back to BUILDING.
        assert (SMState.DONE, SMState.BUILDING) not in LEGAL_EDGES
        # Can't jump DRAFT straight to DONE.
        assert (SMState.DRAFT, SMState.DONE) not in LEGAL_EDGES


# ---------------------------------------------------------------------------
# transition() — happy path (spec case 1)
# ---------------------------------------------------------------------------


class TestTransitionHappyPath:
    """One row in ``transitions`` + updated ``state_machine`` row;
    atomic."""

    def test_writes_one_transition_row(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_item(conn, state=SMState.DRAFT)
        tid = transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.SELECTED,
            actor="speaking",
            reason="jason approved",
        )

        n = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        assert n == 1

        row = conn.execute(
            """
            SELECT transition_id, item_id, from_state, to_state,
                   actor, reason, source
              FROM transitions
            """
        ).fetchone()
        assert row[0] == tid
        assert row[1] == ITEM
        assert row[2] == SMState.DRAFT.label
        assert row[3] == SMState.SELECTED.label
        assert row[4] == "speaking"
        assert row[5] == "jason approved"
        assert row[6] == "sm-store"  # default source

    def test_updates_state_machine_atomically(
        self, conn: sqlite3.Connection
    ) -> None:
        """state_machine.state flips, entered_at refreshes, and
        last_transition_id points at the new row."""
        _seed_item(conn, state=SMState.DRAFT)
        tid = transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.SELECTED,
            actor="speaking",
        )
        row = conn.execute(
            """
            SELECT state, last_transition_id
              FROM state_machine
             WHERE item_id = ?
            """,
            (ITEM,),
        ).fetchone()
        assert row[0] == SMState.SELECTED.label
        assert row[1] == tid

    def test_writes_survive_fresh_connection(
        self, db_path: pathlib.Path
    ) -> None:
        """A second connection sees the transition — proves the
        ``with conn:`` block actually commits."""
        c1 = sqlite3.connect(str(db_path))
        try:
            _seed_item(c1, state=SMState.DRAFT)
            transition(
                conn=c1,
                item_id=ITEM,
                to=SMState.SELECTED,
                actor="dispatcher",
            )
        finally:
            c1.close()

        c2 = sqlite3.connect(str(db_path))
        try:
            n = c2.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
            assert n == 1
            state = c2.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                (ITEM,),
            ).fetchone()[0]
            assert state == SMState.SELECTED.label
        finally:
            c2.close()

    def test_now_override_lands_in_ts(
        self, conn: sqlite3.Connection
    ) -> None:
        """Injected ``now`` lands on both rows."""
        _seed_item(conn, state=SMState.DRAFT)
        fixed = datetime(2026, 11, 7, 12, 34, 56, tzinfo=timezone.utc)
        transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.SELECTED,
            actor="alice",
            now=fixed,
        )
        iso = "2026-11-07T12:34:56+00:00"
        t_ts = conn.execute("SELECT ts FROM transitions").fetchone()[0]
        sm_entered = conn.execute(
            "SELECT entered_at FROM state_machine"
        ).fetchone()[0]
        assert t_ts == iso
        assert sm_entered == iso


# ---------------------------------------------------------------------------
# transition() — atomic rollback (spec case 1 — rollback half)
# ---------------------------------------------------------------------------


class _FailOnStateUpdate(sqlite3.Connection):
    """Raise on the ``state_machine`` UPDATE so we can observe
    rollback of the preceding ``transitions`` INSERT.

    Overriding at the connection factory level means :func:`transition`
    hits a real failure inside its ``with conn:`` block — the harness
    doesn't have to monkey-patch anything the module imported.
    """

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        if sql.strip().upper().startswith("UPDATE STATE_MACHINE"):
            raise sqlite3.OperationalError("simulated IO failure")
        return super().execute(sql, *args, **kwargs)


class TestTransitionAtomicRollback:
    """A failure in the state_machine UPDATE rolls back the
    transitions INSERT so the two rows never drift."""

    def test_rollback_on_update_failure(
        self, db_path: pathlib.Path
    ) -> None:
        # Seed via a plain connection so the row exists.
        seed = sqlite3.connect(str(db_path))
        try:
            _seed_item(seed, state=SMState.DRAFT)
        finally:
            seed.close()

        # Hostile factory on the actual call.
        conn = sqlite3.connect(str(db_path), factory=_FailOnStateUpdate)
        try:
            with pytest.raises(sqlite3.OperationalError):
                transition(
                    conn=conn,
                    item_id=ITEM,
                    to=SMState.SELECTED,
                    actor="dispatcher",
                )
        finally:
            conn.close()

        # Fresh connection — must observe zero transitions rows and
        # the original state untouched.
        verify = sqlite3.connect(str(db_path))
        try:
            n = verify.execute(
                "SELECT COUNT(*) FROM transitions WHERE item_id = ?",
                (ITEM,),
            ).fetchone()[0]
            assert n == 0, f"expected rollback, got {n} transition row(s)"
            state = verify.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                (ITEM,),
            ).fetchone()[0]
            assert state == SMState.DRAFT.label
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# transition() — error paths (spec cases 2 / 3 / 4)
# ---------------------------------------------------------------------------


class TestTransitionActorTrust:
    """Spec case 2: unknown actor → ActorNotTrustedError, no write."""

    def test_untrusted_actor_rejected(self, conn: sqlite3.Connection) -> None:
        _seed_item(conn, state=SMState.DRAFT)
        with pytest.raises(ActorNotTrustedError) as exc:
            transition(
                conn=conn,
                item_id=ITEM,
                to=SMState.SELECTED,
                actor="random-stranger",
            )
        assert "random-stranger" in str(exc.value)

        n = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        assert n == 0
        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (ITEM,),
        ).fetchone()[0]
        assert state == SMState.DRAFT.label

    def test_every_allowed_actor_passes(
        self, conn: sqlite3.Connection
    ) -> None:
        """Smoke: each :data:`ALLOWED_ACTORS` entry can write."""
        for i, actor in enumerate(sorted(ALLOWED_ACTORS)):
            item_id = f"gh:jcronq/alice#actor-{i}"
            _seed_item(conn, item_id=item_id, state=SMState.DRAFT)
            transition(
                conn=conn,
                item_id=item_id,
                to=SMState.SELECTED,
                actor=actor,
            )

    def test_allowed_actors_covers_design_doc_set(self) -> None:
        """Guard against accidentally narrowing the set — the #823
        design lists exactly these seven principals."""
        expected = {
            "jason",
            "alice",
            "speaking",
            "thinking",
            "memory-worker",
            "dispatcher",
            "gh-watcher",
        }
        assert ALLOWED_ACTORS == expected


class TestTransitionIllegalEdge:
    """Spec case 3: ``done → building`` → IllegalTransitionError,
    no write."""

    def test_illegal_edge_rejected(self, conn: sqlite3.Connection) -> None:
        _seed_item(conn, state=SMState.DONE)
        with pytest.raises(IllegalTransitionError) as exc:
            transition(
                conn=conn,
                item_id=ITEM,
                to=SMState.BUILDING,
                actor="speaking",
            )
        assert exc.value.from_state is SMState.DONE
        assert exc.value.to_state is SMState.BUILDING
        assert "sm:done" in str(exc.value)
        assert "sm:building" in str(exc.value)

        n = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        assert n == 0

    def test_unrecognised_label_in_db_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        """A ``state_machine`` row holding a label outside the enum
        is a protocol violation — raise rather than silently
        continue."""
        conn.execute(
            """
            INSERT INTO items (item_id, item_type, title)
            VALUES (?, 'gh:issue', ?)
            """,
            ("gh:jcronq/alice#weird", "weird item"),
        )
        conn.execute(
            """
            INSERT INTO state_machine (item_id, state, entered_at)
            VALUES (?, ?, ?)
            """,
            (
                "gh:jcronq/alice#weird",
                "sm:fictional",
                "2026-11-07T00:00:00+00:00",
            ),
        )
        conn.commit()

        with pytest.raises(IllegalTransitionError) as exc:
            transition(
                conn=conn,
                item_id="gh:jcronq/alice#weird",
                to=SMState.SELECTED,
                actor="speaking",
            )
        assert "sm:fictional" in str(exc.value)
        assert exc.value.from_state is None
        assert exc.value.to_state is SMState.SELECTED


class TestTransitionItemNotFound:
    """Spec case 4: no state_machine row → ItemNotFoundError."""

    def test_missing_item_rejected(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ItemNotFoundError) as exc:
            transition(
                conn=conn,
                item_id="gh:jcronq/alice#nope",
                to=SMState.SELECTED,
                actor="speaking",
            )
        assert "gh:jcronq/alice#nope" in str(exc.value)


# ---------------------------------------------------------------------------
# transition() — source_ref idempotency replay
# ---------------------------------------------------------------------------


class TestTransitionIdempotencyReplay:
    """When ``source_ref`` is provided and already exists, the
    replay returns the prior ``transition_id`` without writing."""

    def test_replay_returns_existing_id(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_item(conn, state=SMState.DRAFT)
        tid1 = transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.SELECTED,
            actor="gh-watcher",
            source="gh-watcher",
            source_ref="comment-id-12345",
        )
        # Rewind state so the "same" transition is legal again
        # from the edge-check's perspective — the replay path must
        # short-circuit BEFORE the write.
        conn.execute(
            "UPDATE state_machine SET state = ? WHERE item_id = ?",
            (SMState.DRAFT.label, ITEM),
        )
        conn.commit()
        tid2 = transition(
            conn=conn,
            item_id=ITEM,
            to=SMState.SELECTED,
            actor="gh-watcher",
            source="gh-watcher",
            source_ref="comment-id-12345",
        )
        assert tid1 == tid2
        n = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
        assert n == 1


# ---------------------------------------------------------------------------
# amend() — spec case 5
# ---------------------------------------------------------------------------


class TestAmend:
    def test_writes_one_amendment_row(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_item(conn)
        aid = amend(
            conn=conn,
            item_id=ITEM,
            body="scope clarification: PR-1 only lands the module",
            author="thinking",
        )
        row = conn.execute(
            """
            SELECT amendment_id, item_id, author, body, source
              FROM amendments
            """
        ).fetchone()
        assert row[0] == aid
        assert row[1] == ITEM
        assert row[2] == "thinking"
        assert "scope clarification" in row[3]
        assert row[4] == "sm-store"

    def test_source_ref_uniqueness_enforced(
        self, conn: sqlite3.Connection
    ) -> None:
        """Second insert with the same ``(item_id, source,
        source_ref)`` raises IntegrityError."""
        _seed_item(conn)
        amend(
            conn=conn,
            item_id=ITEM,
            body="first",
            author="thinking",
            source="gh-watcher",
            source_ref="comment-id-9999",
        )
        with pytest.raises(sqlite3.IntegrityError):
            amend(
                conn=conn,
                item_id=ITEM,
                body="duplicate",
                author="thinking",
                source="gh-watcher",
                source_ref="comment-id-9999",
            )
        # Only the first row persisted.
        n = conn.execute("SELECT COUNT(*) FROM amendments").fetchone()[0]
        assert n == 1

    def test_untrusted_author_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_item(conn)
        with pytest.raises(ActorNotTrustedError):
            amend(
                conn=conn,
                item_id=ITEM,
                body="x",
                author="random-stranger",
            )
        n = conn.execute("SELECT COUNT(*) FROM amendments").fetchone()[0]
        assert n == 0


# ---------------------------------------------------------------------------
# record_dependency — stub until PR-10
# ---------------------------------------------------------------------------


class TestRecordDependencyStub:
    """Importable now so PR-5+ can write tests; invocation raises
    until PR-10 lands the ``item_dependencies`` table."""

    def test_stub_raises_not_implemented(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(NotImplementedError) as exc:
            record_dependency(
                conn=conn,
                item_id="gh:jcronq/alice#824",
                depends_on_item_id="gh:jcronq/alice#823",
                kind="blocks",
                created_by="speaking",
            )
        assert "PR-10" in str(exc.value)
