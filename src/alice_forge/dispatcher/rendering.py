"""Audit-comment renderers for the dispatcher.

Every protocol-bearing comment the dispatcher posts (``[SM] dispatcher-hello``,
``[SM] transition``, ``[SM] study-hint-written``, ``[SM] design-ready-audit``,
``[SM] verify-pass`` / ``-skip`` / ``-failed``, ``[SM] rebase-pushed`` /
``-needed`` / ``-escalated``, etc.) gets rendered by a function in this
module. Centralizing them gives the parser side in
:mod:`alice_forge.comments` exactly one place to look for shape changes.

The :data:`REBASE_*_PREFIX` constants live alongside the rebase
renderers because they're tightly coupled — the handler that consumes
them (``_process_reviewing`` → ``_handle_conflicting_pr``) imports
both via :mod:`alice_forge.dispatcher` (re-exported).
"""

from __future__ import annotations

import pathlib
from typing import Any

from alice_forge.dispatcher.constants import (
    ACTIVE_SM_LABEL,
    DESIGN_READY_AUDIT_PREFIX,
    DESIGN_REVISION_CAP,
    EXIT_TRANSITION_REQUIRED_PREFIX,
    STUDY_HINT_WRITTEN_PREFIX,
    VERIFY_FAILED_PREFIX,
    VERIFY_PASS_PREFIX,
    VERIFY_SKIP_PREFIX,
    _now_iso,
)
from alice_forge.dispatcher.trust import _label_names


def render_hello_comment(
    number: int,
    art_label: str,
    *,
    sm_label: str = ACTIVE_SM_LABEL,
    timestamp: str | None = None,
    version: int = 0,
) -> str:
    """Produce the literal ``[SM] dispatcher-hello ...`` payload."""
    ts = timestamp or _now_iso()
    return (
        f"[SM] dispatcher-hello task=#{number} state={sm_label} "
        f"art={art_label} ts={ts} v={version}"
    )


def render_transition_comment(from_state: str, to_state: str, reason: str) -> str:
    """Produce the literal ``[SM] transition ...`` payload."""
    # Strip the ``sm:`` prefix in the rendered comment to match the
    # spec example: ``from=selected to=reviewing reason="..."``.
    f_short = from_state.removeprefix("sm:")
    t_short = to_state.removeprefix("sm:")
    return f'[SM] transition from={f_short} to={t_short} reason="{reason}"'


def render_study_hint_audit_comment(
    number: int,
    note_path: pathlib.Path | str,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] study-hint-written ...`` payload.

    Posted on the issue after the dispatcher drops a hint markdown file
    into ``inner/notes/`` for the thinking-agent to pick up. The audit
    comment is the source-of-truth dedup signal — :func:`_process_needs_study`
    won't re-write the hint if it sees this prefix from a trusted author
    on a later pass, even if the local state ledger was lost.
    """
    ts = timestamp or _now_iso()
    return (
        f"{STUDY_HINT_WRITTEN_PREFIX} task=#{number} "
        f"path={note_path} ts={ts}"
    )


def render_exit_transition_required_comment(
    number: int,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] exit-transition-required ...`` payload (issue #174).

    Posted by the dispatcher on an ``art:research_note`` issue that has
    been flipped to ``sm:done`` (and not closed) but never received an
    ``[SM] exit-transition`` comment from a trusted author. The reminder
    enumerates the valid values and tells the worker / operator what
    has to land before the close fires.
    """
    ts = timestamp or _now_iso()
    return (
        f"{EXIT_TRANSITION_REQUIRED_PREFIX} task=#{number} "
        f'expected=one-of="disseminate|spawn-code|both" '
        f"ts={ts}"
    )


def render_design_ready_audit_comment(
    number: int,
    note: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] design-ready-audit ...`` payload.

    Issue #164. Posted on the issue when the dispatcher observes a
    fresh ``[SM] design-ready`` from the thinking-agent and transitions
    the issue to ``sm:design_review``. Speaking's review loop polls for
    this prefix as the "there's a design ready to review" signal. The
    ``note=`` field carries the wikilink to the draft so a human (or
    Speaking) can read it without re-parsing the agent's comment.
    """
    ts = timestamp or _now_iso()
    return (
        f"{DESIGN_READY_AUDIT_PREFIX} task=#{number} "
        f"note=[[{note}]] ts={ts}"
    )


def render_design_revisions_capped_comment(
    number: int,
    revisions: int,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] design-revisions-capped ...`` audit payload.

    Issue #164. Posted alongside the transition to ``sm:rejected`` when
    the design/review loop trips :data:`DESIGN_REVISION_CAP`. The audit
    line is in addition to the standard ``[SM] transition`` comment so
    operators have a self-explanatory marker when the issue surfaces in
    triage.
    """
    ts = timestamp or _now_iso()
    return (
        f"[SM] design-revisions-capped task=#{number} "
        f"count={revisions} cap={DESIGN_REVISION_CAP} ts={ts}"
    )


