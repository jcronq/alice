"""Helpers over ``item_dependencies`` (issue #833).

``item_dependencies`` is the inter-item edge table added alongside
the SM v2 substrate (``items`` / ``state_machine`` / ``transitions`` /
``amendments``). The dispatcher consults it to gate a ``sm:selected``
item from transitioning while any ``kind='blocks'`` prerequisite is
still in a non-terminal state. Terminal states are ``sm:done`` and
``sm:rejected`` — anything else counts as "still blocking" for the
gate.

Three edge kinds live in the same table:

* ``blocks``     — hard gate; dispatcher refuses to fire actioning
                   on the dependent while any prerequisite is open.
* ``follows``    — ordering hint; renderers show "after X" context
                   lines, but the dispatcher does NOT gate on it.
* ``supersedes`` — a landing ``→ sm:done`` on the superseder
                   cascades the superseded to ``sm:rejected`` (the
                   cascade itself lives in the store layer so the
                   write is atomic with the transition).

Public surface is five helpers:

* :func:`add_dependency`      — idempotent write, cycle-checked.
* :func:`remove_dependency`   — idempotent delete.
* :func:`blockers_for`        — direct ``kind='blocks'`` prerequisites.
* :func:`blocked_by_count`    — count of non-terminal transitive
                                 blockers (``blocks`` edges only).
* :func:`is_ready`            — ``True`` when every transitive
                                 ``blocks`` prerequisite is terminal.

All helpers take an already-open ``sqlite3.Connection``. The caller
owns transaction boundaries; :func:`add_dependency` and
:func:`remove_dependency` do their own ``conn.commit()`` so a bare
"add one edge" call flushes without ceremony.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

#: States an item can land in that mean "no longer blocking downstream
#: work." Must stay in sync with :class:`alice_forge.sm.states.SMState`
#: terminals; mirrored here as a plain tuple so :mod:`alice_state_db`
#: doesn't drag the dispatcher's import graph into readers.
TERMINAL_STATES: frozenset[str] = frozenset({"sm:done", "sm:rejected"})

#: Allowed values for the ``kind`` column. Kept as a frozenset so the
#: validator stays O(1) and the set is hashable for cheap error
#: formatting.
VALID_KINDS: frozenset[str] = frozenset({"blocks", "follows", "supersedes"})


class DependencyError(ValueError):
    """Base class for :mod:`dependencies` validation failures.

    Subclasses give callers a single ``except DependencyError`` catch
    point — the dispatcher handles each subtype the same way
    (log + skip) so a stringier taxonomy would be overkill.
    """


class CycleError(DependencyError):
    """Raised when :func:`add_dependency` would introduce a cycle.

    Concrete example: ``A -> B`` already exists; the caller tries to
    write ``B -> A``. A BFS from ``depends_on_item_id`` out through the
    existing edges reaches ``item_id`` — the write is refused before
    it lands.
    """


class UnknownItemError(DependencyError):
    """Raised when either side of a dependency refers to a missing item.

    Keeps the ``item_dependencies`` table from accumulating orphan
    rows — the schema has FK CASCADE, so a later ``items`` delete
    would wipe them, but we'd rather reject at write time with a
    clear error than let the mistake sit until an ``items`` row is
    pruned.
    """


# ---------------------------------------------------------------------------
# Internal plumbing


def _utc_now_iso() -> str:
    """Timestamp used for ``item_dependencies.created_at``.

    Matches :func:`alice_state_db.sm_seed._utc_now_iso` on purpose —
    the two files both write ISO-8601 UTC, and a reader joining across
    the two tables shouldn't trip on a sub-second format difference.
    """
    return (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )


def _validate_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise DependencyError(
            f"invalid dependency kind {kind!r}; expected one of "
            f"{sorted(VALID_KINDS)!r}"
        )


def _item_exists(conn: sqlite3.Connection, item_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM items WHERE item_id = ? LIMIT 1", (item_id,)
    ).fetchone()
    return row is not None


def _would_introduce_cycle(
    conn: sqlite3.Connection,
    item_id: str,
    depends_on_item_id: str,
) -> bool:
    """Return ``True`` iff adding ``item_id -> depends_on_item_id`` cycles.

    We're about to add the edge "``item_id`` depends on
    ``depends_on_item_id``". A cycle exists iff ``item_id`` is already
    reachable from ``depends_on_item_id`` via the existing dependency
    graph. We walk forward from ``depends_on_item_id`` (its own
    blockers, then their blockers, etc.) and look for ``item_id``.

    Self-edges (``A -> A``) are cycles too — a one-step walk from
    ``A`` against the existing graph can't catch them because the row
    isn't there yet, so we check for that case separately before the
    BFS.

    The walk considers every ``kind`` — a ``blocks`` edge on one hop
    and a ``follows`` edge on the next still make a cycle in the
    reachability sense, and we don't want to let that build up.
    """
    if item_id == depends_on_item_id:
        return True

    # BFS from depends_on_item_id forward through the "depends on" edges.
    # Visit set guards against pre-existing cycles in the DB (shouldn't
    # happen, but a corrupt seed shouldn't make this walk infinite).
    frontier: list[str] = [depends_on_item_id]
    visited: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        if current == item_id:
            return True
        rows = conn.execute(
            "SELECT depends_on_item_id FROM item_dependencies WHERE item_id = ?",
            (current,),
        ).fetchall()
        for (next_id,) in rows:
            if next_id not in visited:
                frontier.append(next_id)
    return False


def _state_of(conn: sqlite3.Connection, item_id: str) -> str | None:
    """Fetch the current ``state_machine.state`` for ``item_id``.

    Returns ``None`` when the item has no ``state_machine`` row (new
    item seeded before SM state was recorded). The gate treats a
    missing state as "non-terminal" — safer to over-gate than to
    under-gate while the substrate is still filling out.
    """
    row = conn.execute(
        "SELECT state FROM state_machine WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    return row[0]


# ---------------------------------------------------------------------------
# Public API


def add_dependency(
    conn: sqlite3.Connection,
    item_id: str,
    depends_on_item_id: str,
    kind: str = "blocks",
    *,
    created_at: str | None = None,
) -> None:
    """Write one edge into ``item_dependencies``. Idempotent.

    Raises :class:`DependencyError` on invalid ``kind``, unknown
    ``item_id`` / ``depends_on_item_id``, or if the edge would create
    a cycle in the dependency graph. The write uses
    ``INSERT OR IGNORE`` so a re-add with the same composite PK is a
    no-op — callers can safely replay the same ``task deps add`` and
    it won't fail.

    ``created_at`` is injected for test determinism; production calls
    leave it ``None`` and get a UTC ISO-8601 stamp.
    """
    _validate_kind(kind)

    if not _item_exists(conn, item_id):
        raise UnknownItemError(f"item_id not found in items: {item_id!r}")
    if not _item_exists(conn, depends_on_item_id):
        raise UnknownItemError(
            f"depends_on_item_id not found in items: {depends_on_item_id!r}"
        )

    # Cycle check runs BEFORE the insert so a rejected add never leaves
    # a half-written row behind. Self-edges are caught by the same
    # check (see _would_introduce_cycle's self-edge short-circuit).
    if _would_introduce_cycle(conn, item_id, depends_on_item_id):
        raise CycleError(
            f"adding {item_id!r} -> {depends_on_item_id!r} "
            f"(kind={kind!r}) would introduce a cycle"
        )

    conn.execute(
        """
        INSERT OR IGNORE INTO item_dependencies
            (item_id, depends_on_item_id, kind, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (item_id, depends_on_item_id, kind, created_at or _utc_now_iso()),
    )
    conn.commit()


