"""Thinking — one wake, driven through the agent kernel.

Cron-style entry point invoked by ``/usr/local/bin/alice-think``
from the s6 supervisor. Each invocation:

1. Loads auth into the environment (mind/config/model.yml's
   thinking.backend → ``ensure_auth_env(mode_hint=...)``).
2. Applies ``thinking.*`` overrides from ``alice.config.json``.
3. Loads personae + installs a mind-aware prompt loader.
4. Builds a :class:`WakeContext` and asks the selector for the
   :class:`Mode` (today: always ``ActiveMode``; Phase 3 introduces
   hour-based dispatch).
5. Runs the wake via :func:`alice_thinking.kernel_adapter.run_wake`.

No handlers are composed — thinking doesn't persist sessions across
wakes (each is fresh) and doesn't compact (Sonnet stays small by
the "one small pass per wake" ethos). The SDK's structured events
flow straight to the log for the alice-viewer to tail.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime

from alice_core.config.auth import ensure_auth_env
from alice_core.config.model import load as load_model_config
from alice_core.config.personae import (
    PersonaeError,
    load as load_personae,
    placeholder as placeholder_personae,
)
from alice_core.events import EventLogger

from ._prompt_assembly import WAKE_TZ, wake_timestamp_header
from .kernel_adapter import run_wake
from .modes.base import WakeContext
from .modes.sleep import SleepMode
from .selector import select_mode
from .vault_state import snapshot as snapshot_vault


DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_DIRECTIVE = DEFAULT_MIND / "inner" / "directive.md"
DEFAULT_LOG = pathlib.Path("/state/worker/thinking.log")
DEFAULT_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_SECONDS = 0  # 0 == no timeout. Thinking runs as long as it needs.
QUICK_MAX_SECONDS = 30


def _load_token() -> None:
    """Resolve auth from alice.env + os.environ (no model.yml hint).

    Plan 06 Phase 4 superseded direct callers (``main()`` reads
    ``mind/config/model.yml`` and passes a ``mode_hint``). Kept as
    a back-compat shim for any external scripts that import it.
    """
    ensure_auth_env()


def _apply_config_overrides(args: argparse.Namespace) -> None:
    """Pull thinking.* overrides out of alice.config.json if they exist.

    Only overrides values the user didn't explicitly pass on the CLI:
    CLI args > config file > module defaults.
    """
    cfg_path = pathlib.Path(args.mind) / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        return
    think = (cfg or {}).get("thinking") or {}
    if args.model == DEFAULT_MODEL and "model" in think:
        args.model = think["model"]
    if args.max_seconds == DEFAULT_MAX_SECONDS and "max_wake_seconds" in think:
        args.max_seconds = int(think["max_wake_seconds"])
    if args.tools == DEFAULT_TOOLS and "allowed_tools" in think:
        args.tools = ",".join(think["allowed_tools"])


def _load_personae(mind: pathlib.Path):
    """Load mind/personae.yml; placeholder on missing file; raise on
    malformed. The wake fails loudly on a malformed file rather than
    running with degraded identity.
    """
    try:
        return load_personae(mind)
    except FileNotFoundError:
        return placeholder_personae()
    except PersonaeError:
        print(
            f"thinking: personae.yml at {mind / 'personae.yml'} is invalid",
            file=sys.stderr,
        )
        raise


def _install_prompt_loader(mind: pathlib.Path, personae) -> None:
    """Wire a mind-aware PromptLoader as the alice_prompts singleton
    so the wake template's ``{{agent.name}}`` substitutions resolve
    and any per-mind override at
    ``.alice/prompts/thinking/wake.active.md.j2`` applies.
    """
    import alice_prompts as _prompts
    from alice_prompts import DEFAULTS_DIR, PromptLoader

    loader = PromptLoader(
        defaults_path=DEFAULTS_DIR,
        override_path=mind / ".alice" / "prompts",
        context_defaults=personae.as_template_context(),
    )
    _prompts.set_default_loader(loader)


def _render_system_prompt(personae) -> str:
    """Render meta.system_persona for the wake's ``append_system_prompt``."""
    from alice_prompts import load as load_prompt

    return load_prompt("meta.system_persona", **personae.as_template_context())


