"""Persistent cluster identity registry — Phase 2 of the lobe-context
work. Implements Alice's stable cluster-naming spec from
``cortex-memory/research/2026-05-05-design-cluster-naming.md``.

Cluster IDs (``cl-<slug>``) are minted at birth from the highest
in-degree non-generic hub and **frozen forever**. Across rebuilds,
Jaccard matching against ``last_member_set`` decides which fresh
community inherits which existing ID. Unmatched live entries are
marked ``candidate_retired`` and promoted to ``retired`` after
:data:`RETIRE_AFTER_N_MISSING` consecutive missed rebuilds. Drift
(``1 - Jaccard(birth_members, current_members)``) is computed every
rebuild; entries above :data:`DRIFT_ALERT_THRESHOLD` are surfaced via
the API for Alice's thinking to consume.

Phase 2 v1 deviations from the spec, all documented in the surface
back to Alice:

- **Storage location** is ``$ALICE_VIEWER_CACHE_DIR/clusters/registry.json``
  rather than ``~/alice-mind/inner/state/cluster-registry-YYYY-MM-DD.json``
  — the viewer's alice-mind mount is read-only by design. Date-versioned
  files defer to Phase 2 v2; the single ``current`` file is sufficient
  for now.
- **Stub filter (in-degree ≥ 15 AND body ≤ 40 lines)** is skipped — the
  spec allows it ("If the cache is unavailable, skip the stub filter and
  fall back to the generic-stem filter alone"). Today's clusters all
  pass the generic-stem filter on their top hub; if a stub-anchored
  cluster appears, it'll get an undesirable name and we add the cache
  then.
- **Drift alerts** ride on the API response under ``drift_alerts``
  rather than firing a surface to ``inner/notes/`` (same RO mount
  reason). Alice polls ``/api/cluster-registry`` during wakes.
- **Splits / merges** are detected implicitly by the greedy matcher
  (highest-Jaccard wins, others get new IDs). Explicit ``lineage``
  population for split children is deferred — adding a list field is
  trivially additive, no spec break.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
from typing import Any, Iterable


# --- Spec constants ---------------------------------------------------

GENERIC_STEMS = {
    "design",
    "research",
    "notes",
    "reference",
    "dailies",
    "people",
    "projects",
    "sources",
    "feedback",
    "findings",
    "decisions",
    "index",
    "readme",
    "memory",
    "archive",
}

JACCARD_MATCH_THRESHOLD = 0.5
DRIFT_ALERT_THRESHOLD = 0.30
RETIRE_AFTER_N_MISSING = 3
MAX_LABEL_LEN = 48  # including "cl-" prefix
SLUG_BUDGET = MAX_LABEL_LEN - len("cl-")  # 45 chars for slug body

# Drift threshold for recomputing the LLM-derived display label. Same
# magnitude as DRIFT_ALERT_THRESHOLD by design — once Alice would be
# alerted that a lobe has drifted, the human-readable name probably
# also needs a fresh take. Computed against ``llm_label_member_signature``
# (members at last LLM compute), not ``birth_members``.
LLM_RELABEL_DRIFT_THRESHOLD = 0.30

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")


# --- Filesystem ------------------------------------------------------


def registry_path() -> pathlib.Path:
    """Resolve the registry file location.

    Honors ``$ALICE_VIEWER_CACHE_DIR``; defaults to the project's
    standard viewer-cache root. Parent dirs are created on save.
    """
    override = os.environ.get("ALICE_VIEWER_CACHE_DIR")
    base = (
        pathlib.Path(override)
        if override
        else pathlib.Path.home() / ".local/state/alice/viewer-cache"
    )
    return base / "clusters" / "registry.json"


def load_registry(path: pathlib.Path | None = None) -> dict[str, Any]:
    """Load the registry from disk. Returns an empty registry shape
    (``{"entries": {}, "schema_version": 1, "last_rebuild": None}``)
    if the file is missing or unreadable — a missing registry is the
    Phase 2 cold-start case, not an error.
    """
    if path is None:
        path = registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_registry()


def save_registry(path: pathlib.Path, registry: dict[str, Any]) -> None:
    """Atomic save: write to a temp file, then rename. Survives crashes
    mid-write without leaving a half-written registry on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True))
    tmp.replace(path)


def _empty_registry() -> dict[str, Any]:
    return {"schema_version": 1, "last_rebuild": None, "entries": {}}


