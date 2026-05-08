"""Typed state + action types for the Stage B (ADK) workflow.

State threads through the workflow as a :class:`WakeState` carried on
``InvocationContext.session.state[STATE_KEY]`` (so each ADK sub-agent
mutates the same object). Each step builds a typed :class:`StepResult`
and appends it to ``WakeState.step_results``; per-step errors land on
``WakeState.errors`` rather than raising — the wake always closes via
the final ``CloseAgent`` step.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional, Union


__all__ = [
    "STATE_KEY",
    "WakeState",
    "StepError",
    "PromoteToVault",
    "AppendToDaily",
    "CreateConflictNote",
    "RouteToSurface",
    "Discard",
    "Action",
    "FrontmatterChange",
    "WikilinkFix",
    "SectionEdit",
    "Diff",
    "SurfacePayload",
    "SideCheckResult",
    "SideCheckResults",
    "InboxResult",
    "StepResult",
    "WakeSummary",
]


# Key under which the workflow's WakeState lives in ADK session.state.
# Stored as the live Python object (not serialized) — the ADK session
# state dict accepts arbitrary values and we never round-trip via JSON.
STATE_KEY = "stage_b_wake_state"


# ---------------------------------------------------------------------------
# Action union — what classify_and_route_note returns per note
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromoteToVault:
    target_path: pathlib.Path
    new_content: str
    reason: str = ""


@dataclass(frozen=True)
class AppendToDaily:
    line: str
    reason: str = ""


@dataclass(frozen=True)
class CreateConflictNote:
    slug: str
    body: str
    reason: str = ""


@dataclass(frozen=True)
class RouteToSurface:
    surface_payload: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class Discard:
    reason: str = ""


Action = Union[
    PromoteToVault, AppendToDaily, CreateConflictNote, RouteToSurface, Discard
]


# ---------------------------------------------------------------------------
# Diff types — what produce_grooming_diff returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontmatterChange:
    key: str
    new_value: Optional[str]


@dataclass(frozen=True)
class WikilinkFix:
    old_target: str
    new_target: str


@dataclass(frozen=True)
class SectionEdit:
    heading: str
    new_body: str


@dataclass(frozen=True)
class Diff:
    """Structured diff for one grooming-target file.

    Apply order: frontmatter → section_edits → wikilink_fixes (so link
    replacements run last and catch links inside replaced section
    bodies).
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
    surface_type: str
    body: str
    extra_frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SideCheckResult:
    name: str
    ok: bool
    action_summary: Optional[str] = None
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    error: Optional[str] = None


@dataclass(frozen=True)
class SideCheckResults:
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
# Wake state + per-step results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepError:
    step: str
    error_type: str  # "timeout" | "exception" | "error"
    message: str


@dataclass
class WakeState:
    """Per-wake mutable state. Threaded through ADK's session.state by
    key :data:`STATE_KEY`; each step (BaseAgent subclass) reads + mutates
    it in place. ``apply_writes=False`` is the shadow-mode lever.
    """

    mind_dir: pathlib.Path
    state_dir: pathlib.Path
    wake_file_path: Optional[pathlib.Path]
    mode: str
    now: _dt.datetime
    apply_writes: bool = True
    inbox_files: list[pathlib.Path] = field(default_factory=list)
    vault_health: Optional[dict[str, Any]] = None
    active_thread: Optional[str] = None
    inbox_actions: list[Action] = field(default_factory=list)
    grooming_target: Optional[pathlib.Path] = None
    grooming_diff: Optional[Diff] = None
    side_check_results: Optional[SideCheckResults] = None
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    errors: list[StepError] = field(default_factory=list)
    step_results: list["StepResult"] = field(default_factory=list)


@dataclass(frozen=True)
class InboxResult:
    actions: list[Action]
    consumed_paths: list[pathlib.Path]
    surface_payloads: list[SurfacePayload] = field(default_factory=list)
    per_note_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StepResult:
    step: str
    ok: bool
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True)
class WakeSummary:
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
