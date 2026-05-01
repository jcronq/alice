"""Kernel adapter — drives one wake through a :class:`Kernel` impl.

Plan 03 Phase 1 extracts the kernel-driving logic from ``wake.py``
into its own module. The same envelope (``wake_start`` /
``wake_end`` events, exception → exit code mapping, timeout → 124)
applies to every mode.

The mode picks the spec; this module runs it. Modes can do
post-run work via :meth:`Mode.post_run`. The kernel impl is chosen
by :func:`alice_core.kernel.make_kernel` based on
``model_config.thinking.backend`` so thinking can route through
AnthropicKernel (claude_agent_sdk) or PiKernel (pi-coding-agent
subprocess) without any code change here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from alice_core.kernel import make_kernel


if TYPE_CHECKING:
    from alice_core.config.model import BackendSpec
    from alice_core.events import EventLogger

    from .modes.base import Mode, WakeContext


async def run_wake(
    *,
    ctx: "WakeContext",
    mode: "Mode",
    emitter: "EventLogger",
    backend: Optional["BackendSpec"] = None,
) -> int:
    """Drive one wake through the kernel chosen for ``backend``.

    Emits ``wake_start`` (with the chosen mode + model + tools)
    around the kernel.run() call, then ``wake_end`` on clean finish.
    Returns a process-friendly exit code: 0 on clean, 124 on timeout
    (matches the GNU timeout convention), 1 otherwise.

    ``backend`` defaults to a subscription :class:`BackendSpec` so
    legacy callers (test fixtures, ad-hoc invocations) keep working
    without explicit threading.
    """
    if backend is None:
        from alice_core.config.model import BackendSpec
        backend = BackendSpec(backend="subscription")

    wake_id = f"wake-{int(time.time())}"
    prompt_text = await mode.build_prompt(ctx)
    spec = mode.kernel_spec(ctx)

    emitter.emit(
        "wake_start",
        wake_id=wake_id,
        mode=mode.name,
        model=spec.model,
        max_seconds=spec.max_seconds,
        tools=list(spec.allowed_tools),
        cwd=str(ctx.cwd),
        prompt_chars=len(prompt_text),
    )

    kernel = make_kernel(
        backend,
        emitter,
        correlation_id=wake_id,
        # Cap is generous — Sonnet's reasoning blocks are often >1k chars
        # and a wake's whole value is the trace (the operator browses
        # thoughts in the viewer, not just the resulting wiki edits).
        short_cap=4000,
    )

    try:
        result = await kernel.run(prompt_text, spec)
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "exception",
            wake_id=wake_id,
            mode=mode.name,
            type=type(exc).__name__,
            message=str(exc),
        )
        return 1

    if result.error == "timeout":
        # Kernel already emitted the ``timeout`` event; surface exit code.
        return 124

    await mode.post_run(ctx, result)
    emitter.emit("wake_end", wake_id=wake_id, mode=mode.name)
    return 0
