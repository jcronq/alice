"""Typed state enum + per-state metadata for SM v3.

The string values match v1's GitHub label names exactly
(``sm:draft``, ``sm:needs_study``, etc.) so the migration is a typed
wrapper around the existing labels rather than a renaming. Lookups
from a raw label string to an :class:`SMState` go through
:meth:`SMState.from_label`.

:data:`STATE_META` carries the per-state defaults the dispatcher
consults at runtime: whether the state is terminal, the default TTL
between continue comments, and a one-line role description for audit
trails.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping


class SMState(enum.Enum):
    """The twelve sm:* labels v1 uses today.

    The enum value is the literal GitHub label (e.g.
    ``"sm:draft"``); :meth:`from_label` is the inverse lookup. Members
    not listed here (anything not in this enum) are not valid sm:*
    states under v3 — the dispatcher rejects them at startup via
    :func:`verify_state_machine`.
    """

    DRAFT = "sm:draft"
    NEEDS_STUDY = "sm:needs_study"
    SELECTED = "sm:selected"
    DESIGNING = "sm:designing"
    DESIGN_REVIEW = "sm:design_review"
    DESIGNED = "sm:designed"
    COMPACTING = "sm:compacting"
    BUILDING = "sm:building"
    REVIEWING = "sm:reviewing"
    DONE = "sm:done"
    REJECTED = "sm:rejected"
    BLOCKED = "sm:blocked"

    @classmethod
    def from_label(cls, label: str) -> "SMState | None":
        """Reverse-lookup: GitHub label string → :class:`SMState`.

        Returns ``None`` for unknown labels rather than raising; the
        dispatcher decides whether an unknown label is a misconfig
        worth surfacing or just a non-SM label to ignore.
        """
        for state in cls:
            if state.value == label:
                return state
        return None

    @property
    def label(self) -> str:
        """The GitHub label string for this state."""
        return self.value


@dataclass(frozen=True)
class StateMeta:
    """Per-state defaults the dispatcher consults at runtime.

    ``terminal`` — sweep pass leaves terminal states alone; no
    outgoing transitions are allowed from terminals.

    ``default_continue_ttl_seconds`` — wall-clock budget between
    continue comments on a non-transitioning issue. After this many
    seconds with no continue (and no other substantive emission), the
    dispatcher escalates to ``sm:blocked``. ``None`` means the state
    is terminal and has no TTL.

    ``role`` — human-readable one-liner used in audit comments and
    surface payloads. Mirrors v1's ``constants.py`` table.

    First-cut TTL numbers from the design doc
    (``inner/designs/2026-05-21-sm-v3-design.md`` § "TTL defaults —
    TODO before locking in"). These will be revised against the
    actual time-in-state distribution from the last 30 days as part
    of Phase 2's data pull.
    """

    terminal: bool
    default_continue_ttl_seconds: int | None
    role: str


# Per-state metadata table. Editing this is a real protocol change —
# pair with a doc update in 2026-05-21-sm-v3-design.md.
STATE_META: Mapping[SMState, StateMeta] = {
    SMState.DRAFT: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=24 * 3600,
        role="Initial — awaiting triage",
    ),
    SMState.NEEDS_STUDY: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=7 * 24 * 3600,
        role="Thinking is investigating",
    ),
    SMState.SELECTED: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=60 * 60,
        role="Approved, awaiting design/build",
    ),
    SMState.DESIGNING: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=2 * 3600,
        role="Thinking is producing design",
    ),
    SMState.DESIGN_REVIEW: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=1 * 3600,
        role="Speaking is reviewing design",
    ),
    SMState.DESIGNED: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=30 * 60,
        role="Design approved, awaiting build",
    ),
    SMState.COMPACTING: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=30 * 60,
        role="Legacy: agent compacting context",
    ),
    SMState.BUILDING: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=60 * 60,
        role="Worker producing PR",
    ),
    SMState.REVIEWING: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=2 * 3600,
        role="PR open, CI + verify + review",
    ),
    SMState.DONE: StateMeta(
        terminal=True,
        default_continue_ttl_seconds=None,
        role="Work shipped successfully",
    ),
    SMState.REJECTED: StateMeta(
        terminal=True,
        default_continue_ttl_seconds=None,
        role="Work rejected or shipped bad",
    ),
    SMState.BLOCKED: StateMeta(
        terminal=False,
        default_continue_ttl_seconds=None,
        role="Paused — re-enterable via [SM] unblock",
    ),
}
