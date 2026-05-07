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
import time
from datetime import datetime
from typing import Optional

from alice_core.config.auth import ensure_auth_env
from alice_core.config.model import load as load_model_config
from alice_core.config.personae import (
    PersonaeError,
    load as load_personae,
    placeholder as placeholder_personae,
)
from alice_core.events import EventLogger

from . import backoff
from ._prompt_assembly import WAKE_TZ
from . import design_pipeline as _design_pipeline
from .kernel_adapter import run_wake
from .modes.active import ActiveMode
from .modes.base import WakeContext
from .modes.sleep import SleepMode
from .phase import (
    Phase,
    build_vault_snapshot,
    detect_commission_notes,
    detect_conflict_notes,
    select_phase,
)
from .runtime import PhaseRunner, load_phase_config
from .selector import select_mode
from .vault_state import snapshot as snapshot_vault


DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_DIRECTIVE = DEFAULT_MIND / "inner" / "directive.md"
DEFAULT_LOG = pathlib.Path("/state/worker/thinking.log")
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
# Empty default — when the user doesn't pass ``--tools``, ctx.tools is
# left empty and PhaseRunner picks the runtime default (the full tool
# set for every non-Quick phase, an empty list for Quick). Passing
# ``--tools=Foo,Bar`` (or setting ``thinking.allowed_tools`` in
# ``alice.config.json``) still overrides at the WakeContext layer.
DEFAULT_TOOLS = ""
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_SECONDS = 0  # 0 == no timeout. Thinking runs as long as it needs.
QUICK_MAX_SECONDS = 30
INTERVAL_FILE_NAME = "next-thinking-interval-seconds"


def _run_commission(
    commission_note: pathlib.Path,
    *,
    mind: pathlib.Path,
    emitter: EventLogger,
    phase: Phase,
) -> None:
    """Drive one design-commission pipeline, surface the result.

    Approved → commit draft to ``cortex-memory/research/`` + emit a
    ``design-commission-result`` surface.
    Cap-hit → DO NOT commit; emit a ``design-commission-cap-hit``
    surface that includes the unresolved feedback for human review.
    Always emit a ``design_commission`` telemetry event.
    """

    runner = _design_pipeline.DesignPipelineRunner()
    result = runner.run(commission_note)

    if result.verdict == "approved":
        slug_hint = commission_note.stem
        output_path = _design_pipeline.commit_approved_draft(
            mind, draft=result.draft, slug_hint=slug_hint
        )
        result.output_path = output_path
        body = (
            f"Design commission approved after {result.iteration_count} "
            f"iteration(s).\n\n{result.summary}\n\nDraft committed to "
            f"`{output_path.relative_to(mind) if output_path.is_absolute() else output_path}`."
        )
        _design_pipeline.write_surface(
            mind,
            surface_type="design-commission-result",
            body=body,
            extra_frontmatter={
                "verdict": "approved",
                "iterations": result.iteration_count,
                "draft_path": str(output_path),
                "spec_path": str(commission_note),
            },
        )
    else:
        # Cap hit — DO NOT commit. Surface the feedback verbatim.
        feedback_text = "\n".join(
            f"- [{fb.get('severity', '?')}] {fb.get('category', '?')}: "
            f"{fb.get('description', '')}"
            for fb in result.last_feedback
        ) or "(no feedback recorded)"
        body = (
            f"Design commission hit the {result.iteration_count}-iteration "
            "cap without approval. Draft has known unresolved issues; "
            "human review required.\n\n"
            f"Last summary: {result.summary}\n\n"
            f"Outstanding feedback:\n\n{feedback_text}\n"
        )
        _design_pipeline.write_surface(
            mind,
            surface_type="design-commission-cap-hit",
            body=body,
            extra_frontmatter={
                "verdict": "cap_hit",
                "iterations": result.iteration_count,
                "spec_path": str(commission_note),
            },
        )

    emitter.emit(
        "design_commission",
        **_design_pipeline.telemetry_payload(result, phase_value=phase.value),
    )


