"""alice_mam — MyAnonamouse (MAM) private-tracker integration.

Slice 1 scope is strictly read-only: a JSON-Search API wrapper that
returns normalised :class:`alice_mam.search.Candidate` rows for a
free-text query. No download, no qBit handoff, no Signal slash
command, no cookie refresh. See :mod:`alice_mam.search` for the
entrypoint.
"""

from alice_mam.search import (
    Candidate,
    load_mam_cookie,
    pick_candidate,
    search_mam,
)

__all__ = [
    "Candidate",
    "load_mam_cookie",
    "pick_candidate",
    "search_mam",
]
