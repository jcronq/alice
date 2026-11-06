"""A2UI emitter for the per-item Review page (issue #651).

One generic ``page:review`` renderer that takes the active slug from a
module-level ``_active_review_slug`` value (set by ``GET /api/review``
as a side effect). This is the resolved open question from the design
note (``cortex-memory/designs/2026-11-06-issue651-a2ui-pivot.md``,
section "Open questions", suggested-default #1): a single generic
renderer with the slug in the data model keeps
``SectionBroadcaster``'s construction contract intact — no
``add_renderer`` / ``remove_renderer`` API surface needed, no per-slug
lazy registration.

Trade-off: the active slug is process-global, so two tabs viewing
different review items race. For Jason's single-user status site this
is acceptable; the alternative (mutable renderer registration) is
larger surface area for a case that doesn't actually occur in use.

Reads the plan body via :func:`status_site.server.read_review` — same
source of truth as ``GET /api/review`` already uses. ``POST
/api/review-action`` (Approve / Modify / Reject) is untouched; the
Approve/Modify/Reject buttons on the surface identify themselves via
``props.action`` strings the client switches on.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from status_site.a2ui import (
    component,
    create_surface,
    update_components,
    update_data_model,
)

if TYPE_CHECKING:  # pragma: no cover
    from status_site.server import Config


SURFACE_ID = "review"

# Process-global active-review slug. The ``/api/review?slug=…``
# handler sets this as a side effect so the ``page:review`` renderer
# knows which note to describe on the next SSE tick. Guarded by a
# plain Lock — assignment is atomic in CPython but the lock documents
# the shared-state intent and leaves room for a "clear" path later.
_lock = threading.Lock()
_active_review_slug: str | None = None


def set_active_slug(slug: str | None) -> None:
    """Record ``slug`` as the active review. Called by ``/api/review``.

    ``None`` clears the slot (used by tests and by an eventual
    "navigated away" fire-and-forget endpoint — the design calls it
    out as a possible follow-up).
    """
    global _active_review_slug
    with _lock:
        _active_review_slug = slug


def get_active_slug() -> str | None:
    """Return the currently active review slug, or ``None`` if unset."""
    with _lock:
        return _active_review_slug


def build_envelopes(review: dict | None) -> list[dict]:
    """Return the full A2UI envelope list for the Review surface.

    ``review`` is the ``read_review`` dict (``slug`` / ``title`` /
    ``repo`` / ``issue`` / ``url`` / ``markdown`` / ``updated``) or
    ``None`` when no slug is active / the note isn't review-shaped.
    An empty envelope list surfaces as a cleared ``{"a2ui": []}``
    payload, which the client treats as "no surface to render".
    """
    if review is None:
        return []

    slug = review["slug"]
    repo = review.get("repo") or ""
    issue = review.get("issue") or ""
    url = review.get("url") or ""
    meta_label = (
        f"{repo}#{issue}"
        if repo and issue
        else (repo or f"review-{slug}")
    )

    # Three action trios (approve/modify/reject). Each is a textarea +
    # a button bound to it. ``variant`` on the button styles it
    # (approve = success green, reject = warn amber) and ``action`` is
    # the semantic op the client switches on when wiring the click.
    # Generic placeholders — the agent's name isn't spliced into the
    # textarea hints here on purpose (test_no_hardcoded_personae_literals
    # forbids a literal "Alice" in src/).
    actions: tuple[tuple[str, str, str, str, str], ...] = (
        # (key, label, hint, button_variant, action)
        ("approve", "Approve", "Optional notes…", "approve", "review.approve"),
        ("modify", "Modify", "What should change? (required)", "modify", "review.modify"),
        ("reject", "Reject", "Why? (optional)", "reject", "review.reject"),
    )

    action_components: list[dict] = []
    action_children: list[str] = []
    for key, label, hint, variant, action in actions:
        ta_id = f"{key}-ta"
        btn_id = f"{key}-btn"
        wrap_id = f"{key}-act"
        action_children.append(wrap_id)
        action_components.append(
            component(
                wrap_id,
                "list",
                props={"variant": f"review-act review-act-{key}", "heading": label},
                children=[ta_id, btn_id],
            )
        )
        action_components.append(
            component(
                ta_id,
                "textarea",
                props={"placeholder": hint, "name": "notes"},
            )
        )
        action_components.append(
            component(
                btn_id,
                "button",
                props={
                    "label": label,
                    "action": action,
                    "textareaId": ta_id,
                    "variant": variant,
                },
            )
        )

    components = [
        component(
            "root",
            "list",
            props={"variant": "review-page"},
            children=["title", "meta", "plan", "actions"],
        ),
        component(
            "title",
            "text",
            props={"markdown": f"# {review.get('title') or slug}"},
        ),
        component(
            "meta",
            "text",
            props={
                "markdown": _meta_markdown(url, meta_label, review.get("updated")),
                "variant": "review-meta",
            },
        ),
        component(
            "plan",
            "text",
            props={
                "markdown": review.get("markdown") or "",
                "variant": "review-plan",
            },
        ),
        component(
            "actions",
            "list",
            props={"variant": "review-actions"},
            children=action_children,
        ),
        *action_components,
    ]

    return [
        create_surface(SURFACE_ID, data={"slug": slug}),
        update_components(SURFACE_ID, components),
        update_data_model(SURFACE_ID, "/slug", slug),
    ]


def _meta_markdown(url: str, label: str, updated: float | None) -> str:
    """Build the meta line: ``[repo#issue](url) · updated <when>``."""
    from datetime import datetime

    parts: list[str] = []
    if url:
        parts.append(f"[{label}]({url})")
    elif label:
        parts.append(label)
    if updated:
        try:
            stamp = datetime.fromtimestamp(float(updated)).strftime(
                "%b %-d, %-I:%M %p"
            )
            parts.append(f"updated {stamp}")
        except (OSError, ValueError, OverflowError):
            pass
    return " · ".join(parts)


def render(cfg: "Config") -> dict:
    """Section renderer for ``page:review``.

    Reads the active slug via :func:`get_active_slug` and the plan via
    ``read_review``. Emits ``{"a2ui": []}`` when nothing is active so
    the SSE tick's dict-equality compare suppresses repeats.
    """
    from status_site.server import read_review

    slug = get_active_slug()
    if not slug:
        return {"a2ui": []}
    review = read_review(cfg, slug)
    if review is None:
        return {"a2ui": []}
    return {"a2ui": build_envelopes(review)}
