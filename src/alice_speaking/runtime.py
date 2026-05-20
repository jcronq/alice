"""Speaking-side phase runtime — PER_ISSUE_BUILD prompt + kernel-spec composition.

The SM v2 pipeline revision post-amendment
(``cortex-memory/research/2026-05-13-sm-v2-pipeline-revision.md``,
Jason 2026-05-13 09:51 EDT) moved the build phase onto a
stimulus-spawned speaking-instance. The dispatcher
(:func:`sm.dispatcher.spawn_speaking_agent`, filed separately as
issue #184) writes ``prompt.txt`` into a per-issue spawn dir with
frontmatter pointing at the approved design note; this module composes
the build-time prompt and kernel spec the entrypoint
(:mod:`alice_speaking.cli.perissue`) then drives.

Shape parallels :class:`alice_thinking.runtime.PhaseRunner`:
``PhaseRunner.run(phase, inputs)`` returns ``(prompt_text,
KernelSpec)``. The entrypoint reads the spawn dir, calls :meth:`run`,
drives the kernel as the build sub-agent, parses the result for the
PR URL, and posts the ``[SM] build-complete pr=<url>`` (or
``[SM] build-failed reason=...``) audit comment.

The SWE practices baseline is inlined here for v1
(:data:`SWE_BASELINE_PRACTICES`). The follow-up issue per
``cortex-memory/research/2026-05-12-swe-practices-in-agents-review.md``
extracts the baseline into
``cortex-memory/reference/base-worker-prompt.md``; at that point the
runtime swaps the inline constant for a vault-load + cache.
"""

from __future__ import annotations

import enum
import pathlib
import re
from dataclasses import dataclass
from typing import Optional

from core.kernel import KernelSpec


__all__ = [
    "Phase",
    "PhaseRunner",
    "BuildPromptInputs",
    "BUILD_AGENT_TOOL_ALLOWLIST",
    "SWE_BASELINE_PRACTICES",
    "compose_build_prompt",
    "parse_pr_url",
    "render_build_complete_comment",
    "render_build_failed_comment",
]


class Phase(enum.Enum):
    """Speaking-side phase the per-issue entrypoint can dispatch.

    Sibling to :class:`alice_thinking.phase.Phase` — Speaking owns the
    build phase per the SM v2 pipeline revision. The thinking-side
    phase enum carries DESIGN + BUILD because the original design
    (pre-amendment) had a single agent own both; the post-amendment
    split moved BUILD onto Speaking, so the speaking-side enum starts
    with just :attr:`PER_ISSUE_BUILD` and grows as additional
    stimulus-spawn phases land.
    """

    PER_ISSUE_BUILD = "per_issue_build"


# Tool allowance for the build sub-agent. Mirrors the issue spec:
# ``Bash, Read, Edit, Write, Grep, Glob, Agent``. The ``Agent`` entry
# is the SDK's name for the Task tool — present so the sub-agent can
# itself fan out to nested sub-agents if a multi-step build calls for
# it. No web/MCP tools — the build path needs only filesystem + git +
# gh, which Bash covers.
BUILD_AGENT_TOOL_ALLOWLIST: tuple[str, ...] = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Grep",
    "Glob",
    "Agent",
)


# v1: inline. Per ``cortex-memory/research/2026-05-12-swe-practices-in-agents-review.md``
# this baseline migrates into ``cortex-memory/reference/base-worker-prompt.md``
# so the thinking-side worker, the speaking-side worker, and any
# future code-worker dispatch share a single source of truth. Until
# that file exists the prompt is composed from this constant.
SWE_BASELINE_PRACTICES = """\
## SWE practices baseline

You are opening a draft PR against `jcronq/alice`. Operate as a real
engineer would:

- **Branch off `master`.** `git checkout -b feat/<slug>-<N> master`.
- **Push, don't stash.** Commit + push as you go. Never `git stash`
  uncommitted work and walk away — it'll be lost on the next reap.
- **No `--no-verify`.** Pre-commit hooks must pass. If they fail,
  investigate and fix the underlying issue; do not bypass.
- **Draft PRs only.** Open `gh pr create --draft`. The Sonnet code
  reviewer at `sm:reviewing` flips draft → ready once it passes
  review. Do not self-merge from the build phase.
- **Tests must be written.** Every behavior change ships with a test.
  Run the relevant suite locally before opening the PR.
- **No half-finished implementations.** Don't leave TODOs in
  production paths or stub out functions you couldn't complete; if
  you're blocked, post a `[SM] build-blocked reason="<line>"` comment
  on the issue and exit non-zero.
- **PR body includes `Closes #<N>`** so the issue auto-closes on merge.
"""


_PR_URL_RE = re.compile(r"https?://github\.com/[\w./-]+/pull/\d+")


def parse_pr_url(text: str) -> Optional[str]:
    """Extract the first GitHub PR URL from ``text``.

    Matches ``https://github.com/<owner>/<repo>/pull/<N>`` (and the
    ``http://`` variant for completeness). Returns ``None`` when no
    URL is present — the entrypoint treats that as a failed build and
    posts ``[SM] build-failed`` with the raw text as the reason.
    """
    m = _PR_URL_RE.search(text)
    return m.group(0) if m else None


def render_build_complete_comment(pr_url: str) -> str:
    """Produce the literal ``[SM] build-complete pr=<url>`` payload."""
    return f"[SM] build-complete pr={pr_url}"


