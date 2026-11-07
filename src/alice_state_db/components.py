"""Component bucketing — file-path → component name (issue #821).

Every open GH issue the status-site Overview renders in its
``In SM pipeline`` block is grouped by which top-level ``src/``
component its linked PR touches. This module is the single owner of
that mapping so the poller, the DB readers, the renderer, and the
tests all agree on the exact bucket name for a given path.

Buckets:

* ``src/<name>/...`` → ``<name>`` (first segment under ``src/``)
* ``sandbox/*``, ``Dockerfile*`` (at repo root), ``.github/*`` →
  ``infra``
* Anything else that isn't under ``src/`` → ``misc``

Empty / whitespace-only paths return ``None`` so the caller can drop
them silently (``gh api .../files`` occasionally lists a renamed-from
with a blank filename).

The :func:`components_for_paths` helper returns a *sorted de-duplicated
list* so re-running the poller with the same file set produces
byte-identical rows.
"""

from __future__ import annotations

from typing import Iterable, Optional

#: Bucket name reserved for issues with no linked PR.
PRE_BUILD = "pre-build"

#: Bucket name for sandbox / Docker / CI infra paths.
INFRA = "infra"

#: Catch-all bucket for anything outside ``src/`` and infra.
MISC = "misc"


def component_for_path(path: str) -> Optional[str]:
    """Map a single repo-relative file path to its component bucket.

    ``None`` means "ignore this path" — only returned for empty
    strings. Every other path lands in exactly one bucket.
    """
    if not isinstance(path, str):
        return None
    p = path.strip()
    if not p:
        return None
    # Normalise ``./foo`` and leading slashes defensively — the GH
    # files API returns clean repo-relative paths, but a mirror of
    # another API might not.
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if not p:
        return None

    if p.startswith("src/"):
        rest = p[len("src/") :]
        if not rest:
            return MISC  # a lone ``src/`` entry is nonsensical, bucket as misc
        return rest.split("/", 1)[0]

    # Infra bucket: build / orchestration / CI config lives here.
    if p.startswith("sandbox/") or p.startswith(".github/"):
        return INFRA
    # ``Dockerfile`` (exact) and ``Dockerfile.*`` at repo root are infra.
    head = p.split("/", 1)[0]
    if head == "Dockerfile" or head.startswith("Dockerfile."):
        return INFRA

    return MISC


def components_for_paths(paths: Iterable[str]) -> list[str]:
    """Return the sorted, de-duplicated bucket list for ``paths``.

    Skips entries that :func:`component_for_path` rejects (empty /
    unusable). The sort is a plain alpha sort so downstream diff
    tools (and the SQL ``INSERT`` sequence) are deterministic.
    """
    seen: set[str] = set()
    for p in paths:
        bucket = component_for_path(p)
        if bucket is not None:
            seen.add(bucket)
    return sorted(seen)
