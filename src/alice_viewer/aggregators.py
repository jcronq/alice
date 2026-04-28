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


@dataclass
class Run:
    """A unified span of agentic work — one thinking wake or one speaking turn.

    Wraps the existing Wake / Turn models behind one shape so the timeline
    UI can render them in a single chronological list. The detail_url
    points at the existing per-wake / per-turn detail page.
    """

    run_id: str
    kind: str           # thinking-wake | signal-turn | surface-turn | emergency-turn
    hemisphere: str     # thinking | speaking
    start_ts: float
    end_ts: float | None
    status: str         # running | ended | errored | timeout | exception
    summary: str        # one-line label for the row
    duration_ms: int | None
    cost_usd: float | None
    model: str | None
    tools: list[str]
    sender_name: str | None
    inbound: str | None
    outbound: str | None
    error: str | None
    detail_url: str
    events: list[UnifiedEvent] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.end_ts is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "hemisphere": self.hemisphere,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "status": self.status,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
            "model": self.model,
            "tools": self.tools,
            "sender_name": self.sender_name,
            "inbound": self.inbound,
            "outbound": self.outbound,
            "error": self.error,
            "detail_url": self.detail_url,
            "is_running": self.is_running,
            "event_count": len(self.events),
        }


def group_runs(events: list[UnifiedEvent]) -> list[Run]:
    """Build a unified, newest-first list of Runs.

    Each thinking Wake and each speaking Turn becomes one Run. Click-
    through goes to the existing detail pages (``/wakes/<id>``,
    ``/turns/<id>``) — the Run is just a row + summary in the timeline.

    For ended thinking wakes, prefers the cached Haiku-generated single-
    sentence summary; on cache miss, kicks off background generation
    and returns a fallback this iteration.
    """
    # Lazy import — keeps the aggregator pure (testable without the
    # viewer's run_summary side effects).
    from . import run_summary

    runs: list[Run] = []
    for w in group_wakes(events):
        summary = summarize_wake(w, run_summary)
        runs.append(
            Run(
                run_id=w.wake_id,
                kind="thinking-wake",
                hemisphere="thinking",
                start_ts=w.start_ts,
                end_ts=w.end_ts,
                status=w.status,
                summary=summary,
                duration_ms=w.duration_ms,
                cost_usd=w.total_cost_usd,
                model=w.model,
                tools=list(w.tools),
                sender_name=None,
                inbound=None,
                outbound=None,
                error=None,
                detail_url=f"/wakes/{w.wake_id}",
                events=list(w.events),
            )
        )
    for t in group_turns(events):
        summary = summarize_turn(t)
        status = "errored" if t.error else ("ended" if t.end_ts else "running")
        runs.append(
            Run(
                run_id=t.turn_id,
                kind=f"{t.kind}-turn",
                hemisphere="speaking",
                start_ts=t.start_ts,
                end_ts=t.end_ts,
                status=status,
                summary=summary,
                duration_ms=t.duration_ms,
                cost_usd=t.total_cost_usd,
                model=None,
                tools=list(t.tools),
                sender_name=t.sender_name,
                inbound=t.inbound,
                outbound=t.outbound,
                error=t.error,
                detail_url=f"/turns/{t.turn_id}",
                events=list(t.events),
            )
        )
    runs.sort(key=lambda r: r.start_ts, reverse=True)
    return runs


def summarize_wake(w: Wake, run_summary_module=None) -> str:
    """Compact one-line label for a thinking wake row.

    Running wakes get ``running…`` (no point summarizing a moving
    target). Ended wakes prefer the Haiku-generated cached summary; on
    cache miss, schedule one and return a fallback for this render.
    """
    if w.end_ts is None:
        return "running…"

    if run_summary_module is not None:
        cached = run_summary_module.read(w.wake_id)
        if cached:
            return cached
        # Fire-and-forget — fills cache for the next render.
        try:
            run_summary_module.schedule(w.wake_id, w.events)
        except Exception:  # noqa: BLE001
            pass

    # Fallback: first non-empty assistant_text or thinking block.
    for ev in w.events:
        if ev.kind in ("assistant_text", "thinking"):
            text = (ev.detail.get("text") or "").strip()
            if text:
                return text.replace("\n", " ")[:160]
    if w.tools:
        return f"used {', '.join(w.tools[:6])}"
    return "(no activity captured)"


