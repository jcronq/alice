"""Aggregators — turn raw events into wakes, turns, and interaction lineage."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .sources import UnifiedEvent


@dataclass
class Wake:
    wake_id: str
    start_ts: float
    end_ts: float | None
    status: str   # running | ended | timeout | exception
    model: str | None
    duration_ms: int | None
    total_cost_usd: float | None
    tools: list[str] = field(default_factory=list)
    events: list[UnifiedEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wake_id": self.wake_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "status": self.status,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "total_cost_usd": self.total_cost_usd,
            "tools": self.tools,
            "event_count": len(self.events),
        }


@dataclass
class Turn:
    turn_id: str
    start_ts: float
    end_ts: float | None
    kind: str        # signal | surface | emergency
    sender_name: str | None
    surface_id: str | None
    emergency_id: str | None
    inbound: str | None
    outbound: str | None
    error: str | None
    duration_ms: int | None
    total_cost_usd: float | None
    tools: list[str] = field(default_factory=list)
    events: list[UnifiedEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "kind": self.kind,
            "sender_name": self.sender_name,
            "surface_id": self.surface_id,
            "emergency_id": self.emergency_id,
            "inbound": self.inbound,
            "outbound": self.outbound,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "total_cost_usd": self.total_cost_usd,
            "tools": self.tools,
            "event_count": len(self.events),
        }


def group_wakes(events: list[UnifiedEvent]) -> list[Wake]:
    thinking = [e for e in events if e.hemisphere == "thinking"]
    by_id: dict[str, Wake] = {}
    for ev in thinking:
        if ev.correlation_id is None:
            continue
        wid = ev.correlation_id
        wake = by_id.get(wid)
        if wake is None:
            wake = Wake(
                wake_id=wid,
                start_ts=ev.ts,
                end_ts=None,
                status="running",
                model=None,
                duration_ms=None,
                total_cost_usd=None,
            )
            by_id[wid] = wake
        wake.events.append(ev)
        d = ev.detail
        if ev.kind == "wake_start":
            wake.model = d.get("model")
        elif ev.kind == "tool_use":
            name = d.get("name")
            if name and name not in wake.tools:
                wake.tools.append(name)
        elif ev.kind == "result":
            wake.duration_ms = d.get("duration_ms")
            wake.total_cost_usd = d.get("total_cost_usd")
        elif ev.kind == "wake_end":
            wake.end_ts = ev.ts
            wake.status = "ended"
        elif ev.kind == "timeout":
            wake.end_ts = ev.ts
            wake.status = "timeout"
        elif ev.kind == "exception":
            wake.end_ts = ev.ts
            wake.status = "exception"
    return sorted(by_id.values(), key=lambda w: w.start_ts)


def group_turns(events: list[UnifiedEvent]) -> list[Turn]:
    speaking = [e for e in events if e.hemisphere == "speaking" and e.correlation_id]
    by_id: dict[str, Turn] = {}
    for ev in speaking:
        tid = ev.correlation_id
        turn = by_id.get(tid)
        if turn is None:
            turn = Turn(
                turn_id=tid,
                start_ts=ev.ts,
                end_ts=None,
                kind="unknown",
                sender_name=None,
                surface_id=None,
                emergency_id=None,
                inbound=None,
                outbound=None,
                error=None,
                duration_ms=None,
                total_cost_usd=None,
            )
            by_id[tid] = turn
        turn.events.append(ev)
        d = ev.detail
        if ev.kind == "signal_turn_start":
            turn.kind = "signal"
            turn.start_ts = ev.ts
            turn.sender_name = d.get("sender_name")
            turn.inbound = d.get("inbound")
        elif ev.kind == "signal_turn_end":
            turn.end_ts = ev.ts
            turn.outbound = d.get("outbound")
            turn.error = d.get("error")
            turn.duration_ms = d.get("duration_ms")
        elif ev.kind == "surface_dispatch":
            turn.kind = "surface"
            turn.start_ts = ev.ts
            turn.surface_id = d.get("surface_id")
            turn.inbound = d.get("body")
        elif ev.kind == "surface_turn_end":
            turn.end_ts = ev.ts
            turn.duration_ms = d.get("duration_ms")
            turn.error = d.get("error")
        elif ev.kind == "emergency_dispatch":
            turn.kind = "emergency"
            turn.start_ts = ev.ts
            turn.emergency_id = d.get("emergency_id")
            turn.inbound = d.get("body")
        elif ev.kind == "emergency_voiced":
            turn.outbound = d.get("text")
        elif ev.kind == "emergency_turn_end":
            turn.end_ts = ev.ts
            turn.duration_ms = d.get("duration_ms")
        elif ev.kind == "tool_use":
            name = d.get("name")
            if name and name not in turn.tools:
                turn.tools.append(name)
        elif ev.kind == "result":
            turn.total_cost_usd = d.get("total_cost_usd")
    return sorted(by_id.values(), key=lambda t: t.start_ts)


# ---------------------------------------------------------------------------
# Interaction DAG


@dataclass
class InteractionNode:
    id: str
    kind: str     # wake | turn | surface | emergency | note | thought | directive
    label: str
    ts: float
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class InteractionEdge:
    source: str
    target: str
    kind: str     # reads | writes | consumes | surfaces | replies | voices


def build_interaction_graph(
    events: list[UnifiedEvent],
    wakes: list[Wake],
    turns: list[Turn],
) -> tuple[list[InteractionNode], list[InteractionEdge]]:
    nodes: dict[str, InteractionNode] = {}
    edges: list[InteractionEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(n: InteractionNode) -> None:
        nodes.setdefault(n.id, n)

    def add_edge(source: str, target: str, kind: str) -> None:
        key = (source, target, kind)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(InteractionEdge(source=source, target=target, kind=kind))

    # Directive is a single well-known node — every wake reads it.
    add_node(InteractionNode(id="directive", kind="directive", label="directive.md", ts=0.0))

    # Wakes + thoughts.
    for wake in wakes:
        nid = f"wake::{wake.wake_id}"
        add_node(
            InteractionNode(
                id=nid,
                kind="wake",
                label=f"wake {wake.wake_id[-6:]}",
                ts=wake.start_ts,
                meta={"status": wake.status, "tools": wake.tools},
            )
        )
        add_edge("directive", nid, "reads")

    # Turns.
    for turn in turns:
        nid = f"turn::{turn.turn_id}"
        label = f"{turn.kind}-turn"
        if turn.sender_name:
            label = f"{turn.sender_name} → alice"
        add_node(
            InteractionNode(
                id=nid,
                kind="turn",
                label=label,
                ts=turn.start_ts,
                meta={"kind": turn.kind, "sender": turn.sender_name, "tools": turn.tools},
            )
        )

    # Filesystem artifacts → connect them to creators/consumers.
    for ev in events:
        if ev.hemisphere != "inner":
            continue
        if ev.kind in ("surface_pending", "surface_resolved"):
            nid = f"surface::{ev.correlation_id}"
            add_node(
                InteractionNode(
                    id=nid,
                    kind="surface",
                    label=ev.correlation_id or "surface",
                    ts=ev.ts,
                    meta=ev.detail,
                )
            )
            # Thinking surfaces emerge from a wake near the same time.
            wake = _nearest_by_ts(wakes, ev.ts, before=True, within=600)
            if wake:
                add_edge(f"wake::{wake.wake_id}", nid, "surfaces")
            # A turn of kind=surface with matching surface_id consumes it.
            for t in turns:
                if t.kind == "surface" and t.surface_id == ev.correlation_id:
                    add_edge(nid, f"turn::{t.turn_id}", "consumes")
        elif ev.kind in ("emergency_pending", "emergency_resolved"):
            nid = f"emergency::{ev.correlation_id}"
            add_node(
                InteractionNode(
                    id=nid,
                    kind="emergency",
                    label=ev.correlation_id or "emergency",
                    ts=ev.ts,
                    meta=ev.detail,
                )
            )
            for t in turns:
                if t.kind == "emergency" and t.emergency_id == ev.correlation_id:
                    add_edge(nid, f"turn::{t.turn_id}", "voices")
        elif ev.kind in ("note_pending", "note_consumed"):
            nid = f"note::{ev.correlation_id}"
            add_node(
                InteractionNode(
                    id=nid,
                    kind="note",
                    label=ev.correlation_id or "note",
                    ts=ev.ts,
                    meta=ev.detail,
                )
            )
            # A speaking turn around the same time likely wrote the note.
            turn = _nearest_turn_by_ts(turns, ev.ts, within=600)
            if turn:
                add_edge(f"turn::{turn.turn_id}", nid, "writes")
            # The next wake after ev.ts likely consumed it.
            wake = _nearest_by_ts(wakes, ev.ts, before=False, within=3600)
            if wake:
                add_edge(nid, f"wake::{wake.wake_id}", "consumes")
        elif ev.kind == "thought_written":
            nid = f"thought::{ev.correlation_id}"
            add_node(
                InteractionNode(
                    id=nid,
                    kind="thought",
                    label=ev.correlation_id or "thought",
                    ts=ev.ts,
                    meta=ev.detail,
                )
            )
            wake = _nearest_by_ts(wakes, ev.ts, before=True, within=3600)
            if wake:
                add_edge(f"wake::{wake.wake_id}", nid, "writes")

    return list(nodes.values()), edges


def _nearest_by_ts(
    wakes: list[Wake], ts: float, *, before: bool, within: float
) -> Wake | None:
    best: Wake | None = None
    best_delta: float | None = None
    for w in wakes:
        delta = ts - w.start_ts if before else w.start_ts - ts
        if delta < 0 or delta > within:
            continue
        if best_delta is None or delta < best_delta:
            best = w
            best_delta = delta
    return best


def _nearest_turn_by_ts(turns: list[Turn], ts: float, *, within: float) -> Turn | None:
    best: Turn | None = None
    best_delta: float | None = None
    for t in turns:
        if t.end_ts is None:
            continue
        delta = ts - t.end_ts
        if delta < 0 or delta > within:
            continue
        if best_delta is None or delta < best_delta:
            best = t
            best_delta = delta
    return best


# ---------------------------------------------------------------------------
# Activity buckets


def activity_buckets(
    events: list[UnifiedEvent],
    *,
    resolution_seconds: int,
    window_seconds: int,
    now_ts: float,
) -> list[dict[str, Any]]:
    """Returns a list of buckets covering [now - window, now] at resolution.

    Each bucket: {ts, thinking_wakes, signal_turns, surfaces, emergencies, notes}.
    """
    start = now_ts - window_seconds
    bucket_count = window_seconds // resolution_seconds
    buckets: list[dict[str, Any]] = [
        {
            "ts": start + i * resolution_seconds,
            "thinking_wakes": 0,
            "signal_turns": 0,
            "surfaces": 0,
            "emergencies": 0,
            "notes": 0,
            "tool_uses": 0,
        }
        for i in range(bucket_count)
    ]
    for ev in events:
        if ev.ts < start or ev.ts > now_ts:
            continue
        idx = int((ev.ts - start) // resolution_seconds)
        if idx < 0 or idx >= bucket_count:
            continue
        b = buckets[idx]
        if ev.kind == "wake_start":
            b["thinking_wakes"] += 1
        elif ev.kind == "signal_turn_start":
            b["signal_turns"] += 1
        elif ev.kind in ("surface_pending", "surface_resolved"):
            b["surfaces"] += 1
        elif ev.kind in ("emergency_pending", "emergency_resolved"):
            b["emergencies"] += 1
        elif ev.kind in ("note_pending", "note_consumed"):
            b["notes"] += 1
        elif ev.kind == "tool_use":
            b["tool_uses"] += 1
    return buckets


def tool_histogram(events: list[UnifiedEvent]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for ev in events:
        if ev.kind != "tool_use":
            continue
        name = ev.detail.get("name") or "?"
        counts[(ev.hemisphere, name)] += 1
    out = [
        {"hemisphere": h, "name": n, "count": c}
        for (h, n), c in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return out
