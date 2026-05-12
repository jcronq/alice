"""Tests for the viewer cluster of fixes:

1. ``group_turns`` recognises ``viewer_chat_turn_start`` and
   ``a2a_turn_start`` and tags the resulting Turn with the matching
   kind ("viewer-chat" / "a2a") instead of falling through to "unknown".
2. ``read_current_objective`` reads ``inner/state/active-thread.md``
   when present, falls back to the ``Current focus`` paragraph of
   ``inner/directive.md`` when no thread is set, returns ``None``
   only when neither source has content.
"""

from __future__ import annotations

import pathlib

import pytest

from alice_viewer import aggregators, sources
from alice_viewer.sources import UnifiedEvent


# ---------------------------------------------------------------------------
# UNKNOWN turn fix


def _speaking_event(kind: str, ts: float, turn_id: str, **detail) -> UnifiedEvent:
    return UnifiedEvent(
        ts=ts,
        hemisphere="speaking",
        kind=kind,
        correlation_id=turn_id,
        summary="",
        detail=detail,
    )


def test_viewer_chat_turn_classified():
    events = [
        _speaking_event(
            "viewer_chat_turn_start",
            ts=100,
            turn_id="t-vc1",
            display_name="Jason",
            inbound="hello from web",
        ),
        _speaking_event(
            "viewer_chat_turn_end",
            ts=105,
            turn_id="t-vc1",
            error=None,
            duration_ms=5000,
        ),
    ]
    turns = aggregators.group_turns(events)
    assert len(turns) == 1
    t = turns[0]
    assert t.kind == "viewer-chat"
    assert t.sender_name == "Jason"
    assert t.inbound == "hello from web"
    assert t.duration_ms == 5000


def test_a2a_turn_classified():
    events = [
        _speaking_event(
            "a2a_turn_start",
            ts=200,
            turn_id="t-a2a1",
            display_name="agent-foo",
            inbound="task body",
        ),
        _speaking_event(
            "a2a_turn_end",
            ts=210,
            turn_id="t-a2a1",
            error=None,
            duration_ms=10000,
        ),
    ]
    turns = aggregators.group_turns(events)
    assert len(turns) == 1
    assert turns[0].kind == "a2a"
    assert turns[0].sender_name == "agent-foo"


def test_unknown_kind_still_falls_through():
    """If a future transport ships a kind the aggregator hasn't been
    taught, we still want a Turn — just labelled 'unknown' — rather
    than silently dropping the events. Guards against regression on
    the current safety net."""
    events = [
        _speaking_event(
            "future_transport_turn_start",
            ts=300,
            turn_id="t-future",
        ),
    ]
    turns = aggregators.group_turns(events)
    assert len(turns) == 1
    assert turns[0].kind == "unknown"


# ---------------------------------------------------------------------------
# read_current_objective


def test_current_objective_prefers_active_thread(tmp_path):
    inner = tmp_path / "inner"
    (inner / "state").mkdir(parents=True)
    (inner / "state" / "active-thread.md").write_text(
        "---\n"
        "topic: Graph sparsification — PyG retraining\n"
        "last_action: design written\n"
        "next_step: install PyG, run script\n"
        "created: 2026-05-11T20:35:00-04:00\n"
        "---\n"
    )
    # Directive present but should be ignored when thread is set.
    (inner / "directive.md").write_text(
        "# Directive\n\n## Current focus\n\nTend the mind.\n"
    )
    obj = sources.read_current_objective(inner)
    assert obj is not None
    assert obj["source"] == "active-thread"
    assert obj["topic"] == "Graph sparsification — PyG retraining"
    assert obj["detail"] == "install PyG, run script"


def test_current_objective_falls_back_to_directive(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "directive.md").write_text(
        "# Directive\n"
        "\n"
        "preamble paragraph\n"
        "\n"
        "## Current focus\n"
        "\n"
        "Tend the mind. Groom memory. Keep the knowledge graph healthy.\n"
        "\n"
        "## Open lines\n"
    )
    obj = sources.read_current_objective(inner)
    assert obj is not None
    assert obj["source"] == "directive"
    assert obj["topic"] == "current focus"
    assert "Groom memory" in obj["detail"]


def test_current_objective_active_thread_without_topic_falls_back(tmp_path):
    inner = tmp_path / "inner"
    (inner / "state").mkdir(parents=True)
    # active-thread.md exists but has no `topic` field — treat as if absent.
    (inner / "state" / "active-thread.md").write_text(
        "---\nlast_action: something\n---\n"
    )
    (inner / "directive.md").write_text(
        "## Current focus\n\nfallback content\n"
    )
    obj = sources.read_current_objective(inner)
    assert obj is not None
    assert obj["source"] == "directive"


def test_current_objective_returns_none_when_both_missing(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    obj = sources.read_current_objective(inner)
    assert obj is None


def test_current_objective_uses_last_action_when_no_next_step(tmp_path):
    inner = tmp_path / "inner"
    (inner / "state").mkdir(parents=True)
    (inner / "state" / "active-thread.md").write_text(
        "---\ntopic: My topic\nlast_action: did the thing\n---\n"
    )
    obj = sources.read_current_objective(inner)
    assert obj is not None
    assert obj["topic"] == "My topic"
    assert obj["detail"] == "did the thing"