# --- Slug derivation -------------------------------------------------


def _kebab(s: str) -> str:
    out: list[str] = []
    prev_dash = False
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "unnamed"


def _strip_date_prefix(slug: str) -> str:
    stripped = _DATE_PREFIX.sub("", slug)
    # Per the v0.5 stopgap: don't strip if the result is too short
    # to carry meaning. Length floor is the same 4-char threshold as
    # the upstream label derivation.
    return stripped if len(stripped) >= 4 else slug


def _is_generic(slug: str) -> bool:
    return slug in GENERIC_STEMS


def _truncate(slug: str) -> str:
    """Truncate a slug to fit the SLUG_BUDGET, preferring word boundaries."""
    if len(slug) <= SLUG_BUDGET:
        return slug
    cut = slug[:SLUG_BUDGET]
    # Prefer breaking at a hyphen near the end.
    boundary = cut.rfind("-")
    if boundary >= SLUG_BUDGET - 12:  # don't cut too aggressively
        cut = cut[:boundary]
    return cut.rstrip("-") or slug[:SLUG_BUDGET]


def derive_label(
    ranked_hub_ids: list[str],
    label_by_id: dict[str, str],
    *,
    taken: set[str] | None = None,
    fallback_pool: Iterable[str] | None = None,
) -> str:
    """Derive a ``cl-<slug>`` from a cluster's ranked hubs per the spec.

    ``ranked_hub_ids`` are member node ids in descending in-degree order.
    ``taken`` is the set of cluster labels already claimed in this
    rebuild — used for collision resolution per §3.

    Walks the ranked list, skipping generic-stem hubs (per
    :data:`GENERIC_STEMS`). The first survivor's title becomes the
    slug. If all hubs are generic, falls back to concatenating the
    top two hub titles regardless of filter.

    Stub filter (in-degree ≥ 15 AND body ≤ 40 lines) is **deferred** in
    Phase 2 v1 — see module docstring.

    Collision resolution: if the chosen slug is already in ``taken``,
    walk for a runner-up and append it (``cl-<hub1>-<hub2>``). If still
    colliding, append a numeric suffix.
    """
    taken = taken or set()
    survivors: list[str] = []  # kebab slugs of non-generic hubs
    fallback: list[str] = []  # any kebab slugs (used when all generic)
    for nid in ranked_hub_ids:
        title = label_by_id.get(nid, nid)
        slug = _strip_date_prefix(_kebab(title))
        fallback.append(slug)
        if not _is_generic(slug):
            survivors.append(slug)
    if not survivors and fallback_pool:
        for nid in fallback_pool:
            if nid in set(ranked_hub_ids):
                continue
            title = label_by_id.get(nid, nid)
            slug = _strip_date_prefix(_kebab(title))
            fallback.append(slug)
            if not _is_generic(slug):
                survivors.append(slug)

    if survivors:
        primary = survivors[0]
        candidate = f"cl-{_truncate(primary)}"
        if candidate not in taken:
            return candidate
        # §3 collision resolution: append the runner-up. If we don't have
        # one, fall through to numeric suffix.
        if len(survivors) >= 2:
            joined = _truncate(f"{primary}-{survivors[1]}")
            disambiguated = f"cl-{joined}"
            if disambiguated not in taken:
                return disambiguated
        # Numeric suffix as the last-resort tiebreak.
        for i in range(2, 100):
            candidate_n = f"cl-{_truncate(primary)}-{i}"
            if candidate_n not in taken:
                return candidate_n
        return f"cl-{_truncate(primary)}-overflow"

    # All generic — concatenate top two regardless of filter (§2 step 5).
    if len(fallback) >= 2:
        joined = _truncate(f"{fallback[0]}-{fallback[1]}")
        candidate = f"cl-{joined}"
    elif fallback:
        candidate = f"cl-{_truncate(fallback[0])}"
    else:
        candidate = "cl-unnamed"
    if candidate not in taken:
        return candidate
    for i in range(2, 100):
        candidate_n = f"{candidate}-{i}"
        if candidate_n not in taken:
            return candidate_n
    return f"{candidate}-overflow"


# --- Jaccard ---------------------------------------------------------


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# --- Rebuild ---------------------------------------------------------