def remove_dependency(
    conn: sqlite3.Connection,
    item_id: str,
    depends_on_item_id: str,
    kind: str = "blocks",
) -> None:
    """Delete one edge from ``item_dependencies``. Idempotent.

    Removing a non-existent edge is a no-op — ``DELETE`` returns zero
    rows affected and we don't raise. The composite PK is the match
    key, so every argument must be supplied; ``kind`` defaults to
    ``blocks`` to match :func:`add_dependency`.
    """
    _validate_kind(kind)
    conn.execute(
        """
        DELETE FROM item_dependencies
        WHERE item_id = ? AND depends_on_item_id = ? AND kind = ?
        """,
        (item_id, depends_on_item_id, kind),
    )
    conn.commit()


def blockers_for(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    kind: str = "blocks",
) -> list[str]:
    """Direct prerequisites of ``item_id`` at the given ``kind``.

    Returns ``item_id`` values (as a list) sorted ascending for
    deterministic output — the caller usually feeds this into a log
    line or a status-site panel, and stable ordering keeps diffs
    noise-free.

    Default ``kind='blocks'`` matches the dispatcher gate's use case.
    For a different edge type pass ``kind='follows'`` /
    ``kind='supersedes'`` explicitly.
    """
    _validate_kind(kind)
    rows = conn.execute(
        """
        SELECT depends_on_item_id FROM item_dependencies
        WHERE item_id = ? AND kind = ?
        ORDER BY depends_on_item_id ASC
        """,
        (item_id, kind),
    ).fetchall()
    return [r[0] for r in rows]


