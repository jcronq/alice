"""``python -m alice_forge.dispatcher`` entrypoint.

This shim exists so the ``alice-sm-dispatcher`` bin script (which does
``exec "$VENV_PY" -m alice_forge.dispatcher "$@"``) continues to work after
:mod:`alice_forge.dispatcher` was split from a single module file into a
sub-package. Without ``__main__.py`` the ``-m`` form prints "No module
named alice_forge.dispatcher.__main__".

Delegates to :func:`alice_forge.dispatcher.main`, the same CLI entrypoint
the ``alice-sm`` console-script wires up via the ``project.scripts``
table in ``pyproject.toml``.
"""

from __future__ import annotations

import sys

from . import main


if __name__ == "__main__":
    sys.exit(main())
