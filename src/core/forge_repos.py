"""Which GitHub repos Alice's forge/status/poller services target.

Historically the repo slugs (``jcronq/alice`` and friends) were hardcoded
in a dozen modules. This module is the single, stdlib-only resolver.

Resolution order (first hit wins), per value:

1. Environment variable (see below).
2. ``<mind>/config/alice.config.json`` -> ``"forge"`` block.
3. Nothing: raise :class:`ForgeNotConfigured`. There is deliberately no
   built-in default, so a fork never silently targets someone else's repos.

======================  ===========================  ==================
Value                   Env var                      ``forge`` key
======================  ===========================  ==================
all watched repos       ``ALICE_FORGE_REPOS`` (csv)  ``repos`` (list)
primary repo            ``ALICE_FORGE_REPO``         ``primary`` (str)
bug-fix / mud repo      ``ALICE_BUG_FIX_REPO``       ``bug_fix_repo``
======================  ===========================  ==================

If only the repo list is set, the primary is its first entry.
Example (``alice.config.json``)::

    {"forge": {"repos": ["me/alice", "me/other"], "primary": "me/alice"}}
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

class ForgeNotConfigured(RuntimeError):
    """Raised when a forge repo setting has no env var or config value."""


_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _missing(what: str, env: str, key: str) -> ForgeNotConfigured:
    return ForgeNotConfigured(
        f"{what} is not configured. Set {env} in alice.env, or add "
        f'"forge": {{"{key}": ...}} to <mind>/config/alice.config.json '
        "(see core/forge_repos.py)."
    )


def _valid(slugs: Any) -> tuple[str, ...]:
    if isinstance(slugs, str):
        slugs = slugs.split(",")
    if not isinstance(slugs, (list, tuple)):
        return ()
    out: list[str] = []
    for s in slugs:
        s = str(s).strip()
        if _SLUG_RE.match(s) and s not in out:
            out.append(s)
    return tuple(out)


def _forge_block(mind: Optional[os.PathLike] = None) -> dict:
    base = Path(mind) if mind else Path(
        os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind"))
    )
    try:
        data = json.loads((base / "config" / "alice.config.json").read_text())
    except (OSError, ValueError):
        return {}
    block = data.get("forge") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def forge_repos(mind: Optional[os.PathLike] = None) -> tuple[str, ...]:
    env = _valid(os.environ.get("ALICE_FORGE_REPOS", ""))
    if env:
        return env
    cfg = _valid(_forge_block(mind).get("repos"))
    if cfg:
        return cfg
    raise _missing("Forge repo list", "ALICE_FORGE_REPOS", "repos")


def primary_repo(mind: Optional[os.PathLike] = None) -> str:
    env = _valid(os.environ.get("ALICE_FORGE_REPO", ""))
    if env:
        return env[0]
    block = _forge_block(mind)
    cfg = _valid(block.get("primary"))
    if cfg:
        return cfg[0]
    env_list = _valid(os.environ.get("ALICE_FORGE_REPOS", ""))
    if env_list:
        return env_list[0]
    cfg_list = _valid(block.get("repos"))
    if cfg_list:
        return cfg_list[0]
    raise _missing("Primary forge repo", "ALICE_FORGE_REPO", "primary")


def bug_fix_repo(mind: Optional[os.PathLike] = None) -> str:
    env = _valid(os.environ.get("ALICE_BUG_FIX_REPO", ""))
    if env:
        return env[0]
    cfg = _valid(_forge_block(mind).get("bug_fix_repo"))
    if cfg:
        return cfg[0]
    raise _missing("Bug-fix repo", "ALICE_BUG_FIX_REPO", "bug_fix_repo")
