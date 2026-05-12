"""Data sources for the viewer.

Reads Alice's raw artifacts — JSONL event logs, the per-turn log, and
filesystem inbox/outbox artifacts — and normalizes them into a common
UnifiedEvent model the aggregators can reason about.

The JSONL readers (``read_thinking`` / ``read_speaking`` / ``read_turn_log``)
use an in-process incremental tail-parse cache: on the second call for
the same path, only the newly-appended bytes are re-decoded. ``load_all``
also memoizes its merged result keyed on a per-source size signature so
back-to-back requests against unchanged logs return in microseconds
instead of reparsing tens of megabytes of JSON. The cache is per-process
(not threadsafe across workers), which matches how the viewer runs (a
single uvicorn worker).
"""

from __future__ import annotations

import json
import math
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .settings import Paths


@dataclass
class UnifiedEvent:
    ts: float
    hemisphere: str  # "thinking" | "speaking" | "inner"
    kind: str  # canonical event type, e.g. "tool_use"
    correlation_id: str | None  # turn_id | wake_id | surface_id | etc.
    summary: str  # one-line label for the timeline row
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


# Per-process incremental cache for append-only JSONL files.
# Each entry remembers how many bytes we've already parsed so a follow-up
# read only decodes the tail. Rotation (size shrinks) → reparse from
# scratch.
@dataclass
class _JsonlCacheEntry:
    size: int
    events: list[UnifiedEvent]
    state: Any


_jsonl_caches: dict[str, _JsonlCacheEntry] = {}


