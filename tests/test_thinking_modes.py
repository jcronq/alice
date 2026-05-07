"""Plan 03 Phase 2: ``Mode`` protocol + ``ActiveMode``.

Phase 2 codifies today's single-mode behavior as ``ActiveMode``. The
selector returns it unconditionally; tests pin the kernel-spec
shape + the prompt-routing logic (quick → quick template, inline →
override, otherwise → bootstrap+directive).
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from alice_core.config.personae import placeholder
from alice_thinking.modes import ActiveMode, Mode, WakeContext
from alice_thinking.selector import select_mode


WAKE_TZ = ZoneInfo("America/New_York")


def _ctx(tmp_path: pathlib.Path, **kw) -> WakeContext:
    base = dict(
        mind_dir=tmp_path,
        cwd=tmp_path,
        now=datetime(2026, 4, 30, 14, 0, tzinfo=WAKE_TZ),
        personae=placeholder(),
        model="claude-sonnet-test",
        max_seconds=0,
        tools=["Bash", "Read"],
        system_prompt="",
        quick=False,
        inline_prompt=None,
        bootstrap_path=None,
        directive_path=None,
    )
    base.update(kw)
    return WakeContext(**base)


def test_active_mode_implements_protocol() -> None:
    """Pin: ``ActiveMode`` satisfies the :class:`Mode` Protocol —
    has the right name + the three required methods."""
    m: Mode = ActiveMode()
    assert m.name == "active"
    assert callable(m.kernel_spec)
    assert callable(m.build_prompt)
    assert callable(m.post_run)


def test_active_mode_kernel_spec_uses_context_fields(tmp_path) -> None:
    spec = ActiveMode().kernel_spec(_ctx(tmp_path))
    assert spec.model == "claude-sonnet-test"
    assert spec.allowed_tools == ["Bash", "Read"]
    assert spec.cwd == tmp_path
    # "medium" is the normalized ThinkingLevel that AnthropicKernel
    # maps to {type: adaptive, display: summarized}; PiKernel will map
    # it to its own --thinking flag. The Mode no longer constructs
    # SDK-specific dicts.
    assert spec.thinking == "medium"


def test_active_mode_kernel_spec_threads_system_prompt(tmp_path) -> None:
    ctx = _ctx(tmp_path, system_prompt="You are Eve.")
    spec = ActiveMode().kernel_spec(ctx)
    assert spec.append_system_prompt == "You are Eve."


def test_active_mode_kernel_spec_treats_empty_system_prompt_as_none(tmp_path):
    spec = ActiveMode().kernel_spec(_ctx(tmp_path, system_prompt=""))
    assert spec.append_system_prompt is None


def test_active_mode_build_prompt_quick(tmp_path) -> None:
    """``quick=True`` returns the cheap thinking.quick template."""
    ctx = _ctx(tmp_path, quick=True)
    out = asyncio.run(ActiveMode().build_prompt(ctx))
    # The thinking.quick template asks the model to reply verbatim.
    assert out.strip()
    assert "Reply" in out or "QUICK" in out or len(out) < 200


def test_active_mode_build_prompt_inline(tmp_path) -> None:
    """``inline_prompt`` overrides everything else."""
    ctx = _ctx(tmp_path, inline_prompt="custom prompt body")
    out = asyncio.run(ActiveMode().build_prompt(ctx))
    assert out == "custom prompt body"


def test_active_mode_build_prompt_includes_prelude_and_active_fragment(
    tmp_path,
) -> None:
    """Phase routing: ActiveMode delegates to PhaseRunner which
    composes ``timestamp_header + prelude + active.md``."""
    ctx = _ctx(tmp_path)
    out = asyncio.run(ActiveMode().build_prompt(ctx))
    # Identity from prelude.md + Step 0 from active.md should both appear.
    assert "Thinking Alice — wake" in out
    assert "Step 0 — active mode" in out


def test_selector_returns_active_mode(tmp_path) -> None:
    """Phase 2 contract: selector returns ActiveMode for the active
    hour window."""
    ctx = _ctx(tmp_path)
    mode = select_mode(now=ctx.now)
    assert isinstance(mode, ActiveMode)


def test_sleep_mode_loads_sleep_b_fragment(tmp_path) -> None:
    """SleepMode wraps PhaseRunner pinned to Phase.SLEEP_B. The
    composed prompt picks up sleep-b.md rather than active.md."""
    from alice_thinking.modes import SleepMode

    ctx = _ctx(tmp_path)
    out = asyncio.run(SleepMode().build_prompt(ctx))
    assert "Stage B (Consolidation)" in out
    # Should NOT carry the active-mode-only Step 2b context-summary §4 drain.
    assert "context-summary §4" not in out


def test_active_and_sleep_render_distinct_phase_bodies(tmp_path) -> None:
    """Phase routing intent: active and sleep wakes load different
    phase fragments. Both share the prelude verbatim."""
    from alice_thinking.modes import SleepMode

    ctx = _ctx(tmp_path)
    a = asyncio.run(ActiveMode().build_prompt(ctx))
    s = asyncio.run(SleepMode().build_prompt(ctx))
    assert a != s
    # Both pick up the same prelude.
    assert "Thinking Alice — wake" in a
    assert "Thinking Alice — wake" in s
