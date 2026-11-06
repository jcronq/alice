"""Working-status rows for the Chat tab (issue #656).

While Alice handles a chat-origin turn, the speaking daemon appends one
JSONL row per lifecycle event to ``$STATUS_DIR/.chat-status.jsonl``:

* ``turn_start`` / ``turn_end`` bracket the turn,
* ``tool_start`` / ``tool_end`` (AG-UI TOOL_CALL_START / TOOL_CALL_END
  shape) carry one tool call each, joined by ``tool_use_id``.

The server folds those rows into one ``working-group`` thread row per
``turn_id`` and merges them into the chat messages by timestamp. The
sidecar keeps status noise out of ``.chat.jsonl`` (which feeds the
"recent chat" tail in the next surface prompt and the history trim).

Stdlib-only: imported by both the HTTP server and the speaking daemon.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROLE = "alice-status"
VARIANT = "working-group"
STATUS_FILE = ".chat-status.jsonl"
WORKING_TEXT = "Working…"
DONE_TEXT = "Worked"
# A group whose turn_end never landed (daemon crash/restart) stops
# claiming to be active after this long.
STALE_AFTER_SECONDS = 3600
MAX_STATUS_ROWS = 2000
MAX_LABEL = 100

_READ_TOOLS = {"Read", "NotebookRead"}
_SEARCH_TOOLS = {"Grep", "Glob", "LS"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_WORKER_TOOLS = {"Task", "Agent"}
_BASH_TOOLS = {"Bash"}
_GIT_READONLY = {
    "status", "diff", "log", "show", "branch", "rev-parse", "remote",
    "ls-files", "blame", "describe", "config",
}
_SIDE_EFFECT_PROGRAMS = {
    "git", "gh", "ssh", "scp", "rsync", "docker", "status-chat",
    "status-reply", "waiting-ask", "s6-svc", "kubectl",
}

_CATEGORY_NOUNS = {
    "read": ("file read", "files read"),
    "search": ("search", "searches"),
    "command": ("quick command", "quick commands"),
    "other": ("other call", "other calls"),
}


def status_path(status_dir: Path) -> Path:
    return Path(status_dir) / STATUS_FILE


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clip(s: str, n: int = MAX_LABEL) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _bash_program(command: str) -> tuple[str, list[str]]:
    """First real program + its args, skipping ``cd x &&`` / env prefixes."""
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            toks = shlex.split(segment)
        except ValueError:
            toks = segment.split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        if not toks or toks[0] in ("cd", "export", "set", "true", "echo"):
            continue
        return toks[0].rsplit("/", 1)[-1], toks[1:]
    return "", []


def describe_tool(name: str, tool_input: Any) -> tuple[str, str, bool]:
    """Return ``(label, category, significant)`` for one tool call.

    ``significant`` calls get their own step row; the rest are folded
    into a single aggregate row per category by :func:`build_groups`.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    base = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    if base in _READ_TOOLS:
        return _clip(f"📖 Read {inp.get('file_path') or inp.get('notebook_path') or ''}"), "read", False
    if base in _SEARCH_TOOLS:
        return _clip(f"🔍 {base} {inp.get('pattern') or inp.get('path') or ''}"), "search", False
    if base in _EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        verb = "Wrote" if base == "Write" else "Edited"
        return _clip(f"✏️ {verb} {path}"), "edit", True
    if base in _WORKER_TOOLS:
        desc = inp.get("description") or inp.get("subagent_type") or "background task"
        return _clip(f"🚀 Dispatched worker: {desc}"), "worker", True
    if base == "send_message":
        who = inp.get("recipient") or inp.get("to") or inp.get("channel") or ""
        return _clip(f"✉️ Sent message{' to ' + str(who) if who else ''}"), "message", True
    if base == "append_note":
        return "📝 Appended note", "note", True
    if base in _BASH_TOOLS:
        command = str(inp.get("command") or "")
        prog, args = _bash_program(command)
        side_effect = prog in _SIDE_EFFECT_PROGRAMS or "deploy" in command.lower()
        if prog == "git" and args and args[0] in _GIT_READONLY:
            side_effect = False
        if side_effect:
            icon = "🚀" if prog in ("gh", "git") or "deploy" in command.lower() else "🔧"
            return _clip(f"{icon} {' '.join([prog, *args])}"), "bash", True
        return _clip(f"⚙️ {command}"), "command", False
    return _clip(f"🔌 {base}"), "other", False


# ─── write side (speaking daemon) ────────────────────────────────────


