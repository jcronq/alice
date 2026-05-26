---
title: sensors/ schema
tags: [cozylobe-cortex, schema, sensors]
created: 2026-05-26
updated: 2026-05-26
---

# sensors/ — one note per motion sensor

## Frontmatter

```yaml
---
title: binary_sensor.hue_kitchen_motion   # CozyHem entity_id; becomes the filename
tags: [sensor, motion, cozylobe-cortex]
created: 2026-05-26
updated: 2026-05-26
entity_id: binary_sensor.hue_kitchen_motion
kind: PIR                                 # PIR | microwave | mmwave | camera
room: "[[rooms/Kitchen]]"                 # which room this sensor covers
install_date: 2026-05-20
mounting_height: 2.4m                     # optional, helps Phase 4 reason about coverage
sensitivity: normal                       # CozyHem's sensitivity tier
---
```

* `entity_id` is authoritative — the title may be the same string or a
  cleaned-up version, but the field must match what arrives in the SSE
  stream's `entity_id`.
* `kind` constrains how Phase 4 reasons about false-positive
  characteristics (PIR doesn't see through walls; microwave does).
* `room` is required — every sensor must point at an existing room.
  Lint rejects dangling references.
* `install_date` is used for "this sensor hasn't existed long enough to
  bias the trajectory weights" guards in Phase 3.

## Body

Free prose. Useful for one-off notes the schema can't capture:

```
(COVERS:1.0)[[rooms/Kitchen]] — installed in the ceiling above the
sink. Has a known dead-zone behind the island; complement with
(SEE-ALSO)[[sensors/binary_sensor.hue_kitchen_motion_2]] for full
coverage.
```

## What Phase 2+ uses this for

* SSE consumer filters incoming motion events on `INPUT_KINDS`
  allowlist + matches `entity_id` against this list.
* `cortex.sensor_room(vault, entity_id)` resolves an incoming event
  to a room.
* `kind` informs the false-positive prior in the classify call.
