"""Which GitHub repos Alice's forge/status/poller services target.

Historically the repo slugs (``jcronq/alice`` and friends) were hardcoded
in a dozen modules. This module is the single, stdlib-only resolver.

Resolution order (first hit wins), per value:

1. Environment variable (see below).
2. ``<mind>/config/alice.config.json`` -> ``"forge"`` block.
3. Built-in upstream default (preserves legacy behaviour).

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

UPSTREAM_REPOS: tuple[str, ...] = ("jcronq/cozyhem-engine", "jcronq/alice")
UPSTREAM_PRIMARY = "jcronq/alice"
UPSTREAM_BUG_FIX_REPO = "jcronq/modern-mud"

_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


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
    return cfg or UPSTREAM_REPOS


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
    return cfg_list[0] if cfg_list else UPSTREAM_PRIMARY


def bug_fix_repo(mind: Optional[os.PathLike] = None) -> str:
    env = _valid(os.environ.get("ALICE_BUG_FIX_REPO", ""))
    if env:
        return env[0]
    cfg = _valid(_forge_block(mind).get("bug_fix_repo"))
    return cfg[0] if cfg else UPSTREAM_BUG_FIX_REPO
