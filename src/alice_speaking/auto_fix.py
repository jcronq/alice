"""Speaking-side wiring for the ``dispatched-in-flight`` gh-state record.

PR #262 added :func:`alice_daemon.gh_state_mirror.write_dispatched_inflight`
so Thinking's dispatcher can suppress duplicate ``attempt-issue-fix``
surfaces while a worker is mid-flight but hasn't yet pushed a branch /
opened a PR. The race-window write happens here: when Speaking's
``_dispatch_subagent`` is about to spawn a worker, we sniff the prompt
for the auto-fix template header and — if it matches — write the
in-flight note BEFORE the asyncio task starts, so the gh-state mirror
is up-to-date from the moment work begins.

Procedural-logic-in-code per Jason's feedback ("procedural logic lives
in code, not agent instructions"): the LLM doesn't need to call a
separate MCP tool or remember to write the in-flight record. The
template (cortex-memory/reference/auto-fix-worker-prompt.md) defines a
stable leading line we can parse, and this module turns that detection
into the bookkeeping side-effect.

Design: cortex-memory/research/2026-05-19-dispatched-inflight-speaking-wiring.md
Upstream: PR #262 (write_dispatched_inflight implementation).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from alice_daemon import gh_state_mirror


log = logging.getLogger(__name__)


# Matches the leading line of the auto-fix worker prompt template at
# cortex-memory/reference/auto-fix-worker-prompt.md. The shape is
# stable — if the template changes, this regex and the template must
# move together (the test pins the exact format).
_AUTO_FIX_HEADER_RE = re.compile(
    r"^You are an auto-fix worker for issue #(?P<number>\d+) "
    r"in (?P<repo>[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+) "
    r"from @(?P<author>[A-Za-z0-9_\-]+)\.",
    re.MULTILINE,
)


def parse_auto_fix_dispatch(prompt: str) -> Optional[tuple[str, int]]:
    """Return ``(repo, issue_number)`` if ``prompt`` is an auto-fix
    worker dispatch, else ``None``.

    The match keys off the verbatim template leading line — any
    deviation (paraphrased prompt, missing repo slug, etc.) falls
    through to ``None`` so unrelated subagent dispatches don't get a
    spurious in-flight write.
    """
    match = _AUTO_FIX_HEADER_RE.search(prompt or "")
    if match is None:
        return None
    try:
        number = int(match.group("number"))
    except (TypeError, ValueError):
        return None
    return match.group("repo"), number


def record_auto_fix_inflight(
    prompt: str,
    worker_id: str,
) -> Optional[Path]:
    """Write the dispatched-in-flight gh-state note for an auto-fix
    worker spawn. Returns the note path on success, ``None`` when the
    prompt isn't an auto-fix dispatch or the write fails.

    Called from :meth:`SpeakingDaemon._dispatch_subagent` BEFORE the
    asyncio task is created, so the suppression record exists from the
    moment the worker starts. Failure to write is non-fatal — the
    worst case is one duplicate dispatcher surface, far better than
    crashing the dispatch on a transient FS error.
    """
    parsed = parse_auto_fix_dispatch(prompt)
    if parsed is None:
        return None
    repo, number = parsed
    try:
        path = gh_state_mirror.write_dispatched_inflight(
            repo, number, worker_id, title=""
        )
    except Exception:  # noqa: BLE001
        # Don't let a bookkeeping write block the worker. The 4-hour
        # timeout cleanup in gh_state_mirror.main() will reap any
        # stale record; if the worker succeeds and opens a PR, the
        # normal cron-overwrite path replaces this record anyway.
        log.exception(
            "auto-fix in-flight write failed for %s#%d (worker %s)",
            repo,
            number,
            worker_id,
        )
        return None
    log.info(
        "auto-fix in-flight recorded for %s#%d (worker %s) -> %s",
        repo,
        number,
        worker_id,
        path,
    )
    return path


__all__ = [
    "parse_auto_fix_dispatch",
    "record_auto_fix_inflight",
]
