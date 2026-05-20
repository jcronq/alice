#!/usr/bin/env python3
"""Per-issue speaking-agent entrypoint script.

Issue #184 of the SM v2 pipeline revision post-amendment
([[2026-05-13-sm-v2-pipeline-revision]]). Invoked by
:func:`sm.dispatcher.spawn_speaking_agent` for each
``(sm:designed, art:code)`` issue. Reads the spawn dir's ``prompt.txt``,
resolves the configured :class:`alice_thinking.phase.Phase` from
frontmatter (``per_issue_build``), loads the approved design note
referenced by frontmatter, and dispatches the Task / Agent tool with the
design + relevant context so a sub-agent can implement and open a draft
PR.

Thin wrapper around :mod:`sm.speaking_shim` — the real
PhaseRunner-based logic lands in the follow-up sub-issue that replaces
the placeholder shim. Both entry forms (``python -m sm.speaking_shim``
and this script) share the same dispatch logic so the dispatcher can
invoke whichever is closer at hand.
"""

from __future__ import annotations

import pathlib
import sys


def _ensure_src_on_path() -> None:
    """Allow running the script without ``pip install -e .`` in dev shells.

    Inserts the repo's ``src/`` next to this script onto ``sys.path``
    so ``import sm`` resolves to the in-tree package. The venv
    install path (where the package is already on ``sys.path``) is
    unaffected.
    """
    here = pathlib.Path(__file__).resolve().parent
    src = here.parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> int:
    _ensure_src_on_path()
    from sm.speaking_shim import main as _main

    return _main()


if __name__ == "__main__":
    sys.exit(main())
