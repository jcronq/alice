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
    assert fresh.names() == ["speaking", "thinking"]


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
