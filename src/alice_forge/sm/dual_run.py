"""Dual-run logger for the v1 → v3 per-state cutover.

During Phase 2's per-state handler ports, v3 handlers ran in dry-
run mode and logged predicted actions to ``sm-v3-predicted.jsonl``;
v1 logged the corresponding actual action to ``sm-v1-actual.jsonl``.
A nightly diff job compared the two and reported divergences. A
state's flag flipped from ``v1-only`` to ``v3`` only after seven
consecutive days with zero divergences.

**Phase 4 (#301) rename:** v3 is now authoritative for transitions
in production. The dispatcher writes v3's applied results to
``sm-v3-actual.jsonl`` (lane ``"v3-actual"``) for one month so the
previous diff job's output still parses while operators verify
parity. The ``"v1-actual"`` lane is unchanged.

This module just provides the JSONL writer. The diff job lives
outside the dispatcher's hot path.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any

from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    EmitParseError,
    HandlerResult,
    NoProgress,
    SideEffect,
    Transition,
)
from alice_forge.sm.states import SMState


@dataclass(frozen=True)
class DualRunLogEntry:
    """One line in the predicted / actual JSONL file.

    Cycle ID groups a v1-actual + v3-actual (formerly v3-predicted;
    Phase 4 #301 rename) pair so the diff job can match them. Time
    + repo + issue_number identify which issue the entry is about.
    Lane = ``"v1-actual"`` or ``"v3-actual"``.

    ``action_kind`` is the variant name (``"Transition"`` /
    ``"Continue"`` / ``"SideEffect"`` / etc.). ``payload`` is the
    variant's field dict.
    """

    cycle_id: str
    ts: str
    lane: str
    repo: str
    issue_number: int
    state: str
    action_kind: str
    payload: dict[str, Any]


def render_result(result: HandlerResult) -> tuple[str, dict[str, Any]]:
    """Turn a :class:`HandlerResult` into ``(kind, payload)`` for logging.

    Discriminator-on-type. The kind matches the dataclass name so
    the diff job can group cleanly.
    """
    if isinstance(result, Transition):
        return "Transition", {
            "target": result.target.value,
            "reason": result.reason,
            "art_swap": result.art_swap,
            "metadata": dict(result.metadata),
        }
    if isinstance(result, Continue):
        return "Continue", {
            "reason": result.reason,
            "findings": result.findings,
        }
    if isinstance(result, SideEffect):
        return "SideEffect", {
            "name": result.name,
            "body": result.body[:200],  # truncate; full body is on the issue
            "ttl_seconds": result.ttl_seconds,
            "metadata": dict(result.metadata),
        }
    if isinstance(result, NoProgress):
        return "NoProgress", {
            "duplicate_reason": result.duplicate_reason,
            "duplicate_of_emitted_at": result.duplicate_of_emitted_at,
        }
    if isinstance(result, BlockedByTTL):
        return "BlockedByTTL", {
            "state_ttl_seconds": result.state_ttl_seconds,
        }
    if isinstance(result, EmitParseError):
        return "EmitParseError", {
            "verb": result.verb,
            "reason": result.reason,
        }
    raise TypeError(f"unknown HandlerResult variant: {type(result).__name__}")


def log_entry(
    path: pathlib.Path,
    *,
    cycle_id: str,
    lane: str,
    repo: str,
    issue_number: int,
    state: SMState | str,
    result: HandlerResult | None,
    extra: dict[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> None:
    """Append one JSONL line.

    ``result`` may be None for v1-actual entries where the v1 path
    did nothing (the "silent return" pattern). The diff job treats
    a None entry as ``action_kind = "SilentNoOp"`` to compare against
    v3's explicit BlockedByTTL / Continue / NoProgress decisions on
    the same cycle.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    state_label = state.value if isinstance(state, SMState) else state

    if result is None:
        kind = "SilentNoOp"
        payload: dict[str, Any] = {}
    else:
        kind, payload = render_result(result)
    if extra:
        payload = {**payload, "_extra": extra}

    entry = DualRunLogEntry(
        cycle_id=cycle_id,
        ts=now.isoformat(),
        lane=lane,
        repo=repo,
        issue_number=issue_number,
        state=state_label,
        action_kind=kind,
        payload=payload,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(asdict(entry), default=str) + "\n")