def rebuild(
    *,
    cluster_members: dict[str, list[str]],
    cluster_top_hubs: dict[str, list[str]],
    label_by_id: dict[str, str],
    prev_registry: dict[str, Any],
    today: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run a Jaccard-matched rebuild against the previous registry.

    Returns ``(new_registry, drift_alerts)``. ``cluster_members`` maps
    fresh community ids (e.g. ``c0``, ``c1``) to member node ids;
    ``cluster_top_hubs`` provides each fresh cluster's top hubs in
    ranked order for slug derivation when a new ID has to be minted.

    The misc bucket and any non-topical input is the caller's
    responsibility to filter — this function treats every fresh cid in
    the input as a real cluster eligible for a stable ID.
    """
    today_iso = today or datetime.date.today().isoformat()
    entries: dict[str, dict[str, Any]] = {}
    # Deep-copy prev_registry's entries so we can mutate freely.
    for cid, entry in (prev_registry.get("entries") or {}).items():
        entries[cid] = json.loads(json.dumps(entry))

    # --- Greedy bipartite matching: fresh_cid <-> stable_id -----------
    fresh_member_sets = {cid: set(members) for cid, members in cluster_members.items()}
    candidates: list[tuple[float, str, str]] = []  # (jaccard, fresh_cid, stable_id)
    for fresh_cid, fresh_set in fresh_member_sets.items():
        for sid, entry in entries.items():
            if entry.get("status") == "retired":
                continue
            prev_set = set(entry.get("last_member_set") or [])
            if not prev_set:
                continue
            j = _jaccard(fresh_set, prev_set)
            if j >= JACCARD_MATCH_THRESHOLD:
                candidates.append((j, fresh_cid, sid))
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))

    fresh_to_stable: dict[str, str] = {}
    matched_stable: set[str] = set()
    for j, fresh_cid, sid in candidates:
        if fresh_cid in fresh_to_stable or sid in matched_stable:
            continue
        fresh_to_stable[fresh_cid] = sid
        matched_stable.add(sid)

    # --- Mint new IDs for unmatched fresh clusters --------------------
    taken_labels: set[str] = set(entries.keys())
    for fresh_cid in fresh_member_sets:
        if fresh_cid in fresh_to_stable:
            continue
        ranked = cluster_top_hubs.get(fresh_cid, [])
        members_for_cid = cluster_members.get(fresh_cid, [])
        new_label = derive_label(
            ranked_hub_ids=ranked,
            label_by_id=label_by_id,
            taken=taken_labels,
            fallback_pool=members_for_cid,
        )
        taken_labels.add(new_label)
        members = list(fresh_member_sets[fresh_cid])
        top_hub_id = ranked[0] if ranked else (members[0] if members else "")
        top_hub_label = label_by_id.get(top_hub_id, top_hub_id)
        entries[new_label] = {
            "id": new_label,
            "status": "live",
            "birth_top_hub": top_hub_label,
            "birth_members": sorted(members),
            "birth_size": len(members),
            "created": today_iso,
            "current_top_hub": top_hub_label,
            "current_members": sorted(members),
            "current_size": len(members),
            "last_rebuild": today_iso,
            "last_member_set": sorted(members),
            "drift": 0.0,
            "lineage": [],
            "absorbed_into": None,
            "retired_at": None,
            "split_children": [],
            "consecutive_misses": 0,
            # LLM-derived display label fields. Populated out-of-band
            # by the caller via :func:`apply_llm_labels` after rebuild
            # because the LLM call is async and we want it parallelised
            # across pending lobes. Cold-start: all three are None and
            # the UI falls back to the mechanical hub name.
            "llm_label": None,
            "llm_label_member_signature": None,
            "llm_label_computed_at": None,
        }
        fresh_to_stable[fresh_cid] = new_label

    # --- Update matched entries ---------------------------------------
    for fresh_cid, stable_id in fresh_to_stable.items():
        entry = entries[stable_id]
        if (
            entry.get("created") == today_iso
            and entry.get("birth_size") == entry.get("current_size")
            and stable_id not in matched_stable
        ):
            # Just-minted entry — already correct.
            continue
        if stable_id not in matched_stable:
            continue
        members = sorted(fresh_member_sets[fresh_cid])
        ranked = cluster_top_hubs.get(fresh_cid, [])
        entry["current_members"] = members
        entry["current_size"] = len(members)
        entry["last_member_set"] = members
        entry["last_rebuild"] = today_iso
        entry["consecutive_misses"] = 0
        if ranked:
            entry["current_top_hub"] = label_by_id.get(ranked[0], ranked[0])
        birth_set = set(entry.get("birth_members") or [])
        entry["drift"] = round(1.0 - _jaccard(birth_set, set(members)), 4)

    # --- Bump misses on unmatched live entries; retire after N --------
    for sid, entry in entries.items():
        if entry.get("status") == "retired":
            continue
        if sid in matched_stable:
            continue
        # Just-minted entries skip the miss check (they were the matched
        # side of zero candidates because they didn't exist yet).
        if entry.get("last_rebuild") == today_iso:
            continue
        misses = int(entry.get("consecutive_misses") or 0) + 1
        entry["consecutive_misses"] = misses
        if misses >= RETIRE_AFTER_N_MISSING:
            entry["status"] = "retired"
            entry["retired_at"] = today_iso

    # --- Drift alerts -------------------------------------------------
    drift_alerts: list[dict[str, Any]] = []
    for sid, entry in entries.items():
        if entry.get("status") != "live":
            continue
        drift = float(entry.get("drift") or 0.0)
        if drift >= DRIFT_ALERT_THRESHOLD:
            drift_alerts.append(
                {
                    "id": sid,
                    "drift": drift,
                    "birth_top_hub": entry.get("birth_top_hub"),
                    "current_top_hub": entry.get("current_top_hub"),
                    "birth_size": entry.get("birth_size"),
                    "current_size": entry.get("current_size"),
                    "created": entry.get("created"),
                }
            )

    new_registry = {
        "schema_version": 1,
        "last_rebuild": today_iso,
        "entries": entries,
    }
    return new_registry, drift_alerts


def fresh_to_stable_label_map(
    fresh_to_stable: dict[str, str],
) -> dict[str, str]:
    """Convenience: identity mapping if registry-disabled, else passthrough."""
    return dict(fresh_to_stable)


# --- LLM-derived display label management ----------------------------


def pending_llm_label_ids(
    registry: dict[str, Any],
    *,
    threshold: float = LLM_RELABEL_DRIFT_THRESHOLD,
) -> list[str]:
    """Return the stable IDs whose LLM label needs computing or refreshing.

    A live entry is "pending" if any of:

    - It has no ``llm_label`` yet (cold-start, never labeled).
    - Its current member set has drifted from
      ``llm_label_member_signature`` by ``>= threshold`` (Jaccard).
      Same magnitude as ``DRIFT_ALERT_THRESHOLD`` by default — once
      Alice would be alerted to drift, the display label is also
      probably stale.

    Retired entries and the misc bucket (which never enters the
    registry) are skipped. Returned IDs are sorted alphabetically for
    deterministic ordering across calls — the caller typically schedules
    LLM work in this order.
    """
    pending: list[str] = []
    for sid, entry in (registry.get("entries") or {}).items():
        if entry.get("status") != "live":
            continue
        if not entry.get("llm_label"):
            pending.append(sid)
            continue
        sig = set(entry.get("llm_label_member_signature") or [])
        current = set(entry.get("current_members") or [])
        if not sig and current:
            pending.append(sid)
            continue
        drift = 1.0 - _jaccard(sig, current)
        if drift >= threshold:
            pending.append(sid)
    pending.sort()
    return pending


def apply_llm_labels(
    registry: dict[str, Any],
    labels_by_id: dict[str, str],
    *,
    today: str | None = None,
) -> bool:
    """Write computed LLM labels into the registry in place.

    ``labels_by_id`` maps stable cluster id → newly-computed label.
    Empty strings are *skipped* (treated as "LLM call failed, leave
    cached value alone") rather than written, so a transient endpoint
    outage doesn't wipe good cached labels.

    Returns ``True`` if the registry was mutated (at least one label
    written), ``False`` otherwise — caller uses this to decide whether
    to ``save_registry``.
    """
    today_iso = today or datetime.date.today().isoformat()
    entries = registry.get("entries") or {}
    mutated = False
    for sid, label in labels_by_id.items():
        entry = entries.get(sid)
        if entry is None or entry.get("status") != "live":
            continue
        if not label:
            continue
        members_now = sorted(entry.get("current_members") or [])
        prev_label = entry.get("llm_label")
        prev_sig = entry.get("llm_label_member_signature")
        if prev_label == label and prev_sig == members_now:
            continue
        entry["llm_label"] = label
        entry["llm_label_member_signature"] = members_now
        entry["llm_label_computed_at"] = today_iso
        mutated = True
    return mutated
