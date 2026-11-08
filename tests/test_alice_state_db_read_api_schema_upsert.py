"""Schema-upsert-on-every-open tests for :mod:`alice_state_db.read_api`.

Issue #853, Fix 5: ``connect()`` used to call ``apply_schema`` only
when ``read_only=True AND not path.exists()``. That meant a reader
opening an existing DB stamped with an older schema silently saw
whatever tables the stale file had — a schema bump broke every
reader until something wrote to the DB first. Now ``apply_schema``
runs on every RO open via a scratch RW handle, so a stale-but-present
DB gets the missing tables added on first read.

Covers two shapes:

1. A pre-#790 DB with only the original four tables (``tasks`` /
   ``workers`` / ``events`` / ``derive_runs``) picks up the SM v2
   substrate tables (``items`` etc.) on next ``connect()``.
2. Two concurrent ``connect()`` calls on a missing path both succeed
   (the ``CREATE IF NOT EXISTS`` DDL is harmless under races).
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
from typing import List

from alice_state_db import read_api


# ----- helpers -------------------------------------------------------


def _seed_stale_db(path: pathlib.Path) -> None:
    """Create a DB file with only the pre-#790 subset of tables.

    Uses the real production DDL constants from
    :mod:`alice_state_db.schema` for the four tables that predate the
    SM v2 substrate (``tasks`` / ``workers`` / ``events`` /
    ``derive_runs``) and their indexes — so the on-disk shape matches
    an actual pre-#790 file. Deliberately omits ``DDL_ITEMS`` and the
    rest of the SM v2 block so the upsert-on-open path has real
    work to do.
    """
    from alice_state_db import schema as _schema

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_schema.DDL_TASKS)
        for stmt in _schema.DDL_TASKS_INDEX:
            conn.execute(stmt)
        conn.execute(_schema.DDL_WORKERS)
        for stmt in _schema.DDL_WORKERS_INDEX:
            conn.execute(stmt)
        conn.execute(_schema.DDL_EVENTS)
        for stmt in _schema.DDL_EVENTS_INDEX:
            conn.execute(stmt)
        conn.execute(_schema.DDL_DERIVE_RUNS)
        for stmt in _schema.DDL_DERIVE_RUNS_INDEX:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ----- tests ---------------------------------------------------------


def test_connect_applies_schema_to_existing_stale_db(
    tmp_path: pathlib.Path,
) -> None:
    """A reader opening a pre-#790 DB should trigger the upsert on
    open and then see ``items`` / ``state_machine`` etc. on the very
    next read-only connection.
    """
    db = tmp_path / "alice-state.db"
    _seed_stale_db(db)

    # Sanity: the stale DB does not have the SM v2 ``items`` table yet.
    with sqlite3.connect(
        f"file:{db}?mode=ro", uri=True
    ) as pre:
        tables_before = {
            row[0]
            for row in pre.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "tasks" in tables_before
    assert "items" not in tables_before, (
        "precondition broken: stale DB should NOT yet have the SM v2 tables"
    )

    # First connect() must run apply_schema on a scratch RW handle
    # before opening the RO one. After that, the DB file has the new
    # tables permanently.
    with read_api.connect(db) as conn:
        # The RO handle doesn't need to query this — the next connect
        # below is what proves the upsert stuck to disk.
        conn.execute("SELECT 1").fetchone()

    # Second connect() against the same (now-upgraded) file must be
    # able to SELECT from items without raising OperationalError.
    with read_api.connect(db) as conn:
        rows = conn.execute("SELECT * FROM items").fetchall()
    assert rows == [], (
        "items table should exist after upsert-on-open; got unexpected rows"
    )


def test_connect_concurrent_on_missing_path_does_not_raise(
    tmp_path: pathlib.Path,
) -> None:
    """Two threads racing on a missing path both succeed.

    The DDL inside :func:`alice_state_db.schema.apply_schema` is all
    ``CREATE ... IF NOT EXISTS``, and the scratch-RW path swallows
    ``sqlite3.OperationalError`` from a lost race, so concurrent
    callers don't crash each other.
    """
    db = tmp_path / "subdir" / "alice-state.db"
    assert not db.exists()

    errors: List[BaseException] = []

    def _try() -> None:
        try:
            with read_api.connect(db) as conn:
                conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_try)
    t2 = threading.Thread(target=_try)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive(), "threads hung"
    assert errors == [], f"concurrent connects raised: {errors!r}"
    # Both runs produced the same on-disk file.
    assert db.is_file()


def test_connect_rw_mode_also_works(tmp_path: pathlib.Path) -> None:
    """``read_only=False`` still returns a usable RW connection.

    The upsert-on-open path only fires for the RO branch; the RW
    branch is unchanged. This test guards against a regression where
    a refactor accidentally applies the scratch-RW double-open to
    the writer path too.
    """
    db = tmp_path / "alice-state.db"
    with read_api.connect(db, read_only=False) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (x INT)")
        conn.execute("INSERT INTO probe VALUES (1)")
        conn.commit()
    assert db.is_file()


def test_connect_ro_on_readonly_mount_degrades_gracefully(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """Issue #855: RO-mount scenario (alice-status has ``/srv/state``
    bind-mounted read-only) must not break reads.

    Repro: the status-site container opens ``alice-state.db`` for
    read via :func:`connect`, but the mount is read-only. The PR #854
    schema-upsert path opened a scratch RW handle BEFORE the RO one;
    on a RO mount that scratch open raises
    ``OperationalError("unable to open database file")`` and takes
    the Overview with it.

    Fix: swallow the RW-open failure and fall straight through to the
    ``mode=ro`` open — the writer side owns the schema stamp anyway.

    Simulated here by monkeypatching ``sqlite3.connect`` for the first
    (RW) open to raise ``OperationalError`` the same way a RO mount
    would, while leaving the subsequent URI-mode RO open untouched.
    """
    # First seed a fully-schema'd DB from a real writer-side open so
    # the RO reader has something to query.
    db = tmp_path / "alice-state.db"
    with read_api.connect(db, read_only=False) as conn:
        from alice_state_db.schema import apply_schema

        apply_schema(conn)

    original = sqlite3.connect
    calls: List[str] = []

    def _simulated_ro_mount_connect(dsn, *args, **kwargs):
        calls.append(str(dsn))
        # The RW scratch open is `sqlite3.connect(str(path))` — no
        # ``file:`` prefix, no ``uri=True``. The RO open uses
        # ``file:...?mode=ro`` with ``uri=True``. Only the former
        # fails on a RO mount.
        if not kwargs.get("uri") and "mode=ro" not in str(dsn):
            raise sqlite3.OperationalError("unable to open database file")
        return original(dsn, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _simulated_ro_mount_connect)

    with read_api.connect(db) as conn:
        # The reader must open successfully and be able to query a
        # table the writer-side ``apply_schema`` created.
        row = conn.execute(
            "SELECT COUNT(*) FROM items"
        ).fetchone()
    assert row[0] == 0
    # Sanity: both the RW probe (which raised) and the RO URI open ran.
    assert any("mode=ro" in c for c in calls), (
        "RO URI open never happened — the RW failure should fall through, "
        f"not short-circuit. calls={calls!r}"
    )
