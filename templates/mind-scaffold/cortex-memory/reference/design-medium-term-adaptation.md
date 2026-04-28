---
title: Medium-Term Adaptation Layer — Design Spec
tags: [design, alice-speaking, reliability, compaction]
created: 2026-04-26
related: [design-unified-context-compaction, alice-speaking, 2026-04-26-adaptation-timescales-gap]
---

# Medium-Term Adaptation Layer — Design Spec

> **tl;dr** The daemon has per-turn and nightly adaptation but nothing in between. This spec adds a `SessionAdaptation` state object that accumulates turn-level signals and triggers proactive compaction when rate limits recur or a session runs long. Pure additive change: one new module, 3–4 call sites in daemon.py, two new config keys. No existing logic changes.

## Problem (in one paragraph)

`CompactionArmer` fires exactly once per turn when `effective_tokens > 150K`. That's a single signal checked at a single threshold. Two other signals exist per-turn but are discarded: (1) rate limit events, which correlate with context size and API call density; (2) session length (turn count since last compaction), which predicts approaching the threshold before crossing it. Neither feeds any decision beyond the current turn. Full gap analysis: [[2026-04-26-adaptation-timescales-gap]].

## Design overview

Add a `SessionAdaptation` object to `SpeakingDaemon.__init__`. Update it at natural points in the turn lifecycle. Consult it in `_consumer()` where `_compaction_pending` is already checked.

```
signal / surface / emergency arrives
    ↓
_consumer: reload config → check _compaction_pending → check adaptation
    ↓
handle turn → update adaptation state
```

No change to the compaction machinery itself. `_compaction_pending = True` remains the single trigger; adaptation just adds new paths to set it.

## New module: `alice_speaking/adaptation.py`

```python
"""Session-level adaptation counters.

Accumulates turn signals that cross the per-turn horizon:
  - rate_limit_events: triggers proactive compaction after recurrence
  - turns_since_compaction: triggers proactive compaction on age

Pure state; no I/O. The daemon calls update methods and consults
should_compact_proactively() before each event.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionAdaptation:
    rate_limit_events: int = 0
    consecutive_errors: int = 0
    turns_since_compaction: int = 0
    turn_count: int = 0
    session_start: float = field(default_factory=time.time)

    def on_turn_success(self) -> None:
        """Call after any non-compaction turn completes without error."""
        self.turn_count += 1
        self.turns_since_compaction += 1
        self.consecutive_errors = 0

    def on_turn_error(self, is_rate_limit: bool = False) -> None:
        """Call when a turn raises (including rate limits)."""
        self.consecutive_errors += 1
        if is_rate_limit:
            self.rate_limit_events += 1

    def on_compaction(self) -> None:
        """Call after compaction completes; resets the age counter."""
        self.turns_since_compaction = 0

    def should_compact_proactively(
        self,
        *,
        rate_limit_threshold: int = 2,
        turn_age_threshold: int = 50,
    ) -> tuple[bool, str]:
        """Return (should_compact, reason) for the next event."""
        if self.rate_limit_events >= rate_limit_threshold:
            return True, f"rate_limit_recurrence ({self.rate_limit_events} events)"
        if self.turns_since_compaction >= turn_age_threshold:
            return True, f"session_age ({self.turns_since_compaction} turns since last compaction)"
        return False, ""

    def summary(self) -> dict[str, Any]:
        return {
            "rate_limit_events": self.rate_limit_events,
            "consecutive_errors": self.consecutive_errors,
            "turns_since_compaction": self.turns_since_compaction,
            "turn_count": self.turn_count,
            "session_uptime_s": int(time.time() - self.session_start),
        }
```

## daemon.py changes

### `__init__` — add one line

```python
from .adaptation import SessionAdaptation
# ... in __init__:
self._adapt = SessionAdaptation()
```

### `_handle_signal` — update after turn

In the `finally` block of `_handle_signal`, after the turn log append:

```python
# After turn_log appends:
if error is None:
    self._adapt.on_turn_success()
else:
    is_rl = "rate_limit" in (error or "")
    self._adapt.on_turn_error(is_rate_limit=is_rl)
```

Do the same in `_handle_surface` and `_handle_emergency` `finally` blocks (they already have the `error` variable).

### `_run_compaction` — reset adaptation counter

At the end of `_run_compaction`, after `self._compaction_pending = False`:

```python
self._adapt.on_compaction()
self.events.emit("adaptation_compaction_reset", **self._adapt.summary())
```

### `_consumer` — proactive compaction check

In `_consumer`, after the existing compaction check but before dispatching the event:

