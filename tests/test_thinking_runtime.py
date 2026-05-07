"""Tests for ``alice_thinking.runtime`` — :class:`PhaseRunner`.

Pin: prompt + KernelSpec composition for each phase, ``--quick``
and inline-prompt overrides, post-wake hook is a no-op stub, and
the Phase 2 wiring of per-phase tool allowlists + ``max_seconds``
budgets.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from alice_core.config.personae import placeholder
from alice_thinking.modes import WakeContext
from alice_thinking.phase import Phase, PhaseConfig
from alice_thinking.runtime import (
    PhaseRunner,
    load_phase_config,
    phase_default_allowed_tools,
    phase_default_max_seconds,
)


WAKE_TZ = ZoneInfo("America/New_York")


def _ctx(tmp_path: pathlib.Path, **kw) -> WakeContext:
    base = dict(
        mind_dir=tmp_path,
        cwd=tmp_path,
        now=datetime(2026, 5, 7, 14, 0, tzinfo=WAKE_TZ),
        personae=placeholder(),
        model="claude-sonnet-test",
        max_seconds=0,
        # Empty tools — leaves PhaseRunner's per-phase default in charge.
        tools=[],
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
    # Empty ctx.tools → per-phase default.
    assert spec.allowed_tools == phase_default_allowed_tools(Phase.ACTIVE)
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


def test_runner_kernel_spec_honors_ctx_max_seconds_override(tmp_path) -> None:
    """Phase 2: ``ctx.max_seconds`` (CLI ``--max-seconds`` / legacy
    ``thinking.max_wake_seconds``) overrides the per-phase default."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path, max_seconds=120))
    assert spec.max_seconds == 120


def test_runner_kernel_spec_honors_ctx_tools_override(tmp_path) -> None:
    """Phase 2: a non-empty ``ctx.tools`` (CLI ``--tools`` / legacy
    ``thinking.allowed_tools``) overrides the per-phase default."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(
        Phase.SLEEP_C, _ctx(tmp_path, tools=["Bash", "Read"])
    )
    assert spec.allowed_tools == ["Bash", "Read"]


# ---------------------------------------------------------------------------
# Phase 2 — per-phase tool allowlists + max_seconds wired into KernelSpec
# ---------------------------------------------------------------------------


def test_phase2_active_allowlist_includes_web_tools(tmp_path) -> None:
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.ACTIVE, _ctx(tmp_path))
    assert "WebFetch" in spec.allowed_tools
    assert "WebSearch" in spec.allowed_tools
    assert "mcp__alice__send_message" in spec.allowed_tools


def test_phase2_sleep_b_allowlist_includes_web_tools(tmp_path) -> None:
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.SLEEP_B, _ctx(tmp_path))
    assert "WebFetch" in spec.allowed_tools
    assert "WebSearch" in spec.allowed_tools


def test_phase2_sleep_c_allowlist_excludes_web_tools(tmp_path) -> None:
    """Stage C is pure vault maintenance — no Web*."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path))
    assert "WebFetch" not in spec.allowed_tools
    assert "WebSearch" not in spec.allowed_tools
    # File ops + send_message stay.
    for t in ("Bash", "Read", "Write", "Edit", "Grep", "Glob", "mcp__alice__send_message"):
        assert t in spec.allowed_tools


def test_phase2_sleep_d_allowlist_excludes_web_tools(tmp_path) -> None:
    """Stage D synthesizes from the existing corpus — no Web*."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.SLEEP_D, _ctx(tmp_path))
    assert "WebFetch" not in spec.allowed_tools
    assert "WebSearch" not in spec.allowed_tools


def test_phase2_design_commission_allowlist_excludes_web_and_mcp(tmp_path) -> None:
    """Design work is repo-only; no Web*, no MCP."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.DESIGN_COMMISSION, _ctx(tmp_path))
    assert "WebFetch" not in spec.allowed_tools
    assert "WebSearch" not in spec.allowed_tools
    assert not any(t.startswith("mcp__") for t in spec.allowed_tools)
    for t in ("Bash", "Read", "Write", "Edit", "Grep", "Glob"):
        assert t in spec.allowed_tools


def test_phase2_quick_allowlist_is_empty(tmp_path) -> None:
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.QUICK, _ctx(tmp_path))
    assert spec.allowed_tools == []
    assert spec.max_seconds == 30


def test_phase2_quick_via_ctx_quick_flag(tmp_path) -> None:
    """``ctx.quick=True`` short-circuits to the QUICK shape regardless
    of the requested phase. wake.py sets this for ``--quick``."""
    runner = PhaseRunner()
    spec = runner.kernel_spec(Phase.ACTIVE, _ctx(tmp_path, quick=True))
    assert spec.allowed_tools == []
    assert spec.max_seconds == 30


