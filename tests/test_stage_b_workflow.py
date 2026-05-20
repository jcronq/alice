"""Tests for ``alice_thinking.workflows.stage_b`` (google-adk port).

Mirrors the 28-case contract from the prior native port (PR #19 — the
ADK rewrite supersedes that branch). Mocks the LLM at the
:class:`ModelCall` seam — no real model calls; no live Qwen; no live
cloud.

Pin: per-step routing + apply, deterministic scoring, Diff
application, parallel side-checks + per-branch timeouts, shadow-mode
dry-run, full integration via the ADK SequentialAgent runner.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
from typing import Any

import pytest

from core.events import CapturingEmitter

from alice_thinking.workflows.stage_b import (
    AppendToDaily,
    CreateConflictNote,
    DEFAULT_STEP_TIMEOUTS,
    Diff,
    Discard,
    FrontmatterChange,
    PromoteToVault,
    RouteToSurface,
    SectionEdit,
    StageBRunnerConfig,
    SurfacePayload,
    WakeState,
    run_stage_b_shadow,
    run_stage_b_wake,
)
from alice_thinking.workflows.stage_b import scoring as scoring_mod
from alice_thinking.workflows.stage_b import steps as steps_mod


# ---------------------------------------------------------------------------
# Helpers — mind tree + scripted ModelCall
# ---------------------------------------------------------------------------


def _build_mind(root: pathlib.Path) -> dict[str, pathlib.Path]:
    mind = root / "mind"
    state = root / "state"
    (mind / "inner" / "notes").mkdir(parents=True)
    (mind / "inner" / "state").mkdir(parents=True)
    (mind / "inner" / "surface").mkdir(parents=True)
    (mind / "inner" / "thoughts").mkdir(parents=True)
    (mind / "cortex-memory" / "research").mkdir(parents=True)
    (mind / "cortex-memory" / "people").mkdir(parents=True)
    (mind / "cortex-memory" / "dailies").mkdir(parents=True)
    (mind / "cortex-memory" / "conflicts").mkdir(parents=True)
    (mind / "memory").mkdir(parents=True)
    state.mkdir(parents=True)
    return {"mind": mind, "state": state}


class ScriptedModel:
    """Deterministic ModelCall stand-in. Matches against the prompt
    fragment header (``## <name>``). Lists rotate. Callables are
    invoked with ``(system, user)``.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        for key, val in self._responses.items():
            if f"## {key}" in system_prompt:
                return self._resolve(key, val)
        raise AssertionError(
            f"ScriptedModel: no response for system_prompt starting with "
            f"{system_prompt[:80]!r}"
        )

    def _resolve(self, key: str, val: Any) -> str:
        if isinstance(val, list):
            if not val:
                raise AssertionError(f"ScriptedModel: list for {key} exhausted")
            return val.pop(0)
        if callable(val):
            return val()
        return val


def _action_response(action: str, **fields: Any) -> str:
    return json.dumps({"action": action, **fields})


def _diff_response(**fields: Any) -> str:
    payload = {
        "frontmatter_changes": [],
        "wikilink_fixes": [],
        "section_edits": [],
        "rationale": "",
        **fields,
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Step 1 — read_wake_state
# ---------------------------------------------------------------------------


def test_read_wake_state_parses_typed_state(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    state_dir = paths["state"]
    (mind / "inner" / "notes" / "001-foo.md").write_text("hello", encoding="utf-8")
    (mind / "inner" / "notes" / "002-bar.md").write_text("world", encoding="utf-8")
    (mind / "inner" / "notes" / ".hidden.md").write_text("ignore", encoding="utf-8")
    (mind / "inner" / "notes" / ".consumed").mkdir()
    (mind / "inner" / "state" / "active-thread.md").write_text(
        "active thread\n", encoding="utf-8"
    )
    events = mind / "memory" / "events.jsonl"
    events.write_text(
        json.dumps({"ts": 1.0, "event": "vault_health", "score": 0.8}) + "\n",
        encoding="utf-8",
    )
    wake_file = mind / "wake.md"
    wake_file.write_text(
        '---\nmode: sleep_b\ntime: "2026-05-08T03:00:00"\n---\n\nbody\n',
        encoding="utf-8",
    )

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=state_dir,
        wake_file_path=wake_file,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )

    assert state.mode == "sleep_b"
    assert len(state.inbox_files) == 2
    assert {p.name for p in state.inbox_files} == {"001-foo.md", "002-bar.md"}
    assert state.active_thread is not None and "active thread" in state.active_thread
    assert state.vault_health is not None
    assert state.vault_health["score"] == 0.8


# ---------------------------------------------------------------------------
# Step 2 — drain_inbox (action routing + consumption + per-note errors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected_type",
    [
        (
            _action_response(
                "promote_to_vault",
                target_path="cortex-memory/people/jason.md",
                new_content="# Jason\n",
                reason="durable fact",
            ),
            PromoteToVault,
        ),
        (_action_response("append_to_daily", line="ate breakfast"), AppendToDaily),
        (
            _action_response(
                "create_conflict_note", slug="weight-mismatch", body="conflict body"
            ),
            CreateConflictNote,
        ),
        (
            _action_response(
                "route_to_surface",
                surface_payload={
                    "surface_type": "stage-b-routed",
                    "body": "do the thing",
                },
            ),
            RouteToSurface,
        ),
        (_action_response("discard", reason="noise"), Discard),
    ],
)
def test_drain_inbox_routes_each_action_type(
    tmp_path: pathlib.Path, action: str, expected_type: type
) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001-test.md"
    note.write_text("note body", encoding="utf-8")

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )

    model = ScriptedModel({"classify_and_route_note": action})
    result = asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    assert len(result.actions) == 1
    assert isinstance(result.actions[0], expected_type)


