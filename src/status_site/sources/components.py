"""Component derivation for the SM pipeline board (issue #821).

Jason's "In SM pipeline" was rendering as one flat list, which is
misleading — ``alice_forge`` is literally ``src/alice_forge/`` and only
one sibling of many. This module answers the "which ``src/`` component
does this issue touch?" question from a file path.

The GH poller (:mod:`alice_state_db.gh_poll`) uses
:func:`component_for_path` to fan out a PR's touched files into
``item_external_refs`` rows (``ref_type='touches_component'``,
``ref_target_item_id='component:<name>'``). The status-site reader
(:mod:`status_site.sources.db_readers`) joins those rows back when
building the pipeline list, and :mod:`status_site.sources.pipeline`
groups the rendered rows by component.

Design note: the ``src/<dir>/`` regex is intentionally generic — any
new top-level directory under ``src/`` becomes a bucket automatically
without a hardcoded list. :data:`CANONICAL_BUCKET_ORDER` is only used
to pin the *display* order of the well-known buckets; everything else
sorts alphabetically before ``pre-build``.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: Display order for the known-at-design-time buckets. Anything not in
#: this list (e.g. a brand-new top-level dir under ``src/``) is
#: appended alphabetically immediately before ``pre-build``, which
#: always sorts last.
CANONICAL_BUCKET_ORDER: tuple[str, ...] = (
    "alice_forge",
    "alice_speaking",
    "alice_thinking",
    "alice_state_db",
    "alice_bug_fix_dispatcher",
    "alice_pricing",
    "status_site",
    "watchers",
    "core",
    "eval",
    "infra",
    "pre-build",
)

_PRE_BUILD = "pre-build"
_INFRA = "infra"

_SRC_RE = re.compile(r"^src/([^/.][^/]*)/")


def component_for_path(path: str) -> str | None:
    """Map a repo-relative path to a component bucket, or ``None``.

    Rules (in priority order):

    1. ``src/<dir>/...`` → ``<dir>``. Matches every existing top-level
       dir under ``src/`` without a hardcoded list.
    2. ``sandbox/...`` or ``s6/...`` or a basename starting with
       ``Dockerfile`` → ``"infra"``.
    3. Anything else (root-level ``README.md``, ``tests/...``,
       ``docs/...``, ``pyproject.toml``) → ``None``. The caller treats
       ``None`` as "no component signal" — multi-file PRs with at
       least one matching path still pick up those buckets, and a PR
       touching *only* unmatched paths (e.g. ``tests/...``-only) falls
       through to the issue-label fallback or ``pre-build``.
    """
    s = (path or "").strip()
    if not s:
        return None
    m = _SRC_RE.match(s)
    if m:
        return m.group(1)
    if s.startswith("sandbox/") or s.startswith("s6/"):
        return _INFRA
    name = PurePosixPath(s).name
    if name.startswith("Dockerfile"):
        return _INFRA
    return None


def order_buckets(names: list[str] | set[str]) -> list[str]:
    """Return ``names`` ordered per :data:`CANONICAL_BUCKET_ORDER`.

    Buckets present in the canonical order appear in that order (minus
    any that aren't in ``names``). Buckets outside the canonical list
    are appended alphabetically immediately before ``pre-build``, which
    always sorts last if present.
    """
    seen = set(names)
    out: list[str] = []
    canonical = set(CANONICAL_BUCKET_ORDER)
    for c in CANONICAL_BUCKET_ORDER:
        if c == _PRE_BUILD:
            continue
        if c in seen:
            out.append(c)
    unknowns = sorted(n for n in seen if n not in canonical)
    out.extend(unknowns)
    if _PRE_BUILD in seen:
        out.append(_PRE_BUILD)
    return out
