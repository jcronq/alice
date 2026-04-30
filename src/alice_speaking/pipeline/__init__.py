"""Pipeline middleware — code that runs around every speaking turn.

These modules don't model the agent or its conversation surface;
they observe / mutate the turn lifecycle:

- :mod:`compaction` — token-threshold detection, summary turn,
  session roll.
- :mod:`dedup` — Signal envelope-id dedup so reconnects don't
  reprocess.
- :mod:`handlers` — SDK ``BlockHandler`` implementations the kernel
  installs per turn (session id capture, compaction arming, CLI
  trace pass-through).
- :mod:`outbox` — outbound dispatch + quiet-queue routing +
  canonical send-event emission.
- :mod:`quiet_hours` — quiet-hours window predicate + queue.
- :mod:`quiet_queue_runner` — background watcher that drains the
  quiet queue when the window ends.

Plan 02 of the speaking refactor moved these from the flat
``alice_speaking/`` top level into this subpackage so the package's
shape is communicated by the directory listing.
"""
