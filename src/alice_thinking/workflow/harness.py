"""Harness — deterministic driver around the events.jsonl + projection.json store.

Stateless wrapper that:
- loads the projection and rebuilds it from the event log when stale,
- evaluates ``unblock_when`` / ``requeue_when`` predicates on blocked /
  shelved items and proposes ``transition`` events back to ``opened``,
- selects the highest-priority opened item as the next active candidate,
- validates ``completion_criterion`` for a given item.

The harness PROPOSES events; it does not write them. The caller (the
wake template integration landing in PR 2) is responsible for calling
:func:`alice_thinking.workflow.events_log.append_event` and persisting
the resulting projection.

Predicate evaluators:
- ``time_elapsed``: pure (no I/O).
- ``github_pr_merged``: shells out to ``gh pr view ... --json state``.
  Returns False on subprocess failure / non-zero exit — predicates
  must be safe to evaluate on every wake even when ``gh`` is offline.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from alice_thinking.workflow.events_log import read_events
from alice_thinking.workflow.projection import (
    Projection,
    build_from_events,
    is_fresh,
    load_projection,
    save_projection,
)
from alice_thinking.workflow.schema import (
    EventType,
    State,
    WorkflowEvent,
    WorkflowItem,
)


__all__ = [
    "Harness",
    "evaluate_predicate",
]


_PR_REF_RE = re.compile(r"^[^/\s]+/[^#\s]+#\d+$")


def _parse_iso(ts: str) -> _dt.datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` for UTC."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(ts)


def _ensure_aware(dt: _dt.datetime) -> _dt.datetime:
    """Make a datetime tz-aware (UTC default) so naive vs aware comparisons don't crash."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def evaluate_predicate(predicate: dict[str, Any], now: _dt.datetime) -> bool:
    """Evaluate a single predicate dict against ``now``.

    Supported predicate types:
      - ``time_elapsed``: ``{"type": "time_elapsed", "after": "<ISO>"}`` —
        True iff ``now >= after``.
      - ``github_pr_merged``: ``{"type": "github_pr_merged", "pr": "owner/repo#N"}`` —
        True iff ``gh pr view`` reports state ``MERGED``. False on any
        subprocess failure (non-zero exit, gh not installed, network
        unreachable).

    Unknown predicate types return False (silent — predicates fire on
    every wake, so a noisy raise would spam the surface channel).
    """
    ptype = predicate.get("type")
    if ptype == "time_elapsed":
        after_raw = predicate.get("after")
        if not isinstance(after_raw, str):
            return False
        try:
            after = _ensure_aware(_parse_iso(after_raw))
        except ValueError:
            return False
        return _ensure_aware(now) >= after
    if ptype == "github_pr_merged":
        pr_ref = predicate.get("pr")
        if not isinstance(pr_ref, str) or not _PR_REF_RE.match(pr_ref):
            return False
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_ref, "--json", "state"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        return str(data.get("state", "")).upper() == "MERGED"
    return False


