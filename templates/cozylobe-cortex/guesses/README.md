---
title: guesses/ schema
tags: [cozylobe-cortex, schema, guesses]
created: 2026-05-26
updated: 2026-05-26
---

# guesses/ — recent and active positional inferences

**Initially empty.** Phase 2+ writes here on every classify call. A
guess captures the model's current best inference for who is in which
room and where they're headed next.

## Frontmatter

```yaml
---
title: guess-2026-05-26T10:15:00Z
tags: [guess, cozylobe-cortex, positional-inference]
created: 2026-05-26T10:15:00Z
updated: 2026-05-26T10:15:00Z
status: pending                           # pending | confirmed | refuted
confidence: 0.6                            # [0.0, 1.0]; classify call's headline number
person: "[[people/Jason]]"
person_confidence: 0.5
room: "[[rooms/Kitchen]]"
next_room_hypothesis: "[[rooms/Office]]"
next_room_confidence: 0.4
trigger_event:                            # the SSE event that triggered this guess
  entity_id: binary_sensor.hue_kitchen_motion
  kind: motion_detected
  timestamp: 2026-05-26T10:15:00Z
evidence:                                 # links to supporting notes
  - "[[sensors/binary_sensor.hue_kitchen_motion]]"
  - "[[guesses/guess-2026-05-26T10:10:00Z]]"
---
```

* `title` is timestamp-based for uniqueness; Phase 2 generates these.
* `status` lifecycle: `pending` → `confirmed` or `refuted`. See design
  §4.2 for the three confirmation signals (implicit, explicit,
  self-evident).
* `confidence` is the overall guess confidence; per-field confidences
  (`person_confidence`, `next_room_confidence`) refine specific
  inferences.

## Body

The classify call's reasoning, in prose, with inline edges to
supporting evidence:

```
Person (likely Jason, confidence 0.5) in Kitchen (confidence 0.6).
(MOTION-TRIGGERED:1.0)[[sensors/binary_sensor.hue_kitchen_motion]] at
10:15. Previous room was Bedroom at 10:10 — (CONFIRMED-BY:0.7)
[[guesses/guess-2026-05-26T10:10:00Z]]. Next-room hypothesis: Office,
(DEPENDS-ON:0.7)[[trajectories/Bedroom-Kitchen-Office]].
Status: pending.
```

## Retention

* Pending guesses: 24h before garbage collection (design §4.5).
* Confirmed guesses: indefinite (they're the trajectory-training data).
* Refuted guesses that were high-confidence: 30 days (highest-signal
  training data; "we were sure and we were wrong").
* Refuted low-confidence guesses: 24h.

## What Phase 2+ uses this for

* Classify call's input includes the last N guesses as context.
* Phase 3's guess lifecycle reads `status: pending` notes and updates
  them based on subsequent events / corrections / time.
* Surfacing protocol checks `confidence > 0.7` + actionability before
  pinging speaking.
