"""Per-issue speaking-side design-reviewer entrypoint.

Sibling of :mod:`alice_speaking.cli.perissue` (the build entrypoint).
Invoked by :func:`alice_forge.dispatcher.spawn.spawn_design_reviewer_agent`
for each ``(sm:design_review, art:code)`` issue. Reads the spawn dir's
``prompt.txt`` (which carries ``issue=#N`` + ``design_note=<vault-path>``
frontmatter), loads the design note text, composes a review prompt from
``per-issue-design-review.md``, drives the kernel at Opus, parses the
final-line verdict, and posts the ``[SM] design-approved …`` or
``[SM] design-revise reason="…" …`` audit comment on the issue.

Issue #344. Architecture decision: the CLI parses + posts rather than
the sub-agent posting directly. Reasons:

- The dispatcher's design_review handler already parses these exact
  prefixes — the wire format is fixed, and "agent emits literal prefix"
  is the simplest contract.
- No tool privileges (gh, Bash) need to be granted to the sub-agent —
  the kernel call is pure text-in/text-out, easier to bound.
- The CLI can apply a failsafe revise when the agent's final line is
  unparseable, so the issue never sits at sm:design_review forever
  due to a confused agent.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
import time
from importlib import resources
from typing import Callable, Iterable, Optional

from .perissue import parse_frontmatter, post_comment_via_gh


__all__ = ["main", "parse_verdict_line", "render_failsafe_revise"]


# The agent is told to put the verdict on the final line. Tolerate a
# trailing whitespace-only line (model occasionally appends a blank line)
# but otherwise we look for the *last* non-empty line in the response.
_APPROVED_RE = re.compile(r"^\[SM\]\s+design-approved\b", re.IGNORECASE)
_REVISE_RE = re.compile(r"^\[SM\]\s+design-revise\s+reason=", re.IGNORECASE)


# Hard cap on the kernel call. Opus reviews of a typical design note
# (~1KB) take 15-40s; we leave headroom for retries inside the kernel.
DEFAULT_MAX_SECONDS = 180


def parse_verdict_line(text: str) -> Optional[str]:
    """Find the verdict line in the agent's response text.

    Returns the literal verdict line (the comment body the dispatcher
    will parse) or ``None`` when no parseable verdict is present.

    Scans the response **bottom-up** because the prompt says the verdict
    is the FINAL line. Stops at the first matching line — earlier
    occurrences inside reasoning prose are ignored.
    """
    if not text:
        return None
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if _APPROVED_RE.match(line) or _REVISE_RE.match(line):
            return line
        # First non-empty line that doesn't match either prefix — the
        # agent failed to follow the contract. Don't keep scanning;
        # we want the *last* line specifically.
        return None
    return None


def render_failsafe_revise(reason: str) -> str:
    """Build a revise comment body when the agent didn't produce a verdict.

    The reason is one short sentence; the trailing prose is a brief
    explanation so the next iteration of thinking sees what went wrong.
    """
    cleaned = " ".join(reason.split())[:400]
    return (
        f'[SM] design-revise reason="{cleaned}" — reviewer CLI failsafe: '
        f"the design-reviewer agent did not produce a parseable verdict "
        f"on its final line; retry on the next dispatcher pass."
    )


def _load_review_prompt_template() -> str:
    """Load per-issue-design-review.md from the alice_thinking prompts pkg.

    Mirrors :class:`alice_speaking.runtime.PhaseRunner`'s prompt loading
    — the prompts live in :mod:`alice_thinking.prompts` because that's
    where per-issue-design.md and per-issue-build.md already are. The
    reviewer prompt is a sibling of those two.
    """
    return (
        resources.files("alice_thinking.prompts")
        .joinpath("per-issue-design-review.md")
        .read_text(encoding="utf-8")
    )


def _compose_review_prompt(
    *,
    issue_number: int,
    issue_body: str,
    design_note_text: str,
    design_note_path: pathlib.Path,
) -> str:
    """Assemble the prompt sent to the kernel.

    Shape: instructions (from per-issue-design-review.md) +
    a clearly demarcated context block carrying the issue body and the
    design note text. The agent's job is to read both and emit one
    verdict line.
    """
    template = _load_review_prompt_template()
    return (
        f"{template}\n\n"
        f"---\n"
        f"## Context for review\n"
        f"\n"
        f"**Issue: #{issue_number}**\n"
        f"\n"
        f"```\n"
        f"{issue_body}\n"
        f"```\n"
        f"\n"
        f"**Design note** (loaded from `{design_note_path}`):\n"
        f"\n"
        f"```\n"
        f"{design_note_text}\n"
        f"```\n"
    )


async def _drive_kernel_async(
    prompt_text: str,
    *,
    model: str,
    max_seconds: int,
    log: Callable[[str], None],
) -> tuple[int, str]:
    """Run the kernel with the review prompt. Returns ``(exit_code, text)``.

    Mirrors :func:`alice_speaking.cli.perissue._drive_kernel_async` but
    without the heavy PhaseRunner spec building — the reviewer doesn't
    need tool wiring, just a single Opus text call.
    """
    from core.config.model import BackendSpec
    from core.events import EventLogger
    from core.kernel import make_kernel
    from core.kernel.types import KernelSpec

    backend = BackendSpec(backend="subscription", model=model)
    emitter = EventLogger(pathlib.Path("/dev/null"))
    kernel = make_kernel(
        backend,
        emitter,
        correlation_id=f"speaking-design-review-{int(time.time())}",
        short_cap=4000,
    )
    spec = KernelSpec(
        system_prompt=None,
        cwd=pathlib.Path.cwd(),
        max_seconds=max_seconds if max_seconds > 0 else None,
        tools_allowed=(),  # text in, text out — no Bash, no Edit
    )
    try:
        result = await kernel.run(prompt_text, spec)
    except Exception as exc:  # noqa: BLE001
        log(
            f"[sm-design-review-cli] kernel raised "
            f"{type(exc).__name__}: {exc}"
        )
        return 1, ""
    text = getattr(result, "text", "") or ""
    if getattr(result, "error", None) == "timeout":
        log("[sm-design-review-cli] kernel reported timeout")
        return 124, text
    if getattr(result, "is_error", False):
        return 1, text
    return 0, text


def _default_kernel_runner(
    prompt_text: str,
    *,
    model: str,
    max_seconds: int,
    log: Callable[[str], None],
) -> tuple[int, str]:
    return asyncio.run(
        _drive_kernel_async(
            prompt_text, model=model, max_seconds=max_seconds, log=log
        )
    )


def main(
    argv: Optional[Iterable[str]] = None,
    *,
    kernel_runner: Callable[..., tuple[int, str]] = _default_kernel_runner,
    post_comment: Callable[[str, int, str], None] = post_comment_via_gh,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> int:
    """Drive a single design review and post the verdict.

    Exit codes:

    - ``0`` — kernel returned a parseable verdict AND the comment posted
    - ``1`` — any pre-kernel failure (missing prompt.txt, missing/unreadable
      design note, missing frontmatter), kernel exception, or comment-post
      failure. On unparseable verdict we still try to post a failsafe
      revise comment so the issue moves; that path returns 1.
    - ``124`` — kernel-reported timeout (failsafe revise still attempted)
    """
    parser = argparse.ArgumentParser(
        description=(
            "Per-issue design-reviewer entrypoint. Reads <spawn-dir>/prompt.txt, "
            "loads the design note path from frontmatter, drives the kernel, "
            "and posts [SM] design-approved or [SM] design-revise."
        )
    )
    parser.add_argument("--spawn-dir", required=True)
    parser.add_argument(
        "--session-id",
        default=None,
        help="claude-agent-sdk session id pre-minted by the dispatcher",
    )
    parser.add_argument(
        "--repo",
        default="jcronq/alice",
        help="GitHub repo for the verdict comment (default: jcronq/alice)",
    )
    parser.add_argument("--model", default="claude-opus-4-7")
    parser.add_argument(
        "--max-seconds", type=int, default=DEFAULT_MAX_SECONDS
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + compose prompt; skip kernel + comment posting",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        log(f"[sm-design-review-cli] missing prompt.txt at {prompt_path}")
        return 1
    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"[sm-design-review-cli] failed to read {prompt_path}: {exc}")
        return 1

    fm, body = parse_frontmatter(raw)
    issue_raw = fm.get("issue", "")
    try:
        issue_number = int(issue_raw)
    except (TypeError, ValueError):
        log(
            f"[sm-design-review-cli] prompt.txt missing or invalid "
            f"`issue:` frontmatter: {issue_raw!r}"
        )
        return 1

    design_note_raw = fm.get("design_note", "")
    if not design_note_raw:
        log(
            f"[sm-design-review-cli] prompt.txt on #{issue_number} is "
            f"missing the `design_note:` frontmatter"
        )
        return 1
    design_note_path = pathlib.Path(design_note_raw).expanduser()
    if not design_note_path.is_file():
        # Tell the dispatcher: design note vanished. Post a revise so the
        # thinking-design phase retries instead of leaving the issue stuck.
        comment_body = render_failsafe_revise(
            f"design note not found at {design_note_path}"
        )
        try:
            post_comment(args.repo, issue_number, comment_body)
        except Exception as exc:  # noqa: BLE001
            log(
                f"[sm-design-review-cli] failed to post failsafe revise "
                f"on #{issue_number}: {type(exc).__name__}: {exc}"
            )
        return 1
    try:
        design_note_text = design_note_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(
            f"[sm-design-review-cli] failed to read design note "
            f"{design_note_path}: {exc}"
        )
        return 1

    review_prompt = _compose_review_prompt(
        issue_number=issue_number,
        issue_body=body.strip(),
        design_note_text=design_note_text,
        design_note_path=design_note_path,
    )
    log(
        f"[sm-design-review-cli] composed review prompt for "
        f"#{issue_number} (prompt_chars={len(review_prompt)}, "
        f"design_note_chars={len(design_note_text)})"
    )

    if args.dry_run:
        return 0

    rc, result_text = kernel_runner(
        review_prompt,
        model=args.model,
        max_seconds=args.max_seconds,
        log=log,
    )

    verdict = parse_verdict_line(result_text)
    if verdict is None:
        # Failsafe — the agent didn't produce a verdict. Post a revise so
        # the issue doesn't sit at sm:design_review forever.
        reason = (
            "reviewer-cli could not parse a verdict line from the agent "
            f"response (rc={rc})"
        )
        comment_body = render_failsafe_revise(reason)
        try:
            post_comment(args.repo, issue_number, comment_body)
            log(
                f"[sm-design-review-cli] posted failsafe revise on "
                f"#{issue_number}"
            )
        except Exception as exc:  # noqa: BLE001
            log(
                f"[sm-design-review-cli] failed to post failsafe revise "
                f"on #{issue_number}: {type(exc).__name__}: {exc}"
            )
            return 1
        # Treat unparseable as a soft failure. The issue moves (revise →
        # designing) so the dispatcher won't loop on it; but we surface
        # the rc so operators see something went wrong.
        return rc if rc != 0 else 1

    try:
        post_comment(args.repo, issue_number, verdict)
        log(
            f"[sm-design-review-cli] posted verdict on #{issue_number}: "
            f"{verdict[:60]!r}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log(
            f"[sm-design-review-cli] failed to post verdict on "
            f"#{issue_number}: {type(exc).__name__}: {exc}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
