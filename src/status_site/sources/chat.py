"""A2UI emitter for the Chat page (issue #651).

Describes the ``chat`` surface as a server-sent component tree:

* ``createSurface("chat", data={"messages": []})`` opens the surface
  with an empty messages data model.
* ``updateComponents`` emits the static shape — a thread list bound
  to ``/messages``, a textarea, and a Send button whose click handler
  is identified by ``props.action = "chat.send"`` (the client does
  the POST to ``/api/chat``; the server never interprets the action).
* ``updateDataModel("/messages", [...])`` carries the current chat
  log, newest-at-end.

The whole envelope list rides ``page:chat`` on the existing SSE
stream. Since the broadcaster's tick does a deep dict-equality compare,
an unchanged chat log costs one compare per tick and emits nothing.
Working-status groups (issue #656) ride the same ``/messages`` list as
rows with ``variant: "working-group"`` — one per turn, carrying its
tool-call ``steps`` — interleaved by timestamp between Jason's message
and Alice's reply. They come from the ``.chat-status.jsonl`` sidecar,
not ``.chat.jsonl``. ``POST /api/chat`` is unchanged by this module — it only
describes the UI around them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from status_site.chat_status import (
    _parse_ts,
    build_groups,
    merge_thread,
    read_message_ats,
    read_status_rows,
)
from status_site.a2ui import (
    component,
    create_surface,
    update_components,
    update_data_model,
)

if TYPE_CHECKING:  # pragma: no cover
    from status_site.server import Config


SURFACE_ID = "chat"


def build_envelopes(messages: list[dict]) -> list[dict]:
    """Return the full A2UI envelope list for the Chat surface.

    Three envelopes: createSurface, updateComponents (static shape),
    updateDataModel (current messages). Replayed in order on each
    section.update — the client holds the surface state locally and
    rebuilds from the list.
    """
    return [
        create_surface(SURFACE_ID, data={"messages": []}),
        update_components(
            SURFACE_ID,
            [
                component(
                    "root",
                    "list",
                    props={"variant": "chat-page"},
                    children=["thread", "composer"],
                ),
                component(
                    "thread",
                    "list",
                    props={"boundTo": "/messages", "variant": "chat-thread"},
                ),
                component(
                    "composer",
                    "textarea",
                    props={
                        # Generic placeholder — the chat partner's
                        # name is set by the persona loader at the
                        # layer above, and hardcoding it here would
                        # fail test_no_hardcoded_personae_literals.
                        "placeholder": "Type a message…",
                        "name": "text",
                        # Client wires Enter-to-send, paste-image and the
                        # file picker for this textarea; ``submitId`` is
                        # the button whose action Enter triggers.
                        "attachments": True,
                        "submitId": "send",
                    },
                ),
                component(
                    "send",
                    "button",
                    props={
                        "label": "Send",
                        "action": "chat.send",
                        "textareaId": "composer",
                    },
                ),
            ],
        ),
        update_data_model(SURFACE_ID, "/messages", list(messages)),
    ]


def render(cfg: "Config") -> dict:
    """Section renderer for ``page:chat``.

    Reads the chat log via :func:`status_site.server.read_chat` and
    emits a plain ``{"a2ui": [...]}`` payload. The broadcaster's
    ``_safe_render`` coerces a renderer's return into a dict payload,
    so this shape rides unchanged over the SSE wire as a page section
    entry.
    """
    # Local import to avoid a server↔sources cycle at module load:
    # server imports this module via section_renderers.
    from status_site.server import CHAT_SNAPSHOT_LIMIT, read_chat

    recent = read_chat(cfg, limit=CHAT_SNAPSHOT_LIMIT)
    status_rows = read_status_rows(cfg.status_dir)
    groups = build_groups(status_rows)
    read_ats = read_message_ats(status_rows)
    if read_ats:
        recent = [
            {**m, "status": "read"}
            if m.get("from") == "jason" and m.get("at") in read_ats else m
            for m in recent
        ]
    if len(recent) >= CHAT_SNAPSHOT_LIMIT:
        # Older history is only reachable via ``/api/chat?before=``; drop
        # working-groups that predate the snapshot window so they don't
        # float above the first message.
        floor = _parse_ts(recent[0].get("at"))
        if floor is not None:
            groups = [
                g for g in groups
                if (ts := _parse_ts(g.get("at"))) is None or ts >= floor
            ]
    return {"a2ui": build_envelopes(merge_thread(recent, groups))}


PREVIEW_CHARS = 80
_PREVIEW_START = "His new message:\n"
_PREVIEW_END = "\n\nAttachments"
_ATTACHMENT_ONLY = "(no text: attachment only)"
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


def chat_preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    """Short plain-text preview of a ``status-chat`` surface body.

    Reads the message between ``His new message:`` and ``Attachments``,
    drops HTML-ish tags, collapses whitespace and elides past ``limit``
    chars with ``…``. Attachment-only messages yield ``📎 attachment``;
    a body without the expected shape yields ``""``.
    """
    start = body.find(_PREVIEW_START)
    if start < 0:
        return ""
    start += len(_PREVIEW_START)
    end = body.find(_PREVIEW_END, start)
    raw = body[start : end if end >= 0 else len(body)].strip()
    if raw == _ATTACHMENT_ONLY:
        return "📎 attachment"
    text = " ".join(_TAG_RE.sub("", raw).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
