---
name: log-journal
description: Use when the user shares something they want remembered but that doesn't fit a structured event type ("btw I'm feeling off today", "started reading X", "thinking about quitting Y"). Appends a dated note to the daily log AND a generic note event to events.jsonl. Don't use this for food/workouts/weights — those have their own skills.
---

# log-journal

Example skill. Captures free-form observations the user wants remembered.

## Steps

1. **Append to today's daily log** `memory/YYYY-MM-DD.md` under a
   `## Journal` section (create if missing):

   ```
   - 14:32 — started reading "The Pragmatic Programmer" again
   ```

2. **Append to `memory/events.jsonl`** via the helper:

   ```
   event-log note user topic=<short-topic> summary="<one-liner>"
   ```

3. **Brief acknowledgement.** One line. "Logged." / "Noted." / "Got it."

## Don't

- Don't ask follow-up questions. Log what you have.
- Don't add emojis or excessive commentary.
- Don't speculate about meaning — just record.

## Edge cases

- **Mixed content** (e.g. "ate a burger and I'm feeling off"): log the food
  via log-meal AND the feeling via log-journal. Two events, two daily-log
  bullets.
- **Retroactive log** ("yesterday I ..."): use the stated date in the event
  timestamp AND write to the correct daily log file.