def _iter_jsonl_tail(
    path: pathlib.Path, start_offset: int
) -> Iterator[tuple[dict[str, Any] | None, int]]:
    """Yield ``(record, offset_after_line)`` for every complete line at or
    after ``start_offset``. ``record`` is None for blank/malformed lines so
    callers can still advance the offset cursor past them.

    A trailing partial line (writer mid-flush) is left unread; the caller
    keeps the previous offset so the next call picks it up cleanly.
    """
    try:
        with path.open("rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError:
        return
    if not data:
        return
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            return  # partial trailing line — leave for next call
        line_bytes = data[pos:nl]
        new_offset = start_offset + nl + 1
        pos = nl + 1
        line = line_bytes.strip()
        if not line:
            yield None, new_offset
            continue
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            yield None, new_offset
            continue
        yield rec, new_offset


def _cached_jsonl(
    path: pathlib.Path,
    parse_record: Callable[[dict[str, Any], Any], tuple[UnifiedEvent | None, Any]],
    initial_state_factory: Callable[[], Any],
) -> list[UnifiedEvent]:
    """Tail-parse cache. ``parse_record`` is ``(rec, state) -> (event_or_None, new_state)``.

    Returns the cached list directly — callers (``load_all``) consume via
    ``list.extend`` which copies references rather than mutating, so it's
    safe to share. Don't mutate the returned list.
    """
    key = str(path)
    cached = _jsonl_caches.get(key)
    if not path.is_file():
        if cached is not None:
            _jsonl_caches.pop(key, None)
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return cached.events if cached is not None else []

    if cached is None or size < cached.size:
        cached = _JsonlCacheEntry(size=0, events=[], state=initial_state_factory())
        _jsonl_caches[key] = cached

    if size == cached.size:
        return cached.events

    state = cached.state
    new_offset = cached.size
    for rec, off in _iter_jsonl_tail(path, cached.size):
        new_offset = off
        if rec is None:
            continue
        event, state = parse_record(rec, state)
        if event is not None:
            cached.events.append(event)
    cached.state = state
    cached.size = new_offset
    return cached.events


def _parse_thinking_record(
    rec: dict[str, Any], current_wake: str | None
) -> tuple[UnifiedEvent | None, str | None]:
    event = rec.get("event") or "unknown"
    ts = float(rec.get("ts") or 0.0)
    if event == "wake_start":
        current_wake = f"wake-{int(ts)}"
    correlation_id = current_wake
    summary = _thinking_summary(event, rec)
    out = UnifiedEvent(
        ts=ts,
        hemisphere="thinking",
        kind=event,
        correlation_id=correlation_id,
        summary=summary,
        detail=rec,
    )
    if event in ("wake_end", "timeout", "exception"):
        current_wake = None
    return out, current_wake


def read_thinking(path: pathlib.Path) -> list[UnifiedEvent]:
    """Parse thinking.log; assign wake_id = ts of the enclosing wake_start.

    Uses the incremental tail-parse cache: subsequent calls only decode
    bytes appended since the last call.
    """
    return _cached_jsonl(path, _parse_thinking_record, lambda: None)


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
            for field in (
                "file_path",
                "command",
                "pattern",
                "url",
                "query",
                "notebook_path",
                "description",
            ):
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
            decoded = s.encode("latin-1", "backslashreplace").decode("unicode_escape")
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


def _parse_speaking_record(
    rec: dict[str, Any], _state: None
) -> tuple[UnifiedEvent | None, None]:
    event = rec.get("event") or "unknown"
    ts = float(rec.get("ts") or 0.0)
    correlation_id = rec.get("turn_id")
    summary = _speaking_summary(event, rec)
    return (
        UnifiedEvent(
            ts=ts,
            hemisphere="speaking",
            kind=event,
            correlation_id=correlation_id,
            summary=summary,
            detail=rec,
        ),
        None,
    )


def read_speaking(path: pathlib.Path) -> list[UnifiedEvent]:
    """Parse speaking.log; correlation_id = turn_id.

    Tail-parse cached.
    """
    return _cached_jsonl(path, _parse_speaking_record, lambda: None)


def _speaking_summary(event: str, rec: dict[str, Any]) -> str:
    if event == "signal_turn_start":
        return f"signal · {rec.get('sender_name')} → {_trim(rec.get('inbound') or '', 120)}"
    if event == "signal_turn_end":
        chars = rec.get("outbound_chars") or 0
        err = rec.get("error")
        return f"turn end · {chars} chars" + (f" · error={err}" if err else "")
    if event == "viewer_chat_turn_start":
        return f"viewer-chat · {rec.get('display_name')} → {_trim(rec.get('inbound') or '', 120)}"
    if event == "viewer_chat_turn_end":
        err = rec.get("error")
        dur = rec.get("duration_ms")
        return f"viewer-chat end · {dur}ms" + (f" · error={err}" if err else "")
    if event == "a2a_turn_start":
        return f"a2a · {rec.get('display_name')} → {_trim(rec.get('inbound') or '', 120)}"
    if event == "a2a_turn_end":
        err = rec.get("error")
        dur = rec.get("duration_ms")
        return f"a2a end · {dur}ms" + (f" · error={err}" if err else "")
    if event in ("signal_send", "cli_send", "discord_send"):
        transport = event.removesuffix("_send")
        chunks = rec.get("chunk_count")
        chunk_suffix = (
            f", {chunks} chunks" if chunks is not None and chunks != 1 else ""
        )
        return (
            f"{transport}-send · {rec.get('sender_name')} "
            f"({rec.get('text_len')} chars{chunk_suffix})"
        )
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
        return (
            f"quiet-queue · {rec.get('sender_name')} ({rec.get('queue_size')} queued)"
        )
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


def _parse_turn_log_record(
    rec: dict[str, Any], _state: None
) -> tuple[UnifiedEvent | None, None]:
    ts = float(rec.get("ts") or 0.0)
    summary = (
        f"[turn-log] {rec.get('sender_name')} → {_trim(rec.get('inbound') or '', 80)}"
    )
    return (
        UnifiedEvent(
            ts=ts,
            hemisphere="speaking",
            kind="turn_log",
            correlation_id=None,
            summary=summary,
            detail=rec,
        ),
        None,
    )


def read_turn_log(path: pathlib.Path) -> list[UnifiedEvent]:
    """Turn log as an event source. Useful for history before speaking.log existed.

    Tail-parse cached.
    """
    return _cached_jsonl(path, _parse_turn_log_record, lambda: None)


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


def read_current_objective(inner: pathlib.Path) -> dict[str, Any] | None:
    """Return a short summary of what thinking is currently chasing.

    Source priority:
      1. ``inner/state/active-thread.md`` — if a continuation thread is
         in flight, its ``topic`` + ``next_step`` frontmatter is the
         single most accurate "current objective" signal.
      2. ``inner/directive.md`` — the standing directive's ``## Current
         focus`` section (first paragraph after the heading) when no
         active thread is set. This is the durable focus, not a live
         task, so it's the fallback rather than the primary.

    Returns ``None`` if neither source has usable content (rare — the
    directive is part of the repo). Shape::

        {"source": "active-thread" | "directive",
         "topic": str,
         "detail": str | None}
    """
    thread_path = inner / "state" / "active-thread.md"
    thread_text = _read_text(thread_path)
    if thread_text:
        fm = _parse_frontmatter(thread_text)
        topic = (fm.get("topic") or "").strip() if fm else ""
        if topic:
            next_step = (fm.get("next_step") or "").strip() if fm else ""
            last_action = (fm.get("last_action") or "").strip() if fm else ""
            detail = next_step or last_action or None
            return {
                "source": "active-thread",
                "topic": topic,
                "detail": detail,
            }
    directive_text = _read_text(inner / "directive.md")
    if directive_text:
        focus = _extract_section_paragraph(directive_text, "Current focus")
        if focus:
            return {
                "source": "directive",
                "topic": "current focus",
                "detail": focus,
            }
    return None


def _parse_frontmatter(text: str) -> dict[str, str] | None:
    """Return a flat ``key: value`` dict from a YAML-ish frontmatter
    block (``---``-delimited at file start). One-line strings only —
    multi-line / nested YAML is not required by the active-thread.md
    schema. Returns ``None`` when no frontmatter is present."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    block = text[4:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def _extract_section_paragraph(text: str, heading_name: str) -> str | None:
    """Pull the first non-empty paragraph that follows ``## <heading_name>``
    in a markdown document. Returns ``None`` if the heading is missing
    or the section is empty."""
    lines = text.splitlines()
    needle = f"## {heading_name}".lower()
    in_section = False
    para: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_section:
            if stripped.lower() == needle:
                in_section = True
            continue
        if stripped.startswith("## "):
            break  # next heading — stop
        if not stripped:
            if para:
                break  # end of first paragraph
            continue
        para.append(stripped)
    if not para:
        return None
    return " ".join(para)


_CANVAS_SLUG_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")


def _canvas_dir(inner: pathlib.Path) -> pathlib.Path:
    return inner / "canvas"


def _experiment_card_dir(mind_dir: pathlib.Path) -> pathlib.Path:
    return mind_dir / "cortex-memory" / "experiments"


def _canvas_source_dirs(
    inner: pathlib.Path, mind_dir: pathlib.Path | None
) -> list[tuple[str, pathlib.Path]]:
    """Ordered list of ``(source_label, dir)`` for canvas scanning.

    ``canvas`` (authored) is first so a hand-authored slug wins over
    an auto-promoted experiment card if they happen to collide.
    """
    out: list[tuple[str, pathlib.Path]] = [("canvas", _canvas_dir(inner))]
    if mind_dir is not None:
        out.append(("experiment", _experiment_card_dir(mind_dir)))
    return out


def list_canvases(
    inner: pathlib.Path,
    mind_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Return canvas index entries sorted by mtime descending.

    Each entry: {slug, title, mtime, size, path, source}. ``source`` is
    ``"canvas"`` for hand-authored decks under ``inner/canvas/`` or
    ``"experiment"`` for auto-promoted experiment cards under
    ``cortex-memory/experiments/`` (when ``mind_dir`` is provided).

    Title is parsed from the first ``# H1`` line if present, else
    derived from the slug. Missing source dirs are skipped silently.
    Slug collisions are resolved by the order returned from
    ``_canvas_source_dirs`` (canvas wins over experiment).
    """
    out: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for source_label, cdir in _canvas_source_dirs(inner, mind_dir):
        if not cdir.is_dir():
            continue
        for path in cdir.glob("*.md"):
            slug = path.stem
            if not _CANVAS_SLUG_RE.match(slug):
                continue
            if slug in seen_slugs:
                continue
            try:
                text = path.read_text(encoding="utf-8")
                stat = path.stat()
            except OSError:
                continue
            title = slug
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
                if stripped.startswith("---") or stripped == "":
                    continue
            seen_slugs.add(slug)
            out.append(
                {
                    "slug": slug,
                    "title": title,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "path": str(path),
                    "source": source_label,
                }
            )
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def read_canvas(
    inner: pathlib.Path,
    slug: str,
    mind_dir: pathlib.Path | None = None,
) -> dict[str, Any] | None:
    """Read a single canvas by slug. Returns None if slug is invalid
    or the file doesn't exist in any configured source dir. Strips YAML
    frontmatter from body if present (between leading ``---`` lines).

    When ``mind_dir`` is provided, auto-promoted experiment cards under
    ``cortex-memory/experiments/`` become readable too. Authored canvas
    decks win on slug collision (same order as ``list_canvases``).
    """
    if not _CANVAS_SLUG_RE.match(slug):
        return None
    for source_label, cdir in _canvas_source_dirs(inner, mind_dir):
        path = cdir / f"{slug}.md"
        try:
            path = path.resolve()
            cdir_resolved = cdir.resolve()
        except OSError:
            continue
        if not str(path).startswith(str(cdir_resolved) + "/"):
            continue
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        body = text
        title = slug
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                body = text[end + 5 :]
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        return {
            "slug": slug,
            "title": title,
            "body": body,
            "path": str(path),
            "source": source_label,
        }
    return None


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
    id: str  # stable id (relative path without .md)
    label: str  # display name
    path: str  # absolute path for viewing
    folder: str  # first segment, e.g. "memory" or "memory/sources"
    size: int  # bytes
    mtime: float


@dataclass
class MemoryEdge:
    source: str
    target: str


# Incremental in-process memo for the memory graph. The three routes that
# need it (/api/memory-graph, /api/cluster-snapshot, /api/cluster-registry)
# all walked cortex-memory/ + memory/ and re-ran label propagation on every
# request — ~280ms of file IO + clustering per call on a 1300-note vault.
#
# Cache layers (in increasing rebuild cost):
#
#   1. Hit (signature matches): return cached bundle. ~50ms stat walk.
#   2. Near-miss (some files changed, no topical change): re-read only the
#      changed bodies, rebuild edges, **reuse cached cluster_metrics**.
#      Saves the ~100ms cluster recompute.
#   3. Topical miss (a topical file changed): re-read changed bodies +
#      recompute clusters.
#   4. Cold start: full walk.
#
# Per-file body bodies are not stored — we keep the parsed wikilink target
# labels (``file_wikilinks``) which is what the edge step needs, and is
# smaller. ``compute_cluster_metrics`` stays pure; the cache just decides
# whether to call it.
#
# Same single-worker assumption as _load_all_cache: no cross-worker
# coherence concern.
@dataclass
class _MemoryGraphCache:
    signature: tuple  # ((rel_path, mtime_ns, size), ...) for all tracked files
    topical_signature: tuple  # subset of signature for topical files
    nodes_by_id: dict[str, MemoryNode]  # real (non-ghost) nodes
    file_wikilinks: dict[str, tuple[str, ...]]  # source id → raw target labels
    nodes: list[MemoryNode]  # full materialized list (includes ghosts)
    edges: list[MemoryEdge]
    cluster_metrics: dict[str, Any]


_memory_graph_cache: _MemoryGraphCache | None = None


_FINGERPRINT_EXCLUDE_PREFIXES = ("cortex-memory/dailies/",)


def memory_graph_signature(mind: pathlib.Path) -> tuple:
    """Cheap stat-only fingerprint over the markdown corpus.

    Walks the same roots ``read_memory_graph`` walks, but only stat()s —
    no file reads, no wikilink parsing. Any mtime or size change to a
    tracked ``*.md`` file flips the signature and invalidates the cache.

    Dailies (``cortex-memory/dailies/``) are intentionally excluded
    from the signature: thinking writes to today's daily on every wake
    (access_count bumps, etc.), so including them would invalidate the
    cache constantly during active wakes. Dailies are non-topical, so
    their edits never affect cluster metrics anyway; the only cost is
    that newly-added or freshly-edited daily nodes may take until the
    next non-daily change to surface in the response payload.

    Signature tuples are ``(rel_path, mtime_ns, size)`` where ``rel_path``
    is the path relative to ``mind`` with the ``.md`` suffix retained —
    this is enough to reconstruct both the node id (strip ``.md``) and
    the absolute path (prepend ``mind``) without restatting.
    """
    items: list[tuple[str, int, int]] = []
    for root_name in ("cortex-memory", "memory"):
        root = mind / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            try:
                rel = path.relative_to(mind).as_posix()
            except ValueError:
                continue
            if rel.startswith(_FINGERPRINT_EXCLUDE_PREFIXES):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            items.append((rel, st.st_mtime_ns, st.st_size))
    items.sort()
    return tuple(items)


def _topical_subset(signature: tuple) -> tuple:
    """Return only the topical entries from a memory_graph_signature.

    Filtering happens on the in-memory signature tuple so it costs one
    pass over ~1300 entries with no syscalls.
    """
    return tuple(item for item in signature if _is_topical(_folder_from_rel(item[0])))


def _folder_from_rel(rel: str) -> str:
    """Folder of a rel path like ``cortex-memory/people/alice.md`` → ``cortex-memory/people``.

    Matches the ``folder`` field that ``MemoryNode`` stores.
    """
    parts = rel.split("/")
    if len(parts) == 1:
        return parts[0]
    return "/".join(parts[:-1])


def _rel_to_node_id(rel: str) -> str:
    """``cortex-memory/people/alice.md`` → ``cortex-memory/people/alice``."""
    return rel[:-3] if rel.endswith(".md") else rel


def read_memory_graph(mind: pathlib.Path) -> tuple[list[MemoryNode], list[MemoryEdge]]:
    """Scan cortex-memory + legacy memory/ for wikilinks and return a graph.

    Memoized via the cache layers in ``_MemoryGraphCache``. Callers must
    treat the returned lists as read-only — mutating them would poison
    the cache across requests.

    Both roots are unioned so dated daily logs and legacy curated notes
    show up alongside the groomed wiki, inviting Alice to migrate/link
    them over time. Wikilinks resolve across both roots by title/slug.
    """
    nodes, edges, _ = load_memory_graph_bundle(mind)
    return nodes, edges


def load_memory_graph_bundle(
    mind: pathlib.Path,
) -> tuple[list[MemoryNode], list[MemoryEdge], dict[str, Any]]:
    """Return cached ``(nodes, edges, cluster_metrics)`` for ``mind``.

    Routes that need all three should call this rather than chaining
    ``read_memory_graph`` + ``compute_cluster_metrics`` so the bundle
    is consistent and the cluster reuse path actually fires. The
    returned ``cluster_metrics`` is a shared reference — callers must
    not mutate it (build a new decorated dict if you need to add fields).
    """
    global _memory_graph_cache
    sig = memory_graph_signature(mind)
    c = _memory_graph_cache
    if c is not None and c.signature == sig:
        return c.nodes, c.edges, c.cluster_metrics
    new_cache = _update_memory_graph_cache(mind, sig, c)
    _memory_graph_cache = new_cache
    return new_cache.nodes, new_cache.edges, new_cache.cluster_metrics


def _update_memory_graph_cache(
    mind: pathlib.Path,
    new_sig: tuple,
    prev: _MemoryGraphCache | None,
) -> _MemoryGraphCache:
    """Build the next cache entry, reusing whatever ``prev`` still applies.

    Bodies are only re-read for added/changed files. Cluster metrics are
    reused when the topical subset of the signature hasn't shifted.
    Ghost nodes for unresolved wikilinks are rebuilt from scratch each
    call so any wikilink that newly resolves (or stops resolving) is
    reflected in one pass — re-resolving 1300 sources × handful of links
    is cheap (<10ms in practice).
    """
    topical_sig = _topical_subset(new_sig)

    new_stat: dict[str, tuple[int, int]] = {
        rel: (mtime, size) for rel, mtime, size in new_sig
    }
    if prev is not None:
        old_stat: dict[str, tuple[int, int]] = {
            rel: (mtime, size) for rel, mtime, size in prev.signature
        }
        nodes_by_id = dict(prev.nodes_by_id)
        file_wikilinks = dict(prev.file_wikilinks)
    else:
        old_stat = {}
        nodes_by_id = {}
        file_wikilinks = {}

    # Removed files: drop their nodes + cached wikilinks.
    for rel in old_stat.keys() - new_stat.keys():
        node_id = _rel_to_node_id(rel)
        nodes_by_id.pop(node_id, None)
        file_wikilinks.pop(node_id, None)

    # Added or changed files: re-read body + re-parse wikilinks.
    for rel, stat in new_stat.items():
        if old_stat.get(rel) == stat:
            continue
        path = mind / rel
        node_id = _rel_to_node_id(rel)
        folder = _folder_from_rel(rel)
        label = path.stem
        try:
            st = path.stat()
        except OSError:
            nodes_by_id.pop(node_id, None)
            file_wikilinks.pop(node_id, None)
            continue
        nodes_by_id[node_id] = MemoryNode(
            id=node_id,
            label=label,
            path=str(path),
            folder=folder,
            size=st.st_size,
            mtime=st.st_mtime,
        )
        body = _read_text(path)
        labels: list[str] = []
        for match in WIKILINK_RE.finditer(body):
            t = match.group(1).strip()
            if t:
                labels.append(t)
        file_wikilinks[node_id] = tuple(labels)

    # If nothing tracked, short-circuit.
    if not nodes_by_id:
        return _MemoryGraphCache(
            signature=new_sig,
            topical_signature=topical_sig,
            nodes_by_id=nodes_by_id,
            file_wikilinks=file_wikilinks,
            nodes=[],
            edges=[],
            cluster_metrics={},
        )

    # Label → first node_id lookup, for wikilink resolution.
    by_label: dict[str, str] = {}
    for nid, n in nodes_by_id.items():
        by_label.setdefault(n.label.lower(), nid)

    # Materialize edges + ghost nodes from the cached wikilink labels.
    full_nodes: dict[str, MemoryNode] = dict(nodes_by_id)
    edges: list[MemoryEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for src_id, target_labels in file_wikilinks.items():
        for target_label in target_labels:
            target_id = by_label.get(target_label.lower())
            if target_id is None:
                ghost_id = f"unresolved::{target_label}"
                if ghost_id not in full_nodes:
                    full_nodes[ghost_id] = MemoryNode(
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

    nodes_list = list(full_nodes.values())

    # Cluster metrics: reuse iff the topical subset is byte-identical.
    # Non-topical edits (canvases, decisions-index, etc.) skip the
    # ~100ms label-propagation pass.
    if (
        prev is not None
        and prev.topical_signature == topical_sig
        and prev.cluster_metrics
    ):
        cluster_metrics = prev.cluster_metrics
    else:
        cluster_metrics = compute_cluster_metrics(nodes_list, edges)

    return _MemoryGraphCache(
        signature=new_sig,
        topical_signature=topical_sig,
        nodes_by_id=nodes_by_id,
        file_wikilinks=file_wikilinks,
        nodes=nodes_list,
        edges=edges,
        cluster_metrics=cluster_metrics,
    )


# Folders whose notes count as the "topical subgraph" for cluster
# diagnostics. Dailies are excluded — they bridge every domain by design and
# would force a hairball verdict regardless of topical linking discipline.
# Index/README notes at the cortex root, the legacy operational-instruction
# folders under memory/ (how-to-operate directives, one folder per domain),
# and unresolved ghosts are also excluded — none belong to a single
# topical lobe.
_TOPICAL_CORTEX = frozenset(
    {
        "cortex-memory/people",
        "cortex-memory/projects",
        "cortex-memory/reference",
        "cortex-memory/feedback",
        "cortex-memory/sources",
        "cortex-memory/conflicts",
        "cortex-memory/research",
    }
)


def _is_topical(folder: str) -> bool:
    if folder in _TOPICAL_CORTEX:
        return True
    # Legacy memory/sources and memory/projects fold into cortex-memory's
    # sources/projects categories — keep them topical.
    if folder.startswith("memory/sources") or folder.startswith("memory/projects"):
        return True
    return False


def compute_cluster_metrics(
    nodes: list[MemoryNode],
    edges: list[MemoryEdge],
    *,
    top_hub_count: int = 10,
    min_lobe_size: int = 3,
    max_iterations: int = 30,
) -> dict[str, Any]:
    """Compute cluster-quality metrics over the topical subgraph.

    A "healthy" graph has visible domain lobes (fitness, infra, projects,
    people…) linked sparingly through bridge notes. A hairball — one
    undifferentiated nebula — looks fine to vault_health (zero broken, zero
    orphans) because over-linking satisfies that signal trivially. These
    metrics give an actual cluster-quality reading.

    Returned keys:

    - ``modularity`` — Newman's Q on the label-propagation partition
      (range -0.5..1, in practice 0..1 for connected graphs). Above ~0.3
      means recognizable lobes; ~0.1 or below is a hairball.
    - ``cluster_count`` — number of distinct communities label-propagation
      settled on.
    - ``top_hubs`` — ten highest in-degree topical nodes. The bridge notes;
      candidates for a Stage C hub audit.
    - ``lobe_coverage`` — fraction of topical nodes that live in a cluster
      of size ``>= min_lobe_size``. Low coverage = lots of singletons /
      tiny pairs; high coverage = real lobes formed.
    - ``topical_node_count`` / ``topical_edge_count`` — sanity counters
      for the subgraph the rest of the metrics were computed over.

    Dailies, root-level index/README notes, operational-instruction
    folders under memory/, and unresolved ghosts are excluded.
    """
    topical_ids = {n.id for n in nodes if _is_topical(n.folder)}
    if not topical_ids:
        return {
            "modularity": 0.0,
            "cluster_count": 0,
            "top_hubs": [],
            "lobe_coverage": 0.0,
            "topical_node_count": 0,
            "topical_edge_count": 0,
        }

    topical_edges = [
        (e.source, e.target)
        for e in edges
        if e.source in topical_ids and e.target in topical_ids and e.source != e.target
    ]

    # Undirected adjacency. Directed edge dedup happens upstream in
    # read_memory_graph; here we collapse (a,b)/(b,a) pairs.
    neighbors: dict[str, set[str]] = {nid: set() for nid in topical_ids}
    for s, t in topical_edges:
        neighbors[s].add(t)
        neighbors[t].add(s)

    und_edges: set[tuple[str, str]] = set()
    for nid, ns in neighbors.items():
        for other in ns:
            und_edges.add((nid, other) if nid < other else (other, nid))
    m = len(und_edges)

    # Synchronous label propagation. Each node starts in its own community;
    # each sweep we read every node's current label simultaneously, then for
    # each node compute the most-common label among its neighbors. Ties
    # break by "stay if currently tied for top" first, otherwise lex-min on
    # the label string. Synchronous + stay-if-tied is deterministic and
    # avoids the iteration-order collapse that ruins the asynchronous
    # variant on small symmetric graphs (where a single bridge edge can
    # cascade two lobes into one cluster).
    labels: dict[str, str] = {nid: nid for nid in topical_ids}
    sorted_ids = sorted(topical_ids)
    for _ in range(max_iterations):
        new_labels: dict[str, str] = {}
        for nid in sorted_ids:
            ns = neighbors[nid]
            if not ns:
                new_labels[nid] = labels[nid]
                continue
            counts: dict[str, int] = {}
            for other in ns:
                lbl = labels[other]
                counts[lbl] = counts.get(lbl, 0) + 1
            top_count = max(counts.values())
            candidates = [lbl for lbl, c in counts.items() if c == top_count]
            current = labels[nid]
            if current in candidates:
                new_labels[nid] = current
            else:
                new_labels[nid] = min(candidates)
        if new_labels == labels:
            break
        labels = new_labels

    cluster_sizes: dict[str, int] = {}
    for lbl in labels.values():
        cluster_sizes[lbl] = cluster_sizes.get(lbl, 0) + 1
    cluster_count = len(cluster_sizes)
    nodes_in_lobes = sum(c for c in cluster_sizes.values() if c >= min_lobe_size)
    lobe_coverage = nodes_in_lobes / len(topical_ids)

    # Newman modularity on the label-propagation partition.
    if m == 0:
        modularity = 0.0
    else:
        degrees = {nid: len(ns) for nid, ns in neighbors.items()}
        L: dict[str, int] = {}
        K: dict[str, int] = {}
        for nid in topical_ids:
            K[labels[nid]] = K.get(labels[nid], 0) + degrees[nid]
        for s, t in und_edges:
            if labels[s] == labels[t]:
                L[labels[s]] = L.get(labels[s], 0) + 1
        two_m = 2 * m
        Q = 0.0
        for lbl, k_c in K.items():
            l_c = L.get(lbl, 0)
            Q += (l_c / m) - (k_c / two_m) ** 2
        modularity = Q

    # Top hubs = highest in-degree nodes restricted to the topical subgraph.
    # out_degree alongside in_degree distinguishes sinks (high in / low out)
    # from crossroads (high in / high out) — different recombination roles.
    in_deg: dict[str, int] = {}
    out_deg: dict[str, int] = {}
    for src, tgt in topical_edges:
        in_deg[tgt] = in_deg.get(tgt, 0) + 1
        out_deg[src] = out_deg.get(src, 0) + 1
    label_by_id = {n.id: n.label for n in nodes}
    folder_by_id = {n.id: n.folder for n in nodes}
    hub_ids = sorted(in_deg, key=lambda nid: (-in_deg[nid], nid))[:top_hub_count]
    top_hubs = [
        {
            "id": nid,
            "label": label_by_id.get(nid, nid),
            "folder": folder_by_id.get(nid, ""),
            "in_degree": in_deg[nid],
            "out_degree": out_deg.get(nid, 0),
        }
        for nid in hub_ids
    ]

    # ---- Per-cluster membership for the lobe view --------------------
    # Bucket label-propagation results into stable cluster IDs ("c0",
    # "c1", ...) ordered by size descending. Anything below min_lobe_size
    # collapses into a single "misc" lobe so the overview isn't drowned
    # in singletons. Cross-cluster edges are aggregated into weighted
    # pairs for the inter-bubble layer.
    members_by_lp: dict[str, list[str]] = {}
    for nid, lp in labels.items():
        members_by_lp.setdefault(lp, []).append(nid)

    real_lps = sorted(
        (lp for lp, mem in members_by_lp.items() if len(mem) >= min_lobe_size),
        key=lambda lp: (-len(members_by_lp[lp]), lp),
    )
    misc_lps = [lp for lp in members_by_lp if lp not in set(real_lps)]
    lp_to_cid: dict[str, str] = {lp: f"c{i}" for i, lp in enumerate(real_lps)}
    for lp in misc_lps:
        lp_to_cid[lp] = "misc"
    node_cluster: dict[str, str] = {nid: lp_to_cid[lp] for nid, lp in labels.items()}

    members_by_cid: dict[str, list[str]] = {}
    for nid, cid in node_cluster.items():
        members_by_cid.setdefault(cid, []).append(nid)

    def _dominant_folder(member_ids: list[str]) -> str:
        counts: dict[str, int] = {}
        for nid in member_ids:
            f = folder_by_id.get(nid, "")
            counts[f] = counts.get(f, 0) + 1
        if not counts:
            return ""
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    # Build content-derived cluster_label v0 (top hub's label, kebab-cased,
    # `cl-` prefixed). v0 derives on every rebuild — it's not yet stable
    # across rebuilds (Phase 2 brings the Jaccard registry for that). When
    # a real lobe has no in-degree at all (no bridge note), fall back to
    # the cid string. Misc bucket has no meaningful label.
    # Date prefix (YYYY-MM-DD-) is stripped from the slug so dated daily
    # notes that happen to be the top hub don't produce labels like
    # cl-2026-05-04-truenas-host-down-diagnostic-runbook. If the strip
    # leaves a too-short slug (e.g. the top hub is just a bare date), keep
    # the date so the label still has content.
    _DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")

    def _kebab(s: str) -> str:
        out = []
        prev_dash = False
        for ch in s.lower():
            if ch.isalnum():
                out.append(ch)
                prev_dash = False
            elif not prev_dash:
                out.append("-")
                prev_dash = True
        return "".join(out).strip("-") or "unnamed"

    def _label_for_hub(hub_label: str) -> str:
        slug = _kebab(hub_label)
        stripped = _DATE_PREFIX.sub("", slug)
        # Don't strip if the result would be too short to carry meaning.
        if len(stripped) >= 4:
            slug = stripped
        return f"cl-{slug}"

    clusters_out: list[dict[str, Any]] = []
    cluster_label_by_cid: dict[str, str] = {}
    # Real lobes first, in size-desc order; misc bucket last (only if it exists).
    cid_order = [f"c{i}" for i in range(len(real_lps))]
    if "misc" in members_by_cid:
        cid_order.append("misc")
    for cid in cid_order:
        member_ids = members_by_cid.get(cid, [])
        if not member_ids:
            continue
        # Per-cluster top hubs by in-degree (within topical subgraph).
        local_hubs_ids = sorted(
            (nid for nid in member_ids if nid in in_deg),
            key=lambda nid: (-in_deg[nid], nid),
        )[:5]
        if cid == "misc":
            cluster_label = "cl-misc"
        elif local_hubs_ids:
            cluster_label = _label_for_hub(
                label_by_id.get(local_hubs_ids[0], local_hubs_ids[0])
            )
        else:
            cluster_label = f"cl-{cid}"
        cluster_label_by_cid[cid] = cluster_label
        clusters_out.append(
            {
                "id": cid,
                "label": cluster_label,
                "size": len(member_ids),
                "is_misc": cid == "misc",
                "dominant_folder": _dominant_folder(member_ids),
                "member_ids": member_ids,
                "top_hubs": [
                    {
                        "id": nid,
                        "label": label_by_id.get(nid, nid),
                        "in_degree": in_deg.get(nid, 0),
                        "out_degree": out_deg.get(nid, 0),
                    }
                    for nid in local_hubs_ids
                ],
            }
        )

    # Cross-cluster edge weights — undirected pair → count of wikilinks
    # bridging the two lobes (either direction). Self-pairs skipped.
    # weight_normalized = weight / sqrt(|A| * |B|) — comparable across
    # lobe-size pairs, where raw weight is not. Both are surfaced;
    # "low coupling" only means something on the normalized form.
    cluster_size_by_cid = {
        cid: len(members_by_cid.get(cid, [])) for cid in members_by_cid
    }
    cross_pairs: dict[tuple[str, str], int] = {}
    for s, t in topical_edges:
        cs = node_cluster.get(s)
        ct = node_cluster.get(t)
        if not cs or not ct or cs == ct:
            continue
        key = (cs, ct) if cs < ct else (ct, cs)
        cross_pairs[key] = cross_pairs.get(key, 0) + 1
    cross_edges = []
    for (a, b), w in cross_pairs.items():
        sa = cluster_size_by_cid.get(a, 0)
        sb = cluster_size_by_cid.get(b, 0)
        denom = math.sqrt(sa * sb) if sa and sb else 0.0
        cross_edges.append(
            {
                "source": a,
                "target": b,
                "weight": w,
                "weight_normalized": round(w / denom, 4) if denom else 0.0,
            }
        )

    return {
        "modularity": round(modularity, 4),
        "cluster_count": cluster_count,
        "top_hubs": top_hubs,
        "lobe_coverage": round(lobe_coverage, 4),
        "topical_node_count": len(topical_ids),
        "topical_edge_count": m,
        "clusters": clusters_out,
        "cross_cluster_edges": cross_edges,
        "node_cluster": node_cluster,
    }


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
                body_text = body[fm_match.end() :]
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

            results.append(
                {
                    "id": str(rel),
                    "label": label,
                    "title": title,
                    "score": score,
                    "matched_in": matched_in,
                }
            )

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


def _file_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _dir_signature(root: pathlib.Path) -> tuple:
    """Cheap fingerprint of a directory tree used for cache invalidation.

    Walks one level deep — enough for ``inner/{surface,emergency,notes,thoughts}``
    layouts: each has a flat top, plus optional ``.handled``/``.consumed`` /
    date-bucketed subdirs. We capture each visible directory's mtime + entry
    count, which is sensitive to file creation/deletion (the only mutations
    that happen here — files are written-once).
    """
    if not root.is_dir():
        return ()
    parts: list[tuple[str, float, int]] = []
    try:
        for sub in root.rglob("*"):
            if sub.is_dir():
                try:
                    st = sub.stat()
                except OSError:
                    continue
                try:
                    n = sum(1 for _ in sub.iterdir())
                except OSError:
                    n = 0
                parts.append((str(sub), st.st_mtime, n))
        try:
            st = root.stat()
            parts.append((str(root), st.st_mtime, sum(1 for _ in root.iterdir())))
        except OSError:
            pass
    except OSError:
        return ()
    parts.sort()
    return tuple(parts)


# Memoized merge of all event sources. The signature combines:
#   - JSONL file sizes (cheap stat; tail-parse cache handles diffs internally)
#   - Inner directory fingerprints (file-creation/deletion within the tree)
# Hit → return the cached merged+sorted list. Miss → rebuild.
@dataclass
class _LoadAllCache:
    signature: tuple
    events: list[UnifiedEvent]


_load_all_cache: _LoadAllCache | None = None


def load_all_signature(paths: Paths) -> tuple:
    """Cheap fingerprint of every source `load_all` reads.

    The SSE change-watcher polls this on a short tick and emits a
    ``change`` event whenever the value shifts, so live-update consumers
    (sidebar, wakes/turns first page) only refetch when there's actually
    something new to show.
    """
    return (
        _file_size(paths.thinking_log),
        _file_size(paths.speaking_log),
        _file_size(paths.turn_log),
        _dir_signature(paths.inner / "surface"),
        _dir_signature(paths.inner / "emergency"),
        _dir_signature(paths.inner / "notes"),
        _dir_signature(paths.inner / "thoughts"),
    )


def load_all(paths: Paths) -> list[UnifiedEvent]:
    global _load_all_cache
    sig = load_all_signature(paths)
    if _load_all_cache is not None and _load_all_cache.signature == sig:
        return _load_all_cache.events

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

    _load_all_cache = _LoadAllCache(signature=sig, events=events)
    return events


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Currently-running jobs (the /running tab)
#
# A "job" is anything observable that has a start signal but no matching
# completion signal yet. Three kinds:
#   - wake:      thinking wake (one at a time, serial)
#   - experiment: alice-experiment dispatch (multiple possible)
#   - subagent:  speaking background_task_dispatch_request (multiple possible)
# Jobs older than ``stale_threshold_s`` are dropped — they're zombies
# from crashed runtimes, not actually-running work.


@dataclass
class RunningJob:
    kind: str  # "wake" | "experiment" | "subagent"
    job_id: str
    started_at: float  # unix ts
    elapsed_s: float
    what: str  # one-line description
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "job_id": self.job_id,
            "started_at": self.started_at,
            "elapsed_s": self.elapsed_s,
            "what": self.what,
            "detail": self.detail,
        }


def _open_wakes(
    thinking_log: pathlib.Path,
    cutoff_ts: float,
    now_ts: float,
) -> list[RunningJob]:
    """Thinking is serial — at most one wake open at a time. Walk the
    log and track the last unmatched wake_start. If a closing event
    (wake_end / timeout / exception) follows it, the wake is done.
    """
    open_start: dict[str, Any] | None = None
    for rec in _read_jsonl(thinking_log):
        event = rec.get("event") or ""
        if event == "wake_start":
            open_start = rec
        elif event in ("wake_end", "timeout", "exception"):
            open_start = None
    if open_start is None:
        return []
    ts = float(open_start.get("ts") or 0.0)
    if ts < cutoff_ts:
        return []
    model = open_start.get("model") or "?"
    budget = open_start.get("max_seconds")
    what = f"thinking wake · model={model}"
    if budget:
        what += f" · budget={budget}s"
    return [
        RunningJob(
            kind="wake",
            job_id=f"wake-{int(ts)}",
            started_at=ts,
            elapsed_s=max(0.0, now_ts - ts),
            what=what,
            detail={"model": model, "max_seconds": budget},
        )
    ]


def _open_subagents(
    speaking_log: pathlib.Path,
    cutoff_ts: float,
    now_ts: float,
) -> list[RunningJob]:
    """Track ``background_task_dispatch_request`` events by handle;
    remove on ``background_task_dispatch_complete``. Surviving handles
    above ``cutoff_ts`` are still running."""
    open_by_handle: dict[str, dict[str, Any]] = {}
    for rec in _read_jsonl(speaking_log):
        event = rec.get("event") or ""
        handle = rec.get("handle")
        if not handle:
            continue
        if event == "background_task_dispatch_request":
            open_by_handle[handle] = rec
        elif event == "background_task_dispatch_complete":
            open_by_handle.pop(handle, None)
    out: list[RunningJob] = []
    for handle, rec in open_by_handle.items():
        ts = float(rec.get("ts") or 0.0)
        if ts < cutoff_ts:
            continue
        desc = rec.get("description") or "(no description)"
        principal = rec.get("principal_name") or "?"
        out.append(
            RunningJob(
                kind="subagent",
                job_id=handle,
                started_at=ts,
                elapsed_s=max(0.0, now_ts - ts),
                what=f"{desc} · for {principal}",
                detail={
                    "description": desc,
                    "principal_name": principal,
                    "channel_transport": rec.get("channel_transport"),
                },
            )
        )
    return out


def _open_experiments(
    mind_dir: pathlib.Path,
    cutoff_ts: float,
    now_ts: float,
) -> list[RunningJob]:
    """An experiment is "open" if its state dir exists under
    ``inner/state/experiments/<id>/`` and no completion line exists for
    that id in ``inner/state/experiments.jsonl``. The runner appends to
    the jsonl only at completion, so jsonl presence = done.
    """
    state_dir = mind_dir / "inner/state/experiments"
    if not state_dir.is_dir():
        return []
    jsonl_path = mind_dir / "inner/state/experiments.jsonl"
    completed_ids: set[str] = set()
    if jsonl_path.is_file():
        for rec in _read_jsonl(jsonl_path):
            xid = rec.get("experiment_id")
            if xid:
                completed_ids.add(xid)
    out: list[RunningJob] = []
    for child in state_dir.iterdir():
        if not child.is_dir():
            continue
        xid = child.name
        if xid in completed_ids:
            continue
        # Use the state dir's mtime as the dispatch time — close enough
        # for the running view; the formal dispatched_at is only in the
        # card / jsonl, neither of which is written for in-flight runs.
        try:
            ts = child.stat().st_mtime
        except OSError:
            continue
        if ts < cutoff_ts:
            continue
        # Parse settings.json for hypothesis if available.
        what = f"experiment {xid}"
        settings_path = child / "settings.json"
        if settings_path.is_file():
            try:
                settings = json.loads(
                    settings_path.read_text(encoding="utf-8", errors="replace")
                )
                hypothesis = settings.get("hypothesis") or ""
                if hypothesis:
                    what = _trim(hypothesis, 140)
            except (OSError, json.JSONDecodeError):
                pass
        out.append(
            RunningJob(
                kind="experiment",
                job_id=xid,
                started_at=ts,
                elapsed_s=max(0.0, now_ts - ts),
                what=what,
                detail={"state_dir": str(child)},
            )
        )
    return out


def list_running_jobs(
    paths: Paths,
    now_ts: float | None = None,
    stale_threshold_s: int = 7200,
) -> list[RunningJob]:
    """Return all jobs currently in flight, sorted newest start first.

    Jobs are detected by absence of a completion signal:
      - thinking wakes: ``wake_start`` without matching ``wake_end`` /
        ``timeout`` / ``exception``
      - experiments: state dir under ``inner/state/experiments/<id>/``
        without a completion line in ``experiments.jsonl``
      - speaking sub-agents: ``background_task_dispatch_request``
        without matching ``background_task_dispatch_complete`` by handle

    Jobs whose start signal is older than ``stale_threshold_s`` are
    treated as zombies (crashed runtime, never completed) and dropped.
    The default of two hours is generous enough to catch a 30-minute
    experiment timeout plus normal queueing, but cuts off forgotten state.
    """
    if now_ts is None:
        now_ts = time.time()
    cutoff = now_ts - stale_threshold_s
    jobs: list[RunningJob] = []
    jobs.extend(_open_wakes(paths.thinking_log, cutoff, now_ts))
    jobs.extend(_open_experiments(paths.mind_dir, cutoff, now_ts))
    jobs.extend(_open_subagents(paths.speaking_log, cutoff, now_ts))
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs
