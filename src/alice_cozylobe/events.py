"""CozyHem event dataclass — wire-format-agnostic.

Mirrors :class:`alice_speaking.internal.cozyhem.CozyHemEvent` deliberately;
both packages consume the same SSE stream and the wire shape is owned by
``cozyhem-engine``. We keep a local copy here rather than importing the
speaking variant so the cozylobe doesn't take a hard dependency on
alice_speaking — the two are peer hemispheres, not a layered stack.
A shared events module under ``core`` is a fair Phase 2 refactor once
both consumers stabilize; out of scope for the walking skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass, field


__all__ = ["CozyHemEvent"]


@dataclass(frozen=True)
class CozyHemEvent:
    """One event lifted off CozyHem's SSE stream.

    Attributes:
        kind: The SSE ``event:`` line value (e.g. ``"doorbell_pressed"``,
            ``"entity:update"``, ``"motion_detected"``). Maps to the 12
            event types in the inventory note.
        entity_id: The CozyHem entity id this event came from
            (e.g. ``"light.living_room"``). Empty string when the upstream
            event isn't tied to a specific entity.
        payload: Parsed JSON ``data:`` body. Empty dict on parse failure;
            the producer logs a warning in that path so upstream
            regressions are visible.
        received_at: ``time.time()`` at the moment the producer finished
            parsing the frame. Useful for staleness checks in handlers
            that fire after a queue buildup.
    """

    kind: str
    entity_id: str
    payload: dict = field(default_factory=dict)
    received_at: float = 0.0
