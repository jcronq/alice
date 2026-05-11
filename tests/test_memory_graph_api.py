"""End-to-end tests for ``/api/memory-graph`` after the LLM-label wiring.

The new code path (registry rebuild + LLM refresh + cluster decoration)
needs to be exercised through the FastAPI app to catch import errors,
async/sync mistakes, and missing keys in the response shape.

The Qwen endpoint is **never** touched here — every test runs with
``ALICE_LOBE_LLM_LABELS`` unset/disabled. The label-computation seam
itself is covered by ``test_lobe_labeler.py``.
"""

from __future__ import annotations

import pathlib

import pytest

# httpx is needed by FastAPI's TestClient; skip cleanly if absent.
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from alice_viewer.main import create_app
from alice_viewer.settings import Paths


def _make_paths(tmp_path: pathlib.Path) -> Paths:
    mind = tmp_path / "mind"
    (mind / "inner" / "state").mkdir(parents=True)
    (mind / "memory").mkdir()
    (mind / "memory" / "events.jsonl").write_text("")
    (mind / "inner" / "directive.md").write_text("")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return Paths(
        mind_dir=mind,
        state_dir=state_dir,
        thinking_log=mind / "memory" / "events.jsonl",
        speaking_log=mind / "memory" / "events.jsonl",
        turn_log=mind / "inner" / "state" / "speaking-turns.jsonl",
    )


def _seed_lobe(mind: pathlib.Path, folder: str, basename: str, body: str) -> None:
    """Write a topical note under cortex-memory/<folder>/<basename>.md."""
    target = mind / "cortex-memory" / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{basename}.md").write_text(body)


def test_memory_graph_route_returns_cluster_decoration_with_llm_off(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the LLM flag off, the route must still rebuild the registry
    and decorate each cluster with ``cluster_slug`` (from the stable ID
    minted at rebuild) and ``llm_label`` (which is None until labeled).
    """
    monkeypatch.delenv("ALICE_LOBE_LLM_LABELS", raising=False)
    # Registry persists to ALICE_VIEWER_CACHE_DIR — point it at tmp so
    # tests don't pollute the user's real cache dir.
    monkeypatch.setenv("ALICE_VIEWER_CACHE_DIR", str(tmp_path / "vcache"))

    paths = _make_paths(tmp_path)
    # Build a small cortex-memory subgraph with two cross-linked notes
    # so compute_cluster_metrics has something to find.
    _seed_lobe(paths.mind_dir, "research", "alpha", "See [[beta]] and [[gamma]].")
    _seed_lobe(paths.mind_dir, "research", "beta", "Refers to [[alpha]] often.")
    _seed_lobe(paths.mind_dir, "research", "gamma", "Bridge to [[alpha]].")

    app = create_app(paths=paths)
    client = TestClient(app)
    resp = client.get("/api/memory-graph")
    assert resp.status_code == 200
    payload = resp.json()
    assert "cluster_metrics" in payload
    cm = payload["cluster_metrics"]
    assert "clusters" in cm
    # Every cluster should now carry the two new keys, even when the
    # LLM flag is off — llm_label simply stays None.
    for c in cm["clusters"]:
        assert "cluster_slug" in c
        assert "llm_label" in c
        assert c["llm_label"] is None  # LLM disabled


def test_memory_graph_route_runs_llm_when_flag_on(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the LLM flag on, ``compute_label_async`` is invoked for
    each pending lobe and the result lands on the cluster payload.

    We monkeypatch ``compute_label_async`` itself so no LAN call is
    issued — the seam is exactly what tests are meant to cover.
    """
    monkeypatch.setenv("ALICE_LOBE_LLM_LABELS", "1")
    monkeypatch.setenv("ALICE_VIEWER_CACHE_DIR", str(tmp_path / "vcache"))

    from alice_viewer import lobe_labeler

    seen_member_count: list[int] = []

    async def stub_compute(members, *, llm_call=None):
        seen_member_count.append(len(members))
        return "stub-lobe-label"

    monkeypatch.setattr(lobe_labeler, "compute_label_async", stub_compute)

    paths = _make_paths(tmp_path)
    _seed_lobe(paths.mind_dir, "research", "alpha", "Body of alpha. [[beta]]")
    _seed_lobe(paths.mind_dir, "research", "beta", "Body of beta. [[alpha]] [[gamma]]")
    _seed_lobe(paths.mind_dir, "research", "gamma", "Body of gamma. [[alpha]]")

    app = create_app(paths=paths)
    client = TestClient(app)
    resp = client.get("/api/memory-graph")
    assert resp.status_code == 200
    payload = resp.json()
    cm = payload["cluster_metrics"]

    # The stub was called for at least one lobe.
    assert seen_member_count, "expected compute_label_async to be invoked"
    # Each call must have received the per-member dicts (label + snippet).
    for n in seen_member_count:
        assert n >= 1

    # And the cluster carries the stub's label.
    topical = [c for c in cm["clusters"] if not c["is_misc"]]
    assert topical, "expected at least one topical cluster"
    assert any(c.get("llm_label") == "stub-lobe-label" for c in topical)