def _run_conflict_resolution(
    conflict_note: pathlib.Path,
    *,
    mind: pathlib.Path,
    emitter: EventLogger,
    phase: Phase,
) -> None:
    """Drive one conflict-resolution wake.

    Mirrors :func:`_run_commission`'s structural shape — task-type
    dispatched, single note per wake, telemetry event logged.

    The real resolution logic (Sonnet review of the contradictory
    facts, merge or fork, archive into
    ``cortex-memory/conflicts/.resolved/``) is deferred. Today the
    runner stub returns a ``deferred`` verdict and we log it; the
    conflict note stays open until the follow-up commit lands.
    """

    runner = PhaseRunner()
    result = runner._run_conflict_resolution(ctx=None)  # type: ignore[arg-type]

    emitter.emit(
        "conflict_resolution",
        phase=phase.value,
        verdict=result.get("verdict", "deferred"),
        conflict_path=str(conflict_note),
        summary=result.get("summary", ""),
    )


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

    For non-quick wakes, the kernel cwd swaps to the rendered
    thinking-scope skills dir under ``state_dir`` so the agent's
    auto-loader (SDK or pi) sees only ``thinking | both`` skills.
    The original mind stays reachable via :attr:`add_dirs`.
    """
    mind = pathlib.Path(args.mind)
    state_dir = pathlib.Path(args.state_dir)
    add_dirs: Optional[list[pathlib.Path]] = None
    if args.quick:
        cwd = pathlib.Path("/tmp")
        max_seconds = QUICK_MAX_SECONDS
        tools: list[str] = []
    else:
        max_seconds = args.max_seconds
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
        # Plan 07 P3 / plan-pi Phase C: render thinking-scope skills
        # to the per-hemisphere ephemeral dir, then point cwd there.
        from alice_skills.registry import SkillRegistry
        from alice_skills.render import render_to_disk

        cwd = state_dir / "alice-skills" / "thinking"
        registry = SkillRegistry.from_mind(mind)
        render_to_disk(
            registry,
            hemisphere="thinking",
            target_dir=cwd,
            personae=personae,
            mind_dir=mind,
        )
        add_dirs = [mind]

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
        add_dirs=add_dirs,
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
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=(
            "Worker state dir. Used as the parent for the rendered "
            "thinking-scope skills dir; must be writable."
        ),
    )
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
    parser.add_argument(
        "--backend",
        choices=("subscription", "api", "bedrock", "pi"),
        default=None,
        help=(
            "Override mind/config/model.yml's thinking backend for this "
            "wake. Useful for ad-hoc smoke tests without editing config "
            "(e.g. --backend=pi to verify the codex auth bridge after "
            "a fresh `codex login`)."
        ),
    )
    args = parser.parse_args()

    _apply_config_overrides(args)

    # Plan 06 Phase 4: model.yml's thinking block drives auth + model.
    mind = pathlib.Path(args.mind)
    model_config = load_model_config(mind)
    thinking_spec = model_config.thinking
    if args.backend is not None:
        # Plan-pi Phase E: ad-hoc backend override for smoke testing.
        # Reuse the model.yml-resolved model + region/profile/base_url
        # but flip the backend value.
        from alice_core.config.model import BackendSpec

        thinking_spec = BackendSpec(
            backend=args.backend,
            model=thinking_spec.model,
            region=thinking_spec.region,
            profile=thinking_spec.profile,
            base_url=thinking_spec.base_url,
        )
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
    # Phase routing — design:
    # cortex-memory/research/2026-05-07-thinking-phase-routing-design.md.
    # Build the vault snapshot once at wake-start, pass it to
    # ``select_phase``. ``--quick`` short-circuits to Phase.QUICK so
    # the selector doesn't touch the filesystem in /tmp.
    phase_cfg = load_phase_config(mind)
    if args.quick:
        from dataclasses import replace as _replace

        phase_cfg = _replace(phase_cfg, quick_mode=True)
        phase = Phase.QUICK
    else:
        snap = build_vault_snapshot(
            mind,
            now=ctx.now,
            state_dir=pathlib.Path(args.state_dir),
            cfg=phase_cfg,
        )
        phase = select_phase(snap, phase_cfg)

        # Task-type preempts — these run BEFORE the cadence-routed
        # phase fires. Order is deterministic:
        #   1. Design commission (Jason-explicit work) — highest priority.
        #   2. Conflict resolution (vault-state-driven) — vault hygiene.
        #   3. Cadence routing (Active / Sleep B/C/D) — falls through.
        # Only one task-type wake per cron tick; oldest note wins
        # within a category.

        # Design-commission preempt.
        commissions = detect_commission_notes(mind)
        if commissions:
            phase = Phase.DESIGN_COMMISSION
            try:
                _run_commission(
                    commissions[0],
                    mind=mind,
                    emitter=emitter,
                    phase=phase,
                )
            except Exception as exc:  # noqa: BLE001
                emitter.emit(
                    "exception",
                    phase=phase.value,
                    type=type(exc).__name__,
                    message=str(exc),
                )
                return 1
            return 0

        # Conflict-resolution preempt — open items in
        # ``cortex-memory/conflicts/`` get one wake of attention.
        # Stub today (deferred verdict + telemetry); real resolution
        # logic ships in a follow-up commit.
        conflicts = detect_conflict_notes(mind / "cortex-memory")
        if conflicts:
            phase = Phase.CONFLICT_RESOLUTION
            try:
                _run_conflict_resolution(
                    conflicts[0],
                    mind=mind,
                    emitter=emitter,
                    phase=phase,
                )
            except Exception as exc:  # noqa: BLE001
                emitter.emit(
                    "exception",
                    phase=phase.value,
                    type=type(exc).__name__,
                    message=str(exc),
                )
                return 1
            return 0

    runner = PhaseRunner(config=phase_cfg)
    if phase == Phase.ACTIVE:
        mode_obj = ActiveMode(runner=runner)
    elif phase in (Phase.SLEEP_B, Phase.SLEEP_C, Phase.SLEEP_D):
        mode_obj = SleepMode(runner=runner, phase=phase)
    else:
        # QUICK / DESIGN_COMMISSION fall back to the active wrapper —
        # the runner handles ``ctx.quick`` / inline_prompt internally.
        mode_obj = ActiveMode(runner=runner)

    # ``vault`` is still computed for backoff (existing contract) —
    # build_vault_snapshot doesn't replace VaultState's frontmatter
    # heuristics for did_work counting.
    vault = None if args.quick else snapshot_vault(mind, now=ctx.now)
    # Selector kept around for the explicit `select_mode` invariant
    # the existing test suite asserts on.
    _ = select_mode(now=ctx.now, vault=vault)

    wake_start_ts = time.time()
    rc = asyncio.run(
        run_wake(
            ctx=ctx,
            mode=mode_obj,
            emitter=emitter,
            backend=thinking_spec,
            phase=phase.value,
        )
    )

    # Sleep-mode exponential backoff: write the next wake-to-wake
    # interval for the s6 supervisor. Skipped for --quick (a smoke
    # test shouldn't reshape the live cadence). See
    # cortex-memory/research/2026-05-01-sleep-mode-exponential-backoff-design.md.
    if not args.quick:
        interval_path = pathlib.Path(args.state_dir) / INTERVAL_FILE_NAME
        prev_interval = backoff.read_interval(interval_path)
        did_work = backoff.detect_did_work(mind, since_ts=wake_start_ts)
        next_interval = backoff.next_interval_seconds(
            prev_seconds=prev_interval,
            mode=mode_obj.name,
            did_work=did_work,
        )
        try:
            backoff.write_interval_atomic(interval_path, next_interval)
        except OSError as exc:
            # State-dir issues shouldn't fail the wake; supervisor
            # falls back to its built-in default if the file is
            # unreadable on the next iteration.
            print(f"thinking: failed to write {interval_path}: {exc}", file=sys.stderr)

    return rc


if __name__ == "__main__":
    sys.exit(main())
