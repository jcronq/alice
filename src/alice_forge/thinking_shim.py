"""Per-issue thinking-agent entrypoint — writes SM transitions directly.

PR-4 of #823 (issue #827) promotes this module from a placeholder
(issue #156) to the real per-issue entrypoint. The dispatcher
(:func:`alice_forge.dispatcher.spawn.spawn_thinking_agent`) writes
the prompt to ``<spawn_dir>/prompt.txt`` and launches this module
via::

    python -m alice_forge.thinking_shim --spawn-dir <dir> --session-id <uuid> --mode design

Design mode (``--mode design``): the shim delegates the kernel
drive to :mod:`alice_thinking.cli.perissue` (which composes the
prompt through :class:`alice_thinking.runtime.PhaseRunner` and
drives the kernel), then — regardless of what the long-lived agent
did or did not post to GitHub — records the outcome as a single
``state_machine`` + ``transitions`` write via
:func:`alice_forge.sm.store.transition`:

  * kernel exited clean AND a design doc landed under
    ``cortex-memory/designs/*-issue<N>-*.md`` → ``sm:designing →
    sm:design_review`` (``reason='design doc ready'``,
    ``source='thinking-shim'``, ``source_ref=<session_id>``).
  * kernel exited non-zero, raised, or produced no design doc →
    ``sm:designing → sm:blocked`` (``reason`` starts with
    ``'PhaseRunner failed'``, same source + ref).

Build mode (``--mode build``): the shim runs the kernel the same
way but does NOT transition SM state — the build-phase transition
is the speaking shim's job (:mod:`alice_forge.speaking_shim`).

Shadow mode (#823 PR-4/PR-6 interlock): the long-lived agent's
prompt still tells it to post ``[SM] design-ready
note=[[<wikilink>]]`` on the issue (see
:func:`alice_forge.dispatcher.spawn.compose_thinking_spawn_prompt`).
We leave that instruction in place so human readers scanning
issue comments keep seeing the familiar audit trail. The shim's
``store.transition`` call races the dispatcher's GH-comment parser
and whichever write lands first wins — the loser catches
:class:`alice_forge.sm.store.IllegalTransitionError` (state already
advanced) and logs loudly rather than crashing. PR-6 (#829) drops
the comment-emission half of this dual-write once PR-3 (#826) flips
the dispatcher to read the DB first.

Three DI seams (``runner_factory`` / ``kernel_runner`` /
``db_connect``) let the test suite swap in doubles without touching
the kernel, the state DB, or the vault.
"""

from __future__ import annotations

from core.forge_repos import primary_repo as _primary_repo
import argparse
import pathlib
import re
import sys
import time
from typing import Any, Callable, Iterable, Optional

from alice_forge.sm import store
from alice_forge.sm.states import SMState
from alice_state_db.read_api import connect as state_db_connect
from alice_state_db.schema import apply_schema
from alice_thinking.cli.perissue import (
    PerIssueDispatchError,
    _build_wake_context,
    _default_cwd,
    _default_kernel_runner,
    _default_mind,
    _load_backend,
    parse_frontmatter,
    resolve_phase,
)
from alice_thinking.runtime import PhaseRunner


# ---------------------------------------------------------------------------
# Issue-number parsing + item_id convention
# ---------------------------------------------------------------------------

#: The repo this dispatcher owns (single-repo per ``PROJECT_PROFILE.md``).
#: Paired with the issue number parsed out of ``prompt.txt`` to form
#: the ``items.item_id`` surrogate — matches the convention seeded by
#: :func:`alice_state_db.sm_seed.gh_issue_id` (``f"gh:{repo}#{number}"``).
REPO = _primary_repo()

#: Pattern matching the ``Issue: #<N>`` header rendered by
#: :func:`alice_forge.dispatcher.spawn.compose_thinking_spawn_prompt`.
#: The issue body's spec called for ``GitHub-Issue: jcronq/alice#<N>``
#: but that header does not exist in this repo; the real header is bare
#: ``Issue: #<N>``. See design note §4
#: (``cortex-memory/designs/2026-11-07-issue827-...``).
_ISSUE_RE = re.compile(r"^Issue:\s*#(\d+)", re.MULTILINE)


#: Design-doc glob pattern under ``<mind>/cortex-memory/designs/``.
#: Dates + slugs vary; the issue number is the stable anchor. Matches
#: the naming convention already in use (e.g. this very design note is
#: ``2026-11-07-issue827-thinking-shim-sm-store-transitions.md``).
_DESIGN_DOC_GLOB = "cortex-memory/designs/*issue{number}*.md"


class PhaseRunFailed(Exception):
    """Internal sentinel — the kernel run failed or produced no artifact.

    Carries the short reason that gets written to
    ``transitions.reason``. Not a public type — callers see
    ``return code 1`` and a transition row at ``sm:blocked``.
    """


def _item_id_from_prompt(raw: str) -> Optional[str]:
    """Parse the ``Issue: #<N>`` header and return the item_id surrogate.

    Returns ``None`` when the header is absent — the shim surfaces
    that as a loud log + exit 1 (no state_machine target to
    transition).
    """
    m = _ISSUE_RE.search(raw)
    if m is None:
        return None
    return f"gh:{REPO}#{m.group(1)}"


