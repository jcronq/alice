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
        async def run(
            self, prompt: str, spec: Any, handlers: Any = None
        ) -> _FakeResult:
            captured["spec"] = spec
            captured["prompt"] = prompt
            return _FakeResult()

    monkeypatch.setattr(ka, "make_kernel", lambda *a, **kw: _FakeKernel())

    ctx = _make_ctx(tmp_path, system_prompt="You are Eve. Talk to Jordan.")
    rc = asyncio.run(
        ka.run_wake(ctx=ctx, mode=ActiveMode(), emitter=_CapturingEmitter())
    )
    assert rc == 0
    assert captured["spec"].append_system_prompt == "You are Eve. Talk to Jordan."


def test_run_wake_with_empty_system_prompt_passes_none(monkeypatch, tmp_path) -> None:
    """Empty system_prompt → kernel sees None so it skips the
    system_prompt kwarg entirely (back-compat with callers that
    don't render personae)."""
    captured: dict[str, Any] = {}

    class _FakeResult:
        error = None

    class _FakeKernel:
        async def run(
            self, prompt: str, spec: Any, handlers: Any = None
        ) -> _FakeResult:
            captured["spec"] = spec
            return _FakeResult()

    monkeypatch.setattr(ka, "make_kernel", lambda *a, **kw: _FakeKernel())

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
        async def run(
            self, prompt: str, spec: Any, handlers: Any = None
        ) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(ka, "make_kernel", lambda *a, **kw: _FakeKernel())

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


# Strix Halo Phase 2: per-stage backend override wiring.


def _stub_main_dependencies(monkeypatch, tmp_path, *, phase_value):
    """Stub everything in ``wake.main`` past argparse so the per-stage
    override branch is the only meaningful work the call performs.
    Returns a list that captures ``run_wake`` invocations and a list
    of emitted ``stage_backend_override`` events."""
    from alice_thinking import runtime as runtime_mod
    from alice_thinking import vault_state as vault_state_mod

    captured: dict[str, Any] = {"runs": [], "events": []}

    monkeypatch.setattr(wake_module, "build_vault_snapshot", lambda *a, **kw: object())
    monkeypatch.setattr(wake_module, "select_phase", lambda *a, **kw: phase_value)
    monkeypatch.setattr(wake_module, "detect_commission_notes", lambda *a, **kw: [])
    monkeypatch.setattr(wake_module, "detect_conflict_notes", lambda *a, **kw: [])
    monkeypatch.setattr(
        wake_module,
        "load_phase_config",
        lambda *a, **kw: runtime_mod.load_phase_config(tmp_path),
    )
    monkeypatch.setattr(vault_state_mod, "snapshot", lambda *a, **kw: None)

    monkeypatch.setattr(
        wake_module,
        "run_wake",
        lambda **kw: _async_zero(capture=captured, **kw),
    )

    # No-op for backoff state-dir writes.
    from alice_thinking import backoff as backoff_mod

    monkeypatch.setattr(backoff_mod, "read_interval", lambda *a, **kw: 60)
    monkeypatch.setattr(backoff_mod, "detect_did_work", lambda *a, **kw: False)
    monkeypatch.setattr(backoff_mod, "next_interval_seconds", lambda **kw: 60)
    monkeypatch.setattr(backoff_mod, "write_interval_atomic", lambda *a, **kw: None)

    # Force the EventLogger to capture in-memory rather than touch disk.
    real_logger_cls = wake_module.EventLogger

    class _MemoryLogger(real_logger_cls):
        def __init__(self, *a, **kw):
            self.events_in: list[tuple[str, dict[str, Any]]] = []
            captured["events_in"] = self.events_in

        def emit(self, event, **fields):
            self.events_in.append((event, fields))

    monkeypatch.setattr(wake_module, "EventLogger", _MemoryLogger)

    # Skip personae rendering side effects.
    from alice_core.config.personae import placeholder

    monkeypatch.setattr(wake_module, "_load_personae", lambda mind: placeholder())
    monkeypatch.setattr(wake_module, "_install_prompt_loader", lambda *a, **kw: None)
    monkeypatch.setattr(wake_module, "_render_system_prompt", lambda *a, **kw: "")

    # Skip skill-rendering side effects in _build_context.
    import alice_skills.registry as registry_mod
    import alice_skills.render as render_mod

    monkeypatch.setattr(
        registry_mod.SkillRegistry,
        "from_mind",
        classmethod(lambda cls, mind: object()),
    )
    monkeypatch.setattr(render_mod, "render_to_disk", lambda *a, **kw: None)

    return captured


