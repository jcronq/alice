"""Typed state + action types for the Stage B workflow.

Per the design sketch (``docs/designs/stage-b-adk-workflow-sketch.md``),
state threads through the workflow as a :class:`WakeState` object and
each step returns a typed :class:`StepResult`. The :class:`Action` union
is what :func:`classify_and_route_note` returns for each inbox note;
:func:`apply_action` turns it into deterministic file ops.

Errors at any step append a :class:`StepError` to ``WakeState.errors``
rather than throwing — the wake always closes via Step 7.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional, Union


__all__ = [
    # Wake state
    "WakeState",
    "StepError",
    # Actions (Step 2 outputs)
    "PromoteToVault",
    "AppendToDaily",
    "CreateConflictNote",
    "RouteToSurface",
    "Discard",
    "Action",
    # Diffs (Step 4 outputs)
    "FrontmatterChange",
    "WikilinkFix",
    "SectionEdit",
    "Diff",
    # Side-check + surface payloads
    "SurfacePayload",
    "SideCheckResult",
    "SideCheckResults",
    # Step + wake-summary results
    "InboxResult",
    "StepResult",
    "WakeSummary",
]


# ---------------------------------------------------------------------------
# Action union — what classify_and_route_note returns per note
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromoteToVault:
    """Promote the note's content to a new (or existing) vault path.

    ``target_path`` is relative to the mind dir (e.g.
    ``cortex-memory/people/jason.md``). ``new_content`` replaces the
    file body; if the file already exists, ``apply_action`` appends
    rather than overwriting (preserving prior content).
    """

    target_path: pathlib.Path
    new_content: str
    reason: str = ""


@dataclass(frozen=True)
class AppendToDaily:
    """Append a single bullet line to today's daily.

    ``line`` is the bullet body (no leading "- "). The daily lives at
    ``cortex-memory/dailies/<today>.md``.
    """

    line: str
    reason: str = ""


@dataclass(frozen=True)
class CreateConflictNote:
    """File a conflict note in ``cortex-memory/conflicts/``.

    ``slug`` becomes part of the filename; ``body`` is the markdown
    body (the dispatcher prepends frontmatter).
    """

    slug: str
    body: str
    reason: str = ""


@dataclass(frozen=True)
class RouteToSurface:
    """Route the note to ``inner/surface/`` for Speaking-side attention.

    ``surface_payload`` is a dict carrying ``surface_type``, ``body``,
    and any ``extra_frontmatter`` — fed through to
    :func:`alice_thinking.design_pipeline.write_surface`.
    """

    surface_payload: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class Discard:
    """Discard the note. The dispatcher still consumes (moves to
    ``.consumed/``) so the inbox doesn't fill up."""

    reason: str = ""


# Union of everything the LLM classifier can return.
Action = Union[
    PromoteToVault,
    AppendToDaily,
    CreateConflictNote,
    RouteToSurface,
    Discard,
]


# ---------------------------------------------------------------------------
# Diff types — what produce_grooming_diff returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontmatterChange:
    """One frontmatter key change. ``new_value`` may be ``None`` to
    remove the key entirely."""

    key: str
    new_value: Optional[str]


@dataclass(frozen=True)
class WikilinkFix:
    """Replace ``old_target`` with ``new_target`` everywhere it appears
    as a wikilink target. If ``old_target`` is missing the section's
    full ``[[...]]`` enclosure, the apply_diff helper still finds it.

    Display text after the pipe is preserved.
    """

    old_target: str
    new_target: str


@dataclass(frozen=True)
class SectionEdit:
    """Replace the section under ``heading`` with ``new_body``.

    ``heading`` is matched case-insensitively against ATX-style
    markdown headings (``#`` / ``##`` / ...). ``new_body`` is the
    section content (no heading line; the heading is preserved).
    """

    heading: str
    new_body: str


