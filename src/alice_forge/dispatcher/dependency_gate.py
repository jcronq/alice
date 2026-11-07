"""Dispatcher-side dependency gate (issue #833).

Thin glue that lets the dispatcher skip a ``sm:selected`` item while
any ``item_dependencies`` prerequisite is still non-terminal. The gate
is a pure READ against ``alice-state.db``'s ``items`` /
``state_machine`` / ``item_dependencies`` tables — writes are never
issued here.

Signature of the public hook is deliberately narrow:

    check(repo: str, number: int) -> Optional[str]

A return of ``None`` means "ready, no gating." A return of a non-empty
string is the operator-facing log line the dispatcher should emit
before skipping the item. Keeping the log string construction here
(and not in ``main.py``) means the format matches issue #833 exactly
without duplicating the detail-walk code.

Why a factory instead of a single ``check(repo, number)`` top-level?
The dispatcher constructs the gate once per :func:`alice_forge.dispatcher.main.run`
call — the DB path is resolved on that outer pass. Once bound, the
per-item check opens a short-lived read-only connection against the
same path. If the DB can't be opened, the gate returns ``None`` for
every item — "fail open" is the right default because the gate is a
safety filter layered over an already-stable dispatcher, and locking
Alice out of all ``sm:selected`` work because of a missing DB would
be the worse failure mode.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Callable, Optional

from alice_state_db import connect as _connect_state_db
from alice_state_db import gh_issue_id, is_ready, non_terminal_blocker_detail


def build_default_check(
    *,
    db_path: Optional[pathlib.Path] = None,
    log: Callable[[str], None] = lambda s: None,
) -> Callable[[str, int], Optional[str]]:
    """Return a ``check(repo, number)`` closure backed by ``alice-state.db``.

    ``db_path=None`` resolves to :data:`alice_state_db.DEFAULT_DB_PATH`
    inside :func:`alice_state_db.connect`. Tests pass an explicit path
    into a tmp alice-mind tree.

    The closure captures ``log`` so DB-open failures surface once per
    item (noisy but useful during the rollout); callers that want a
    quieter path can pass ``log=lambda _: None``.
    """

    def _check(repo: str, number: int) -> Optional[str]:
        item_id = gh_issue_id(repo, number)
        try:
            # Read-only connection; cheap enough to open per-item
            # (the dispatcher cadence is seconds, items per pass is
            # single-digit-tens in steady state). If this grows into
            # a bottleneck, swap for a per-pass cached connection on
            # the ``run()`` scope — but don't do it preemptively.
            with _connect_state_db(db_path=db_path, read_only=True) as conn:
                return _check_against_conn(conn, item_id)
        except (sqlite3.Error, OSError) as exc:
            # Fail open — a broken state DB must NOT lock the
            # dispatcher out of its primary job. Catches both SQLite
            # errors (corrupt file, locked WAL, unknown schema) and
            # OSError (missing parent dir, EACCES, ENOSPC). Log so
            # the operator notices; the sweep loop will keep running.
            log(
                f"[sm-dispatcher] dependency gate: DB open failed for "
                f"{item_id}: {type(exc).__name__}: {exc} (fail-open)"
            )
            return None

    return _check


def _check_against_conn(
    conn: sqlite3.Connection, item_id: str
) -> Optional[str]:
    """Core gate check — pure function of the DB connection.

    Split out from :func:`build_default_check` so unit tests can
    exercise the gate against an in-memory connection without going
    through the ``_connect_state_db`` factory.

    Returns a log line (string) if the item is gated, else ``None``.
    The log format matches the one named in issue #833:
    ``item-X gated by item-Y (blocks, state sm:...)``.
    """
    if is_ready(conn, item_id):
        return None
    details = non_terminal_blocker_detail(conn, item_id)
    if not details:
        # Belt-and-braces: is_ready() said non-ready but the detail walk
        # returned empty — treat as ready so we don't skip silently.
        return None
    # Pick the first blocker (deterministic sort from the detail call)
    # for the log line. All blockers still count — the gate stays
    # engaged until every one of them is terminal — but spelling them
    # all out would make the line unreadable for the N>3 case. The
    # count makes it clear there are others.
    first_blocker, first_state = details[0]
    state_label = first_state or "unknown"
    extra = (
        f" (+{len(details) - 1} more)" if len(details) > 1 else ""
    )
    return (
        f"[sm-dispatcher] {item_id} gated by {first_blocker} "
        f"(blocks, state {state_label}){extra}"
    )
