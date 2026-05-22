"""Phase 3 of #194 — call-site migration tests.

Pins three things:

1. Every ``persona != ("thinking" | "speaking")`` SPAWN_MAP row carries
   an ``agent_spec`` field that resolves to a registered
   :class:`AgentSpec` in
   :data:`core.agent_library.default_registry`. (The two SDK-lane rows
   carry ``agent_spec`` too — see :func:`test_spawn_map_rows_reference_registered_agent_spec`.)
2. The dispatcher spawn module looks up the registered spec on every
   ``compose_spawn_prompt`` call (smoke test on
   :func:`alice_forge.dispatcher.spawn._resolve_agent_spec`).
3. The design-pipeline reviewer dispatches via
   :func:`core.agent_library.run_agent` against the registered
   ``"reviewer"`` spec with a per-call
   :func:`dataclasses.replace` swap for the design-doc system prompt.

The tests do NOT exercise the real kernel — they stub the registry
lookup / ``run_agent`` so the assertions stay deterministic and the
test suite can run without network access.
"""

from __future__ import annotations

from typing import Any

import pytest

from alice_forge.dispatcher import constants as sm_constants
from alice_forge.dispatcher import spawn as spawn_module
from alice_thinking import design_pipeline
from core.agent_library import default_registry
from core.kernel import KernelResult, UsageInfo


# ---------------------------------------------------------------------------
# (1) SPAWN_MAP rows reference registered agent specs
# ---------------------------------------------------------------------------


def test_spawn_map_rows_reference_registered_agent_spec() -> None:
    """Every SPAWN_MAP row carries an ``agent_spec`` that resolves to a
    registered :class:`AgentSpec`. Phase 4 (#321) will tighten this to
    require the field — Phase 3 ships the lookup."""
    for key, row in sm_constants.SPAWN_MAP.items():
        name = row.get("agent_spec")
        assert name, f"SPAWN_MAP row {key!r} missing ``agent_spec`` field"
        # Lookup must succeed against the production default_registry.
        spec = default_registry.get(name)
        assert spec.name == name, (
            f"SPAWN_MAP row {key!r}: registry returned spec name "
            f"{spec.name!r} for lookup {name!r}"
        )


@pytest.mark.parametrize(
    "row_key,expected_name",
    [
        (("sm:selected", "art:code"), "designer"),
        (("sm:selected", "art:config_change"), "config-worker"),
        (("sm:selected", "art:research_note"), "research-writer"),
        (("sm:selected", "art:experiment"), "research-writer"),
        (("sm:designed", "art:code"), "speaking"),
        (("sm:reviewing", "art:code"), "reviewer"),
    ],
)
def test_spawn_map_row_pins_specific_agent_spec(
    row_key: tuple[str, str], expected_name: str
) -> None:
    """Spot-check each row's ``agent_spec`` field. Locks in the
    canonical mapping per the Phase 3 mandate so a future SPAWN_MAP
    edit that drops the wire surfaces here."""
    row = sm_constants.SPAWN_MAP[row_key]
    assert row.get("agent_spec") == expected_name


def test_spawn_map_legacy_fields_preserved_for_worker_rows() -> None:
    """Phase 4 territory: the v1 worker rows still carry
    ``system_prompt_role`` and ``instruction_trailer`` so
    :func:`compose_spawn_prompt` stays byte-identical. Phase 3 must
    NOT drop these fields — only Phase 4 (#321) collapses them."""
    for key in (
        ("sm:selected", "art:config_change"),
        ("sm:selected", "art:research_note"),
        ("sm:selected", "art:experiment"),
    ):
        row = sm_constants.SPAWN_MAP[key]
        assert "system_prompt_role" in row, key
        assert "instruction_trailer" in row, key


# ---------------------------------------------------------------------------
# (2) spawn._resolve_agent_spec — registry lookup at compose_spawn_prompt
# ---------------------------------------------------------------------------


def test_resolve_agent_spec_returns_registered_spec() -> None:
    """The helper that compose_spawn_prompt calls returns the
    registered :class:`AgentSpec` for the ``agent_spec`` field."""
    row = {"agent_spec": "research-writer"}
    spec = spawn_module._resolve_agent_spec(row)
    assert spec is not None
    assert spec.name == "research-writer"


def test_resolve_agent_spec_returns_none_for_missing_field() -> None:
    """Pre-#194 rows / test fixtures without ``agent_spec`` get a
    ``None`` so the caller can fall through to the inline fields."""
    assert spawn_module._resolve_agent_spec({}) is None


def test_resolve_agent_spec_returns_none_for_unknown_name() -> None:
    """An unknown agent name doesn't raise — returns ``None`` so the
    legacy worker-prompt path keeps working while the migration is in
    flight."""
    assert spawn_module._resolve_agent_spec({"agent_spec": "ghost"}) is None