def test_drain_inbox_consumes_processed_notes(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001-foo.md"
    note.write_text("body", encoding="utf-8")
    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )
    model = ScriptedModel(
        {"classify_and_route_note": _action_response("discard", reason="noise")}
    )
    result = asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    assert not note.exists()
    consumed_dir = mind / "inner" / "notes" / ".consumed" / "2026-05-08"
    assert consumed_dir.is_dir()
    assert (consumed_dir / "001-foo.md").is_file()
    assert len(result.consumed_paths) == 1


def test_drain_inbox_continues_on_per_note_error(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    (mind / "inner" / "notes" / "001-bad.md").write_text("body1", encoding="utf-8")
    (mind / "inner" / "notes" / "002-good.md").write_text("body2", encoding="utf-8")

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )

    responses = ["not valid json", _action_response("discard", reason="noise")]
    model = ScriptedModel({"classify_and_route_note": responses})
    result = asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    assert len(result.actions) == 1
    assert len(result.per_note_errors) == 1
    assert "001-bad.md" in result.per_note_errors[0]
    assert (mind / "inner" / "notes" / "001-bad.md").is_file()
    assert not (mind / "inner" / "notes" / "002-good.md").is_file()


def test_drain_inbox_promote_to_vault_writes_file(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001-test.md"
    note.write_text("note body", encoding="utf-8")

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )
    model = ScriptedModel(
        {
            "classify_and_route_note": _action_response(
                "promote_to_vault",
                target_path="cortex-memory/research/2026-05-08-test.md",
                new_content="# Test\n\nbody\n",
            )
        }
    )
    asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    target = mind / "cortex-memory" / "research" / "2026-05-08-test.md"
    assert target.is_file()
    assert "# Test" in target.read_text()


def test_drain_inbox_append_to_daily(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001-event.md"
    note.write_text("event note", encoding="utf-8")

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )
    model = ScriptedModel(
        {
            "classify_and_route_note": _action_response(
                "append_to_daily", line="finished workout"
            )
        }
    )
    asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    daily = mind / "cortex-memory" / "dailies" / "2026-05-08.md"
    assert daily.is_file()
    assert "- finished workout" in daily.read_text()


