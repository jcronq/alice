---
title: trajectories/ schema
tags: [cozylobe-cortex, schema, trajectories]
created: 2026-05-26
updated: 2026-05-26
---

# trajectories/ — observed room sequences with weights

A trajectory records a path through the home: "bedroom → kitchen →
office" with a weight reflecting how often that pattern was observed.

**Initially empty.** Phase 3 (guess lifecycle) writes here as classify
calls produce confirmed sequences. Operators may seed a handful by
hand during onboarding if they want to bias early inferences, but
that's optional.

## Frontmatter

```yaml
---
title: Bedroom-Kitchen-Office
tags: [trajectory, cozylobe-cortex]
created: 2026-05-26
updated: 2026-05-26
room_sequence:                            # ordered; one-to-one with the title
  - "[[rooms/Bedroom]]"
  - "[[rooms/Kitchen]]"
  - "[[rooms/Office]]"
weight: 0.7                               # [0.0, 1.0]; fraction of times observed
typical_time: "07:15"
person: "[[people/Jason]]"                # optional — person-attributed
observations: 7                           # count; weight = observations / opportunities
opportunities: 10
---
```

* `room_sequence` must have at least 2 entries (a one-room trajectory
  is just an occupancy fact).
* `weight` is the headline number; `observations / opportunities` is
  the bookkeeping behind it.
* `person` is optional — global trajectories (no person attribution)
  exist when the classify call couldn't disambiguate.

## Body

Free prose explaining the trajectory's context:

```
Morning trajectory: Jason wakes, makes coffee, heads to the office.
(DEPENDS-ON:1.0)[[rooms/Bedroom]], (DEPENDS-ON:1.0)[[rooms/Kitchen]],
(DEPENDS-ON:1.0)[[rooms/Office]] all exist on the main floor; the
sequence (CONFIRMED-BY:0.7)[[destinations/Kitchen-at-07:00]] for
seven out of ten weekday mornings.
```

## Filename convention

`Room1-Room2-Room3.md` with hyphens (NOT the unicode arrow — keeps
filenames git-friendly). The title in frontmatter can use the arrow
if that reads better; lint compares on the title.
