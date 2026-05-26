---
title: rooms/ schema
tags: [cozylobe-cortex, schema, rooms]
created: 2026-05-26
updated: 2026-05-26
---

# rooms/ — one note per physical room

## Frontmatter

```yaml
---
title: Kitchen                     # human-readable room name; becomes filename Kitchen.md
tags: [room, cozylobe-cortex]
created: 2026-05-26
updated: 2026-05-26
floor: 1                           # integer; main floor = 1
adjacent:                          # rooms motion can flow into
  - "[[rooms/Living Room]]"
  - "[[rooms/Dining Room]]"
sensors:                           # motion sensors that cover this room
  - "[[sensors/binary_sensor.hue_kitchen_motion]]"
---
```

* `floor` is optional but recommended — Phase 2's adjacency inference
  uses it to disambiguate stairs vs. cross-floor false positives.
* `adjacent` is symmetric in expectation but not enforced — if A lists
  B as adjacent, the lint command does NOT require B to list A back.
  Half-open rooms (e.g. an archway from kitchen to dining) sometimes
  warrant asymmetric weights when the inline-edge form is used.
* `sensors` is the inverse of each sensor's `room:` field — the lint
  command warns on mismatches.

## Body

Free prose. Use inline typed-weighted edges to record nuance the
frontmatter can't capture:

```
The Kitchen (IS-ADJACENT-TO:0.6)[[rooms/Office]] only through the
hallway, so motion bleed is rare. The (IS-ADJACENT-TO:1.0)
[[rooms/Living Room]] edge is open-doorway and reliable.
```

## What Phase 2+ uses this for

* Build the room adjacency graph for the classify call's input.
* Resolve `sensor → room` lookups when a motion event fires.
* Provide the room-list context the qwen model needs to reason about
  positional inference.
