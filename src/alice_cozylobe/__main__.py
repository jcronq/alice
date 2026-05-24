"""Entry point: ``python -m alice_cozylobe`` ≡ run the daemon."""

from __future__ import annotations

import sys

from .daemon import main


if __name__ == "__main__":
    sys.exit(main())