def render_build_failed_comment(reason: str) -> str:
    """Produce the literal ``[SM] build-failed reason="<one line>"`` payload.

    The reason is collapsed to a single line + length-capped at 400
    chars so a kernel stack trace or runaway sub-agent output can't
    blow out the GitHub comment.
    """
    cleaned = " ".join(reason.split())[:400]
    return f'[SM] build-failed reason="{cleaned}"'


@dataclass(frozen=True)
class BuildPromptInputs:
    """Inputs to :func:`compose_build_prompt`.

    The entrypoint reads ``issue_body`` out of ``prompt.txt`` (the
    dispatcher writes the GitHub issue body verbatim into the body
    section after frontmatter) and loads ``design_note_text`` from
    the vault path the dispatcher embedded in the ``design_note:``
    frontmatter line. ``issue_number`` + ``repo`` surface back into
    the prompt so the sub-agent knows what to render into the
    ``Closes #<N>`` PR footer.
    """

    issue_number: int
    issue_body: str
    design_note_text: str
    repo: str = "jcronq/alice"


def compose_build_prompt(
    inputs: BuildPromptInputs,
    *,
    swe_baseline: str = SWE_BASELINE_PRACTICES,
) -> str:
    """Compose the sub-agent prompt for one PER_ISSUE_BUILD dispatch.

    Order: framing → issue body → approved design → SWE baseline →
    final-line directive. Sub-agents read top-down; putting the
    design above the baseline keeps the "what to build" prominent
    before the cross-cutting practice rules.

    The trailing directive tells the sub-agent to emit
    ``PR opened at <url>`` on success. The entrypoint
    (:func:`parse_pr_url`) is intentionally lenient — any
    ``github.com/.../pull/<N>`` URL in the output works — but the
    sentence steers the model toward the cheapest parseable shape.
    """
    sections = [
        (
            f"You are the speaking-side build sub-agent dispatched to "
            f"implement issue #{inputs.issue_number} on "
            f"`{inputs.repo}`. The design phase has already produced "
            f"and approved a design note for this issue (loaded "
            f"below) — your job is to implement it."
        ),
        "",
        "## Issue",
        "",
        inputs.issue_body.strip(),
        "",
        "## Approved design",
        "",
        inputs.design_note_text.strip(),
        "",
        swe_baseline.strip(),
        "",
        (
            f"When the draft PR is open, end your turn with a single "
            f"line of the form `PR opened at <url>` so the dispatcher "
            f"can record it. If you cannot complete the build, post a "
            f"`[SM] build-blocked reason=\"<one line>\"` comment on "
            f"issue #{inputs.issue_number} and exit without opening a "
            f"PR; the dispatcher will mark the build failed."
        ),
    ]
    return "\n".join(sections)


class PhaseRunner:
    """Composes prompt + :class:`KernelSpec` for one speaking-side phase.

    Today only :attr:`Phase.PER_ISSUE_BUILD` is supported; the gate
    keeps the surface narrow until additional stimulus-spawn phases
    land. Mirrors :class:`alice_thinking.runtime.PhaseRunner` in
    shape: stateless aside from the SWE baseline override, and
    :meth:`run` returns ``(prompt_text, KernelSpec)`` ready for the
    entrypoint to drive.
    """

    def __init__(self, *, swe_baseline: str = SWE_BASELINE_PRACTICES) -> None:
        self.swe_baseline = swe_baseline

    def build_prompt(self, inputs: BuildPromptInputs) -> str:
        return compose_build_prompt(inputs, swe_baseline=self.swe_baseline)

    def kernel_spec(
        self,
        *,
        model: str,
        cwd: pathlib.Path,
        add_dirs: Optional[list[pathlib.Path]] = None,
        max_seconds: int = 0,
        append_system_prompt: Optional[str] = None,
    ) -> KernelSpec:
        """Build the :class:`KernelSpec` for the build sub-agent.

        ``allowed_tools`` is the full code-worker set
        (:data:`BUILD_AGENT_TOOL_ALLOWLIST`). ``max_seconds=0`` keeps
        the build unbounded; per-issue builds are long-lived and the
        dispatcher pidfile-reap path is the kill-switch.
        """
        return KernelSpec(
            model=model,
            allowed_tools=list(BUILD_AGENT_TOOL_ALLOWLIST),
            cwd=cwd,
            add_dirs=add_dirs,
            max_seconds=max_seconds,
            thinking="medium",
            append_system_prompt=append_system_prompt,
        )

    def run(
        self,
        phase: Phase,
        inputs: BuildPromptInputs,
        *,
        model: str,
        cwd: pathlib.Path,
        add_dirs: Optional[list[pathlib.Path]] = None,
        max_seconds: int = 0,
        append_system_prompt: Optional[str] = None,
    ) -> tuple[str, KernelSpec]:
        """Compose ``(prompt_text, KernelSpec)`` for ``phase``.

        Validates ``phase`` against the (single-value, today) Phase
        enum so a typo in the entrypoint surfaces as a
        ``ValueError`` rather than a silently-degraded dispatch.
        """
        if not isinstance(phase, Phase):
            raise ValueError(f"unknown speaking Phase: {phase!r}")
        prompt = self.build_prompt(inputs)
        spec = self.kernel_spec(
            model=model,
            cwd=cwd,
            add_dirs=add_dirs,
            max_seconds=max_seconds,
            append_system_prompt=append_system_prompt,
        )
        return prompt, spec
