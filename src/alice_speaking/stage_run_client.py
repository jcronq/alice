"""Daemon-side client for the status-site ``POST /api/stage-run`` edge.

Slice: slice-timeline daemon wire-up. Each item on the status site
(Waiting-on-you / In-flight / Tracked tasks) renders five stage dots
(design → build → review → check → merge) inside its expanded card.
The dots light up when the daemon POSTs a stage-run record to the
status site; this module is the authenticated write edge Alice's
dispatch sites use.

Soft-fail by design: any error, timeout, bad JSON, or non-200 means
"no dot update this time" and is logged at debug level. A dispatch
site MUST NOT crash because the status site hiccupped — the stage
timeline is observability, not a correctness-critical path.

Stdlib only — same shape as :mod:`alice_speaking.infra.presence` so the
daemon doesn't grow an httpx dependency just for a 1 s POST.

The server contract lives at
``src/status_site/server.py::handle_stage_run`` (PR #890).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional


log = logging.getLogger(__name__)


#: Hard cap on the stage-run POST. The handler is a short file append
#: under a lock — a 1.5 s budget is many orders of magnitude more than
#: the happy-path.
DEFAULT_TIMEOUT_S: float = 1.5


#: Default URL. Status-site shares the ``sandbox_default`` docker
#: network with the alice container so DNS resolves to the compose
#: alias; override with ``ALICE_STAGE_RUN_URL`` for anything else.
_FALLBACK_URL: str = "http://status-site:8095/api/stage-run"


#: Shared-key file inside the alice container. Mirrors the presence
#: client's default so a single key rotation flows everywhere.
_KEY_FALLBACK = "/home/alice/alice-mind/inner/status-server/key"


#: Stage vocabulary, mirrors :data:`status_site.stage_runs.STAGES`.
#: Listed here so callers inside the daemon can validate without an
#: import cycle through the status-site package.
STAGES: tuple[str, ...] = ("design", "build", "review", "check", "merge")
_STAGES_SET: frozenset[str] = frozenset(STAGES)


#: Status vocabulary, mirrors :data:`status_site.stage_runs.STATUSES`.
STATUSES: tuple[str, ...] = ("running", "success", "failed")
_STATUSES_SET: frozenset[str] = frozenset(STATUSES)


def _default_url() -> str:
    return os.environ.get("ALICE_STAGE_RUN_URL", _FALLBACK_URL)


def _key_path() -> Path:
    return Path(os.environ.get("ALICE_STATUS_KEY_FILE", _KEY_FALLBACK))


def _load_key() -> str | None:
    try:
        key = _key_path().read_text().strip()
    except OSError:
        return None
    return key or None


# ---------------------------------------------------------------------------
# Stage + item inference — procedural logic the LLM shouldn't have to
# re-derive each dispatch. Called by ``_dispatch_subagent``.


# Prompts that are specifically design work. Order matters: a prompt
# that contains both "design" and "code review" is a review pass of a
# design doc, not a design task.
_DESIGN_HINTS = re.compile(
    r"\b(design\s+doc|design\s+review|architect\s+the|designer\s+agent"
    r"|write\s+a\s+design|produce\s+a\s+design|plan\s+the\s+implementation)\b",
    re.IGNORECASE,
)
_REVIEW_HINTS = re.compile(
    r"\b(code\s*review|lensed\s+review|review\s+pass|adversarial\s+review"
    r"|code-reviewer|security\s+review|/code-review)\b",
    re.IGNORECASE,
)

# Simple ``task-NNNN`` extractor for item_slug inference when the
# worker prompt references an SM v2 task directly.
_TASK_SLUG_RE = re.compile(r"\btask-(\d{4,})\b", re.IGNORECASE)


def infer_stage(description: str, instructions: str) -> str:
    """Infer which stage a dispatch belongs to.

    Rules in priority order:

    * ``review`` when either the description or the prompt has a code-
      review hint (``code review`` / ``lensed review`` / ``review pass``
      / ``code-reviewer`` subagent).
    * ``design`` when the dispatch is a design task (``design doc`` /
      ``architect the`` / ``designer agent`` / ``write a design``).
    * ``build`` for everything else — the overwhelmingly common case
      for Agent / Bash bg-task dispatches.

    Returns a value always in :data:`STAGES`.
    """
    haystack = f"{description}\n{instructions or ''}"
    if _REVIEW_HINTS.search(haystack):
        return "review"
    if _DESIGN_HINTS.search(haystack):
        return "design"
    return "build"


def infer_item_slug(
    description: str,
    instructions: str,
    *,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Infer the status-site item slug a dispatch is in service of.

    Precedence:

    * ``task_id`` (when set, usually the SM v2 ``task-NNNN`` id
      returned by :func:`auto_fix.record_auto_fix_task_intake`).
    * A ``task-NNNN`` marker inside the description or prompt (workers
      occasionally mention it when re-firing an existing task).
    * ``None`` when no item context is available. The dispatch site
      skips emission in that case — not every Agent / Bash call ties
      back to a WoY item.
    """
    if task_id:
        return task_id.strip() or None
    haystack = f"{description}\n{instructions or ''}"
    match = _TASK_SLUG_RE.search(haystack)
    if match is None:
        return None
    return f"task-{match.group(1)}"


