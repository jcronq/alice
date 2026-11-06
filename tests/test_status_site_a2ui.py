"""Unit tests for the A2UI v1.0 envelope builders (issue #651).

Message shapes only — no server wiring, no SSE. Guards that each
builder emits the exact envelope keys the A2UI v1.0 Candidate spec
expects so the generic client renderer can rely on them.
"""

from __future__ import annotations

from status_site.a2ui import (
    component,
    create_surface,
    delete_surface,
    update_components,
    update_data_model,
)


# ─── component() ─────────────────────────────────────────────────────


def test_component_basic_shape():
    c = component("root", "list")
    assert c == {"id": "root", "type": "list", "props": {}, "children": []}


def test_component_preserves_props_and_children():
    c = component(
        "composer",
        "textarea",
        props={"placeholder": "Message Alice…", "name": "text"},
        children=[],
    )
    assert c["id"] == "composer"
    assert c["type"] == "textarea"
    assert c["props"] == {"placeholder": "Message Alice…", "name": "text"}
    assert c["children"] == []


def test_component_children_are_id_refs_not_nested_dicts():
    """Adjacency list, per the A2UI spec — children are ids, not dicts."""
    c = component("root", "list", children=["a", "b", "c"])
    assert c["children"] == ["a", "b", "c"]


def test_component_props_and_children_isolated_from_caller():
    """Mutating the dict/list passed in shouldn't mutate the component."""
    props = {"label": "Send"}
    kids = ["ta"]
    c = component("btn", "button", props=props, children=kids)
    props["label"] = "mutated"
    kids.append("extra")
    assert c["props"]["label"] == "Send"
    assert c["children"] == ["ta"]


# ─── create_surface ──────────────────────────────────────────────────


def test_create_surface_minimal():
    env = create_surface("chat")
    assert env == {"type": "createSurface", "surfaceId": "chat"}


def test_create_surface_with_components_and_data():
    comps = [component("root", "list", children=["m"])]
    env = create_surface("chat", components=comps, data={"messages": []})
    assert env["type"] == "createSurface"
    assert env["surfaceId"] == "chat"
    assert env["components"] == comps
    assert env["data"] == {"messages": []}


def test_create_surface_omits_empty_components_and_data():
    env = create_surface("chat", components=[], data=None)
    assert "components" not in env
    assert "data" not in env


def test_create_surface_keeps_empty_data_dict_when_explicit():
    """``data={}`` (empty data model) is still a declaration, not absence."""
    env = create_surface("chat", data={})
    assert env["data"] == {}


# ─── update_components ──────────────────────────────────────────────


def test_update_components_envelope():
    comps = [
        component("root", "list", children=["msg", "ta"]),
        component("ta", "textarea"),
    ]
    env = update_components("chat", comps)
    assert env["type"] == "updateComponents"
    assert env["surfaceId"] == "chat"
    assert env["components"] == comps


def test_update_components_copies_the_list():
    """Builder stores its own list so the caller can't retroactively
    smuggle entries in after the envelope is committed.
    """
    comps = [component("root", "list")]
    env = update_components("chat", comps)
    comps.append(component("extra", "text"))
    assert len(env["components"]) == 1


# ─── update_data_model ──────────────────────────────────────────────


def test_update_data_model_replace():
    env = update_data_model("chat", "/messages", [{"text": "hi"}])
    assert env == {
        "type": "updateDataModel",
        "surfaceId": "chat",
        "path": "/messages",
        "value": [{"text": "hi"}],
    }


def test_update_data_model_empty_pointer_targets_root():
    """RFC 6901: the empty string refers to the whole document."""
    env = update_data_model("chat", "", {"messages": []})
    assert env["path"] == ""
    assert env["value"] == {"messages": []}


def test_update_data_model_accepts_scalar_values():
    env = update_data_model("review", "/slug", "design-review-xyz")
    assert env["value"] == "design-review-xyz"


# ─── delete_surface ─────────────────────────────────────────────────


def test_delete_surface_envelope():
    env = delete_surface("chat")
    assert env == {"type": "deleteSurface", "surfaceId": "chat"}


# ─── composition: a realistic chat page sequence ─────────────────────


def test_chat_page_envelope_sequence_shape():
    """Create → components → data-model: the ordering a page emitter uses.

    Not a semantic test (the client orders tolerant), just a sanity
    pass that the three envelopes compose into a plain JSON-safe list.
    """
    msgs = [{"from": "jason", "text": "hi", "at": "2026-11-06T10:00:00"}]
    envelopes = [
        create_surface("chat", data={"messages": []}),
        update_components(
            "chat",
            [
                component("root", "list", children=["thread", "composer", "send"]),
                component(
                    "thread",
                    "list",
                    props={"boundTo": "/messages"},
                ),
                component(
                    "composer",
                    "textarea",
                    props={"placeholder": "Message Alice…", "name": "text"},
                ),
                component(
                    "send",
                    "button",
                    props={"label": "Send", "action": "chat.send"},
                ),
            ],
        ),
        update_data_model("chat", "/messages", msgs),
    ]
    assert [e["type"] for e in envelopes] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]
    assert all(e["surfaceId"] == "chat" for e in envelopes)
    assert envelopes[-1]["value"] == msgs
