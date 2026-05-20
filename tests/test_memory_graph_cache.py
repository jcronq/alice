"""Tests for the incremental memo behind read_memory_graph / load_memory_graph_bundle.

Covers the four cache paths:

- Cold load builds the full graph.
- Pure hit (no FS change) reuses everything by identity.
- Near-miss (non-topical file changed) reuses cluster_metrics.
- Topical miss recomputes clusters.

Plus the edge cases that make incremental updates load-bearing: ghosts
re-resolving when a new note adds itself to the label index, removed
files dropping nodes + outgoing edges, and dailies being excluded from
the signature.
"""

from __future__ import annotations

import pathlib

import pytest

from viewer import sources


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the module-level cache between tests so each starts cold."""
    sources._memory_graph_cache = None
    yield
    sources._memory_graph_cache = None


def _write(mind: pathlib.Path, rel: str, body: str = "") -> pathlib.Path:
    path = mind / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_cold_load_builds_full_graph(tmp_path: pathlib.Path) -> None:
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "Links to [[bob]].")
    _write(mind, "cortex-memory/people/bob.md", "Talks back to [[alice]].")

    nodes, edges, cm = sources.load_memory_graph_bundle(mind)

    node_ids = {n.id for n in nodes}
    assert "cortex-memory/people/alice" in node_ids
    assert "cortex-memory/people/bob" in node_ids
    edge_pairs = {(e.source, e.target) for e in edges}
    assert ("cortex-memory/people/alice", "cortex-memory/people/bob") in edge_pairs
    assert ("cortex-memory/people/bob", "cortex-memory/people/alice") in edge_pairs
    assert "clusters" in cm


def test_pure_hit_returns_same_objects(tmp_path: pathlib.Path) -> None:
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "[[bob]]")
    _write(mind, "cortex-memory/people/bob.md", "[[alice]]")

    n1, e1, cm1 = sources.load_memory_graph_bundle(mind)
    n2, e2, cm2 = sources.load_memory_graph_bundle(mind)

    # Same identity — nothing was rebuilt.
    assert n1 is n2
    assert e1 is e2
    assert cm1 is cm2


def test_non_topical_change_reuses_cluster_metrics(tmp_path: pathlib.Path) -> None:
    """index.md at the cortex-memory root is non-topical. Touching it
    must trigger a re-read but leave cluster_metrics identity-stable."""
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "[[bob]]")
    _write(mind, "cortex-memory/people/bob.md", "[[alice]]")
    index = _write(mind, "cortex-memory/index.md", "v1")

    _, _, cm1 = sources.load_memory_graph_bundle(mind)
    # Mutate body + bump mtime so the signature actually shifts.
    index.write_text("v2 with extra content")
    nodes2, _, cm2 = sources.load_memory_graph_bundle(mind)

    assert cm1 is cm2, "cluster_metrics should be reused across non-topical edits"
    # And the rebuild still sees the new content (mtime/size on the node updated).
    index_node = next(n for n in nodes2 if n.id == "cortex-memory/index")
    assert index_node.size == len("v2 with extra content")


def test_topical_change_recomputes_cluster_metrics(tmp_path: pathlib.Path) -> None:
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "[[bob]]")
    bob = _write(mind, "cortex-memory/people/bob.md", "[[alice]]")

    _, _, cm1 = sources.load_memory_graph_bundle(mind)
    bob.write_text("[[alice]] and [[charlie]]")  # add a wikilink → ghost edge
    _, edges2, cm2 = sources.load_memory_graph_bundle(mind)

    assert cm1 is not cm2, "cluster_metrics should be rebuilt after a topical edit"
    # And the ghost edge from bob → charlie shows up.
    edge_pairs = {(e.source, e.target) for e in edges2}
    assert ("cortex-memory/people/bob", "unresolved::charlie") in edge_pairs


def test_adding_note_resolves_existing_ghost(tmp_path: pathlib.Path) -> None:
    """If alice.md links to [[charlie]] before charlie exists, the edge
    points to an ``unresolved::charlie`` ghost. Once charlie.md is
    written, the same edge must resolve to the real node — proving the
    incremental builder re-resolves on every miss."""
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "Mentions [[charlie]].")

    _, edges1, _ = sources.load_memory_graph_bundle(mind)
    assert any(e.target == "unresolved::charlie" for e in edges1)

    _write(mind, "cortex-memory/people/charlie.md", "Hi.")
    nodes2, edges2, _ = sources.load_memory_graph_bundle(mind)
    edge_targets = {(e.source, e.target) for e in edges2}
    assert (
        "cortex-memory/people/alice",
        "cortex-memory/people/charlie",
    ) in edge_targets
    # Ghost should no longer appear.
    assert not any(e.target == "unresolved::charlie" for e in edges2)
    assert not any(n.id == "unresolved::charlie" for n in nodes2)


def test_removed_note_drops_node_and_outgoing_edges(tmp_path: pathlib.Path) -> None:
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "[[bob]]")
    bob = _write(mind, "cortex-memory/people/bob.md", "[[alice]]")

    sources.load_memory_graph_bundle(mind)
    bob.unlink()
    nodes2, edges2, _ = sources.load_memory_graph_bundle(mind)

    assert not any(n.id == "cortex-memory/people/bob" for n in nodes2)
    # bob's outgoing edge gone.
    assert not any(e.source == "cortex-memory/people/bob" for e in edges2)
    # alice's edge to bob now resolves to a ghost (alice still has the [[bob]] wikilink).
    assert any(e.target == "unresolved::bob" for e in edges2)


def test_dailies_excluded_from_signature(tmp_path: pathlib.Path) -> None:
    """Writes to cortex-memory/dailies/ must not invalidate the cache —
    otherwise every wake's access_count bump would force a recompute."""
    mind = tmp_path / "mind"
    _write(mind, "cortex-memory/people/alice.md", "[[bob]]")
    _write(mind, "cortex-memory/people/bob.md", "[[alice]]")
    daily = _write(mind, "cortex-memory/dailies/2026-05-12.md", "v1")

    n1, e1, cm1 = sources.load_memory_graph_bundle(mind)
    daily.write_text("v2 — much later content")
    n2, e2, cm2 = sources.load_memory_graph_bundle(mind)

    # Pure identity hit: dailies don't shift the signature.
    assert n1 is n2
    assert e1 is e2
    assert cm1 is cm2
