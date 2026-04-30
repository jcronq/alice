"""Phase F of plan 05: thinking wake threads personae into KernelSpec.

The wake is a one-shot CLI; we don't need an end-to-end smoke. The
load-bearing assertion is that the constructed ``KernelSpec`` carries
``append_system_prompt`` containing the rendered persona fragment, so
extending the kernel translates that to the SDK's ``system_prompt``
preset shape.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

from alice_thinking import wake as wake_module


class _CapturingEmitter:
    def emit(self, event: str, **fields: Any) -> None:  # pragma: no cover
        pass


def test_run_wake_passes_system_prompt_to_kernel(monkeypatch, tmp_path) -> None:
    """``_run_wake`` constructs a KernelSpec with append_system_prompt;
    a fake AgentKernel captures the spec it receives so we can pin
    the field made it through."""
    captured: dict[str, Any] = {}

    class _FakeResult:
        error = None

    class _FakeKernel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            pass

        async def run(self, prompt: str, spec: Any) -> _FakeResult:
            captured["spec"] = spec
            return _FakeResult()

    monkeypatch.setattr(wake_module, "AgentKernel", _FakeKernel)

    rc = asyncio.run(
        wake_module._run_wake(
            prompt_text="hi",
            model="claude-sonnet-test",
            tools=[],
            cwd=tmp_path,
            max_seconds=0,
            emitter=_CapturingEmitter(),
            system_prompt="You are Eve. Talk to Jordan.",
        )
    )
    assert rc == 0
    assert captured["spec"].append_system_prompt == "You are Eve. Talk to Jordan."


def test_run_wake_with_empty_system_prompt_passes_none(
    monkeypatch, tmp_path
) -> None:
    """An empty string is treated as ``None`` so the kernel skips
    setting ``system_prompt`` entirely (back-compat for callers that
    don't render personae)."""
    captured: dict[str, Any] = {}

    class _FakeResult:
        error = None

    class _FakeKernel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            pass

        async def run(self, prompt: str, spec: Any) -> _FakeResult:
            captured["spec"] = spec
            return _FakeResult()

    monkeypatch.setattr(wake_module, "AgentKernel", _FakeKernel)

    asyncio.run(
        wake_module._run_wake(
            prompt_text="hi",
            model="m",
            tools=[],
            cwd=tmp_path,
            max_seconds=0,
            emitter=_CapturingEmitter(),
        )
    )
    assert captured["spec"].append_system_prompt is None


def test_load_personae_falls_back_to_placeholder(tmp_path: pathlib.Path) -> None:
    """Missing personae.yml → placeholder (today's behaviour)."""
    p = wake_module._load_personae(tmp_path)
    assert p.agent.name == "Alice"
    assert p.user.name == "the operator"


def test_render_system_prompt_includes_agent_and_user(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """End-to-end: install loader + render system prompt with a
    fixture personae. Both names show up in the rendered string."""
    (tmp_path / "personae.yml").write_text(
        "agent:\n  name: Eve\nuser:\n  name: Jordan\n"
    )
    p = wake_module._load_personae(tmp_path)
    wake_module._install_prompt_loader(tmp_path, p)
    out = wake_module._render_system_prompt(p)
    assert "Eve" in out
    assert "Jordan" in out
    assert "Alice" not in out
    assert "the operator" not in out