def render_auto_study_complete_comment(
    slug: str,
    *,
    art_label: str = "art:research_note",
) -> str:
    """Render the synthetic ``[SM] study-complete`` audit comment (issue #212).

    Posted by the dispatcher when :func:`_find_resolving_research_note`
    spots a vault note whose frontmatter resolves the issue. The
    ``auto-posted=true`` field is the audit-trail marker so anyone
    reading the comment trail can tell this transition was synthesized
    from the vault rather than authored by thinking — the parser
    happily ignores unknown key=value pairs.

    The default ``art_label`` is ``art:research_note`` because the
    synthesis path is, by construction, triggered by a research note
    living in ``cortex-memory/research/``.
    """
    return (
        f"[SM] study-complete art={art_label} "
        f"findings=[[{slug}]] auto-posted=true"
    )


def render_study_hint_note_body(issue: dict[str, Any]) -> str:
    """Render the hint-file body for ``inner/notes/sm-needs-study-issue<N>.md``.

    Minimal viable shape: YAML-like frontmatter with the bits the
    thinking-agent's wake prompt needs to pick the file up + route it
    (kind=sm-needs-study, issue=#<N>, source=alice-sm-dispatcher),
    followed by the issue title and body verbatim. The fully-baked
    prompt format lands in sub-issue #6 — this body is the contract
    surface the prompt will eventually consume.
    """
    number = issue.get("number")
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    labels = ", ".join(sorted(_label_names(issue)))
    frontmatter = (
        "---\n"
        "kind: sm-needs-study\n"
        f"issue: {number}\n"
        f"title: {title}\n"
        f"labels: [{labels}]\n"
        "source: alice-sm-dispatcher\n"
        "---\n"
    )
    return f"{frontmatter}\n# Issue #{number}: {title}\n\n{body.rstrip()}\n"


# ---------------------------------------------------------------------------
# Issue #128 — verification (smoke-test) machinery
# ---------------------------------------------------------------------------


def render_verify_comment(
    outcome: str,
    number: int,
    *,
    reason: str | None = None,
    route: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Produce a literal ``[SM] verify-{pass,skip,failed} ...`` payload.

    ``outcome`` selects the prefix; the other fields are formatted to
    match the existing ``[SM] xxx key=value ...`` shape used throughout
    the dispatcher's audit trail.
    """
    ts = timestamp or _now_iso()
    if outcome == "pass":
        return f"{VERIFY_PASS_PREFIX} task=#{number} route={route} ts={ts}"
    if outcome == "skip":
        return (
            f"{VERIFY_SKIP_PREFIX} task=#{number} "
            f'reason="{reason or "no recipe matched"}" ts={ts}'
        )
    if outcome == "failed":
        return (
            f"{VERIFY_FAILED_PREFIX} task=#{number} "
            f'reason="{reason or "verification failed"}" ts={ts}'
        )
    raise ValueError(f"unknown verify outcome: {outcome!r}")


# Issue #173 — audit-comment prefixes for the auto-rebase handler. The
# dispatcher posts one of these on the originating issue whenever it
# acts on a CONFLICTING PR so the audit trail records what happened:
#
#   * ``[SM] rebase-pushed`` — Tier 1 succeeded, force-pushed cleanly.
#   * ``[SM] rebase-needed`` — Tier 2 fired, a fresh worker was spawned
#     to resolve conflicts manually.
#   * ``[SM] rebase-escalated`` — Tier 3 fired, the spawned worker died
#     without producing a clean push; surfaced for human triage.
REBASE_PUSHED_PREFIX = "[SM] rebase-pushed"
REBASE_NEEDED_PREFIX = "[SM] rebase-needed"
REBASE_ESCALATED_PREFIX = "[SM] rebase-escalated"


def render_rebase_pushed_audit_comment(
    number: int,
    branch: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] rebase-pushed ...`` payload (Tier 1 success).

    Posted when the dispatcher's in-process auto-rebase succeeded —
    the feature branch was force-pushed with the rebased history. CI
    will re-fire on the new head; the dispatcher will pick the PR up
    again on the next cadence.
    """
    ts = timestamp or _now_iso()
    return f"{REBASE_PUSHED_PREFIX} task=#{number} branch={branch} ts={ts}"


def render_rebase_needed_audit_comment(
    number: int,
    branch: str,
    reason: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] rebase-needed ...`` payload (Tier 2 escalation).

    Posted when the cheap auto-rebase failed and the dispatcher spawned
    a fresh worker to resolve conflicts. ``reason`` is the short
    diagnostic from the rebase attempt (e.g. ``"git rebase produced
    conflicts"``).
    """
    ts = timestamp or _now_iso()
    return (
        f"{REBASE_NEEDED_PREFIX} task=#{number} branch={branch} "
        f'reason="{reason}" ts={ts}'
    )


def render_rebase_escalation_comment(
    number: int,
    branch: str,
    reason: str,
    *,
    timestamp: str | None = None,
) -> str:
    """Produce a ``[SM] rebase-escalated ...`` payload (Tier 3 surface).

    Posted when the spawned rebase worker has died and the PR is still
    CONFLICTING — the dispatcher gives up and tags the issue for human
    triage. Dedup'd by :class:`DispatcherState.rebase_escalated_posted`
    so it fires at most once per CONFLICTING episode.
    """
    ts = timestamp or _now_iso()
    return (
        f"{REBASE_ESCALATED_PREFIX} task=#{number} branch={branch} "
        f'reason="{reason}" ts={ts}'
    )

