"""Per-issue speaking-agent entrypoint — PER_ISSUE_BUILD phase.

Counterpart of :mod:`alice_thinking.cli.perissue` for the build half
of the SM v2 pipeline. Invoked by
:func:`forge.dispatcher.spawn_speaking_agent` (filed separately
as #184) for each ``(sm:designed, art:code)`` issue. Reads the
spawn dir's ``prompt.txt`` (which carries ``issue=#N``,
``design_note=<vault-path>``, ``art=<art-label>`` frontmatter +
the issue body), loads the approved design note, composes the
build sub-agent prompt via :class:`alice_speaking.runtime.PhaseRunner`,
drives the kernel, parses the result for the PR URL, and posts the
``[SM] build-complete pr=<url>`` (or ``[SM] build-failed reason=...``)
audit comment on the issue.

``scripts/sm-speaking-perissue.py`` is a thin wrapper around
:func:`main`; the dispatcher can launch either form. Both share the
same parse-resolve-dispatch logic so they stay testable.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Optional

from ..runtime import (
    BuildPromptInputs,
    Phase,
    PhaseRunner,
    parse_pr_url,
    render_build_complete_comment,
    render_build_failed_comment,
)


__all__ = [
    "PHASE_BY_NAME",
    "PerIssueDispatchError",
    "main",
    "parse_frontmatter",
    "resolve_phase",
]


# Maps the ``phase:`` frontmatter value (and ``--mode`` CLI flag) onto
# the speaking-side Phase enum. Kept narrow on purpose — only the
# build phase is a valid speaking entrypoint.
PHASE_BY_NAME: dict[str, Phase] = {
    Phase.PER_ISSUE_BUILD.value: Phase.PER_ISSUE_BUILD,
}


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class PerIssueDispatchError(ValueError):
    """Raised when prompt.txt cannot be resolved into a valid dispatch.

    The script catches this at the top of :func:`main`, logs the
    message, and exits non-zero — no audit comment is posted (the
    dispatcher's stderr capture is the operator's surface).
    """


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---\\n…\\n---\\n`` frontmatter block off the head of ``text``.

    Returns ``(frontmatter_dict, body)``. Frontmatter values are
    stripped of surrounding quotes for ergonomics. The ``issue``
    field's leading ``#`` (``issue: #185``) is stripped so callers
    can just ``int()`` the value.

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
        key = key.strip()
        if key == "issue":
            value = value.lstrip("#").strip()
        fm[key] = value
    return fm, text[m.end():]


def resolve_phase(text: str, *, override: Optional[str] = None) -> Phase:
    """Return the :class:`Phase` carried by ``text`` (or by ``override``).

    Resolution order:

    1. The ``--mode`` CLI override (``override``), if set.
    2. The ``phase:`` frontmatter value in ``text``.

    Raises :class:`PerIssueDispatchError` when neither source supplies
    a phase or the supplied value isn't a recognized speaking phase
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


def post_comment_via_gh(repo: str, issue_number: int, body: str) -> None:
    """Default audit-comment poster — shells out to ``gh issue comment``.

    Replaceable in tests via the ``post_comment`` kwarg on
    :func:`main`. We don't import :func:`forge.dispatcher.gh_post_comment`
    here to avoid pulling the entire dispatcher module into a worker
    process (the dispatcher imports heavy gh-watcher plumbing).
    """
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo,
            "--body",
            body,
        ],
        check=True,
    )


def _default_cwd() -> pathlib.Path:
    """Return ``~/alice`` if it exists, else the script's cwd.

    The build sub-agent runs ``git``/``gh`` against the alice repo,
    so cwd defaults to the repo root.
    """
    home_alice = pathlib.Path.home() / "alice"
    if home_alice.is_dir():
        return home_alice
    return pathlib.Path.cwd()


async def _drive_kernel_async(
    prompt_text: str,
    spec: Any,
    *,
    log: Callable[[str], None],
) -> tuple[int, str]:
    """Run the kernel, return ``(exit_code, result_text)``.

    Returns 0 on clean completion (with the assistant's final text),
    124 on kernel-reported timeout, 1 otherwise. The caller parses
    ``result_text`` for the PR URL.
    """
    from core.config.model import BackendSpec
    from core.events import EventLogger
    from core.kernel import make_kernel

    backend = BackendSpec(backend="subscription")
    emitter = EventLogger(pathlib.Path("/dev/null"))
    kernel = make_kernel(
        backend,
        emitter,
        correlation_id=f"speaking-perissue-{int(time.time())}",
        short_cap=4000,
    )
    try:
        result = await kernel.run(prompt_text, spec)
    except Exception as exc:  # noqa: BLE001
        log(f"[sm-speaking-perissue] kernel raised {type(exc).__name__}: {exc}")
        return 1, ""
    text = getattr(result, "text", "") or ""
    if getattr(result, "error", None) == "timeout":
        log("[sm-speaking-perissue] kernel reported timeout")
        return 124, text
    if getattr(result, "is_error", False):
        return 1, text
    return 0, text


def _default_kernel_runner(
    prompt_text: str,
    spec: Any,
    *,
    log: Callable[[str], None],
) -> tuple[int, str]:
    return asyncio.run(_drive_kernel_async(prompt_text, spec, log=log))


