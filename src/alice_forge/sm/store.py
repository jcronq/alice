"""Single write choke point for ``state_machine`` / ``transitions`` / ``amendments``.

Design: jcronq/alice#823 — deprecate SM-on-GitHub, DB-primary state
machine. This module (#824) is the sanctioned write path for all
three tables; no caller migrations happen in this PR. Future PRs
flip :mod:`alice_forge.sm.apply` (PR-3 / #826), the dispatcher
(PR-4 / #827), the thinking shim (PR-5 / #828), and the PR linker
to call through here.

Why a single writer? Three reasons:

1. **Actor trust enforcement.** Only trusted actors can mutate SM
   state. Today the trust set is :data:`ALLOWED_ACTORS`; tomorrow
   we can tighten it (e.g. require a signed caller token) without
   hunting down every ``INSERT INTO transitions`` in the tree.
2. **Edge legality enforcement.** The transition graph in
   :mod:`alice_forge.sm.transitions` is the source of truth. An
   illegal edge (``done → building``) is a protocol violation,
   not a quirky value — it raises :class:`IllegalTransitionError`
   and writes nothing.
3. **Atomic coupling of ``transitions`` + ``state_machine``.** The
   append-only history row and the current-state row must land
   together or not at all; a crash between the two would silently
   drift state. Every :func:`transition` call wraps the two writes
   in a single ``with conn:`` block so SQLite rolls both back on
   any failure.

:data:`ALLOWED_ACTORS` is an *internal* trust set — dispatcher,
thinking, speaking, memory-worker, gh-watcher, plus the human
principals ``jason`` and ``alice``. It is intentionally **not** the
same thing as
:data:`alice_forge.sm.comments.TRUSTED_AUTHORS_DEFAULT` (GitHub
login names, used by the v1/v3 comment parser to decide whether a
GH comment's ``[SM] <verb>`` should be honoured). The two sets
answer different questions — "is this internal caller allowed to
mutate the DB" vs. "is this GitHub commenter's verb trustworthy" —
and must not be merged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from alice_forge.sm.states import SMState
from alice_forge.sm.transitions import (
    TRANSITIONS,
    EventTransition,
    SelfLoop,
    TransitionTo,
)


# ---------------------------------------------------------------------------
# Trust set + edge set
# ---------------------------------------------------------------------------

#: Actors allowed to write SM transitions / amendments. Any caller
#: whose ``actor`` / ``author`` string is not in this set fails
#: closed with :class:`ActorNotTrustedError` before any DB write
#: happens.
#:
#: The set mirrors the design in #823's "Writes" section: ``jason``
#: + ``alice`` are the human / agent principals; ``speaking``,
#: ``thinking``, ``memory-worker``, ``dispatcher``, and
#: ``gh-watcher`` are the automated services that legitimately
#: author transitions.
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


def _derive_legal_edges() -> frozenset[tuple[SMState, SMState]]:
    """Derive ``(from, to)`` edge set from the declarative transition table.

    Walks :data:`alice_forge.sm.transitions.TRANSITIONS` once at
    import. Both comment-verb entries (:class:`TransitionTo`) and
    world-state entries (:class:`EventTransition`) contribute edges
    via their ``target`` attribute. :class:`SelfLoop` entries
    contribute a ``(state, state)`` edge so callers that log a
    self-loop transition (useful for the ``[SM] continue`` audit
    trail) are not rejected as illegal.

    Deriving once at import is correct because
    :data:`TRANSITIONS` is a module-level constant frozen at
    definition — if it ever becomes mutable, :data:`LEGAL_EDGES`
    needs a rebuild hook.
    """
    edges: set[tuple[SMState, SMState]] = set()
    for from_state, action_map in TRANSITIONS.items():
        for action in action_map.values():
            if isinstance(action, (TransitionTo, EventTransition)):
                edges.add((from_state, action.target))
            elif isinstance(action, SelfLoop):
                edges.add((from_state, from_state))
    return frozenset(edges)


#: Precomputed set of legal ``(from_state, to_state)`` pairs
#: derived once at import from :data:`TRANSITIONS`. O(1) membership
#: check in :func:`transition`.
LEGAL_EDGES: frozenset[tuple[SMState, SMState]] = _derive_legal_edges()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ActorNotTrustedError(ValueError):
    """Raised when the caller's ``actor`` / ``author`` is not in
    :data:`ALLOWED_ACTORS`. Write never reaches the DB."""


class IllegalTransitionError(ValueError):
    """Raised when the requested edge is not in :data:`LEGAL_EDGES`.

    Attributes ``from_state`` and ``to_state`` carry the SMState
    enum members (or ``None`` for ``from_state`` if the DB holds an
    unrecognised label) so callers can log / surface the exact
    illegal pair. Write never reaches the DB.
    """

    def __init__(
        self,
        message: str,
        *,
        from_state: Optional[SMState] = None,
        to_state: Optional[SMState] = None,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(message)


class ItemNotFoundError(KeyError):
    """Raised when :func:`transition` is called for an ``item_id``
    with no ``state_machine`` row.

    Items must be seeded (via :mod:`alice_state_db.sm_seed` or a
    future ``create_item`` helper) before they can transition.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(now: Optional[datetime]) -> str:
    """Return an ISO-8601 UTC timestamp with second precision.

    ``now`` is an injectable test seam. A naive datetime is treated
    as UTC rather than local — explicit is better than guessing the
    caller's zone, and every production path already provides tz
    info.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    to: SMState,
    actor: str,
    reason: Optional[str] = None,
    source: str = "sm-store",
    source_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Record a legal state-machine transition atomically.

    Steps (all-or-nothing under a single ``with conn:`` block):

    1. Verify ``actor`` ∈ :data:`ALLOWED_ACTORS` →
       :class:`ActorNotTrustedError` otherwise.
    2. Load current ``state_machine`` row for ``item_id`` →
       :class:`ItemNotFoundError` if missing.
    3. Verify the edge ``(current_state, to)`` is in
       :data:`LEGAL_EDGES` → :class:`IllegalTransitionError`
       otherwise.
    4. If ``source_ref`` is provided and a transition row already
       exists at ``(item_id, source, source_ref)``, treat as a
       replay: return the existing ``transition_id`` without
       writing. This is the primary idempotency mechanism —
       callers that need idempotent transitions pass a stable
       ``source_ref``.
    5. ``INSERT`` into ``transitions``, read ``transition_id``.
    6. ``UPDATE state_machine`` SET ``state``, ``entered_at``,
       ``last_transition_id`` for ``item_id``.
    7. ``with conn:`` commits on exit; on any exception, both
       writes roll back.

    :param conn: SQLite connection owned by the caller. Must have
        ``PRAGMA foreign_keys = ON`` (set by
        :func:`alice_state_db.schema.apply_schema`).
    :param item_id: Human-readable item surrogate
        (``gh:jcronq/alice#790``, ``local:task-0824`` …).
    :param to: Target state as :class:`SMState`.
    :param actor: Author of the transition. Must be in
        :data:`ALLOWED_ACTORS`.
    :param reason: Free-form reason, persisted to
        ``transitions.reason``. Optional but strongly encouraged —
        the audit trail is only as good as the reasons.
    :param source: Short identifier of the caller surface
        (``"sm-store"``, ``"thinking-shim"``, ``"gh-watcher"`` …).
        Paired with ``source_ref`` for the idempotency key.
    :param source_ref: Idempotency key within ``source`` — a GH
        comment id, a worker spawn_id, a session uuid. Pass a
        stable value to make replays safe.
    :param now: Timestamp override (tests inject deterministic
        time).

    :returns: ``transition_id`` of the newly-inserted (or
        previously-inserted, for ``source_ref`` replays) row.
    """
    if actor not in ALLOWED_ACTORS:
        raise ActorNotTrustedError(
            f"actor {actor!r} not in ALLOWED_ACTORS "
            f"(allowed={sorted(ALLOWED_ACTORS)!r})"
        )

    # Read current state BEFORE the write transaction — keeps the
    # read lock duration minimal and surfaces ItemNotFoundError
    # before any mutation is attempted.
    row = conn.execute(
        "SELECT state FROM state_machine WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(
            f"no state_machine row for item_id={item_id!r} — seed it first"
        )
    # Positional access survives both plain tuple rows and
    # ``sqlite3.Row`` factories — don't tie callers to one factory.
    current_label = row[0]
    current = SMState.from_label(current_label)
    if current is None:
        # DB holds a label outside the enum. Protocol violation —
        # better to surface than silently write a new row from an
        # unknown base state.
        raise IllegalTransitionError(
            f"current state {current_label!r} for item {item_id!r} "
            f"is not a recognised SMState",
            from_state=None,
            to_state=to,
        )
    if (current, to) not in LEGAL_EDGES:
        raise IllegalTransitionError(
            f"illegal transition {current.label} -> {to.label} "
            f"for item {item_id!r}",
            from_state=current,
            to_state=to,
        )

    # Idempotency replay: if the caller supplied a source_ref and a
    # row already exists at (item_id, source, source_ref), return
    # the existing id. The uniq_transitions_source_ref partial index
    # would otherwise raise IntegrityError inside the txn.
    if source_ref is not None:
        existing = conn.execute(
            """
            SELECT transition_id FROM transitions
            WHERE item_id = ? AND source = ? AND source_ref = ?
            """,
            (item_id, source, source_ref),
        ).fetchone()
        if existing is not None:
            return int(existing[0])

    ts = _utc_iso(now)
    # ``with conn:`` wraps the two writes in a single transaction:
    # commit on success, rollback on any exception. This is the
    # atomicity invariant — ``transitions`` and ``state_machine``
    # land together or not at all.
    with conn:
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
        transition_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        conn.execute(
            """
            UPDATE state_machine
               SET state = ?,
                   entered_at = ?,
                   last_transition_id = ?
             WHERE item_id = ?
            """,
            (to.label, ts, transition_id, item_id),
        )
    return transition_id


