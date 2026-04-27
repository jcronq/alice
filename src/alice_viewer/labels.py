"""Shared metadata for event kinds — humanized labels + visual families.

Kept in lockstep with `static/events.js` (the client-side mirror). If you add
a new event kind, add it here AND in events.js so both the server-rendered
timeline and SSE-appended live events stay aligned.
"""

from __future__ import annotations


KIND_LABELS: dict[str, str] = {
    # wake / turn boundaries
    "wake_start": "wake start",
    "wake_end": "wake end",
    "timeout": "timeout",
    "exception": "exception",
    "signal_turn_start": "signal in",
    "signal_turn_end": "signal done",
    "signal_send": "signal sent",
    "cli_turn_start": "cli in",
    "cli_turn_end": "cli done",
    "cli_send": "cli sent",
    "discord_turn_start": "discord in",
    "discord_turn_end": "discord done",
    "discord_send": "discord sent",
    "surface_dispatch": "surface in",
    "surface_turn_end": "surface done",
    "emergency_dispatch": "emergency in",
    "emergency_turn_end": "emergency done",
    "emergency_voiced": "emergency voiced",
    "emergency_downgraded": "emergency downgrade",
    "emergency_error": "emergency error",
    "emergency_no_recipient": "emergency: no recipient",
    "daemon_start": "daemon start",
    "daemon_ready": "daemon ready",
    "shutdown": "shutdown",
    # tool calls / SDK flow
    "tool_use": "tool call",
    "user_message": "tool result",
    # text blocks
    "assistant_text": "reply",
    "thinking": "thought",
    "assistant_error": "assistant error",
    # results
    "result": "result",
    # config / meta
    "config_reload": "config reload",
    "quiet_queue_enter": "queued (quiet hours)",
    "quiet_queue_drain": "queue drained",
    "system": "system",
    # filesystem artifacts
    "surface_pending": "surface · pending",
    "surface_resolved": "surface · resolved",
    "emergency_pending": "emergency · pending",
    "emergency_resolved": "emergency · resolved",
    "note_pending": "note · pending",
    "note_consumed": "note · consumed",
    "thought_written": "thought · written",
    "turn_log": "signal turn (legacy log)",
}


# Color family per kind. CSS uses `.fam-<family>` classes. Keep values limited
# to this set: tool, text, thought, result, boundary, turn, artifact, note,
# emergency, error, meta.
KIND_FAMILIES: dict[str, str] = {
    # tool
    "tool_use": "tool",
    "user_message": "tool",
    # text / reflection
    "assistant_text": "text",
    "thinking": "thought",
    # result
    "result": "result",
    # boundaries
    "wake_start": "boundary",
    "wake_end": "boundary",
    "daemon_start": "boundary",
    "daemon_ready": "boundary",
    "shutdown": "boundary",
    "surface_turn_end": "boundary",
    "emergency_turn_end": "boundary",
    # turns
    "signal_turn_start": "turn",
    "signal_turn_end": "turn",
    "signal_send": "turn",
    "cli_turn_start": "turn",
    "cli_turn_end": "turn",
    "cli_send": "turn",
    "discord_turn_start": "turn",
    "discord_turn_end": "turn",
    "discord_send": "turn",
    "turn_log": "turn",
    # artifacts
    "surface_dispatch": "artifact",
    "surface_pending": "artifact",
    "surface_resolved": "artifact",
    "thought_written": "thought",
    # notes
    "note_pending": "note",
    "note_consumed": "note",
    # emergencies
    "emergency_dispatch": "emergency",
    "emergency_voiced": "emergency",
    "emergency_pending": "emergency",
    "emergency_resolved": "emergency",
    "emergency_downgraded": "emergency",
    "emergency_no_recipient": "emergency",
    # errors
    "timeout": "error",
    "exception": "error",
    "emergency_error": "error",
    "assistant_error": "error",
    # meta
    "config_reload": "meta",
    "quiet_queue_enter": "meta",
    "quiet_queue_drain": "meta",
    "system": "meta",
}


def humanize(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


def family(kind: str) -> str:
    return KIND_FAMILIES.get(kind, "meta")
