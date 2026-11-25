"""Agent UI TODO schema + backfill (Phase 2 backend port).

Mirrors the per-item schema in
``reference/agent-ui-testing/shared/src/todo.ts`` so the Phase 2
frontend can read richer fields than the structured-ask row carries
on disk. The on-disk ``asks.jsonl`` schema is NOT changed — this
module builds the richer Todo dict by composing an ask record + its
per-thread messages + the ``.woy-effort.jsonl`` ledger.

Status is INFERRED, never stored. Taxonomy (matches the TS
``TodoStatus`` plus Agent UI's work-state additions):

* ``needs_approval`` — carries a tool-permission request (reserved;
  Phase 2 doesn't produce these yet).
* ``waiting_on_you`` — unresolved ask with no in-flight assistant turn.
* ``in_progress`` — the last thread message is from ``alice`` / ``claude``
  so Jason's the one being waited on.
* ``pending`` — reserved (an ask parked for later).
* ``done`` — ``resolved=True`` in the ask record.

Keep this module stdlib-only — it runs inside the same
``python:3.12-alpine`` surface as the rest of the status site.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Optional, Sequence, TypedDict


TodoStatus = Literal[
    "needs_approval",
    "waiting_on_you",
    "in_progress",
    "pending",
    "done",
]

# Effort levels mirror shared/src/effort.ts (1..4). ``word`` → level so
# the on-disk ``.woy-effort.jsonl`` ledger (which stores the UI's word
# labels, e.g. ``"solid"``) can be lifted into the frontend's numeric
# shape.
EFFORT_WORD_TO_LEVEL: Mapping[str, int] = {
    "spike": 1,
    "prototype": 2,
    "solid": 3,
    "autonomous": 4,
}


class LastMessage(TypedDict, total=False):
    role: str
    text: str
    at: str


class Todo(TypedDict, total=False):
    id: str
    title: str
    status: TodoStatus
    createdAt: str
    updatedAt: str
    lastMessage: Optional[LastMessage]
    unread: bool
    messageCount: int
    unreadCount: int
    detailsExcerpt: str
    effort: Optional[int]
    effortBy: Optional[Literal["claude", "user"]]
    permission: Optional[dict]
    link: Optional[dict]


# ─── backfill helpers ───────────────────────────────────────────────


def title_and_excerpt(text: str) -> tuple[str, str]:
    """Split a free-form ask text into ``(title, excerpt)``.

    Title is the first non-empty line, stripped. Excerpt is the rest,
    with leading blank lines trimmed. Either half can be empty.
    """
    if not text:
        return ("", "")
    lines = text.splitlines()
    title = ""
    rest_start = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip()
            rest_start = i + 1
            break
    rest_lines = lines[rest_start:]
    while rest_lines and not rest_lines[0].strip():
        rest_lines.pop(0)
    return (title, "\n".join(rest_lines).strip())


def effort_level(v: Any) -> Optional[int]:
    """Coerce a raw effort value (from the ledger, body, etc.) into 1-4.

    ``spike``/``prototype``/``solid``/``autonomous`` (case-insensitive)
    map to 1-4. A numeric 1-4 (int or numeric string) passes through.
    ``None`` / empty / out-of-band returns ``None`` (the item has no
    effort set).
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 1 <= v <= 4 else None
    if isinstance(v, float):
        iv = int(v)
        return iv if 1 <= iv <= 4 and iv == v else None
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return None
        if s in EFFORT_WORD_TO_LEVEL:
            return EFFORT_WORD_TO_LEVEL[s]
        if s in ("1", "2", "3", "4"):
            return int(s)
    return None


def infer_status(
    record: Mapping[str, Any], *, thread: Sequence[Mapping[str, Any]] = ()
) -> TodoStatus:
    """Compute the inferred status for an ask record + its thread.

    * ``resolved=True`` → ``done`` (regardless of thread content).
    * Unresolved record whose last thread message is from ``alice`` /
      ``claude`` / ``assistant`` → ``in_progress``.
    * Everything else unresolved → ``waiting_on_you``.
    """
    if record.get("resolved"):
        return "done"
    if thread:
        last = thread[-1]
        role = str(last.get("from") or last.get("role") or "").lower()
        if role in ("alice", "claude", "assistant"):
            return "in_progress"
    return "waiting_on_you"


