#!/usr/bin/env python3
"""Thin shim around :func:`alice_cozylobe.cortex_cli.onboard_main`.

The actual onboarding logic lives in the package so it can be wired as
a console entry point (``cozylobe-cortex-onboard``) and imported
cleanly by tests. This script exists for the spec'd invocation
``python scripts/cozylobe_cortex_onboard.py …``.

Run ``--help`` for usage.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure ``src/`` is importable when run from a checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir():
    # Always insert at position 0 so this checkout shadows any other
    # alice_cozylobe install (e.g. a stale editable .pth from another
    # worktree).
    _src_str = str(_SRC)
    if _src_str in sys.path:
        sys.path.remove(_src_str)
    sys.path.insert(0, _src_str)

from alice_cozylobe.cortex_cli import onboard_main  # noqa: E402


if __name__ == "__main__":
    sys.exit(onboard_main())
