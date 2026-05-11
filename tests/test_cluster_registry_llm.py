"""Tests for the LLM-label hooks added to ``alice_viewer.cluster_registry``.

Covers three behaviours:

1. ``rebuild()`` seeds new entries with empty LLM-label fields.
2. ``pending_llm_label_ids()`` correctly identifies entries needing
   compute (cold-start, drift past threshold, retired skipped).
3. ``apply_llm_labels()`` writes labels + signature, skips empty
   strings, returns a sane mutated flag.
"""

from __future__ import annotations

from alice_viewer import cluster_registry


def _empty_prev() -> dict:
    return {"schema_version": 1, "last_rebuild": None, "entries": {}}


def _label_by_id(node_ids: list[str]) -> dict[str, str]:
    # Use the node id itself as its display title.
    return {nid: nid for nid in node_ids}


# ---------------------------------------------------------------------------
# rebuild() seeds the new fields
# ---------------------------------------------------------------------------


def test_rebuild_seeds_empty_llm_label_fields_at_mint() -> None:
    members = ["alpha", "beta", "gamma"]
    new_reg, _alerts = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    entries = new_reg["entries"]
    assert len(entries) == 1
    entry = next(iter(entries.values()))
    assert entry["llm_label"] is None
    assert entry["llm_label_member_signature"] is None
    assert entry["llm_label_computed_at"] is None


# ---------------------------------------------------------------------------
# pending_llm_label_ids
# ---------------------------------------------------------------------------


def test_pending_includes_entries_with_no_llm_label() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    pending = cluster_registry.pending_llm_label_ids(reg)
    # The single newly-minted entry has no llm_label yet.
    assert len(pending) == 1
    assert pending[0] in reg["entries"]


def test_pending_excludes_entries_with_fresh_llm_label() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    cluster_registry.apply_llm_labels(
        reg, {sid: "alpha-bridge-cluster"}, today="2026-05-11"
    )
    # Same membership → no drift → not pending.
    assert cluster_registry.pending_llm_label_ids(reg) == []


def test_pending_includes_entries_with_drift_above_threshold() -> None:
    # Five-member cluster, swap one member: Jaccard(t1, t2) = 4/6 ≈ 0.67,
    # which is above the 0.5 *match* threshold so the entry is matched
    # (not newly minted), and the drift = 0.33 which is just over the
    # 0.30 LLM-relabel threshold so pending should pick it up.
    members_t1 = ["a", "b", "c", "d", "e"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members_t1},
        cluster_top_hubs={"c0": members_t1},
        label_by_id=_label_by_id(members_t1),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    cluster_registry.apply_llm_labels(
        reg, {sid: "early-label"}, today="2026-05-11"
    )
    assert cluster_registry.pending_llm_label_ids(reg) == []

    members_t2 = ["a", "b", "c", "d", "x"]  # swap "e" → "x"
    reg2, _ = cluster_registry.rebuild(
        cluster_members={"c0": members_t2},
        cluster_top_hubs={"c0": members_t2},
        label_by_id=_label_by_id(members_t1 + members_t2),
        prev_registry=reg,
        today="2026-05-12",
    )
    pending = cluster_registry.pending_llm_label_ids(reg2)
    assert sid in pending


def test_pending_skips_entries_below_drift_threshold() -> None:
    # Five-member cluster: swap one → Jaccard ≈ 4/6 = 0.67 → drift = 0.33.
    # That's just over the 0.30 default. Add one instead → Jaccard = 5/6 = 0.83
    # → drift = 0.17, well under threshold.
    members_t1 = ["a", "b", "c", "d", "e"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members_t1},
        cluster_top_hubs={"c0": members_t1},
        label_by_id=_label_by_id(members_t1),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    cluster_registry.apply_llm_labels(
        reg, {sid: "settled-label"}, today="2026-05-11"
    )

    members_t2 = members_t1 + ["f"]  # additive only → low drift
    reg2, _ = cluster_registry.rebuild(
        cluster_members={"c0": members_t2},
        cluster_top_hubs={"c0": members_t2},
        label_by_id=_label_by_id(members_t2),
        prev_registry=reg,
        today="2026-05-12",
    )
    assert cluster_registry.pending_llm_label_ids(reg2) == []


def test_pending_skips_retired_entries() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    # Manually flip status to retired — pending should ignore it
    # regardless of the missing llm_label.
    reg["entries"][sid]["status"] = "retired"
    assert cluster_registry.pending_llm_label_ids(reg) == []


# ---------------------------------------------------------------------------
# apply_llm_labels
# ---------------------------------------------------------------------------


def test_apply_llm_labels_writes_label_and_signature() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    mutated = cluster_registry.apply_llm_labels(
        reg, {sid: "minted-label"}, today="2026-05-11"
    )
    assert mutated is True
    entry = reg["entries"][sid]
    assert entry["llm_label"] == "minted-label"
    assert entry["llm_label_member_signature"] == sorted(members)
    assert entry["llm_label_computed_at"] == "2026-05-11"


def test_apply_llm_labels_skips_empty_strings_to_preserve_cache() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    cluster_registry.apply_llm_labels(
        reg, {sid: "good-label"}, today="2026-05-11"
    )
    # Now an LLM call returned empty (transient outage). Apply should
    # preserve the cached "good-label" rather than wiping it.
    mutated = cluster_registry.apply_llm_labels(
        reg, {sid: ""}, today="2026-05-12"
    )
    assert mutated is False
    assert reg["entries"][sid]["llm_label"] == "good-label"


def test_apply_llm_labels_returns_false_when_unchanged() -> None:
    members = ["a", "b", "c"]
    reg, _ = cluster_registry.rebuild(
        cluster_members={"c0": members},
        cluster_top_hubs={"c0": members},
        label_by_id=_label_by_id(members),
        prev_registry=_empty_prev(),
        today="2026-05-11",
    )
    sid = next(iter(reg["entries"]))
    cluster_registry.apply_llm_labels(
        reg, {sid: "stable-label"}, today="2026-05-11"
    )
    # Re-applying the identical label with identical membership is a no-op.
    mutated = cluster_registry.apply_llm_labels(
        reg, {sid: "stable-label"}, today="2026-05-11"
    )
    assert mutated is False


def test_apply_llm_labels_ignores_unknown_ids() -> None:
    reg = _empty_prev()
    mutated = cluster_registry.apply_llm_labels(
        reg, {"cl-not-a-real-id": "ghost"}, today="2026-05-11"
    )
    assert mutated is False
    assert reg["entries"] == {}