def _find_design_doc(
    mind: pathlib.Path, issue_number: str, since_ts: float
) -> Optional[pathlib.Path]:
    """Return the newest design doc for this issue modified after ``since_ts``.

    Globs ``<mind>/cortex-memory/designs/*issue{N}*.md`` and picks the
    newest-mtime match that was written at or after ``since_ts`` —
    i.e. during this kernel run. Matches the file-naming convention
    the dispatcher's prompt instructs the agent to use.

    Returns ``None`` when no such file exists — treated by the shim
    as a failed design run (no artifact → ``sm:blocked``).
    """
    pattern = _DESIGN_DOC_GLOB.format(number=issue_number)
    candidates: list[tuple[float, pathlib.Path]] = []
    for path in mind.glob(pattern):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since_ts:
            candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _run_phase(
    phase: Any,
    ctx: Any,
    body: str,
    *,
    backend: Any,
    runner_factory: Callable[[], PhaseRunner],
    kernel_runner: Callable[..., int],
    log: Callable[[str], None],
) -> int:
    """Compose + drive the kernel. Matches perissue's shape.

    Kept as a module-level helper so the test harness can swap in a
    pure in-process stub (returning 0 / 1 / 124) without touching
    :class:`PhaseRunner` or the kernel. The ``kernel_runner``
    signature follows perissue's convention — the default takes
    ``backend`` + ``log`` as kwargs; test doubles take only ``log``.
    """
    runner = runner_factory()
    prompt_text, spec = runner.run(phase, ctx, injected_content=body)
    if kernel_runner is _default_kernel_runner:
        return kernel_runner(prompt_text, spec, backend=backend, log=log)
    return kernel_runner(prompt_text, spec, log=log)


def _transition(
    db_connect: Callable[..., Any],
    item_id: str,
    *,
    to: SMState,
    reason: str,
    session_id: Optional[str],
    log: Callable[[str], None],
) -> None:
    """Call :func:`alice_forge.sm.store.transition` with shim defaults.

    Catches the three sm.store-side errors
    (:class:`store.ActorNotTrustedError`,
    :class:`store.IllegalTransitionError`,
    :class:`store.ItemNotFoundError`) and logs loudly instead of
    re-raising. Rationale: a failed SM write should not mask whatever
    the kernel already did (design doc on disk, comment posted, etc.);
    the operator surface is ``stderr.log`` / ``speaking.log`` — the
    smoke-verify step in the issue grep's for a ``transitions`` row,
    and a loud log tells them which path to look down when the row
    doesn't show up.

    ``source='thinking-shim'`` + ``source_ref=<session_id>`` form the
    idempotency key — a replay of the same session is safe. A dual
    write with the dispatcher's GH-comment parser (different source /
    ref) is NOT idempotent via this key; whichever path writes first
    wins and the second surfaces an :class:`IllegalTransitionError`
    that this helper swallows.
    """
    try:
        with db_connect(read_only=False) as conn:
            apply_schema(conn)
            store.transition(
                conn=conn,
                item_id=item_id,
                to=to,
                actor="thinking",
                reason=reason,
                source="thinking-shim",
                source_ref=session_id,
            )
    except (
        store.ActorNotTrustedError,
        store.IllegalTransitionError,
        store.ItemNotFoundError,
    ) as exc:
        log(
            f"[sm-thinking-shim] store.transition failed for "
            f"item_id={item_id!r} to={to.label}: {type(exc).__name__}: {exc}"
        )


