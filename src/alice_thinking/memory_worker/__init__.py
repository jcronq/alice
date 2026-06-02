"""Memory worker — vault grooming on a 30-min cadence.

This package extracts Stage B (inbox drain), Stage C (atomize /
archive / dedupe / orphan removal), and Stage D (recombination)
from :mod:`alice_thinking.wake`'s sleep cycle into a dedicated
s6-supervised service. Design contract:
``cortex-memory/research/2026-06-01-memory-worker-extraction-design.md``.

Phase 1 ships scaffolding only:

* :func:`alice_thinking.memory_worker.main` — sleep-loop entry point
  (cadence configurable via ``alice.config.json``).
* :mod:`alice_thinking.memory_worker.journal` — write-ahead log for
  intended vault mutations; replayed on startup so a crashed worker
  resumes without losing or double-applying operations.
* :mod:`alice_thinking.vault_lock` — shared per-file flock guard
  used by both thinking and the memory worker.

The actual B/C/D stage implementations land in phases 2–4.
"""

from __future__ import annotations

from .wake import main

__all__ = ["main"]