def _async_zero(*, capture, **run_kwargs):
    """Replacement for ``asyncio.run(run_wake(...))``: capture the kwargs
    instead of dispatching, return 0 synchronously."""
    capture["runs"].append(
        {
            "ctx": run_kwargs.get("ctx"),
            "mode": run_kwargs.get("mode"),
            "backend": run_kwargs.get("backend"),
            "phase": run_kwargs.get("phase"),
        }
    )

    async def _zero():
        return 0

    return _zero()


def _write_model_yml(tmp_path: pathlib.Path, body: str) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model.yml").write_text(body)


def test_stage_override_applies_when_configured(monkeypatch, tmp_path) -> None:
    """Phase.SLEEP_D + thinking.stages.sleep_d in model.yml → run_wake
    receives the override BackendSpec and a stage_backend_override
    event is emitted."""
    from alice_thinking.phase import Phase

    _write_model_yml(
        tmp_path,
        """
        thinking:
          harness: pi-mono
          backend: pi
          model: openai-local/Qwen3.6-35B
          stages:
            sleep_d:
              backend: subscription
              harness: claude-code
              model: claude-sonnet-4-6
        """,
    )
    captured = _stub_main_dependencies(monkeypatch, tmp_path, phase_value=Phase.SLEEP_D)
    monkeypatch.setattr(
        "sys.argv",
        [
            "alice-think",
            "--mind",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--log",
            str(tmp_path / "thinking.log"),
        ],
    )

    rc = wake_module.main()
    assert rc == 0

    runs = captured["runs"]
    assert runs, "run_wake was never invoked"
    backend = runs[-1]["backend"]
    assert backend.backend == "subscription"
    assert backend.harness == "claude-code"
    assert backend.model == "claude-sonnet-4-6"

    events = captured["events_in"]
    overrides = [f for ev, f in events if ev == "stage_backend_override"]
    assert overrides, f"no stage_backend_override event in {events!r}"
    assert overrides[0]["phase"] == "sleep_d"
    assert overrides[0]["backend"] == "subscription"
    assert overrides[0]["model"] == "claude-sonnet-4-6"


def test_stage_override_absent_leaves_thinking_spec_unchanged(
    monkeypatch, tmp_path
) -> None:
    """No stages: block in model.yml → run_wake sees the base
    hemisphere spec; no override event is emitted."""
    from alice_thinking.phase import Phase

    _write_model_yml(
        tmp_path,
        """
        thinking:
          harness: pi-mono
          backend: pi
          model: openai-local/Qwen3.6-35B
        """,
    )
    captured = _stub_main_dependencies(monkeypatch, tmp_path, phase_value=Phase.SLEEP_D)
    monkeypatch.setattr(
        "sys.argv",
        [
            "alice-think",
            "--mind",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--log",
            str(tmp_path / "thinking.log"),
        ],
    )

    rc = wake_module.main()
    assert rc == 0

    backend = captured["runs"][-1]["backend"]
    assert backend.backend == "pi"
    assert backend.harness == "pi-mono"
    assert backend.model == "openai-local/Qwen3.6-35B"

    events = captured["events_in"]
    assert not [ev for ev, _ in events if ev == "stage_backend_override"]


def test_cli_backend_flag_wins_over_stage_override(monkeypatch, tmp_path) -> None:
    """``--backend=pi`` on the CLI suppresses the stage override path
    entirely so smoke tests stay deterministic."""
    from alice_thinking.phase import Phase

    _write_model_yml(
        tmp_path,
        """
        thinking:
          harness: pi-mono
          backend: pi
          model: openai-local/Qwen3.6-35B
          stages:
            sleep_d:
              backend: subscription
              harness: claude-code
              model: claude-sonnet-4-6
        """,
    )
    captured = _stub_main_dependencies(monkeypatch, tmp_path, phase_value=Phase.SLEEP_D)
    monkeypatch.setattr(
        "sys.argv",
        [
            "alice-think",
            "--mind",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--log",
            str(tmp_path / "thinking.log"),
            "--backend",
            "pi",
        ],
    )

    rc = wake_module.main()
    assert rc == 0

    backend = captured["runs"][-1]["backend"]
    assert backend.backend == "pi"
    # CLI flag preserves the base model + harness.
    assert backend.model == "openai-local/Qwen3.6-35B"

    events = captured["events_in"]
    assert not [ev for ev, _ in events if ev == "stage_backend_override"]


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
