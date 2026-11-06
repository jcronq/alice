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
``handle_chat`` / ``POST /api/chat`` are untouched — this module only
describes the UI around them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
                    children=["thread", "composer", "send"],
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
    from status_site.server import read_chat

    messages = read_chat(cfg)
    return {"a2ui": build_envelopes(messages)}
