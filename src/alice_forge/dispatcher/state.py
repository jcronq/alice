"""Dispatcher state persistence.

:class:`DispatcherState` is the in-memory view of the dispatcher's
dedup ledgers — issue-number lists for hello/verify-failed/study-hint
posts, the design-revision counters, etc. :func:`load_state` and
:func:`save_state` round-trip the dataclass to/from JSON at
``/state/worker/sm-dispatcher-state.json`` (the path is injected by
:func:`alice_forge.dispatcher.run`).

The schema version (:data:`STATE_VERSION`) and the FIFO cap
(:data:`SEEN_ISSUE_CAP`) live in :mod:`alice_forge.dispatcher.constants`;
this module just reads them.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

from alice_forge.dispatcher.constants import SEEN_ISSUE_CAP, STATE_VERSION


@dataclass
class DispatcherState:
    """In-memory view of the dispatcher's persisted state.

    ``hello_commented`` is the FIFO list of issue numbers we've already
    posted the dispatcher-hello on. Insertion-ordered so the oldest
    fall off first when we hit :data:`SEEN_ISSUE_CAP`.

    ``verify_failed_posted`` (issue #128) is the FIFO list of issue
    numbers we've already posted a ``[SM] verify-failed`` comment on
    while the issue remained at ``sm:reviewing``. Without this dedup,
    every dispatcher cadence would re-post the failure (CI stays green
    on the same merge commit, so the verifier keeps running). Cleared
    implicitly when the issue eventually transitions out of reviewing.

    ``needs_study_hinted`` (issue #157) is the FIFO list of issue
    numbers we've already written a hint file + posted a
    ``[SM] study-hint-written`` audit comment on while the issue carried
    ``sm:needs_study``. Mirrors ``hello_commented`` — same FIFO eviction
    semantics. The defensive comment-prefix scan in
    :func:`_process_needs_study` handles state-file resets so a single
    cycle can't double-fire the hint.

    ``design_revisions`` (issue #164) maps issue number → count of
    ``[SM] design-revise`` bounces seen at the ``sm:design_review``
    gate. Capped at :data:`DESIGN_REVISION_CAP` to prevent an infinite
    design/revise loop; the entry is cleared on a successful
    ``design-approved`` transition (so a future re-entry into the
    design lane starts fresh).

    ``rebase_attempted`` (issue #173) is the FIFO list of issue numbers
    we've already fired a Tier 2 rebase spawn for at ``sm:reviewing``.
    Used to detect "spawned worker died without resolving the conflict"
    on a follow-up cycle: if the entry is set, no live spawn dir
    exists, and the PR is still CONFLICTING, escalate to Tier 3.

    ``rebase_escalated_posted`` (issue #173) is the FIFO list of issue
    numbers where the Tier 3 escalation comment has already been
    posted. Without this dedup the dispatcher would re-escalate on
    every cadence while the operator is still triaging.

    ``exit_required_posted`` (issue #174) is the FIFO list of issue
    numbers where the ``[SM] exit-transition-required`` reminder has
    already been posted while the issue sat at ``sm:done`` + still
    OPEN + ``art:research_note`` with no exit-transition comment. The
    research_note close path stays a no-op on subsequent passes until
    the worker (or a human) lands the exit-transition; the reminder
    must not re-fire every cadence.

    ``triage_surfaced`` (issue #235) is the FIFO list of issue numbers
    where the draft handler has already written a triage surface to
    ``inner/surface/`` asking Speaking to decide ``[SM] route-to-study``
    vs. close-as-rejected. Without this dedup the dispatcher would
    re-emit a fresh surface every 60-second cadence, burying the
    surface dispatcher. Cleared when the issue transitions out of
    ``sm:draft`` so a future re-entry (e.g. operator manually labels a
    closed issue back to draft) starts fresh.
    """

    version: int = STATE_VERSION
    hello_commented: list[int] = field(default_factory=list)
    verify_failed_posted: list[int] = field(default_factory=list)
    needs_study_hinted: list[int] = field(default_factory=list)
    design_revisions: dict[int, int] = field(default_factory=dict)
    rebase_attempted: list[int] = field(default_factory=list)
    rebase_escalated_posted: list[int] = field(default_factory=list)
    exit_required_posted: list[int] = field(default_factory=list)
    triage_surfaced: list[int] = field(default_factory=list)

    def has_hello(self, number: int) -> bool:
        return number in self.hello_commented

    def mark_hello(self, number: int) -> None:
        # Move-to-front semantics would defeat FIFO eviction. Append-only.
        if number in self.hello_commented:
            return
        self.hello_commented.append(number)
        # Hard cap — drop oldest first.
        if len(self.hello_commented) > SEEN_ISSUE_CAP:
            overflow = len(self.hello_commented) - SEEN_ISSUE_CAP
            del self.hello_commented[:overflow]

    def has_verify_failed(self, number: int) -> bool:
        return number in self.verify_failed_posted

    def mark_verify_failed(self, number: int) -> None:
        if number in self.verify_failed_posted:
            return
        self.verify_failed_posted.append(number)
        if len(self.verify_failed_posted) > SEEN_ISSUE_CAP:
            overflow = len(self.verify_failed_posted) - SEEN_ISSUE_CAP
            del self.verify_failed_posted[:overflow]

    def clear_verify_failed(self, number: int) -> None:
        # Called when the issue transitions out of sm:reviewing — the
        # dedup signal is scoped to "still pending verification on this
        # merge". Once the label changes (to done after a retry pass,
        # or to building if CI flips red), the ledger entry is stale.
        try:
            self.verify_failed_posted.remove(number)
        except ValueError:
            pass

    def has_needs_study_hint(self, number: int) -> bool:
        return number in self.needs_study_hinted

    def mark_needs_study_hint(self, number: int) -> None:
        if number in self.needs_study_hinted:
            return
        self.needs_study_hinted.append(number)
        if len(self.needs_study_hinted) > SEEN_ISSUE_CAP:
            overflow = len(self.needs_study_hinted) - SEEN_ISSUE_CAP
            del self.needs_study_hinted[:overflow]

    def design_revision_count(self, number: int) -> int:
        return self.design_revisions.get(number, 0)

    def bump_design_revisions(self, number: int) -> int:
        """Increment the revision counter for ``number`` and return the new value."""
        new = self.design_revisions.get(number, 0) + 1
        self.design_revisions[number] = new
        return new

    def clear_design_revisions(self, number: int) -> None:
        self.design_revisions.pop(number, None)

    def has_rebase_attempted(self, number: int) -> bool:
        return number in self.rebase_attempted

    def mark_rebase_attempted(self, number: int) -> None:
        if number in self.rebase_attempted:
            return
        self.rebase_attempted.append(number)
        if len(self.rebase_attempted) > SEEN_ISSUE_CAP:
            overflow = len(self.rebase_attempted) - SEEN_ISSUE_CAP
            del self.rebase_attempted[:overflow]

    def clear_rebase_attempted(self, number: int) -> None:
        # Cleared on a successful Tier 1 rebase OR when the issue leaves
        # sm:reviewing — the conflict episode is closed and a future
        # re-entry should start fresh.
        try:
            self.rebase_attempted.remove(number)
        except ValueError:
            pass

    def has_rebase_escalated(self, number: int) -> bool:
        return number in self.rebase_escalated_posted

    def mark_rebase_escalated(self, number: int) -> None:
        if number in self.rebase_escalated_posted:
            return
        self.rebase_escalated_posted.append(number)
        if len(self.rebase_escalated_posted) > SEEN_ISSUE_CAP:
            overflow = len(self.rebase_escalated_posted) - SEEN_ISSUE_CAP
            del self.rebase_escalated_posted[:overflow]

    def has_exit_required(self, number: int) -> bool:
        return number in self.exit_required_posted

    def mark_exit_required(self, number: int) -> None:
        if number in self.exit_required_posted:
            return
        self.exit_required_posted.append(number)
        if len(self.exit_required_posted) > SEEN_ISSUE_CAP:
            overflow = len(self.exit_required_posted) - SEEN_ISSUE_CAP
            del self.exit_required_posted[:overflow]

    def clear_exit_required(self, number: int) -> None:
        # Cleared once the issue actually closes — a future re-open is
        # an aberration the next pass will handle from scratch.
        try:
            self.exit_required_posted.remove(number)
        except ValueError:
            pass

    def has_triage_surfaced(self, number: int) -> bool:
        return number in self.triage_surfaced

    def mark_triage_surfaced(self, number: int) -> None:
        if number in self.triage_surfaced:
            return
        self.triage_surfaced.append(number)
        if len(self.triage_surfaced) > SEEN_ISSUE_CAP:
            overflow = len(self.triage_surfaced) - SEEN_ISSUE_CAP
            del self.triage_surfaced[:overflow]

    def clear_triage_surfaced(self, number: int) -> None:
        # Called when the issue leaves ``sm:draft`` (Speaking routed or
        # closed it). A future re-entry into draft should start fresh.
        try:
            self.triage_surfaced.remove(number)
        except ValueError:
            pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hello_commented": list(self.hello_commented),
            "verify_failed_posted": list(self.verify_failed_posted),
            "needs_study_hinted": list(self.needs_study_hinted),
            # Keys are stringified for JSON-stability; load_state coerces back.
            "design_revisions": {str(k): v for k, v in self.design_revisions.items()},
            "rebase_attempted": list(self.rebase_attempted),
            "rebase_escalated_posted": list(self.rebase_escalated_posted),
            "exit_required_posted": list(self.exit_required_posted),
            "triage_surfaced": list(self.triage_surfaced),
        }


def load_state(state_path: pathlib.Path) -> DispatcherState:
    """Load dispatcher state. Returns an empty skeleton on first run."""
    if not state_path.is_file():
        return DispatcherState()
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        # Corrupt state: log via stderr and start fresh. Re-firing the
        # dispatcher-hello on existing ``sm:selected`` issues once is
        # acceptable; staying broken isn't.
        print(
            f"[sm-dispatcher] state at {state_path} is corrupt — resetting",
            file=sys.stderr,
        )
        return DispatcherState()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return DispatcherState()
    raw = data.get("hello_commented") or []
    numbers: list[int] = [int(n) for n in raw if isinstance(n, int)]
    # ``verify_failed_posted`` was added in #128. Older state files
    # don't have the field; default to empty so the dispatcher keeps
    # working across the upgrade without a manual reset.
    raw_vf = data.get("verify_failed_posted") or []
    vf_numbers: list[int] = [int(n) for n in raw_vf if isinstance(n, int)]
    # ``needs_study_hinted`` was added in #157. Same forward-compat
    # default-to-empty treatment for older state files.
    raw_ns = data.get("needs_study_hinted") or []
    ns_numbers: list[int] = [int(n) for n in raw_ns if isinstance(n, int)]
    # ``design_revisions`` was added in #164. Keys are persisted as
    # strings (JSON object keys); coerce back to int and skip any
    # malformed entry so a hand-edited state file can't crash the load.
    raw_dr = data.get("design_revisions") or {}
    design_revisions: dict[int, int] = {}
    if isinstance(raw_dr, dict):
        for k, v in raw_dr.items():
            try:
                design_revisions[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
    # ``rebase_attempted`` / ``rebase_escalated_posted`` were added in
    # #173. Forward-compat default-to-empty so the dispatcher keeps
    # working across the upgrade without a manual reset.
    raw_ra = data.get("rebase_attempted") or []
    ra_numbers: list[int] = [int(n) for n in raw_ra if isinstance(n, int)]
    raw_re = data.get("rebase_escalated_posted") or []
    re_numbers: list[int] = [int(n) for n in raw_re if isinstance(n, int)]
    # ``exit_required_posted`` was added in #174. Forward-compat
    # default-to-empty so the dispatcher keeps working across the
    # upgrade without a manual reset.
    raw_er = data.get("exit_required_posted") or []
    er_numbers: list[int] = [int(n) for n in raw_er if isinstance(n, int)]
    # ``triage_surfaced`` was added in #235. Forward-compat
    # default-to-empty so older state files keep working — the first
    # pass after the upgrade will re-emit triage surfaces for any
    # still-draft issues, which is the bug-fix behaviour we want.
    raw_ts = data.get("triage_surfaced") or []
    ts_numbers: list[int] = [int(n) for n in raw_ts if isinstance(n, int)]
    return DispatcherState(
        version=STATE_VERSION,
        hello_commented=numbers,
        verify_failed_posted=vf_numbers,
        needs_study_hinted=ns_numbers,
        design_revisions=design_revisions,
        rebase_attempted=ra_numbers,
        rebase_escalated_posted=re_numbers,
        exit_required_posted=er_numbers,
        triage_surfaced=ts_numbers,
    )


def save_state(state_path: pathlib.Path, state: DispatcherState) -> None:
    """Atomically replace the state file. Caps the seen-issue list."""
    if len(state.hello_commented) > SEEN_ISSUE_CAP:
        overflow = len(state.hello_commented) - SEEN_ISSUE_CAP
        del state.hello_commented[:overflow]
    if len(state.verify_failed_posted) > SEEN_ISSUE_CAP:
        overflow = len(state.verify_failed_posted) - SEEN_ISSUE_CAP
        del state.verify_failed_posted[:overflow]
    if len(state.needs_study_hinted) > SEEN_ISSUE_CAP:
        overflow = len(state.needs_study_hinted) - SEEN_ISSUE_CAP
        del state.needs_study_hinted[:overflow]
    if len(state.rebase_attempted) > SEEN_ISSUE_CAP:
        overflow = len(state.rebase_attempted) - SEEN_ISSUE_CAP
        del state.rebase_attempted[:overflow]
    if len(state.rebase_escalated_posted) > SEEN_ISSUE_CAP:
        overflow = len(state.rebase_escalated_posted) - SEEN_ISSUE_CAP
        del state.rebase_escalated_posted[:overflow]
    if len(state.exit_required_posted) > SEEN_ISSUE_CAP:
        overflow = len(state.exit_required_posted) - SEEN_ISSUE_CAP
        del state.exit_required_posted[:overflow]
    if len(state.triage_surfaced) > SEEN_ISSUE_CAP:
        overflow = len(state.triage_surfaced) - SEEN_ISSUE_CAP
        del state.triage_surfaced[:overflow]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=state_path.parent, prefix=".sm-dispatcher-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, state_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

