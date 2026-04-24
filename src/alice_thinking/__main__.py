"""Entry point: ``python -m alice_thinking`` ≡ one wake."""

from __future__ import annotations

import sys

from .wake import main


if __name__ == "__main__":
    sys.exit(main())
