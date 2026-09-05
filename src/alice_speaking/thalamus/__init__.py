"""Speaking-side thalamus consumers.

Sits downstream of the memory-worker's Stage B thalamus drain
(``alice_thinking.memory_worker.thalamus``) and upstream of the
``inner/surface/`` / ``inner/notes/`` promoters. Applies context
that only the speaking hemisphere owns (deep-work state today,
adaptive attention thresholds tomorrow) to events that already
survived volume filtering.

Modules:

- :mod:`.deep_work` — deep-work protection FSM (pure).
- :mod:`.consumer`  — thin orchestration wrapper around the FSM
  so downstream callers get a stable decision surface without
  reaching into the state machine internals.

See :doc:`cortex-memory/research/2026-08-07-deep-work-implementation-spec`.
"""
