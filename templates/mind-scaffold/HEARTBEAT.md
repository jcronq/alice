# Heartbeat

Alice runs an hourly heartbeat: a short scheduled prompt that lets her
check in on things without waiting for the user. Useful for catching
mid-day check-ins, noticing calendar events, flagging stalled projects.

The heartbeat isn't configured by default — set it up when you have a
specific cadence you want.

## Suggested heartbeat prompts

- "Any proactive messages I should send today (reminders, check-ins)?"
- "Review today's daily log — is there anything I should follow up on?"
- "Scan events.jsonl for patterns since yesterday — notable or concerning?"

## How to enable

Configure a cron trigger outside Alice (e.g., `cron` on the host, or a
GitHub Action) that calls `alice -p "<heartbeat prompt>"` at your desired
cadence.

The bridge's `alice` wrapper works fine for this — it resumes whatever
session the heartbeat last used, so context accumulates.