def main(
    argv: Optional[Iterable[str]] = None,
    *,
    runner_factory: Callable[[], PhaseRunner] = PhaseRunner,
    kernel_runner: Callable[..., tuple[int, str]] = _default_kernel_runner,
    post_comment: Callable[[str, int, str], None] = post_comment_via_gh,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> int:
    """Read prompt.txt, resolve phase, drive the build sub-agent, post audit.

    Returns a process exit code:

    - ``0`` — kernel ran clean AND the sub-agent emitted a PR URL
      AND the build-complete audit comment posted successfully.
    - ``1`` — any pre-kernel failure (missing prompt.txt, malformed
      frontmatter, missing/unreadable design note), kernel-side
      exception, or sub-agent failure (no PR URL in output). On
      sub-agent failure, the entrypoint posts ``[SM] build-failed``
      before returning.
    - ``124`` — kernel-reported timeout. Posts build-failed.

    ``runner_factory`` / ``kernel_runner`` / ``post_comment`` are
    injection points for the test harness; production callers don't
    pass them.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Per-issue speaking-agent entrypoint (PER_ISSUE_BUILD). "
            "Reads <spawn-dir>/prompt.txt, loads the approved design "
            "note from the vault path in frontmatter, composes a "
            "build sub-agent prompt, drives the kernel, parses the "
            "PR URL, and posts the [SM] build-complete audit comment."
        )
    )
    parser.add_argument(
        "--spawn-dir",
        required=True,
        help="per-issue spawn dir containing prompt.txt",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="claude-agent-sdk session id pre-minted by the dispatcher",
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=tuple(PHASE_BY_NAME),
        help="phase override (defaults to the prompt.txt `phase:` frontmatter)",
    )
    parser.add_argument(
        "--repo",
        default="jcronq/alice",
        help="GitHub repo for the audit comment (default: jcronq/alice)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="kernel cwd (default: ~/alice if it exists, else current dir)",
    )
    parser.add_argument(
        "--model",
        default="claude-opus-4-7",
        help="model id passed to the kernel (default: claude-opus-4-7)",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="kernel max_seconds (0 = unbounded; per-issue builds are long-lived)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve phase + compose prompt; skip kernel + comment posting",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        log(f"[sm-speaking-perissue] missing prompt.txt at {prompt_path}")
        return 1

    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"[sm-speaking-perissue] failed to read {prompt_path}: {exc}")
        return 1

    try:
        phase = resolve_phase(raw, override=args.mode)
    except PerIssueDispatchError as exc:
        log(f"[sm-speaking-perissue] {exc}")
        return 1

    fm, body = parse_frontmatter(raw)
    body = body.strip()

    issue_raw = fm.get("issue", "")
    try:
        issue_number = int(issue_raw)
    except (TypeError, ValueError):
        log(
            f"[sm-speaking-perissue] prompt.txt missing or invalid "
            f"`issue:` frontmatter: {issue_raw!r}"
        )
        return 1

    design_note_raw = fm.get("design_note", "")
    if not design_note_raw:
        log(
            f"[sm-speaking-perissue] prompt.txt on #{issue_number} is "
            f"missing the `design_note:` frontmatter — cannot compose "
            f"build prompt"
        )
        return 1
    design_note_path = pathlib.Path(design_note_raw).expanduser()
    if not design_note_path.is_file():
        log(
            f"[sm-speaking-perissue] design_note path does not exist: "
            f"{design_note_path}"
        )
        return 1
    try:
        design_note_text = design_note_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(
            f"[sm-speaking-perissue] failed to read design note "
            f"{design_note_path}: {exc}"
        )
        return 1

    if not body:
        log(
            f"[sm-speaking-perissue] prompt.txt at {prompt_path} has "
            f"no body after frontmatter — nothing to forward to the "
            f"sub-agent"
        )
        return 1

    inputs = BuildPromptInputs(
        issue_number=issue_number,
        issue_body=body,
        design_note_text=design_note_text,
        repo=args.repo,
    )

    runner = runner_factory()
    cwd = pathlib.Path(args.cwd) if args.cwd else _default_cwd()
    prompt_text, spec = runner.run(
        phase,
        inputs,
        model=args.model,
        cwd=cwd,
        max_seconds=args.max_seconds,
    )

    log(
        f"[sm-speaking-perissue] resolved phase={phase.value} "
        f"issue=#{issue_number} design_note={design_note_path} "
        f"prompt_chars={len(prompt_text)} "
        f"session_id={args.session_id or '(none)'}"
    )

    if args.dry_run:
        return 0

    rc, result_text = kernel_runner(prompt_text, spec, log=log)

    if rc == 0:
        pr_url = parse_pr_url(result_text)
        if pr_url:
            comment_body = render_build_complete_comment(pr_url)
            try:
                post_comment(args.repo, issue_number, comment_body)
                log(
                    f"[sm-speaking-perissue] posted build-complete on "
                    f"#{issue_number} pr={pr_url}"
                )
                return 0
            except Exception as exc:  # noqa: BLE001
                log(
                    f"[sm-speaking-perissue] failed to post "
                    f"build-complete on #{issue_number}: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 1
        # Kernel exited 0 but no PR URL in the output — sub-agent
        # didn't open a PR. Surface as build-failed.
        rc = 1

    reason = _summarize_failure_reason(result_text) or (
        f"kernel exited rc={rc} with no parseable PR URL"
    )
    comment_body = render_build_failed_comment(reason)
    try:
        post_comment(args.repo, issue_number, comment_body)
        log(
            f"[sm-speaking-perissue] posted build-failed on "
            f"#{issue_number} reason={reason!r}"
        )
    except Exception as exc:  # noqa: BLE001
        log(
            f"[sm-speaking-perissue] failed to post build-failed on "
            f"#{issue_number}: {type(exc).__name__}: {exc}"
        )
    return rc


def _summarize_failure_reason(text: str) -> str:
    """Collapse a multi-line sub-agent output into a one-line reason.

    Prefers the last non-empty line (sub-agents tend to put a status
    line at the end). Falls back to the joined head of the text.
    Length capping happens in :func:`render_build_failed_comment`.
    """
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return lines[-1]


if __name__ == "__main__":
    sys.exit(main())
