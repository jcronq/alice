"""Adjacency inference for the cozylobe motion-cortex pipeline (Phase 4 of #381).

When two motion sensors covering different rooms fire in quick succession
AND those rooms are NOT listed as adjacent in the cozylobe-cortex vault,
that's evidence of an unknown adjacency (open doorway, hallway shortcut,
or — less interesting — a sensor misconfiguration). The
:class:`AdjacencyInferrer` counts these co-occurrences and promotes a
pair to a confirmed inline-edge entry on the corresponding room notes
once the observation count crosses a threshold.

Design references:

* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` §4.1
  (Inferred adjacency bootstrap rule).
* ``cortex-memory/research/2026-05-26-cozylobe-motion-cortex.md`` §5.2
  (typed-weighted-edges — the inline edge syntax this module writes).

Promotion curve (matches the Phase 4 prompt exactly):

* 5 observations → weight 0.3 (initial promotion)
* 10 → 0.5
* 20 → 0.7
* 50 → 0.9 (asymptote — further observations don't push past this)

Write path: when an observed pair crosses a threshold, this module
appends — or updates, if it already exists — an inline edge of the
shape ``(IS-ADJACENT-TO:<weight>)[[rooms/<Other Room>]]`` to BOTH
rooms' notes. The write is atomic (tempfile + ``os.replace``) and
bumps the ``updated:`` frontmatter timestamp.

The inferrer is sandbox-only: it reads + writes inside ``vault.root``,
never outside. Failure modes (missing room note, malformed
frontmatter, IO error) log a warning and fail-open — the motion
pipeline must NEVER go down because of an adjacency write.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import cortex as cortex_mod
from .motion import MotionEvent


__all__ = [
    "DEFAULT_INFERENCE_WINDOW_S",
    "DEFAULT_PROMOTION_THRESHOLD",
    "PROMOTION_CURVE",
    "AdjacencyInferrer",
    "weight_for_count",
]


log = logging.getLogger(__name__)


# Default window (seconds) within which two consecutive motion events
# in non-adjacent rooms count as evidence of an unknown adjacency.
# Configurable in ``cozylobe-throttle.yaml`` as
# ``adjacency_inference_window_s``. 30s is the design's default — long
# enough to catch a brisk hallway walk between two adjacent rooms,
# short enough to filter out coincidental fires from different people
# in different parts of the house.
DEFAULT_INFERENCE_WINDOW_S: float = 30.0

# Minimum observation count before a hypothesised adjacency gets
# promoted to an inline edge in the vault. Five matches the design's
# bootstrap rule: under five and we're still in the noise floor.
DEFAULT_PROMOTION_THRESHOLD: int = 5

# Promotion curve: (observation_count_threshold, edge_weight). Sorted
# ascending by count. weight_for_count() picks the largest weight
# whose threshold the count meets-or-exceeds; below the smallest
# threshold returns None (no edge written yet).
PROMOTION_CURVE: tuple[tuple[int, float], ...] = (
    (5, 0.3),
    (10, 0.5),
    (20, 0.7),
    (50, 0.9),
)


# Match an existing inline IS-ADJACENT-TO edge so we can update its
# weight in place rather than appending duplicates on every promotion.
# Tolerates the bare ``(IS-ADJACENT-TO)[[...]]`` form (no weight) and
# the explicit ``(IS-ADJACENT-TO:0.3)[[...]]`` form.
_INLINE_EDGE_RE_TEMPLATE = (
    r"\(IS-ADJACENT-TO(?::(?P<weight>\d+(?:\.\d+)?))?\)\[\[rooms/"
    r"(?P<target>{target_escaped})\]\]"
)


def weight_for_count(count: int) -> Optional[float]:
    """Map an observation count → promoted edge weight, or ``None`` if the
    count hasn't reached the first promotion threshold yet.

    Pure function — exposed for tests + sanity checks. The curve is
    sorted ascending, so we walk it once and take the highest matching
    weight. The 0.9 asymptote at 50+ observations is the design ceiling:
    further evidence does not push past it, since the system should
    leave room for explicit operator confirmation to push to 1.0.
    """
    best: Optional[float] = None
    for threshold, weight in PROMOTION_CURVE:
        if count >= threshold:
            best = weight
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Pair helpers


def _normalize_pair(a: str, b: str) -> tuple[str, str]:
    """Return a deterministic ordering of a room pair.

    Adjacency is symmetric — (Kitchen, Hallway) is the same observation
    as (Hallway, Kitchen). Sorting by title gives us a stable key for
    the counter map so both orderings collide on the same slot.
    """
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def _strip_category(target: str) -> str:
    """Drop a leading ``rooms/`` prefix from a frontmatter wikilink target."""
    return target.split("/", 1)[-1] if "/" in target else target


def _vault_adjacent_rooms(room: cortex_mod.Room) -> set[str]:
    """Return the set of rooms ``room`` already considers adjacent.

    Reads BOTH the frontmatter ``adjacent:`` field AND any pre-existing
    inline ``(IS-ADJACENT-TO)[[rooms/Other]]`` edges in the body so a
    pair we've already promoted doesn't keep tripping the counter. The
    body edges are the canonical form (vault tiering design); the
    frontmatter mirror is the legacy onboarding shape.
    """
    out: set[str] = set()
    for target in room.adjacent:
        out.add(_strip_category(target))
    for edge in room.edges:
        if edge.verb == "IS-ADJACENT-TO":
            out.add(_strip_category(edge.target))
    return out


# ---------------------------------------------------------------------------
# Inferrer


@dataclass
class AdjacencyInferrer:
    """Observes consecutive motion-event pairs and promotes unknown
    adjacencies to vault edges once their observation count crosses a
    threshold.

    Single-consumer by design: the wake loop drains motion events
    serially, so no locking is required. The counter persists in-memory
    only — a daemon restart resets it. Phase 5 (out of scope for this
    issue) will persist counts under ``cozylobe-cortex/scratch/`` so
    intermediate evidence survives bounces.

    Attributes:
        vault: A loaded :class:`alice_cozylobe.cortex.Vault` — read-only
            adjacency lookups + room-path resolution for the promotion
            write.
        window_s: Maximum seconds between two consecutive motion events
            for them to count as evidence of an unknown adjacency.
        promotion_threshold: Minimum observation count before the first
            edge is written. Subsequent observations move along
            :data:`PROMOTION_CURVE`.
        counter: Internal map of frozenset({room_a, room_b}) →
            observation_count. Exposed for tests; do not mutate from
            outside.
    """

    vault: cortex_mod.Vault
    window_s: float = DEFAULT_INFERENCE_WINDOW_S
    promotion_threshold: int = DEFAULT_PROMOTION_THRESHOLD
    counter: dict[frozenset[str], int] = field(default_factory=dict)

    # Tracks the last weight we WROTE for a pair so a re-observation
    # past the same threshold doesn't rewrite the room note for no
    # gain. Reset when the daemon restarts (matches counter scope).
    _last_written_weight: dict[frozenset[str], float] = field(default_factory=dict)

    def observe(self, trail: Iterable[MotionEvent]) -> list[tuple[str, str, float]]:
        """Walk consecutive events in ``trail`` and count any non-adjacent
        pair within the window. Promotes pairs that cross a threshold.

        Returns the list of ``(room_a, room_b, weight)`` tuples that
        got promoted on THIS call. Empty list when nothing crossed.

        Skips:
          * pairs where either room is unknown (``room_id is None``)
          * pairs where both events are in the same room (no transition)
          * pairs separated by more than ``window_s``
          * pairs already listed as adjacent in either room's vault note
          * pairs where either room note is missing from the vault
        """
        events = list(trail)
        promoted: list[tuple[str, str, float]] = []
        if len(events) < 2:
            return promoted
        for prev, curr in zip(events, events[1:]):
            pair = self._classify_pair(prev, curr)
            if pair is None:
                continue
            key = frozenset(pair)
            self.counter[key] = self.counter.get(key, 0) + 1
            count = self.counter[key]
            weight = weight_for_count(count)
            if weight is None:
                continue
            last_weight = self._last_written_weight.get(key)
            if last_weight is not None and weight <= last_weight:
                # Already promoted to this weight or higher — skip.
                continue
            room_a, room_b = sorted(pair)
            ok = self._promote_pair(room_a, room_b, weight, count)
            if ok:
                self._last_written_weight[key] = weight
                promoted.append((room_a, room_b, weight))
        return promoted

    # ------------------------------------------------------------------
    # Internals

    def _classify_pair(
        self, prev: MotionEvent, curr: MotionEvent
    ) -> Optional[tuple[str, str]]:
        """Return ``(prev_room, curr_room)`` iff the pair counts as an
        unknown-adjacency observation; ``None`` otherwise.
        """
        if prev.room_id is None or curr.room_id is None:
            return None
        if prev.room_id == curr.room_id:
            return None
        if (curr.timestamp - prev.timestamp) > self.window_s:
            return None
        if (curr.timestamp - prev.timestamp) < 0:
            # Out-of-order events — let it through. Trail snapshots are
            # oldest-first so this shouldn't happen, but a misbehaving
            # producer shouldn't crash the inferrer.
            return None
        room_a = self.vault.rooms.get(f"rooms/{prev.room_id}")
        room_b = self.vault.rooms.get(f"rooms/{curr.room_id}")
        if room_a is None or room_b is None:
            return None
        if curr.room_id in _vault_adjacent_rooms(room_a):
            return None
        if prev.room_id in _vault_adjacent_rooms(room_b):
            return None
        return prev.room_id, curr.room_id

    def _promote_pair(
        self, room_a_title: str, room_b_title: str, weight: float, count: int
    ) -> bool:
        """Write an inline edge between two rooms at the given weight.

        Returns True on success (both room notes were updated), False
        on any IO error or missing room note. Failure is logged at
        WARNING level — fail-open so a write error doesn't take the
        motion pipeline down.
        """
        room_a = self.vault.rooms.get(f"rooms/{room_a_title}")
        room_b = self.vault.rooms.get(f"rooms/{room_b_title}")
        if room_a is None or room_b is None:
            log.warning(
                "cozylobe adjacency: cannot promote (%s ↔ %s, weight=%.2f, n=%d): "
                "room note missing",
                room_a_title,
                room_b_title,
                weight,
                count,
            )
            return False
        ok_a = self._write_inline_edge(room_a, room_b_title, weight)
        ok_b = self._write_inline_edge(room_b, room_a_title, weight)
        if ok_a and ok_b:
            log.info(
                "cozylobe adjacency: promoted %s ↔ %s to weight %.2f (n=%d)",
                room_a_title,
                room_b_title,
                weight,
                count,
            )
        return ok_a and ok_b

    def _write_inline_edge(
        self, room: cortex_mod.Room, other_title: str, weight: float
    ) -> bool:
        """Add or update an ``(IS-ADJACENT-TO:weight)[[rooms/Other]]``
        edge in ``room``'s note body. Atomic.

        If the edge already exists, the weight is updated in place. If
        not, a fresh edge line is appended after the body's heading
        block. The frontmatter ``updated:`` timestamp is bumped to
        current UTC.
        """
        try:
            text = room.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                "cozylobe adjacency: read %s failed: %s", room.path, exc
            )
            return False
        try:
            new_text = self._render_with_edge(text, other_title, weight)
        except Exception as exc:  # noqa: BLE001 - fail-open
            log.warning(
                "cozylobe adjacency: render for %s ↔ %s failed: %s",
                room.title,
                other_title,
                exc,
            )
            return False
        try:
            _atomic_write(room.path, new_text)
        except OSError as exc:
            log.warning(
                "cozylobe adjacency: write %s failed: %s", room.path, exc
            )
            return False
        return True

    @staticmethod
    def _render_with_edge(text: str, other_title: str, weight: float) -> str:
        """Pure: return ``text`` with the edge inserted or updated.

        Body shape (after the closing ``---``):

            # <Room Title>

            <prose...>

            (IS-ADJACENT-TO:<weight>)[[rooms/<other_title>]]  ← inferred
            (IS-ADJACENT-TO:<weight>)[[rooms/<other_title2>]]
            ...

        The frontmatter ``updated:`` line is rewritten to current UTC.
        """
        # Frontmatter split. Tolerant of missing closing fence — in
        # that pathological case we treat the whole file as body so
        # we still write SOMETHING rather than crashing.
        fm_text, body = _split_fm(text)

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        fm_text = _set_frontmatter_updated(fm_text, now_iso)

        edge_re = re.compile(
            _INLINE_EDGE_RE_TEMPLATE.format(
                target_escaped=re.escape(other_title)
            )
        )
        replacement = (
            f"(IS-ADJACENT-TO:{weight:.2f})[[rooms/{other_title}]]"
        )
        if edge_re.search(body):
            body = edge_re.sub(replacement, body, count=1)
        else:
            # Append the edge on its own line, before any trailing
            # newline padding. Markdown-friendly: blank line before
            # the edge so it reads as a list-ish callout.
            stripped = body.rstrip("\n")
            comment = "  <!-- inferred from motion co-occurrence -->"
            body = stripped + "\n\n" + replacement + comment + "\n"

        if fm_text:
            return f"---\n{fm_text}\n---\n{body}"
        return body


# ---------------------------------------------------------------------------
# Atomic IO + frontmatter helpers
#
# Duplicated (not imported) from guesses._atomic_write so motion.py /
# adjacency.py don't grow a runtime dependency on guesses.py's larger
# API surface. Both implementations are the same tempfile-and-rename
# pattern Phase 1's onboarding CLI uses.


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-", suffix=".md"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_FENCE = "---"


def _split_fm(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_text, body)`` for a note.

    ``frontmatter_text`` is the YAML between the two ``---`` fences
    (no fences). ``body`` is everything after the closing fence —
    including any leading blank line. A note without frontmatter
    returns ``("", text)``.
    """
    lines = text.splitlines(keepends=False)
    if not lines or lines[0].strip() != _FENCE:
        return "", text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        return "", text
    fm = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    if body and not body.startswith("\n"):
        body = "\n" + body
    return fm, body.lstrip("\n")


_UPDATED_RE = re.compile(r"^updated:.*$", re.MULTILINE)


def _set_frontmatter_updated(fm_text: str, iso: str) -> str:
    """Set or insert the ``updated:`` field in a frontmatter block."""
    if not fm_text:
        return f"updated: {iso}"
    if _UPDATED_RE.search(fm_text):
        return _UPDATED_RE.sub(f"updated: {iso}", fm_text, count=1)
    return fm_text.rstrip("\n") + f"\nupdated: {iso}"
