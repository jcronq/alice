"""Unit tests for :mod:`core.kernel.hooks_registry`.

PR2 ships the composition primitive (:class:`CompositeAgentHook`)
and the module-level registry (:func:`register_agent_hook`,
:func:`hooks_for`, :func:`get_all_hooks`). The registry is
import-time state, so tests reset it via a fixture rather than
risk leaking between cases.

Covered:

- :class:`CompositeAgentHook` chains ``before_agent_start`` /
  ``before_turn`` mutations (each hook sees the previous output).
- :meth:`after_turn` concatenates non-empty injections with
  newlines; absent injections drop out; an all-empty result is
  ``None``.
- :meth:`after_agent_end` runs every hook sequentially in
  registration order.
- An empty composite is a valid no-op on every method.
- :func:`register_agent_hook` appends to the right slot for
  role-scoped, multi-role, and global registrations.
- :func:`hooks_for` returns a fresh composite each call, with
  role-scoped hooks first then globals.
- :func:`get_all_hooks` returns globals + every role-scoped hook.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Optional

import pytest

from claude_agent_sdk import Message

from core.kernel import (
    AgentHook,
    CompositeAgentHook,
    KernelResult,
    KernelSpec,
    ToolResult,
    ToolUse,
    TurnResult,
    get_all_hooks,
    hooks_for,
    register_agent_hook,
)
from core.kernel import hooks_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Wipe the module-level registry around each test.

    The registry is shared module state — without this, hooks
    registered in one test would leak into the next. We snapshot
    both ``_REGISTRY`` (role-scoped) and ``_ALL_HOOKS`` (global)
    and restore them after the test runs.
    """
    saved_registry = {k: list(v) for k, v in hooks_registry._REGISTRY.items()}
    saved_all = list(hooks_registry._ALL_HOOKS)
    for v in hooks_registry._REGISTRY.values():
        v.clear()
    hooks_registry._ALL_HOOKS.clear()
    yield
    hooks_registry._REGISTRY.clear()
    hooks_registry._REGISTRY.update(saved_registry)
    hooks_registry._ALL_HOOKS.clear()
    hooks_registry._ALL_HOOKS.extend(saved_all)


@pytest.fixture
def spec() -> KernelSpec:
    return KernelSpec(model="claude-sonnet-4-5", allowed_tools=["Read"])


@pytest.fixture
def turn_result() -> TurnResult:
    return TurnResult(
        text="hi",
        tool_uses=[ToolUse(name="Edit", input={"file_path": "/tmp/x.md"}, id="tu_1")],
        tool_results=[ToolResult(tool_use_id="tu_1", content="ok", is_error=False)],
        stop_reason="end_turn",
    )