def amend(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    body: str,
    author: str,
    source: str = "sm-store",
    source_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Append an ``amendments`` row. Returns ``amendment_id``.

    No state change — amendments carry observational updates
    attached to an item (added context, revised scope, a reviewer
    note). Two gates:

    * ``author`` must be in :data:`ALLOWED_ACTORS` (same trust set
      as :func:`transition`). This is stricter than the design
      doc's "writes" constraint, but the alternative — allowing
      any string — makes the amendments trail noisy and
      unreviewable. Downstream widening to a broader set is a
      deliberate future change.
    * ``(item_id, source, source_ref)`` triples are unique when
      ``source_ref`` is provided (enforced by the existing
      ``uniq_amendments_source_ref`` partial index). A second
      insert with the same triple raises
      :class:`sqlite3.IntegrityError` **unchanged** — unlike
      :mod:`alice_state_db.sm_seed`, which swallows it for replay
      safety. Callers that want replay semantics should either
      precheck with a ``SELECT`` or catch the error.

    :returns: ``amendment_id`` of the newly-inserted row.
    :raises ActorNotTrustedError: ``author`` outside
        :data:`ALLOWED_ACTORS`.
    :raises sqlite3.IntegrityError: Duplicate
        ``(item_id, source, source_ref)``.
    """
    if author not in ALLOWED_ACTORS:
        raise ActorNotTrustedError(
            f"author {author!r} not in ALLOWED_ACTORS "
            f"(allowed={sorted(ALLOWED_ACTORS)!r})"
        )

    ts = _utc_iso(now)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO amendments (
                item_id, author, body, ts, source, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, author, body, ts, source, source_ref),
        )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def record_dependency(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    depends_on_item_id: str,
    kind: str,
    created_by: str,
    now: Optional[datetime] = None,
) -> None:
    """Record an item dependency.

    .. warning::

       The ``item_dependencies`` table lands in PR-10 of #823.
       Until that PR merges, this function is a stub that raises
       :class:`NotImplementedError` so downstream callers can
       import the name (and write tests that will activate once
       the table exists) but not invoke it yet.

    :param kind: One of ``'blocks'``, ``'follows'``, ``'supersedes'``.
    """
    raise NotImplementedError(
        "record_dependency requires the item_dependencies table "
        "(lands in PR-10 of #823). Import is safe; invocation is not."
    )


# ---------------------------------------------------------------------------
# PR → item linking (#828, PR-5)
# ---------------------------------------------------------------------------


def record_pr_ref(
    *,
    conn: sqlite3.Connection,
    item_id: str,
    pr_url: str,
    actor: str,
    now: Optional[datetime] = None,
) -> None:
    """Record a ``gh:pr`` reference from ``item_id`` to ``pr_url``.

    Idempotent — the underlying ``item_external_refs`` table has a
    composite primary key over ``(item_id, ref_type,
    ref_target_item_id)`` and this INSERT uses ``OR IGNORE``, so
    calling twice with identical arguments is a no-op on the second
    call. Different ``pr_url`` values for the same item simply append
    additional rows (the row-level key differs).

    The actor must be a member of :data:`ALLOWED_ACTORS` or
    :class:`ActorNotTrustedError` is raised before any DB write.

    Design: #823's PR→item linking section — the self-merge path
    (and PR-7's gh-watcher wire-up) calls this on PR-open events
    so the status-site done-today join (and PR-6's label strip) can
    identify which item each merged PR closed.
    """
    if actor not in ALLOWED_ACTORS:
        raise ActorNotTrustedError(actor)
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO item_external_refs (
                item_id, ref_type, ref_target_item_id, ref_source, created_at
            ) VALUES (?, 'gh:pr', ?, ?, ?)
            """,
            (item_id, pr_url, actor, _utc_iso(now)),
        )
