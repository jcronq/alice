"""Data sources for the viewer.

Reads Alice's raw artifacts — JSONL event logs, the per-turn log, and
filesystem inbox/outbox artifacts — and normalizes them into a common
UnifiedEvent model the aggregators can reason about.

All readers are stateless and file-based. No DB.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .settings import Paths


@dataclass
class UnifiedEvent:
    ts: float
    hemisphere: str            # "thinking" | "speaking" | "inner"
    kind: str                  # canonical event type, e.g. "tool_use"
    correlation_id: str | None # turn_id | wake_id | surface_id | etc.
    summary: str               # one-line label for the timeline row
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "hemisphere": self.hemisphere,
            "kind": self.kind,
            "correlation_id": self.correlation_id,
            "summary": self.summary,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# JSONL readers


def _read_jsonl(path: pathlib.Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def read_thinking(path: pathlib.Path) -> list[UnifiedEvent]:
    """Parse thinking.log; assign wake_id = ts of the enclosing wake_start."""
    out: list[UnifiedEvent] = []
    current_wake: str | None = None
    for rec in _read_jsonl(path):
        event = rec.get("event") or "unknown"
        ts = float(rec.get("ts") or 0.0)
        if event == "wake_start":
            current_wake = f"wake-{int(ts)}"
        correlation_id = current_wake
        summary = _thinking_summary(event, rec)
        out.append(
            UnifiedEvent(
                ts=ts,
                hemisphere="thinking",
                kind=event,
                correlation_id=correlation_id,
                summary=summary,
                detail=rec,
            )
        )
        if event in ("wake_end", "timeout", "exception"):
            current_wake = None
    return out


def _tool_summary(name: str, input_raw: Any) -> str:
    """Compact one-line representation of a tool call: '<tool> <primary arg>'.

    Falls back to just the tool name if the input can't be parsed.
    """
    if input_raw is None:
        return name
    if isinstance(input_raw, str):
        try:
            parsed: Any = json.loads(input_raw)
        except (json.JSONDecodeError, ValueError):
            # Daemon truncates large inputs at 2000 chars → JSON often invalid.
            # Fall back to regex-plucking the most useful field.
            for field in ("file_path", "command", "pattern", "url", "query", "notebook_path", "description"):
                m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', input_raw)
                if m:
                    val = m.group(1).encode().decode("unicode_escape", errors="replace")
                    return f"{name} {_trim(val, 140)}"
            return f"{name} {_trim(input_raw, 140)}"
    else:
        parsed = input_raw
    if not isinstance(parsed, dict):
        return f"{name} {_trim(str(parsed), 140)}"

    # Per-tool: pick the key argument.
    primary: str | None = None
    if name == "Bash":
        primary = parsed.get("command")
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        primary = parsed.get("file_path") or parsed.get("notebook_path")
    elif name == "Grep":
        primary = parsed.get("pattern")
        if parsed.get("path"):
            primary = f"{primary}  in {parsed['path']}"
    elif name == "Glob":
        primary = parsed.get("pattern")
        if parsed.get("path"):
            primary = f"{primary}  in {parsed['path']}"
    elif name == "WebFetch":
        primary = parsed.get("url")
    elif name == "WebSearch":
        primary = parsed.get("query")
    elif name == "Task":
        primary = parsed.get("description") or parsed.get("subagent_type")
    elif name == "TaskCreate" or name == "TaskUpdate":
        primary = parsed.get("subject") or parsed.get("taskId")
    elif name.startswith("mcp__"):
        # Custom MCP tools — show the first non-empty value.
        for v in parsed.values():
            if isinstance(v, str) and v:
                primary = v
                break

    if primary is None:
        # Unknown tool — render the dict compactly.
        return f"{name} {_trim(json.dumps(parsed, ensure_ascii=False), 140)}"
    return f"{name} {_trim(str(primary), 140)}"


def _trim(s: str, cap: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


# `user_message` events store the SDK's tool-result blocks as the
# *str()* of a list, e.g. ``["ToolResultBlock(tool_use_id='X', content='Y',
# is_error=False)"]``. Because each list element is already a string, the
# outer ``str(list)`` re-escapes its contents — so quotes around content
# come through as ``\"`` (backslash + quote) and inner ``\t`` / ``\n``
# come through as ``\\t`` / ``\\n``. We accept an optional leading
# backslash on each quote and decode escapes twice on readout.
_TOOL_RESULT_RE = re.compile(
    r"ToolResultBlock\(\s*"
    r"tool_use_id=\\?(?P<idq>['\"])(?P<tid>[^'\"\\]+)\\?(?P=idq)"
    r"\s*,\s*content=\\?(?P<cq>['\"])(?P<content>(?:[^\\]|\\.)*?)\\?(?P=cq)"
    r"\s*,\s*is_error=(?P<err>True|False|None)\s*\)",
    re.DOTALL,
)
# Lenient match for truncated entries (kernel applies a length cap) —
# pulls tool_use_id and whatever opening content we have.
_TOOL_RESULT_TRUNC_RE = re.compile(
    r"ToolResultBlock\(\s*"
    r"tool_use_id=\\?(?P<idq>['\"])(?P<tid>[^'\"\\]+)\\?(?P=idq)"
    r"(?:\s*,\s*content=\\?(?P<cq>['\"])(?P<content>.*))?",
    re.DOTALL,
)


def parse_tool_results(text: Any) -> list[dict[str, Any]]:
    """Best-effort parse of a `user_message` event's `content` field into
    a list of ``{tool_use_id, content, is_error, truncated}`` dicts.

    Returns ``[]`` when the input doesn't look like a tool-result list — the
    caller should fall back to displaying the raw string.
    """
    if not isinstance(text, str) or "ToolResultBlock(" not in text:
        return []

    out: list[dict[str, Any]] = []
    consumed_until = 0
    for m in _TOOL_RESULT_RE.finditer(text):
        out.append(_decode_block(m, truncated=False))
        consumed_until = m.end()

    # If a `ToolResultBlock(` appears past where the strict regex stopped,
    # it's a truncated tail. Try to pull what we can.
    tail = text[consumed_until:]
    tail_start = tail.find("ToolResultBlock(")
    if tail_start != -1:
        tm = _TOOL_RESULT_TRUNC_RE.search(tail, tail_start)
        if tm:
            out.append(_decode_block(tm, truncated=True))
    return out


def _unescape(s: str) -> str:
    """Decode Python repr-style escapes. Tries twice because the outer
    `str(list)` of pre-stringified blocks introduces a second layer of
    escaping (``\\\\t`` → ``\\t`` → tab)."""
    for _ in range(2):
        try:
            decoded = s.encode("latin-1", "backslashreplace").decode(
                "unicode_escape"
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return s
        if decoded == s:
            break
        s = decoded
    return s


def _decode_block(m: re.Match, *, truncated: bool) -> dict[str, Any]:
    content = m.groupdict().get("content")
    if content is not None:
        content = _unescape(content)
    err = m.groupdict().get("err")
    return {
        "tool_use_id": m.group("tid"),
        "content": content,
        "is_error": err == "True" if err else False,
        "truncated": truncated or err is None,
    }


def _thinking_summary(event: str, rec: dict[str, Any]) -> str:
    if event == "wake_start":
        return f"wake start · model={rec.get('model')} budget={rec.get('max_seconds')}s"
    if event == "wake_end":
        return "wake end"
    if event == "timeout":
        return f"timeout at {rec.get('max_seconds')}s"
    if event == "exception":
        return f"exception: {rec.get('type')} {rec.get('message')}"
    if event == "assistant_text":
        return _trim(rec.get("text") or "", 140)
    if event == "tool_use":
        return _tool_summary(rec.get("name") or "?", rec.get("input"))
    if event == "thinking":
        return f"(thought) {_trim(rec.get('text') or '', 120)}"
    if event == "result":
        dur = rec.get("duration_ms")
        cost = rec.get("total_cost_usd")
        return f"result · {dur}ms · ${cost}"
    if event == "system":
        return f"system · {rec.get('subtype')}"
    if event == "user_message":
        return f"(tool-result) {_trim(str(rec.get('content') or ''), 120)}"
    return event


def read_speaking(path: pathlib.Path) -> list[UnifiedEvent]:
    """Parse speaking.log; correlation_id = turn_id."""
    out: list[UnifiedEvent] = []
    for rec in _read_jsonl(path):
        event = rec.get("event") or "unknown"
        ts = float(rec.get("ts") or 0.0)
        correlation_id = rec.get("turn_id")
        # Collapse some event families to a stable kind for coloring.
        summary = _speaking_summary(event, rec)
        out.append(
            UnifiedEvent(
                ts=ts,
                hemisphere="speaking",
                kind=event,
                correlation_id=correlation_id,
                summary=summary,
                detail=rec,
            )
        )
    return out


def _speaking_summary(event: str, rec: dict[str, Any]) -> str:
    if event == "signal_turn_start":
        return f"signal · {rec.get('sender_name')} → {_trim(rec.get('inbound') or '', 120)}"
    if event == "signal_turn_end":
        chars = rec.get("outbound_chars") or 0
        err = rec.get("error")
        return f"turn end · {chars} chars" + (f" · error={err}" if err else "")
    if event == "signal_send":
        return f"signal-send · {rec.get('sender_name')} ({rec.get('text_len')} chars)"
    if event == "surface_dispatch":
        return f"surface dispatch · {rec.get('surface_id')}"
    if event == "surface_turn_end":
        return f"surface end · {rec.get('surface_id')}"
    if event == "emergency_dispatch":
        return f"EMERGENCY dispatch · {rec.get('emergency_id')}"
    if event == "emergency_voiced":
        return f"EMERGENCY voiced → {rec.get('recipient')}"
    if event == "emergency_downgraded":
        return f"emergency downgraded · {rec.get('emergency_id')}"
    if event == "emergency_turn_end":
        return f"emergency end · {rec.get('emergency_id')} · {rec.get('verdict')}"
    if event == "quiet_queue_enter":
        return f"quiet-queue · {rec.get('sender_name')} ({rec.get('queue_size')} queued)"
    if event == "quiet_queue_drain":
        return f"quiet-queue drain · {rec.get('count')} msgs ({rec.get('reason')})"
    if event == "config_reload":
        return f"config reload · {rec.get('changes')}"
    if event == "daemon_start":
        return f"daemon start · model={rec.get('model')}"
    if event == "daemon_ready":
        return "daemon ready"
    if event == "shutdown":
        return "daemon shutdown"
    if event == "assistant_text":
        return _trim(rec.get("text") or "", 140)
    if event == "tool_use":
        return _tool_summary(rec.get("name") or "?", rec.get("input"))
    if event == "thinking":
        return f"(thought) {_trim(rec.get('text') or '', 120)}"
    if event == "result":
        dur = rec.get("duration_ms")
        cost = rec.get("total_cost_usd")
        return f"result · {dur}ms · ${cost}"
    return event


def read_turn_log(path: pathlib.Path) -> list[UnifiedEvent]:
    """Turn log as an event source. Useful for history before speaking.log existed."""
    out: list[UnifiedEvent] = []
    for rec in _read_jsonl(path):
        ts = float(rec.get("ts") or 0.0)
        summary = f"[turn-log] {rec.get('sender_name')} → {_trim(rec.get('inbound') or '', 80)}"
        out.append(
            UnifiedEvent(
                ts=ts,
                hemisphere="speaking",
                kind="turn_log",
                correlation_id=None,
                summary=summary,
                detail=rec,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Filesystem artifact scanners


def _safe_iter_files(root: pathlib.Path, pattern: str = "*") -> Iterable[pathlib.Path]:
    if not root.is_dir():
        return []
    try:
        return sorted(root.glob(pattern))
    except OSError:
        return []


def read_surfaces(inner: pathlib.Path) -> list[UnifiedEvent]:
    """inner/surface/*.md (pending) + inner/surface/.handled/<date>/*.md (resolved)."""
    out: list[UnifiedEvent] = []
    surface_dir = inner / "surface"
    handled_dir = surface_dir / ".handled"

    for path in _safe_iter_files(surface_dir, "*.md"):
        if path.name.startswith("."):
            continue
        body = _read_text(path)
        out.append(
            UnifiedEvent(
                ts=path.stat().st_mtime,
                hemisphere="inner",
                kind="surface_pending",
                correlation_id=path.name,
                summary=f"surface pending · {path.name}",
                detail={
                    "path": str(path),
                    "filename": path.name,
                    "body": body,
                    "frontmatter": _parse_frontmatter(body),
                },
            )
        )

    if handled_dir.is_dir():
        for date_dir in sorted(handled_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for path in _safe_iter_files(date_dir, "*.md"):
                body = _read_text(path)
                out.append(
                    UnifiedEvent(
                        ts=path.stat().st_mtime,
                        hemisphere="inner",
                        kind="surface_resolved",
                        correlation_id=path.name,
                        summary=f"surface resolved · {path.name}",
                        detail={
                            "path": str(path),
                            "filename": path.name,
                            "date": date_dir.name,
                            "body": body,
                            "frontmatter": _parse_frontmatter(body),
                            "trailer": _parse_trailer(body),
                        },
                    )
                )
    return out


def read_emergencies(inner: pathlib.Path) -> list[UnifiedEvent]:
    out: list[UnifiedEvent] = []
    emer_dir = inner / "emergency"
    handled_dir = emer_dir / ".handled"

    for path in _safe_iter_files(emer_dir, "*.md"):
        if path.name.startswith("."):
            continue
        body = _read_text(path)
        out.append(
            UnifiedEvent(
                ts=path.stat().st_mtime,
                hemisphere="inner",
                kind="emergency_pending",
                correlation_id=path.name,
                summary=f"EMERGENCY pending · {path.name}",
                detail={"path": str(path), "filename": path.name, "body": body},
            )
        )
    if handled_dir.is_dir():
        for date_dir in sorted(handled_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for path in _safe_iter_files(date_dir, "*.md"):
                body = _read_text(path)
                out.append(
                    UnifiedEvent(
                        ts=path.stat().st_mtime,
                        hemisphere="inner",
                        kind="emergency_resolved",
                        correlation_id=path.name,
                        summary=f"emergency resolved · {path.name}",
                        detail={
                            "path": str(path),
                            "filename": path.name,
                            "date": date_dir.name,
                            "body": body,
                            "trailer": _parse_trailer(body),
                        },
                    )
                )
    return out


def read_notes(inner: pathlib.Path) -> list[UnifiedEvent]:
    """inner/notes/*.md (pending, from speaking) + inner/notes/.consumed/<date>/*.md."""
    out: list[UnifiedEvent] = []
    notes_dir = inner / "notes"
    consumed_dir = notes_dir / ".consumed"

    for path in _safe_iter_files(notes_dir, "*.md"):
        if path.name.startswith("."):
            continue
        body = _read_text(path)
        out.append(
            UnifiedEvent(
                ts=path.stat().st_mtime,
                hemisphere="inner",
                kind="note_pending",
                correlation_id=path.name,
                summary=f"note pending · {path.name}",
                detail={"path": str(path), "filename": path.name, "body": body},
            )
        )
    if consumed_dir.is_dir():
        for date_dir in sorted(consumed_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            for path in _safe_iter_files(date_dir, "*.md"):
                body = _read_text(path)
                out.append(
                    UnifiedEvent(
                        ts=path.stat().st_mtime,
                        hemisphere="inner",
                        kind="note_consumed",
                        correlation_id=path.name,
                        summary=f"note consumed · {path.name}",
                        detail={
                            "path": str(path),
                            "filename": path.name,
                            "date": date_dir.name,
                            "body": body,
                            "trailer": _parse_trailer(body),
                        },
                    )
                )
    return out


def read_thoughts(inner: pathlib.Path) -> list[UnifiedEvent]:
    """inner/thoughts/<YYYY-MM-DD>/*.md — thinking wake records."""
    out: list[UnifiedEvent] = []
    thoughts_dir = inner / "thoughts"
    if not thoughts_dir.is_dir():
        return out
    for date_dir in sorted(thoughts_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for path in _safe_iter_files(date_dir, "*.md"):
            body = _read_text(path)
            out.append(
                UnifiedEvent(
                    ts=path.stat().st_mtime,
                    hemisphere="inner",
                    kind="thought_written",
                    correlation_id=path.name,
                    summary=f"thought · {date_dir.name}/{path.name}",
                    detail={
                        "path": str(path),
                        "filename": path.name,
                        "date": date_dir.name,
                        "body": body,
                    },
                )
            )
    return out


def read_directive(inner: pathlib.Path) -> str:
    return _read_text(inner / "directive.md")


def find_wake_thought(
    events: list[UnifiedEvent],
    wake_start_ts: float,
    wake_end_ts: float | None,
) -> dict[str, Any] | None:
    """Locate the `*-wake.md` thought file written during this wake.

    Matches by file mtime (the `thought_written` event ts) rather than
    parsing the filename, since filename HHMMSS reflects the writer's
    local TZ which may differ from the viewer process's TZ. Picks the
    closest `*-wake.md` whose mtime falls between wake start and end
    (or start + 2h if the wake never closed cleanly).
    """
    upper = (wake_end_ts if wake_end_ts is not None else wake_start_ts + 7200) + 60
    best: UnifiedEvent | None = None
    best_delta: float | None = None
    for ev in events:
        if ev.kind != "thought_written":
            continue
        filename = (ev.detail or {}).get("filename") or ""
        if not filename.endswith("-wake.md"):
            continue
        if ev.ts < wake_start_ts - 5 or ev.ts > upper:
            continue
        delta = abs(ev.ts - wake_start_ts)
        if best_delta is None or delta < best_delta:
            best = ev
            best_delta = delta
    if best is None:
        return None
    d = best.detail or {}
    return {
        "filename": d.get("filename") or best.correlation_id or "wake.md",
        "body": d.get("body") or "",
        "path": d.get("path") or "",
    }


# ---------------------------------------------------------------------------
# Memory graph source


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]")


@dataclass
class MemoryNode:
    id: str         # stable id (relative path without .md)
    label: str      # display name
    path: str       # absolute path for viewing
    folder: str     # first segment, e.g. "memory" or "memory/sources"
    size: int       # bytes
    mtime: float


@dataclass
class MemoryEdge:
    source: str
    target: str


def read_memory_graph(mind: pathlib.Path) -> tuple[list[MemoryNode], list[MemoryEdge]]:
    """Scan cortex-memory + legacy memory/ for wikilinks and return a graph.

    Both roots are unioned so dated daily logs and legacy curated notes show
    up alongside the groomed wiki, inviting Alice to migrate/link them over
    time. Wikilinks resolve across both roots by title/slug.
    """
    nodes: dict[str, MemoryNode] = {}
    file_bodies: dict[str, str] = {}
    for root_name in ("cortex-memory", "memory"):
        root = mind / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            try:
                rel = path.relative_to(mind).with_suffix("")
            except ValueError:
                continue
            node_id = str(rel)  # e.g. "cortex-memory/people/jason" or "memory/2026-04-24"
            label = path.stem
            folder = "/".join(rel.parts[:-1]) or rel.parts[0]
            try:
                st = path.stat()
            except OSError:
                continue
            nodes[node_id] = MemoryNode(
                id=node_id,
                label=label,
                path=str(path),
                folder=folder,
                size=st.st_size,
                mtime=st.st_mtime,
            )
            file_bodies[node_id] = _read_text(path)

    if not nodes:
        return [], []
    root = None  # sentinel; kept for later functions that reference root

    # Build a label → node_id index for resolving wikilinks.
    by_label: dict[str, str] = {}
    for nid, n in nodes.items():
        by_label.setdefault(n.label.lower(), nid)

    edges: list[MemoryEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for src_id, body in file_bodies.items():
        for match in WIKILINK_RE.finditer(body):
            target_label = match.group(1).strip()
            if not target_label:
                continue
            target_id = by_label.get(target_label.lower())
            if target_id is None:
                # Broken link — create a placeholder node so it shows up.
                ghost_id = f"unresolved::{target_label}"
                if ghost_id not in nodes:
                    nodes[ghost_id] = MemoryNode(
                        id=ghost_id,
                        label=target_label,
                        path="",
                        folder="unresolved",
                        size=0,
                        mtime=0.0,
                    )
                target_id = ghost_id
            if src_id == target_id:
                continue
            edge = (src_id, target_id)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            edges.append(MemoryEdge(source=src_id, target=target_id))

    return list(nodes.values()), edges


def search_memory(
    mind: pathlib.Path, query: str, limit: int = 25
) -> list[dict[str, Any]]:
    """Token-AND search across cortex-memory + legacy memory/.

    A note matches when *every* whitespace-separated token in the query
    appears (case-insensitive substring) somewhere in its label, frontmatter
    title/aliases/tags, or body. Hits in the strong haystack score 10×
    body hits; an all-strong match gets a +20 bonus so labelled hits float
    to the top. Returns ranked records `{id, label, title, score,
    matched_in}`. Body of the note is not returned — fetch via
    /api/memory/note for that.
    """
    tokens = [t.lower() for t in query.split() if t]
    if not tokens:
        return []

    results: list[dict[str, Any]] = []
    for root_name in ("cortex-memory", "memory"):
        root = mind / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            try:
                rel = path.relative_to(mind).with_suffix("")
            except ValueError:
                continue
            body = _read_text(path)
            if not body:
                continue

            fm_match = FRONTMATTER_RE.match(body)
            if fm_match:
                fm_text = fm_match.group(1)
                body_text = body[fm_match.end():]
            else:
                fm_text = ""
                body_text = body

            label = path.stem
            strong = (label + " " + fm_text).lower()
            weak = body_text.lower()

            score = 0
            strong_hits = 0
            weak_hits = 0
            for tok in tokens:
                if tok in strong:
                    strong_hits += 1
                    score += 10
                elif tok in weak:
                    weak_hits += 1
                    score += 1
                else:
                    score = 0
                    break

            if score == 0:
                continue
            if strong_hits == len(tokens):
                score += 20
                matched_in = "label/title/aliases"
            elif weak_hits == len(tokens):
                matched_in = "body"
            else:
                matched_in = "mixed"

            # Title from frontmatter, if any — used for display.
            title = ""
            for line in fm_text.splitlines():
                if line.startswith("title:"):
                    title = line.partition(":")[2].strip()
                    break

            results.append({
                "id": str(rel),
                "label": label,
                "title": title,
                "score": score,
                "matched_in": matched_in,
            })

    results.sort(key=lambda r: (-r["score"], r["label"]))
    return results[:limit]


# ---------------------------------------------------------------------------
# Helpers


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(body: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(body)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _parse_trailer(body: str) -> dict[str, str]:
    """Extract the `resolved / verdict / action_taken / voiced_text` trailer
    that _archive_* writes on .handled files."""
    idx = body.rfind("\n---\n")
    if idx < 0:
        return {}
    trailer = body[idx + 5 :]
    out: dict[str, str] = {}
    for line in trailer.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Unified loader


def load_all(paths: Paths) -> list[UnifiedEvent]:
    events: list[UnifiedEvent] = []
    events.extend(read_thinking(paths.thinking_log))
    events.extend(read_speaking(paths.speaking_log))
    # Backfill: if speaking.log is empty but turn-log has history, include that.
    if not any(e.hemisphere == "speaking" for e in events):
        events.extend(read_turn_log(paths.turn_log))
    inner = paths.inner
    events.extend(read_surfaces(inner))
    events.extend(read_emergencies(inner))
    events.extend(read_notes(inner))
    events.extend(read_thoughts(inner))
    events.sort(key=lambda e: e.ts)
    return events


def now() -> float:
    return time.time()
