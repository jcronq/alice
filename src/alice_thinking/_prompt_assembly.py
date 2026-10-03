"""Prompt assembly helpers — extracted from ``wake.py`` in Plan 03 Phase 1.

The active-mode prompt is built from two pieces:

1. The wake-time header (local time + day) — Python-rendered so the
   agent doesn't have to compute the local hour herself, which was
   brittle (the bootstrap instruction can drift out of sync with
   mode/stage logic).
2. The bootstrap body — a Jinja template at
   ``prompts/templates/thinking/wake.active.md.j2`` (Plan 04
   Phase 6). The mind's ``inner/directive.md`` is injected as a
   template variable, not as part of the template, so the
   directive stays operator-edited while the bootstrap stays
   runtime-controlled.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


WAKE_TZ = ZoneInfo("America/New_York")


def wake_timestamp_header(
    now: Optional[datetime] = None,
    *,
    max_seconds: int = 0,
) -> str:
    """Return the wake-time header for the prompt.

    The first line is the local time (``Current local time: 2026-04-26
    14:32 EDT (Sunday)``). When ``max_seconds > 0``, three deadline
    lines are appended so the model can pace itself inside the budget:
    wake-start UTC, hard-stop UTC, and a short pacing directive. This
    is the P3 half of the 2026-10-02 wake-death-loop fix — a 39-wake
    streak was hard-killed at the outer 60-min timeout with no state
    persisted, in large part because the model had no visible deadline
    inside the prompt.
    """
    moment = (now or datetime.now(WAKE_TZ)).astimezone(WAKE_TZ)
    local_line = (
        "Current local time: "
        f"{moment.strftime('%Y-%m-%d %H:%M %Z')} ({moment.strftime('%A')})"
    )
    if max_seconds <= 0:
        return local_line
    start_utc = moment.astimezone(timezone.utc)
    stop_utc = start_utc + timedelta(seconds=max_seconds)
    deadline_lines = [
        f"Wake start (UTC): {start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Hard-stop (UTC): {stop_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f" (max_wake_seconds={max_seconds})",
        "Pace yourself. Finish any in-flight write before the hard-stop"
        " or your work is lost.",
    ]
    return "\n".join([local_line, *deadline_lines])


def build_wake_prompt(
    template_name: str,
    *,
    now: Optional[datetime] = None,
    directive_path: Optional[pathlib.Path] = None,
) -> str:
    """Compose a wake prompt via the prompts package.

    ``template_name`` selects the prompt — e.g.
    ``"thinking.wake.active"`` for active mode,
    ``"thinking.wake.sleep.consolidate"`` for Stage B. The directive
    is operator-edited and lives in the mind, so it's loaded as a
    runtime variable and injected via ``{% if directive %}``. Per-
    mind overrides drop into ``mind/.alice/prompts/<same-path>``
    and apply automatically (the wake's PromptLoader carries that
    path).
    """
    from prompts import load as load_prompt

    directive_text = ""
    if directive_path is not None and directive_path.is_file():
        directive_text = directive_path.read_text().strip()
    return load_prompt(
        template_name,
        timestamp_header=wake_timestamp_header(now),
        directive=directive_text,
    )


def build_active_prompt(
    *,
    now: Optional[datetime] = None,
    directive_path: Optional[pathlib.Path] = None,
) -> str:
    """Back-compat shim: delegates to :func:`build_wake_prompt` with
    the active-mode template name. Existing callers stay unchanged."""
    return build_wake_prompt(
        "thinking.wake.active", now=now, directive_path=directive_path
    )
