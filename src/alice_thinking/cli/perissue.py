"""Per-issue thinking-agent entrypoint — sub-issue 3 of SM v2 pipeline revision.

Replaces the :mod:`forge.thinking_shim` placeholder shipped in #156.
Invoked by :func:`forge.dispatcher.spawn_thinking_agent` for each
``(sm:selected, art:code)`` issue. Reads the spawn dir's ``prompt.txt``,
resolves the configured :class:`alice_thinking.phase.Phase` from
frontmatter (or a ``--mode`` override), composes the prompt + kernel
spec via :class:`alice_thinking.runtime.PhaseRunner`, and drives the
kernel.

A single long-lived thinking-agent instance handles
``DESIGN → compaction → BUILD`` per design (the compaction step is a
separate runtime primitive, sub-issue 4). The entrypoint is invoked
once per phase: the dispatcher launches DESIGN at issue selection time
and BUILD after Speaking's approve + compaction.

``scripts/sm-thinking-perissue.py`` is a thin wrapper around
:func:`main` so the dispatcher can either call ``python -m
alice_thinking.cli.perissue ...`` (module path) or invoke the script
file directly. Both paths share the same parse-resolve-dispatch logic.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from core.config.personae import placeholder as placeholder_personae

from ..modes.base import WakeContext
from ..phase import Phase
from ..runtime import PhaseRunner


__all__ = [
    "main",
    "parse_frontmatter",
    "resolve_phase",
    "PHASE_BY_NAME",
    "PerIssueDispatchError",
]


# Wake clock — matches :data:`alice_thinking.wake.WAKE_TZ`. Per-issue
# spawns aren't tied to the local cadence (the dispatcher could be on
# a different timezone than the worker), but the timestamp header is
# rendered into the prompt for context.
_WAKE_TZ = ZoneInfo("America/New_York")


# Maps the ``phase:`` frontmatter value (and ``--mode`` CLI flag) onto
# the runtime enum. Kept narrow on purpose — only per-issue phases are
# valid entry points for this script.
PHASE_BY_NAME: dict[str, Phase] = {
    Phase.PER_ISSUE_DESIGN.value: Phase.PER_ISSUE_DESIGN,
    Phase.PER_ISSUE_BUILD.value: Phase.PER_ISSUE_BUILD,
}


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class PerIssueDispatchError(ValueError):
    """Raised when prompt.txt cannot be resolved to a valid Phase.

    The script catches this at the top of :func:`main`, logs the
    message, and exits non-zero — the dispatcher's stderr capture is
    the operator's surface.
    """


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---\\n…\\n---\\n`` frontmatter block off the head of ``text``.

    Returns ``(frontmatter_dict, body)``. Frontmatter values are
    stripped of surrounding quotes for ergonomics — the dispatcher
    may render ``phase: "per_issue_design"`` or ``phase: per_issue_design``;
    both work.

    On absent frontmatter, returns ``({}, text)``.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        value = raw_value.strip().strip('"').strip("'")
        fm[key.strip()] = value
    return fm, text[m.end():]


def resolve_phase(text: str, *, override: Optional[str] = None) -> Phase:
    """Return the :class:`Phase` carried by ``text`` (or by ``override``).

    Resolution order:

    1. The ``--mode`` CLI override (``override``), if set. Useful when
       the dispatcher reuses a prompt.txt across DESIGN → BUILD without
       rewriting the frontmatter.
    2. The ``phase:`` frontmatter value in ``text``.

    Raises :class:`PerIssueDispatchError` when:

    - Neither source supplies a phase.
    - The supplied value isn't a recognized per-issue phase
      (i.e., not in :data:`PHASE_BY_NAME`).
    """
    if override is not None:
        if override not in PHASE_BY_NAME:
            raise PerIssueDispatchError(
                f"unknown --mode value: {override!r} "
                f"(expected one of: {sorted(PHASE_BY_NAME)})"
            )
        return PHASE_BY_NAME[override]

    fm, _ = parse_frontmatter(text)
    raw = fm.get("phase")
    if not raw:
        raise PerIssueDispatchError(
            "prompt.txt has no `phase:` frontmatter and no --mode override"
        )
    if raw not in PHASE_BY_NAME:
        raise PerIssueDispatchError(
            f"unknown phase value in prompt.txt frontmatter: {raw!r} "
            f"(expected one of: {sorted(PHASE_BY_NAME)})"
        )
    return PHASE_BY_NAME[raw]


def _default_cwd() -> pathlib.Path:
    """Return the alice repo path, falling back to the script's cwd.

    BUILD-phase wakes need a cwd where ``git``/``gh`` operate against
    the alice repo; DESIGN-phase wakes only write inside the vault but
    a sensible default keeps both paths exercising the same plumbing.
    """
    home_alice = pathlib.Path.home() / "alice"
    if home_alice.is_dir():
        return home_alice
    return pathlib.Path.cwd()


def _default_mind() -> pathlib.Path:
    return pathlib.Path.home() / "alice-mind"


def _build_wake_context(
    *,
    spawn_dir: pathlib.Path,
    mind: pathlib.Path,
    cwd: pathlib.Path,
    model: str,
    max_seconds: int,
    now: Optional[datetime] = None,
) -> WakeContext:
    """Compose the minimal :class:`WakeContext` per-issue dispatch needs.

    Per-issue spawns don't render skill dirs (the agent reads the
    repo + vault directly) and don't carry a directive — the prompt
    fragment + injected content is the directive. ``add_dirs``
    includes the mind so the kernel grants the agent read access to
    the vault regardless of which cwd it ends up in.
    """
    return WakeContext(
        mind_dir=mind,
        cwd=cwd,
        now=now or datetime.now(_WAKE_TZ),
        personae=placeholder_personae(),
        model=model,
        max_seconds=max_seconds,
        tools=[],  # leave the per-phase default in charge
        system_prompt="",
        quick=False,
        inline_prompt=None,
        bootstrap_path=None,
        directive_path=None,
        add_dirs=[mind, spawn_dir],
    )


async def _drive_kernel(
    prompt_text: str,
    spec: Any,
    *,
    log: Callable[[str], None],
) -> int:
    """Default kernel-driver: run the spec via :func:`core.kernel.make_kernel`.

    Mirrors :func:`alice_thinking.kernel_adapter.run_wake` minus the
    event-log envelope — per-issue spawns capture their session via
    the SDK's session JSONL (the dispatcher pre-mints ``--session-id``)
    rather than a separate events.jsonl, so the kernel run can stay
    bare.

    Returns 0 on clean exit, 124 on kernel-reported timeout, 1
    otherwise. Replaceable in tests via the ``kernel_runner`` arg on
    :func:`main`.
    """
    from core.config.model import BackendSpec
    from core.events import EventLogger
    from core.kernel import make_kernel

    backend = BackendSpec(backend="subscription")
    emitter = EventLogger(pathlib.Path("/dev/null"))
    kernel = make_kernel(
        backend,
        emitter,
        correlation_id=f"perissue-{int(time.time())}",
        short_cap=4000,
    )
    try:
        result = await kernel.run(prompt_text, spec)
    except Exception as exc:  # noqa: BLE001
        log(f"[sm-thinking-perissue] kernel raised {type(exc).__name__}: {exc}")
        return 1
    if getattr(result, "error", None) == "timeout":
        log("[sm-thinking-perissue] kernel reported timeout")
        return 124
    return 0


def main(
    argv: Optional[Iterable[str]] = None,
    *,
    runner_factory: Callable[[], PhaseRunner] = PhaseRunner,
    context_builder: Callable[..., WakeContext] = _build_wake_context,
    kernel_runner: Callable[..., int] = lambda prompt_text, spec, *, log: asyncio.run(
        _drive_kernel(prompt_text, spec, log=log)
    ),
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> int:
    """Read prompt.txt, resolve phase, dispatch via PhaseRunner, run kernel.

    Returns a process exit code: ``0`` on clean dispatch + clean kernel
    exit, ``1`` on any pre-kernel failure (missing prompt.txt,
    malformed frontmatter, unknown phase) or a kernel-side exception,
    ``124`` on a kernel-reported timeout.

    ``runner_factory`` / ``context_builder`` / ``kernel_runner`` are
    injection points used by the test harness — production callers
    don't pass them.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Per-issue thinking-agent entrypoint. Reads <spawn-dir>/prompt.txt, "
            "resolves the configured Phase (PER_ISSUE_DESIGN | PER_ISSUE_BUILD), "
            "and drives the kernel through alice_thinking.runtime.PhaseRunner."
        )
    )
    parser.add_argument(
        "--spawn-dir",
        required=True,
        help="per-issue spawn dir containing prompt.txt",
    )
    parser.add_argument(
        "--session-id",
        required=False,
        default=None,
        help="claude-agent-sdk session id pre-minted by the dispatcher (informational)",
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=tuple(PHASE_BY_NAME),
        help=(
            "phase override (defaults to the prompt.txt `phase:` frontmatter). "
            "Useful when the dispatcher recomposes the prompt for the BUILD "
            "phase without rewriting frontmatter."
        ),
    )
    parser.add_argument(
        "--mind",
        default=None,
        help="alice-mind path (default: ~/alice-mind)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="kernel cwd (default: ~/alice if it exists, else current dir)",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="model id passed to the kernel (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="kernel max_seconds (0 = unbounded; per-issue work is long-lived)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve phase + compose prompt; skip kernel invocation. For tests.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        log(f"[sm-thinking-perissue] missing prompt.txt at {prompt_path}")
        return 1

    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"[sm-thinking-perissue] failed to read {prompt_path}: {exc}")
        return 1

    try:
        phase = resolve_phase(raw, override=args.mode)
    except PerIssueDispatchError as exc:
        log(f"[sm-thinking-perissue] {exc}")
        return 1

    _, body = parse_frontmatter(raw)
    body = body.strip()
    if not body:
        log(
            f"[sm-thinking-perissue] prompt.txt at {prompt_path} has no body "
            "after frontmatter — nothing to dispatch"
        )
        return 1

    runner = runner_factory()
    ctx = context_builder(
        spawn_dir=spawn_dir,
        mind=pathlib.Path(args.mind) if args.mind else _default_mind(),
        cwd=pathlib.Path(args.cwd) if args.cwd else _default_cwd(),
        model=args.model,
        max_seconds=args.max_seconds,
    )
    prompt_text, spec = runner.run(phase, ctx, injected_content=body)

    log(
        f"[sm-thinking-perissue] resolved phase={phase.value} "
        f"prompt_chars={len(prompt_text)} session_id={args.session_id or '(none)'}"
    )

    if args.dry_run:
        return 0

    return kernel_runner(prompt_text, spec, log=log)


if __name__ == "__main__":
    sys.exit(main())
