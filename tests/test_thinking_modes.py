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
    # Adaptive thinking with summarized display is the
    # always-on viewer-friendly setting.
    assert spec.thinking == {"type": "adaptive", "display": "summarized"}


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


def test_active_mode_build_prompt_with_directive(tmp_path) -> None:
    """A directive.md on disk gets injected into the bootstrap
    template; missing directive still renders without errors."""
    directive = tmp_path / "directive.md"
    directive.write_text("Standing orders: be terse.")
    ctx = _ctx(tmp_path, directive_path=directive)
    out = asyncio.run(ActiveMode().build_prompt(ctx))
    assert "Standing orders: be terse." in out


def test_selector_returns_active_mode(tmp_path) -> None:
    """Phase 2 contract: selector always returns ActiveMode. Phase 3
    swaps in hour-based dispatch."""
    ctx = _ctx(tmp_path)
    mode = select_mode(now=ctx.now)
    assert isinstance(mode, ActiveMode)


def test_consolidation_stage_loads_stage_template(tmp_path) -> None:
    """Plan 03 Phase 5: ConsolidationStage loads its own template
    name (``thinking.wake.sleep.consolidate``) rather than the
    active template directly. The template currently extends
    thinking.wake.active so the rendered body equals the active
    body byte-for-byte — Phase 4 (deferred) is what differentiates."""
    from alice_thinking.modes import ConsolidationStage

    directive = tmp_path / "directive.md"
    directive.write_text("Standing orders: be terse.")
    ctx = _ctx(tmp_path, directive_path=directive)
    out = asyncio.run(ConsolidationStage().build_prompt(ctx))
    # Directive is included in both via the shared template body.
    assert "Standing orders: be terse." in out


def test_active_and_sleep_consolidate_render_identical_body(tmp_path) -> None:
    """Phase 5 stub-equivalence: the two templates should produce
    the same prompt today (different templates, same body via
    Jinja {% include %}). Phase 4 removes the include."""
    from alice_thinking.modes import ActiveMode, ConsolidationStage

    ctx = _ctx(tmp_path)
    a = asyncio.run(ActiveMode().build_prompt(ctx))
    s = asyncio.run(ConsolidationStage().build_prompt(ctx))
    assert a == s