def main(
    argv: Optional[Iterable[str]] = None,
    *,
    runner_factory: Callable[[], PhaseRunner] = PhaseRunner,
    kernel_runner: Callable[..., int] = _default_kernel_runner,
    db_connect: Callable[..., Any] = state_db_connect,
    find_design_doc: Callable[..., Optional[pathlib.Path]] = _find_design_doc,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
    clock: Callable[[], float] = time.time,
) -> int:
    """Read prompt.txt, drive the kernel, write the SM transition.

    Returns a process exit code:

    * ``0`` — design-mode: kernel clean exit, design doc found,
      ``sm:design_review`` transition written (or build-mode: kernel
      clean exit, no transition written).
    * ``1`` — any pre-kernel failure (missing prompt.txt, malformed
      frontmatter, unknown phase, missing ``Issue:`` header), a
      kernel-side failure (non-zero exit / exception), or a
      design-mode run that produced no design doc. A ``sm:blocked``
      transition is attempted when ``item_id`` is resolvable.

    ``runner_factory`` / ``kernel_runner`` / ``db_connect`` /
    ``find_design_doc`` / ``log`` / ``clock`` are DI seams; production
    callers don't pass them.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Per-issue thinking-agent entrypoint. Reads <spawn-dir>/prompt.txt, "
            "drives the per-issue kernel via alice_thinking.cli.perissue's "
            "helpers, and records the SM transition via alice_forge.sm.store."
        )
    )
    parser.add_argument(
        "--spawn-dir",
        required=True,
        help="path to the per-issue spawn dir containing prompt.txt",
    )
    parser.add_argument(
        "--session-id",
        required=True,
        help=(
            "claude-agent-sdk session id pre-minted by the dispatcher — "
            "becomes the ``source_ref`` idempotency key for the "
            "``transitions`` write"
        ),
    )
    parser.add_argument(
        "--mode",
        default="design",
        choices=("design", "build"),
        help=(
            "per-issue phase the agent is entering "
            "(design: writes the sm:design_review transition on success; "
            "build: no SM transition — that's speaking_shim's job)"
        ),
    )
    parser.add_argument(
        "--mind",
        default=None,
        help="alice-mind path (default: ~/alice-mind or $ALICE_MIND)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="kernel cwd (default: ~/alice if present, else current dir)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="model id override passed to the kernel (default: thinking config)",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="kernel max_seconds (0 = unbounded; per-issue work is long-lived)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        log(f"[sm-thinking-shim] no prompt.txt at {prompt_path} — exiting")
        return 1

    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"[sm-thinking-shim] failed to read {prompt_path}: {exc}")
        return 1

    # Parse item_id EARLY — a missing header means we can't transition
    # the state_machine row either way (no target), so bail out loudly
    # instead of running an expensive kernel we can't record the outcome
    # of.
    item_id = _item_id_from_prompt(raw)
    if item_id is None:
        log(
            f"[sm-thinking-shim] prompt.txt at {prompt_path} has no "
            f"`Issue: #<N>` header — cannot resolve item_id; exiting"
        )
        return 1

    try:
        phase = resolve_phase(raw, override=args.mode)
    except PerIssueDispatchError as exc:
        log(f"[sm-thinking-shim] {exc}")
        # No item_id-less failure — we DO have an item_id here; route
        # the failure through the blocked-transition so the audit trail
        # carries a reason.
        _transition(
            db_connect,
            item_id,
            to=SMState.BLOCKED,
            reason=f"PhaseRunner failed: {exc}",
            session_id=args.session_id,
            log=log,
        )
        return 1

    _, body = parse_frontmatter(raw)
    body = body.strip()
    if not body:
        log(
            f"[sm-thinking-shim] prompt.txt at {prompt_path} has no body "
            "after frontmatter — nothing to dispatch"
        )
        _transition(
            db_connect,
            item_id,
            to=SMState.BLOCKED,
            reason="PhaseRunner failed: empty prompt body",
            session_id=args.session_id,
            log=log,
        )
        return 1

    mind = pathlib.Path(args.mind) if args.mind else _default_mind()
    backend = _load_backend(mind=mind, model_override=args.model)
    ctx = _build_wake_context(
        spawn_dir=spawn_dir,
        mind=mind,
        cwd=pathlib.Path(args.cwd) if args.cwd else _default_cwd(),
        model=backend.model,
        max_seconds=args.max_seconds,
    )

    start_ts = clock()

    try:
        rc = _run_phase(
            phase,
            ctx,
            body,
            backend=backend,
            runner_factory=runner_factory,
            kernel_runner=kernel_runner,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 — kernel-side blast radius
        reason = f"PhaseRunner failed: {type(exc).__name__}: {exc}"
        log(f"[sm-thinking-shim] {reason}")
        _transition(
            db_connect,
            item_id,
            to=SMState.BLOCKED,
            reason=reason,
            session_id=args.session_id,
            log=log,
        )
        return 1

    if rc != 0:
        reason = f"PhaseRunner failed: kernel exited rc={rc}"
        log(f"[sm-thinking-shim] {reason}")
        _transition(
            db_connect,
            item_id,
            to=SMState.BLOCKED,
            reason=reason,
            session_id=args.session_id,
            log=log,
        )
        return 1

    # Build mode: the per-issue build kernel ran clean; the build-phase
    # transition belongs to :mod:`alice_forge.speaking_shim`, NOT here.
    if args.mode == "build":
        return 0

    # Design mode: confirm an artifact landed. We can't rely on
    # PhaseRunner.run() for a path (it only returns prompt+spec); glob
    # for a doc modified during this run.
    issue_number = item_id.rsplit("#", 1)[-1]
    doc_path = find_design_doc(mind, issue_number, start_ts)
    if doc_path is None:
        reason = (
            "PhaseRunner failed: no design doc found under "
            f"cortex-memory/designs/ for issue #{issue_number}"
        )
        log(f"[sm-thinking-shim] {reason}")
        _transition(
            db_connect,
            item_id,
            to=SMState.BLOCKED,
            reason=reason,
            session_id=args.session_id,
            log=log,
        )
        return 1

    log(
        f"[sm-thinking-shim] design doc found at {doc_path} — "
        f"transitioning {item_id} -> sm:design_review"
    )
    _transition(
        db_connect,
        item_id,
        to=SMState.DESIGN_REVIEW,
        reason="design doc ready",
        session_id=args.session_id,
        log=log,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
