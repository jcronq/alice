"""A2UI v1.0 Candidate message builders (issue #651).

Server-side envelope builders for the four A2UI message types the
status-site surfaces emit. Stdlib-only (same invariant as
``_sse.py``/``server.py`` — the module docstrings there spell it out)
and intentionally un-typed: each function returns a plain ``dict`` that
rides the existing ``/api/events`` SSE stream as a section payload
entry (``"page:<slug>": {"a2ui": [envelope, ...]}``). Dict equality is
deep, so ``SectionBroadcaster.tick()``'s change-detection compare keeps
working unchanged for page sections.

Shapes match the A2UI v1.0 Candidate spec
(``a2ui.org/specification/v1.0-a2ui/``):

* ``createSurface`` opens a surface with an optional initial component
  tree + data model.
* ``updateComponents`` is a flat adjacency list of ``{id, type, props,
  children}`` nodes; the client buffers by id until ``id: "root"``
  arrives then walks ``children`` refs incrementally.
* ``updateDataModel`` is a JSON-Pointer-addressed value replace into
  the surface's data model (how list contents / form values update
  without resending the tree).
* ``deleteSurface`` tears down a surface + its components/data.

We don't invent a ``Component`` dataclass wrapper — :func:`component`
is a tiny constructor returning the same ``{"id", "type", "props",
"children"}`` dict the spec expects, kept consistent with how the rest
of the status-site module hands plain dicts over the wire (see
``collect_bugs`` / ``running_now.render``).
"""

from __future__ import annotations

from typing import Any


# ─── component constructor ───────────────────────────────────────────


def component(
    id: str,
    type: str,
    *,
    props: dict[str, Any] | None = None,
    children: list[str] | None = None,
) -> dict:
    """Build one A2UI component node.

    ``children`` is a list of component ids (adjacency list, per the
    spec) — not nested component dicts. Empty props / children are
    stored as empty containers rather than omitted so the client's
    walker can rely on the keys being present.
    """
    return {
        "id": id,
        "type": type,
        "props": dict(props) if props else {},
        "children": list(children) if children else [],
    }


# ─── envelope builders ──────────────────────────────────────────────


def create_surface(
    surface_id: str,
    *,
    components: list[dict] | None = None,
    data: dict | None = None,
) -> dict:
    """``createSurface`` envelope. Opens ``surface_id`` with optional
    initial components + data model.
    """
    envelope: dict[str, Any] = {
        "type": "createSurface",
        "surfaceId": surface_id,
    }
    if components:
        envelope["components"] = list(components)
    if data is not None:
        envelope["data"] = dict(data)
    return envelope


def update_components(surface_id: str, components: list[dict]) -> dict:
    """``updateComponents`` envelope. Flat list of components to add/update
    by id into ``surface_id``.
    """
    return {
        "type": "updateComponents",
        "surfaceId": surface_id,
        "components": list(components),
    }


def update_data_model(surface_id: str, path: str, value: Any) -> dict:
    """``updateDataModel`` envelope. JSON-Pointer-addressed replace of
    ``value`` into ``surface_id``'s data model at ``path``.

    Pointer syntax follows RFC 6901: ``""`` is the root document;
    ``"/foo/bar"`` navigates into nested keys. The client resolves
    pointers at render time against a per-surface ``dataModel`` object.
    """
    return {
        "type": "updateDataModel",
        "surfaceId": surface_id,
        "path": path,
        "value": value,
    }


def delete_surface(surface_id: str) -> dict:
    """``deleteSurface`` envelope. Tears down ``surface_id`` + its
    components + data model on the client.
    """
    return {
        "type": "deleteSurface",
        "surfaceId": surface_id,
    }
