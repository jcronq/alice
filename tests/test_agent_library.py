"""Tests for :mod:`core.agent_library`.

Phase 1 of issue #194. Covers:

* :meth:`ToolPolicy.evaluate` for both allow and deny modes.
* :meth:`BehavioralRule.render` and :func:`prompt_assembly.merge`.
* :meth:`AgentSpec.build_spec` end-to-end (tool filtering + prompt
  merge applied to the wrapped :class:`KernelSpec`).
* :class:`Registry` get / register / replace / contains semantics.
* :func:`run_agent` plumbing — uses a stub backend + kernel so the
  test does not hit the network.
* Module-level :data:`default_registry` exposes the built-in
  ``thinking`` and ``speaking`` entries with the expected
  behavioral rules and tool sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.agent_library import (
    AgentSpec,
    BehavioralRule,
    PolicyViolation,
    Registry,
    ToolPolicy,
    agents as builtin_agents,
    default_registry,
    policies,
    run_agent,
)
from core.agent_library.prompt_assembly import merge
from core.kernel import KernelResult, KernelSpec, UsageInfo


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------


def test_tool_policy_allow_returns_intersection():
    policy = ToolPolicy(
        type="allow", allowlist=frozenset({"Read", "Write", "Bash"})
    )
    assert policy.evaluate({"Read", "Bash", "Edit"}) == {"Read", "Bash"}


def test_tool_policy_deny_returns_complement():
    policy = ToolPolicy(type="deny", denylist=frozenset({"Bash", "Edit"}))
    assert policy.evaluate({"Read", "Bash", "Edit", "Grep"}) == {"Read", "Grep"}


def test_tool_policy_allow_drains_to_empty_when_no_overlap():
    policy = ToolPolicy(type="allow", allowlist=frozenset({"Read"}))
    assert policy.evaluate({"Bash", "Edit"}) == set()


# ---------------------------------------------------------------------------
# BehavioralRule + prompt_assembly.merge
# ---------------------------------------------------------------------------


def test_behavioral_rule_render_includes_id_heading():
    rule = BehavioralRule(id="rule-001", injection="Be brief.")
    rendered = rule.render()
    assert "## Constraint: rule-001" in rendered
    assert "Be brief." in rendered


def test_merge_returns_none_for_all_empty_inputs():
    assert merge(base=None, rules=(), extra="") is None
    assert merge(base="", rules=(), extra="") is None


def test_merge_joins_base_rules_extra_with_blank_lines():
    rules = (
        BehavioralRule(id="a", injection="alpha"),
        BehavioralRule(id="b", injection="beta"),
    )
    out = merge(base="BASE", rules=rules, extra="EXTRA")
    sections = out.split("\n\n")
    assert sections[0] == "BASE"
    assert sections[1].startswith("## Constraint: a")
    assert sections[1].endswith("alpha")
    assert sections[2].startswith("## Constraint: b")
    assert sections[2].endswith("beta")
    assert sections[3] == "EXTRA"


def test_merge_skips_empty_sections():
    rules = (BehavioralRule(id="x", injection="only-rule"),)
    out = merge(base=None, rules=rules, extra="")
    # Single section — no leading or trailing blank-line padding.
    assert out is not None
    assert out.startswith("## Constraint: x")
    assert out.endswith("only-rule")


# ---------------------------------------------------------------------------
# AgentSpec
# ---------------------------------------------------------------------------


def _bare_spec(**overrides: Any) -> AgentSpec:
    defaults = dict(
        name="probe",
        persona="probe",
        kernel_spec=KernelSpec(
            model="claude-opus-4-7",
            allowed_tools=["Read", "Write", "Bash"],
            append_system_prompt="hello",
        ),
    )
    defaults.update(overrides)
    return AgentSpec(**defaults)


def test_agent_spec_effective_tools_sorted_and_policy_applied():
    spec = _bare_spec(
        tool_policy=ToolPolicy(type="allow", allowlist=frozenset({"Read", "Bash"})),
    )
    assert spec.effective_tools() == ["Bash", "Read"]


def test_agent_spec_effective_tools_raises_when_drained():
    spec = _bare_spec(
        tool_policy=ToolPolicy(type="allow", allowlist=frozenset({"Glob"})),
    )
    with pytest.raises(PolicyViolation):
        spec.effective_tools()


def test_agent_spec_no_policy_passes_tools_through():
    spec = _bare_spec()
    assert spec.effective_tools() == ["Bash", "Read", "Write"]


def test_agent_spec_assembled_prompt_merges_rules_after_base():
    spec = _bare_spec(
        behavioral_constraints=(
            BehavioralRule(id="r1", injection="be sharp"),
        ),
    )
    out = spec.assembled_system_prompt()
    assert out is not None
    assert out.startswith("hello")
    assert "## Constraint: r1" in out
    assert "be sharp" in out


def test_agent_spec_assembled_prompt_none_when_empty():
    spec = AgentSpec(
        name="empty",
        persona="empty",
        kernel_spec=KernelSpec(model="claude-opus-4-7"),
    )
    assert spec.assembled_system_prompt() is None


def test_agent_spec_build_spec_returns_new_kernel_spec():
    rule = BehavioralRule(id="rule-x", injection="constraint text")
    spec = _bare_spec(
        tool_policy=ToolPolicy(
            type="allow", allowlist=frozenset({"Read", "Bash"})
        ),
        behavioral_constraints=(rule,),
    )
    built = spec.build_spec()
    assert built is not spec.kernel_spec
    assert built.allowed_tools == ["Bash", "Read"]
    assert built.append_system_prompt is not None
    assert "hello" in built.append_system_prompt
    assert "## Constraint: rule-x" in built.append_system_prompt
    # Original spec untouched.
    assert spec.kernel_spec.allowed_tools == ["Read", "Write", "Bash"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_then_get_roundtrip():
    reg = Registry()
    spec = _bare_spec(name="solo")
    reg.register(spec)
    assert reg.get("solo") is spec
    assert "solo" in reg
    assert reg.names() == ["solo"]


def test_registry_duplicate_register_raises():
    reg = Registry()
    reg.register(_bare_spec(name="dup"))
    with pytest.raises(ValueError):
        reg.register(_bare_spec(name="dup"))


def test_registry_replace_overwrites():
    reg = Registry()
    reg.register(_bare_spec(name="dup"))
    replacement = _bare_spec(name="dup", persona="other")
    reg.replace(replacement)
    assert reg.get("dup") is replacement


def test_registry_get_unknown_raises_key_error_with_known_names():
    reg = Registry()
    reg.register(_bare_spec(name="alpha"))
    reg.register(_bare_spec(name="beta"))
    with pytest.raises(KeyError) as exc:
        reg.get("ghost")
    assert "alpha" in str(exc.value)
    assert "beta" in str(exc.value)


# ---------------------------------------------------------------------------
# Built-in registrations
# ---------------------------------------------------------------------------


def test_default_registry_includes_thinking_and_speaking():
    assert "thinking" in default_registry
    assert "speaking" in default_registry


def test_thinking_agent_has_real_world_write_constraint():
    spec = default_registry.get("thinking")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "no-real-world-writes" in rule_ids
    assert "design-not-build" in rule_ids


def test_speaking_agent_carries_send_message_constraint():
    spec = default_registry.get("speaking")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "signal-via-send-message" in rule_ids


def test_register_builtins_is_idempotent_on_default_registry():
    # Calling again must not raise; the agents are already registered.
    builtin_agents.register_builtins()
    assert "thinking" in default_registry


def test_register_builtins_populates_a_fresh_registry():
    fresh = Registry()
    builtin_agents.register_builtins(fresh)
    # Phase 1 + Phase 2 — eight built-in flavors. Sorted by Registry.names().
    assert fresh.names() == [
        "code-worker",
        "config-worker",
        "designer",
        "research-writer",
        "reviewer",
        "speaking",
        "thinking",
        "watcher",
    ]


# ---------------------------------------------------------------------------
# Phase 2 — built-in flavor specifics
# ---------------------------------------------------------------------------


def test_code_worker_carries_pr_and_no_verify_rules():
    spec = default_registry.get("code-worker")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "open-pr-closes-issue" in rule_ids
    assert "self-merge-on-green" in rule_ids
    assert "no-verify-forbidden" in rule_ids


def test_code_worker_build_spec_full_access_keeps_edit_and_bash():
    spec = default_registry.get("code-worker")
    built = spec.build_spec()
    assert built.model == "claude-opus-4-7"
    assert "Bash" in built.allowed_tools
    assert "Edit" in built.allowed_tools
    assert "Write" in built.allowed_tools


def test_code_worker_prompt_merges_no_verify_block():
    spec = default_registry.get("code-worker")
    built = spec.build_spec()
    prompt = built.append_system_prompt or ""
    assert "## Constraint: open-pr-closes-issue" in prompt
    assert "Closes #" in prompt
    assert "## Constraint: no-verify-forbidden" in prompt
    assert "--no-verify" in prompt


def test_research_writer_carries_vault_and_transition_rules():
    spec = default_registry.get("research-writer")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "write-research-note-under-vault" in rule_ids
    assert "transition-to-done-on-completion" in rule_ids


def test_research_writer_build_spec_includes_websearch_for_grounding():
    spec = default_registry.get("research-writer")
    built = spec.build_spec()
    # Grounding claims outside the vault requires WebSearch.
    assert "WebSearch" in built.allowed_tools
    assert "Write" in built.allowed_tools


def test_research_writer_prompt_names_vault_path():
    spec = default_registry.get("research-writer")
    built = spec.build_spec()
    prompt = built.append_system_prompt or ""
    assert "~/alice-mind/cortex-memory/research/" in prompt


def test_reviewer_uses_sonnet_model():
    spec = default_registry.get("reviewer")
    assert spec.kernel_spec.model == "claude-sonnet-4-6"


def test_reviewer_build_spec_strips_edit_and_write():
    spec = default_registry.get("reviewer")
    built = spec.build_spec()
    # read_only policy: Edit / Write / Bash all gone, Read / Glob /
    # Grep remain.
    assert "Edit" not in built.allowed_tools
    assert "Write" not in built.allowed_tools
    assert "Bash" not in built.allowed_tools
    assert "Read" in built.allowed_tools
    assert "Glob" in built.allowed_tools
    assert "Grep" in built.allowed_tools


def test_reviewer_carries_strict_json_constraint():
    spec = default_registry.get("reviewer")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "strict-json-verdict" in rule_ids
    assert "verdict-gate" in rule_ids


def test_reviewer_output_schema_references_canonical_prompt():
    spec = default_registry.get("reviewer")
    assert spec.output_schema is not None
    # The OutputSchema name records the dotted reference to the
    # canonical prompt constant so a Phase 2+ validator can resolve
    # it at registration time. Core must not import alice_speaking.
    assert "alice_speaking.review.code_reviewer" in spec.output_schema.name
    assert "CODE_REVIEWER_SYSTEM_PROMPT" in spec.output_schema.name


def test_designer_inherits_thinking_rules_and_adds_design_ready():
    spec = default_registry.get("designer")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    # Inherits the thinking sandbox + design-not-build rules.
    assert "no-real-world-writes" in rule_ids
    assert "design-not-build" in rule_ids
    # And adds the SM v2 exit-comment rule.
    assert "design-ready-exit-comment" in rule_ids


def test_designer_runs_per_issue_not_background():
    spec = default_registry.get("designer")
    assert spec.scope == "per-issue"
    assert spec.lifecycle == "per-issue"
    # Persona collapses onto the thinking persona — same behavioral
    # core, narrower scope. Documented in agents.py module docstring.
    assert spec.persona == "thinking"


def test_designer_build_spec_keeps_full_thinking_tool_surface():
    spec = default_registry.get("designer")
    built = spec.build_spec()
    assert "mcp__alice__run_experiment" in built.allowed_tools
    assert "mcp__alice__send_message" in built.allowed_tools


def test_watcher_uses_read_only_with_signal_policy():
    spec = default_registry.get("watcher")
    built = spec.build_spec()
    # Read tools present; send_message present; write tools stripped.
    assert "Read" in built.allowed_tools
    assert "mcp__alice__send_message" in built.allowed_tools
    assert "Edit" not in built.allowed_tools
    assert "Write" not in built.allowed_tools
    assert "Bash" not in built.allowed_tools


def test_watcher_carries_notes_not_action_constraint():
    spec = default_registry.get("watcher")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "notes-not-direct-action" in rule_ids
    assert "signal-only-when-warranted" in rule_ids


def test_config_worker_carries_threshold_and_scope_rules():
    spec = default_registry.get("config-worker")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "config-files-only" in rule_ids
    assert "smaller-diff-threshold" in rule_ids
    assert "open-pr-closes-issue" in rule_ids


def test_config_worker_threshold_prompt_names_loc_cap():
    spec = default_registry.get("config-worker")
    built = spec.build_spec()
    prompt = built.append_system_prompt or ""
    # Threshold lives in the rule injection so it's visible to the
    # model at dispatch time. The user's tactical-threshold feedback
    # pins this at 15 LOC.
    assert "15" in prompt
    assert "schema" in prompt.lower()


def test_config_worker_build_spec_keeps_edit_and_strips_mcp():
    spec = default_registry.get("config-worker")
    built = spec.build_spec()
    assert "Edit" in built.allowed_tools
    assert "Write" in built.allowed_tools
    assert "Bash" in built.allowed_tools
    # MCP excluded — config-worker doesn't reach for Signal or
    # experiments.
    assert "mcp__alice__send_message" not in built.allowed_tools
    assert "mcp__alice__run_experiment" not in built.allowed_tools


# ---------------------------------------------------------------------------
# run_agent — plumbing test
# ---------------------------------------------------------------------------


@dataclass
class _StubKernel:
    """Capture the spec/prompt the runner would have dispatched."""

    captured_prompt: str = ""
    captured_spec: KernelSpec | None = None
    result: KernelResult = field(
        default_factory=lambda: KernelResult(
            text="ok",
            session_id="sess-1",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            duration_ms=10,
            cost_usd=None,
            is_error=False,
            num_turns=1,
        )
    )

    async def run(self, prompt, spec, handlers=None):
        self.captured_prompt = prompt
        self.captured_spec = spec
        return self.result


@pytest.mark.asyncio
async def test_run_agent_applies_policy_and_dispatches(monkeypatch):
    stub = _StubKernel()
    captured_factory_args: dict[str, Any] = {}

    def _fake_make_kernel(backend, emitter, **kwargs):
        captured_factory_args["backend"] = backend
        captured_factory_args["emitter"] = emitter
        captured_factory_args.update(kwargs)
        return stub

    # Patch where run_agent looks it up.
    from core.agent_library import runner as runner_mod

    monkeypatch.setattr(runner_mod, "make_kernel", _fake_make_kernel)

    spec = _bare_spec(
        tool_policy=ToolPolicy(
            type="allow", allowlist=frozenset({"Read", "Bash"})
        ),
        behavioral_constraints=(
            BehavioralRule(id="audit", injection="be sharp"),
        ),
    )

    emitter = object()  # opaque — the stub kernel doesn't touch it
    backend = object()  # opaque — make_kernel is stubbed
    result = await run_agent(
        spec,
        prompt="hello",
        emitter=emitter,
        backend=backend,
        correlation_id="probe-1",
    )

    assert result.text == "ok"
    assert stub.captured_prompt == "hello"
    assert stub.captured_spec is not None
    assert stub.captured_spec.allowed_tools == ["Bash", "Read"]
    assert "## Constraint: audit" in (
        stub.captured_spec.append_system_prompt or ""
    )
    assert captured_factory_args["backend"] is backend
    assert captured_factory_args["emitter"] is emitter
    assert captured_factory_args["correlation_id"] == "probe-1"


@pytest.mark.asyncio
async def test_run_agent_uses_default_subscription_backend(monkeypatch):
    """When ``backend=None`` the runner falls back to a default
    subscription :class:`BackendSpec` — verify the factory still
    receives a non-None value."""
    captured_backend: list[Any] = []

    def _fake_make_kernel(backend, emitter, **kwargs):
        captured_backend.append(backend)
        return _StubKernel()

    from core.agent_library import runner as runner_mod

    monkeypatch.setattr(runner_mod, "make_kernel", _fake_make_kernel)

    spec = _bare_spec()
    await run_agent(spec, prompt="x", emitter=object())

    assert len(captured_backend) == 1
    # Default backend has a "backend" attribute equal to "subscription".
    assert getattr(captured_backend[0], "backend", None) == "subscription"


# ---------------------------------------------------------------------------
# Policies — sanity check the pre-built instances
# ---------------------------------------------------------------------------


def test_read_only_policy_excludes_write_and_bash():
    assert "Read" in policies.read_only.allowlist
    assert "Write" not in policies.read_only.allowlist
    assert "Bash" not in policies.read_only.allowlist


def test_exec_only_policy_excludes_writes_but_allows_bash():
    assert "Bash" in policies.exec_only.allowlist
    assert "Read" in policies.exec_only.allowlist
    assert "Write" not in policies.exec_only.allowlist
    assert "Edit" not in policies.exec_only.allowlist


def test_full_access_policy_includes_signal_send_message():
    assert "mcp__alice__send_message" in policies.full_access.allowlist


def test_read_only_with_signal_policy_keeps_send_message_only():
    pol = policies.read_only_with_signal
    assert "Read" in pol.allowlist
    assert "mcp__alice__send_message" in pol.allowlist
    # Mutating tools excluded.
    assert "Edit" not in pol.allowlist
    assert "Write" not in pol.allowlist
    assert "Bash" not in pol.allowlist
    # The other MCP surface (experiments) is also out — watchers
    # don't run experiments, they observe.
    assert "mcp__alice__run_experiment" not in pol.allowlist


def test_config_writer_policy_keeps_edit_but_excludes_mcp():
    pol = policies.config_writer
    assert "Edit" in pol.allowlist
    assert "Write" in pol.allowlist
    assert "Bash" in pol.allowlist
    assert "Read" in pol.allowlist
    # MCP excluded — config-worker isn't a Signal/experiment endpoint.
    assert "mcp__alice__send_message" not in pol.allowlist
    assert "mcp__alice__run_experiment" not in pol.allowlist
