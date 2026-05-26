---
title: destinations/ schema
tags: [cozylobe-cortex, schema, destinations]
created: 2026-05-26
updated: 2026-05-26
---

# destinations/ — semantic time-of-day locations

A destination ties a person to a room at a time-window with a purpose.
"Kitchen at 07:00 on weekdays = Jason's breakfast" is one destination
note.

## Frontmatter

```yaml
---
title: Kitchen-at-07:00
tags: [destination, cozylobe-cortex]
created: 2026-05-26
updated: 2026-05-26
person: "[[people/Jason]]"
room: "[[rooms/Kitchen]]"
time_window: "06:30-08:00"
days: [Mon, Tue, Wed, Thu, Fri]           # ISO weekday short names
purpose: breakfast                         # free-form short label
frequency: "observed N/M mornings"         # operator hint; Phase 3 updates this
---
```

* `time_window` is a `HH:MM-HH:MM` range in local time. The cozylobe
  pipeline lives on the alice host whose timezone Jason has set; no
  multi-zone reasoning needed.
* `days` defaults to all 7 if omitted. Onboarding seeds weekday
  destinations only.
* `purpose` is a label for the classify prompt; it has no semantic
  effect beyond what qwen reasons from it.
* `frequency` is the only mutable field — Phase 3 may update it as
  evidence accrues.

## Body

Free prose. Use inline edges to link to the trajectories or
sensor-coverage notes that support this destination:

```
Jason (OFTEN-VISITS:0.8)[[destinations/Kitchen-at-07:00]] —
(CONFIRMED-BY:0.9)[[sensors/binary_sensor.hue_kitchen_motion]] over
multiple weekday mornings.
```

## Optional during onboarding

Destinations are the only category the onboarding CLI does NOT require
the operator to fill in. Empty `destinations/` is valid; Phase 3 will
populate it as patterns emerge.
