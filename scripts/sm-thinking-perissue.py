#!/usr/bin/env python3
"""Per-issue thinking-agent entrypoint script.

Sub-issue 3 of the SM v2 pipeline revision
([[2026-05-13-sm-v2-pipeline-revision]]). Invoked by
:func:`sm.dispatcher.spawn_thinking_agent` for each
``(sm:selected, art:code)`` issue. Reads the spawn dir's ``prompt.txt``,
resolves the configured :class:`alice_thinking.phase.Phase` from
frontmatter, and drives the kernel via
:class:`alice_thinking.runtime.PhaseRunner`.

Thin wrapper around :mod:`alice_thinking.cli.perissue` — the real
logic lives in the module so it can be imported by tests and invoked
either as ``python -m alice_thinking.cli.perissue`` or as this script.
"""

from __future__ import annotations

import pathlib
import sys


def _ensure_src_on_path() -> None:
    """Allow running the script without ``pip install -e .`` in dev shells.

    Inserts the repo's ``src/`` next to this script onto ``sys.path``
    so ``import alice_thinking`` resolves to the in-tree package. The
    venv install path (where the package is already on ``sys.path``)
    is unaffected.
    """
    here = pathlib.Path(__file__).resolve().parent
    src = here.parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> int:
    _ensure_src_on_path()
    from alice_thinking.cli.perissue import main as _main

    return _main()


if __name__ == "__main__":
    sys.exit(main())
