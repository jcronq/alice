"""Review-TODO source for the Waiting-on-you tier.

Reads Alice-authored vault-write + outbound-draft TODOs from
``inner/state/review-todos.jsonl``, applies Slice-2's
``.woy-dismissed.jsonl`` overlay and an N-day age cap, and emits rows
+ a markdown sub-list for the WoY render. Memory-worker writes do NOT
appear here — the emit side (``core.review_todos``) is only called by
Alice-authored write paths; the memory-worker tree never imports
``emit_review_todo``.

Rows are ordered oldest-first by ``created`` so Jason's attention
goes to the stalest unapproved change. A click on the headline opens
the ``target`` directly in a new tab; a server-side click handler
(``/api/review-todo-click``) logs the slug into
``.woy-dismissed.jsonl`` so the next tick drops it.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
from typing import Optional

from core.review_todos import (
    DEFAULT_EXPIRY_DAYS,
    STORE_PATH_ENV,
    STORE_PATH_REL,
    active_todos,
    read_review_todos,
)


SOURCE_NAME = "review-todos"


def resolve_store_path() -> pathlib.Path | None:
    """Status-site view of the review-TODOs store path.

    Mirrors ``core.review_todos._resolve_store_path`` but returns
    ``None`` instead of a home-relative fallback — the status site
    runs under a service account whose ``$HOME`` is not the mind
    repo, so guessing would miss the real store. The daemon sets
    ``ALICE_REVIEW_TODOS_PATH`` or ``ALICE_MIND`` explicitly.
    """
    env = os.environ.get(STORE_PATH_ENV)
    if env:
        return pathlib.Path(env)
    mind = os.environ.get("ALICE_MIND")
    if mind:
        return pathlib.Path(mind) / STORE_PATH_REL
    return None


def read_rows(
    dismissed: set[str] | None = None,
    *,
    path: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
    expiry_days: float = DEFAULT_EXPIRY_DAYS,
) -> list[dict]:
    """Return active review-TODO rows for the WoY tier.

    Each row:

        {
          "id": "<record id>",
          "kind": "vault-write" | "outbound-draft",
          "target": "<path or url>",
          "title": "<title>",
          "alice_summary": "<summary or ''>",
          "slug": "<dismissal slug>",
          "headline": "Review changes: <title>",
          "age_seconds": <float or None>,
          "age_days": <float or None>,
          "created": "<iso8601>"
        }

    ``path`` defaults to :func:`resolve_store_path`. ``dismissed`` is
    the slug set from ``.woy-dismissed.jsonl``. ``expiry_days``
    defaults to :data:`core.review_todos.DEFAULT_EXPIRY_DAYS`.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    resolved = path or resolve_store_path()
    if resolved is None:
        return []
    recs = read_review_todos(path=resolved)
    active = active_todos(
        recs, dismissed, now=now, expiry_days=expiry_days
    )
    rows: list[dict] = []
    for rec in active:
        slug = rec.get("_slug") or ""
        title = (rec.get("title") or "").strip()
        headline = f"Review changes: {title}" if title else "Review changes"
        age_days = rec.get("_age_days")
        age_seconds = (
            None if age_days is None else float(age_days) * 86400.0
        )
        rows.append(
            {
                "id": rec.get("id") or "",
                "kind": rec.get("kind") or "",
                "target": rec.get("target") or "",
                "title": title,
                "alice_summary": (rec.get("alice_summary") or "").strip(),
                "slug": slug,
                "headline": headline,
                "age_days": age_days,
                "age_seconds": age_seconds,
                "created": rec.get("created") or "",
            }
        )
    return rows


def render_markdown(rows: list[dict]) -> str:
    """Emit a bullet list for the WoY tier's hand-zone splice.

    Format mirrors the hand-zone convention: one top-level bullet
    whose first bold span is the headline, followed by a plain-text
    tail. The dismissal filter already slug-matches on the bold-title
    so a click on the ``[open]`` link + a ``/api/review-todo-click``
    POST removes the entire bullet on the next render.

    Empty input → ``""`` (caller decides whether to splice an empty
    section or hide it entirely).
    """
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        headline = r.get("headline") or ""
        if not headline:
            # Record with no title + no target — shouldn't exist (emit
            # raises) but guard anyway so a hand-rolled JSONL line
            # can't poison the whole render.
            continue
        summary = (r.get("alice_summary") or "").strip()
        if not summary:
            summary = _default_summary_for_kind(r.get("kind") or "")
        target = (r.get("target") or "").strip()
        tail_bits: list[str] = []
        if summary:
            tail_bits.append(summary)
        if target:
            tail_bits.append(f"[open]({target})")
        tail = " — " + " · ".join(tail_bits) if tail_bits else ""
        lines.append(f"- **{headline}**{tail}")
    return "\n".join(lines)


def _default_summary_for_kind(kind: str) -> str:
    # No hardcoded persona name here — the ``test_no_hardcoded_
    # personae_literals`` AST walker forbids literal "Alice" in
    # ``src/``. These phrases describe the artifact itself, which
    # is agent-neutral anyway.
    if kind == "vault-write":
        return "Vault updated"
    if kind == "outbound-draft":
        return "Outbound draft ready"
    return ""


__all__ = [
    "SOURCE_NAME",
    "resolve_store_path",
    "read_rows",
    "render_markdown",
]