def _transitive_blockers(
    conn: sqlite3.Connection, item_id: str
) -> set[str]:
    """Walk the ``kind='blocks'`` graph and return every reachable prereq.

    The dispatcher gate needs transitive blocking: if A blocks B and
    B blocks C (both ``kind='blocks'`` edges), then C is still gated
    while A is non-terminal, even though there's no direct ``A -> C``
    edge. BFS from ``item_id`` outward through ``blocks`` edges, visit
    set guards against cycles that a corrupt seed could have left in
    place.
    """
    reached: set[str] = set()
    frontier: list[str] = [item_id]
    while frontier:
        current = frontier.pop()
        rows = conn.execute(
            """
            SELECT depends_on_item_id FROM item_dependencies
            WHERE item_id = ? AND kind = 'blocks'
            """,
            (current,),
        ).fetchall()
        for (next_id,) in rows:
            if next_id in reached:
                continue
            reached.add(next_id)
            frontier.append(next_id)
    return reached


def blocked_by_count(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    terminal_states: Iterable[str] = TERMINAL_STATES,
) -> int:
    """Count transitive ``blocks`` prerequisites that are non-terminal.

    Walks the graph with :func:`_transitive_blockers` then filters by
    ``state_machine.state`` — a prerequisite in ``sm:done`` /
    ``sm:rejected`` is "cleared" and doesn't count. Missing
    ``state_machine`` rows count as blocking (the item exists but
    hasn't been seeded a state yet; we'd rather gate than fire).

    ``terminal_states`` is injectable so tests can simulate a custom
    terminal set without monkeypatching the module-level constant.
    """
    blockers = _transitive_blockers(conn, item_id)
    if not blockers:
        return 0
    terminals = set(terminal_states)
    blocked_count = 0
    for blocker in blockers:
        state = _state_of(conn, blocker)
        if state not in terminals:
            blocked_count += 1
    return blocked_count


def is_ready(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    terminal_states: Iterable[str] = TERMINAL_STATES,
) -> bool:
    """``True`` iff every transitive ``blocks`` prerequisite is terminal.

    Thin wrapper over :func:`blocked_by_count` kept as its own name so
    the dispatcher-side code reads naturally:
    ``if not is_ready(conn, item_id): continue``.
    """
    return blocked_by_count(
        conn, item_id, terminal_states=terminal_states
    ) == 0


def non_terminal_blocker_detail(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    terminal_states: Iterable[str] = TERMINAL_STATES,
) -> list[tuple[str, str | None]]:
    """List ``(blocker_item_id, state)`` for every non-terminal blocker.

    The dispatcher needs more than a boolean — the log line Issue #833
    asks for names a specific blocker + its state, e.g.
    ``[sm-dispatcher] item-X gated by item-Y (blocks, state sm:building)``.
    Returns a list so callers can mention the first (deterministic
    ordering) or spell them all out. The ``state`` second element is
    ``None`` when the blocker has no ``state_machine`` row.
    """
    blockers = sorted(_transitive_blockers(conn, item_id))
    terminals = set(terminal_states)
    out: list[tuple[str, str | None]] = []
    for blocker in blockers:
        state = _state_of(conn, blocker)
        if state not in terminals:
            out.append((blocker, state))
    return out
