---
title: people/ schema
tags: [cozylobe-cortex, schema, people]
created: 2026-05-26
updated: 2026-05-26
---

# people/ — one note per resident or regular visitor

## Frontmatter

```yaml
---
title: Jason
tags: [person, resident, cozylobe-cortex]
created: 2026-05-26
updated: 2026-05-26
role: resident                            # resident | visitor | unknown
time_patterns:                            # links to destinations/
  - "[[destinations/Kitchen-at-07:00]]"
  - "[[destinations/Bedroom-at-23:00]]"
known_phones: ["+14357091512"]            # E.164; cross-references with Signal presence in Phase 4
---
```

* `role` controls how strongly the classify call biases toward this
  person. Residents have continuous time-of-day priors; visitors are
  rare and usually tied to a scheduled event.
* `time_patterns` is a list of destinations the person is known to
  visit. The destinations carry the actual time-window data — this is
  just the index.
* `known_phones` is optional. Phase 4 may use Signal-presence to bias
  identity inference when phones are reachable.

## Body

Free prose for behavioral observations. Use inline edges to capture
patterns:

```
Jason (OFTEN-VISITS:0.8)[[destinations/Kitchen-at-07:00]] on weekday
mornings. The trajectory (DEPENDS-ON:0.7)[[trajectories/Bedroom-Kitchen-Office]]
is well-established.
```

## Special people

The onboarding CLI always creates four notes:

* `Jason.md` — primary resident
* `Katie.md` — primary resident (different time patterns)
* `Mike.md` — visitor (restricted-access principal; see
  `cortex-memory/people/mike.md`)
* `unknown.md` — placeholder when classify can't identify the person

You can add more visitor or regular-guest notes during onboarding's
people step.

## What Phase 2+ uses this for

* Identity inference: which person is the motion source?
* Surfacing decisions: a motion event identified as "Jason at 07:00"
  may trigger an automation; "unknown at 03:00" surfaces as an alert.