class ChatStatusWriter:
    """Appends status rows for one chat-origin turn. Never raises."""

    def __init__(self, status_dir: Path, turn_id: str) -> None:
        self._path = status_path(status_dir)
        self.turn_id = turn_id

    def _append(self, kind: str, **fields: Any) -> None:
        row = {
            "from": ROLE,
            "role": ROLE,
            "kind": kind,
            "turn_id": self.turn_id,
            "at": now_iso(),
            **fields,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            # Status is best-effort observability; never break the turn.
            pass

    def turn_start(self) -> None:
        self._append("turn_start", text=WORKING_TEXT)

    def turn_end(self, error: str | None = None) -> None:
        self._append("turn_end", text=DONE_TEXT, **({"error": error} if error else {}))

    def tool_start(self, name: str, tool_input: Any, tool_use_id: str) -> None:
        label, category, significant = describe_tool(name, tool_input)
        self._append(
            "tool_start",
            tool_use_id=tool_use_id,
            tool=name,
            text=label,
            category=category,
            significant=significant,
        )

    def tool_end(self, tool_use_id: str, is_error: bool = False) -> None:
        self._append("tool_end", tool_use_id=tool_use_id, is_error=bool(is_error))


# ─── read side (status-site server) ──────────────────────────────────


def read_status_rows(status_dir: Path, limit: int = MAX_STATUS_ROWS) -> list[dict]:
    p = status_path(status_dir)
    if not p.is_file():
        return []
    rows: list[dict] = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("turn_id"):
            rows.append(row)
    return rows[-limit:] if limit else rows


def _parse_ts(s: object) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return None


def _aggregate_label(counts: dict[str, int]) -> str:
    parts = []
    for cat, (one, many) in _CATEGORY_NOUNS.items():
        n = counts.get(cat, 0)
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    return "🔍 " + " · ".join(parts)


def build_groups(rows: list[dict], now: float | None = None) -> list[dict]:
    """Fold raw status rows into one ``working-group`` row per ``turn_id``.

    Order follows each turn's first row. Groups with a ``turn_end`` are
    ``done``; so are groups whose last activity is older than
    :data:`STALE_AFTER_SECONDS`.
    """
    now = time.time() if now is None else now
    turns: dict[str, dict] = {}
    for r in rows:
        tid = str(r.get("turn_id"))
        g = turns.setdefault(
            tid,
            {"at": r.get("at", ""), "last": r.get("at"), "done": False, "error": None,
             "calls": {}, "order": []},
        )
        g["last"] = r.get("at") or g["last"]
        kind = r.get("kind")
        if kind == "turn_start":
            g["at"] = r.get("at", g["at"])
        elif kind == "turn_end":
            g["done"] = True
            g["error"] = r.get("error")
        elif kind == "tool_start":
            uid = str(r.get("tool_use_id") or f"anon-{len(g['order'])}")
            g["calls"][uid] = {
                "id": uid,
                "label": r.get("text") or r.get("tool") or "tool",
                "category": r.get("category") or "other",
                "significant": bool(r.get("significant")),
                "state": "running",
                "at": r.get("at", ""),
            }
            g["order"].append(uid)
        elif kind == "tool_end":
            call = g["calls"].get(str(r.get("tool_use_id")))
            if call:
                call["state"] = "error" if r.get("is_error") else "done"

    out = []
    for tid, g in turns.items():
        last_ts = _parse_ts(g["last"])
        done = g["done"] or (last_ts is not None and now - last_ts > STALE_AFTER_SECONDS)
        steps = []
        counts: dict[str, int] = {}
        aggregate_state = "done"
        for uid in g["order"]:
            c = g["calls"][uid]
            if c["significant"]:
                steps.append({k: c[k] for k in ("id", "label", "state", "at")})
            else:
                counts[c["category"]] = counts.get(c["category"], 0) + 1
                if c["state"] == "running":
                    aggregate_state = "running"
        if counts:
            steps.append(
                {"id": f"{tid}-quiet", "label": _aggregate_label(counts),
                 "state": aggregate_state, "at": g["last"] or ""}
            )
        out.append(
            {
                "from": ROLE,
                "role": ROLE,
                "variant": VARIANT,
                "turn_id": tid,
                "at": g["at"],
                "active": not done,
                "text": DONE_TEXT if done else WORKING_TEXT,
                "step_count": len(g["order"]),
                "steps": steps,
                **({"error": g["error"]} if g["error"] else {}),
            }
        )
    return out


def merge_thread(messages: list[dict], groups: list[dict]) -> list[dict]:
    """Interleave working-groups into the message list by timestamp.

    Stable: a group sorts after any message with an equal-or-earlier
    ``at`` (the Jason message that triggered the turn) and before a
    later Alice reply. A group with no parsable timestamp goes last.
    """
    if not groups:
        return list(messages)
    merged = list(messages)
    for g in groups:
        gts = _parse_ts(g.get("at"))
        if gts is None:
            merged.append(g)
            continue
        idx = len(merged)
        for i, m in enumerate(merged):
            mts = _parse_ts(m.get("at"))
            if mts is not None and mts > gts:
                idx = i
                break
        merged.insert(idx, g)
    return merged
