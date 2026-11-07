"""Single write choke point for state_machine/transitions/amendments.

ALLOWED_ACTORS is the actor-trust check: every write must name a
member of this frozenset in `actor=`/`created_by=`/`author=`, or the
call raises ActorNotTrustedError before touching the DB. This set is
internal-system-actor identity (dispatcher, thinking, speaking, ...),
NOT the same thing as alice_forge.sm.comments.TRUSTED_AUTHORS_DEFAULT
(GitHub human login names, used by the v1/v3 comment parser to decide
whether a GH comment's [SM] verb should be honored). The two sets
answer different questions -- "is this internal caller allowed to
mutate the DB directly" vs. "is this GitHub commenter's verb
trustworthy" -- and must not be merged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from alice_forge.sm.states import SMState
from alice_forge.sm.transitions import (
    TRANSITIONS,
    EventTransition,
    SelfLoop,
    TransitionTo,
)


ALLOWED_ACTORS: frozenset[str] = frozenset(
    {
        "jason",
        "alice",
        "speaking",
        "thinking",
        "memory-worker",
        "dispatcher",
        "gh-watcher",
    }
)


class ActorNotTrustedError(ValueError):
    """Raised when a write names an actor outside ALLOWED_ACTORS."""


class IllegalTransitionError(ValueError):
    """Raised when (current_state, target_state) is not a legal edge."""


class ItemNotFoundError(KeyError):
    """Raised when the item_id has no state_machine row."""


def _derive_legal_edges() -> frozenset[tuple[SMState, SMState]]:
    """Walk TRANSITIONS once and build the set of legal (from, to) edges.

    Matches the transitions module's three action kinds:
    ``TransitionTo`` / ``EventTransition`` both carry a ``target``
    attribute (the destination state); ``SelfLoop`` is implicit
    ``(from, from)``. This runs exactly once at module load; callers
    get a frozenset for O(1) membership checks.
    """
    edges: set[tuple[SMState, SMState]] = set()
    for from_state, action_map in TRANSITIONS.items():
        for action in action_map.values():
            if isinstance(action, (TransitionTo, EventTransition)):
                edges.add((from_state, action.target))
            elif isinstance(action, SelfLoop):
                edges.add((from_state, from_state))
    return frozenset(edges)


_LEGAL_EDGES: frozenset[tuple[SMState, SMState]] = _derive_legal_edges()


def _utc_iso(now: datetime | None) -> str:
    """Normalize to UTC and return an ISO-8601 string with second precision."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.isoformat(timespec="seconds")


def transition(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    to: SMState,
    actor: str,
    reason: str | None = None,
    source: str = "sm-store",
    source_ref: str | None = None,
    now: datetime | None = None,
) -> int:
    """Record a legal state-machine transition atomically.

    Steps run in order; any failure during the INSERT transition +
    UPDATE state_machine block rolls back both writes before
    re-raising. Returns the new ``transitions.transition_id``.
    """
    if actor not in ALLOWED_ACTORS:
        raise ActorNotTrustedError(actor)

    row = conn.execute(
        "SELECT state FROM state_machine WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(item_id)

    current = SMState.from_label(row["state"])
    if current is None:
        raise IllegalTransitionError(
            f"stored state {row['state']!r} does not parse as SMState"
        )
    if (current, to) not in _LEGAL_EDGES:
        raise IllegalTransitionError(f"{current.label} -> {to.label}")

    ts = _utc_iso(now)
    try:
        cur = conn.execute(
            """
            INSERT INTO transitions (
                item_id, from_state, to_state, actor, reason,
                ts, source, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                current.label,
                to.label,
                actor,
                reason,
                ts,
                source,
                source_ref,
            ),
        )
        conn.execute(
            """
            UPDATE state_machine
            SET state = ?, entered_at = ?, last_transition_id = ?
            WHERE item_id = ?
            """,
            (to.label, ts, cur.lastrowid, item_id),
        )
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return int(cur.lastrowid)


def amend(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    body: str,
    author: str,
    source: str = "sm-store",
    source_ref: str | None = None,
    now: datetime | None = None,
) -> int:
    """Append an amendment row. Returns the new ``amendment_id``.

    Duplicate ``(item_id, source, source_ref)`` triples collide with
    the existing ``uniq_amendments_source_ref`` partial index; the
    resulting ``sqlite3.IntegrityError`` propagates unchanged -- unlike
    :mod:`alice_state_db.sm_seed`, which swallows it for replay safety.
    """
    if author not in ALLOWED_ACTORS:
        raise ActorNotTrustedError(author)

    ts = _utc_iso(now)
    cur = conn.execute(
        """
        INSERT INTO amendments (item_id, author, body, ts, source, source_ref)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, author, body, ts, source, source_ref),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_dependency(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    depends_on_item_id: str,
    kind: str,
    created_by: str,
    now: datetime | None = None,
) -> None:
    raise NotImplementedError("item_dependencies lands in PR-10")