def _build_context(args: argparse.Namespace, personae) -> WakeContext:
    """Resolve CLI args + config into the per-wake :class:`WakeContext`.

    The selector + mode read fields off the context; this is the one
    place that knows about argparse + alice.config.json + model.yml.
    """
    mind = pathlib.Path(args.mind)
    if args.quick:
        cwd = pathlib.Path("/tmp")
        max_seconds = QUICK_MAX_SECONDS
        tools: list[str] = []
    else:
        cwd = mind
        max_seconds = args.max_seconds
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]

    bootstrap_path: pathlib.Path | None = None
    if not args.quick and not args.prompt:
        bootstrap_path = pathlib.Path(
            args.bootstrap or (mind / "prompts" / "thinking-bootstrap.md")
        )

    return WakeContext(
        mind_dir=mind,
        cwd=cwd,
        now=datetime.now(WAKE_TZ),
        personae=personae,
        model=args.model,
        max_seconds=max_seconds,
        tools=tools,
        system_prompt=_render_system_prompt(personae),
        quick=args.quick,
        inline_prompt=args.prompt,
        bootstrap_path=bootstrap_path,
        directive_path=mind / "inner" / "directive.md",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One thinking wake (Claude Agent kernel)."
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument(
        "--bootstrap",
        default=None,
        help="prompt file (default: mind/prompts/thinking-bootstrap.md)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="inline prompt (overrides --bootstrap)",
    )
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="event log path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_SECONDS,
        help="Wake budget in seconds. 0 or negative = no timeout (default).",
    )
    parser.add_argument("--tools", default=DEFAULT_TOOLS)
    parser.add_argument(
        "--echo", action="store_true", help="also echo events to stderr"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "30s plumbing smoke test — tiny prompt, no tools, cwd=/tmp. "
            "Verifies SDK + OAuth + Sonnet end-to-end without running the real "
            "thinking workflow."
        ),
    )
    args = parser.parse_args()

    _apply_config_overrides(args)

    # Plan 06 Phase 4: model.yml's thinking block drives auth + model.
    mind = pathlib.Path(args.mind)
    model_config = load_model_config(mind)
    thinking_spec = model_config.thinking
    ensure_auth_env(
        mode_hint=thinking_spec.backend,
        aws_region=thinking_spec.region,
        aws_profile=thinking_spec.profile,
    )
    if args.model == DEFAULT_MODEL and thinking_spec.model:
        args.model = thinking_spec.model

    emitter = EventLogger(pathlib.Path(args.log), echo=args.echo)

    # Plan 05 Phase 4: personae feeds the prompt loader's
    # context_defaults + the kernel's append_system_prompt.
    personae = _load_personae(mind)
    _install_prompt_loader(mind, personae)

    ctx = _build_context(args, personae)
    # Phase 3: vault-state snapshot at wake-start. Cheap I/O; gives
    # the selector + (Phase 4) sleep sub-stage logic something to
    # reason against. Skipped for --quick because /tmp isn't a mind.
    vault = None if args.quick else snapshot_vault(mind, now=ctx.now)
    mode = select_mode(now=ctx.now, vault=vault)
    # SleepMode delegates to a Stage; emit the stage's specific name
    # (e.g. ``sleep:consolidate``) so the viewer can attribute behavior.
    if isinstance(mode, SleepMode):
        emitted_mode = mode.stage
    else:
        emitted_mode = mode

    return asyncio.run(
        run_wake(ctx=ctx, mode=emitted_mode, emitter=emitter, backend=thinking_spec)
    )


if __name__ == "__main__":
    sys.exit(main())
