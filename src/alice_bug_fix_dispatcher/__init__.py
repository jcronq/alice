"""Auto-remediation pipeline for in-game bug reports from Modern MUD.

This is a standalone s6-supervised daemon — it does NOT run inside
Speaking's turn loop. On each tick it:

1. Honours a pause-sentinel file (ships paused; Jason removes the
   sentinel to resume once Wave A is signed off).
2. Scans ``inner/status/bug-*.md`` for the three states it owns:
   * ``open``            → spawn a worker, flip to ``in-progress``,
                           write ``dispatched_at`` + ``worker_task_id``.
   * ``in-progress``     → if the frontmatter already carries a
                           ``pr_url``, poll it. Merged → ``fixed``;
                           closed-unmerged → back to ``open`` with
                           ``last_dispatch_failed_at``.
   * dispatch stale      → ``dispatched_at`` older than the stall TTL
                           with no ``pr_url`` → ``dispatch-failed``.

The state transitions are additive to the frontmatter — all unknown
fields are preserved and writes are atomic (tmp + rename), so hand-
edits by Jason or future renderer-driven fields stay intact.

Design notes:

* Mirrors the shape of ``alice_thinking.memory_worker``: the module
  is the runnable artifact (``python -m alice_bug_fix_dispatcher``),
  the s6 supervisor fires it on the configured cadence. Keeps the
  per-wake process short-lived so a config edit takes effect on the
  next tick without a service restart.
* Idempotency key: the presence of ``dispatched_at`` in a bug file's
  frontmatter. A worker that never produces a PR is caught by the
  stall sweep; it is NEVER silently redispatched.
* Draft-only enforcement is baked into the worker-prompt template's
  literal ``gh pr create --draft`` command — the dispatcher never
  passes a ``--draft``-ish flag of its own, so the invariant can't
  drift through the dispatcher layer. See
  ``cortex-memory/reference/mud-bug-fix-worker-prompt.md``.
"""

from __future__ import annotations

from .main import tick, main

__all__ = ["tick", "main"]
