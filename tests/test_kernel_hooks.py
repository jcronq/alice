"""Unit tests for the agent-level hook surface.

PR1 ships the shape only — no registry, no run-loop wiring, no
concrete consumers. These tests pin the contract:

- :class:`BaseAgentHook` no-ops on every method without raising.
- A subclass that overrides one method returns the expected value
  while the other three stay no-ops.
- :class:`LoggingReporter` routes through both the stdlib logger
  (captured via :func:`caplog`) and an attached
  :class:`~core.events.EventEmitter`.
- The hook methods are genuinely awaitable — coroutine objects, not
  sync callables that happen to share the signature.

Stays free of sibling-package imports so the test runs even if
``alice_speaking`` or ``kernels.*`` can't be loaded.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from core.events import CapturingEmitter
from core.kernel import (
    AgentHook,
    BaseAgentHook,
    KernelResult,
    KernelSpec,
    LoggingReporter,
    Reporter,
    ToolResult,
    ToolUse,
    TurnResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reporter() -> LoggingReporter:
    """Bare reporter with no event emitter — most hook tests only
    care that the reporter slot is populated, not that it routes."""
    return LoggingReporter()


@pytest.fixture
def spec() -> KernelSpec:
    return KernelSpec(model="claude-sonnet-4-5", allowed_tools=["Read"])


@pytest.fixture
def turn_result() -> TurnResult:
    return TurnResult(
        text="hi",
        tool_uses=[
            ToolUse(name="Edit", input={"file_path": "/tmp/x.md"}, id="tu_1")
        ],
        tool_results=[
            ToolResult(tool_use_id="tu_1", content="ok", is_error=False)
        ],
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
# BaseAgentHook — no-op defaults
# ---------------------------------------------------------------------------


class TestBaseAgentHookNoOps:
    """The base class must be instantiable with just a reporter and
    every lifecycle method must return ``None`` without raising."""

    def test_constructor_stores_reporter(self, reporter):
        hook = BaseAgentHook(reporter)
        assert hook.reporter is reporter

    def test_satisfies_agent_hook_protocol(self, reporter):
        # AgentHook is runtime_checkable — the base class must pass.
        hook = BaseAgentHook(reporter)
        assert isinstance(hook, AgentHook)

    def test_before_agent_start_returns_none(self, reporter, spec):
        hook = BaseAgentHook(reporter)
        assert asyncio.run(hook.before_agent_start(spec)) is None

    def test_before_turn_returns_none(self, reporter):
        hook = BaseAgentHook(reporter)
        assert asyncio.run(hook.before_turn([])) is None

    def test_after_turn_returns_none(self, reporter, turn_result):
        hook = BaseAgentHook(reporter)
        assert asyncio.run(hook.after_turn(turn_result)) is None

    def test_after_agent_end_returns_none(self, reporter, kernel_result):
        hook = BaseAgentHook(reporter)
        assert asyncio.run(hook.after_agent_end(kernel_result)) is None


# ---------------------------------------------------------------------------
# Subclass override behavior
# ---------------------------------------------------------------------------


class _CountingMarkdownHook(BaseAgentHook):
    """Toy hook: count edits to .md files and inject a correction
    string mirroring the production validator's shape. Other methods
    inherit the base no-op behavior — this proves consumers can
    override one method without touching the other three."""

    def __init__(self, reporter):
        super().__init__(reporter)
        self.edits_seen = 0

    async def after_turn(self, turn_result: TurnResult):
        md_paths = [
            tu.input["file_path"]
            for tu in turn_result.tool_uses
            if tu.name in {"Edit", "Write"}
            and isinstance(tu.input, dict)
            and tu.input.get("file_path", "").endswith(".md")
        ]
        self.edits_seen += len(md_paths)
        if not md_paths:
            return None
        return "[validation] reviewed: " + ", ".join(md_paths)


class TestSubclassOverride:
    def test_override_produces_expected_injection(self, reporter, turn_result):
        hook = _CountingMarkdownHook(reporter)
        injection = asyncio.run(hook.after_turn(turn_result))
        assert injection == "[validation] reviewed: /tmp/x.md"
        assert hook.edits_seen == 1
        # Convention check: validation hooks lead with [validation].
        assert injection.startswith("[validation]")

    def test_override_returns_none_when_nothing_matches(self, reporter):
        empty = TurnResult(text="", tool_uses=[], tool_results=[], stop_reason="end_turn")
        hook = _CountingMarkdownHook(reporter)
        assert asyncio.run(hook.after_turn(empty)) is None
        assert hook.edits_seen == 0

    def test_unoverridden_methods_still_noop(self, reporter, spec, kernel_result):
        """Override of after_turn must not break the inherited no-ops."""
        hook = _CountingMarkdownHook(reporter)
        assert asyncio.run(hook.before_agent_start(spec)) is None
        assert asyncio.run(hook.before_turn([])) is None
        assert asyncio.run(hook.after_agent_end(kernel_result)) is None


# ---------------------------------------------------------------------------
# LoggingReporter
# ---------------------------------------------------------------------------


class TestLoggingReporter:
    def test_satisfies_reporter_protocol(self):
        assert isinstance(LoggingReporter(), Reporter)

    def test_warn_logs_at_warning_level(self, caplog):
        rep = LoggingReporter()
        with caplog.at_level(logging.WARNING, logger="core.kernel.hooks"):
            rep.warn("frontmatter invalid at line 3")
        assert any(
            "frontmatter invalid at line 3" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_error_logs_at_error_level(self, caplog):
        rep = LoggingReporter()
        with caplog.at_level(logging.ERROR, logger="core.kernel.hooks"):
            rep.error("kaboom")
        assert any(
            "kaboom" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )

    def test_emit_event_routes_through_emitter(self):
        emitter = CapturingEmitter()
        rep = LoggingReporter(emitter)
        rep.emit_event("md_validation_failed", path="/tmp/x.md", line=3)
        recorded = emitter.of_kind("md_validation_failed")
        assert len(recorded) == 1
        assert recorded[0]["path"] == "/tmp/x.md"
        assert recorded[0]["line"] == 3

    def test_warn_also_emits_event_when_emitter_present(self):
        emitter = CapturingEmitter()
        rep = LoggingReporter(emitter)
        rep.warn("flagged")
        assert emitter.of_kind("hook_warn")[0]["msg"] == "flagged"

    def test_emit_event_no_op_without_emitter(self):
        # Doesn't raise; nothing to capture. Pure smoke test of the
        # None-guard path.
        LoggingReporter().emit_event("nothing_here", x=1)

    def test_custom_logger_namespace(self, caplog):
        logger = logging.getLogger("test.hooks.custom")
        rep = LoggingReporter(logger=logger)
        with caplog.at_level(logging.WARNING, logger="test.hooks.custom"):
            rep.warn("scoped")
        assert any(
            r.name == "test.hooks.custom" and "scoped" in r.getMessage()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Async correctness
# ---------------------------------------------------------------------------


class TestAsyncCorrectness:
    """Hook methods must be genuine coroutine functions so the run
    loop can ``await`` them. A sync def with the same signature
    would type-check but fail at runtime."""

    @pytest.fixture
    def hook(self, reporter):
        return BaseAgentHook(reporter)

    def test_before_agent_start_is_coroutine_function(self, hook):
        assert inspect.iscoroutinefunction(hook.before_agent_start)

    def test_before_turn_is_coroutine_function(self, hook):
        assert inspect.iscoroutinefunction(hook.before_turn)

    def test_after_turn_is_coroutine_function(self, hook):
        assert inspect.iscoroutinefunction(hook.after_turn)

    def test_after_agent_end_is_coroutine_function(self, hook):
        assert inspect.iscoroutinefunction(hook.after_agent_end)

    @pytest.mark.asyncio
    async def test_after_turn_awaitable(self, hook, turn_result):
        # The actual run-loop wiring lives in PR3 — for PR1 we just
        # confirm an `await` against the surface works end-to-end.
        result = await hook.after_turn(turn_result)
        assert result is None


# ---------------------------------------------------------------------------
# TurnResult shape
# ---------------------------------------------------------------------------


class TestTurnResultShape:
    """Pin the TurnResult fields so PR3's run-loop wiring has a
    stable target to populate."""

    def test_defaults(self):
        tr = TurnResult(text="x")
        assert tr.text == "x"
        assert tr.tool_uses == []
        assert tr.tool_results == []
        assert tr.stop_reason == ""

    def test_carries_tool_uses_and_results(self, turn_result):
        assert len(turn_result.tool_uses) == 1
        assert turn_result.tool_uses[0].name == "Edit"
        assert len(turn_result.tool_results) == 1
        assert turn_result.tool_results[0].tool_use_id == "tu_1"
        assert turn_result.stop_reason == "end_turn"
