"""Experiment-card writer.

The canonical result of an experiment is the markdown card at
``cortex-memory/experiments/<experiment_id>.md``. v2 of the design (see
``inner/notes/2026-05-11-115827-design-proposal.md``) makes this the
source of truth — the ``run_experiment`` MCP tool's return dict only
carries dispatch metadata; the card is what thinking, the viewer, and
future readers consume.

Schema (frontmatter + six markdown sections):

- ``experiment_id``, ``title``, ``hypothesis`` (1-2 sentences)
- ``status``: ``complete | failed | incomplete``
- ``dispatched_at`` / ``completed_at`` / ``duration_seconds``
- ``has_transcript`` (bool) + ``transcript_path``
- ``repo_under_test`` (or null)
- ``result_paths`` (list of /tmp paths the subagent flagged)
- ``created``, ``tags`` (always ``[experiment]``)

Sections (H2 headings, in fixed order): Abstract / Hypothesis / Method /
Results / Discussion / Conclusion. A "## Cross-references" section is
appended if the subagent supplies wikilinks via the ``result_paths``
or future ``cross_references`` field (today: a stub placeholder is
written so thinking can fill it in on a later wake).

Two writers in this module:

- :func:`write_card` — happy path. Called when the subagent's
  ``submit_result`` tool fires successfully.
- :func:`write_failed_stub_card` — fallback. Called when the subagent
  crashes / times out / exits without invoking ``submit_result``.
  Surfaces ``status: failed`` and a pointer to the transcript so
  thinking still gets an artifact to reason about (no silent failures).

Both writers are pure I/O — no logging, no event emission, no surface
write. Those side effects are owned by :mod:`.surface` so the writer is
trivial to unit-test.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import re
from typing import Any, Iterable, Optional


__all__ = [
    "CardContent",
    "DEFAULT_VAULT_EXPERIMENTS_DIR",
    "card_path_for",
    "write_card",
    "write_failed_stub_card",
]


# Default vault directory for experiment cards. Caller may override via the
# explicit ``vault_path`` argument; this constant is what the runner
# defaults to.
DEFAULT_VAULT_EXPERIMENTS_DIR = pathlib.Path(
    "/home/alice/alice-mind/cortex-memory/experiments"
)


@dataclasses.dataclass(frozen=True)
class CardContent:
    """The payload the subagent's ``submit_result`` tool produces.

    Each field maps directly to a section in the rendered card. Frontmatter
    fields are populated by the writer from dispatch metadata; this struct
    only holds the body content the subagent owns.
    """

    title: str
    abstract: str
    hypothesis: str
    method: str
    results: str
    discussion: str
    conclusion: str
    # Optional list of /tmp output paths the subagent created. Surfaced
    # in the frontmatter so the viewer can inline plots when rendering.
    result_paths: list[str] = dataclasses.field(default_factory=list)
    # Optional list of cross-reference wikilinks. The subagent typically
    # leaves this empty and thinking fills it on a later wake; carrying
    # it through the schema means a smart subagent CAN seed the section.
    cross_references: list[str] = dataclasses.field(default_factory=list)


def card_path_for(
    experiment_id: str,
    *,
    vault_path: pathlib.Path = DEFAULT_VAULT_EXPERIMENTS_DIR,
) -> pathlib.Path:
    """Return the absolute path the experiment's card will be written to."""
    return vault_path / f"{experiment_id}.md"


def _iso(dt: datetime.datetime) -> str:
    """Render an aware datetime as the ISO-8601 string the frontmatter uses.

    Naive datetimes are treated as local time. The wake-side helpers
    already produce aware datetimes; this is the belt-and-braces shim.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    # Drop microseconds — frontmatter parsers (YAML) handle them but
    # they're noise.
    return dt.replace(microsecond=0).isoformat()


def _yaml_str(value: str) -> str:
    """Render a single-line string as a YAML scalar.

    YAML is brittle around colons / leading hyphens / quotes. We always
    double-quote and escape ``\\`` and ``"`` — covers the cases the
    subagent can throw at us without pulling in PyYAML.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(items: Iterable[str]) -> str:
    """Render a list of strings as a YAML inline flow list."""
    parts = [_yaml_str(s) for s in items]
    return "[" + ", ".join(parts) + "]"