@pytest.fixture
def kernel_result() -> KernelResult:
    return KernelResult(
        text="done",
        session_id="s1",
        usage=None,
        duration_ms=100,
        cost_usd=None,
        is_error=False,
        num_turns=1,
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _RecordingHook:
    """Records every lifecycle call it observes.

    Used as the workhorse mock for CompositeAgentHook tests. The
    return values for the mutators are configurable so we can
    verify the composite threads the previous hook's output into
    the next.
    """

    def __init__(
        self,
        *,
        name: str = "hook",
        before_agent_start_return: Optional[KernelSpec] = None,
        before_turn_return: Optional[list[Message]] = None,
        after_turn_return: Optional[str] = None,
    ) -> None:
        self.name = name
        self.calls: dict[str, list] = defaultdict(list)
        self._before_agent_start_return = before_agent_start_return
        self._before_turn_return = before_turn_return
        self._after_turn_return = after_turn_return

    async def before_agent_start(
        self, spec: KernelSpec
    ) -> Optional[KernelSpec]:
        self.calls["before_agent_start"].append(spec)
        return self._before_agent_start_return

    async def before_turn(
        self, messages: list[Message]
    ) -> Optional[list[Message]]:
        self.calls["before_turn"].append(messages)
        return self._before_turn_return

    async def after_turn(self, turn_result: TurnResult) -> Optional[str]:
        self.calls["after_turn"].append(turn_result)
        return self._after_turn_return

    async def after_agent_end(self, result: KernelResult) -> None:
        self.calls["after_agent_end"].append(result)


# ---------------------------------------------------------------------------
# CompositeAgentHook
# ---------------------------------------------------------------------------


class TestCompositeAgentHook:
    def test_satisfies_agent_hook_protocol(self):
        """The composite itself is an AgentHook — runtime_checkable
        means it can be nested in another composite."""
        composite = CompositeAgentHook([])
        assert isinstance(composite, AgentHook)

    def test_empty_composite_is_noop(self, spec, turn_result, kernel_result):
        composite = CompositeAgentHook([])
        # before_agent_start: returns the spec unchanged.
        assert asyncio.run(composite.before_agent_start(spec)) is spec
        # before_turn: returns the messages list unchanged.
        messages: list[Message] = []
        assert asyncio.run(composite.before_turn(messages)) is messages
        # after_turn: no children means no injection.
        assert asyncio.run(composite.after_turn(turn_result)) is None
        # after_agent_end: doesn't raise.
        asyncio.run(composite.after_agent_end(kernel_result))

    def test_before_agent_start_chaining(self, spec):
        """Each hook sees the previous hook's modification."""
        spec_b = KernelSpec(model="claude-opus", allowed_tools=["Write"])
        spec_c = KernelSpec(model="claude-haiku", allowed_tools=["Bash"])
        hook_a = _RecordingHook(name="a", before_agent_start_return=spec_b)
        hook_b = _RecordingHook(name="b", before_agent_start_return=spec_c)
        hook_c = _RecordingHook(name="c")  # returns None → no further change
        composite = CompositeAgentHook([hook_a, hook_b, hook_c])

        result = asyncio.run(composite.before_agent_start(spec))

        # Final result is the last non-None return value.
        assert result is spec_c
        # Hook A saw the original spec.
        assert hook_a.calls["before_agent_start"] == [spec]
        # Hook B saw what A returned.
        assert hook_b.calls["before_agent_start"] == [spec_b]
        # Hook C saw what B returned (None pass-through means C sees B's output).
        assert hook_c.calls["before_agent_start"] == [spec_c]

    def test_before_turn_chaining(self):
        """before_turn threads message lists through children."""
        msgs_a: list[Message] = []
        msgs_b: list[Message] = []
        msgs_c: list[Message] = []
        hook_a = _RecordingHook(name="a", before_turn_return=msgs_b)
        hook_b = _RecordingHook(name="b", before_turn_return=msgs_c)
        hook_c = _RecordingHook(name="c")
        composite = CompositeAgentHook([hook_a, hook_b, hook_c])

        result = asyncio.run(composite.before_turn(msgs_a))

        assert result is msgs_c
        assert hook_a.calls["before_turn"] == [msgs_a]
        assert hook_b.calls["before_turn"] == [msgs_b]
        assert hook_c.calls["before_turn"] == [msgs_c]

    def test_before_turn_none_passthrough(self):
        """None return from a child must not break the chain — the
        previous value flows through to the next child."""
        msgs: list[Message] = []
        hook_a = _RecordingHook(name="a")  # None → pass-through
        hook_b = _RecordingHook(name="b")  # None → pass-through
        composite = CompositeAgentHook([hook_a, hook_b])

        result = asyncio.run(composite.before_turn(msgs))
        # No mutation, but the original list flows through.
        assert result is msgs

    def test_after_turn_concatenation(self, turn_result):
        """All hooks get the same turn_result; injections concatenate."""
        hook_a = _RecordingHook(name="a", after_turn_return="alpha")
        hook_b = _RecordingHook(name="b", after_turn_return="beta")
        hook_c = _RecordingHook(name="c")  # None → no injection
        hook_d = _RecordingHook(name="d", after_turn_return="delta")
        composite = CompositeAgentHook([hook_a, hook_b, hook_c, hook_d])

        injection = asyncio.run(composite.after_turn(turn_result))
        assert injection == "alpha\nbeta\ndelta"

        # Every hook sees the SAME turn_result (no chaining on input).
        for hook in (hook_a, hook_b, hook_c, hook_d):
            assert hook.calls["after_turn"] == [turn_result]

    def test_after_turn_none_when_all_empty(self, turn_result):
        """If no hook produces an injection, the composite returns None
        (not an empty string) so the run loop can short-circuit."""
        hook_a = _RecordingHook(name="a")
        hook_b = _RecordingHook(name="b")
        composite = CompositeAgentHook([hook_a, hook_b])
        assert asyncio.run(composite.after_turn(turn_result)) is None

    def test_after_turn_skips_empty_strings(self, turn_result):
        """Falsy injection returns (``""``, ``None``) don't appear in
        the concatenated output — only truthy strings contribute."""
        hook_a = _RecordingHook(name="a", after_turn_return="")
        hook_b = _RecordingHook(name="b", after_turn_return="real")
        composite = CompositeAgentHook([hook_a, hook_b])
        assert asyncio.run(composite.after_turn(turn_result)) == "real"

    def test_after_agent_end_sequential(self, kernel_result):
        """All hooks called in registration order; no return wiring."""
        order: list[str] = []

        class _Ordered:
            def __init__(self, name: str) -> None:
                self.name = name

            async def before_agent_start(self, spec):  # pragma: no cover
                return None

            async def before_turn(self, messages):  # pragma: no cover
                return None

            async def after_turn(self, turn_result):  # pragma: no cover
                return None

            async def after_agent_end(self, result):
                order.append(self.name)

        composite = CompositeAgentHook([_Ordered("a"), _Ordered("b"), _Ordered("c")])
        asyncio.run(composite.after_agent_end(kernel_result))
        assert order == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Registry: register_agent_hook / hooks_for / get_all_hooks
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_single_role(self):
        hook = _RecordingHook(name="speaking-only")
        register_agent_hook(hook, roles=["speaking"])
        assert hooks_registry._REGISTRY["speaking"] == [hook]
        assert hooks_registry._REGISTRY["thinking"] == []
        assert hooks_registry._REGISTRY["worker"] == []
        assert hooks_registry._ALL_HOOKS == []

    def test_register_multiple_roles(self):
        """A hook scoped to two roles appears in each list — the same
        instance, not a copy."""
        hook = _RecordingHook(name="both")
        register_agent_hook(hook, roles=["speaking", "worker"])
        assert hooks_registry._REGISTRY["speaking"] == [hook]
        assert hooks_registry._REGISTRY["worker"] == [hook]
        assert hooks_registry._REGISTRY["thinking"] == []
        assert hooks_registry._ALL_HOOKS == []

    def test_register_global_via_none(self):
        """``roles=None`` registers globally."""
        hook = _RecordingHook(name="global")
        register_agent_hook(hook)
        assert hooks_registry._ALL_HOOKS == [hook]
        for slot in hooks_registry._REGISTRY.values():
            assert slot == []

    def test_register_global_via_star(self):
        """The explicit ``["*"]`` sentinel also registers globally."""
        hook = _RecordingHook(name="global-star")
        register_agent_hook(hook, roles=["*"])
        assert hooks_registry._ALL_HOOKS == [hook]

    def test_register_unknown_role_extends_registry(self):
        """Unknown role names are accepted — the registry grows to
        accommodate them. Keeps the system pluggable without forcing
        a central enum."""
        hook = _RecordingHook(name="custom")
        register_agent_hook(hook, roles=["custom-role"])
        assert hooks_registry._REGISTRY["custom-role"] == [hook]

    def test_hooks_for_returns_composite(self):
        composite = hooks_for("speaking")
        assert isinstance(composite, CompositeAgentHook)

    def test_hooks_for_composition_ordering(self):
        """Role-scoped hooks come first, globals appended after."""
        scoped = _RecordingHook(name="scoped")
        global_hook = _RecordingHook(name="global")
        register_agent_hook(scoped, roles=["speaking"])
        register_agent_hook(global_hook)

        composite = hooks_for("speaking")
        assert composite._hooks == [scoped, global_hook]

    def test_hooks_for_unknown_role_returns_globals(self):
        """Unknown role names yield a composite with only globals."""
        global_hook = _RecordingHook(name="global")
        register_agent_hook(global_hook)
        composite = hooks_for("nonexistent")
        assert composite._hooks == [global_hook]

    def test_hooks_for_returns_fresh_composite_each_call(self):
        """Each call constructs a new composite — no caching. Mutating
        the returned composite's hook list must not affect the
        registry or future calls."""
        hook = _RecordingHook(name="h")
        register_agent_hook(hook, roles=["speaking"])

        first = hooks_for("speaking")
        second = hooks_for("speaking")
        assert first is not second
        assert first._hooks == second._hooks

        # Mutating one composite's list does not leak.
        first._hooks.append(_RecordingHook(name="rogue"))
        third = hooks_for("speaking")
        assert third._hooks == [hook]

    def test_hooks_for_role_isolation(self):
        """Hooks registered on one role do not appear in another
        role's composite (unless registered globally)."""
        speaking_hook = _RecordingHook(name="s")
        worker_hook = _RecordingHook(name="w")
        register_agent_hook(speaking_hook, roles=["speaking"])
        register_agent_hook(worker_hook, roles=["worker"])

        assert hooks_for("speaking")._hooks == [speaking_hook]
        assert hooks_for("worker")._hooks == [worker_hook]
        assert hooks_for("thinking")._hooks == []

    def test_get_all_hooks_returns_globals_and_scoped(self):
        scoped_speaking = _RecordingHook(name="s")
        scoped_worker = _RecordingHook(name="w")
        global_hook = _RecordingHook(name="g")
        register_agent_hook(scoped_speaking, roles=["speaking"])
        register_agent_hook(scoped_worker, roles=["worker"])
        register_agent_hook(global_hook)

        all_hooks = get_all_hooks()
        # Set membership: every registered hook is reported.
        assert set(map(id, all_hooks)) == {
            id(scoped_speaking),
            id(scoped_worker),
            id(global_hook),
        }
        # Globals come first.
        assert all_hooks[0] is global_hook

    def test_get_all_hooks_returns_independent_list(self):
        """Mutating the returned list must not affect the registry."""
        hook = _RecordingHook(name="h")
        register_agent_hook(hook, roles=["speaking"])
        result = get_all_hooks()
        result.clear()
        assert hooks_registry._REGISTRY["speaking"] == [hook]


# ---------------------------------------------------------------------------
# End-to-end: register hooks then drive a fake turn through the composite
# ---------------------------------------------------------------------------


class TestRegistryThenComposite:
    """Smoke test: register two hooks, fetch the composite, run a
    turn through it. Exercises the registry → composite → lifecycle
    path the run-loop will use in PR3."""

    def test_e2e_after_turn_injection(self, turn_result):
        a = _RecordingHook(name="a", after_turn_return="[hook: a] ok")
        b = _RecordingHook(name="b", after_turn_return="[hook: b] ok")
        register_agent_hook(a, roles=["speaking"])
        register_agent_hook(b)

        composite = hooks_for("speaking")
        injection = asyncio.run(composite.after_turn(turn_result))

        assert injection == "[hook: a] ok\n[hook: b] ok"
        assert a.calls["after_turn"] == [turn_result]
        assert b.calls["after_turn"] == [turn_result]