def _normalize_role(from_field: Any) -> str:
    """Normalise a thread-message role into the Agent UI vocabulary.

    On-disk threads store ``"from": "alice" | "jason"``; the Agent UI
    schema uses ``"role": "claude" | "user" | "system"``. ``alice`` →
    ``claude``; ``jason`` → ``user``; anything else passes through
    (e.g. ``system``).
    """
    role = str(from_field or "").lower()
    if role == "alice":
        return "claude"
    if role == "jason":
        return "user"
    return role or "user"


def build_last_message(msg: Mapping[str, Any]) -> LastMessage:
    """Shape one message dict into the Todo ``lastMessage`` field.

    Text is truncated to 200 chars to match ``TodoManifestItem``
    (shared/src/todo.ts line 47).
    """
    role = _normalize_role(msg.get("from") or msg.get("role"))
    return {
        "role": role,
        "text": str(msg.get("text") or "")[:200],
        "at": str(msg.get("at") or ""),
    }


def build_todo(
    record: Mapping[str, Any],
    *,
    thread: Sequence[Mapping[str, Any]] = (),
    effort: Optional[int] = None,
    effort_by: Optional[str] = None,
    link: Optional[dict] = None,
    permission: Optional[dict] = None,
    unread: Optional[bool] = None,
    unread_count: Optional[int] = None,
) -> Todo:
    """Build one Todo dict from an on-disk ask record (+ optional extras).

    Backfills the fields the TS schema wants but ``asks.jsonl`` doesn't
    carry today: ``title`` (first line of the ask text),
    ``detailsExcerpt`` (the rest), ``status`` (inferred), ``messageCount``
    (len(thread)), ``lastMessage`` (the thread's tail), ``updatedAt``
    (resolved_at when done, else created). Optional fields absent from
    the record default to ``None`` / sensible zeros.
    """
    text = str(record.get("text") or "")
    title, excerpt = title_and_excerpt(text)
    status = infer_status(record, thread=thread)
    msgs = list(thread) if thread else []
    last_message: Optional[LastMessage] = (
        build_last_message(msgs[-1]) if msgs else None
    )
    created = str(record.get("created") or "")
    updated = str(record.get("resolved_at") or record.get("updated") or created)

    if unread is None:
        unread = bool(record.get("unread"))
    if unread_count is None:
        unread_count = int(record.get("unreadCount") or 0)

    out: dict[str, Any] = {
        "id": str(record.get("id") or ""),
        "title": title or text[:80] or "(untitled)",
        "status": status,
        "createdAt": created,
        "updatedAt": updated,
        "lastMessage": last_message,
        "unread": bool(unread),
        "messageCount": len(msgs),
        "unreadCount": int(unread_count),
    }
    if excerpt:
        out["detailsExcerpt"] = excerpt
    if effort is not None:
        out["effort"] = effort
        if effort_by:
            out["effortBy"] = effort_by
    if permission is not None:
        out["permission"] = permission
    if link is not None:
        out["link"] = link
    return out  # type: ignore[return-value]


def build_todos(
    ask_records: Sequence[Mapping[str, Any]],
    *,
    threads: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    effort_by_id: Mapping[str, Any] | None = None,
) -> list[Todo]:
    """Build the full Todo bundle for an SSE snapshot.

    Pulls each ask's thread out of ``threads`` (keyed by ``id``) and
    its effort out of ``effort_by_id`` (also keyed by ``id``). Done
    (``resolved``) asks are included — the Agent UI shell filters
    client-side (same as the current ``done_today`` behaviour but
    without the time-window cutoff; the frontend trims if it wants to).
    """
    out: list[Todo] = []
    threads = threads or {}
    effort_by_id = effort_by_id or {}
    for rec in ask_records:
        rid = str(rec.get("id") or "")
        thread = threads.get(rid) or []
        effort = effort_level(effort_by_id.get(rid))
        # Effort values this list carries came from Jason via the UI
        # (``.woy-effort.jsonl`` is only written by ``/api/effort``), so
        # tag them as ``user`` per the TS schema.
        effort_by = "user" if effort is not None else None
        out.append(build_todo(rec, thread=thread, effort=effort, effort_by=effort_by))
    return out