def test_compose_spawn_prompt_still_byte_identical_for_worker_row() -> None:
    """The compose_spawn_prompt output for a v1 worker row must not
    regress on the Phase 3 migration — the instruction trailer + role
    label are still consumed from the inline fields."""
    row = sm_constants.SPAWN_MAP[("sm:selected", "art:config_change")]
    issue = {
        "number": 42,
        "title": "tighten X",
        "body": "Body text.",
        "labels": [{"name": "art:config_change"}],
        "user": {"login": "jcronq"},
    }
    out = spawn_module.compose_spawn_prompt(issue, row)
    assert "You are a code-worker agent" in out
    assert "Closes #42" in out
    assert "Self-merge once CI is green" in out
    assert "Do not --no-verify" in out


# ---------------------------------------------------------------------------
# (3) design_pipeline.SubAgentRunner._review_via_agent_library dispatch
# ---------------------------------------------------------------------------


def _kernel_result(text: str) -> KernelResult:
    return KernelResult(
        text=text,
        session_id="sess-test",
        usage=UsageInfo(input_tokens=1, output_tokens=1),
        duration_ms=10,
        cost_usd=None,
        is_error=False,
        num_turns=1,
    )


@pytest.mark.asyncio
async def test_review_via_agent_library_uses_registered_reviewer_spec(
    monkeypatch,
) -> None:
    """The reviewer dispatch goes through :func:`run_agent` against
    the registered ``"reviewer"`` spec, with the design-pipeline
    system prompt swapped in via :func:`dataclasses.replace`."""
    captured: dict[str, Any] = {}

    async def _fake_run_agent(agent, *, prompt, emitter, **kwargs):
        captured["agent"] = agent
        captured["prompt"] = prompt
        captured["emitter"] = emitter
        captured["kwargs"] = kwargs
        return _kernel_result('{"verdict": "approved", "summary": "ok", "confidence": 0.9}')

    monkeypatch.setattr(design_pipeline, "default_registry", default_registry, raising=False)
    # Patch run_agent at the import site inside the reviewer method.
    from core.agent_library import runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_agent", _fake_run_agent)
    # The method imports run_agent from ``core.agent_library`` (the
    # package's re-export) — patch that surface too.
    import core.agent_library as agent_library_pkg

    monkeypatch.setattr(agent_library_pkg, "run_agent", _fake_run_agent)

    runner = design_pipeline.SubAgentRunner(
        model="claude-sonnet-4-6",
        system_prompt="DESIGN_REVIEWER_SYSTEM_PROMPT_TEST",
        max_seconds=120,
    )
    out = await runner._review_via_agent_library("test prompt body")

    assert "approved" in out
    agent = captured["agent"]
    # Persona / behavioral_constraints come from the registered
    # reviewer spec (not a hand-rolled AgentSpec).
    registered = default_registry.get("reviewer")
    assert agent.persona == registered.persona
    assert agent.behavioral_constraints == registered.behavioral_constraints
    # Per-call override pinned the kernel_spec to the runner's model /
    # max_seconds / system prompt, and the policy was cleared so the
    # explicit allowed_tools=[] override stays empty.
    assert agent.kernel_spec.model == "claude-sonnet-4-6"
    assert agent.kernel_spec.max_seconds == 120
    assert agent.kernel_spec.allowed_tools == []
    assert agent.kernel_spec.append_system_prompt == "DESIGN_REVIEWER_SYSTEM_PROMPT_TEST"
    assert agent.tool_policy is None
    # Prompt was forwarded verbatim.
    assert captured["prompt"] == "test prompt body"
    # correlation_id is set for telemetry / audit-trail attribution.
    assert captured["kwargs"].get("correlation_id") == "design-pipeline-reviewer"


@pytest.mark.asyncio
async def test_review_via_agent_library_does_not_mutate_registered_spec(
    monkeypatch,
) -> None:
    """The per-call ``dataclasses.replace`` must NOT mutate the
    registry singleton — re-fetching the spec after a review turn
    yields the original code-reviewer ``append_system_prompt``."""
    original = default_registry.get("reviewer")
    original_prompt = original.kernel_spec.append_system_prompt

    async def _fake_run_agent(agent, *, prompt, emitter, **kwargs):
        return _kernel_result('{"verdict": "approved", "summary": "x", "confidence": 0.5}')

    import core.agent_library as agent_library_pkg

    monkeypatch.setattr(agent_library_pkg, "run_agent", _fake_run_agent)

    runner = design_pipeline.SubAgentRunner(
        model="claude-sonnet-4-6",
        system_prompt="EPHEMERAL_PROMPT",
    )
    await runner._review_via_agent_library("hello")

    refetched = default_registry.get("reviewer")
    assert refetched.kernel_spec.append_system_prompt == original_prompt
    # And the registered spec is the exact same identity object — no
    # accidental copy-on-write inside the registry.
    assert refetched is original


def test_review_text_drives_async_review(monkeypatch) -> None:
    """:meth:`SubAgentRunner.review_text` is the public sync seam.
    It runs the async :meth:`_review_via_agent_library` to completion
    and returns the result text. Patch the async method to avoid the
    real kernel dispatch."""

    async def _fake_review(self, prompt: str) -> str:
        return f"OK:{prompt}"

    monkeypatch.setattr(
        design_pipeline.SubAgentRunner,
        "_review_via_agent_library",
        _fake_review,
    )

    runner = design_pipeline.SubAgentRunner()
    assert runner.review_text("hi") == "OK:hi"
