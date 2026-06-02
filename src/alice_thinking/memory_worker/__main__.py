"""Entry point: ``python -m alice_thinking.memory_worker`` ≡ one wake.

Mirrors ``python -m alice_thinking`` (the generative wake): the
module is the runnable artifact; the s6 supervisor loop fires it
on the configured cadence. Keeping the per-wake invocation
short-lived (rather than a long-running daemon) lets the
supervisor pick up config edits + apply the same flock-based
singleton protection it already uses for ``alice-thinker``.
"""

from __future__ import annotations

import sys

from .wake import main


if __name__ == "__main__":
    sys.exit(main())