@dataclass(frozen=True)
class Diff:
    """Structured diff for a single grooming-target file.

    Apply order:

    1. :attr:`frontmatter_changes` — applied first so subsequent edits
       see the canonical frontmatter shape.
    2. :attr:`section_edits` — applied to the body before wikilink
       fixes so the link replacement also catches links inside any
       newly-written section text.
    3. :attr:`wikilink_fixes` — global string replacements, last so
       they pick up links in section_edits' new bodies as well.
    """

    frontmatter_changes: list[FrontmatterChange] = field(default_factory=list)
    wikilink_fixes: list[WikilinkFix] = field(default_factory=list)
    section_edits: list[SectionEdit] = field(default_factory=list)
    rationale: str = ""

    def is_empty(self) -> bool:
        return not (
            self.frontmatter_changes or self.wikilink_fixes or self.section_edits
        )


# ---------------------------------------------------------------------------
# Side-check + surface payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfacePayload:
    """One surface payload — gets written by ``emit_surfaces`` (Step 6).

    Mirrors :func:`alice_thinking.design_pipeline.write_surface`'s
    expected shape so we reuse that helper.
    """

    surface_type: str
    body: str
    extra_frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SideCheckResult:
    """Per-branch side-check result.

    ``name`` is one of ``"stale_finding_lint"``, ``"shadow_neighbor"``,
    ``"conflict_scan"``. ``ok=True, action=None`` means "branch ran and
    found nothing" (silent null result is correct for Stage B side
    checks). ``ok=False`` means the branch errored or timed out.
    """

    name: str
    ok: bool
    action_summary: Optional[str] = None
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    error: Optional[str] = None


@dataclass(frozen=True)
class SideCheckResults:
    """Aggregate of the three side-check branches."""

    stale_finding_lint: Optional[SideCheckResult]
    shadow_neighbor: Optional[SideCheckResult]
    conflict_scan: Optional[SideCheckResult]

    def all(self) -> list[SideCheckResult]:
        return [
            r
            for r in (
                self.stale_finding_lint,
                self.shadow_neighbor,
                self.conflict_scan,
            )
            if r is not None
        ]


# ---------------------------------------------------------------------------
# Wake state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepError:
    """One step failure entry on :attr:`WakeState.errors`."""

    step: str
    error_type: str  # "timeout" | "exception" | "error"
    message: str


@dataclass
class WakeState:
    """Per-wake mutable state. Steps thread this through; some attach
    side-effect summaries (``inbox_actions``, ``surface_payloads``) so
    later steps can read them without re-reading the filesystem.

    ``errors`` is the failure log — appended to by :class:`StageBRunner`
    on per-step error/timeout. Final wake summary reports
    ``len(errors)`` as ``steps_failed``.
    """

    mind_dir: pathlib.Path
    state_dir: pathlib.Path
    wake_file_path: Optional[pathlib.Path]
    mode: str
    now: _dt.datetime
    inbox_files: list[pathlib.Path] = field(default_factory=list)
    vault_health: Optional[dict[str, Any]] = None
    active_thread: Optional[str] = None
    inbox_actions: list[Action] = field(default_factory=list)
    grooming_target: Optional[pathlib.Path] = None
    grooming_diff: Optional[Diff] = None
    side_check_results: Optional[SideCheckResults] = None
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    errors: list[StepError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboxResult:
    """Step 2 (drain_inbox) result."""

    actions: list[Action]
    consumed_paths: list[pathlib.Path]
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    per_note_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StepResult:
    """Generic per-step outcome — produced by the runner, written to
    telemetry, and also passed to :func:`emit_surfaces` (Step 6) +
    :func:`close` (Step 7) so they see the full step record.
    """

    step: str
    ok: bool
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True)
class WakeSummary:
    """Final wake summary. Written by :func:`close` (Step 7)."""

    steps: list[StepResult]
    actions_total: int
    surfaces_emitted: int
    duration_ms: int
    errors: list[StepError] = field(default_factory=list)
    summary_path: Optional[pathlib.Path] = None

    @property
    def steps_ok(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def steps_failed(self) -> int:
        return sum(1 for s in self.steps if not s.ok)