@dataclass
class Harness:
    """Deterministic driver around the thinking-workflow store.

    Constructed with ``mind_dir`` (typically ``~/alice-mind``); the
    event log and projection paths derive from there per the design.
    Stateless across calls — every method takes / returns the
    projection explicitly so the wake driver can compose operations
    cleanly.
    """

    mind_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.mind_dir / "inner" / "state" / "thinking-workflow"

    @property
    def events_log_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def projection_path(self) -> Path:
        return self.state_dir / "projection.json"

    # ------------------------------------------------------------------
    # Projection management
    # ------------------------------------------------------------------

    def refresh(self) -> Projection:
        """Return an up-to-date projection.

        Loads the cached projection.json; if it's missing or stale
        relative to the event log, rebuilds from the log and writes the
        fresh snapshot. Returns the projection in memory either way.
        """
        projection = load_projection(self.projection_path)
        if not is_fresh(projection, self.events_log_path):
            projection = build_from_events(read_events(self.events_log_path))
            save_projection(self.projection_path, projection)
        return projection

    # ------------------------------------------------------------------
    # Predicate evaluation
    # ------------------------------------------------------------------

    def evaluate_predicates(self, now: _dt.datetime) -> list[WorkflowEvent]:
        """Propose ``transition`` events for blocked / shelved items whose
        predicate now fires.

        Returns events with ``event_id=0`` (the caller assigns the real
        id via :func:`append_event`), ``by="harness:predicate"``, and
        ``ts=now.isoformat()``. Does NOT write — proposal only.
        """
        projection = self.refresh()
        proposed: list[WorkflowEvent] = []
        ts = now.isoformat()
        for item in projection.items.values():
            if item.state == State.BLOCKED and item.unblock_when:
                if evaluate_predicate(item.unblock_when, now):
                    proposed.append(
                        WorkflowEvent(
                            event_id=0,
                            ts=ts,
                            event_type=EventType.TRANSITION,
                            item_id=item.id,
                            from_state=State.BLOCKED,
                            to_state=State.OPENED,
                            by="harness:predicate",
                            reason="unblock_when predicate satisfied",
                            evidence={"predicate": item.unblock_when},
                        )
                    )
            elif item.state == State.SHELVED and item.requeue_when:
                if evaluate_predicate(item.requeue_when, now):
                    proposed.append(
                        WorkflowEvent(
                            event_id=0,
                            ts=ts,
                            event_type=EventType.TRANSITION,
                            item_id=item.id,
                            from_state=State.SHELVED,
                            to_state=State.OPENED,
                            by="harness:predicate",
                            reason="requeue_when predicate satisfied",
                            evidence={"predicate": item.requeue_when},
                        )
                    )
        return proposed

    # ------------------------------------------------------------------
    # Active-item selection
    # ------------------------------------------------------------------

    def select_next_active(self, projection: Projection) -> Optional[WorkflowItem]:
        """Pick the highest-priority opened item, FIFO on tie.

        Higher integer priority wins (PR 1 decision — see PR body /
        module docstring). Tie-break: oldest ``opened_at`` wins.
        Returns None when the queue has no ``opened`` items.
        """
        opened = [
            item for item in projection.items.values() if item.state == State.OPENED
        ]
        if not opened:
            return None
        opened.sort(key=lambda i: (-i.priority, i.opened_at))
        return opened[0]

    def propose_active_transition(
        self, projection: Projection, now: _dt.datetime
    ) -> Optional[WorkflowEvent]:
        """Propose an ``opened → active`` transition for the head of the queue.

        Returns None when an item is already ``active`` (the single-active
        invariant is preserved), or when no ``opened`` item exists.
        """
        for item in projection.items.values():
            if item.state == State.ACTIVE:
                return None
        candidate = self.select_next_active(projection)
        if candidate is None:
            return None
        return WorkflowEvent(
            event_id=0,
            ts=now.isoformat(),
            event_type=EventType.TRANSITION,
            item_id=candidate.id,
            from_state=State.OPENED,
            to_state=State.ACTIVE,
            by="harness:auto-select",
            reason="head of priority queue",
        )

    # ------------------------------------------------------------------
    # Completion validation
    # ------------------------------------------------------------------

    def validate_completion(
        self, item: WorkflowItem, mind_dir: Optional[Path] = None
    ) -> bool:
        """Check the item's ``completion_criterion`` against the world.

        Supported criterion types:
          - ``research_note_resolves``: scan
            ``<mind_dir>/cortex-memory/research/*.md`` for a frontmatter
            key ``resolves_workflow_item: <item.id>``.
          - ``pr_merged``: shell ``gh pr view <pr> --json state``;
            True iff state is ``MERGED``.

        Returns False when the criterion is missing, the type is
        unknown, or the validator can't reach its evidence source.
        """
        if item.completion_criterion is None:
            return False
        base = mind_dir if mind_dir is not None else self.mind_dir
        ctype = item.completion_criterion.get("type")
        if ctype == "research_note_resolves":
            return _research_note_resolves(item, base)
        if ctype == "pr_merged":
            pr_ref = item.completion_criterion.get("pr")
            if not isinstance(pr_ref, str) or not _PR_REF_RE.match(pr_ref):
                return False
            return evaluate_predicate(
                {"type": "github_pr_merged", "pr": pr_ref}, _dt.datetime.now(_dt.timezone.utc)
            )
        return False


def _research_note_resolves(item: WorkflowItem, mind_dir: Path) -> bool:
    """True iff any ``cortex-memory/research/*.md`` carries
    ``resolves_workflow_item: <item.id>`` in its frontmatter.

    Frontmatter is the YAML block between the first two ``---`` lines.
    We don't parse YAML — a literal-line match is enough for V1, and
    keeps the dep surface to stdlib.
    """
    research_dir = mind_dir / "cortex-memory" / "research"
    if not research_dir.is_dir():
        return False
    needle_key = "resolves_workflow_item"
    target = item.id
    for path in research_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter = _extract_frontmatter(text)
        if frontmatter is None:
            continue
        for raw_line in frontmatter.splitlines():
            line = raw_line.strip()
            if not line.startswith(needle_key):
                continue
            # Match "resolves_workflow_item: ITEM-1" or "resolves_workflow_item:ITEM-1"
            _, _, value = line.partition(":")
            if value.strip().strip('"').strip("'") == target:
                return True
    return False


def _extract_frontmatter(text: str) -> Optional[str]:
    """Return the YAML frontmatter between the first two ``---`` markers.

    Tolerates a leading BOM or blank lines before the opening fence.
    Returns None if no closing fence is found (treat as no frontmatter).
    """
    lines = text.splitlines()
    # Find opening fence — must be the first non-empty line.
    start: Optional[int] = None
    for idx, line in enumerate(lines):
        if line.strip() == "":
            continue
        if line.strip() == "---":
            start = idx
        break
    if start is None:
        return None
    # Find closing fence.
    for idx in range(start + 1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[start + 1 : idx])
    return None
