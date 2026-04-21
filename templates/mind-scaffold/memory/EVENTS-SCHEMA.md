# events.jsonl schema

Append-only structured event stream. One JSON object per line.

## Shape

```json
{
  "ts": "2026-04-21T14:32:00-04:00",
  "type": "meal",
  "subject": "user",
  "data": { "...": "type-specific payload" },
  "source": "signal-msg"
}
```

## Fields

- `ts` — ISO-8601 timestamp with timezone offset. MUST be set; defaults to
  `now()` when using the `event-log` CLI.
- `type` — coarse event category. See [types](#types) below.
- `subject` — who/what the event is about. `user`, `system`, or a name.
- `data` — type-specific object. Schema per type below.
- `source` — where the event came from. Free-form; common values:
  `manual`, `signal-msg`, `apple_health`, `google_calendar`.

## Types

Extend this list for your use. Built-in examples:

### `meal`
User ate something. Fields:
- `meal` (string) — `breakfast` | `lunch` | `dinner` | `snack` | `shake`
- `items` (string, optional) — comma-joined food items
- `kcal` (number, optional) — estimated calories
- `protein_g` (number, optional) — protein in grams

### `workout`
User completed a workout. Fields:
- `split` (string) — `upper`, `lower`, `full`, etc.
- `completed` (bool) — did it actually finish
- `duration_min` (number, optional)
- `notes` (string, optional)

### `weight`
Body-weight reading.
- `lbs` OR `kg` (number)
- `scale` (string, optional) — which device reported it

### `reminder`
A timed reminder was fired.
- `text` (string)
- `for_ts` (string, optional) — when it was supposed to fire

### `error`
Something failed.
- `component` (string)
- `message` (string)

### `note`
Catch-all. `{"topic": "...", "summary": "..."}` works.

### `correction`
Overrides a prior event. Include `refers_to_ts` pointing at the original.

## Appending

Never edit events.jsonl in place. Append via the `event-log` CLI:

```bash
event-log meal user meal=breakfast items="yogurt,granola" kcal=350 protein_g=26
event-log weight user lbs=178.2 scale=hume --source apple_health
event-log note system topic=deploy summary="rolled out v0.4"
```

## Why append-only

- Trivially git-mergeable — no edit conflicts
- Full history is the audit log
- Easy to analyze: `jq -c 'select(.type == "meal")' events.jsonl`