def summarize_turn(t: Turn) -> str:
    """Compact one-line label for a speaking turn row."""
    if t.kind == "signal":
        sender = t.sender_name or "?"
        inbound = (t.inbound or "").replace("\n", " ").strip()
        if not inbound:
            return f"{sender} → (image / attachment)"
        return f"{sender} → {inbound[:160]}"
    if t.kind == "surface":
        return f"surface · {t.surface_id or '?'}"
    if t.kind == "emergency":
        return f"EMERGENCY · {t.emergency_id or '?'}"
    return t.kind


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
# Token-usage stats — sidebar metrics


def _usage_breakdown(usage: dict[str, Any]) -> dict[str, int]:
    """Pull the four headline token counts off an SDK `result.usage` payload."""
    return {
        "input": int(usage.get("input_tokens") or 0),
        "cache_creation": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def _last_iteration(usage: dict[str, Any]) -> dict[str, int] | None:
    """The trailing iteration's tokens — closest analog to `/context` after the run."""
    iters = usage.get("iterations") or []
    if not iters:
        return None
    last = iters[-1]
    return {
        "input": int(last.get("input_tokens") or 0),
        "cache_creation": int(last.get("cache_creation_input_tokens") or 0),
        "cache_read": int(last.get("cache_read_input_tokens") or 0),
        "output": int(last.get("output_tokens") or 0),
    }


def latest_speaking_usage(events: list[UnifiedEvent]) -> dict[str, Any] | None:
    """Token snapshot from the most recent speaking `result` event."""
    latest: UnifiedEvent | None = None
    for ev in events:
        if ev.hemisphere != "speaking" or ev.kind != "result":
            continue
        if latest is None or ev.ts > latest.ts:
            latest = ev
    if latest is None:
        return None
    usage = latest.detail.get("usage") or {}
    totals = _usage_breakdown(usage)
    last_iter = _last_iteration(usage)
    iters = usage.get("iterations") or []
    return {
        "ts": latest.ts,
        "turn_id": latest.correlation_id,
        "totals": totals,
        "context": last_iter,           # snapshot at end of run (≈ /context output)
        "iterations": len(iters),
        "model": (latest.detail or {}).get("model"),
    }


def thinking_usage_average(
    events: list[UnifiedEvent], *, window_seconds: float = 86400, now_ts: float | None = None
) -> dict[str, Any] | None:
    """Average the four token counts over thinking results in the last window."""
    import time as _time
    cutoff = (now_ts if now_ts is not None else _time.time()) - window_seconds
    results = [
        e for e in events
        if e.hemisphere == "thinking" and e.kind == "result" and e.ts >= cutoff
    ]
    if not results:
        return None
    totals = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}
    for ev in results:
        b = _usage_breakdown(ev.detail.get("usage") or {})
        for k in totals:
            totals[k] += b[k]
    n = len(results)
    return {
        "samples": n,
        "window_hours": int(window_seconds // 3600),
        "avg": {k: totals[k] // n for k in totals},
    }


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


# ---------------------------------------------------------------------------
# Conversation Arcs — the /interactions view's primary unit.
#
# A ConversationArc is one Speaking turn presented as a 3-node story:
#   stimulus  → what triggered Speaking (inbound Signal text, or a surface
#               body if Thinking poked Speaking, or an emergency body)
#   thinking  → Speaking's first reasoning block in the turn (or for surface
#               turns, the originating thinking wake's surface body)
#   response  → what Speaking actually sent back (outbound text)
#
# This is intentionally simpler than the connected-component approach below
# (which lives in `build_interaction_arcs` for the graph view). One turn ==
# one card. Per the owner 2026-04-26: track each interaction start to
# finish, with the originating note, the thinking, and the response visible
# at a glance. Implementation follows the design at
# `cortex-memory/research/2026-04-26-interactions-tab-arc-design.md`.


@dataclass
class ConversationArc:
    arc_id: str          # the underlying turn_id
    kind: str            # "signal" | "surface" | "emergency"
    ts: float            # turn start
    end_ts: float | None
    duration_ms: int | None
    sender: str | None   # principal display name for signal; "Thinking Alice" for surface
    stimulus: str | None  # inbound text or surface body (full)
    stimulus_kind: str    # "signal" | "surface" | "emergency"
    surface_id: str | None  # originating surface filename, if any
    thinking_excerpt: str | None  # first thinking block text in the turn
    thinking_count: int   # number of thinking blocks captured
    response: str | None  # turn.outbound
    tools: list[str]
    error: str | None
    has_response: bool
    detail_url: str       # /turns/{turn_id}
    surface_event: UnifiedEvent | None  # surface_pending/_resolved if matched
    # Raw turn for drilldown
    turn: Turn

    def to_dict(self) -> dict[str, Any]:
        return {
            "arc_id": self.arc_id,
            "kind": self.kind,
            "ts": self.ts,
            "end_ts": self.end_ts,
            "duration_ms": self.duration_ms,
            "sender": self.sender,
            "stimulus": self.stimulus,
            "stimulus_kind": self.stimulus_kind,
            "surface_id": self.surface_id,
            "thinking_excerpt": self.thinking_excerpt,
            "thinking_count": self.thinking_count,
            "response": self.response,
            "tools": list(self.tools),
            "error": self.error,
            "has_response": self.has_response,
            "detail_url": self.detail_url,
            "surface_filename": (
                self.surface_event.detail.get("filename")
                if self.surface_event else None
            ),
        }


def group_arcs(
    events: list[UnifiedEvent],
    *,
    turns: list[Turn] | None = None,
) -> list[ConversationArc]:
    """Build one ConversationArc per Speaking turn, newest first.

    Joins each turn with its triggering surface (via `turn.surface_id` ==
    surface event's `correlation_id`, both being the full `path.name`
    including the `.md` suffix). Extracts the first thinking block in the
    turn as the visible "what Speaking was reasoning about" snippet.

    Pass `turns=` to reuse a pre-grouped list — the route enriches turns
    with outbound text from `speaking-turns.jsonl` before arc-building.
    """
    if turns is None:
        turns = group_turns(events)

    # Surface map: correlation_id (filename) → surface event. Prefer the
    # resolved (.handled) version when both exist; that's the one with the
    # trailer the owner cares about.
    surface_map: dict[str, UnifiedEvent] = {}
    for ev in events:
        if ev.kind not in ("surface_pending", "surface_resolved"):
            continue
        if not ev.correlation_id:
            continue
        cur = surface_map.get(ev.correlation_id)
        if cur is None:
            surface_map[ev.correlation_id] = ev
        elif cur.kind == "surface_pending" and ev.kind == "surface_resolved":
            surface_map[ev.correlation_id] = ev

    arcs: list[ConversationArc] = []
    for turn in turns:
        # Thinking blocks: the assistant's reasoning emitted during this
        # turn. Speaking.log records `thinking` events with detail.text.
        thinking_blocks: list[str] = []
        for ev in turn.events:
            if ev.kind != "thinking":
                continue
            text = (ev.detail.get("text") or "").strip()
            if text:
                thinking_blocks.append(text)
        thinking_excerpt = thinking_blocks[0] if thinking_blocks else None

        # Surface lookup for surface-kind turns. Try the literal id first,
        # then with `.md` appended in case some logger variant strips it.
        surface_ev: UnifiedEvent | None = None
        if turn.surface_id:
            surface_ev = surface_map.get(turn.surface_id)
            if surface_ev is None and not turn.surface_id.endswith(".md"):
                surface_ev = surface_map.get(turn.surface_id + ".md")

        # Stimulus selection. For signal turns, inbound is the user's text.
        # For surface turns, the dispatch event already populated turn.inbound
        # with the surface body — fall back to surface_event if not.
        stimulus = turn.inbound
        stimulus_kind = turn.kind
        if not stimulus and surface_ev is not None:
            stimulus = (surface_ev.detail or {}).get("body")
            stimulus_kind = "surface"

        # Sender label
        if turn.kind == "signal":
            sender = turn.sender_name
        elif turn.kind == "surface":
            sender = "Thinking Alice"
        elif turn.kind == "emergency":
            sender = "EMERGENCY"
        else:
            sender = None

        arcs.append(
            ConversationArc(
                arc_id=turn.turn_id,
                kind=turn.kind,
                ts=turn.start_ts,
                end_ts=turn.end_ts,
                duration_ms=turn.duration_ms,
                sender=sender,
                stimulus=stimulus,
                stimulus_kind=stimulus_kind,
                surface_id=turn.surface_id,
                thinking_excerpt=thinking_excerpt,
                thinking_count=len(thinking_blocks),
                response=turn.outbound,
                tools=list(turn.tools),
                error=turn.error,
                has_response=bool(turn.outbound),
                detail_url=f"/turns/{turn.turn_id}",
                surface_event=surface_ev,
                turn=turn,
            )
        )

    arcs.sort(key=lambda a: a.ts, reverse=True)
    return arcs


# ---------------------------------------------------------------------------
# Interaction Arcs (graph-component view) — used by the graph tab.
#
# An Arc is a connected component in the interaction graph (with the global
# ``directive`` node excluded — otherwise every wake collapses into one
# component). Each Arc threads together what was actually a single
# end-to-end exchange across the two hemispheres: an inbound signal turn
# that wrote a note → the thinking wake that consumed it → the surface that
# wake produced → the speaking surface-turn that voiced it back to the owner.
#
# We render each Arc as a card with a vertical timeline of ArcSteps. The
# step's ``hemisphere`` and ``incoming_edge`` are what give the UI its
# "track from start to finish" character.


@dataclass
class ArcStep:
    node_id: str
    kind: str             # turn | wake | surface | emergency | note | thought
    hemisphere: str       # speaking | thinking | inner
    label: str
    ts: float
    status: str           # pending | resolved | ended | running | errored | timeout | exception | written | consumed
    detail: dict[str, Any]    # type-specific body/inbound/outbound/etc
    incoming_edge: str | None  # edge kind from the predecessor in the arc, if any
    detail_url: str | None     # /turns/{id} or /wakes/{id} when applicable


@dataclass
class Arc:
    arc_id: str
    start_ts: float
    end_ts: float
    hemispheres: list[str]
    is_cross_hemisphere: bool
    summary: str
    trigger_label: str
    steps: list[ArcStep] = field(default_factory=list)


def _hemisphere_for_node(kind: str) -> str:
    if kind == "turn":
        return "speaking"
    if kind in ("wake", "thought"):
        return "thinking"
    return "inner"


def _detail_url_for_node(node: InteractionNode) -> str | None:
    if node.kind == "turn":
        return f"/turns/{node.id.removeprefix('turn::')}"
    if node.kind == "wake":
        return f"/wakes/{node.id.removeprefix('wake::')}"
    return None


def _step_status(node: InteractionNode, wake: Wake | None, turn: Turn | None) -> str:
    if turn is not None:
        if turn.error:
            return "errored"
        if turn.end_ts is None:
            return "running"
        return "ended"
    if wake is not None:
        return wake.status
    # File-based artifacts: meta carries the original event kind via
    # detail — but we lost the kind on the node. Reconstruct from
    # the trailer / frontmatter shape and the node's `meta`.
    meta = node.meta or {}
    if node.kind in ("surface", "emergency"):
        # Resolved artifacts have a trailer; pending ones don't.
        # Verdicts can be free-form prose ("Applied. Edited prompts/..."),
        # so collapse anything longer than one short token to "resolved".
        # The full verdict still shows in the trailer panel below.
        if meta.get("trailer"):
            raw = (meta["trailer"] or {}).get("verdict")
            if isinstance(raw, str):
                token = raw.strip().split()[0].rstrip(".,;:").lower() if raw.strip() else ""
                short_tokens = {
                    "pending", "resolved", "applied", "voiced", "noted",
                    "let", "drop", "drop.", "skip", "skipped", "deferred",
                    "accepted", "rejected", "filed", "ack", "ok",
                }
                if token and token in short_tokens and len(raw.strip()) <= 24:
                    return token
            return "resolved"
        return "pending"
    if node.kind == "note":
        if meta.get("trailer"):
            return "consumed"
        return "pending"
    if node.kind == "thought":
        return "written"
    return ""


def _step_detail(
    node: InteractionNode,
    wake: Wake | None,
    turn: Turn | None,
    *,
    run_summary_module=None,
) -> dict[str, Any]:
    if turn is not None:
        return {
            "kind": turn.kind,
            "sender_name": turn.sender_name,
            "inbound": turn.inbound,
            "outbound": turn.outbound,
            "error": turn.error,
            "duration_ms": turn.duration_ms,
            "tools": list(turn.tools),
            "surface_id": turn.surface_id,
            "emergency_id": turn.emergency_id,
        }
    if wake is not None:
        return {
            "model": wake.model,
            "duration_ms": wake.duration_ms,
            "total_cost_usd": wake.total_cost_usd,
            "tools": list(wake.tools),
            "summary": summarize_wake(wake, run_summary_module),
            "event_count": len(wake.events),
        }
    # Filesystem artifact — pull body/frontmatter/trailer from node.meta.
    meta = node.meta or {}
    return {
        "body": meta.get("body") or "",
        "frontmatter": meta.get("frontmatter") or {},
        "trailer": meta.get("trailer") or {},
    }


def _arc_trigger_label(
    first: InteractionNode, wake: Wake | None, turn: Turn | None
) -> str:
    if turn is not None:
        if turn.kind == "signal":
            sender = turn.sender_name or "?"
            inbound = (turn.inbound or "").replace("\n", " ").strip()
            if not inbound:
                return f"{sender} → (image / attachment)"
            return f"{sender} → {inbound[:140]}"
        if turn.kind == "surface":
            return f"voicing surface · {turn.surface_id or '?'}"
        if turn.kind == "emergency":
            return f"voicing EMERGENCY · {turn.emergency_id or '?'}"
        return turn.kind
    if wake is not None:
        return f"thinking wake · {summarize_wake(wake)}"
    if first.kind == "surface":
        body = ((first.meta or {}).get("body") or "").replace("\n", " ").strip()
        return f"surface · {body[:120]}" if body else f"surface · {first.label}"
    if first.kind == "emergency":
        body = ((first.meta or {}).get("body") or "").replace("\n", " ").strip()
        return f"EMERGENCY · {body[:120]}" if body else f"emergency · {first.label}"
    if first.kind == "note":
        body = ((first.meta or {}).get("body") or "").replace("\n", " ").strip()
        return f"note · {body[:120]}" if body else f"note · {first.label}"
    if first.kind == "thought":
        body = ((first.meta or {}).get("body") or "").replace("\n", " ").strip()
        return f"thought · {body[:120]}" if body else f"thought · {first.label}"
    return first.label


def _arc_summary(steps: list[ArcStep]) -> str:
    """One-line headline summarising what the arc accomplished, if anything."""
    # Prefer: surface body (the thing thinking actually said).
    for s in steps:
        if s.kind == "surface":
            body = (s.detail.get("body") or "").replace("\n", " ").strip()
            if body:
                return body[:160]
    # Then: the speaking outbound (what landed in Signal).
    for s in reversed(steps):
        if s.kind == "turn" and s.detail.get("outbound"):
            ob = (s.detail["outbound"] or "").replace("\n", " ").strip()
            if ob:
                return ob[:160]
    # Then: a note body.
    for s in steps:
        if s.kind == "note":
            body = (s.detail.get("body") or "").replace("\n", " ").strip()
            if body:
                return body[:160]
    # Fallback: thought / wake summary.
    for s in steps:
        if s.kind == "thought":
            body = (s.detail.get("body") or "").replace("\n", " ").strip()
            if body:
                return body[:160]
        if s.kind == "wake" and s.detail.get("summary"):
            return s.detail["summary"][:160]
    return "(empty arc)"


def build_interaction_arcs(
    nodes: list[InteractionNode],
    edges: list[InteractionEdge],
    *,
    wakes: list[Wake],
    turns: list[Turn],
    run_summary_module=None,
) -> list[Arc]:
    """Cluster the interaction graph into end-to-end arcs.

    Drops the global ``directive`` node before component-finding (it would
    otherwise pull every wake into one giant arc). Skips singleton wake-
    only or turn-only components — those already have their own list pages.
    Returns arcs newest-first.
    """
    real_nodes = [n for n in nodes if n.kind != "directive"]
    real_edges = [e for e in edges if e.source != "directive" and e.target != "directive"]

    node_by_id = {n.id: n for n in real_nodes}
    wake_by_id = {w.wake_id: w for w in wakes}
    turn_by_id = {t.turn_id: t for t in turns}

    # Undirected adjacency for connected components.
    adj: dict[str, set[str]] = {n.id: set() for n in real_nodes}
    for e in real_edges:
        if e.source in adj and e.target in adj:
            adj[e.source].add(e.target)
            adj[e.target].add(e.source)

    visited: set[str] = set()
    components: list[list[str]] = []
    for nid in adj:
        if nid in visited:
            continue
        stack = [nid]
        comp: list[str] = []
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            stack.extend(adj[cur] - visited)
        components.append(comp)

    arcs: list[Arc] = []
    for comp_ids in components:
        # Skip lone wakes / lone turns / lone thoughts — those have their
        # own dedicated tabs and aren't "interactions" in the owner's sense.
        if len(comp_ids) == 1:
            n0 = node_by_id[comp_ids[0]]
            if n0.kind in ("wake", "turn", "thought"):
                continue

        comp_nodes = sorted((node_by_id[i] for i in comp_ids), key=lambda n: n.ts)
        comp_set = set(comp_ids)
        edges_in_comp = [
            e for e in real_edges if e.source in comp_set and e.target in comp_set
        ]
        edges_by_target: dict[str, list[InteractionEdge]] = {}
        for e in edges_in_comp:
            edges_by_target.setdefault(e.target, []).append(e)

        steps: list[ArcStep] = []
        for n in comp_nodes:
            wake = wake_by_id.get(n.id.removeprefix("wake::")) if n.kind == "wake" else None
            turn = turn_by_id.get(n.id.removeprefix("turn::")) if n.kind == "turn" else None

            # Pick the incoming edge from the latest predecessor that's
            # still earlier in the arc — that's "what got us here."
            in_edge = None
            ins = edges_by_target.get(n.id, [])
            preds = [
                (node_by_id[e.source].ts, e.kind)
                for e in ins
                if e.source in node_by_id and node_by_id[e.source].ts <= n.ts
            ]
            if preds:
                preds.sort(reverse=True)
                in_edge = preds[0][1]
            elif ins:
                in_edge = ins[0].kind

            steps.append(
                ArcStep(
                    node_id=n.id,
                    kind=n.kind,
                    hemisphere=_hemisphere_for_node(n.kind),
                    label=n.label,
                    ts=n.ts,
                    status=_step_status(n, wake, turn),
                    detail=_step_detail(n, wake, turn, run_summary_module=run_summary_module),
                    incoming_edge=in_edge,
                    detail_url=_detail_url_for_node(n),
                )
            )

        first = comp_nodes[0]
        first_wake = wake_by_id.get(first.id.removeprefix("wake::")) if first.kind == "wake" else None
        first_turn = turn_by_id.get(first.id.removeprefix("turn::")) if first.kind == "turn" else None
        hemispheres = sorted({s.hemisphere for s in steps})
        arcs.append(
            Arc(
                arc_id=first.id,
                start_ts=first.ts,
                end_ts=comp_nodes[-1].ts,
                hemispheres=hemispheres,
                is_cross_hemisphere=len(hemispheres) > 1,
                summary=_arc_summary(steps),
                trigger_label=_arc_trigger_label(first, first_wake, first_turn),
                steps=steps,
            )
        )

    arcs.sort(key=lambda a: a.start_ts, reverse=True)
    return arcs


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
