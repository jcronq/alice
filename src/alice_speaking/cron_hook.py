"""Bridge between Speaking's cron tool-calls and the status-site Schedules
panel (issue #815; follow-on to #810 / PR #813).

PR #813 landed the writer API in :mod:`alice_speaking.schedule_state`
and the reader in :mod:`status_site.sources.schedules`, but nothing in
between updates the on-disk snapshot at
``~/alice-mind/inner/state/active-crons.json``. This module closes that
gap: one call per cron-adjacent tool completion from the daemon's
``PostToolUse`` hook flows through :func:`record_cron_tool_outcome`
and mutates the shared :class:`~alice_speaking.schedule_state.ScheduleState`.

Wire-up
-------

- :data:`CRON_TOOL_NAMES` lists the three tool names we react to. The
  caller filters on this before paying for the extraction cost — the
  PostToolUse hook fires on every tool, not just these three.
- :func:`record_cron_tool_outcome` is the single entry point. It is
  intentionally synchronous and swallow-exceptions: a broken sidecar
  write must NOT crash the daemon's hook chain (mid-turn drain + Task
  interception would regress).
- :class:`~alice_speaking.schedule_state.ScheduleState` is injected for
  testability; the daemon passes the process-wide singleton.

Security
--------

Prompts never reach the on-disk snapshot. CronCreate's ``prompt`` input
arg is a JSON field we deliberately do not read here; schedule_state's
:func:`~alice_speaking.schedule_state.ScheduleState.record_cron` uses
an allowlisted parameter list so even accidental leakage upstream
can't propagate. The ``description`` we persist is the model-visible
``humanSchedule`` string returned by CronCreate — safe and useful.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .schedule_state import ScheduleState, global_state


__all__ = [
    "CRON_TOOL_NAMES",
    "WAKEUP_JOB_ID",
    "WAKEUP_CRON_SENTINEL",
    "record_cron_tool_outcome",
]


log = logging.getLogger(__name__)


#: Tool names we hook. The SDK's built-in cron surface is three tools —
#: see ``_bundled/claude`` (``CRON_CREATE_TOOL_NAME`` / ``CRON_DELETE_TOOL_NAME`` /
#: ``SCHEDULE_WAKEUP_TOOL_NAME``). ``CronList`` has no state change so
#: it's not here.
CRON_TOOL_NAMES: frozenset[str] = frozenset({
    "CronCreate",
    "CronDelete",
    "ScheduleWakeup",
})


#: Stable id for the one-shot ``ScheduleWakeup`` slot. The tool doesn't
#: expose a server-side id (it's an in-process timer), and every
#: ``/loop`` iteration re-arms a fresh wakeup — so we use a singleton
#: id and let successive calls overwrite the entry. Avoids littering
#: ``active-crons.json`` with dozens of stale rows.
WAKEUP_JOB_ID = "schedule-wakeup"

#: Placeholder "cron expression" written for the wakeup row. The
#: humaniser in :mod:`status_site.sources.schedules` falls back to the
#: raw string when it doesn't match a known shape, so the panel will
#: display ``@wakeup`` verbatim. The description field carries the
#: human reason.
WAKEUP_CRON_SENTINEL = "@wakeup"


def record_cron_tool_outcome(
    *,
    tool_name: str,
    tool_input: Optional[dict[str, Any]],
    tool_response: Any,
    state: Optional[ScheduleState] = None,
) -> Optional[str]:
    """Mirror one cron-tool completion into the on-disk schedule state.

    Args:
        tool_name: The SDK's ``tool_name`` field from the PostToolUse
            hook input. Non-cron tools return ``None`` immediately.
        tool_input: The raw ``tool_input`` dict the model emitted.
        tool_response: The tool's return payload. Shape varies — see
            the per-tool helpers for defensive extraction.
        state: Optional :class:`ScheduleState` instance (tests use
            their own path). Defaults to the module-level singleton.

    Returns:
        The job id touched (or synthesised for ``ScheduleWakeup``) so
        callers can log, or ``None`` on no-op / failure.

    Never raises: the daemon's hook chain must keep running even if
    the on-disk write failed. Exceptions are logged and swallowed.
    """
    if tool_name not in CRON_TOOL_NAMES:
        return None
    try:
        st = state if state is not None else global_state()
        inp = tool_input or {}
        if tool_name == "CronCreate":
            return _on_cron_create(inp, tool_response, st)
        if tool_name == "CronDelete":
            return _on_cron_delete(inp, tool_response, st)
        if tool_name == "ScheduleWakeup":
            return _on_schedule_wakeup(inp, tool_response, st)
    except Exception:  # noqa: BLE001 — defense in depth
        log.exception(
            "cron_hook: schedule-state update failed for tool=%s",
            tool_name,
        )
    return None


# ─── per-tool handlers ──────────────────────────────────────────────


def _on_cron_create(
    tool_input: dict[str, Any],
    tool_response: Any,
    state: ScheduleState,
) -> Optional[str]:
    """CronCreate → upsert the job.

    Input args (from the SDK bundle): ``{cron, prompt, recurring?, durable?}``.
    Response data: ``{id, humanSchedule, recurring, durable?}``.

    We persist only ``id`` + ``cron`` + the humanised schedule (as
    ``description``). The prompt stays in-process.
    """
    cron = _str_field(tool_input.get("cron"))
    if not cron:
        return None
    job_id = _extract_id(tool_response)
    if not job_id:
        return None
    human = _response_field(tool_response, "humanSchedule")
    description = human if human else ""
    state.record_cron(id=job_id, cron=cron, description=description)
    return job_id


def _on_cron_delete(
    tool_input: dict[str, Any],
    tool_response: Any,
    state: ScheduleState,
) -> Optional[str]:
    """CronDelete → drop the job.

    Input arg: ``{id}``. Response: ``{id}`` structured, OR a plain
    ``"Cancelled job <id>."`` content block depending on which hook
    rung sees it. We prefer the input id (always present on a
    successful call — ``validateInput`` rejects unknown ids) and fall
    back to extraction.
    """
    job_id = _str_field(tool_input.get("id"))
    if not job_id:
        job_id = _extract_id(tool_response) or ""
    if not job_id:
        return None
    state.forget_cron(job_id)
    return job_id


def _on_schedule_wakeup(
    tool_input: dict[str, Any],
    tool_response: Any,
    state: ScheduleState,
) -> Optional[str]:
    """ScheduleWakeup → upsert a one-shot singleton row.

    Response: ``{scheduledFor (epoch_ms), clampedDelaySeconds, wasClamped}``.
    A zero ``scheduledFor`` means "not scheduled" (gate off or loop
    exhausted) and we treat the call as a no-op.

    We use a stable singleton id (:data:`WAKEUP_JOB_ID`) so repeat
    arms overwrite each other — the normal ``/loop`` dynamic-mode
    cadence re-fires the tool every turn, and persistent rows would
    bloat the file.
    """
    scheduled_for = _number(_response_field(tool_response, "scheduledFor"))
    if scheduled_for is None or scheduled_for <= 0:
        return None
    clamped = _number(_response_field(tool_response, "clampedDelaySeconds"))
    reason = _str_field(tool_input.get("reason")) or "loop wake-up"
    delay_hint = ""
    if clamped is not None and clamped > 0:
        delay_hint = f" (+{int(clamped)}s)"
    next_run_iso = _iso_from_epoch_ms(scheduled_for)
    state.record_cron(
        id=WAKEUP_JOB_ID,
        cron=WAKEUP_CRON_SENTINEL,
        description=f"{reason}{delay_hint}",
        next_run_at=next_run_iso,
    )
    return WAKEUP_JOB_ID


# ─── extractors ─────────────────────────────────────────────────────


def _str_field(value: Any) -> str:
    """``str(...)``.strip() that is None-safe + treats whitespace-only
    as empty."""
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> Optional[float]:
    """Coerce ``value`` to ``float`` or return ``None``. Booleans are
    treated as non-numeric to avoid the ``True`` → ``1.0`` footgun."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_field(tool_response: Any, key: str) -> Any:
    """Pick ``key`` from ``tool_response`` regardless of whether the
    SDK handed us the raw data object or a nested ``{"data": {...}}``.

    Returns ``None`` when the response isn't a dict — callers then
    fall through to their own defaults.
    """
    if not isinstance(tool_response, dict):
        return None
    if key in tool_response:
        return tool_response.get(key)
    inner = tool_response.get("data")
    if isinstance(inner, dict):
        return inner.get(key)
    return None


