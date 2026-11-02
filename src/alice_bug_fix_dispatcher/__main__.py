"""Entry point: ``python -m alice_bug_fix_dispatcher`` ≡ one tick.

Mirrors ``python -m alice_thinking.memory_worker``: the module is
the runnable artifact; the s6 supervisor loop fires it on the
configured cadence. Keeping the per-tick invocation short-lived
(rather than a long-running daemon) lets the supervisor pick up
config edits + apply the same flock-based singleton protection it
already uses for the other services.
"""

from __future__ import annotations

import sys

from .main import main


if __name__ == "__main__":
    sys.exit(main())
