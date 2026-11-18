"""Review-TODO emitter — Alice-authored vault writes + outbound drafts.

When Alice writes a design to ``cortex-memory/`` or drafts outbound
content for Jason (email, PR description, Signal message that needs
approval), the authoring code path calls :func:`emit_review_todo` to
append a record to ``inner/state/review-todos.jsonl``. The status
site's Waiting-on-you tier picks up unresolved records and renders
them under a "Review" sub-bucket as ``Review changes: <title>`` items,
ordered oldest-first. A click on the status site writes a slug into
``.woy-dismissed.jsonl`` (reusing Slice-2's dismissal pattern), and an
N-day age cap (default 7) auto-expires stale records so the tier
doesn't accumulate forever.

Memory-worker writes (mechanical atomize / archive / dedupe /
recombination) do NOT emit — those paths deliberately never import
this module. The negative case is enforced by
``tests/test_review_todos.py::test_memory_worker_does_not_emit`` which
scans the memory-worker tree for imports.

Store shape — one JSON object per line:

    {
      "id": "<12 hex>",
      "kind": "vault-write" | "outbound-draft",
      "target": "<path-or-url>",
      "title": "<short human label>",
      "alice_summary": "<optional 1-2 sentence why>",
      "created": "<iso8601 UTC>"
    }

Best-effort on write failure — a broken store must not break the
calling write path. The main call sites (``commit_approved_draft`` in
``alice_thinking.design_pipeline``) already run inside wake loops
that swallow telemetry errors; this module matches that posture.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import pathlib
import re
import uuid
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# Kinds of review-TODO. Narrow on purpose — the frontend conditions
# bucket labels on this. Add a new literal before wiring a new hook
# point. ``emit_review_todo`` raises ``ValueError`` for unknown kinds
# so a typo in a caller surfaces at the first emit, not later during
# render.
KINDS: tuple[str, ...] = ("vault-write", "outbound-draft")

# Default on-disk store. Resolved per call so tests can swap in a tmp
# path without touching globals.
STORE_PATH_ENV = "ALICE_REVIEW_TODOS_PATH"
STORE_PATH_REL = "inner/state/review-todos.jsonl"
MIND_ROOT_ENV = "ALICE_MIND"

# Default auto-expiry — click OR this many days, whichever comes
# first. Configurable per read via :func:`active_todos`'s
# ``expiry_days`` kwarg.
DEFAULT_EXPIRY_DAYS: float = 7.0

# Defensive cap on the record count read from the store per load.
# Oldest-first iteration, so this caps historical bloat; a full
# dismissal / expiry sweep stays O(N) in the active tail.
MAX_LOAD_RECORDS = 5000


def _resolve_store_path(path: Optional[pathlib.Path]) -> pathlib.Path:
    """Where to write / read the JSONL store.

    Precedence: explicit ``path`` > ``$ALICE_REVIEW_TODOS_PATH`` >
    ``$ALICE_MIND/inner/state/review-todos.jsonl`` >
    ``~/alice-mind/inner/state/review-todos.jsonl``.
    """
    if path is not None:
        return pathlib.Path(path)
    env = os.environ.get(STORE_PATH_ENV)
    if env:
        return pathlib.Path(env)
    mind = os.environ.get(MIND_ROOT_ENV) or str(pathlib.Path.home() / "alice-mind")
    return pathlib.Path(mind) / STORE_PATH_REL


def emit_review_todo(
    kind: str,
    target: str,
    title: str,
    *,
    alice_summary: str = "",
    path: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Append one review-TODO record. Best-effort on write failure.

    Parameters
    ----------
    kind
        One of :data:`KINDS`. Unknown kinds raise :class:`ValueError`.
    target
        Path or URL that a click-through should open. For vault writes
        this is the absolute (or vault-relative) ``.md`` path; for
        outbound drafts it's the PR / email / message URL.
    title
        Short human label — forms the WoY headline
        (``Review changes: <title>``).
    alice_summary
        Optional one- to two-sentence summary of what changed or why
        Jason should look. Falls back to a kind-based label if empty.
    path
        Override the store path. ``None`` (default) resolves via
        :func:`_resolve_store_path`.
    now
        Override the ``created`` timestamp. ``None`` uses UTC now.
    extra
        Extra fields merged into the record. Reserved keys (``id``,
        ``kind``, ``target``, ``title``, ``alice_summary``,
        ``created``) are NOT overwritten by ``extra``.

    Returns
    -------
    dict
        The record as written (even if the disk write failed — the
        caller can inspect the in-memory value for tests).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    title = (title or "").strip()
    target = (target or "").strip()
    if not title and not target:
        raise ValueError("either title or target must be non-empty")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    rec: dict = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "target": target,
        "title": title or target,
        "alice_summary": (alice_summary or "").strip(),
        "created": now.isoformat(),
    }
    if extra:
        for k, v in extra.items():
            if k in rec:
                continue
            rec[k] = v
    store = _resolve_store_path(path)
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        with open(store, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        log.warning("emit_review_todo write failed (path=%s): %r", store, e)
    return rec


def read_review_todos(
    path: Optional[pathlib.Path] = None, *, max_records: int = MAX_LOAD_RECORDS
) -> list[dict]:
    """Load all review-TODO records from the store, oldest first.

    Malformed lines are skipped; missing file returns an empty list.
    Records with an unknown ``kind`` are dropped — the renderer must
    not guess what to do with them.
    """
    store = _resolve_store_path(path)
    try:
        lines = store.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max_records:]:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict) and v.get("kind") in KINDS:
            out.append(v)
    return out


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _title_to_slug(title: str, limit: int = 60) -> str:
    return _SLUG_RE.sub("-", title.lower()).strip("-")[:limit]


def _rec_headline(rec: dict) -> str:
    """Build the WoY headline for a record.

    ``Review changes: <title>`` is the canonical form; the status
    site's ``title_slug`` collapses this to a dismissal slug that
    ``.woy-dismissed.jsonl`` filters on.
    """
    title = (rec.get("title") or "").strip()
    return f"Review changes: {title}" if title else "Review changes"


def slug_for(rec: dict) -> str:
    """Dismissal-slug the status site writes when Jason clicks.

    Kept in sync with the status site's ``title_slug`` output for the
    headline; a click through stores this slug in
    ``.woy-dismissed.jsonl`` so the next render skips the record.
    """
    return _title_to_slug(_rec_headline(rec))


def active_todos(
    records: Iterable[dict],
    dismissed_slugs: set[str] | None = None,
    *,
    now: Optional[_dt.datetime] = None,
    expiry_days: float = DEFAULT_EXPIRY_DAYS,
) -> list[dict]:
    """Filter records to the live tier: not dismissed, not expired.

    Expired = older than ``expiry_days`` days from ``now``. The return
    is sorted oldest-first by ``created`` (ISO-8601 UTC strings sort
    lexicographically as timestamps do, which is the contract
    ``status_site`` relies on for its oldest-first ordering).

    Each returned dict carries two extra keys for the renderer:
    ``_slug`` (the dismissal slug) and ``_age_days`` (float, present
    only when ``created`` parses as a timestamp).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    dismissed = set(dismissed_slugs or ())
    out: list[dict] = []
    for rec in records:
        slug = slug_for(rec)
        if slug in dismissed:
            continue
        created_iso = rec.get("created") or ""
        enriched = {**rec, "_slug": slug}
        try:
            created = _dt.datetime.fromisoformat(created_iso)
            if created.tzinfo is None:
                created = created.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            # Keep records without a parseable timestamp so a bad
            # write can't silently vanish from the tier; sort puts
            # them first (empty string sorts earliest).
            out.append(enriched)
            continue
        age_days = (now - created).total_seconds() / 86400.0
        if age_days >= float(expiry_days):
            continue
        enriched["_age_days"] = age_days
        out.append(enriched)
    out.sort(key=lambda r: r.get("created") or "")
    return out