```python
if self._compaction_pending:
    await self._run_compaction()

# NEW: proactive adaptation check
if not self._compaction_pending:
    rate_limit_thresh = int(self.cfg.speaking.get(
        "proactive_compaction_after_rate_limits", 2
    ))
    turn_age_thresh = int(self.cfg.speaking.get(
        "proactive_compaction_after_turns", 50
    ))
    should, reason = self._adapt.should_compact_proactively(
        rate_limit_threshold=rate_limit_thresh,
        turn_age_threshold=turn_age_thresh,
    )
    if should:
        log.info("proactive compaction triggered: %s", reason)
        self.events.emit("proactive_compaction_armed", reason=reason,
                         **self._adapt.summary())
        self._compaction_pending = True
        await self._run_compaction()
```

The `not self._compaction_pending` guard prevents double-compaction when both token-threshold and adaptation want to fire on the same event.

## config.py changes

Add two keys to `SPEAKING_DEFAULTS`:

```python
# Proactive compaction: fire after this many rate limit events in a session.
# 0 or None to disable.
"proactive_compaction_after_rate_limits": 2,
# Proactive compaction: fire after this many turns since last compaction,
# regardless of token pressure. 0 or None to disable.
"proactive_compaction_after_turns": 50,
```

Both are hot-reloadable (they're in the `speaking` dict, consulted on each event).

## Events emitted

| Event | When | Payload |
|-------|------|---------|
| `proactive_compaction_armed` | adaptation threshold crossed | `reason`, counters |
| `adaptation_compaction_reset` | after compaction clears age counter | counters |

Both visible in `speaking.log` for debugging.

## What this does NOT change

- Token-threshold compaction (`CompactionArmer`) — unchanged. The proactive path is additive.
- Session identity, Layer 1/2 bootstrap — unchanged.
- Quiet hours, emergency bypass — unchanged.
- Thinking Alice — no impact.

## Thresholds rationale

**`proactive_compaction_after_rate_limits: 2`**  
First rate limit might be transient. Second in the same session suggests the session is genuinely large. Compact then rather than waiting for a third.

**`proactive_compaction_after_turns: 50`**  
Each Signal turn ~3–5K tokens compressed. 50 turns × 4K = ~200K — approaching the 150K threshold even without Sonnet's cache hits inflating the effective count. Belt-and-suspenders against sessions that stay under threshold turn-by-turn but accumulate. Conservative default; Owner can tune via `write_config`.

## File summary

```
alice/src/alice_speaking/
  adaptation.py          ← NEW (the SessionAdaptation class)
  daemon.py              ← 4 small edits (import, __init__, 3 finally blocks, consumer check)
  config.py              ← 2 new default keys
```

Total change: ~60 lines of new code, ~15 lines of edits. No new dependencies.

## Dual use of `turns_since_compaction`

`turns_since_compaction` currently drives only the proactive-compaction trigger. It's also the best predictor of **vault retrieval urgency**: as sessions age, context freshness drops and the vault becomes proportionally more valuable as a supplement (the two are inversely related). The practical consequence is already wired into [[design-retrieval-protocol]]'s trigger table — row: "Session just compacted OR `turns_since_compaction > 30` → re-read the active project note(s)." No code change needed; the counter serves both purposes.

Full structural model: [[2026-04-26-three-tier-information-priority]] §The new insight.

## Related

- [[2026-04-26-adaptation-timescales-gap]] — gap analysis this spec addresses
- [[2026-04-26-compaction-never-fires]] — the original compaction bug; this spec adds new triggers
- [[design-unified-context-compaction]] — full v3 compaction design
- [[alice-speaking]] — daemon architecture overview
- [[2026-04-26-behavioral-adaptation-design]] — companion: extends `SessionAdaptation` to detect Owner's communication mode (command/exploratory/debug) and inject a one-line context hint per turn; addresses the tonal/behavioral gap this spec does not cover
- [[2026-04-26-behavioral-principles-adaptation-synthesis]] — cross-domain synthesis: Owner's 3 operating principles as consistency check; reveals that DEBUG mood should lower the compaction turn-age threshold (~5-line `mood_multiplier` extension)
- [[2026-04-26-three-tier-information-priority]] — `turns_since_compaction` also drives retrieval urgency (dual-trigger; see §Dual use above)
- [[2026-04-26-session-topology-analysis]] — empirical validation: session data confirms adaptation impact is concentrated in the 28% of long sessions generating 69% of turns; `turns_since_compaction >= 50` is confirmed belt-and-suspenders that never fires first