def test_phase2_max_seconds_defaults_unbounded_for_real_wakes(tmp_path) -> None:
    """Active / Sleep B / C / D default to 0 (unbounded) when nothing
    overrides them. Quick stays at 30s."""
    runner = PhaseRunner()
    for phase in (Phase.ACTIVE, Phase.SLEEP_B, Phase.SLEEP_C, Phase.SLEEP_D):
        spec = runner.kernel_spec(phase, _ctx(tmp_path))
        assert spec.max_seconds == 0
    assert phase_default_max_seconds(Phase.QUICK) == 30


def test_phase2_config_max_seconds_overrides_phase_default(tmp_path) -> None:
    """``PhaseConfig.max_seconds`` (config) wins over the phase default."""
    cfg = PhaseConfig(max_seconds=900)
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path))
    assert spec.max_seconds == 900


def test_phase2_config_max_seconds_overrides_ctx(tmp_path) -> None:
    """Config wins over ctx (CLI flags) — config is the explicit pin."""
    cfg = PhaseConfig(max_seconds=900)
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path, max_seconds=120))
    assert spec.max_seconds == 900


def test_phase2_config_allowed_tools_overrides_phase_default(tmp_path) -> None:
    cfg = PhaseConfig(allowed_tools=["Read"])
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(Phase.ACTIVE, _ctx(tmp_path))
    assert spec.allowed_tools == ["Read"]


def test_phase2_config_allowed_tools_overrides_ctx(tmp_path) -> None:
    cfg = PhaseConfig(allowed_tools=["Read"])
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(
        Phase.ACTIVE, _ctx(tmp_path, tools=["Bash", "Edit"])
    )
    assert spec.allowed_tools == ["Read"]


def test_phase2_zero_max_seconds_in_config_falls_through(tmp_path) -> None:
    """``max_seconds=0`` in config means "fall through to ctx / phase default."
    Without it we couldn't keep the dataclass default frozen at 0 while
    still letting ctx and phase-default kick in."""
    cfg = PhaseConfig(max_seconds=0)
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(Phase.SLEEP_C, _ctx(tmp_path, max_seconds=120))
    assert spec.max_seconds == 120


def test_phase2_none_allowed_tools_in_config_falls_through(tmp_path) -> None:
    cfg = PhaseConfig(allowed_tools=None)
    runner = PhaseRunner(config=cfg)
    spec = runner.kernel_spec(
        Phase.ACTIVE, _ctx(tmp_path, tools=["Bash"])
    )
    # ctx.tools wins over the phase default when config is None.
    assert spec.allowed_tools == ["Bash"]


def test_phase2_kernel_spec_uses_phase_default_when_no_overrides(tmp_path) -> None:
    """End-to-end: empty ctx.tools, default config → phase default."""
    runner = PhaseRunner()
    for phase in (Phase.ACTIVE, Phase.SLEEP_B, Phase.SLEEP_C, Phase.SLEEP_D):
        spec = runner.kernel_spec(phase, _ctx(tmp_path))
        assert spec.allowed_tools == phase_default_allowed_tools(phase)


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


def test_load_phase_config_top_level_kill_switch(tmp_path) -> None:
    """Phase 3 kill-switch lives at ``thinking.enable_full_sleep_dispatch``
    so Jason can flip it without nesting under ``phase_routing``."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alice.config.json").write_text(
        '{"thinking": {"enable_full_sleep_dispatch": false}}'
    )
    cfg = load_phase_config(tmp_path)
    assert cfg.enable_full_sleep_dispatch is False


def test_load_phase_config_top_level_max_wake_seconds(tmp_path) -> None:
    """``thinking.max_wake_seconds`` (the legacy CLI knob's home)
    feeds ``PhaseConfig.max_seconds`` so Phase 2 honors it
    everywhere."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alice.config.json").write_text(
        '{"thinking": {"max_wake_seconds": 1800}}'
    )
    cfg = load_phase_config(tmp_path)
    assert cfg.max_seconds == 1800


def test_load_phase_config_phase_routing_block_wins(tmp_path) -> None:
    """If both the top-level and phase_routing blocks declare a key,
    the explicit ``phase_routing`` block wins."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alice.config.json").write_text(
        '{"thinking": {"enable_full_sleep_dispatch": true, '
        '"phase_routing": {"enable_full_sleep_dispatch": false}}}'
    )
    cfg = load_phase_config(tmp_path)
    assert cfg.enable_full_sleep_dispatch is False


def test_load_phase_config_default_is_full_dispatch_on(tmp_path) -> None:
    """Phase 3 ships with full dispatch on by default."""
    cfg = load_phase_config(tmp_path)
    assert cfg.enable_full_sleep_dispatch is True