# ---------------------------------------------------------------------------
# HTTP write edge.


def post_stage_run(
    *,
    item_slug: str,
    stage: str,
    status: str,
    task_id: str = "",
    started_at: str = "",
    ended_at: Optional[str] = None,
    summary: str = "",
    url: Optional[str] = None,
    key: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> bool:
    """POST one stage-run record. Returns ``True`` on 200, ``False`` otherwise.

    Validates the vocabulary locally so a typo in a dispatch site
    doesn't earn a 400 round-trip from the server. All error modes
    (missing key, non-200, bad JSON, network error) are soft-failed:
    the caller MUST never surface an exception to its dispatch flow.

    Positional args are all keyword-only to make the dispatch-site
    call sites self-documenting — ``item_slug`` / ``stage`` /
    ``status`` are never ambiguous.
    """
    if not item_slug:
        return False
    s = str(stage or "").strip().lower()
    if s not in _STAGES_SET:
        log.debug("stage_run: bad stage %r for %s", stage, item_slug)
        return False
    st = str(status or "").strip().lower()
    if st not in _STATUSES_SET:
        log.debug("stage_run: bad status %r for %s", status, item_slug)
        return False
    if key is None:
        key = _load_key()
    if not key:
        log.debug("stage_run: no key available; skipping POST for %s", item_slug)
        return False
    target = url or _default_url()
    payload: dict = {
        "key": key,
        "item_slug": item_slug,
        "stage": s,
        "status": st,
    }
    if task_id:
        payload["task_id"] = task_id
    if started_at:
        payload["started_at"] = started_at
    if ended_at is not None:
        payload["ended_at"] = ended_at
    if summary:
        payload["summary"] = summary
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        target,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        if opener is None:
            resp = urllib.request.urlopen(req, timeout=timeout)
        else:
            resp = opener.open(req, timeout=timeout)
        with resp:
            code = getattr(resp, "status", 200)
            if code != 200:
                log.debug(
                    "stage_run: non-200 %s for %s/%s", code, item_slug, s
                )
                return False
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug(
            "stage_run: transport error for %s/%s: %s: %s",
            item_slug,
            s,
            type(exc).__name__,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — soft-fail invariant
        log.debug(
            "stage_run: unexpected %s for %s/%s: %s",
            type(exc).__name__,
            item_slug,
            s,
            exc,
        )
        return False


__all__: Iterable[str] = (
    "STAGES",
    "STATUSES",
    "infer_stage",
    "infer_item_slug",
    "post_stage_run",
)
