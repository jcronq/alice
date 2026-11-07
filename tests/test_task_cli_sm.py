"""Integration tests for the ``task sm ...`` CLI verbs (#825).

Each test runs the CLI end-to-end against a tmp-path
``alice-state.db``, seeds items + state via direct SQL, invokes the
CLI, and verifies the DB mutation via direct SQL (not via the CLI's
own read helpers — the whole point of testing both layers).

Acceptance spec (from issue #825):

1. ``task sm transition item-X sm:selected --actor jason`` on a seeded
   DB writes one ``transitions`` row and updates ``state_machine``.
2. ``task sm reject item-X --reason "duplicate"`` writes a transition
   to ``sm:rejected`` with reason persisted.
3. ``task sm unblock item-X`` where prior state was ``sm:designing``
   restores the DB state to ``sm:designing``.
4. ``task sm show item-X`` prints the current state and the last
   transition's ``(from, to, actor, reason, ts)`` tuple.
5. ``task sm transition item-X sm:done --actor rando`` exits non-zero
   with ``ActorNotTrustedError``-equivalent stderr.

Plus orthogonal coverage:

* ``amend`` writes an ``amendments`` row.
* ``block`` + ``unblock`` roundtrip.
* ``select`` / ``done`` shorthand verbs transition correctly.
* ``transition`` to a label outside the SMState enum exits non-zero.
* ``show`` on a missing item emits a ``not found`` error.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from alice_forge import task_cli
from alice_forge.sm.states import SMState
from alice_state_db.schema import apply_schema


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "alice-state.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return db


def _seed_item(
    db_path: pathlib.Path,
    *,
    item_id: str,
    state: SMState,
) -> None:
    """Minimum rows so ``sm.store`` can mutate state."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO items (item_id, item_type, title, created_at, updated_at)
            VALUES (?, 'gh:issue', ?, '2026-11-07T00:00:00+00:00',
                                       '2026-11-07T00:00:00+00:00')
            """,
            (item_id, f"test item {item_id}"),
        )
        conn.execute(
            """
            INSERT INTO state_machine (item_id, state, entered_at)
            VALUES (?, ?, '2026-11-07T00:00:00+00:00')
            """,
            (item_id, state.label),
        )


def _run(args: list[str], db_path: pathlib.Path) -> int:
    """Invoke the CLI with ``--db-path`` wired in for every sm verb.

    The ``--root`` default (``~/alice-mind/inner/tasks``) is
    irrelevant to sm verbs — they don't touch the filesystem task
    store — but argparse still requires the top-level to resolve, so
    we hand it a scratch root under the DB's parent.
    """
    scratch_root = db_path.parent / "tasks-root"
    scratch_root.mkdir(exist_ok=True)
    # --db-path is a flag on the ``sm`` subparser, so it goes AFTER
    # the ``sm`` token but BEFORE the specific verb.
    cli = ["--root", str(scratch_root), "sm", "--db-path", str(db_path)]
    cli.extend(args)
    return task_cli.main(cli)


def _run_json(args: list[str], db_path: pathlib.Path) -> int:
    scratch_root = db_path.parent / "tasks-root"
    scratch_root.mkdir(exist_ok=True)
    cli = [
        "--root", str(scratch_root),
        "--json",
        "sm", "--db-path", str(db_path),
    ]
    cli.extend(args)
    return task_cli.main(cli)


# ---------------------------------------------------------------------------
# Acceptance 1: transition writes transitions + updates state_machine


class TestTransitionGeneric:
    def test_writes_transition_row_and_updates_state_machine(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            ["transition", "item-X", "sm:selected", "--actor", "jason"],
            db_path,
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "sm:selected" in out

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            tr_rows = conn.execute(
                "SELECT * FROM transitions WHERE item_id = ?",
                ("item-X",),
            ).fetchall()
            assert len(tr_rows) == 1
            tr = tr_rows[0]
            assert tr["from_state"] == "sm:draft"
            assert tr["to_state"] == "sm:selected"
            assert tr["actor"] == "jason"

            sm_row = conn.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert sm_row["state"] == "sm:selected"

    def test_json_mode_emits_transition_id(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run_json(
            ["transition", "item-X", "sm:selected", "--actor", "jason"],
            db_path,
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["item_id"] == "item-X"
        assert payload["state"] == "sm:selected"
        assert isinstance(payload["transition_id"], int)

class TestTransitionReasonPersisted:
    """When --reason is passed on the generic verb, it's stored."""

    def test_reason_persisted_when_provided(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            [
                "transition", "item-X", "sm:selected",
                "--actor", "jason", "--reason", "pulling in",
            ],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT reason FROM transitions WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert row[0] == "pulling in"


# ---------------------------------------------------------------------------
# Acceptance 2: reject persists reason


class TestRejectShorthand:
    def test_reject_persists_reason(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            ["reject", "item-X", "--reason", "duplicate", "--actor", "jason"],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT to_state, reason FROM transitions WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert row[0] == "sm:rejected"
            assert row[1] == "duplicate"

    def test_reject_requires_reason(
        self, db_path: pathlib.Path
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        # argparse rejects a missing --reason at parse time (required=True)
        # → SystemExit(2). We assert via pytest.raises since argparse
        # uses sys.exit rather than returning a code.
        with pytest.raises(SystemExit):
            _run(["reject", "item-X", "--actor", "jason"], db_path)


# ---------------------------------------------------------------------------
# Acceptance 3: unblock restores prior state


class TestUnblock:
    def test_unblock_restores_prior_state(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        # DRAFT → SELECTED → DESIGNING → BLOCKED, then unblock.
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        assert _run(
            ["transition", "item-X", "sm:selected",
             "--actor", "jason", "--reason", "pick"],
            db_path,
        ) == 0
        # SELECTED → DESIGNING via the event trigger isn't a generic
        # edge, but LEGAL_EDGES includes it (event-transition on the
        # ``spawn-dispatch-art-code`` key). Use the generic verb.
        assert _run(
            ["transition", "item-X", "sm:designing",
             "--actor", "jason", "--reason", "designing"],
            db_path,
        ) == 0
        # DESIGNING → BLOCKED is not a direct legal edge in the SM.
        # We need a path from DESIGNING through a state that CAN
        # reach BLOCKED. Looking at the TRANSITIONS table: BLOCKED is
        # reachable from NEEDS_STUDY (study-blocked) and SELECTED (via
        # dep-rejected event). We can't easily reach BLOCKED from
        # DESIGNING through legal edges. For this test, we insert a
        # transition manually that moves item-X from a hypothetical
        # DESIGNING state to BLOCKED — because the acceptance test is
        # about restoring prior state, not about reaching BLOCKED.
        #
        # Direct SQL mimics the "dispatcher-side" path where blocked
        # entry is driven by world state (dep-rejected event), so this
        # is faithful to how a real item ends up here.
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO transitions (
                    item_id, from_state, to_state, actor, reason, ts,
                    source, source_ref
                ) VALUES (?, 'sm:designing', 'sm:blocked', 'dispatcher',
                          'dep-rejected', '2026-11-07T00:00:00+00:00',
                          'test-seed', NULL)
                """,
                ("item-X",),
            )
            conn.execute(
                "UPDATE state_machine SET state = 'sm:blocked' WHERE item_id = ?",
                ("item-X",),
            )
            conn.commit()

        code = _run(["unblock", "item-X", "--actor", "jason"], db_path)
        assert code == 0
        out = capsys.readouterr().out
        assert "sm:designing" in out

        with sqlite3.connect(str(db_path)) as conn:
            sm = conn.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert sm[0] == "sm:designing"
            # Verify a transitions row was recorded for the unblock.
            latest = conn.execute(
                """
                SELECT from_state, to_state, actor FROM transitions
                WHERE item_id = ?
                ORDER BY transition_id DESC LIMIT 1
                """,
                ("item-X",),
            ).fetchone()
            assert latest[0] == "sm:blocked"
            assert latest[1] == "sm:designing"
            assert latest[2] == "jason"

    def test_unblock_without_history_errors(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        # Item was born in BLOCKED (hypothetical) → no prior transition.
        _seed_item(db_path, item_id="item-X", state=SMState.BLOCKED)
        code = _run(["unblock", "item-X", "--actor", "jason"], db_path)
        assert code != 0
        assert "no prior transition" in capsys.readouterr().err.lower()

    def test_unblock_not_blocked_errors(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(["unblock", "item-X", "--actor", "jason"], db_path)
        assert code != 0
        assert "sm:blocked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Acceptance 4: show prints current state + last transition tuple


class TestShow:
    def test_show_prints_current_state_and_last_transition(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        assert _run(
            ["transition", "item-X", "sm:selected",
             "--actor", "jason", "--reason", "pick"],
            db_path,
        ) == 0
        capsys.readouterr()

        code = _run(["show", "item-X"], db_path)
        assert code == 0
        out = capsys.readouterr().out
        # Current state line.
        assert "sm:selected" in out
        # Last transition tuple: from, to, actor, reason.
        assert "sm:draft" in out
        assert "jason" in out
        assert "pick" in out

    def test_show_json_mode(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        _run(
            ["transition", "item-X", "sm:selected",
             "--actor", "jason", "--reason", "pick"],
            db_path,
        )
        capsys.readouterr()
        code = _run_json(["show", "item-X"], db_path)
        assert code == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["state"]["state"] == "sm:selected"
        tr = payload["recent_transitions"]
        assert len(tr) >= 1
        assert tr[0]["to_state"] == "sm:selected"
        assert tr[0]["actor"] == "jason"
        assert tr[0]["reason"] == "pick"

    def test_show_missing_item_errors(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        code = _run(["show", "item-not-here"], db_path)
        assert code != 0
        assert "not found" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Acceptance 5: untrusted actor exits non-zero


class TestUntrustedActor:
    def test_untrusted_actor_exits_non_zero(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.REVIEWING)
        code = _run(
            [
                "transition", "item-X", "sm:done",
                "--actor", "rando", "--reason", "trying it",
            ],
            db_path,
        )
        assert code != 0
        err = capsys.readouterr().err
        assert "rando" in err
        assert "ALLOWED_ACTORS" in err


# ---------------------------------------------------------------------------
# Orthogonal coverage


class TestAmend:
    def test_amend_writes_row(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            [
                "amend", "item-X",
                "--body", "revised scope — see doc",
                "--author", "jason",
            ],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT author, body FROM amendments WHERE item_id = ?",
                ("item-X",),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "jason"
            assert rows[0][1] == "revised scope — see doc"


class TestBlockShorthand:
    def test_block_transitions_to_blocked(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        # Block from NEEDS_STUDY → BLOCKED is a legal edge
        # (study-blocked verb).
        _seed_item(db_path, item_id="item-X", state=SMState.NEEDS_STUDY)
        code = _run(
            [
                "block", "item-X",
                "--reason", "waiting on upstream",
                "--actor", "jason",
            ],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert row[0] == "sm:blocked"


class TestDoneShorthand:
    def test_done_transitions_to_done(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        # REVIEWING → DONE isn't a direct verb edge (it's an
        # EventTransition on pr-merged-master-green-verified), but
        # LEGAL_EDGES is derived from the transitions table and
        # includes it.
        _seed_item(db_path, item_id="item-X", state=SMState.REVIEWING)
        code = _run(
            ["done", "item-X", "--actor", "jason"],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert row[0] == "sm:done"


class TestSelectShorthand:
    def test_select_transitions_to_selected(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            ["select", "item-X", "--actor", "jason"],
            db_path,
        )
        assert code == 0
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT state FROM state_machine WHERE item_id = ?",
                ("item-X",),
            ).fetchone()
            assert row[0] == "sm:selected"


# ---------------------------------------------------------------------------
# Error paths


class TestErrors:
    def test_unknown_state_label(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        _seed_item(db_path, item_id="item-X", state=SMState.DRAFT)
        code = _run(
            [
                "transition", "item-X", "sm:not_a_state",
                "--actor", "jason", "--reason", "try",
            ],
            db_path,
        )
        assert code != 0
        err = capsys.readouterr().err
        assert "unknown state" in err.lower()

    def test_illegal_edge(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        # done → building is explicitly called out as illegal in #824.
        _seed_item(db_path, item_id="item-X", state=SMState.DONE)
        code = _run(
            [
                "transition", "item-X", "sm:building",
                "--actor", "jason", "--reason", "revert",
            ],
            db_path,
        )
        assert code != 0
        err = capsys.readouterr().err
        assert "illegal transition" in err.lower()

    def test_unknown_item(
        self, db_path: pathlib.Path, capsys
    ) -> None:
        code = _run(
            [
                "transition", "ghost-item", "sm:selected",
                "--actor", "jason", "--reason", "fail",
            ],
            db_path,
        )
        assert code != 0
        err = capsys.readouterr().err
        assert "ghost-item" in err