def _sanitize_title(raw: str) -> str:
    """Normalise the H1 title — collapse whitespace, strip leading hashes
    a forgetful subagent might prepend, and cap at ~120 chars so it
    renders cleanly in the viewer's sidebar.
    """
    cleaned = re.sub(r"\s+", " ", raw).strip()
    cleaned = cleaned.lstrip("#").strip()
    return cleaned[:120] or "(untitled experiment)"


def _build_frontmatter(
    *,
    experiment_id: str,
    title: str,
    hypothesis: str,
    status: str,
    dispatched_at: datetime.datetime,
    completed_at: Optional[datetime.datetime],
    duration_seconds: Optional[float],
    has_transcript: bool,
    transcript_path: str,
    repo_under_test: Optional[str],
    result_paths: list[str],
    tool_calls_made: Optional[int],
    created_date: Optional[datetime.date] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """Render the YAML frontmatter block for the card.

    Hand-rolled rather than PyYAML to keep dependencies minimal (the
    vault grooming tooling does likewise; see cortex-memory dailies).
    Field order is fixed to match the spec for diff-friendliness.
    """
    if created_date is None:
        created_date = dispatched_at.date()
    completed_str = _iso(completed_at) if completed_at is not None else "null"
    duration_str = (
        f"{int(duration_seconds)}" if duration_seconds is not None else "null"
    )
    tool_calls_str = f"{tool_calls_made}" if tool_calls_made is not None else "null"
    repo_str = (
        _yaml_str(repo_under_test) if repo_under_test else "null"
    )

    lines = [
        "---",
        f"experiment_id: {_yaml_str(experiment_id)}",
        f"title: {_yaml_str(title)}",
        f"hypothesis: {_yaml_str(hypothesis)}",
        f"status: {status}",
        f"dispatched_at: {_yaml_str(_iso(dispatched_at))}",
        f"completed_at: {completed_str if completed_str == 'null' else _yaml_str(completed_str)}",
        f"duration_seconds: {duration_str}",
        f"tool_calls_made: {tool_calls_str}",
        f"has_transcript: {'true' if has_transcript else 'false'}",
        f"transcript_path: {_yaml_str(transcript_path)}",
        f"repo_under_test: {repo_str}",
        f"result_paths: {_yaml_list(result_paths)}",
        f"created: {created_date.isoformat()}",
        "tags: [experiment]",
    ]
    if extra:
        for key, value in extra.items():
            if isinstance(value, str):
                lines.append(f"{key}: {_yaml_str(value)}")
            elif isinstance(value, list):
                lines.append(f"{key}: {_yaml_list(value)}")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            elif value is None:
                lines.append(f"{key}: null")
            else:
                lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _render_section(heading: str, body: str) -> str:
    """Render one ``## heading`` + body block. Strips trailing whitespace
    from body so consecutive sections don't accumulate blank lines.
    """
    cleaned = (body or "").rstrip()
    if not cleaned:
        cleaned = f"(no {heading.lower()} provided)"
    return f"## {heading}\n\n{cleaned}\n"


def _render_cross_refs(refs: list[str]) -> str:
    """Render the cross-references section. Empty list → placeholder so
    thinking can fill it later without re-parsing the card schema.
    """
    if not refs:
        return "## Cross-references\n\n_(none yet — thinking can fill these on a later wake)_\n"
    lines = "\n".join(f"- {ref}" for ref in refs)
    return f"## Cross-references\n\n{lines}\n"


def write_card(
    target_path: pathlib.Path,
    *,
    experiment_id: str,
    content: CardContent,
    dispatched_at: datetime.datetime,
    completed_at: datetime.datetime,
    duration_seconds: float,
    transcript_path: str,
    has_transcript: bool = True,
    repo_under_test: Optional[str] = None,
    tool_calls_made: Optional[int] = None,
    status: str = "complete",
) -> pathlib.Path:
    """Write a happy-path experiment card to ``target_path``.

    ``status`` defaults to ``complete`` but the subagent's
    ``submit_result`` tool MAY pass ``incomplete`` when the subagent
    knows its own conclusion is partial. ``failed`` is reserved for the
    stub writer — a subagent that calls submit_result has not failed.

    Returns the path written, so callers can chain card_path → log/event
    emission without re-deriving it.
    """
    title = _sanitize_title(content.title)
    frontmatter = _build_frontmatter(
        experiment_id=experiment_id,
        title=title,
        hypothesis=content.hypothesis.strip().splitlines()[0]
        if content.hypothesis.strip()
        else "(no hypothesis)",
        status=status,
        dispatched_at=dispatched_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        has_transcript=has_transcript,
        transcript_path=transcript_path,
        repo_under_test=repo_under_test,
        result_paths=content.result_paths,
        tool_calls_made=tool_calls_made,
    )
    body = "\n".join(
        [
            frontmatter,
            "",
            f"# {title}",
            "",
            _render_section("Abstract", content.abstract),
            _render_section("Hypothesis", content.hypothesis),
            _render_section("Method", content.method),
            _render_section("Results", content.results),
            _render_section("Discussion", content.discussion),
            _render_section("Conclusion", content.conclusion),
            _render_cross_refs(content.cross_references),
        ]
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(body)
    return target_path


def write_failed_stub_card(
    target_path: pathlib.Path,
    *,
    experiment_id: str,
    hypothesis: str,
    dispatched_at: datetime.datetime,
    completed_at: datetime.datetime,
    duration_seconds: float,
    transcript_path: str,
    failure_reason: str,
    has_transcript: bool = True,
    repo_under_test: Optional[str] = None,
) -> pathlib.Path:
    """Write a failed-stub card when the subagent never invoked submit_result.

    The point of the stub is "no silent failures" — thinking should always
    get a discoverable artifact, even when the experiment died mid-flight.
    The card body explains what we DO know (the hypothesis, the failure
    reason, the transcript path) and admits what we don't (no
    Method/Results/Discussion/Conclusion bodies).
    """
    title = f"[failed] {_sanitize_title(hypothesis or experiment_id)}"
    frontmatter = _build_frontmatter(
        experiment_id=experiment_id,
        title=title,
        hypothesis=hypothesis.strip().splitlines()[0] if hypothesis.strip() else "(no hypothesis)",
        status="failed",
        dispatched_at=dispatched_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        has_transcript=has_transcript,
        transcript_path=transcript_path,
        repo_under_test=repo_under_test,
        result_paths=[],
        tool_calls_made=None,
        extra={"failure_reason": failure_reason},
    )
    abstract_body = (
        f"Experiment failed before the subagent could call `submit_result`. "
        f"Reason: {failure_reason}. The transcript at `{transcript_path}` "
        f"may contain partial output."
    )
    body = "\n".join(
        [
            frontmatter,
            "",
            f"# {title}",
            "",
            _render_section("Abstract", abstract_body),
            _render_section("Hypothesis", hypothesis),
            _render_section("Method", "(subagent did not report a method)"),
            _render_section("Results", "(no results — see transcript)"),
            _render_section(
                "Discussion",
                "Failure mode surfaces this card automatically so thinking "
                "has something to reason about on her next wake. Consider "
                "re-dispatching with a tighter scope or inspecting the "
                "transcript for the failure mode.",
            ),
            _render_section(
                "Conclusion",
                "**Inconclusive** — the run did not complete. See "
                "`failure_reason` in frontmatter.",
            ),
            _render_cross_refs([]),
        ]
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(body)
    return target_path