# --------------------------------------------------------------- hook helpers


def emit_vault_write_todo(
    target_path: pathlib.Path | str,
    title: str,
    *,
    alice_summary: str = "",
    path: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Convenience wrapper for vault-write hooks.

    Call this from the generative write path (thinking's
    ``commit_approved_draft`` et al.) after the markdown lands on
    disk. The memory worker's mechanical writes deliberately do not
    call this.
    """
    return emit_review_todo(
        "vault-write",
        str(target_path),
        title,
        alice_summary=alice_summary,
        path=path,
        now=now,
    )


def emit_outbound_draft_todo(
    target_url_or_path: str,
    title: str,
    *,
    alice_summary: str = "",
    path: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Convenience wrapper for outbound-draft hooks.

    Call this when Alice drafts outbound content (email, PR
    description, Signal message that needs Jason's approval). The
    ``target`` is the URL or filesystem path the click-through should
    open — e.g. the draft PR URL, the Gmail draft URL, or the path to
    a status-site review note.
    """
    return emit_review_todo(
        "outbound-draft",
        target_url_or_path,
        title,
        alice_summary=alice_summary,
        path=path,
        now=now,
    )


__all__ = [
    "KINDS",
    "STORE_PATH_ENV",
    "STORE_PATH_REL",
    "DEFAULT_EXPIRY_DAYS",
    "emit_review_todo",
    "emit_vault_write_todo",
    "emit_outbound_draft_todo",
    "read_review_todos",
    "active_todos",
    "slug_for",
]
