"""Dispatcher run-report + dependency resolution.

:class:`RunReport` is the per-pass summary the dispatcher writes to
stderr and tests assert against. :class:`DependencyResolution` and
:func:`resolve_dependencies` (issue #176) bucket parsed
``Depends on #N`` / ``Blocked by #N`` references so the
``sm:selected`` handler can gate spawning on open hard-deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sm.dispatcher.constants import REJECTED_SM_LABEL


@dataclass
class RunReport:
    """Summary of one dispatcher pass — for tests + stderr logging."""

    polled: int = 0
    posted: int = 0
    skipped_dedup: int = 0
    skipped_trust: int = 0
    posted_numbers: list[int] = field(default_factory=list)
    transitioned: int = 0
    transitions: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (issue_number, from, to)
    # Phase 1.6 — count of stale-closed-issue sweep transitions. Counted
    # separately from ``transitioned`` so the done-line tells you at a
    # glance whether the missed-window sweep is firing.
    swept: int = 0
    # Phase 2 — count of agent spawns this pass.
    spawned: int = 0
    # Issue numbers + spawn ids for which an agent was spawned. Useful
    # for tests + dry-run reporting.
    spawn_records: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (issue_number, art_label, spawn_id or "<dry-run>")
    # Issue #127 — count of post-merge working-tree cleanups invoked on
    # the ``sm:reviewing → sm:done`` path. Logged on the done-line so
    # the operator sees whether checkouts are firing.
    cleaned_up: int = 0
    # Issue #128 — verification outcome counters. ``verify_pass`` and
    # ``verify_skip`` both allow the issue through to ``sm:done`` (the
    # latter records that no recipe matched); ``verify_failed`` holds
    # the issue at ``sm:reviewing`` for human inspection.
    verify_pass: int = 0
    verify_skip: int = 0
    verify_failed: int = 0
    # (issue_number, outcome, reason) for the done-line / tests.
    verify_records: list[tuple[int, str, str]] = field(default_factory=list)
    # Issue #157 — count of ``sm:needs_study`` hint files written this
    # pass. Tracked separately so the operator can tell at a glance
    # whether the thinking-agent has been handed fresh work.
    hinted: int = 0
    # Issue #173 — auto-rebase outcome counters for the
    # ``sm:reviewing`` × CONFLICTING handler. ``rebase_pushed`` covers
    # Tier 1 (cheap in-process rebase pushed to origin),
    # ``rebase_spawned`` covers Tier 2 (rebase failed → fresh worker
    # spawned), ``rebase_escalated`` covers Tier 3 (spawn also dead,
    # PR still conflicting → surfaced to Jason).
    rebase_pushed: int = 0
    rebase_spawned: int = 0
    rebase_escalated: int = 0
    # (issue_number, tier, reason) for the done-line / tests.
    rebase_records: list[tuple[int, str, str]] = field(default_factory=list)
    # Issue #174 — count of research_note issues closed this pass via
    # the open-done sweep + count of ``exit-transition-required``
    # reminder comments posted while waiting on the worker.
    research_closed: int = 0
    exit_required_posted: int = 0
    # Issue #176 — count of spawns skipped because at least one hard
    # dependency was still open ("blocked by #N"). Distinct from
    # ``skipped_dedup`` / ``skipped_trust`` so the done-line shows the
    # queue is gated on dependencies, not auth / dedup.
    spawn_skipped_blocked_deps: int = 0
    # Issue #235 — count of triage surfaces written this pass for
    # ``sm:draft`` issues that lacked a trusted ``[SM] route-to-study``
    # comment. Tracked separately so the operator can see at a glance
    # whether the dispatcher woke Speaking on idle drafts.
    triage_surfaced: int = 0


@dataclass(frozen=True)
class DependencyResolution:
    """Result of resolving a list of dependency issue numbers.

    Issue #176. Returned by :func:`resolve_dependencies`. The dispatcher
    uses ``rejected`` to short-circuit to ``sm:blocked`` (a closed
    rejected dep is permanent), ``blocking`` to gate spawning (open
    dep — try again next pass), and ``missing`` only as a log signal
    (a typo / moved issue is treated as resolved so the dispatcher
    doesn't stall forever on a misspelling).
    """

    blocking: tuple[int, ...]
    rejected: tuple[int, ...]
    missing: tuple[int, ...]
    resolved: tuple[int, ...]


def resolve_dependencies(
    deps: Iterable[int],
    get_issue: Callable[[int], dict[str, Any] | None],
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> DependencyResolution:
    """Look up each dep's current GH state + labels and bucket the results.

    Issue #176. ``get_issue`` is the ``(number) -> payload | None``
    callable bound to the active repo (typically
    ``lambda n: gh_get_issue(repo, n)``). The dispatcher wires it up in
    :func:`run` when ``enable_spawn`` is True; tests inject a fake.

    Buckets:

      * ``blocking``  — open issue (still needs to be resolved)
      * ``rejected``  — closed with ``sm:rejected`` (permanent block)
      * ``missing``   — ``get_issue`` returned ``None`` (404 / typo);
                        treated as resolved per spec to avoid stalling
                        the queue on a misspelled reference.
      * ``resolved``  — closed without ``sm:rejected`` (assumed
                        ``sm:done`` or otherwise satisfied).
    """
    blocking: list[int] = []
    rejected: list[int] = []
    missing: list[int] = []
    resolved: list[int] = []
    for n in deps:
        payload = get_issue(n)
        if payload is None:
            log(
                f"[sm-dispatcher] dep #{n} not found "
                f"(404 / typo / permission) — treating as resolved"
            )
            missing.append(n)
            continue
        gh_state = (payload.get("state") or "").upper()
        labels = {
            lab.get("name")
            for lab in (payload.get("labels") or [])
            if isinstance(lab, dict) and isinstance(lab.get("name"), str)
        }
        if gh_state == "OPEN":
            blocking.append(n)
            continue
        if REJECTED_SM_LABEL in labels:
            rejected.append(n)
            continue
        resolved.append(n)
    return DependencyResolution(
        blocking=tuple(blocking),
        rejected=tuple(rejected),
        missing=tuple(missing),
        resolved=tuple(resolved),
    )
