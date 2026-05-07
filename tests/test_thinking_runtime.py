"""Tests for ``alice_thinking.runtime`` — :class:`PhaseRunner`.

Pin: prompt + KernelSpec composition for each phase, ``--quick``
and inline-prompt overrides, post-wake hook is a no-op stub.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from alice_core.config.personae import placeholder
from alice_thinking.modes import WakeContext
from alice_thinking.phase import Phase, PhaseConfig
from alice_thinking.runtime import PhaseRunner, load_phase_config


WAKE_TZ = ZoneInfo("America/New_York")


def _ctx(tmp_path: pathlib.Path, **kw) -> WakeContext:
    base = dict(
        mind_dir=tmp_path,
        cwd=tmp_path,
        now=datetime(2026, 5, 7, 14, 0, tzinfo=WAKE_TZ),
        personae=placeholder(),
        model="claude-sonnet-test",
        max_seconds=0,
        tools=["Bash", "Read"],
        system_prompt="You are Eve.",
        quick=False,
        inline_prompt=None,
        bootstrap_path=None,
        directive_path=None,
    )
    base.update(kw)
    return WakeContext(**base)


def test_runner_returns_prompt_and_spec(tmp_path) -> None:
    runner = PhaseRunner()
    prompt, spec = runner.run(Phase.ACTIVE, _ctx(tmp_path))
    assert isinstance(prompt, str) and prompt.strip()
    assert spec.model == "claude-sonnet-test"
    assert spec.allowed_tools == ["Bash", "Read"]
    assert spec.append_system_prompt == "You are Eve."


def test_runner_quick_uses_quick_template(tmp_path) -> None:
    runner = PhaseRunner()
    prompt, _ = runner.run(Phase.ACTIVE, _ctx(tmp_path, quick=True))
    # Quick template is short and doesn't carry the prelude.
    assert len(prompt) < 500
    assert "Thinking Alice — wake" not in prompt


def test_runner_inline_prompt_wins_over_phase_compose(tmp_path) -> None:
    runner = PhaseRunner()
    prompt, _ = runner.run(
        Phase.ACTIVE, _ctx(tmp_path, inline_prompt="just this")
    )
    assert prompt == "just this"


def test_runner_phase_active_vs_sleep_have_distinct_bodies(tmp_path) -> None:
    runner = PhaseRunner()
    a, _ = runner.run(Phase.ACTIVE, _ctx(tmp_path))
    s, _ = runner.run(Phase.SLEEP_B, _ctx(tmp_path))
    assert a != s
    assert "Step 0 — active mode" in a
    assert "Stage B (Consolidation)" in s


def test_runner_threads_injected_content(tmp_path) -> None:
    """Forward-compat: the ``injected_content`` kwarg appears between
    the prelude and the phase fragment."""
    runner = PhaseRunner()
    a, _ = runner.run(
        Phase.ACTIVE,
        _ctx(tmp_path),
        injected_content="STM EXCERPT GOES HERE",
    )
    assert "STM EXCERPT GOES HERE" in a


def test_runner_post_wake_hook_is_noop(tmp_path) -> None:
    runner = PhaseRunner()
    # Returns None and doesn't raise — hook is reserved for STM/LTM.
    out = asyncio.run(runner._run_post_wake_hooks(_ctx(tmp_path)))
    assert out is None


def test_runner_kernel_spec_uses_phase_runner_inputs(tmp_path) -> None:
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path, max_seconds=120))
    assert spec.max_seconds == 120
    # Phase 0/1 keep ``ctx.tools`` as-is — Phase 2 swaps in per-phase
    # defaults. This test pins today's behavior so a regression jumps
    # out when Phase 2 lands.
    assert spec.allowed_tools == ["Bash", "Read"]


# ---------------------------------------------------------------------------
# load_phase_config
# ---------------------------------------------------------------------------


def test_load_phase_config_defaults_when_no_file(tmp_path) -> None:
    cfg = load_phase_config(tmp_path)
    assert cfg == PhaseConfig()


def test_load_phase_config_picks_up_overrides(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alice.config.json").write_text(
        '{"thinking": {"phase_routing": '
        '{"enable_full_sleep_dispatch": true, "consecutive_b_threshold": 4}'
        "}}"
    )
    cfg = load_phase_config(tmp_path)
    assert cfg.enable_full_sleep_dispatch is True
    assert cfg.consecutive_b_threshold == 4
    # Untouched defaults survive.
    assert cfg.recent_research_window_days == 7


def test_load_phase_config_ignores_unknown_keys(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alice.config.json").write_text(
        '{"thinking": {"phase_routing": {"unknown_key": 99}}}'
    )
    # Should not raise — unknown keys are dropped.
    cfg = load_phase_config(tmp_path)
    assert cfg == PhaseConfig()
