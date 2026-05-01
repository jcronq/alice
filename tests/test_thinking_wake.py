"""Plan 03 + Plan 05 Phase 4: thinking wake tests.

Two surfaces:

- :mod:`alice_thinking.wake` — argparse + config loading + context build.
- :mod:`alice_thinking.kernel_adapter` + :mod:`alice_thinking.modes` —
  protocol-driven mode dispatch.

Plan 03 Phase 1 split the original monolithic ``_run_wake`` into
``run_wake(ctx, mode, emitter)`` driving a :class:`Mode`'s
``build_prompt`` + ``kernel_spec``. Tests pin: the personae system
prompt threads through; the mode + spec are observable; placeholder
fallbacks still work.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

from alice_thinking import kernel_adapter as ka
from alice_thinking import wake as wake_module
from alice_thinking.modes import ActiveMode, WakeContext


class _CapturingEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _make_ctx(tmp_path: pathlib.Path, *, system_prompt: str = "") -> WakeContext:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from alice_core.config.personae import placeholder

    return WakeContext(
        mind_dir=tmp_path,
        cwd=tmp_path,
        now=datetime(2026, 4, 30, 14, 0, tzinfo=ZoneInfo("America/New_York")),
        personae=placeholder(),
        model="claude-sonnet-test",
        max_seconds=0,
        tools=[],
        system_prompt=system_prompt,
        quick=True,  # use the cheap quick prompt so we don't hit a real bootstrap
    )


def test_run_wake_passes_system_prompt_to_kernel(monkeypatch, tmp_path) -> None:
    """The mode's KernelSpec carries append_system_prompt; the kernel
    adapter does not modify it. Use ActiveMode + WakeContext for the
    end-to-end shape."""
    captured: dict[str, Any] = {}

    class _FakeResult:
        error = None

    class _FakeKernel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, spec: Any) -> _FakeResult:
            captured["spec"] = spec
            captured["prompt"] = prompt
            return _FakeResult()

    monkeypatch.setattr(ka, "AnthropicKernel", _FakeKernel)

    ctx = _make_ctx(tmp_path, system_prompt="You are Eve. Talk to Jordan.")
    rc = asyncio.run(ka.run_wake(ctx=ctx, mode=ActiveMode(), emitter=_CapturingEmitter()))
    assert rc == 0
    assert captured["spec"].append_system_prompt == "You are Eve. Talk to Jordan."


def test_run_wake_with_empty_system_prompt_passes_none(
    monkeypatch, tmp_path
) -> None:
    """Empty system_prompt → kernel sees None so it skips the
    system_prompt kwarg entirely (back-compat with callers that
    don't render personae)."""
    captured: dict[str, Any] = {}

    class _FakeResult:
        error = None

    class _FakeKernel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, spec: Any) -> _FakeResult:
            captured["spec"] = spec
            return _FakeResult()

    monkeypatch.setattr(ka, "AnthropicKernel", _FakeKernel)

    ctx = _make_ctx(tmp_path)
    asyncio.run(ka.run_wake(ctx=ctx, mode=ActiveMode(), emitter=_CapturingEmitter()))
    assert captured["spec"].append_system_prompt is None


def test_run_wake_emits_mode_in_envelope_events(monkeypatch, tmp_path) -> None:
    """Plan 03: every wake emits ``mode=<name>`` on wake_start +
    wake_end so the viewer / telemetry can attribute behavior to a
    specific mode without parsing prompt bodies."""

    class _FakeResult:
        error = None

    class _FakeKernel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self, prompt: str, spec: Any) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(ka, "AnthropicKernel", _FakeKernel)

    emitter = _CapturingEmitter()
    asyncio.run(
        ka.run_wake(ctx=_make_ctx(tmp_path), mode=ActiveMode(), emitter=emitter)
    )
    starts = [f for ev, f in emitter.events if ev == "wake_start"]
    ends = [f for ev, f in emitter.events if ev == "wake_end"]
    assert starts and starts[0]["mode"] == "active"
    assert ends and ends[0]["mode"] == "active"


def test_load_personae_falls_back_to_placeholder(tmp_path: pathlib.Path) -> None:
    """Missing personae.yml → placeholder (today's behaviour)."""
    p = wake_module._load_personae(tmp_path)
    assert p.agent.name == "Alice"
    assert p.user.name == "the operator"


def test_render_system_prompt_includes_agent_and_user(
    tmp_path: pathlib.Path,
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


def test_viewer_loads_model_config_at_startup(tmp_path: pathlib.Path) -> None:
    """Plan 06 Phase 4: viewer's create_app pulls mind/config/model.yml
    into app.state.model_config so narrative + run_summary can read
    backend + model from there in the future."""
    from alice_viewer.main import create_app
    from alice_viewer.settings import Paths

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model.yml").write_text(
        "viewer:\n  backend: subscription\n  model: claude-haiku-test\n"
    )
    paths = Paths(
        thinking_log=tmp_path / "t.log",
        speaking_log=tmp_path / "s.log",
        turn_log=tmp_path / "turn.jsonl",
        mind_dir=tmp_path,
        state_dir=tmp_path / "state",
    )
    app = create_app(paths)
    assert app.state.model_config.viewer.model == "claude-haiku-test"
    assert app.state.model_config.viewer.backend == "subscription"