def _extract_id(tool_response: Any) -> Optional[str]:
    """Pull an ``id`` from the tool response.

    Handles three shapes:

    - Direct ``{"id": "..."}``.
    - Nested ``{"data": {"id": "..."}}`` (if the hook pipeline passes
      the handler's full return envelope).
    - Content-block fallback: a plain string like
      ``"Cancelled job <id>."`` (what ``CronDelete`` renders).
    """
    if isinstance(tool_response, dict):
        maybe = _response_field(tool_response, "id")
        s = _str_field(maybe)
        if s:
            return s
        content = tool_response.get("content")
        parsed = _id_from_cancel_string(content)
        if parsed:
            return parsed
    if isinstance(tool_response, str):
        parsed = _id_from_cancel_string(tool_response)
        if parsed:
            return parsed
    return None


def _id_from_cancel_string(content: Any) -> Optional[str]:
    """Parse ``"Cancelled job <id>."`` → ``<id>``. Returns ``None``
    when ``content`` isn't a str or the pattern doesn't match."""
    if not isinstance(content, str):
        return None
    marker = "Cancelled job "
    if not content.startswith(marker):
        return None
    tail = content[len(marker):].rstrip()
    tail = tail.rstrip(".").strip()
    return tail or None


def _iso_from_epoch_ms(epoch_ms: float) -> str:
    """UTC ISO-8601 (seconds resolution) for an epoch-ms timestamp."""
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return dt.isoformat(timespec="seconds")