def test_drain_inbox_create_conflict_note(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001-conflict.md"
    note.write_text("body", encoding="utf-8")

    state = steps_mod.read_wake_state(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )
    model = ScriptedModel(
        {
            "classify_and_route_note": _action_response(
                "create_conflict_note",
                slug="bench-weight",
                body="bench is 100 vs 105",
            )
        }
    )
    asyncio.run(steps_mod.drain_inbox(state, model_call=model))

    conflicts = list((mind / "cortex-memory" / "conflicts").iterdir())
    assert len(conflicts) == 1
    assert conflicts[0].name.startswith("2026-05-08-bench-weight")


# ---------------------------------------------------------------------------
# Step 3 — pick_grooming_target
# ---------------------------------------------------------------------------


def test_pick_grooming_target_deterministic_scoring(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    research = mind / "cortex-memory" / "research"

    (research / "stale-low.md").write_text(
        "---\nupdated: 2025-01-01\naccess_count: 0\n---\n\nbody\n", encoding="utf-8"
    )
    (research / "fresh-high.md").write_text(
        "---\nupdated: 2026-05-07\naccess_count: 5\n---\n\nbody\n", encoding="utf-8"
    )
    (research / "stale-only.md").write_text(
        "---\nupdated: 2025-01-01\naccess_count: 5\n---\n\nbody\n", encoding="utf-8"
    )

    candidates = scoring_mod.score_candidates(
        vault_dir=mind / "cortex-memory",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )
    paths_in_order = [c.path.name for c in candidates]
    assert paths_in_order[0] == "stale-low.md"
    assert "fresh-high.md" not in paths_in_order

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )
    target = steps_mod.pick_grooming_target(state)
    assert target is not None
    assert target.name == "stale-low.md"

    (research / "another-stale-low.md").write_text(
        "---\nupdated: 2025-01-01\naccess_count: 0\n---\n\nbody\n", encoding="utf-8"
    )
    target2 = steps_mod.pick_grooming_target(state)
    assert target2 is not None
    assert target2.name == "another-stale-low.md"


def test_pick_grooming_target_handles_empty_vault(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    state = WakeState(
        mind_dir=paths["mind"],
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )
    assert steps_mod.pick_grooming_target(state) is None


# ---------------------------------------------------------------------------
# Step 4 — groom_target + apply_diff
# ---------------------------------------------------------------------------


def test_groom_target_applies_diff(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    target = mind / "cortex-memory" / "research" / "groom-me.md"
    target.write_text(
        "---\nupdated: 2025-01-01\naccess_count: 0\ntitle: old\n---\n\n"
        "## Body\n\nold content with [[bad-link]] and [[other-link]].\n\n"
        "## Aside\n\nstale aside.\n",
        encoding="utf-8",
    )

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )

    model = ScriptedModel(
        {
            "produce_grooming_diff": _diff_response(
                frontmatter_changes=[
                    {"key": "updated", "new_value": "2026-05-08"},
                    {"key": "title", "new_value": "new"},
                ],
                wikilink_fixes=[{"old_target": "bad-link", "new_target": "good-link"}],
                section_edits=[
                    {"heading": "Aside", "new_body": "fresh aside text."}
                ],
                rationale="freshen",
            )
        }
    )
    diff = asyncio.run(steps_mod.groom_target(state, target, model_call=model))
    assert diff is not None
    after = target.read_text()
    assert "updated: 2026-05-08" in after
    assert "title: new" in after
    assert "[[good-link]]" in after
    assert "[[bad-link]]" not in after
    assert "[[other-link]]" in after
    assert "fresh aside text" in after
    assert "stale aside" not in after


def test_groom_target_returns_none_on_no_changes(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    target = mind / "cortex-memory" / "research" / "ok.md"
    original = "---\nupdated: 2026-05-08\n---\n\nbody\n"
    target.write_text(original, encoding="utf-8")

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )
    model = ScriptedModel({"produce_grooming_diff": _diff_response()})
    diff = asyncio.run(steps_mod.groom_target(state, target, model_call=model))
    assert diff is None
    assert target.read_text() == original


def test_apply_diff_section_edit_replaces_named_heading(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "f.md"
    target.write_text(
        "---\na: 1\n---\n\n## A\n\naaa\n\n## B\n\nbbb\n", encoding="utf-8"
    )
    diff = Diff(section_edits=[SectionEdit(heading="A", new_body="new aaa")])
    assert steps_mod.apply_diff(target, diff) is True
    after = target.read_text()
    assert "new aaa" in after
    assert "bbb" in after


def test_apply_diff_removes_frontmatter_key_when_value_none(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "f.md"
    target.write_text(
        "---\nkeep: me\nremove: this\n---\n\nbody\n", encoding="utf-8"
    )
    diff = Diff(
        frontmatter_changes=[FrontmatterChange(key="remove", new_value=None)]
    )
    assert steps_mod.apply_diff(target, diff) is True
    after = target.read_text()
    assert "keep: me" in after
    assert "remove" not in after.split("---")[1]


# ---------------------------------------------------------------------------
# Step 5 — side_checks (parallel + per-branch timeout)
# ---------------------------------------------------------------------------


def test_side_checks_run_in_parallel(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    target = mind / "cortex-memory" / "research" / "hub.md"
    target.write_text(
        "---\nupdated: 2026-05-07\n---\n\n"
        "Hub note with [[neighbor-a]] and [[neighbor-b]].\n",
        encoding="utf-8",
    )
    (mind / "cortex-memory" / "research" / "neighbor-a.md").write_text(
        "---\naccess_count: 0\n---\n\nneighbor a body\n", encoding="utf-8"
    )
    (mind / "cortex-memory" / "research" / "neighbor-b.md").write_text(
        "---\naccess_count: 5\nupdated: 2026-05-07\n---\n\nneighbor b\n",
        encoding="utf-8",
    )

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )

    async def slow_response(_sys: str, _user: str) -> str:
        await asyncio.sleep(0.2)
        if "stale_finding_lint" in _sys:
            return json.dumps({"verdict": "still_open", "summary": "ok"})
        if "shadow_neighbor" in _sys:
            return json.dumps({"tldr": "auto tldr"})
        if "conflict_scan" in _sys:
            return json.dumps({"verdict": "no_conflict", "summary": "ok"})
        raise AssertionError("unknown system prompt")

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        import time as _time

        t0 = _time.perf_counter()
        results = started.run_until_complete(
            steps_mod.side_checks(state, target, model_call=slow_response)
        )
        elapsed = _time.perf_counter() - t0
    finally:
        started.close()

    assert all(r is not None and r.ok for r in results.all())
    assert elapsed < 0.5
    after = (mind / "cortex-memory" / "research" / "neighbor-a.md").read_text()
    assert "access_count: 1" in after
    assert "tldr: auto tldr" in after


def test_side_check_timeout_returns_none(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    target = mind / "cortex-memory" / "research" / "hub.md"
    target.write_text("---\n---\n\nHub with [[stale-friend]].\n", encoding="utf-8")
    (mind / "cortex-memory" / "research" / "stale-friend.md").write_text(
        "---\naccess_count: 0\n---\n\nfriend body\n", encoding="utf-8"
    )

    async def hang(_sys: str, _user: str) -> str:
        await asyncio.sleep(2.0)
        return "{}"

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 0, 0, 0),
    )

    results = asyncio.run(
        steps_mod.side_checks(state, target, model_call=hang, branch_timeout_s=0.1)
    )
    for r in results.all():
        assert r.ok is False
        assert r.error == "timeout"


# ---------------------------------------------------------------------------
# Step 6 — emit_surfaces
# ---------------------------------------------------------------------------


def test_emit_surfaces_writes_one_file_per_payload(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    state = WakeState(
        mind_dir=paths["mind"],
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
        surface_payloads=[
            SurfacePayload(surface_type="stage-b-routed", body="payload one"),
            SurfacePayload(
                surface_type="stage-b-conflict",
                body="payload two",
                extra_frontmatter={"slug": "x"},
            ),
        ],
    )
    written = steps_mod.emit_surfaces(state)
    assert written == 2
    surfaces = sorted((paths["mind"] / "inner" / "surface").glob("2026-05-08-*.md"))
    assert len(surfaces) == 2
    bodies = [p.read_text() for p in surfaces]
    assert any("payload one" in b for b in bodies)
    assert any("payload two" in b for b in bodies)


def test_emit_surfaces_apply_writes_false_is_dry_run(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    state = WakeState(
        mind_dir=paths["mind"],
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
        surface_payloads=[SurfacePayload(surface_type="x", body="y")],
    )
    written = steps_mod.emit_surfaces(state, apply_writes=False)
    assert written == 1
    assert not list((paths["mind"] / "inner" / "surface").glob("*.md"))


# ---------------------------------------------------------------------------
# Step 7 — close
# ---------------------------------------------------------------------------


def test_close_writes_summary_and_runs_prune(tmp_path: pathlib.Path) -> None:
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    old_dir = mind / "inner" / "thoughts" / "2025-01-01"
    young_dir = mind / "inner" / "thoughts" / "2026-05-07"
    old_dir.mkdir(parents=True)
    young_dir.mkdir(parents=True)
    (old_dir / "x.md").write_text("old", encoding="utf-8")
    (young_dir / "y.md").write_text("young", encoding="utf-8")

    state = WakeState(
        mind_dir=mind,
        state_dir=paths["state"],
        wake_file_path=None,
        mode="sleep_b",
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
    )
    from alice_thinking.workflows.stage_b.types import StepResult

    results = [
        StepResult(step="read_wake_state", ok=True, duration_ms=1, details={}),
        StepResult(step="emit_surfaces", ok=True, duration_ms=1, details={"count": 0}),
    ]
    summary = steps_mod.close(state, results, duration_ms=10)
    assert summary.summary_path is not None
    assert summary.summary_path.is_file()
    body = summary.summary_path.read_text()
    assert "Stage B wake" in body
    assert "read_wake_state" in body

    assert not old_dir.exists()
    assert young_dir.exists()


# ---------------------------------------------------------------------------
# Runner integration — wake closes cleanly on step error + per-step timeout
# ---------------------------------------------------------------------------


def _runner_config(
    paths: dict[str, pathlib.Path],
    *,
    timeouts: dict[str, float] | None = None,
) -> StageBRunnerConfig:
    return StageBRunnerConfig(
        mind_dir=paths["mind"],
        state_dir=paths["state"],
        wake_file_path=None,
        now=_dt.datetime(2026, 5, 8, 3, 0, 0),
        step_timeouts=timeouts or dict(DEFAULT_STEP_TIMEOUTS),
        side_check_branch_timeout_s=0.5,
        event_log_path=paths["state"] / "events.log",
    )


def test_wake_closes_cleanly_on_step_error(tmp_path: pathlib.Path) -> None:
    """A model failure during drain_inbox is handled per-note (caught
    inside the step). Step 7 still runs and emits a summary."""
    paths = _build_mind(tmp_path)
    note = paths["mind"] / "inner" / "notes" / "001.md"
    note.write_text("body", encoding="utf-8")

    async def boom(_sys: str, _user: str) -> str:
        raise RuntimeError("classifier exploded")

    cfg = _runner_config(paths)
    emitter = CapturingEmitter()
    summary = asyncio.run(run_stage_b_wake(cfg, model_call=boom, emitter=emitter))
    assert summary.summary_path is not None
    assert summary.summary_path.is_file()
    drain_step = next(s for s in summary.steps if s.step == "drain_inbox")
    assert drain_step.ok is True
    assert drain_step.details["errors"] == 1


def test_wake_per_step_timeout(tmp_path: pathlib.Path) -> None:
    """If drain_inbox hangs past its timeout, the runner cancels it,
    records the error, and continues."""
    paths = _build_mind(tmp_path)
    (paths["mind"] / "inner" / "notes" / "001.md").write_text("x", encoding="utf-8")

    async def hang(_sys: str, _user: str) -> str:
        await asyncio.sleep(2.0)
        return "{}"

    cfg = _runner_config(
        paths,
        timeouts={**DEFAULT_STEP_TIMEOUTS, "drain_inbox": 0.05},
    )
    emitter = CapturingEmitter()
    summary = asyncio.run(run_stage_b_wake(cfg, model_call=hang, emitter=emitter))
    drain_step = next(s for s in summary.steps if s.step == "drain_inbox")
    assert drain_step.ok is False
    assert drain_step.error == "timeout"
    assert summary.summary_path is not None


def test_full_workflow_end_to_end_with_mocks(tmp_path: pathlib.Path) -> None:
    """Synthetic vault + scripted ModelCall — drives the full ADK
    SequentialAgent end-to-end, asserts side-effects + telemetry."""
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    (mind / "inner" / "notes" / "001-promote.md").write_text(
        "promotable fact", encoding="utf-8"
    )
    (mind / "inner" / "notes" / "002-event.md").write_text(
        "event note", encoding="utf-8"
    )
    target = mind / "cortex-memory" / "research" / "stale.md"
    target.write_text(
        "---\nupdated: 2025-01-01\naccess_count: 0\n---\n\n"
        "## body\n\nold content with [[broken-link]].\n",
        encoding="utf-8",
    )
    (mind / "cortex-memory" / "research" / "neighbor.md").write_text(
        "---\naccess_count: 0\nupdated: 2026-05-07\n---\n\nneighbor body\n",
        encoding="utf-8",
    )

    classify_responses = [
        _action_response(
            "promote_to_vault",
            target_path="cortex-memory/research/2026-05-08-promoted.md",
            new_content="# Promoted\n",
        ),
        _action_response("append_to_daily", line="event happened"),
    ]
    diff_response = _diff_response(
        frontmatter_changes=[{"key": "updated", "new_value": "2026-05-08"}],
        wikilink_fixes=[{"old_target": "broken-link", "new_target": "fixed-link"}],
        rationale="freshen",
    )
    side_responses = {
        "stale_finding_lint": json.dumps(
            {"verdict": "still_open", "summary": "ok"}
        ),
        "shadow_neighbor": json.dumps({"tldr": "auto tldr"}),
        "conflict_scan": json.dumps(
            {"verdict": "no_conflict", "summary": "ok"}
        ),
    }
    model = ScriptedModel(
        {
            "classify_and_route_note": list(classify_responses),
            "produce_grooming_diff": diff_response,
            **side_responses,
        }
    )

    cfg = _runner_config(paths)
    emitter = CapturingEmitter()
    summary = asyncio.run(run_stage_b_wake(cfg, model_call=model, emitter=emitter))

    assert (mind / "cortex-memory" / "research" / "2026-05-08-promoted.md").is_file()
    daily = mind / "cortex-memory" / "dailies" / "2026-05-08.md"
    assert daily.is_file() and "event happened" in daily.read_text()
    assert not (mind / "inner" / "notes" / "001-promote.md").exists()
    assert not (mind / "inner" / "notes" / "002-event.md").exists()
    after = target.read_text()
    assert "updated: 2026-05-08" in after
    assert "[[fixed-link]]" in after
    assert summary.summary_path is not None and summary.summary_path.is_file()
    step_events = emitter.of_kind("stage_b_step")
    assert {e["step"] for e in step_events} >= {
        "read_wake_state",
        "drain_inbox",
        "pick_grooming_target",
        "groom_target",
        "side_checks",
        "emit_surfaces",
        "close",
    }
    summary_events = emitter.of_kind("stage_b_wake_summary")
    assert len(summary_events) == 1
    se = summary_events[0]
    assert se["actions_total"] == 2
    assert se["steps_failed"] == 0


def test_shadow_mode_does_not_write_or_consume(tmp_path: pathlib.Path) -> None:
    """Shadow mode classifies + reports but never touches disk."""
    paths = _build_mind(tmp_path)
    mind = paths["mind"]
    note = mind / "inner" / "notes" / "001.md"
    note.write_text("body", encoding="utf-8")
    target = mind / "cortex-memory" / "research" / "stale.md"
    target_original = "---\nupdated: 2025-01-01\naccess_count: 0\n---\n\nbody\n"
    target.write_text(target_original, encoding="utf-8")

    model = ScriptedModel(
        {
            "classify_and_route_note": _action_response("discard", reason="x"),
            "produce_grooming_diff": _diff_response(
                frontmatter_changes=[{"key": "updated", "new_value": "2026-05-08"}]
            ),
            "stale_finding_lint": json.dumps(
                {"verdict": "still_open", "summary": "ok"}
            ),
            "shadow_neighbor": json.dumps({"tldr": ""}),
            "conflict_scan": json.dumps({"verdict": "no_conflict", "summary": "ok"}),
        }
    )

    cfg = _runner_config(paths)
    emitter = CapturingEmitter()
    asyncio.run(run_stage_b_shadow(cfg, model_call=model, emitter=emitter))

    assert note.is_file()
    assert target.read_text() == target_original
    shadow_events = emitter.of_kind("stage_b_shadow_step")
    assert len(shadow_events) == 7
    assert emitter.of_kind("stage_b_step") == []
    assert len(emitter.of_kind("stage_b_shadow_wake_summary")) == 1


# ---------------------------------------------------------------------------
# Subroutines — JSON parsing + coercion edges
# ---------------------------------------------------------------------------


def test_classify_rejects_invalid_json(tmp_path: pathlib.Path) -> None:
    from alice_thinking.workflows.stage_b.subroutines import classify_and_route_note

    async def model(_sys: str, _user: str) -> str:
        return "not json at all"

    with pytest.raises(ValueError):
        asyncio.run(
            classify_and_route_note(
                note_path=tmp_path / "n.md",
                note_body="x",
                model_call=model,
            )
        )


def test_classify_rejects_unknown_action() -> None:
    from alice_thinking.workflows.stage_b.subroutines import classify_and_route_note

    async def model(_sys: str, _user: str) -> str:
        return json.dumps({"action": "do_something_weird"})

    with pytest.raises(ValueError):
        asyncio.run(
            classify_and_route_note(
                note_path=pathlib.Path("/tmp/n.md"),
                note_body="x",
                model_call=model,
            )
        )
