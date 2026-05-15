"""State Machine v0/v1.5/v2 dispatcher — ``gh``-driven label-driven dispatcher.

Modeled on :mod:`alice_watchers.github`. Each invocation is a single pass:

  1. Poll ``jcronq/alice`` for open issues with any ``sm:*`` label
     (``gh issue list ... --json number,title,labels,author,...``).
  2. For ``sm:selected`` issues:
     - Apply the v0 trust filter — author whitelist, exactly one
       ``sm:*`` label, at least one ``art:*`` label — all from explicit
       allow-lists so a typo (``sm:building-pleaserun``) is silently
       dropped instead of producing a fuzzy match.
     - For each unseen passing issue, post a one-time
       ``[SM] dispatcher-hello ...`` comment as audit-trail evidence
       and record the issue number in
       ``/state/worker/sm-dispatcher-state.json`` so we don't
       re-comment on the next cadence.
     - If a linked open PR exists, transition to ``sm:reviewing``
       (Phase 1.5 T1). Hello + transition can co-occur in one pass.
     - Phase 2: if the issue has not already been spawned on (no
       ``[SM] spawn-started`` comment from a trusted author), and the
       global concurrency cap has room, spawn a detached ``claude``
       CLI subprocess to actually do the work. The spawn comment is
       posted *before* the Popen so the next pass sees the dedup
       marker even if the spawn crashes immediately.
  3. For ``sm:reviewing`` issues (Phase 1.5 T2/T3):
     - If the linked PR is merged AND master CI on the merge commit
       is green → relabel ``sm:done``, close the issue.
     - If the linked PR is merged AND master CI is red → relabel
       ``sm:building`` (do NOT close, do NOT spawn anything yet).
     - If still pending or PR still open, stay.

Phase 2 adds agent spawning but does NOT handle the persona × runtime
matrix (everything spawns Claude CLI), amendments in-flight, or
session continuity across review cycles. Those land in later phases.

The script is intended to be invoked on a cadence by s6 (later phase);
right now it runs by hand via ``python -m alice_sm.dispatcher``. The
``--dry-run`` flag prints the comments / transitions / spawns that
would be made without touching GitHub or launching subprocesses —
useful for tests and manual verification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Defaults + constants
# ---------------------------------------------------------------------------

DEFAULT_REPO = "jcronq/alice"
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_STATE_FILE = "sm-dispatcher-state.json"

# Cap on the dedup list. Issue numbers are monotonic, so dropping the
# oldest first is safe — once an issue is closed and "seen," it stays
# closed; we don't need an unbounded ledger.
SEEN_ISSUE_CAP = 1000

# Pull recent open ``sm:*`` issues per poll. Bounded by the active task
# slate, not historical issues.
RECENT_ISSUE_LIMIT = 50

# v0 author whitelist. Bot identities (the eventual ``alice-bot`` GitHub
# App) land in a later phase — until then only Jason can drop tasks into
# Alice's lane.
TRUSTED_AUTHORS: frozenset[str] = frozenset({"jcronq"})

# Strict ``sm:*`` allow-list. A typo like ``sm:building-pleaserun`` must
# be skipped rather than fuzzy-matched into ``sm:building`` — drift in
# the state vocabulary corrupts the whole protocol.
SM_LABEL_WHITELIST: frozenset[str] = frozenset(
    {
        "sm:draft",
        "sm:needs_study",
        "sm:selected",
        "sm:designing",
        "sm:design_review",
        "sm:designed",
        "sm:compacting",
        "sm:building",
        "sm:reviewing",
        "sm:validating",
        "sm:done",
        "sm:rejected",
        "sm:blocked",
    }
)

# Strict ``art:*`` allow-list. Every task must declare what kind of
# artifact it produces; the dispatcher refuses to engage with tasks that
# don't.
ART_LABEL_WHITELIST: frozenset[str] = frozenset(
    {
        "art:code",
        "art:research_note",
        "art:experiment",
        "art:config_change",
    }
)

# v0 only acted on ``sm:selected``. Phase 1.5 also acts on
# ``sm:reviewing``. Other ``sm:*`` states will be handled in later
# phases (building → spawn agent, validating → quality-gate, etc.).
DRAFT_SM_LABEL = "sm:draft"
ACTIVE_SM_LABEL = "sm:selected"
REVIEWING_SM_LABEL = "sm:reviewing"
BUILDING_SM_LABEL = "sm:building"
DONE_SM_LABEL = "sm:done"
REJECTED_SM_LABEL = "sm:rejected"
BLOCKED_SM_LABEL = "sm:blocked"

# SM v2 design-pipeline states (#148, #149). The dispatcher's main
# switch falls through to the "no action this phase" branch for these
# until follow-on issues wire the thinking-agent spawn machinery and
# the per-state handlers. Adding them here makes them valid sm:*
# labels so an issue tagged e.g. sm:designing doesn't trip the
# "expected exactly one whitelisted sm:* label" trust-filter rejection.
NEEDS_STUDY_SM_LABEL = "sm:needs_study"
DESIGNING_SM_LABEL = "sm:designing"
DESIGN_REVIEW_SM_LABEL = "sm:design_review"
DESIGNED_SM_LABEL = "sm:designed"
COMPACTING_SM_LABEL = "sm:compacting"

# Issue #157 — sm:needs_study handler. The dispatcher posts a
# ``[SM] study-hint-written`` audit comment once per issue at this
# state, after writing an ``inner/notes/sm-needs-study-issue<N>.md``
# hint file for the thinking-agent to pick up on her next wake.
# Idempotency is enforced by a ledger field on :class:`DispatcherState`
# plus a defensive scan of existing audit comments (so a state-file
# reset doesn't double-fire the hint).
STUDY_HINT_WRITTEN_PREFIX = "[SM] study-hint-written"

# Directory where the thinking-agent reads inbound work. Mirrors
# ``alice_watchers.github.DEFAULT_MIND / "inner/notes"`` — kept as a
# path constant rather than imported to avoid a watcher → dispatcher
# import cycle and to let tests override per-call.
NEEDS_STUDY_HINT_DIR = pathlib.Path("/home/alice/alice-mind/inner/notes")

# Issue #212 — directory the dispatcher scans for groomed research
# notes when auto-advancing ``sm:needs_study`` issues. A note whose
# YAML frontmatter contains ``resolves_issue: <N>`` (scalar) or
# ``resolves_issues: [<N>, ...]`` (flow list) is treated as the
# study-complete signal for issue ``N`` — the dispatcher synthesizes
# the ``[SM] study-complete art=art:research_note findings=[[<slug>]]
# auto-posted=true`` audit comment so the existing comment-driven
# transition path fires on the next pass. Kept as a path constant for
# the same reasons as :data:`NEEDS_STUDY_HINT_DIR` (test override,
# avoid watcher → dispatcher import cycle).
RESEARCH_NOTES_DIR = pathlib.Path("/home/alice/alice-mind/cortex-memory/research")

# Issue #164 — design-pipeline handlers. The dispatcher caps design
# revision iterations to prevent an infinite design/review loop; the
# fourth ``[SM] design-revise`` comment trips the cap and the issue
# bounces to ``sm:rejected`` with an audit comment.
DESIGN_REVISION_CAP = 3

# Filename of the signal file the dispatcher drops into the live
# per-issue spawn dir to ask the thinking-agent to compact. The agent
# (see ``alice_thinking/cli/perissue.py``) polls for this file after
# Speaking approves the design; finding it triggers the compaction +
# BUILD-phase restart. Kept short / deterministic so the agent's
# filesystem watch doesn't need to be clever.
COMPACT_SIGNAL_FILENAME = "compact.signal"

# Audit-comment prefix the dispatcher posts when it sees a fresh
# ``[SM] design-ready`` from the thinking-agent — Speaking polls for
# this prefix to know there's a new design draft to review.
DESIGN_READY_AUDIT_PREFIX = "[SM] design-ready-audit"

# Issue #174 — research_note close path. Worker-emitted ``[SM]
# exit-transition`` comment is required from a trusted author before
# the dispatcher will close the GH issue on an ``art:research_note``
# task. ``EXIT_TRANSITION_REQUIRED_PREFIX`` is the reminder the
# dispatcher posts (once) when the issue is at ``sm:done`` + still
# OPEN but no exit-transition has been recorded yet. Dedup is
# enforced by a state-ledger field and a defensive scan of existing
# audit comments (so a state-file reset doesn't re-spam the reminder).
EXIT_TRANSITION_PREFIX = "[SM] exit-transition"
EXIT_TRANSITION_REQUIRED_PREFIX = "[SM] exit-transition-required"

# Terminal ``sm:*`` states — the dispatcher's sweep pass leaves these
# alone. Non-terminal labels on a *closed* issue indicate a missed
# transition (Phase 1.6 sweep target).
TERMINAL_SM_LABELS: frozenset[str] = frozenset({DONE_SM_LABEL, REJECTED_SM_LABEL})
NON_TERMINAL_SM_LABELS: frozenset[str] = SM_LABEL_WHITELIST - TERMINAL_SM_LABELS

# Schema version of the state file. Bump if the structure changes
# incompatibly.
STATE_VERSION = 1


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Phase 2 — agent spawn constants
# ---------------------------------------------------------------------------

# Per-(state, artifact) spawn config. Each entry tells the dispatcher
# how to frame the spawn prompt and which spawn machinery to invoke
# for one ``(sm:*, art:*)`` combination:
#
#   * ``persona`` — selects the spawn machinery the dispatcher invokes:
#     ``"worker"`` for the v1 claude-cli code-worker pool
#     (:func:`spawn_agent`); ``"thinking"`` for the per-issue design
#     agent (:func:`spawn_thinking_agent`); ``"speaking"`` for the
#     per-issue build agent (:func:`spawn_speaking_agent`); ``"reviewer"``
#     for the structured-output code-review sub-agent invoked from
#     ``_process_reviewing`` (wired separately).
#   * ``runtime`` — label rendered into the audit comment so the audit
#     trail records which runtime executed the spawn (claude-cli for
#     the v1 pool, claude-agent-sdk for the SM v2 thinking/speaking
#     lanes).
#   * ``phase`` (optional) — per-issue phase the SDK lanes pass to the
#     shim entrypoint. Unused for the v1 worker pool.
#   * ``system_prompt_role`` — short role label rendered into the
#     v1 worker prompt header (the claude-cli runtime). Ignored by the
#     SDK lanes — those compose their own prompts in
#     :func:`compose_thinking_spawn_prompt` and
#     :func:`compose_speaking_spawn_prompt`.
#   * ``instruction_trailer`` — final instructions appended after the
#     issue body in the v1 worker prompt. ``{issue_number}`` is the
#     substitution token. Ignored by the SDK lanes.
#   * ``system_prompt_module`` (optional) — dotted path to a system
#     prompt constant when the agent is a structured-output sub-agent
#     (e.g., the code reviewer). Consumed by the
#     ``(sm:reviewing, art:code)`` reviewer wiring (separate sub-issue).
#
# Sub-issue 7 (#186) — SM v2 SPAWN_MAP cutover. The
# ``(sm:selected, art:code)`` row routes to the per-issue thinking-agent
# designer (was v1 claude-cli code-worker pre-cutover). The new
# ``(sm:designed, art:code)`` row routes to the speaking-agent builder.
# Other ``(sm:selected, art:*)`` rows still spawn the v1 worker pool.
SPAWN_MAP: dict[tuple[str, str], dict[str, str]] = {
    # SM v2 design lane. The per-issue thinking-agent reads the issue,
    # produces a design note at
    # ``~/alice-mind/cortex-memory/designs/<date>-issue<N>-<slug>.md``,
    # and posts ``[SM] design-ready note=[[<wikilink>]]`` to advance the
    # issue to ``sm:design_review``.
    ("sm:selected", "art:code"): {
        "persona": "thinking",
        "runtime": "claude-agent-sdk:opus",
        "phase": "per_issue_design",
    },
    ("sm:selected", "art:config_change"): {
        "persona": "worker",
        "runtime": "claude-cli",
        "system_prompt_role": "code-worker",
        "instruction_trailer": (
            "Open a PR titled appropriately with `Closes #{issue_number}` "
            "in the body. Self-merge once CI is green. Do not --no-verify."
        ),
    },
    ("sm:selected", "art:research_note"): {
        "persona": "worker",
        "runtime": "claude-cli",
        "system_prompt_role": "research-writer",
        "instruction_trailer": (
            "Produce a research note at "
            "~/alice-mind/cortex-memory/research/<date>-<slug>.md. After "
            "writing the note, edit issue #{issue_number} to relabel "
            "sm:selected → sm:done and post a "
            "`[SM] transition from=selected to=done reason=\"research "
            "note at <path>\"` comment."
        ),
    },
    ("sm:selected", "art:experiment"): {
        "persona": "worker",
        "runtime": "claude-cli",
        "system_prompt_role": "research-writer",
        "instruction_trailer": (
            "Same as research_note for v1. Produce a note with "
            "hypothesis/null/verdict frontmatter; transition to done "
            "when complete."
        ),
    },
    # SM v2 build lane. The per-issue speaking-agent loads the approved
    # design note, dispatches the actual code change to a sub-agent via
    # the Task tool, and posts ``[SM] build-complete pr=<url>`` when the
    # sub-agent has opened a draft PR. The dispatcher transitions the
    # issue ``sm:designed → sm:building`` at spawn time so
    # :func:`_process_building` picks the linked PR up on the next pass.
    ("sm:designed", "art:code"): {
        "persona": "speaking",
        "runtime": "claude-agent-sdk:opus",
        "phase": "per_issue_build",
    },
    # Issue #107 — code-quality reviewer for PRs at sm:reviewing. The
    # ``system_prompt_module`` is the dotted import path to
    # :data:`alice_speaking.review.code_reviewer.CODE_REVIEWER_SYSTEM_PROMPT`;
    # the dispatcher's ``(sm:reviewing, art:code)`` reviewer wiring (a
    # separate sub-issue) loads it and drives a Sonnet sub-agent that
    # returns the structured JSON verdict defined in that module.
    ("sm:reviewing", "art:code"): {
        "persona": "reviewer",
        "runtime": "claude-agent-sdk:sonnet",
        "system_prompt_role": "code-reviewer",
        "system_prompt_module": (
            "alice_speaking.review.code_reviewer:CODE_REVIEWER_SYSTEM_PROMPT"
        ),
        "instruction_trailer": (
            "Review the PR linked from issue #{issue_number}. Return a "
            "single STRICT JSON object matching the schema in your system "
            "prompt — no markdown fences, no prose. ``verdict: approved`` "
            "means the dispatcher will close the issue at "
            "sm:reviewing → sm:done; ``verdict: needs_revision`` means "
            "sm:reviewing → sm:building."
        ),
    },
}

# Cap on simultaneously running claude subprocess spawns. Excess
# eligible ``sm:selected`` issues stay queued until the next dispatcher
# pass — back-pressure rather than crash-on-overload.
MAX_CONCURRENT_SPAWNS = 2

# Per-spawn workdir. One subdir per spawn id, with ``prompt.txt``,
# ``pidfile``, ``stdout.log``, ``stderr.log``. Dead spawns get moved
# under ``.finished/<id>/`` by :func:`count_running_spawns` so the live
# count stays accurate on the next pass.
SPAWN_DIR = pathlib.Path("/state/worker/sm-dispatcher-spawns")

# The ``claude`` binary used to launch worker agents. Issue #101's
# original spec named ``/opt/alice-venv/bin/claude`` but the live host
# ships ``/usr/bin/claude``. We resolve at run time: prefer the spec'd
# path if it exists, fall back to the on-PATH binary.
CLAUDE_BIN_PREFERRED = "/opt/alice-venv/bin/claude"
CLAUDE_BIN_FALLBACK = "claude"

# Prefix on the audit-trail comment that signals "we've already spawned
# an agent on this issue". The next pass's
# :func:`gh_find_unspawned_selected_issues` filters on this prefix +
# trusted-author authorship to dedup.
SPAWN_STARTED_PREFIX = "[SM] spawn-started"

# ---------------------------------------------------------------------------
# Issue #156 — per-issue thinking-agent spawn (SM v2 design pipeline)
# ---------------------------------------------------------------------------
#
# The SM v2 pipeline replaces the v1 claude-cli code-worker on
# ``(sm:selected, art:code)`` with a long-lived per-issue thinking-agent
# that owns design + build (per
# ``cortex-memory/research/2026-05-13-sm-v2-pipeline-revision.md`` §3 Q1).
# Spawn machinery lives in a sibling pool to the v1 worker spawn dir so
# the two lanes have independent concurrency caps (Q4): a multi-hour
# design loop must not block one-shot research-writer dispatches and
# vice versa.
#
# This issue (#156) only ships the spawn machinery — :func:`spawn_thinking_agent`,
# the live-spawn / count helpers, and a placeholder entrypoint shim. The
# wire-up into ``_process_selected`` is sub-issue 7 (the SPAWN_MAP
# cutover). The real entrypoint script is sub-issue 3.

# Separate spawn dir for the thinking lane. Same on-disk shape as
# :data:`SPAWN_DIR` (per-spawn subdir with ``prompt.txt`` / ``pidfile`` /
# ``stdout.log`` / ``stderr.log`` / ``session_id``).
SM_THINKING_SPAWN_DIR = pathlib.Path("/state/worker/sm-thinking-spawns")

# Concurrency cap for the thinking lane. Distinct from
# :data:`MAX_CONCURRENT_SPAWNS` so a multi-hour design loop can't starve
# one-shot research-writer dispatches. Configurable via env so operators
# can tune without a redeploy.
MAX_CONCURRENT_THINKING_SPAWNS = int(
    os.environ.get("ALICE_MAX_CONCURRENT_THINKING_SPAWNS", "2")
)

# Audit-comment prefix for the thinking lane. Distinct from
# :data:`SPAWN_STARTED_PREFIX` so the comments module can disambiguate
# the two spawn events without re-implementing the parser cascade.
THINKING_SPAWN_STARTED_PREFIX = "[SM] thinking-spawn-started"

# Runtime label rendered into the audit comment. The shim itself is a
# Python entrypoint, but the *agent* it boots talks to claude-agent-sdk
# at Opus depth — that's the row in the persona × runtime matrix
# (``[[2026-05-12-sm-v2-agent-type-system]]``).
THINKING_RUNTIME_LABEL = "claude-agent-sdk:opus"

# Per-issue phase the thinking-agent enters at spawn time. The dispatcher
# only handles the design phase here; build-phase entry is via the
# compaction restart (sub-issue 4) and reuses the same shim with
# ``--mode=build``.
THINKING_PHASE_PER_ISSUE_DESIGN = "per_issue_design"

# Python interpreter used to launch the thinking shim. We prefer the
# venv Python (which has claude-agent-sdk installed) and fall back to
# the ambient ``python3`` so the dispatcher still runs cleanly in a
# test or dev shell that doesn't have the venv mounted.
PYTHON_BIN_PREFERRED = "/opt/alice-venv/bin/python"
PYTHON_BIN_FALLBACK = "python3"

# Dotted module path of the thinking-mode entrypoint shim. Placeholder
# implementation lives at :mod:`alice_sm.thinking_shim`; sub-issue 3
# replaces it with the real PhaseRunner dispatch.
THINKING_SHIM_MODULE = "alice_sm.thinking_shim"

# ---------------------------------------------------------------------------
# Issue #184 — per-issue speaking-agent spawn (SM v2 build phase)
# ---------------------------------------------------------------------------
#
# Per the post-amendment to ``[[2026-05-13-sm-v2-pipeline-revision]]``
# (Jason 2026-05-13 09:51 EDT), the build phase is owned by a
# stimulus-spawned speaking-instance — NOT the long-lived thinking-agent
# that produced the design. Sibling to :func:`spawn_thinking_agent`
# (which owns the design phase, PR #159). The two lanes have independent
# concurrency caps and on-disk dirs so a multi-hour build can't starve a
# pending design draft and vice versa.
#
# This issue (#184) only ships the spawn machinery. Wiring into
# ``_process_designed`` / the SPAWN_MAP cutover is a separate sub-issue,
# as is the Sonnet code-review wiring at sm:reviewing.

# Separate spawn dir for the speaking-build lane. Same on-disk shape as
# :data:`SPAWN_DIR` and :data:`SM_THINKING_SPAWN_DIR` (per-spawn subdir
# with ``prompt.txt`` / ``pidfile`` / ``stdout.log`` / ``stderr.log`` /
# ``session_id``) so the reaper, the viewer, and ``has_live_*`` helpers
# don't need a third on-disk shape to read.
SM_SPEAKING_SPAWN_DIR = pathlib.Path("/state/worker/sm-speaking-spawns")

# Concurrency cap for the speaking-build lane. Distinct from
# :data:`MAX_CONCURRENT_SPAWNS` and :data:`MAX_CONCURRENT_THINKING_SPAWNS`
# so a long-running build can't drain capacity from one-shot research
# dispatches or the thinking-agent design loop. Configurable via env so
# operators can tune without a redeploy.
MAX_CONCURRENT_SPEAKING_SPAWNS = int(
    os.environ.get("ALICE_MAX_CONCURRENT_SPEAKING_SPAWNS", "2")
)

# Audit-comment prefix for the speaking-build lane. Distinct from
# :data:`SPAWN_STARTED_PREFIX` and :data:`THINKING_SPAWN_STARTED_PREFIX`
# so the comments module can disambiguate the three spawn events without
# a body-shape cascade. Neither is a prefix of the other.
SPEAKING_SPAWN_STARTED_PREFIX = "[SM] speaking-spawn-started"

# Runtime label rendered into the audit comment. The shim is a Python
# entrypoint; the *agent* it boots reaches for the Task/Agent tool to
# dispatch the actual implementation sub-agent (claude-agent-sdk at Opus
# depth), per the persona × runtime matrix in
# ``[[2026-05-12-sm-v2-agent-type-system]]``.
SPEAKING_RUNTIME_LABEL = "claude-agent-sdk:opus"

# Per-issue phase the speaking-agent enters at spawn time. The dispatcher
# only handles the build phase here; the design phase belongs to the
# thinking-agent (see :data:`THINKING_PHASE_PER_ISSUE_DESIGN`).
SPEAKING_PHASE_PER_ISSUE_BUILD = "per_issue_build"

# Audit-comment prefix the speaking-agent emits when its sub-agent has
# opened a draft PR. Posted from inside the shim, not the dispatcher.
# Kept here so the dedup machinery has a single source of truth for the
# wire format.
SPEAKING_BUILD_COMPLETE_PREFIX = "[SM] build-complete"

# Dotted module path of the speaking-mode entrypoint shim. Placeholder
# implementation lives at :mod:`alice_sm.speaking_shim`; the real
# PhaseRunner.PER_ISSUE_BUILD dispatch (with Task-tool sub-agent
# invocation) lands in the speaking sub-issue that replaces the shim.
SPEAKING_SHIM_MODULE = "alice_sm.speaking_shim"

# Issue #137 — worker session capture. We pre-mint a UUID per spawn and
# pass it via ``--session-id`` so the worker writes its session JSONL to
# a known file. The id is persisted in ``<spawn_dir>/session_id`` at
# launch time so the reaper (and the viewer) can find the session log
# even if the dispatcher crashes mid-pass. On reap we copy the JSONL
# from ``~/.claude/projects/<normalized-cwd>/<session_id>.jsonl`` into
# ``<spawn_dir>/session.jsonl`` so the spawn dir stays self-contained
# (survives a ``find -delete`` purge of ~/.claude/projects later).
SESSION_ID_FILENAME = "session_id"
SESSION_JSONL_FILENAME = "session.jsonl"
CLAUDE_PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Issue #128 — sm:reviewing → sm:done verification (smoke-test) gate
# ---------------------------------------------------------------------------
#
# After CI is green on the merge commit but before we relabel the issue
# sm:done, run an artifact-specific smoke test. CI catches regressions
# inside the source tree; this third tier confirms the actually-running
# system reflects the change (the canonical motivating bug: PR #119
# merged green, but the live viewer process was still serving stale
# Python — only a real HTTP probe would have caught it).
#
# v1 (this issue's minimal cut) only ships the *viewer-route* recipe:
# if the merged PR touched any file under ``src/alice_viewer/``, the
# dispatcher GETs a configured URL on the running viewer and asserts
# a marker substring is present in the response body. Anything else
# (dispatcher touches, speaking touches, art:research_note, ...) is
# treated as ``verify-skip`` — recorded in the audit comment but
# allowed through to ``sm:done``. The shape extends to more recipes
# without changing the dispatcher control flow.

VERIFY_PASS_PREFIX = "[SM] verify-pass"
VERIFY_SKIP_PREFIX = "[SM] verify-skip"
VERIFY_FAILED_PREFIX = "[SM] verify-failed"

# Path prefix that flags a PR file as a "viewer touch" — anything under
# the alice_viewer package, including templates and static assets. Kept
# narrow so dispatcher / speaking / sm changes don't accidentally
# trigger the viewer probe.
VERIFY_VIEWER_PATH_PREFIX = "src/alice_viewer/"

# Default URL hit by the viewer-route smoke test. Override with
# ``ALICE_VERIFY_VIEWER_URL``. The path is intentionally the index —
# v1 only needs to confirm the FastAPI process is alive and serving
# *some* response with the marker; per-route assertions are a follow-up.
VERIFY_VIEWER_URL_ENV = "ALICE_VERIFY_VIEWER_URL"
VERIFY_VIEWER_URL_DEFAULT = "http://localhost:7777/"

# Substring asserted in the viewer response body. Override with
# ``ALICE_VERIFY_VIEWER_MARKER``. ``</html>`` is the cheapest
# "rendered a template, didn't 500" signal short of parsing HTML.
VERIFY_VIEWER_MARKER_ENV = "ALICE_VERIFY_VIEWER_MARKER"
VERIFY_VIEWER_MARKER_DEFAULT = "</html>"

# HTTP timeout for the viewer probe. Short so a wedged viewer doesn't
# stall the whole dispatcher pass.
VERIFY_HTTP_TIMEOUT_SECONDS = 5

# Master kill-switch. ``ALICE_VERIFY_ENABLED=0`` reverts the dispatcher
# to pre-#128 behavior — straight from CI-green to ``sm:done`` with no
# smoke test. Defaults to enabled.
VERIFY_ENABLED_ENV = "ALICE_VERIFY_ENABLED"

# Shared working tree on the worker — all v1 workers checkout into this
# one repo. Issue #127: after a PR merges, the dispatcher restores this
# tree to ``BASE_BRANCH`` so the next cycle reads master and not the
# departing worker's feature branch. If we move to per-worker worktrees
# in a later phase, cleanup migrates with the worker's lifecycle and
# this constant becomes per-spawn config.
WORKER_REPO_PATH = pathlib.Path("/home/alice/alice")
BASE_BRANCH = "master"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GHCommandError(RuntimeError):
    """Raised when a ``gh`` invocation exits non-zero.

    Mirrors :class:`alice_watchers.github.GHCommandError` — we keep the
    stderr around so the auth-failure / rate-limit heuristic has
    something to sniff.
    """

    def __init__(self, returncode: int, stderr: str, args: list[str]) -> None:
        super().__init__(f"gh exited {returncode}: {stderr.strip()[:400]}")
        self.returncode = returncode
        self.stderr = stderr
        self.args = args

    @property
    def looks_like_auth_failure(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "401",
                "403",
                "bad credentials",
                "requires authentication",
                "must authenticate",
                "auth login",
            )
        )

    @property
    def looks_like_rate_limit(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "rate limit",
                "secondary rate limit",
                "api rate limit exceeded",
            )
        )


# ---------------------------------------------------------------------------
# State load/save
# ---------------------------------------------------------------------------


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
    """

    version: int = STATE_VERSION
    hello_commented: list[int] = field(default_factory=list)
    verify_failed_posted: list[int] = field(default_factory=list)
    needs_study_hinted: list[int] = field(default_factory=list)
    design_revisions: dict[int, int] = field(default_factory=dict)
    rebase_attempted: list[int] = field(default_factory=list)
    rebase_escalated_posted: list[int] = field(default_factory=list)
    exit_required_posted: list[int] = field(default_factory=list)

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
    return DispatcherState(
        version=STATE_VERSION,
        hello_commented=numbers,
        verify_failed_posted=vf_numbers,
        needs_study_hinted=ns_numbers,
        design_revisions=design_revisions,
        rebase_attempted=ra_numbers,
        rebase_escalated_posted=re_numbers,
        exit_required_posted=er_numbers,
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


# ---------------------------------------------------------------------------
# gh CLI shims (injectable for tests)
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Invoke ``gh`` with the given args, raise GHCommandError on failure.

    Returns stdout as a string. Empty stdout is returned as ``""``.
    """
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GHCommandError(returncode=-1, stderr=str(exc), args=args) from exc
    if result.returncode != 0:
        raise GHCommandError(
            returncode=result.returncode,
            stderr=result.stderr or result.stdout,
            args=args,
        )
    return result.stdout


def _sort_oldest_first(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # FIFO: oldest createdAt first so the concurrency cap is fair and
    # new arrivals don't starve queued tasks. Issues without a
    # createdAt sort last (treated as "newer than any timestamped
    # peer") so a malformed payload can't silently jump the queue.
    return sorted(
        issues,
        key=lambda i: (i.get("createdAt") or "9999-12-31T23:59:59Z", i.get("number", 0)),
    )


def gh_list_selected_issues(repo: str, *, gh_bin: str = "gh") -> list[dict[str, Any]]:
    """Return open ``sm:selected`` issues. v0 helper, retained for compat.

    Phase 1.5's actual main poll uses :func:`gh_list_sm_issues` and
    filters by label client-side so the same payload covers
    ``sm:reviewing``, ``sm:building``, etc.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--label",
        ACTIVE_SM_LABEL,
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    return _sort_oldest_first(payload)


def gh_list_sm_issues(repo: str, *, gh_bin: str = "gh") -> list[dict[str, Any]]:
    """Return all open issues with any ``sm:*`` label.

    ``gh issue list`` doesn't have an "OR across labels" flag; we use
    ``--search`` with ``label:sm:selected,sm:reviewing,...`` (comma is
    OR in the GitHub search syntax for the label qualifier when
    repeated). Simpler: pull all open issues at once and filter
    client-side. RECENT_ISSUE_LIMIT keeps the payload bounded.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        "label:sm:draft,sm:needs_study,sm:selected,sm:designing,sm:design_review,sm:designed,sm:compacting,sm:building,sm:reviewing,sm:validating",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Defensive client-side filter: the search qualifier above is OR
    # across the listed labels, but if gh ever loosens parsing we still
    # only act on issues with at least one whitelisted ``sm:*`` label.
    filtered = [
        issue
        for issue in payload
        if any(n in SM_LABEL_WHITELIST for n in _label_names(issue))
    ]
    return _sort_oldest_first(filtered)


def gh_list_stale_closed_sm_issues(
    repo: str, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return closed issues that still carry a non-terminal ``sm:*`` label.

    Phase 1.6 sweep target: when a PR with ``Closes #N`` merges fast
    enough that the dispatcher's open-PR window is missed, GitHub
    auto-closes the issue but leaves its ``sm:*`` label at whatever it
    was (typically ``sm:selected``). The main poll filters ``--state
    open`` and never sees the closed issue. This helper finds those
    strays so :func:`_process_stale_closed` can route them to the
    correct terminal state.

    Same ``--search`` OR-syntax trick as :func:`gh_list_sm_issues`,
    scoped to ``--state closed`` and to non-terminal ``sm:*`` labels.
    Defense-in-depth: also filters client-side, so a relaxed gh parse
    or stale label cache can't pull a terminal-labeled issue into the
    sweep.
    """
    search_terms = ",".join(sorted(NON_TERMINAL_SM_LABELS))
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "closed",
        "--search",
        f"label:{search_terms}",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Client-side defense: only keep issues whose label set contains at
    # least one *non-terminal* whitelisted ``sm:*`` label. A closed
    # issue at ``sm:done`` must never appear here even if the search
    # qualifier loosens upstream.
    return [
        issue
        for issue in payload
        if any(n in NON_TERMINAL_SM_LABELS for n in _label_names(issue))
    ]


def gh_list_open_done_sm_issues(
    repo: str, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return OPEN issues that carry ``sm:done`` — the #174 close-stragglers.

    The ``art:research_note`` worker flips ``sm:selected → sm:done`` directly
    (no PR, no ``sm:reviewing`` pit-stop), so the main open-issue poll —
    which only searches non-terminal ``sm:*`` labels — never sees these
    issues again and ``gh issue close`` never fires. The result: research
    items look "failed" in the viewer because the card stays in the open
    list while the work is actually done.

    This helper is the open-side companion to
    :func:`gh_list_stale_closed_sm_issues`: it returns OPEN issues whose
    ``sm:*`` label is *terminal*. The caller (:func:`_process_open_done`)
    re-validates the artifact and enforces the
    ``[SM] exit-transition`` gate before closing.
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        f"label:{DONE_SM_LABEL}",
        "--json",
        "number,title,labels,author,createdAt,body",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    # Client-side defense: only keep issues whose label set contains
    # ``sm:done``. A loosened search qualifier upstream must not pull
    # in unrelated issues.
    return [
        issue
        for issue in payload
        if DONE_SM_LABEL in _label_names(issue)
    ]


def gh_post_comment(repo: str, number: int, body: str, *, gh_bin: str = "gh") -> None:
    """Post a comment on an issue via ``gh issue comment``."""
    args = [
        gh_bin,
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body",
        body,
    ]
    _run_gh(args)


def gh_edit_labels(
    repo: str,
    number: int,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    gh_bin: str = "gh",
) -> None:
    """Add/remove labels on an issue via ``gh issue edit``."""
    args = [gh_bin, "issue", "edit", str(number), "--repo", repo]
    for label in add:
        args.extend(["--add-label", label])
    for label in remove:
        args.extend(["--remove-label", label])
    if len(args) == 6:
        # No-op: caller passed empty add/remove. Don't shell out.
        return
    _run_gh(args)


def gh_close_issue(repo: str, number: int, *, gh_bin: str = "gh") -> None:
    """Close an issue via ``gh issue close``."""
    args = [gh_bin, "issue", "close", str(number), "--repo", repo]
    _run_gh(args)


def gh_get_issue(
    repo: str, number: int, *, gh_bin: str = "gh"
) -> dict[str, Any] | None:
    """Fetch a single issue's state + labels via ``gh issue view``.

    Issue #142 — the proactive reap pass needs to know whether the
    issue behind a dead spawn dir has reached a terminal state (CLOSED
    / sm:done / sm:rejected). ``gh_list_sm_issues`` only returns OPEN
    issues, so we can't reuse the polled list to answer that question.

    Returns the raw ``{"number", "state", "labels"}`` payload, or
    ``None`` if the issue doesn't exist (404 / repo permission error /
    transport failure). A ``None`` return is the caller's signal to
    leave the spawn dir alone for this cycle and retry on the next
    pass.
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,state,labels",
    ]
    try:
        stdout = _run_gh(args)
    except GHCommandError:
        return None
    if not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def gh_list_issue_comments(
    repo: str, number: int, *, gh_bin: str = "gh"
) -> list[dict[str, Any]]:
    """Return the comment list for an issue via ``gh issue view``.

    Each entry has ``body`` and ``author.login``. Used by
    :func:`gh_find_unspawned_selected_issues` to check for the
    ``[SM] spawn-started`` audit comment.
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "comments",
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return []
    raw = payload.get("comments") or []
    if not isinstance(raw, list):
        return []
    return raw


def gh_find_unspawned_selected_issues(
    repo: str,
    *,
    list_issues: Callable[[str], list[dict[str, Any]]] | None = None,
    list_comments: Callable[[str, int], list[dict[str, Any]]] | None = None,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    spawn_prefix: str = SPAWN_STARTED_PREFIX,
) -> list[dict[str, Any]]:
    """Return open ``sm:selected`` issues with no ``[SM] spawn-started`` comment.

    Phase 2 dedup primitive — paired with :func:`spawn_agent`. The
    "we've already spawned" signal is a comment whose body starts with
    :data:`SPAWN_STARTED_PREFIX` and whose author is in
    ``trusted_authors`` (so a random commenter typing the prefix
    can't trick the dispatcher into skipping a real task).

    Both ``list_issues`` and ``list_comments`` are injectable for tests.
    """
    if list_issues is None:
        list_issues = gh_list_selected_issues
    if list_comments is None:
        list_comments = gh_list_issue_comments

    candidates = list_issues(repo)
    unspawned: list[dict[str, Any]] = []
    for issue in candidates:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        try:
            comments = list_comments(repo, number)
        except GHCommandError:
            # Defer to caller's error handling — re-raise so the main
            # loop can detect auth/rate-limit and bail. For other
            # transient errors the caller's outer try/except will skip
            # this issue.
            raise
        already_spawned = False
        for c in comments:
            body = c.get("body") if isinstance(c, dict) else None
            author = c.get("author") if isinstance(c, dict) else None
            if isinstance(author, dict):
                login = author.get("login")
            elif isinstance(author, str):
                login = author
            else:
                login = None
            if (
                isinstance(body, str)
                and body.startswith(spawn_prefix)
                and isinstance(login, str)
                and login in trusted_authors
            ):
                already_spawned = True
                break
        if not already_spawned:
            unspawned.append(issue)
    return unspawned


def gh_find_linked_pr(
    repo: str, issue_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any] | None:
    """Return the first PR referencing this issue, or None.

    Uses ``gh pr list --search "linked:issue"`` (which returns PRs that
    have a "Closes #N"-style link) and filters by
    ``closingIssuesReferences`` containing the issue number. First
    match wins; later phases may need ordering rules.

    Queries ``--state all`` so callers in the T2/T3 path can find the
    linked PR after it has merged. Callers in the T1 path (sm:selected
    → sm:reviewing) must filter by the returned ``state`` field — T1
    should only fire when the linked PR is still ``OPEN``.
    """
    args = [
        gh_bin,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--search",
        "linked:issue",
        "--json",
        "number,url,state,closingIssuesReferences",
        "--limit",
        "100",
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return None
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return None
    for pr in payload:
        refs = pr.get("closingIssuesReferences") or []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("number") == issue_number:
                return {
                    "number": pr.get("number"),
                    "url": pr.get("url"),
                    "state": pr.get("state"),
                }
    return None


def gh_get_pr_merge_status(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return ``{merged, merge_commit_oid, pr_url, head_ref_name}`` for a PR.

    ``head_ref_name`` is the source branch (the worker's feature branch
    for SM-spawned PRs) — Issue #127 uses it to delete the merged local
    branch during post-merge cleanup. ``None`` if the gh payload didn't
    return it (defensive against schema drift).
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state,mergeCommit,url,headRefName",
    ]
    stdout = _run_gh(args)
    empty = {
        "merged": False,
        "merge_commit_oid": None,
        "pr_url": None,
        "head_ref_name": None,
    }
    if not stdout.strip():
        return empty
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return empty
    merge_commit = payload.get("mergeCommit") or {}
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    head_ref = payload.get("headRefName")
    return {
        "merged": payload.get("state") == "MERGED",
        "merge_commit_oid": oid,
        "pr_url": payload.get("url"),
        "head_ref_name": head_ref if isinstance(head_ref, str) and head_ref else None,
    }


def gh_get_pr_mergeable(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return ``{mergeable, head_ref_name, head_ref_oid}`` for an open PR.

    Issue #173 — the dispatcher uses this at ``sm:reviewing`` when the
    PR is still open to decide whether to attempt an auto-rebase.

    ``mergeable`` is one of:
      * ``"MERGEABLE"`` — clean merge possible
      * ``"CONFLICTING"`` — needs rebase / manual resolution
      * ``"UNKNOWN"``    — GitHub is still computing
      * ``None``         — gh returned no payload (treat as UNKNOWN)
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "mergeable,headRefName,headRefOid",
    ]
    stdout = _run_gh(args)
    empty = {"mergeable": None, "head_ref_name": None, "head_ref_oid": None}
    if not stdout.strip():
        return empty
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return empty
    head_ref = payload.get("headRefName")
    head_oid = payload.get("headRefOid")
    return {
        "mergeable": payload.get("mergeable"),
        "head_ref_name": head_ref if isinstance(head_ref, str) and head_ref else None,
        "head_ref_oid": head_oid if isinstance(head_oid, str) and head_oid else None,
    }


def gh_get_master_ci_status(
    repo: str, commit_sha: str, *, gh_bin: str = "gh"
) -> dict[str, Any]:
    """Return master CI status for a specific commit.

    Returns ``{conclusion, run_url}`` where ``conclusion`` is:
      - ``"success"`` — all completed runs succeeded
      - ``"failure"`` — at least one completed run failed/cancelled/timed_out
      - ``"pending"`` — at least one run still in_progress/queued
      - ``None``     — no runs found (yet)
    """
    args = [
        gh_bin,
        "run",
        "list",
        "--repo",
        repo,
        "--branch",
        "master",
        "--commit",
        commit_sha,
        "--json",
        "conclusion,status,url",
        "--limit",
        "5",
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return {"conclusion": None, "run_url": None}
    payload = json.loads(stdout)
    if not isinstance(payload, list) or not payload:
        return {"conclusion": None, "run_url": None}

    failure_url: str | None = None
    pending = False
    for run in payload:
        status = (run.get("status") or "").lower()
        conclusion = (run.get("conclusion") or "").lower()
        url = run.get("url")
        # GitHub statuses: queued, in_progress, completed.
        if status != "completed":
            pending = True
            continue
        # Completed: conclusion is success / failure / cancelled /
        # timed_out / skipped / neutral / action_required.
        if conclusion in ("success", "skipped", "neutral"):
            continue
        # Anything else completed-but-not-green is a failure.
        if failure_url is None:
            failure_url = url

    if failure_url is not None:
        # Failure dominates: a single red run is enough to gate on.
        return {"conclusion": "failure", "run_url": failure_url}
    if pending:
        return {"conclusion": "pending", "run_url": None}
    # All completed runs were success/skipped/neutral.
    first_url = payload[0].get("url") if isinstance(payload[0], dict) else None
    return {"conclusion": "success", "run_url": first_url}


def gh_get_pr_files(
    repo: str, pr_number: int, *, gh_bin: str = "gh"
) -> list[str]:
    """Return the list of file paths changed by a PR.

    Used by the issue #128 verification step to decide whether the
    viewer-route smoke test applies (any path under
    ``src/alice_viewer/`` flips the recipe on). An empty list on a
    successful call is legal (a PR with only renames-as-deletes is
    unusual but not impossible); the verifier treats empty as "no
    viewer touch → skip", which is the safe default.
    """
    args = [
        gh_bin,
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "files",
    ]
    stdout = _run_gh(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        return []
    raw = payload.get("files") or []
    if not isinstance(raw, list):
        return []
    paths: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str):
                paths.append(p)
    return paths


# Callable aliases — tests inject fakes here without monkeypatching the
# module-level names.
ListIssuesFn = Callable[[str], list[dict[str, Any]]]
PostCommentFn = Callable[[str, int, str], None]
EditLabelsFn = Callable[..., None]
CloseIssueFn = Callable[[str, int], None]
FindLinkedPRFn = Callable[[str, int], dict[str, Any] | None]
PRMergeStatusFn = Callable[[str, int], dict[str, Any]]
PRMergeableFn = Callable[[str, int], dict[str, Any]]
MasterCIStatusFn = Callable[[str, str], dict[str, Any]]
ListCommentsFn = Callable[[str, int], list[dict[str, Any]]]
FindUnspawnedFn = Callable[[str], list[dict[str, Any]]]
PRFilesFn = Callable[[str, int], list[str]]
# Verifier contract: takes a PR number + the list of files it changed,
# returns a verdict dict ``{outcome, reason, route}``. ``outcome`` is
# one of ``"pass"`` / ``"skip"`` / ``"fail"`` — corresponding to the
# three audit comment shapes. ``route`` is the URL hit on pass / fail
# (None on skip).
VerifyFn = Callable[[int, list[str]], dict[str, Any]]
# (cmd_args, cwd) → CompletedProcess. ``cmd_args`` is the trailing
# argv (no leading ``git``); ``cwd`` is the repo to operate in. Tests
# inject a fake to avoid touching the real working tree.
GitRunFn = Callable[[list[str], pathlib.Path], "subprocess.CompletedProcess[str]"]
PostMergeCleanupFn = Callable[[str | None, int], None]


# ---------------------------------------------------------------------------
# Issue #127 — post-merge working-tree cleanup
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: pathlib.Path,
    *,
    timeout: int = 30,
) -> "subprocess.CompletedProcess[str]":
    """Invoke ``git -C <cwd> <args...>`` and return the CompletedProcess.

    Never raises on non-zero exit — callers inspect ``returncode`` /
    ``stderr`` and decide whether to log+continue or bail. Wraps
    ``OSError`` / ``TimeoutExpired`` as a synthetic returncode=-1 result
    so the cleanup helper has a uniform shape to inspect.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(cwd), *args],
            returncode=-1,
            stdout="",
            stderr=str(exc),
        )


def _post_merge_cleanup(
    *,
    repo_path: pathlib.Path,
    branch: str | None,
    issue_number: int,
    base_branch: str = BASE_BRANCH,
    run_git: GitRunFn = _run_git,
    log: Callable[[str], None],
) -> None:
    """Restore the worker's shared tree to ``base_branch`` after a PR merge.

    Issue #127. Called from the ``sm:reviewing → sm:done`` transition
    (i.e., only after the dispatcher has confirmed PR merged + master CI
    green). Idempotent; safe to call when already on master or when the
    feature branch has already been pulled.

    Steps (each tolerates the "already in target state" case):
      1. If the working tree has uncommitted changes — log a warning
         and skip the rest. We never want to clobber in-flight edits;
         the operator handles it manually.
      2. ``git checkout base_branch`` (skipped if already on it).
      3. ``git pull --ff-only origin base_branch``. Failure here is
         logged but non-fatal — the checkout still succeeded.
      4. ``git branch -d <branch>`` for the merged feature branch.
         Skipped if ``branch`` is None, equal to ``base_branch``, or
         already absent locally.

    All log lines use the ``[SM] checkout`` prefix for the audit trail.
    """
    log_prefix = f"[SM] checkout #{issue_number}"

    if not repo_path.is_dir():
        log(f"{log_prefix} skip: repo path missing at {repo_path}")
        return

    dirty = run_git(["status", "--porcelain"], repo_path)
    if dirty.returncode != 0:
        # If we can't tell, be defensive — don't touch the tree.
        log(
            f"{log_prefix} skip: git status failed in {repo_path} "
            f"({dirty.stderr.strip() or dirty.returncode}); leaving alone"
        )
        return
    if dirty.stdout.strip():
        log(
            f"{log_prefix} skip: uncommitted changes in {repo_path} "
            f"(branch={branch!r}); not switching — operator should resolve"
        )
        return

    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    current_branch = current.stdout.strip() if current.returncode == 0 else None
    if current.returncode != 0:
        log(
            f"{log_prefix}: could not read current branch "
            f"({current.stderr.strip() or current.returncode}); continuing"
        )

    if current_branch == base_branch:
        log(f"{log_prefix}: {repo_path} already on {base_branch}")
    else:
        checkout = run_git(["checkout", base_branch], repo_path)
        if checkout.returncode != 0:
            log(
                f"{log_prefix} failed: git checkout {base_branch} "
                f"({checkout.stderr.strip() or checkout.returncode}); "
                f"leaving tree on {current_branch!r}"
            )
            return
        log(
            f"{log_prefix}: switched {repo_path} from "
            f"{current_branch!r} to {base_branch}"
        )

    pull = run_git(["pull", "--ff-only", "origin", base_branch], repo_path)
    if pull.returncode != 0:
        log(
            f"{log_prefix}: git pull --ff-only origin {base_branch} failed "
            f"({pull.stderr.strip() or pull.returncode}); next cycle will retry"
        )
    else:
        log(f"{log_prefix}: pulled origin/{base_branch} into {repo_path}")

    if branch and branch != base_branch:
        delete = run_git(["branch", "-d", branch], repo_path)
        if delete.returncode == 0:
            log(f"{log_prefix}: deleted local branch {branch!r}")
        else:
            stderr = delete.stderr.lower()
            # "not found" / "no such branch" → already gone (the
            # previous cleanup pass got it, or the worker never created
            # a local ref). Don't escalate.
            if "not found" in stderr or "no such branch" in stderr:
                log(f"{log_prefix}: local branch {branch!r} already absent")
            else:
                log(
                    f"{log_prefix}: git branch -d {branch} failed "
                    f"({delete.stderr.strip() or delete.returncode}); "
                    f"leaving branch in place"
                )


# ---------------------------------------------------------------------------
# Issue #173 — Tier 1 auto-rebase on CONFLICTING PRs
# ---------------------------------------------------------------------------


def _attempt_auto_rebase(
    *,
    branch: str,
    repo_path: pathlib.Path,
    base_branch: str = BASE_BRANCH,
    run_git: GitRunFn = _run_git,
    log: Callable[[str], None],
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Try to rebase ``branch`` onto ``origin/<base_branch>`` and force-push.

    Issue #173 Tier 1. Returns ``{ok, reason}``:
      * ``ok=True`` — the rebase produced no conflicts AND the
        force-push (``--force-with-lease``) succeeded; ``reason`` is a
        short human-readable description for the audit comment.
      * ``ok=False`` — at least one step failed; ``reason`` describes
        which step and (when known) the offending file or stderr.

    Defensive choices:
      * Refuses to act if the working tree has uncommitted changes —
        we never want to clobber in-flight edits, even on a worker tree
        nominally owned by this loop.
      * Always tries ``git rebase --abort`` on failure so the tree
        isn't left mid-rebase for the next caller (post-merge cleanup,
        or the next dispatcher pass).
      * Always tries to restore ``base_branch`` at the end of a
        failed rebase so a follow-up cycle starts clean.

    The function returns the verdict; the caller decides whether to
    fire the Tier 2 spawn.
    """
    log_prefix = (
        f"[SM] rebase #{issue_number}"
        if issue_number is not None
        else "[SM] rebase"
    )

    if not repo_path.is_dir():
        return {
            "ok": False,
            "reason": f"worker repo path missing at {repo_path}",
        }

    dirty = run_git(["status", "--porcelain"], repo_path)
    if dirty.returncode != 0:
        return {
            "ok": False,
            "reason": (
                f"git status failed: {dirty.stderr.strip()[:200] or dirty.returncode}"
            ),
        }
    if dirty.stdout.strip():
        return {
            "ok": False,
            "reason": "worker tree dirty; refusing to rebase",
        }

    fetch = run_git(["fetch", "origin", "--prune"], repo_path)
    if fetch.returncode != 0:
        return {
            "ok": False,
            "reason": (
                f"git fetch failed: "
                f"{fetch.stderr.strip()[:200] or fetch.returncode}"
            ),
        }

    # Force-create / reset the local branch to origin/<branch> so we
    # rebase from the published tip, not from whatever stale local
    # state the worker tree happens to be on.
    checkout = run_git(
        ["checkout", "-B", branch, f"origin/{branch}"],
        repo_path,
    )
    if checkout.returncode != 0:
        # Best-effort restore to base.
        run_git(["checkout", base_branch], repo_path)
        return {
            "ok": False,
            "reason": (
                f"git checkout origin/{branch} failed: "
                f"{checkout.stderr.strip()[:200] or checkout.returncode}"
            ),
        }

    rebase = run_git(["rebase", f"origin/{base_branch}"], repo_path)
    if rebase.returncode != 0:
        # Parse stderr/stdout for the offending file (best-effort).
        offender = _extract_rebase_conflict_file(rebase.stdout, rebase.stderr)
        run_git(["rebase", "--abort"], repo_path)
        run_git(["checkout", base_branch], repo_path)
        return {
            "ok": False,
            "reason": (
                f"auto-rebase failed at {offender}"
                if offender
                else "auto-rebase produced conflicts"
            ),
        }

    push = run_git(
        ["push", "--force-with-lease", "origin", f"HEAD:{branch}"],
        repo_path,
    )
    if push.returncode != 0:
        # Push failed — leave history rebased locally but report
        # failure. The next cycle's cleanup / re-fetch will reconcile.
        run_git(["checkout", base_branch], repo_path)
        return {
            "ok": False,
            "reason": (
                f"git push --force-with-lease failed: "
                f"{push.stderr.strip()[:200] or push.returncode}"
            ),
        }

    # Restore to base so the next dispatcher pass doesn't read the
    # feature branch. Mirrors :func:`_post_merge_cleanup` — failure is
    # logged but non-fatal; the rebase + push already succeeded.
    restore = run_git(["checkout", base_branch], repo_path)
    if restore.returncode != 0:
        log(
            f"{log_prefix}: rebase+push ok but failed to restore "
            f"{base_branch}: {restore.stderr.strip()[:200] or restore.returncode}"
        )
    log(f"{log_prefix}: rebased {branch} onto origin/{base_branch} and pushed")
    return {
        "ok": True,
        "reason": f"rebased onto origin/{base_branch} and force-pushed",
    }


_REBASE_CONFLICT_FILE_RE = re.compile(
    r"CONFLICT\s*\([^)]*\)\s*:\s*Merge conflict in (\S+)"
)


def _extract_rebase_conflict_file(stdout: str, stderr: str) -> str | None:
    """Pull the first ``CONFLICT (...): Merge conflict in <path>`` filename.

    git's rebase output writes conflict notices to either stdout or
    stderr depending on the version / terminal. We scan both and return
    the first match. ``None`` if no recognisable conflict line is
    present (caller falls back to a generic reason).
    """
    for chunk in (stdout, stderr):
        if not chunk:
            continue
        m = _REBASE_CONFLICT_FILE_RE.search(chunk)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Phase 2 — spawn machinery
# ---------------------------------------------------------------------------


def resolve_claude_bin(
    *,
    preferred: str = CLAUDE_BIN_PREFERRED,
    fallback: str = CLAUDE_BIN_FALLBACK,
) -> str:
    """Return the path to the ``claude`` binary.

    Prefers the spec'd venv path when it exists; otherwise returns the
    PATH-resolved binary name (so ``subprocess.Popen`` will resolve it
    via the shell's normal lookup).
    """
    if pathlib.Path(preferred).is_file():
        return preferred
    return fallback


def _spawn_dir_is_alive(child: pathlib.Path) -> bool:
    """Return True iff ``child`` has a pidfile whose PID is still live.

    Missing/unreadable pidfile → False. ``ProcessLookupError`` or
    ``PermissionError`` from ``os.kill(pid, 0)`` → False (PID recycled
    or no longer ours). Unexpected ``OSError`` → True (be conservative
    rather than reaping a possibly-live spawn).
    """
    pidfile = child / "pidfile"
    if not pidfile.is_file():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return True
    return True


def _find_worker_session_jsonl(
    session_id: str,
    *,
    projects_dir: pathlib.Path = CLAUDE_PROJECTS_DIR,
) -> pathlib.Path | None:
    """Locate the claude CLI's session JSONL for ``session_id``.

    The CLI writes to ``~/.claude/projects/<normalized-cwd>/<id>.jsonl``;
    the normalized-cwd path depends on where the worker was launched.
    Since session IDs are UUIDs, a glob across project dirs will match
    at most one file — we don't need to know the cwd to find it.

    Returns None if the JSONL is missing (worker died before writing,
    or session persistence was disabled).
    """
    if not projects_dir.is_dir():
        return None
    for hit in projects_dir.glob(f"*/{session_id}.jsonl"):
        return hit
    return None


def _copy_session_jsonl_into_spawn(
    spawn_dir: pathlib.Path,
    *,
    log: Callable[[str], None] | None = None,
    projects_dir: pathlib.Path = CLAUDE_PROJECTS_DIR,
) -> None:
    """Copy the worker's session JSONL into ``spawn_dir/session.jsonl``.

    Called at reap time so the finished spawn dir is self-contained:
    the viewer can render the worker's full trace from the spawn dir
    alone, even after a future ``find -delete`` cleans up
    ``~/.claude/projects/``. No-op when:
      * the spawn never wrote a ``session_id`` file (older spawn dir
        from before #137)
      * the session JSONL doesn't exist (worker crashed pre-write,
        or ``--no-session-persistence`` was in play)
      * a ``session.jsonl`` is already present (idempotent — re-reaping
        an already-finished dir doesn't redo the copy)
    """
    sid_path = spawn_dir / SESSION_ID_FILENAME
    if not sid_path.is_file():
        return
    target = spawn_dir / SESSION_JSONL_FILENAME
    if target.exists():
        return
    try:
        session_id = sid_path.read_text().strip()
    except OSError:
        return
    if not session_id:
        return
    src = _find_worker_session_jsonl(session_id, projects_dir=projects_dir)
    if src is None:
        if log is not None:
            log(
                f"[sm-dispatcher] reap {spawn_dir.name}: no session "
                f"JSONL found for session_id={session_id} (worker may "
                f"have crashed before writing)"
            )
        return
    try:
        shutil.copy2(src, target)
    except OSError as exc:
        if log is not None:
            log(
                f"[sm-dispatcher] reap {spawn_dir.name}: failed to "
                f"copy session JSONL from {src}: {exc}"
            )


def _reap_spawn_dir(
    child: pathlib.Path,
    finished_root: pathlib.Path,
    *,
    log: Callable[[str], None] | None = None,
    projects_dir: pathlib.Path = CLAUDE_PROJECTS_DIR,
) -> None:
    """Move a dead spawn dir into ``finished_root/<name>``.

    On name collision, suffix ``.1``, ``.2``, ... so a previous reap
    isn't clobbered. ``OSError`` is swallowed and logged — the next
    pass will retry.

    Issue #137: before the rename, copy the worker's session JSONL into
    the spawn dir so the finished entry is self-contained.
    """
    # Copy session JSONL while the dir is still at its original path
    # (paths inside the spawn dir don't depend on the rename, but
    # doing it before the move keeps a clean failure mode — if rename
    # fails we still have the JSONL alongside the live dir for retry).
    _copy_session_jsonl_into_spawn(child, log=log, projects_dir=projects_dir)
    try:
        finished_root.mkdir(parents=True, exist_ok=True)
        target = finished_root / child.name
        if target.exists():
            i = 1
            while (finished_root / f"{child.name}.{i}").exists():
                i += 1
            target = finished_root / f"{child.name}.{i}"
        child.rename(target)
    except OSError as exc:
        if log is not None:
            log(
                f"[sm-dispatcher] could not reap dead spawn "
                f"{child}: {exc}"
            )


def count_running_spawns(
    spawn_dir: pathlib.Path = SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Return the number of live spawned subprocesses.

    Walks ``spawn_dir/*/pidfile``. Live spawns count toward the
    returned total; dead spawns are moved to ``spawn_dir/.finished/``
    so a future pass doesn't keep re-checking them.
    """
    if not spawn_dir.is_dir():
        return 0
    finished_root = spawn_dir / ".finished"
    live = 0
    for child in spawn_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == ".finished":
            continue
        if _spawn_dir_is_alive(child):
            live += 1
        else:
            _reap_spawn_dir(child, finished_root, log=log)
    return live


def has_live_spawn_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Return True iff a live spawn dir exists for ``issue_number``.

    Scans ``spawn_dir/spawn-<issue_number>-*/`` (active dir only —
    ``.finished/`` is excluded). If any matching dir has a pidfile
    pointing at a live PID, returns True. Any matching dir whose
    pidfile is missing or points at a dead PID is moved into
    ``spawn_dir/.finished/`` so it doesn't clutter future passes.

    Issue #115: previously the dispatcher dedup-ed on the
    ``[SM] spawn-started`` audit comment alone, which made the comment
    a permanent gate — a worker that died after posting the comment
    but before opening a PR could not be replaced without manual
    intervention. The comment is now an audit trail only; ground truth
    is the live spawn dir.
    """
    if not spawn_dir.is_dir():
        return False
    finished_root = spawn_dir / ".finished"
    prefix = f"spawn-{issue_number}-"
    alive = False
    for child in spawn_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == ".finished":
            continue
        if not child.name.startswith(prefix):
            continue
        if _spawn_dir_is_alive(child):
            alive = True
        else:
            _reap_spawn_dir(child, finished_root, log=log)
    return alive


def find_live_spawn_dir_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SPAWN_DIR,
) -> pathlib.Path | None:
    """Return the path of the live spawn dir for ``issue_number``, or None.

    Issue #164's design-pipeline handlers use this to drop a compaction
    signal file into the per-issue spawn dir at ``sm:designed`` entry.
    Mirrors :func:`has_live_spawn_for_issue` but returns the path
    instead of a bool. Does NOT reap dead dirs — the caller already
    ran the bool check (or, in dry-run, doesn't care).

    When multiple live dirs exist for the same issue (shouldn't happen
    in practice; the spawn machinery enforces at-most-one), the first
    one found is returned. The signal file is per-dir, so a stale
    second dir won't pick up the signal — that's a feature, not a bug.
    """
    if not spawn_dir.is_dir():
        return None
    prefix = f"spawn-{issue_number}-"
    for child in spawn_dir.iterdir():
        if not child.is_dir() or child.name == ".finished":
            continue
        if not child.name.startswith(prefix):
            continue
        if _spawn_dir_is_alive(child):
            return child
    return None


def count_running_thinking_spawns(
    spawn_dir: pathlib.Path = SM_THINKING_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Mirror of :func:`count_running_spawns` scoped to the thinking lane.

    Issue #156. The thinking and worker spawn pools are independent
    (separate concurrency caps, separate cleanup), so the dispatcher
    can't reuse the worker pool's counter — a thinking-agent that's been
    running for hours mustn't appear in the worker-pool count and
    vice versa. The on-disk shape is identical so the implementation
    is a thin wrapper.
    """
    return count_running_spawns(spawn_dir, log=log)


def has_live_thinking_spawn_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SM_THINKING_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Mirror of :func:`has_live_spawn_for_issue` scoped to the thinking lane.

    Issue #156. A live thinking-agent spawn for ``issue_number`` →
    True; stale matches are reaped into ``.finished/``. Crucially, this
    helper consults only :data:`SM_THINKING_SPAWN_DIR` — a code-worker
    spawn in :data:`SPAWN_DIR` on the same issue must NOT satisfy this
    check (the two lanes have independent dedup semantics, even though
    the SPAWN_MAP cutover in sub-issue 7 will normally route an issue
    through exactly one of them).
    """
    return has_live_spawn_for_issue(issue_number, spawn_dir, log=log)


def count_running_speaking_spawns(
    spawn_dir: pathlib.Path = SM_SPEAKING_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Mirror of :func:`count_running_spawns` scoped to the speaking-build lane.

    Issue #184. The speaking-build lane has its own concurrency cap
    (:data:`MAX_CONCURRENT_SPEAKING_SPAWNS`) so a long-running build can
    not drain capacity from the thinking-design loop or the v1
    code-worker pool. The on-disk shape is identical so the
    implementation is a thin wrapper.
    """
    return count_running_spawns(spawn_dir, log=log)


def has_live_speaking_spawn_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SM_SPEAKING_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Mirror of :func:`has_live_spawn_for_issue` scoped to the speaking lane.

    Issue #184. Returns True when ``spawn_dir`` contains a live
    ``spawn-<issue_number>-*/`` for the speaking-build lane; stale
    matches are reaped into ``.finished/`` the same way as the worker
    and thinking lanes. Consults only :data:`SM_SPEAKING_SPAWN_DIR` —
    a live thinking-agent spawn in :data:`SM_THINKING_SPAWN_DIR` on
    the same issue must NOT satisfy this check.
    """
    return has_live_spawn_for_issue(issue_number, spawn_dir, log=log)


# Issue #142 — canonical spawn dir name shape, ``spawn-<N>-<unix-ts>``.
# Used by :func:`proactive_reap_dead_spawns` to recover the issue number
# the dispatcher should look up when deciding whether a dead dir is safe
# to reap.
_SPAWN_DIR_NAME_RE = re.compile(r"^spawn-(\d+)-\d+$")


def _spawn_dir_issue_number(name: str) -> int | None:
    """Extract the issue number from a ``spawn-<N>-<ts>`` dir name.

    Returns ``None`` for any name that doesn't match the canonical
    pattern (defensive — keeps the proactive reap from accidentally
    touching unrelated dirs that happen to live alongside spawn dirs).
    """
    m = _SPAWN_DIR_NAME_RE.match(name)
    if m is None:
        return None
    return int(m.group(1))


def proactive_reap_dead_spawns(
    spawn_dir: pathlib.Path = SPAWN_DIR,
    *,
    get_issue: Callable[[int], dict[str, Any] | None],
    log: Callable[[str], None] | None = None,
    projects_dir: pathlib.Path = CLAUDE_PROJECTS_DIR,
) -> tuple[int, int]:
    """Sweep ``spawn_dir`` and reap dead dirs whose issue has moved on.

    Issue #142 — without this pass, dead spawn dirs only get reaped
    when a NEW spawn attempt fires for the same issue (via
    :func:`has_live_spawn_for_issue`) or when the dispatcher walks the
    full set for a concurrency count (via :func:`count_running_spawns`,
    which only runs on the spawn path inside ``_process_selected``).
    Once the issue closes, neither trigger fires again and the dead
    dir sits in ``active/`` indefinitely — the symptom that broke the
    /running and /runs viewer entries for #135 and #137.

    Walks ``spawn_dir/*`` (skipping ``.finished/``). For each dir with
    a dead pidfile:

      * Issue closed (any label) → reap. The task is settled; the dead
        dir is just clutter.
      * Issue at sm:done / sm:rejected (terminal) even if still
        technically open → reap.
      * Issue at sm:reviewing / sm:building / sm:validating / sm:draft
        → reap. The worker progressed past spawn (a PR is open or the
        pipeline took over); the spawn subprocess being dead is the
        normal terminal state once init has reaped it.
      * Issue still at sm:selected → leave the dir alone and log a
        WARNING. This is the "worker died mid-flight, never opened a
        PR" case — silently reaping would lose the only on-disk
        evidence (prompt.txt, stderr.log) a human needs to triage.
      * ``get_issue`` returned ``None`` (404 / transport error) →
        leave alone; the next cycle will retry.

    Live spawn dirs are never touched.

    Returns ``(reaped, stuck)`` — count of dirs moved to ``.finished/``
    and count of dirs left in place for human review.
    """
    if not spawn_dir.is_dir():
        return (0, 0)
    finished_root = spawn_dir / ".finished"
    reaped = 0
    stuck = 0
    for child in sorted(spawn_dir.iterdir()):
        if not child.is_dir() or child.name == ".finished":
            continue
        if _spawn_dir_is_alive(child):
            continue
        number = _spawn_dir_issue_number(child.name)
        if number is None:
            if log is not None:
                log(
                    f"[sm-dispatcher] proactive-reap: skipping "
                    f"{child.name} (non-canonical spawn dir name)"
                )
            continue
        issue = get_issue(number)
        if issue is None:
            if log is not None:
                log(
                    f"[sm-dispatcher] proactive-reap: could not fetch "
                    f"#{number} state — leaving {child.name} in place"
                )
            continue
        issue_state = (issue.get("state") or "").upper()
        sm_label = _current_sm_label(issue)
        if issue_state == "CLOSED" or sm_label in TERMINAL_SM_LABELS:
            if log is not None:
                log(
                    f"[sm-dispatcher] proactive-reap: #{number} "
                    f"state={issue_state or '?'} sm={sm_label} — "
                    f"reaping {child.name}"
                )
            _reap_spawn_dir(
                child, finished_root, log=log, projects_dir=projects_dir
            )
            reaped += 1
            continue
        if sm_label == ACTIVE_SM_LABEL:
            if log is not None:
                log(
                    f"[sm-dispatcher] proactive-reap WARNING: #{number} "
                    f"still at {ACTIVE_SM_LABEL} but {child.name} pid "
                    f"is dead — worker likely crashed before opening a "
                    f"PR. Leaving in place for human review."
                )
            stuck += 1
            continue
        if sm_label is None:
            if log is not None:
                log(
                    f"[sm-dispatcher] proactive-reap: #{number} has no "
                    f"single whitelisted sm:* label — leaving "
                    f"{child.name} alone"
                )
            continue
        # sm:reviewing / sm:building / sm:validating / sm:draft — the
        # spawn phase is done by definition.
        if log is not None:
            log(
                f"[sm-dispatcher] proactive-reap: #{number} at "
                f"{sm_label} (past spawn phase) — reaping {child.name}"
            )
        _reap_spawn_dir(
            child, finished_root, log=log, projects_dir=projects_dir
        )
        reaped += 1
    return reaped, stuck


def compose_spawn_prompt(
    issue: dict[str, Any],
    spawn_config: dict[str, str],
) -> str:
    """Render the full prompt text fed to the spawned ``claude`` agent.

    The prompt embeds the issue body verbatim, the artifact label, the
    issue source (author identity), and the role-specific instruction
    trailer with ``{issue_number}`` substituted.
    """
    number = issue.get("number")
    title = issue.get("title") or "(no title)"
    body = issue.get("body") or "(no body)"
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break
    login = _author_login(issue) or "(unknown)"
    source_label = f"source:{login}"

    role = spawn_config["system_prompt_role"]
    trailer = spawn_config["instruction_trailer"].format(issue_number=number)

    if role == "code-worker":
        task_framing = (
            "Your task: implement the change described above. Read the "
            "relevant code first, write a focused diff, run tests, and "
            "open a PR."
        )
    else:
        task_framing = (
            "Your task: produce the research note described above. "
            "Read prior art in the vault, write the note with proper "
            "frontmatter and wikilinks, then post the SM transition "
            "comment when finished."
        )

    # The agent name itself is intentionally left out of the literal
    # prompt — the SM task is repo-anchored, not persona-anchored, and
    # the runtime persona system owns identity rendering. The role
    # label (``code-worker`` / ``research-writer``) carries the
    # behavioral framing.
    return (
        f"You are a {role} agent working on an SM task.\n"
        f"\n"
        f"Issue: #{number}\n"
        f"Title: {title}\n"
        f"Source: {source_label}\n"
        f"Artifact type: {art_label}\n"
        f"\n"
        f"Issue body:\n"
        f"{body}\n"
        f"\n"
        f"{task_framing}\n"
        f"\n"
        f"{trailer}\n"
        f"\n"
        f"Operate as a real engineer would: read the relevant code "
        f"first, test before merging, do not bypass CI hooks. "
        f"Self-merge when CI is green (for code work) or post the "
        f"transition comment (for research work).\n"
    )


def render_spawn_started_comment(
    number: int,
    art_label: str,
    spawn_id: str,
    *,
    runtime: str = "claude-cli",
    timestamp: str | None = None,
) -> str:
    """Produce the literal ``[SM] spawn-started ...`` audit comment."""
    ts = timestamp or _now_iso()
    return (
        f"{SPAWN_STARTED_PREFIX} task=#{number} artifact={art_label} "
        f"runtime={runtime} spawn_id={spawn_id} ts={ts}"
    )


def spawn_agent(
    issue: dict[str, Any],
    art_label: str,
    repo: str,
    *,
    sm_state: str = ACTIVE_SM_LABEL,
    spawn_dir: pathlib.Path = SPAWN_DIR,
    claude_bin: str | None = None,
    post_comment: PostCommentFn = gh_post_comment,
    popen: Callable[..., Any] = subprocess.Popen,
    now_iso: Callable[[], str] = _now_iso,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] = None,  # type: ignore[assignment]
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str | None:
    """Spawn a detached ``claude`` agent for an SM issue.

    Steps (per issue #101 spec):

      1. Mint ``spawn_id = "spawn-<N>-<unix-ts>"``.
      2. Create ``spawn_dir/<spawn_id>/``.
      3. Compose the prompt + write ``prompt.txt``.
      4. Post ``[SM] spawn-started ...`` audit comment (dedup signal
         for the next dispatcher pass — posted BEFORE the Popen so a
         crash during launch still leaves the dedup marker).
      5. Launch claude detached via ``subprocess.Popen``:
         stdin=open(prompt.txt), stdout/stderr to log files,
         ``start_new_session=True`` so the agent survives the
         dispatcher exiting.
      6. Write PID to ``pidfile``.

    ``sm_state`` selects which SPAWN_MAP row to use; defaults to
    ``sm:selected`` (the v1 worker-spawn path). Issue #107 added the
    ``(sm:reviewing, art:code)`` row, which a later dispatcher change
    will route here with ``sm_state="sm:reviewing"``.

    Returns the ``spawn_id`` on success, or ``None`` if the spawn
    config is missing (unknown ``(sm_state, art:*)`` combination).
    Does NOT wait for the spawned subprocess to complete — the
    dispatcher exits immediately after the Popen returns.
    """
    if clock is None:
        clock = time.time
    spawn_config = SPAWN_MAP.get((sm_state, art_label))
    if spawn_config is None:
        log(
            f"[sm-dispatcher] no spawn config for artifact {art_label!r} "
            f"at state {sm_state!r} on #{issue.get('number')} — skipping spawn"
        )
        return None

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn on non-integer issue "
            f"number: {number!r}"
        )
        return None

    if claude_bin is None:
        claude_bin = resolve_claude_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_spawn_prompt(issue, spawn_config)
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    # Issue #137: pre-mint a session id so we can find the worker's
    # session JSONL after the fact (and copy it into the spawn dir on
    # reap). Persist BEFORE Popen so a crash mid-launch still leaves
    # the id for the reaper to consult.
    session_id = new_session_id()
    (work_dir / SESSION_ID_FILENAME).write_text(session_id)

    # Post the [SM] spawn-started audit comment FIRST. If this fails
    # we abort the spawn — without the dedup marker, the next pass
    # would re-spawn the same task. Posting before Popen means a
    # crash-during-launch still leaves the marker, which is the
    # correct dedup semantics (the dispatcher exits after Popen
    # returns; the supervisor cadence will catch the dead pidfile via
    # count_running_spawns on the next pass).
    body = render_spawn_started_comment(
        number, art_label, spawn_id, timestamp=now_iso()
    )
    try:
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post spawn-started on #{number}: "
            f"{exc} — aborting spawn"
        )
        # Re-raise so the caller (main loop) can detect auth / rate
        # limit and bail. Other errors propagate too — the spawn dir
        # is left behind (without a pidfile) and gets reaped on the
        # next pass.
        raise

    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    pidfile_path = work_dir / "pidfile"

    # Open prompt as stdin, log files as stdout/stderr. start_new_session
    # detaches the subprocess from the dispatcher's controlling
    # terminal + signal group — the dispatcher process can exit and
    # the agent keeps running.
    stdin_fh = open(prompt_path, "rb")
    stdout_fh = open(stdout_path, "wb")
    stderr_fh = open(stderr_path, "wb")
    try:
        proc = popen(
            [claude_bin, "--print", "--session-id", session_id],
            stdin=stdin_fh,
            stdout=stdout_fh,
            stderr=stderr_fh,
            start_new_session=True,
        )
    finally:
        # Close the parent's view of the FDs — the child inherits its
        # own copies. Keeping them open in the parent would mean the
        # files only fully release when the dispatcher exits.
        stdin_fh.close()
        stdout_fh.close()
        stderr_fh.close()

    pid = getattr(proc, "pid", None)
    if pid is not None:
        pidfile_path.write_text(str(pid))
    log(
        f"[sm-dispatcher] spawned {spawn_id} (pid={pid}) on #{number} "
        f"art={art_label} session_id={session_id}"
    )
    return spawn_id


def compose_rebase_prompt(
    issue: dict[str, Any],
    *,
    branch: str,
    reason: str,
    base_branch: str = BASE_BRANCH,
    repo_path: pathlib.Path = WORKER_REPO_PATH,
) -> str:
    """Render the prompt fed to a Tier 2 rebase worker.

    Issue #173. The worker's only job is to resolve conflicts on
    ``branch`` against ``base_branch`` and push the result; it must
    not merge or close anything. The dispatcher picks the PR up again
    on the next cycle once the push lands.
    """
    number = issue.get("number")
    title = issue.get("title") or "(no title)"
    return (
        f"You are a code-worker agent. Your task: resolve merge "
        f"conflicts on branch '{branch}' against '{base_branch}' for "
        f"issue #{number} ({title}).\n"
        f"\n"
        f"Context: the dispatcher attempted an auto-rebase and it "
        f"failed: {reason}\n"
        f"\n"
        f"Steps:\n"
        f"  1. cd {repo_path}\n"
        f"  2. git fetch origin\n"
        f"  3. git checkout {branch}\n"
        f"  4. git rebase origin/{base_branch}\n"
        f"  5. Resolve conflicts using your judgment. Re-run tests "
        f"locally if the conflict touches code semantics, not just "
        f"adjacent line changes. Do not bypass hooks (no --no-verify).\n"
        f"  6. git push --force-with-lease origin {branch}\n"
        f"\n"
        f"Do NOT close or merge the PR yourself. After your push, the "
        f"State Machine dispatcher will pick the PR up on the next "
        f"cycle, re-run verification, and self-merge if CI is green.\n"
    )


def spawn_rebase_agent(
    issue: dict[str, Any],
    repo: str,
    branch: str,
    reason: str,
    *,
    spawn_dir: pathlib.Path = SPAWN_DIR,
    repo_path: pathlib.Path = WORKER_REPO_PATH,
    base_branch: str = BASE_BRANCH,
    claude_bin: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] = None,  # type: ignore[assignment]
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str | None:
    """Spawn a detached ``claude`` worker to resolve a merge conflict.

    Issue #173 Tier 2 spawn. Mirrors :func:`spawn_agent` (same spawn dir
    layout, pidfile, session JSONL) so the existing reap / liveness
    machinery treats this spawn identically to a regular worker. The
    only difference: the prompt is composed by
    :func:`compose_rebase_prompt` and the SPAWN_MAP is bypassed.

    Returns the ``spawn_id`` on success or ``None`` if the issue
    payload is malformed.

    Caller is responsible for posting the ``[SM] rebase-needed`` audit
    comment — that doubles as the dedup marker on the issue and
    persists across dispatcher passes whether or not the spawn lands.
    """
    if clock is None:
        clock = time.time

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn rebase on non-integer issue "
            f"number: {number!r}"
        )
        return None

    if claude_bin is None:
        claude_bin = resolve_claude_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_rebase_prompt(
        issue,
        branch=branch,
        reason=reason,
        base_branch=base_branch,
        repo_path=repo_path,
    )
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    session_id = new_session_id()
    (work_dir / SESSION_ID_FILENAME).write_text(session_id)

    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    pidfile_path = work_dir / "pidfile"

    stdin_fh = open(prompt_path, "rb")
    stdout_fh = open(stdout_path, "wb")
    stderr_fh = open(stderr_path, "wb")
    try:
        proc = popen(
            [claude_bin, "--print", "--session-id", session_id],
            stdin=stdin_fh,
            stdout=stdout_fh,
            stderr=stderr_fh,
            start_new_session=True,
        )
    finally:
        stdin_fh.close()
        stdout_fh.close()
        stderr_fh.close()

    pid = getattr(proc, "pid", None)
    if pid is not None:
        pidfile_path.write_text(str(pid))
    log(
        f"[sm-dispatcher] spawned rebase {spawn_id} (pid={pid}) on "
        f"#{number} branch={branch} session_id={session_id}"
    )
    return spawn_id


# ---------------------------------------------------------------------------
# Issue #156 — per-issue thinking-agent spawn machinery
# ---------------------------------------------------------------------------


def resolve_python_bin(
    *,
    preferred: str = PYTHON_BIN_PREFERRED,
    fallback: str = PYTHON_BIN_FALLBACK,
) -> str:
    """Return the Python interpreter path used to launch the thinking shim.

    Mirrors :func:`resolve_claude_bin`: prefer the venv interpreter when
    it exists, otherwise rely on ``$PATH`` so the dispatcher still runs
    cleanly in a test / dev shell without the worker venv mounted.
    """
    if pathlib.Path(preferred).is_file():
        return preferred
    return fallback


def compose_thinking_spawn_prompt(issue: dict[str, Any]) -> str:
    """Render the prompt fed into ``<thinking_spawn_dir>/prompt.txt``.

    The thinking-agent reads this verbatim at boot (sub-issue 3 wires
    the real PhaseRunner dispatch). The structure mirrors
    :func:`compose_spawn_prompt` so the operator-facing
    ``cat prompt.txt`` view is familiar across both lanes — only the
    role framing and the instruction trailer change.
    """
    number = issue.get("number")
    title = issue.get("title") or "(no title)"
    body = issue.get("body") or "(no body)"
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break
    login = _author_login(issue) or "(unknown)"
    source_label = f"source:{login}"

    return (
        f"You are a thinking-agent working on SM task #{number} in "
        f"design mode ({THINKING_PHASE_PER_ISSUE_DESIGN}).\n"
        f"\n"
        f"Issue: #{number}\n"
        f"Title: {title}\n"
        f"Source: {source_label}\n"
        f"Artifact type: {art_label}\n"
        f"\n"
        f"Issue body:\n"
        f"{body}\n"
        f"\n"
        f"Your task: produce a design note that captures the structure "
        f"of the change, prior art, alternatives considered, and a "
        f"sub-issue breakdown if the work decomposes. Write the note "
        f"into ~/alice-mind/cortex-memory/designs/<date>-issue"
        f"{number}-<slug>.md and post `[SM] design-ready "
        f"note=[[<wikilink>]] author=alice` on the issue when the "
        f"draft is ready for speaking's review.\n"
    )


def render_thinking_spawn_started_comment(
    number: int,
    art_label: str,
    spawn_id: str,
    *,
    phase: str = THINKING_PHASE_PER_ISSUE_DESIGN,
    runtime: str = THINKING_RUNTIME_LABEL,
    timestamp: str | None = None,
) -> str:
    """Produce the literal ``[SM] thinking-spawn-started ...`` audit comment.

    The shape is distinct from :func:`render_spawn_started_comment` —
    distinct prefix plus a ``phase=`` field — so the comments module can
    disambiguate the two spawn events without re-implementing a
    body-shape cascade.
    """
    ts = timestamp or _now_iso()
    return (
        f"{THINKING_SPAWN_STARTED_PREFIX} task=#{number} "
        f"artifact={art_label} phase={phase} runtime={runtime} "
        f"spawn_id={spawn_id} ts={ts}"
    )


def spawn_thinking_agent(
    issue: dict[str, Any],
    art_label: str,
    repo: str,
    *,
    spawn_dir: pathlib.Path = SM_THINKING_SPAWN_DIR,
    python_bin: str | None = None,
    shim_module: str = THINKING_SHIM_MODULE,
    phase: str = THINKING_PHASE_PER_ISSUE_DESIGN,
    runtime: str = THINKING_RUNTIME_LABEL,
    post_comment: PostCommentFn = gh_post_comment,
    popen: Callable[..., Any] = subprocess.Popen,
    now_iso: Callable[[], str] = _now_iso,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] | None = None,
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str | None:
    """Spawn a per-issue thinking-agent for an SM issue.

    Issue #156. Sibling of :func:`spawn_agent`. The shape is identical
    on disk (``<spawn_dir>/<spawn_id>/`` with ``prompt.txt`` / ``pidfile`` /
    ``stdout.log`` / ``stderr.log`` / ``session_id``) but the lane is
    separate: distinct concurrency cap (:data:`MAX_CONCURRENT_THINKING_SPAWNS`),
    distinct audit-comment prefix (:data:`THINKING_SPAWN_STARTED_PREFIX`),
    distinct spawn dir (:data:`SM_THINKING_SPAWN_DIR`).

    Steps:

      1. Mint ``spawn_id = "spawn-<N>-<unix-ts>"`` and create
         ``spawn_dir/<spawn_id>/``.
      2. Compose the design-mode prompt and write ``prompt.txt``.
      3. Pre-mint and persist the claude-agent-sdk session id
         (``session_id``) before Popen so a crash mid-launch still
         leaves the reaper a pointer.
      4. Post the ``[SM] thinking-spawn-started ...`` audit comment
         FIRST — without the dedup marker the next pass would re-spawn.
      5. Launch the thinking shim detached via
         ``python -m alice_sm.thinking_shim --spawn-dir <dir> --session-id <uuid>``
         with stdout/stderr to log files, ``start_new_session=True`` so
         the agent survives the dispatcher exiting.
      6. Write PID to ``pidfile``.

    Returns the ``spawn_id`` on success, or ``None`` if the issue number
    isn't an integer (defensive — the dispatcher's main loop already
    filters those out). Does NOT wait for the spawned subprocess.

    The wire-up into ``_process_selected`` lands in sub-issue 7 (the
    SPAWN_MAP cutover); this issue ships only the machinery. The real
    entrypoint replaces :mod:`alice_sm.thinking_shim` in sub-issue 3.
    """
    if clock is None:
        clock = time.time

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn thinking-agent on "
            f"non-integer issue number: {number!r}"
        )
        return None

    if python_bin is None:
        python_bin = resolve_python_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_thinking_spawn_prompt(issue)
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    # Pre-mint the SDK session id so the reaper can recover the worker's
    # session JSONL even if the shim crashes before logging it. Persist
    # BEFORE Popen for the same reason :func:`spawn_agent` does.
    session_id = new_session_id()
    (work_dir / SESSION_ID_FILENAME).write_text(session_id)

    body = render_thinking_spawn_started_comment(
        number,
        art_label,
        spawn_id,
        phase=phase,
        runtime=runtime,
        timestamp=now_iso(),
    )
    try:
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post thinking-spawn-started on "
            f"#{number}: {exc} — aborting spawn"
        )
        raise

    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    pidfile_path = work_dir / "pidfile"

    stdout_fh = open(stdout_path, "wb")
    stderr_fh = open(stderr_path, "wb")
    try:
        proc = popen(
            [
                python_bin,
                "-m",
                shim_module,
                "--spawn-dir",
                str(work_dir),
                "--session-id",
                session_id,
                "--mode",
                "design",
            ],
            stdout=stdout_fh,
            stderr=stderr_fh,
            start_new_session=True,
        )
    finally:
        stdout_fh.close()
        stderr_fh.close()

    pid = getattr(proc, "pid", None)
    if pid is not None:
        pidfile_path.write_text(str(pid))
    log(
        f"[sm-dispatcher] spawned thinking-agent {spawn_id} (pid={pid}) "
        f"on #{number} art={art_label} phase={phase} "
        f"session_id={session_id}"
    )
    return spawn_id


# ---------------------------------------------------------------------------
# Issue #184 — per-issue speaking-agent spawn machinery (build phase)
# ---------------------------------------------------------------------------


def compose_speaking_spawn_prompt(
    issue: dict[str, Any],
    *,
    design_note_path: str | pathlib.Path | None = None,
) -> str:
    """Render the prompt fed into ``<speaking_spawn_dir>/prompt.txt``.

    The speaking-agent reads this verbatim at boot. The structure mirrors
    :func:`compose_spawn_prompt` and :func:`compose_thinking_spawn_prompt`
    so the operator-facing ``cat prompt.txt`` view is familiar across
    all three lanes — only the role framing and instruction trailer
    change.

    A frontmatter block carries the ``phase:`` value the shim resolves
    via :func:`alice_thinking.cli.perissue.parse_frontmatter`-style
    parsing, plus a ``design_note:`` pointer to the approved design.
    The path comes from the dispatcher's ``_process_designed`` wiring
    (a separate sub-issue); when no path is supplied (e.g., dry-run /
    unit-test invocation) the field renders as ``(unset)`` so the
    operator can spot the misconfiguration in ``cat prompt.txt``.
    """
    number = issue.get("number")
    title = issue.get("title") or "(no title)"
    body = issue.get("body") or "(no body)"
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break
    login = _author_login(issue) or "(unknown)"
    source_label = f"source:{login}"
    design_note_value = (
        str(design_note_path) if design_note_path is not None else "(unset)"
    )

    return (
        f"---\n"
        f"phase: {SPEAKING_PHASE_PER_ISSUE_BUILD}\n"
        f"design_note: {design_note_value}\n"
        f"---\n"
        f"You are a speaking-agent working on SM task #{number} in "
        f"build mode ({SPEAKING_PHASE_PER_ISSUE_BUILD}).\n"
        f"\n"
        f"Issue: #{number}\n"
        f"Title: {title}\n"
        f"Source: {source_label}\n"
        f"Artifact type: {art_label}\n"
        f"Design note: {design_note_value}\n"
        f"\n"
        f"Issue body:\n"
        f"{body}\n"
        f"\n"
        f"Your task: load the approved design note above, then dispatch "
        f"the Task / Agent tool with the design + relevant repo context "
        f"to a sub-agent. The sub-agent implements the change and opens "
        f"a draft PR titled appropriately with `Closes #{number}` in "
        f"the body. When the sub-agent returns, post "
        f"`{SPEAKING_BUILD_COMPLETE_PREFIX} pr=<url>` on the issue (or "
        f"an error variant if the sub-agent could not open a PR).\n"
        f"\n"
        f"Operate as a real engineer would: the sub-agent must read the "
        f"relevant code first, test before opening the PR, and not "
        f"bypass CI hooks.\n"
    )


def render_speaking_spawn_started_comment(
    number: int,
    art_label: str,
    spawn_id: str,
    *,
    phase: str = SPEAKING_PHASE_PER_ISSUE_BUILD,
    runtime: str = SPEAKING_RUNTIME_LABEL,
    timestamp: str | None = None,
) -> str:
    """Produce the literal ``[SM] speaking-spawn-started ...`` audit comment.

    The shape is distinct from :func:`render_spawn_started_comment` and
    :func:`render_thinking_spawn_started_comment` — distinct prefix plus
    a ``phase=`` field — so the comments module can disambiguate the
    three spawn events without re-implementing a body-shape cascade.
    """
    ts = timestamp or _now_iso()
    return (
        f"{SPEAKING_SPAWN_STARTED_PREFIX} task=#{number} "
        f"artifact={art_label} phase={phase} runtime={runtime} "
        f"spawn_id={spawn_id} ts={ts}"
    )


def spawn_speaking_agent(
    issue: dict[str, Any],
    art_label: str,
    repo: str,
    *,
    design_note_path: str | pathlib.Path | None = None,
    spawn_dir: pathlib.Path = SM_SPEAKING_SPAWN_DIR,
    python_bin: str | None = None,
    shim_module: str = SPEAKING_SHIM_MODULE,
    phase: str = SPEAKING_PHASE_PER_ISSUE_BUILD,
    runtime: str = SPEAKING_RUNTIME_LABEL,
    post_comment: PostCommentFn = gh_post_comment,
    popen: Callable[..., Any] = subprocess.Popen,
    now_iso: Callable[[], str] = _now_iso,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] | None = None,
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str | None:
    """Spawn a per-issue speaking-agent for the build phase of an SM issue.

    Issue #184. Sibling of :func:`spawn_thinking_agent` (the design
    phase) and :func:`spawn_agent` (the v1 worker pool). The shape is
    identical on disk (``<spawn_dir>/<spawn_id>/`` with ``prompt.txt`` /
    ``pidfile`` / ``stdout.log`` / ``stderr.log`` / ``session_id``) but
    the lane is separate: distinct concurrency cap
    (:data:`MAX_CONCURRENT_SPEAKING_SPAWNS`), distinct audit-comment
    prefix (:data:`SPEAKING_SPAWN_STARTED_PREFIX`), distinct spawn dir
    (:data:`SM_SPEAKING_SPAWN_DIR`).

    Steps (mirror of :func:`spawn_thinking_agent`):

      1. Mint ``spawn_id = "spawn-<N>-<unix-ts>"`` and create
         ``spawn_dir/<spawn_id>/``.
      2. Compose the build-mode prompt (with ``design_note_path`` baked
         into the frontmatter) and write ``prompt.txt``.
      3. Pre-mint and persist the claude-agent-sdk session id
         (``session_id``) before Popen so a crash mid-launch still
         leaves the reaper a pointer.
      4. Post the ``[SM] speaking-spawn-started ...`` audit comment
         FIRST — without the dedup marker the next pass would re-spawn.
      5. Launch the speaking shim detached via
         ``python -m alice_sm.speaking_shim --spawn-dir <dir> --session-id <uuid> --mode build``
         with stdout/stderr to log files, ``start_new_session=True`` so
         the agent survives the dispatcher exiting.
      6. Write PID to ``pidfile``.

    Returns the ``spawn_id`` on success, or ``None`` if the issue number
    isn't an integer (defensive — the dispatcher's main loop already
    filters those out). Does NOT wait for the spawned subprocess.

    The wire-up into ``_process_designed`` lands in a separate sub-issue
    once spawn + entrypoint are tested in isolation; this issue ships
    only the machinery. The real entrypoint replaces
    :mod:`alice_sm.speaking_shim`.
    """
    if clock is None:
        clock = time.time

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn speaking-agent on "
            f"non-integer issue number: {number!r}"
        )
        return None

    if python_bin is None:
        python_bin = resolve_python_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_speaking_spawn_prompt(
        issue, design_note_path=design_note_path
    )
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    # Pre-mint the SDK session id so the reaper can recover the worker's
    # session JSONL even if the shim crashes before logging it. Persist
    # BEFORE Popen for the same reason :func:`spawn_thinking_agent` does.
    session_id = new_session_id()
    (work_dir / SESSION_ID_FILENAME).write_text(session_id)

    body = render_speaking_spawn_started_comment(
        number,
        art_label,
        spawn_id,
        phase=phase,
        runtime=runtime,
        timestamp=now_iso(),
    )
    try:
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post speaking-spawn-started on "
            f"#{number}: {exc} — aborting spawn"
        )
        raise

    stdout_path = work_dir / "stdout.log"
    stderr_path = work_dir / "stderr.log"
    pidfile_path = work_dir / "pidfile"

    stdout_fh = open(stdout_path, "wb")
    stderr_fh = open(stderr_path, "wb")
    try:
        proc = popen(
            [
                python_bin,
                "-m",
                shim_module,
                "--spawn-dir",
                str(work_dir),
                "--session-id",
                session_id,
                "--mode",
                "build",
            ],
            stdout=stdout_fh,
            stderr=stderr_fh,
            start_new_session=True,
        )
    finally:
        stdout_fh.close()
        stderr_fh.close()

    pid = getattr(proc, "pid", None)
    if pid is not None:
        pidfile_path.write_text(str(pid))
    log(
        f"[sm-dispatcher] spawned speaking-agent {spawn_id} (pid={pid}) "
        f"on #{number} art={art_label} phase={phase} "
        f"session_id={session_id}"
    )
    return spawn_id


# ---------------------------------------------------------------------------
# Trust filter
# ---------------------------------------------------------------------------


@dataclass
class TrustDecision:
    """Outcome of running the trust filter on a single issue."""

    accepted: bool
    reason: str  # human-readable; populated on rejection too for logging
    art_label: str | None = None  # populated on acceptance


def _label_names(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("labels") or []
    names: list[str] = []
    for entry in raw:
        # ``gh issue list --json labels`` returns
        # ``[{"id": ..., "name": ..., "description": ..., "color": ...}, ...]``.
        # Accept bare strings too — keeps the test fixtures readable.
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(entry, str):
            names.append(entry)
    return names


def _author_login(issue: dict[str, Any]) -> str | None:
    author = issue.get("author") or {}
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str):
            return login
    # ``gh`` sometimes returns the bare login string under unusual configs.
    if isinstance(author, str):
        return author
    return None


def _current_sm_label(issue: dict[str, Any]) -> str | None:
    """Return the single whitelisted ``sm:*`` label, or None if not exactly one."""
    names = _label_names(issue)
    sm_labels = [n for n in names if n.startswith("sm:") and n in SM_LABEL_WHITELIST]
    if len(sm_labels) != 1:
        return None
    return sm_labels[0]


def evaluate_trust(
    issue: dict[str, Any],
    *,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    sm_whitelist: frozenset[str] = SM_LABEL_WHITELIST,
    art_whitelist: frozenset[str] = ART_LABEL_WHITELIST,
) -> TrustDecision:
    """Run the v0 trust filter against one ``gh issue list`` payload.

    Returns a :class:`TrustDecision`. On rejection, ``reason`` is a short
    diagnostic string suitable for stderr; on acceptance, ``art_label``
    carries the matched ``art:*`` label so the caller can render it into
    the dispatcher-hello comment without re-scanning.
    """
    login = _author_login(issue)
    if not login or login not in trusted_authors:
        return TrustDecision(
            accepted=False,
            reason=f"untrusted author: {login!r}",
        )

    names = _label_names(issue)
    sm_labels = [n for n in names if n.startswith("sm:")]
    sm_in_whitelist = [n for n in sm_labels if n in sm_whitelist]
    if len(sm_labels) != 1 or len(sm_in_whitelist) != 1:
        return TrustDecision(
            accepted=False,
            reason=(f"expected exactly one whitelisted sm:* label, got {sm_labels!r}"),
        )

    art_labels = [n for n in names if n.startswith("art:") and n in art_whitelist]
    if not art_labels:
        return TrustDecision(
            accepted=False,
            reason=(
                "expected at least one whitelisted art:* label, "
                f"got {[n for n in names if n.startswith('art:')]!r}"
            ),
        )

    # When multiple ``art:*`` labels are set, pick the lexicographically
    # smallest for determinism in the dispatcher-hello payload. v0 isn't
    # required to handle multi-artifact tasks; sorting just keeps the
    # output stable.
    return TrustDecision(
        accepted=True,
        reason="ok",
        art_label=sorted(art_labels)[0],
    )


# ---------------------------------------------------------------------------
# Comment rendering
# ---------------------------------------------------------------------------


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


def _http_get_body(
    url: str,
    *,
    timeout: float = VERIFY_HTTP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, str]:
    """Issue a GET, return ``(status, body_text)``.

    Wraps :func:`urllib.request.urlopen` so tests can inject a fake
    opener and avoid actual network I/O. Decodes the body as UTF-8 with
    ``errors='replace'`` — the marker check is a substring match so
    mojibake on the boundary won't matter.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "alice-sm-verify/1"})
    with opener(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
    if isinstance(raw, bytes):
        body = raw.decode("utf-8", errors="replace")
    else:
        body = str(raw)
    return status, body


def verify_viewer_route(
    *,
    url: str,
    marker: str,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Run the viewer-route smoke test, return a verdict dict.

    Verdict keys:
      - ``outcome``: ``"pass"`` or ``"fail"``
      - ``reason``: short human string (populated on fail)
      - ``route``: URL probed (populated on pass; included on fail for
        the audit comment so Jason can replay it manually)

    Failure modes that count as a *fail* (not a transient bail-out):
      - Connection refused / timeout / DNS error
      - Non-2xx HTTP status
      - 2xx but the marker substring isn't in the response body

    The verifier never raises; transport errors are caught and
    reported as ``outcome="fail"`` so the dispatcher can post the
    ``verify-failed`` audit comment and leave the issue at
    ``sm:reviewing`` for a human to inspect.
    """
    getter = http_get or _http_get_body
    try:
        status, body = getter(url)
    except (urllib.error.URLError, OSError) as exc:
        return {
            "outcome": "fail",
            "reason": f"viewer probe failed: {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "outcome": "fail",
            "reason": f"viewer probe raised {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    if not (200 <= int(status) < 300):
        return {
            "outcome": "fail",
            "reason": f"viewer probe HTTP {status}",
            "route": url,
        }
    if marker not in body:
        return {
            "outcome": "fail",
            "reason": f"marker {marker!r} not found in response body",
            "route": url,
        }
    return {"outcome": "pass", "reason": "viewer marker present", "route": url}


def _touches_viewer(files: Iterable[str]) -> bool:
    return any(p.startswith(VERIFY_VIEWER_PATH_PREFIX) for p in files)


def default_verifier(
    pr_number: int,
    files: list[str],
    *,
    viewer_url: str | None = None,
    viewer_marker: str | None = None,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Default issue-#128 verification recipe dispatcher.

    Picks a verification recipe based on what the merged PR touched.
    v1 only ships the *viewer-route* recipe; anything else returns
    ``outcome="skip"`` with a recipe-not-matched reason so the
    dispatcher can still close the issue (audit-trail visible) without
    pretending we ran a check we didn't.

    Wired into :func:`run` via the ``verify_pr`` keyword argument so
    tests can inject a recipe stub that doesn't open sockets.
    """
    url = viewer_url or os.environ.get(VERIFY_VIEWER_URL_ENV, VERIFY_VIEWER_URL_DEFAULT)
    marker = viewer_marker or os.environ.get(
        VERIFY_VIEWER_MARKER_ENV, VERIFY_VIEWER_MARKER_DEFAULT
    )
    if _touches_viewer(files):
        return verify_viewer_route(url=url, marker=marker, http_get=http_get)
    # Future recipes (dispatcher --check, speaking enqueue-and-assert,
    # research-note path-exists) extend this branch. Until then,
    # anything outside the viewer touch is treated as "no recipe
    # matched" and allowed through with a verify-skip audit comment.
    return {
        "outcome": "skip",
        "reason": "no verification recipe matched (no src/alice_viewer/ files in PR)",
        "route": None,
    }


def _verify_enabled() -> bool:
    raw = os.environ.get(VERIFY_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------


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


def _process_selected(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    has_live_spawn: Callable[[int], bool] | None,
    count_running: Callable[[], int] | None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None,
    max_concurrent_spawns: int,
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
    get_issue: Callable[[int], dict[str, Any] | None] | None = None,
    has_live_thinking_spawn: Callable[[int], bool] | None = None,
    count_running_thinking: Callable[[], int] | None = None,
    spawn_thinking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    max_concurrent_thinking_spawns: int = MAX_CONCURRENT_THINKING_SPAWNS,
) -> None:
    """Return-to-study check + Hello + T1 (selected → reviewing) + Phase 2
    spawn for one sm:selected issue.

    Order matters: trust filter → return-to-study scan (terminating: an
    explicit ``[SM] return-to-study`` from the worker reverses the
    state before any new work fires) → dependency check (issue #176:
    rejected dep → ``sm:blocked``, terminating) → hello (idempotent) →
    T1 if linked PR exists (terminating, since work is already in
    flight) → otherwise Phase 2 spawn (gated by concurrency cap + dedup
    on a live spawn dir + open hard-deps from issue #176).

    ``get_issue`` (issue #176) is the per-issue lookup used to resolve
    ``Depends on #N`` references on the body. ``None`` disables the
    dependency gate entirely — production callers always bind it; tests
    that don't exercise the gate can leave it unset.

    Spawn dispatch (sub-issue 7 / #186): the
    :data:`SPAWN_MAP` row's ``persona`` field selects which spawn
    machinery to invoke. ``persona == "thinking"`` (the SM v2 design
    lane for ``art:code``) routes to ``spawn_thinking`` and gates
    against the thinking-lane's dedup / concurrency helpers
    (``has_live_thinking_spawn`` / ``count_running_thinking`` /
    :data:`MAX_CONCURRENT_THINKING_SPAWNS`). All other personae
    (``"worker"`` for ``art:config_change`` / ``art:research_note`` /
    ``art:experiment``) route to the v1 ``spawn`` callable, same as
    the pre-cutover behavior.
    """
    number = issue["number"]
    decision = evaluate_trust(issue, trusted_authors=trusted_authors)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

    # ----- return-to-study check -----
    # A worker that realises it can't advance from sm:selected without
    # further thinking input emits ``[SM] return-to-study reason=...``;
    # the dispatcher reverses the state on the next pass. This must
    # short-circuit the hello/T1/spawn flow — once the issue is going
    # back to needs_study there's no point posting a hello or queuing a
    # new spawn.
    try:
        sel_comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] selected #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        sel_comments = []
    from alice_sm.comments import ReturnToStudy
    parsed_return = _find_parsed_comment_of_type(
        sel_comments,
        ReturnToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed_return is not None:
        reason = f'return-to-study reason="{parsed_return.reason}"'
        transition_body = render_transition_comment(
            ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL, reason
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"selected → needs_study ({reason})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[NEEDS_STUDY_SM_LABEL],
                remove=[ACTIVE_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] selected #{number}: "
                f"failed return-to-study transition: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append(
            (number, ACTIVE_SM_LABEL, NEEDS_STUDY_SM_LABEL)
        )
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"selected → needs_study ({reason})"
        )
        return

    # ----- dependency parse + resolve (issue #176) -----
    # ``Depends on #N`` / ``Blocked by #N`` / etc. live in plain prose
    # on the issue body and any trusted-author amendment comments. The
    # parser is anchored to start-of-line so prose inside ordinary
    # comments doesn't produce false positives.
    from alice_sm.comments import parse_dependencies as _parse_deps

    dep_sources: list[str] = []
    body_text = issue.get("body")
    if isinstance(body_text, str) and body_text:
        dep_sources.append(body_text)
    for c in sel_comments:
        if not isinstance(c, dict):
            continue
        cb = c.get("body")
        if not isinstance(cb, str) or not cb:
            continue
        # Skip ``[SM] ...`` audit/protocol comments — those are the
        # dispatcher's own log lines and won't contain user-authored
        # dependency directives. The trust filter further restricts to
        # trusted authors so a drive-by commenter can't inject deps
        # that would gate or transition the issue.
        if cb.startswith("[SM] "):
            continue
        author = _comment_author_login(c)
        if author not in trusted_authors:
            continue
        dep_sources.append(cb)
    parsed_deps = _parse_deps("\n".join(dep_sources)) if dep_sources else None

    blocking_deps: tuple[int, ...] = ()
    if parsed_deps is not None and (parsed_deps.hard or parsed_deps.soft):
        if get_issue is None:
            # Production wires get_issue via ``run()``; tests that don't
            # exercise the gate leave it None. Treat as "no resolver" =
            # don't block, but log so the operator notices if it ever
            # fires in prod.
            log(
                f"[sm-dispatcher] #{number}: deps "
                f"hard={list(parsed_deps.hard)} soft={list(parsed_deps.soft)} "
                f"present but no get_issue resolver bound — "
                f"skipping dependency gate"
            )
        else:
            resolution = resolve_dependencies(
                parsed_deps.hard, get_issue, log=log
            )
            if resolution.rejected:
                rejected_str = ", ".join(f"#{n}" for n in resolution.rejected)
                inner_reason = (
                    f"dependency {rejected_str} was rejected"
                )
                transition_body = (
                    f'[SM] transition from=selected to=blocked '
                    f'reason="{inner_reason}" '
                    f'unblocked_by="speaking to re-scope"'
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would transition "
                        f"#{number}: selected → blocked ({inner_reason})"
                    )
                    report.transitioned += 1
                    report.transitions.append(
                        (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                    )
                    return
                try:
                    edit_labels(
                        repo,
                        number,
                        add=[BLOCKED_SM_LABEL],
                        remove=[ACTIVE_SM_LABEL],
                    )
                    post_comment(repo, number, transition_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] selected #{number}: "
                        f"failed dependency-rejected transition: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                report.transitioned += 1
                report.transitions.append(
                    (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                )
                log(
                    f"[sm-dispatcher] transitioned #{number}: "
                    f"selected → blocked ({inner_reason})"
                )
                return
            # Soft-dep + missing branches are log-only; the hard-blocking
            # gate is applied below, after hello + T1, so the audit comment
            # still posts even when the issue is queued.
            blocking_deps = resolution.blocking

    art_label = decision.art_label or "art:unknown"

    # Hello (dedup-guarded)
    if state.has_hello(number):
        report.skipped_dedup += 1
    else:
        body = render_hello_comment(number, art_label, timestamp=now_iso())
        if dry_run:
            log(f"[sm-dispatcher] DRY-RUN would post on #{number}: {body}")
            report.posted += 1
            report.posted_numbers.append(number)
        else:
            try:
                post_comment(repo, number, body)
            except GHCommandError as exc:
                log(f"[sm-dispatcher] failed to comment on #{number}: {exc}")
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            state.mark_hello(number)
            report.posted += 1
            report.posted_numbers.append(number)
            log(f"[sm-dispatcher] posted dispatcher-hello on #{number}")

    # T1: sm:selected → sm:reviewing if a linked open PR exists.
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to look up PR for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is not None:
        # T1 fires only when the linked PR is still OPEN.
        # ``gh_find_linked_pr`` queries ``--state all`` (so the T2/T3
        # path can find merged PRs); we filter here so an sm:selected
        # issue whose PR has already merged or closed doesn't get
        # bounced to sm:reviewing — that lifecycle stage is past.
        pr_state = (pr.get("state") or "").upper()
        if pr_state != "OPEN":
            log(
                f"[sm-dispatcher] #{number} selected but linked PR is "
                f"{pr_state!r} (not OPEN) — not transitioning to reviewing"
            )
            return
        pr_url = pr.get("url") or "<unknown>"
        transition_body = render_transition_comment(
            ACTIVE_SM_LABEL, REVIEWING_SM_LABEL, f"PR opened: {pr_url}"
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"selected → reviewing ({pr_url})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, REVIEWING_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[REVIEWING_SM_LABEL],
                remove=[ACTIVE_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed to transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append((number, ACTIVE_SM_LABEL, REVIEWING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: selected → reviewing")
        return

    # No linked PR yet — Phase 2 spawn path.
    spawn_config = SPAWN_MAP.get((ACTIVE_SM_LABEL, art_label))
    if spawn_config is None:
        log(
            f"[sm-dispatcher] spawn skip #{number}: "
            f"unrecognized artifact {art_label!r}"
        )
        return

    persona = spawn_config.get("persona", "worker")

    # Persona selects the spawn lane (sub-issue 7 / #186). The thinking
    # lane uses its own dedup + concurrency helpers so a long-running
    # design loop can't starve the v1 worker pool (and vice versa).
    if persona == "thinking":
        lane_spawn = spawn_thinking
        lane_has_live = has_live_thinking_spawn
        lane_count_running = count_running_thinking
        lane_cap = max_concurrent_thinking_spawns
        lane_label = "thinking"
    else:
        lane_spawn = spawn
        lane_has_live = has_live_spawn
        lane_count_running = count_running
        lane_cap = max_concurrent_spawns
        lane_label = "worker"

    # Caller passes the lane's helpers as None to disable spawning
    # entirely (tests that only care about hello/T1 paths take this
    # escape hatch).
    if lane_spawn is None or lane_count_running is None or lane_has_live is None:
        return

    # Issue #176 — gate the spawn on any unresolved hard dependency.
    # No spawn-started comment, no label change; the issue stays at
    # sm:selected and the dispatcher re-checks on the next pass when
    # the dep may have closed. Logged once per pass per blocking dep
    # so the operator can see what's holding the queue.
    if blocking_deps:
        blocked_str = ", ".join(f"#{n}" for n in blocking_deps)
        log(
            f"[sm-dispatcher] spawn skip #{number}: "
            f"blocked by {blocked_str}"
        )
        report.spawn_skipped_blocked_deps += 1
        return

    # Dedup on a live spawn dir (issue #115). The historic
    # [SM] spawn-started audit comment is NOT consulted — if the
    # worker died after posting the comment but before opening a PR,
    # we want the next pass to retry, not be permanently gated by the
    # comment. The lane-scoped helper also reaps stale ``spawn-<N>-*``
    # dirs into ``.finished/`` so they don't keep getting re-checked.
    if lane_has_live(number):
        log(
            f"[sm-dispatcher] spawn skip #{number}: live {lane_label} "
            f"spawn dir already running"
        )
        return

    # Issue #202 — silent thinking-spawn guard. The thinking lane has
    # no equivalent of the worker lane's "open a PR" terminal signal at
    # sm:selected; instead, the thinking-agent is expected to post
    # ``[SM] design-ready`` once the design note is written. If a prior
    # spawn already fired (audit comment present) but no design-ready
    # ever followed AND no live spawn dir remains, the shim completed
    # without doing anything useful — retrying just loops forever (the
    # observed failure mode on #194: ~1125 respawns over 22h). Block
    # the issue rather than re-spawning; an operator (or sub-issue 3
    # shim replacement) can unblock once the underlying entrypoint is
    # wired up. Scoped to ``persona == "thinking"`` so the v1 worker
    # retry semantics above stay untouched.
    if persona == "thinking":
        saw_thinking_spawn_started = False
        saw_design_ready = False
        for c in sel_comments:
            if not isinstance(c, dict):
                continue
            body = c.get("body")
            if not isinstance(body, str):
                continue
            login = _comment_author_login(c)
            if not isinstance(login, str) or login not in trusted_authors:
                continue
            if body.startswith(THINKING_SPAWN_STARTED_PREFIX):
                saw_thinking_spawn_started = True
            elif body.startswith("[SM] design-ready"):
                # Matches both the agent-emitted ``[SM] design-ready``
                # and the dispatcher's ``[SM] design-ready-audit`` echo;
                # either is evidence that the design phase produced its
                # terminal signal.
                saw_design_ready = True
        if saw_thinking_spawn_started and not saw_design_ready:
            reason = (
                "thinking-agent spawn exited without posting "
                "[SM] design-ready (see #202)"
            )
            transition_body = render_transition_comment(
                ACTIVE_SM_LABEL, BLOCKED_SM_LABEL, reason
            )
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                    f"selected → blocked ({reason})"
                )
                report.transitioned += 1
                report.transitions.append(
                    (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
                )
                return
            try:
                edit_labels(
                    repo,
                    number,
                    add=[BLOCKED_SM_LABEL],
                    remove=[ACTIVE_SM_LABEL],
                )
                post_comment(repo, number, transition_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] selected #{number}: "
                    f"failed silent-spawn-failure transition: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            report.transitioned += 1
            report.transitions.append(
                (number, ACTIVE_SM_LABEL, BLOCKED_SM_LABEL)
            )
            log(
                f"[sm-dispatcher] transitioned #{number}: "
                f"selected → blocked ({reason})"
            )
            return

    live = lane_count_running()
    if live >= lane_cap:
        log(
            f"[sm-dispatcher] spawn skip #{number}: {lane_label} "
            f"concurrency cap reached ({live}/{lane_cap}) — queued for "
            f"next pass"
        )
        return

    if dry_run:
        if persona == "thinking":
            preview = compose_thinking_spawn_prompt(issue)[:240]
        else:
            preview = compose_spawn_prompt(issue, spawn_config)[:240]
        log(
            f"[sm-dispatcher] DRY-RUN would spawn {lane_label} on "
            f"#{number} art={art_label} "
            f"(running={live}/{lane_cap})"
        )
        log(f"[sm-dispatcher] DRY-RUN prompt preview: {preview!r}")
        report.spawned += 1
        report.spawn_records.append((number, art_label, "<dry-run>"))
        return

    try:
        spawn_id = lane_spawn(issue, art_label, repo)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to spawn on #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        log(f"[sm-dispatcher] spawn OS error on #{number}: {exc}")
        return
    if spawn_id is None:
        return
    report.spawned += 1
    report.spawn_records.append((number, art_label, spawn_id))


def _process_reviewing(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    close_issue: CloseIssueFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    pr_files: PRFilesFn | None,
    verify_pr: VerifyFn | None,
    post_merge_cleanup: PostMergeCleanupFn | None,
    pr_mergeable: "PRMergeableFn | None" = None,
    attempt_rebase: "Callable[[str], dict[str, Any]] | None" = None,
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None" = None,
    has_live_spawn: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda s: None,
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """T2 (reviewing → done) and T3 (reviewing → building) for one issue.

    ``post_merge_cleanup`` (Issue #127) is invoked after a successful
    ``reviewing → done`` transition with the merged PR's head branch and
    the issue number. ``None`` disables cleanup (the test default).

    ``verify_pr`` (Issue #128) is the smoke-test gate run between
    "CI-green" and the actual ``sm:done`` transition. ``None`` disables
    verification entirely (pre-#128 behavior — used by tests that
    don't want to stub the verifier). When non-None, the verifier is
    called with the linked PR number + its changed-file list (obtained
    via ``pr_files``); the verdict's ``outcome`` decides whether to
    proceed, skip-with-audit, or halt at ``sm:reviewing``.

    ``pr_mergeable`` / ``attempt_rebase`` / ``spawn_rebase`` /
    ``has_live_spawn`` (Issue #173) drive the auto-rebase handler on
    unmerged PRs at sm:reviewing. If the PR comes back ``CONFLICTING``,
    the dispatcher fires the three-tier rebase recovery (in-process
    rebase → fresh worker → escalation comment). All four arguments
    default to ``None`` — when any is unset the conflict handler is
    a no-op and the issue stays at sm:reviewing (pre-#173 behavior).
    """
    number = issue["number"]
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed to look up PR for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is None:
        # No PR found at all — stay at reviewing. ``find_linked_pr``
        # queries ``--state all``, so this branch only fires when there
        # is genuinely no linked PR (deleted or never existed).
        # Surfaces are escalation-only.
        log(f"[sm-dispatcher] #{number} reviewing but no linked PR found — staying")
        return

    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        return
    try:
        merge_info = pr_merge_status(repo, pr_number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed merge-status for PR #{pr_number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    if not merge_info.get("merged"):
        # PR still open — check whether it's stuck on a merge conflict
        # and drive the Tier 1/2/3 auto-rebase handler. When the helper
        # callables aren't wired (e.g. tests that don't care about
        # conflicts), this stays a no-op.
        _handle_conflicting_pr(
            issue=issue,
            repo=repo,
            pr_number=pr_number,
            state=state,
            report=report,
            post_comment=post_comment,
            pr_mergeable=pr_mergeable,
            attempt_rebase=attempt_rebase,
            spawn_rebase=spawn_rebase,
            has_live_spawn=has_live_spawn,
            dry_run=dry_run,
            log=log,
            now_iso=now_iso,
        )
        return

    sha = merge_info.get("merge_commit_oid")
    pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
    if not sha:
        log(f"[sm-dispatcher] #{number} PR merged but no merge_commit_oid — staying")
        return

    try:
        ci = master_ci_status(repo, sha)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] failed CI lookup for {sha[:8]}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    conclusion = ci.get("conclusion")
    if conclusion is None or conclusion == "pending":
        # No verdict yet — stay at reviewing for next pass.
        return

    if conclusion == "success":
        # ----- Issue #128 verification gate -----
        # CI green is necessary but not sufficient — run an
        # artifact-specific smoke test against the *actually-running*
        # system before declaring the issue done.
        verdict: dict[str, Any] | None = None
        if verify_pr is not None:
            files: list[str] = []
            if pr_files is not None:
                try:
                    files = pr_files(repo, pr_number)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to fetch PR files for "
                        f"#{pr_number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    # Without the file list we can't pick a recipe; bail
                    # this cadence and let the next poll retry. The
                    # issue stays at sm:reviewing.
                    return
            try:
                verdict = verify_pr(pr_number, files)
            except Exception as exc:  # noqa: BLE001 — verifier must never crash the loop
                log(
                    f"[sm-dispatcher] verifier raised for #{number}: "
                    f"{exc.__class__.__name__}: {exc} — treating as verify-failed"
                )
                verdict = {
                    "outcome": "fail",
                    "reason": f"verifier crashed: {exc.__class__.__name__}: {exc}",
                    "route": None,
                }
            outcome = (verdict or {}).get("outcome") or "fail"

            if outcome == "fail":
                v_reason = (verdict or {}).get("reason") or "verification failed"
                v_route = (verdict or {}).get("route")
                # Counter reflects "verifier returned fail this pass" —
                # incremented regardless of whether we actually post a
                # comment (dedup may suppress it). The operator's
                # done-line read of ``verify_failed=N`` should mean
                # "there are still N broken merges parked at reviewing"
                # rather than "we sent N comments to GH this cadence".
                report.verify_failed += 1
                report.verify_records.append((number, "fail", v_reason))
                verify_body = render_verify_comment(
                    "failed",
                    number,
                    reason=v_reason,
                    route=v_route,
                    timestamp=now_iso(),
                )
                if dry_run:
                    log(
                        f"[sm-dispatcher] DRY-RUN would post verify-failed on "
                        f"#{number}: {v_reason}"
                    )
                    return
                if state.has_verify_failed(number):
                    # Already posted this cadence-or-prior; don't spam.
                    # The label stays at sm:reviewing — a human inspects
                    # and either rolls back, escalates, or overrides.
                    log(
                        f"[sm-dispatcher] #{number} verify still failing "
                        f"({v_reason}) — comment already posted, staying"
                    )
                    return
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-failed on "
                        f"#{number}: {exc}"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
                    return
                state.mark_verify_failed(number)
                log(
                    f"[sm-dispatcher] #{number} verify-failed posted "
                    f"({v_reason}) — staying at sm:reviewing"
                )
                return

            # outcome == "pass" or "skip" — both allow the transition.
            # Post the audit comment first so the trail records *why*
            # we proceeded (pass means a probe succeeded; skip means
            # no recipe matched). If posting fails we still proceed —
            # the audit is best-effort, not gating.
            v_reason = (verdict or {}).get("reason") or ""
            v_route = (verdict or {}).get("route")
            verify_body = render_verify_comment(
                outcome,
                number,
                reason=v_reason,
                route=v_route,
                timestamp=now_iso(),
            )
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would post verify-{outcome} on "
                    f"#{number}: {v_reason}"
                )
            else:
                try:
                    post_comment(repo, number, verify_body)
                except GHCommandError as exc:
                    log(
                        f"[sm-dispatcher] failed to post verify-{outcome} on "
                        f"#{number}: {exc} — proceeding anyway"
                    )
                    if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                        raise
            if outcome == "pass":
                report.verify_pass += 1
            else:
                report.verify_skip += 1
            report.verify_records.append((number, outcome, v_reason))
            # If the issue had a prior verify-failed entry, clear it —
            # this cadence succeeded and the dedup ledger entry is
            # stale.
            state.clear_verify_failed(number)

        # ----- end verification gate -----

        reason = f"PR merged: {pr_url}, CI green on {sha}"
        body = render_transition_comment(REVIEWING_SM_LABEL, DONE_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → done ({sha[:8]})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[DONE_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            close_issue(repo, number)
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed close/transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, DONE_SM_LABEL))
        # Issue #173: a successful done transition closes any prior
        # CONFLICTING episode for this issue. Clear the dedup ledger so a
        # future re-entry into sm:reviewing (unlikely, but the state file
        # is long-lived) can fire Tier 1/2/3 again from scratch.
        state.clear_rebase_attempted(number)
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → done (closed)")
        # Issue #127 — restore the worker's working tree to master so the
        # next cycle doesn't read dispatcher.py from this departing
        # worker's feature branch. Cleanup is bounded to this exact
        # transition (merged + green); CI-red and unmerged-closed paths
        # never reach here.
        if post_merge_cleanup is not None:
            try:
                post_merge_cleanup(merge_info.get("head_ref_name"), number)
                report.cleaned_up += 1
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                log(
                    f"[sm-dispatcher] post-merge cleanup raised for #{number}: "
                    f"{exc!r}"
                )
        return

    if conclusion == "failure":
        run_url = ci.get("run_url") or "<unknown>"
        reason = f"CI red on merge: {run_url}"
        body = render_transition_comment(REVIEWING_SM_LABEL, BUILDING_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"reviewing → building (CI red {run_url})"
            )
            report.transitioned += 1
            report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
            return
        try:
            edit_labels(
                repo,
                number,
                add=[BUILDING_SM_LABEL],
                remove=[REVIEWING_SM_LABEL],
            )
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed transition #{number}: {exc}")
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        # CI flipped red — the prior verify-failed entry (if any) was
        # for the green build that just regressed. Clear so when CI
        # eventually re-greens we don't suppress a fresh failure.
        state.clear_verify_failed(number)
        # Issue #173: a CI-red transition also closes the CONFLICTING
        # episode — the work moves back to sm:building and a fresh PR
        # may eventually open. Clear the ledger entry so the next
        # CONFLICTING incident starts fresh.
        state.clear_rebase_attempted(number)
        report.transitioned += 1
        report.transitions.append((number, REVIEWING_SM_LABEL, BUILDING_SM_LABEL))
        log(f"[sm-dispatcher] transitioned #{number}: reviewing → building (CI red)")
        return


def _handle_conflicting_pr(
    *,
    issue: dict[str, Any],
    repo: str,
    pr_number: int,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    pr_mergeable: "PRMergeableFn | None",
    attempt_rebase: "Callable[[str], dict[str, Any]] | None",
    spawn_rebase: "Callable[[dict[str, Any], str, str, str], str | None] | None",
    has_live_spawn: "Callable[[int], bool] | None",
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """Issue #173 — Tier 1/2/3 auto-rebase handler for a CONFLICTING PR.

    Called from :func:`_process_reviewing` when the linked PR is still
    open. Looks up the GitHub-computed ``mergeable`` state and, if it
    is ``CONFLICTING``, runs the recovery ladder:

      * **Tier 1 (cheap)** — fire :func:`attempt_rebase`. On success
        post ``[SM] rebase-pushed`` and return; CI will re-fire on the
        new head and the dispatcher picks the PR up next cycle.
      * **Tier 2 (escalation)** — on rebase failure, post
        ``[SM] rebase-needed`` (with the offending file / stderr in the
        reason) AND spawn a fresh worker via :func:`spawn_rebase` to
        resolve conflicts manually. Marks the issue in
        ``state.rebase_attempted`` so a follow-up cycle can detect
        "the spawn died but the PR is still conflicting".
      * **Tier 3 (give up)** — if a prior Tier 2 spawn is dead (no live
        spawn dir) AND the PR is still CONFLICTING, post a
        ``[SM] rebase-escalated`` audit comment exactly once and stop
        retrying. Dedup'd by ``state.rebase_escalated_posted``.

    ``MERGEABLE`` and ``UNKNOWN`` results are no-ops — the existing
    worker self-merge path drives MERGEABLE, and UNKNOWN means GitHub
    is still computing so we wait. Any wiring callable left as ``None``
    short-circuits the handler (test/dry-run escape hatch).
    """
    number = issue["number"]
    if pr_mergeable is None or attempt_rebase is None or spawn_rebase is None:
        # Conflict handler isn't wired this run — silent no-op.
        return

    try:
        info = pr_mergeable(repo, pr_number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed mergeable lookup for PR #{pr_number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    mergeable = info.get("mergeable")
    if mergeable != "CONFLICTING":
        # MERGEABLE → wait for the worker's self-merge.
        # UNKNOWN → GH still computing, retry next cycle.
        # Anything else (None/odd) → treat as UNKNOWN.
        if mergeable in (None, "UNKNOWN"):
            log(
                f"[sm-dispatcher] #{number} PR #{pr_number} mergeable={mergeable!r} "
                f"— retry next cycle"
            )
        return

    branch = info.get("head_ref_name")
    if not branch:
        # Can't act without a branch name. Log and wait.
        log(
            f"[sm-dispatcher] #{number} PR #{pr_number} CONFLICTING but no "
            f"head_ref_name in gh payload — staying"
        )
        return

    # Already escalated to Tier 3 — stay silent until the operator
    # intervenes (either rebases manually, closes the PR, or flips the
    # state ledger entry by transitioning out of sm:reviewing).
    if state.has_rebase_escalated(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING + already escalated — staying"
        )
        return

    # Tier 2 spawn already in flight — give it room to work.
    if has_live_spawn is not None and has_live_spawn(number):
        log(
            f"[sm-dispatcher] #{number} CONFLICTING — rebase spawn in flight, waiting"
        )
        return

    # Prior Tier 2 spawn is dead but the PR is still CONFLICTING → Tier 3.
    if state.has_rebase_attempted(number):
        reason = "spawned rebase worker dead but PR still CONFLICTING"
        body = render_rebase_escalation_comment(
            number, branch, reason, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would escalate rebase on "
                f"#{number} (branch={branch})"
            )
            report.rebase_escalated += 1
            report.rebase_records.append((number, "tier3-escalation", reason))
            return
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] failed to post rebase escalation on "
                f"#{number}: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.mark_rebase_escalated(number)
        report.rebase_escalated += 1
        report.rebase_records.append((number, "tier3-escalation", reason))
        log(
            f"[sm-dispatcher] #{number} rebase escalation surfaced (Tier 3, "
            f"branch={branch})"
        )
        return

    # Tier 1 — cheap in-process rebase attempt.
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would attempt rebase on "
            f"#{number} (branch={branch})"
        )
        return

    result = attempt_rebase(branch)
    if result.get("ok"):
        report.rebase_pushed += 1
        reason = result.get("reason") or "rebased and pushed"
        report.rebase_records.append((number, "tier1-pushed", reason))
        body = render_rebase_pushed_audit_comment(
            number, branch, timestamp=now_iso()
        )
        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] rebase pushed on #{number} but audit "
                f"comment failed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            # Non-fatal: the push already happened.
        log(
            f"[sm-dispatcher] #{number} auto-rebased and pushed branch={branch}"
        )
        return

    # Tier 2 — rebase failed. Post audit + spawn worker.
    reason = result.get("reason") or "auto-rebase failed"
    audit_body = render_rebase_needed_audit_comment(
        number, branch, reason, timestamp=now_iso()
    )
    try:
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to post rebase-needed audit on "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    try:
        spawn_id = spawn_rebase(issue, repo, branch, reason)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to launch rebase spawn for "
            f"#{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        # Popen / filesystem errors: log + continue; the audit comment
        # was already posted so the cadence trail records the attempt.
        log(
            f"[sm-dispatcher] rebase spawn launch raised OSError on "
            f"#{number}: {exc}"
        )
        return

    if spawn_id is None:
        log(
            f"[sm-dispatcher] rebase spawn returned None for #{number} — "
            f"will retry next cycle"
        )
        return

    state.mark_rebase_attempted(number)
    report.rebase_spawned += 1
    report.rebase_records.append((number, "tier2-spawn", reason))
    log(
        f"[sm-dispatcher] #{number} rebase spawn launched ({spawn_id}, "
        f"branch={branch})"
    )


# ---------------------------------------------------------------------------
# Issue #157 — sm:needs_study handler
# ---------------------------------------------------------------------------


def _comment_author_login(comment: dict[str, Any]) -> str | None:
    """Pull the GitHub login off a ``gh issue view --json comments`` entry.

    ``gh`` returns the ``author`` field as ``{"login": "..."}``; older
    payloads or test fixtures sometimes use a bare string. Returns
    ``None`` on any shape we don't understand so the parser layer can
    apply its own trust check and reject.
    """
    author = comment.get("author") if isinstance(comment, dict) else None
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str):
            return login
    if isinstance(author, str):
        return author
    return None


def _has_prior_study_hint_audit(
    comments: list[dict[str, Any]],
    *,
    trusted_authors: frozenset[str],
) -> bool:
    """Return True iff any comment is a trusted-authored study-hint audit.

    Defense-in-depth dedup: if the local state file was reset, the
    audit comment is the only persistent record that the hint was
    already written. Trust is required so a random commenter pasting
    the prefix can't trick the dispatcher into skipping a real hint
    emission.
    """
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str) or not body.startswith(STUDY_HINT_WRITTEN_PREFIX):
            continue
        login = _comment_author_login(c)
        if isinstance(login, str) and login in trusted_authors:
            return True
    return False


def _matches_resolves_issue(value: Any, issue_number: int) -> bool:
    """Compare a frontmatter ``resolves_issue[s]`` value against an issue number.

    The vault's YAML frontmatter is hand-authored, so a single field can
    show up as an int (``resolves_issue: 212``), a bare string
    (``resolves_issue: 212``), or an octothorpe-prefixed string
    (``resolves_issue: "#212"``). All three should match.
    """
    if isinstance(value, bool):
        # Python booleans are ``int`` subclasses; ``True == 1`` would
        # otherwise spuriously match issue #1.
        return False
    if isinstance(value, int):
        return value == issue_number
    if isinstance(value, str):
        s = value.strip().lstrip("#").strip()
        if not s:
            return False
        try:
            return int(s) == issue_number
        except ValueError:
            return False
    return False


def _find_resolving_research_note(
    issue_number: int,
    research_dir: pathlib.Path,
) -> pathlib.Path | None:
    """Scan ``research_dir`` for a note whose frontmatter resolves ``issue_number``.

    Issue #212. Recognises two frontmatter shapes:

      * ``resolves_issue: <N>``  — scalar; matches when ``<N>`` parses
        to ``issue_number``.
      * ``resolves_issues: [<A>, <B>, ...]`` — flow list; matches when
        any element parses to ``issue_number``.

    Returns the lexicographically-first matching path so the result is
    stable across passes (the typical vault filename starts with
    ``YYYY-MM-DD``, so this is also chronologically-first in practice).
    Returns ``None`` if ``research_dir`` is missing or contains no
    matching note — the caller falls back to the existing comment-poll
    behaviour.

    Read failures on individual files are swallowed: a single
    unreadable note must not block the rest of the scan.
    """
    if not research_dir.is_dir():
        return None
    # Local import — :mod:`alice_indexer.yaml_lite` lives in a sibling
    # package and importing at module top would pull the indexer's
    # dependency surface into every dispatcher session.
    from alice_indexer.yaml_lite import split_frontmatter

    for path in sorted(research_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        if not fm:
            continue
        scalar = fm.get("resolves_issue")
        if scalar is not None and _matches_resolves_issue(scalar, issue_number):
            return path
        listed = fm.get("resolves_issues")
        if isinstance(listed, list):
            for v in listed:
                if _matches_resolves_issue(v, issue_number):
                    return path
    return None


def _current_art_label(
    issue: dict[str, Any], art_whitelist: frozenset[str]
) -> str | None:
    """Return the single whitelisted ``art:*`` label, or None if not exactly one.

    Used by :func:`_process_needs_study` to decide whether to swap the
    art label on study-complete. Multiple art labels or zero matches
    return None — the swap path treats either as "no current label to
    remove" and applies the parsed art label additively.
    """
    names = _label_names(issue)
    arts = [n for n in names if n.startswith("art:") and n in art_whitelist]
    if len(arts) != 1:
        return None
    return arts[0]


def _find_parsed_comment_of_type(
    comments: list[dict[str, Any]],
    expected_type: type,
    *,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
):
    """Scan ``comments`` newest-first and return the first parsed match of
    ``expected_type``, or ``None``.

    Trust is enforced by :func:`alice_sm.comments.parse_comment` itself
    (forged comments from untrusted authors parse to ``None``). Comments
    that aren't ``[SM] <verb>`` shape are silently ignored.
    """
    from alice_sm.comments import parse_comment

    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        login = _comment_author_login(c)
        parsed = parse_comment(
            body,
            login,
            trusted_authors=trusted_authors,
            log=log,
        )
        if isinstance(parsed, expected_type):
            return parsed
    return None


def _process_draft(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:draft → sm:needs_study on a trusted ``[SM] route-to-study`` comment.

    The ``art=<art-label>`` field is optional. When present *and*
    different from the issue's current ``art:*`` label, the dispatcher
    swaps the label atomically with the state transition.
    """
    number = issue["number"]
    decision = evaluate_trust(issue, trusted_authors=trusted_authors)
    if not decision.accepted:
        log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
        report.skipped_trust += 1
        return

    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import RouteToStudy

    parsed = _find_parsed_comment_of_type(
        comments,
        RouteToStudy,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        return

    add_labels = [NEEDS_STUDY_SM_LABEL]
    remove_labels = [DRAFT_SM_LABEL]
    reason = "route-to-study"
    if parsed.art_label is not None:
        current_art = _current_art_label(issue, art_whitelist)
        if parsed.art_label != current_art:
            add_labels.append(parsed.art_label)
            if current_art is not None:
                remove_labels.append(current_art)
        reason += f" art={parsed.art_label}"

    transition_body = render_transition_comment(
        DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"draft → needs_study ({reason})"
        )
        report.transitioned += 1
        report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
        return
    try:
        edit_labels(repo, number, add=add_labels, remove=remove_labels)
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] draft #{number}: "
            f"failed route-to-study transition: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append((number, DRAFT_SM_LABEL, NEEDS_STUDY_SM_LABEL))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"draft → needs_study ({reason})"
    )


def _process_needs_study(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    notes_dir: pathlib.Path,
    research_dir: pathlib.Path,
    trusted_authors: frozenset[str],
    art_whitelist: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """Hint emission + comment-driven transitions for one ``sm:needs_study`` issue.

    Three-phase pass:

      1. **Hint emission.** Idempotent on the ledger field
         ``DispatcherState.needs_study_hinted`` and defensively on the
         ``[SM] study-hint-written`` audit comment from a trusted
         author. On first encounter we write
         ``inner/notes/sm-needs-study-issue<N>.md`` (issue body +
         frontmatter the thinking-agent's wake prompt picks up — see
         #6) and post the audit comment.

      2. **Comment-driven transitions.** Scan comments newest-first
         via :func:`alice_sm.comments.parse_comment`. The first parsed
         study-verb wins:

           * ``study-complete`` → ``sm:selected``, swap ``art:*`` if
             the parsed art label differs from the issue's current one
             (the parser already validated whitelist membership).
           * ``study-blocked``  → ``sm:blocked``.
           * ``study-rejected`` → ``sm:rejected``.
           * ``study-progress`` → no-op (thinking still working);
             ``study-progress`` resets the 7-day stall clock in #4.

         Comments that aren't ``[SM] study-*`` (audit comments,
         human prose) are ignored. The trust check inside each parser
         keeps a random commenter from forging a transition.

      3. **Vault auto-advance (issue #212).** If step 2 finds no
         parsed study-verb yet, scan ``research_dir`` for a note whose
         frontmatter contains ``resolves_issue: <N>`` (scalar) or
         ``resolves_issues: [<N>, ...]`` (flow list). On match the
         dispatcher posts a synthetic
         ``[SM] study-complete art=art:research_note
         findings=[[<note-slug>]] auto-posted=true`` audit comment and
         returns; the next pass picks the comment up via step 2 and
         the issue transitions out of ``sm:needs_study`` naturally.

         Rationale: thinking writes the groomed research note but
         frequently forgets to post the audit comment, leaving the
         issue parked indefinitely (cf. #198/#200/#201 on
         2026-05-14). The mechanics belong in deterministic dispatcher
         code, not the agent's prompt — see the feedback note
         ``procedural-logic-in-code``.

         Idempotency: once the synthetic comment is on the issue, the
         next pass parses it as a real ``study-complete`` (parsers
         tolerate the trailing ``auto-posted=true`` field) and step 2
         transitions normally. Step 3 doesn't re-fire because step 2
         no longer returns ``parsed_study is None``.
    """
    number = issue["number"]

    # ----- step 1: hint emission -----
    # The comments list is needed for both the audit-comment dedup
    # check and the transition scan below, so fetch once and reuse.
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    if state.has_needs_study_hint(number):
        already_hinted = True
    elif _has_prior_study_hint_audit(comments, trusted_authors=trusted_authors):
        # Defensive: state file lost, audit comment persists. Mark in
        # the ledger so the next pass takes the fast path.
        state.mark_needs_study_hint(number)
        already_hinted = True
    else:
        already_hinted = False

    if not already_hinted:
        note_path = notes_dir / f"sm-needs-study-issue{number}.md"
        note_body = render_study_hint_note_body(issue)
        audit_body = render_study_hint_audit_comment(
            number, note_path, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would write hint for #{number} "
                f"at {note_path} and post audit comment"
            )
            report.hinted += 1
        else:
            try:
                notes_dir.mkdir(parents=True, exist_ok=True)
                note_path.write_text(note_body)
            except OSError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to write hint at {note_path}: {exc}"
                )
                return
            try:
                post_comment(repo, number, audit_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to post study-hint-written: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                # The hint file is on disk. We didn't mark the ledger,
                # so the next pass will retry the comment post — the
                # audit-comment scan above will see no prior audit and
                # re-attempt (the file write is idempotent on the
                # known filename).
                return
            state.mark_needs_study_hint(number)
            report.hinted += 1
            log(
                f"[sm-dispatcher] needs_study #{number}: hint written "
                f"at {note_path}"
            )

    # ----- step 2: comment-driven transitions -----
    # Local import to avoid a top-of-module cycle: ``alice_sm.comments``
    # imports ``ART_LABEL_WHITELIST`` / ``TRUSTED_AUTHORS`` from this
    # module.
    from alice_sm.comments import (
        StudyBlocked,
        StudyComplete,
        StudyProgress,
        StudyRejected,
        parse_comment,
    )

    parsed_study = None
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        login = _comment_author_login(c)
        parsed = parse_comment(
            body,
            login,
            trusted_authors=trusted_authors,
            log=log,
        )
        if isinstance(
            parsed, (StudyComplete, StudyBlocked, StudyRejected, StudyProgress)
        ):
            parsed_study = parsed
            break

    if parsed_study is None:
        # Step 3 — vault auto-advance (issue #212). Thinking's research
        # note carries ``resolves_issue: <N>`` in its frontmatter; if
        # we find one matching this issue, synthesize the
        # study-complete audit comment that thinking forgot to post.
        resolving_note = _find_resolving_research_note(number, research_dir)
        if resolving_note is not None:
            slug = resolving_note.stem
            synth_body = render_auto_study_complete_comment(slug)
            if dry_run:
                log(
                    f"[sm-dispatcher] DRY-RUN would auto-post "
                    f"study-complete for #{number} from "
                    f"{resolving_note} (slug={slug})"
                )
                return
            try:
                post_comment(repo, number, synth_body)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] needs_study #{number}: "
                    f"failed to auto-post study-complete: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            log(
                f"[sm-dispatcher] needs_study #{number}: auto-posted "
                f"study-complete from {resolving_note} (slug={slug}); "
                f"transition fires on next pass"
            )
            # Intentional: the freshly-posted comment isn't in the
            # ``comments`` list we already fetched, so the transition
            # has to wait for the next pass. Returning here keeps the
            # one-action-per-pass invariant the rest of the handler
            # follows.
            return
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"no parsed study-* comment yet"
        )
        return

    if isinstance(parsed_study, StudyProgress):
        # Thinking checkpointed but hasn't decided yet. Sub-issue #4
        # will hang the 7-day stall sweep off this branch.
        log(
            f"[sm-dispatcher] needs_study #{number}: thinking still "
            f"working (note=[[{parsed_study.note}]])"
        )
        return

    # Transition verb. Build the (target, reason, add, remove) tuple
    # per verdict, then apply uniformly.
    current_art = _current_art_label(issue, art_whitelist)
    if isinstance(parsed_study, StudyComplete):
        target = ACTIVE_SM_LABEL
        reason = (
            f"study-complete findings=[[{parsed_study.findings}]] "
            f"art={parsed_study.art_label}"
        )
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
        if (
            parsed_study.art_label != current_art
            and current_art is not None
        ):
            add_labels.append(parsed_study.art_label)
            remove_labels.append(current_art)
        elif current_art is None:
            # Issue carried no whitelisted art:* before — apply the
            # parsed one rather than leave the issue art-less.
            add_labels.append(parsed_study.art_label)
    elif isinstance(parsed_study, StudyBlocked):
        target = BLOCKED_SM_LABEL
        reason = f"study-blocked reason=\"{parsed_study.reason}\""
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
    elif isinstance(parsed_study, StudyRejected):
        target = REJECTED_SM_LABEL
        reason = f"study-rejected reason=\"{parsed_study.reason}\""
        add_labels = [target]
        remove_labels = [NEEDS_STUDY_SM_LABEL]
    else:  # pragma: no cover — exhaustively matched above.
        return

    transition_body = render_transition_comment(
        NEEDS_STUDY_SM_LABEL, target, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"needs_study → {target} ({reason})"
        )
        report.transitioned += 1
        report.transitions.append((number, NEEDS_STUDY_SM_LABEL, target))
        return
    try:
        edit_labels(repo, number, add=add_labels, remove=remove_labels)
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] needs_study #{number}: "
            f"failed to transition to {target}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append((number, NEEDS_STUDY_SM_LABEL, target))
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"needs_study → {target} ({reason})"
    )


# ---------------------------------------------------------------------------
# Issue #164 — sm:designing / design_review / designed / compacting / building
# ---------------------------------------------------------------------------


def _find_parsed_comment_of_type(
    comments: list[dict[str, Any]],
    expected_types: type | tuple[type, ...],
    *,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
):
    """Scan ``comments`` newest-first and return the first parsed match, or None.

    The trust check is enforced inside the parsers themselves
    (:mod:`alice_sm.comments`), so an untrusted commenter pasting one
    of the canonical verbs parses to ``None`` and is silently skipped.
    """
    from alice_sm.comments import parse_comment

    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str):
            continue
        login = _comment_author_login(c)
        parsed = parse_comment(
            body,
            login,
            trusted_authors=trusted_authors,
            log=log,
        )
        if isinstance(parsed, expected_types):
            return parsed
    return None


def _process_designing(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:designing → sm:design_review on a fresh ``[SM] design-ready`` comment.

    The thinking-agent is running and producing a design draft. When it
    emits ``[SM] design-ready note=[[...]]`` the dispatcher relabels the
    issue ``sm:design_review`` and posts a ``[SM] design-ready-audit``
    so Speaking's review loop knows to pick it up.

    No design-ready comment yet → no action; the agent is still
    working. The handler is otherwise idempotent: once the label flips
    to ``sm:design_review`` the issue's next pass goes through
    :func:`_process_design_review` instead.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import DesignReady

    parsed = _find_parsed_comment_of_type(
        comments,
        DesignReady,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"no [SM] design-ready comment yet"
        )
        return

    reason = f"design-ready note=[[{parsed.note}]]"
    transition_body = render_transition_comment(
        DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL, reason
    )
    audit_body = render_design_ready_audit_comment(
        number, parsed.note, timestamp=now_iso()
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"designing → design_review ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGN_REVIEW_SM_LABEL],
            remove=[DESIGNING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
        post_comment(repo, number, audit_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designing #{number}: "
            f"failed to transition to design_review: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNING_SM_LABEL, DESIGN_REVIEW_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designing → design_review ({reason})"
    )


def _process_design_review(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str],
) -> None:
    """sm:design_review → sm:designed | sm:designing | sm:rejected.

    Speaking owns this gate. Two parseable verbs from a trusted author:

      * ``[SM] design-approved`` → ``sm:designed``. Clears the per-issue
        revision counter so a future re-entry starts fresh.
      * ``[SM] design-revise reason=... feedback=[[...]]`` → bumps
        :attr:`DispatcherState.design_revisions` for the issue. While
        the count is at or below :data:`DESIGN_REVISION_CAP` the issue
        bounces back to ``sm:designing`` for another iteration.
        On the (cap+1)th bounce the issue is routed to ``sm:rejected``
        with a ``[SM] design-revisions-capped`` audit so the operator
        sees why the loop terminated.

    Comments that aren't ``[SM] design-{approved,revise}`` are
    ignored; we wait for the next pass.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import DesignApproved, DesignRevise

    parsed = _find_parsed_comment_of_type(
        comments,
        (DesignApproved, DesignRevise),
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"awaiting design-approved / design-revise"
        )
        return

    if isinstance(parsed, DesignApproved):
        target = DESIGNED_SM_LABEL
        reason = "design-approved"
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, target, reason
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → designed (approved)"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, target)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[target],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to designed: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append((number, DESIGN_REVIEW_SM_LABEL, target))
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → designed (approved)"
        )
        return

    # ----- design-revise branch -----
    # Use the pre-existing count to decide: if the count is already at
    # the cap, the new revise comment is the (cap+1)th bounce — reject.
    # Otherwise increment and bounce back to designing.
    prior = state.design_revision_count(number)
    if prior >= DESIGN_REVISION_CAP:
        capped_count = prior + 1
        reason = (
            f"design-revisions-capped count={capped_count} "
            f"cap={DESIGN_REVISION_CAP}"
        )
        transition_body = render_transition_comment(
            DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL, reason
        )
        audit_body = render_design_revisions_capped_comment(
            number, capped_count, timestamp=now_iso()
        )
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would transition #{number}: "
                f"design_review → rejected ({reason})"
            )
            report.transitioned += 1
            report.transitions.append(
                (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
            )
            return
        try:
            edit_labels(
                repo,
                number,
                add=[REJECTED_SM_LABEL],
                remove=[DESIGN_REVIEW_SM_LABEL],
            )
            post_comment(repo, number, transition_body)
            post_comment(repo, number, audit_body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] design_review #{number}: "
                f"failed to transition to rejected: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_design_revisions(number)
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, REJECTED_SM_LABEL)
        )
        log(
            f"[sm-dispatcher] transitioned #{number}: "
            f"design_review → rejected ({reason})"
        )
        return

    # Under the cap → iterate.
    new_count = state.bump_design_revisions(number)
    reason = (
        f'design-revise iteration={new_count} '
        f'reason="{parsed.reason}" feedback=[[{parsed.feedback}]]'
    )
    transition_body = render_transition_comment(
        DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL, reason
    )
    if dry_run:
        # Roll back the bump so dry-run is side-effect-free on the
        # ledger; we already incremented above to render the reason.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"design_review → designing ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[DESIGNING_SM_LABEL],
            remove=[DESIGN_REVIEW_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        # Undo the ledger bump — the GH side didn't move, so the next
        # pass should observe the same revise comment and retry.
        state.design_revisions[number] = new_count - 1
        if state.design_revisions[number] == 0:
            state.clear_design_revisions(number)
        log(
            f"[sm-dispatcher] design_review #{number}: "
            f"failed to bounce to designing: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGN_REVIEW_SM_LABEL, DESIGNING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"design_review → designing ({reason})"
    )


def _process_designed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    live_spawn_dir: Callable[[int], pathlib.Path | None] | None,
    dry_run: bool,
    log: Callable[[str], None],
    has_live_speaking_spawn: Callable[[int], bool] | None = None,
    count_running_speaking: Callable[[], int] | None = None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    max_concurrent_speaking_spawns: int = MAX_CONCURRENT_SPEAKING_SPAWNS,
) -> None:
    """sm:designed → next-phase routing for one issue.

    For ``(sm:designed, art:code)`` (sub-issue 7 / #186): spawn the
    per-issue speaking-agent build lane (:func:`spawn_speaking_agent`),
    then transition the issue ``sm:designed → sm:building`` so
    :func:`_process_building` waits for the speaking-agent's draft PR
    on the next pass.

    For other artifact labels with no ``(sm:designed, *)`` row in
    :data:`SPAWN_MAP`: fall back to the legacy compact-signal behavior
    (locate the live thinking-agent spawn dir, drop a
    ``compact.signal``, transition ``sm:designed → sm:compacting``).
    The compact lane is preserved so an in-flight pre-cutover agent on
    a non-art:code task can finish without the dispatcher stranding it
    at ``sm:designed``.

    Speaking-lane spawn helpers default to ``None`` for tests that
    only exercise the compact-signal path; production wires them in
    :func:`run`.
    """
    number = issue["number"]
    art_label = "art:unknown"
    for name in _label_names(issue):
        if name.startswith("art:") and name in ART_LABEL_WHITELIST:
            art_label = name
            break

    spawn_config = SPAWN_MAP.get((DESIGNED_SM_LABEL, art_label))
    persona = spawn_config.get("persona") if spawn_config else None

    if persona == "speaking":
        _designed_spawn_speaking(
            issue=issue,
            repo=repo,
            number=number,
            art_label=art_label,
            report=report,
            post_comment=post_comment,
            edit_labels=edit_labels,
            has_live_speaking_spawn=has_live_speaking_spawn,
            count_running_speaking=count_running_speaking,
            spawn_speaking=spawn_speaking,
            max_concurrent_speaking_spawns=max_concurrent_speaking_spawns,
            dry_run=dry_run,
            log=log,
        )
        return

    # Legacy compact-signal lane (pre-cutover thinking-agent that
    # restarts itself in build mode). Kept so an in-flight non-art:code
    # issue at sm:designed isn't stranded by the cutover.
    spawn_path: pathlib.Path | None = None
    if live_spawn_dir is not None:
        spawn_path = live_spawn_dir(number)

    if spawn_path is None:
        log(
            f"[sm-dispatcher] designed #{number}: WARNING — no live "
            f"per-issue spawn dir; cannot write compact signal. "
            f"Leaving at sm:designed for the next pass / human triage."
        )
        return

    reason = f"compact signal at {spawn_path / COMPACT_SIGNAL_FILENAME}"
    transition_body = render_transition_comment(
        DESIGNED_SM_LABEL, COMPACTING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"designed → compacting ({reason})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNED_SM_LABEL, COMPACTING_SM_LABEL)
        )
        return

    signal_path = spawn_path / COMPACT_SIGNAL_FILENAME
    try:
        signal_path.write_text("compact\n")
    except OSError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: failed to write "
            f"compact signal at {signal_path}: {exc}"
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[COMPACTING_SM_LABEL],
            remove=[DESIGNED_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: "
            f"failed to transition to compacting: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNED_SM_LABEL, COMPACTING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designed → compacting ({reason})"
    )


def _designed_spawn_speaking(
    *,
    issue: dict[str, Any],
    repo: str,
    number: int,
    art_label: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    has_live_speaking_spawn: Callable[[int], bool] | None,
    count_running_speaking: Callable[[], int] | None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None,
    max_concurrent_speaking_spawns: int,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:designed → sm:building: spawn the speaking-agent build lane.

    Sub-issue 7 (#186). Mirrors the spawn block in
    :func:`_process_selected` for the thinking lane: dedup on a live
    speaking-lane spawn dir, gate on the lane's concurrency cap, then
    invoke ``spawn_speaking`` and transition the issue's label to
    ``sm:building`` so the next dispatcher pass picks the draft PR up
    via :func:`_process_building`.

    The transition runs BEFORE the spawn — without it, the next pass
    would re-enter ``_process_designed`` and double-spawn (the live
    spawn dir dedup would only catch this AFTER the first spawn has
    written its pidfile; a slow Popen could allow a race). Posting the
    label change first also matches the pattern in
    ``_process_selected`` for the v1 worker pool.
    """
    if (
        spawn_speaking is None
        or has_live_speaking_spawn is None
        or count_running_speaking is None
    ):
        log(
            f"[sm-dispatcher] designed #{number}: speaking-lane spawn "
            f"machinery not wired — leaving at sm:designed"
        )
        return

    if has_live_speaking_spawn(number):
        log(
            f"[sm-dispatcher] designed #{number}: live speaking spawn "
            f"dir already running — skipping spawn"
        )
        return

    live = count_running_speaking()
    if live >= max_concurrent_speaking_spawns:
        log(
            f"[sm-dispatcher] designed #{number}: speaking concurrency "
            f"cap reached ({live}/{max_concurrent_speaking_spawns}) — "
            f"queued for next pass"
        )
        return

    reason = "build-started: speaking-agent spawned"
    transition_body = render_transition_comment(
        DESIGNED_SM_LABEL, BUILDING_SM_LABEL, reason
    )

    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would spawn speaking on #{number} "
            f"art={art_label} "
            f"(running={live}/{max_concurrent_speaking_spawns}) and "
            f"transition designed → building"
        )
        report.spawned += 1
        report.spawn_records.append((number, art_label, "<dry-run>"))
        report.transitioned += 1
        report.transitions.append(
            (number, DESIGNED_SM_LABEL, BUILDING_SM_LABEL)
        )
        return

    # Spawn first — the speaking-agent posts its own
    # [SM] speaking-spawn-started audit comment before launching the
    # shim, so failure to spawn leaves a recoverable audit trail and
    # doesn't move the label.
    try:
        spawn_id = spawn_speaking(issue, art_label, repo)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: failed to spawn "
            f"speaking-agent: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    except OSError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: speaking spawn "
            f"OS error: {exc}"
        )
        return
    if spawn_id is None:
        return
    report.spawned += 1
    report.spawn_records.append((number, art_label, spawn_id))

    # Transition designed → building so _process_building picks the
    # draft PR up on the next pass.
    try:
        edit_labels(
            repo,
            number,
            add=[BUILDING_SM_LABEL],
            remove=[DESIGNED_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] designed #{number}: "
            f"failed to transition to building: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, DESIGNED_SM_LABEL, BUILDING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"designed → building (speaking spawn_id={spawn_id})"
    )


def _process_compacting(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    list_comments: ListCommentsFn,
    has_live_spawn: Callable[[int], bool] | None,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:compacting → sm:building on the agent's ``[SM] build-started`` comment.

    The thinking-agent is mid-compaction (container restart in
    progress). When it comes back up in BUILD mode it posts
    ``[SM] build-started`` — that's the dispatcher's signal to flip
    the label so :func:`_process_building` takes over and watches for
    the PR.

    The ``has_live_spawn`` callable is consulted as a confidence
    check: if the agent died during compaction (no live spawn) we
    still honor the build-started signal but log a warning, since the
    audit trail says the agent claimed it started; humans can sort it
    out from there.
    """
    number = issue["number"]
    try:
        comments = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to list comments: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    from alice_sm.comments import BuildStarted

    parsed = _find_parsed_comment_of_type(
        comments,
        BuildStarted,
        trusted_authors=trusted_authors,
        log=log,
    )
    if parsed is None:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"awaiting [SM] build-started"
        )
        return

    if has_live_spawn is not None and not has_live_spawn(number):
        log(
            f"[sm-dispatcher] compacting #{number}: WARNING — "
            f"build-started seen but no live spawn dir; agent may have "
            f"died during compaction. Transitioning anyway per audit trail."
        )

    reason = "build-started"
    transition_body = render_transition_comment(
        COMPACTING_SM_LABEL, BUILDING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"compacting → building (build-started)"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[BUILDING_SM_LABEL],
            remove=[COMPACTING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] compacting #{number}: "
            f"failed to transition to building: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, COMPACTING_SM_LABEL, BUILDING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"compacting → building (build-started)"
    )


def _process_building(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """sm:building → sm:reviewing once a linked PR appears.

    Mirrors the T1 sub-path inside :func:`_process_selected`: an
    open linked PR is the "build complete" signal. The build-phase
    agent opens its PR as a draft (per ``per-issue-build.md``); the
    dispatcher relabels and hands off to the existing reviewing-state
    pipeline (CI + verify + Sonnet review).
    """
    number = issue["number"]
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] building #{number}: "
            f"failed to look up linked PR: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    if pr is None:
        log(
            f"[sm-dispatcher] building #{number}: "
            f"no linked PR yet — staying"
        )
        return
    pr_state = (pr.get("state") or "").upper()
    if pr_state != "OPEN":
        log(
            f"[sm-dispatcher] building #{number}: linked PR is "
            f"{pr_state!r} (not OPEN) — not transitioning"
        )
        return

    pr_url = pr.get("url") or "<unknown>"
    reason = f"PR opened: {pr_url}"
    transition_body = render_transition_comment(
        BUILDING_SM_LABEL, REVIEWING_SM_LABEL, reason
    )
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would transition #{number}: "
            f"building → reviewing ({pr_url})"
        )
        report.transitioned += 1
        report.transitions.append(
            (number, BUILDING_SM_LABEL, REVIEWING_SM_LABEL)
        )
        return
    try:
        edit_labels(
            repo,
            number,
            add=[REVIEWING_SM_LABEL],
            remove=[BUILDING_SM_LABEL],
        )
        post_comment(repo, number, transition_body)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] building #{number}: "
            f"failed to transition to reviewing: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.transitioned += 1
    report.transitions.append(
        (number, BUILDING_SM_LABEL, REVIEWING_SM_LABEL)
    )
    log(
        f"[sm-dispatcher] transitioned #{number}: "
        f"building → reviewing ({pr_url})"
    )


def _process_stale_closed(
    *,
    issue: dict[str, Any],
    repo: str,
    report: RunReport,
    post_comment: PostCommentFn,
    edit_labels: EditLabelsFn,
    find_linked_pr: FindLinkedPRFn,
    pr_merge_status: PRMergeStatusFn,
    master_ci_status: MasterCIStatusFn,
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Phase 1.6 sweep: route a closed issue with a non-terminal ``sm:*``
    label to its correct terminal state.

    The issue is already closed — we never re-open and we never close
    further; only labels and the ``[SM] transition`` audit comment are
    written. Decision tree:

      * linked PR merged + master CI green → ``sm:done``
      * linked PR merged + master CI red   → ``sm:rejected``
        (the merge happened but broke master; the work shipped-but-bad
        and downstream tracking should treat it as rejected pending
        follow-up.)
      * linked PR closed-unmerged          → ``sm:rejected``
      * no linked PR at all                → ``sm:rejected``
        (manual close or supersession — there's no merge artifact, so
        the safe terminal state is rejected.)

    A pending master CI verdict is treated as "wait" — we stay at the
    stale label and let the next pass re-evaluate. This keeps the
    sweep idempotent under flaky CI: we'd rather leave a stale label
    one more cadence than commit to ``sm:done`` before the build is
    actually green.
    """
    number = issue["number"]
    stale_label = _current_sm_label(issue)
    if stale_label is None:
        # Defensive: the helper already filters to non-terminal sm:*,
        # but if some odd label set sneaks through (multi-sm, typo),
        # don't guess.
        names = _label_names(issue)
        sm_labels_seen = [n for n in names if n.startswith("sm:")]
        log(
            f"[sm-dispatcher] sweep skip #{number}: "
            f"ambiguous sm:* label set {sm_labels_seen!r}"
        )
        return
    if stale_label in TERMINAL_SM_LABELS:
        # Belt-and-suspenders: helper's client-side filter should have
        # excluded this. If we got here anyway, do nothing.
        return

    # Resolve linked PR + outcome.
    try:
        pr = find_linked_pr(repo, number)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep: failed PR lookup for #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return

    target_label: str
    reason: str
    if pr is None:
        # Closed with no PR linkage: manual close, supersession, or
        # a bot that closed without a "Closes #" reference. Without a
        # merge artifact the safe terminal is rejected.
        target_label = REJECTED_SM_LABEL
        reason = "issue closed without linked PR (manual close or supersession)"
    else:
        pr_number = pr.get("number")
        pr_state = (pr.get("state") or "").upper()
        if not isinstance(pr_number, int):
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"linked PR payload missing number ({pr!r})"
            )
            return
        if pr_state == "MERGED":
            try:
                merge_info = pr_merge_status(repo, pr_number)
            except GHCommandError as exc:
                log(
                    f"[sm-dispatcher] sweep: merge-status failed for "
                    f"PR #{pr_number}: {exc}"
                )
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            sha = merge_info.get("merge_commit_oid")
            pr_url = merge_info.get("pr_url") or pr.get("url") or "<unknown>"
            if not sha:
                log(
                    f"[sm-dispatcher] sweep skip #{number}: "
                    f"PR #{pr_number} reports MERGED but no merge_commit_oid"
                )
                return
            try:
                ci = master_ci_status(repo, sha)
            except GHCommandError as exc:
                log(f"[sm-dispatcher] sweep: CI lookup failed for {sha[:8]}: {exc}")
                if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                    raise
                return
            conclusion = ci.get("conclusion")
            if conclusion is None or conclusion == "pending":
                # Hold the stale label one more cadence rather than
                # commit to a terminal before CI returns a verdict.
                log(
                    f"[sm-dispatcher] sweep wait #{number}: "
                    f"PR #{pr_number} merged but master CI is {conclusion!r}"
                )
                return
            if conclusion == "success":
                target_label = DONE_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha}, "
                    f"master CI success ({pr_url})"
                )
            else:
                # CI red post-merge: the work shipped but broke master.
                # Downgrade to rejected so a human picks up the follow-up;
                # we don't have the Phase 2 quality-gate plumbing yet.
                run_url = ci.get("run_url") or "<unknown>"
                target_label = REJECTED_SM_LABEL
                reason = (
                    f"closed-by-merge sweep: PR #{pr_number} merged at {sha} "
                    f"but master CI failure ({run_url})"
                )
        elif pr_state == "CLOSED":
            target_label = REJECTED_SM_LABEL
            reason = f"PR #{pr_number} closed without merge"
        else:
            # PR is still OPEN (or some state we don't recognise) and
            # the issue is closed. Possible scenarios: the PR was
            # un-merged after the fact, or the issue was hand-closed
            # while a PR still exists. Either way, don't sweep — let a
            # human (or a later phase) decide.
            log(
                f"[sm-dispatcher] sweep skip #{number}: "
                f"issue closed but linked PR #{pr_number} is {pr_state!r}"
            )
            return

    body = render_transition_comment(stale_label, target_label, reason)
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would sweep #{number}: "
            f"{stale_label} → {target_label} ({reason})"
        )
        report.swept += 1
        report.transitions.append((number, stale_label, target_label))
        return
    try:
        edit_labels(
            repo,
            number,
            add=[target_label],
            remove=[stale_label],
        )
        post_comment(repo, number, body)
    except GHCommandError as exc:
        log(f"[sm-dispatcher] sweep failed to transition #{number}: {exc}")
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    report.swept += 1
    report.transitions.append((number, stale_label, target_label))
    log(
        f"[sm-dispatcher] swept #{number}: "
        f"{stale_label} → {target_label} (issue stays closed)"
    )


# Prefix the worker is instructed (by the ``(sm:selected, art:research_note)``
# row of :data:`SPAWN_ARTIFACT_HANDLERS`) to post on completion:
#
#     [SM] transition from=selected to=done reason="research note at <path>"
#
# This is the canonical machine-readable "I finished writing the research
# note" signal. Recognizing it as a valid close-gate input is what makes
# issue #195's fix work — see :func:`_research_close_signal`.
_RESEARCH_WORKER_DONE_PREFIX = "[SM] transition from=selected to=done"


def _research_close_signal(
    repo: str,
    number: int,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
) -> tuple[bool, str | None]:
    """Inspect issue comments for a trusted close-gate signal (issues #174, #195).

    Returns ``(satisfied, reason_suffix)``:

      * ``satisfied`` — True iff a trusted comment on the issue justifies
        closing the OPEN + ``sm:done`` + ``art:research_note`` row.
      * ``reason_suffix`` — short human-readable tag describing *which*
        signal landed (``"exit-transition recorded"`` for the explicit
        ``[SM] exit-transition=<value>`` form, ``"worker self-transition
        to=done"`` for the worker's own ``[SM] transition`` audit
        comment). Used by :func:`_process_open_done` to render the close
        audit comment so the trail records why the gate opened.

    Two signals are accepted (in priority order):

    1. ``[SM] exit-transition=<value> findings=[[...]]?`` — the explicit
       "what should happen to this note next" verb introduced by #174
       (``disseminate`` | ``spawn-code`` | ``both``). Preferred when
       present because the value carries downstream-action metadata.

    2. ``[SM] transition from=selected to=done reason="..."`` — the
       worker's own audit comment, posted as instructed by the
       ``(sm:selected, art:research_note)`` dispatch row. This is the
       canonical "research-writer worker finished" signal and the only
       one any producer in this codebase actually emits today. Without
       this fallback the close path is dead-on-arrival (see #195).

    Pre-existing rows (closed manually after #181 shipped — #105, #178,
    #179, #180) have only signal 2 because no producer of signal 1 was
    ever wired up. The fallback closes them on the next dispatcher pass
    so the migration story is "manual close was a transitional hack" and
    not "manual close forever."

    Why not relax further (e.g. accept human prose like
    ``Exit-transition: disseminate``)? Because the rest of the SM
    protocol is strict machine-readable shapes and adding loose
    natural-language matching here invites false positives on adjacent
    issue threads. Signal 2 is the worker's contract-mandated audit
    comment from a trusted author — strict enough, broad enough.

    We import :mod:`alice_sm.comments` lazily to avoid the import cycle
    (comments imports ``TRUSTED_AUTHORS`` / ``ART_LABEL_WHITELIST`` from
    this module at top level).
    """
    from alice_sm import comments as cm  # local import — avoid cycle

    try:
        items = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to list comments for #{number} while "
            f"checking research-note close signal: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return False, None

    worker_done_seen = False
    for item in items:
        body = item.get("body")
        author = item.get("author")
        # ``gh issue view --json comments`` returns
        # ``[{"author": {"login": ...}, "body": ...}, ...]``. Accept the
        # bare-login shape for test-fixture readability.
        if isinstance(author, dict):
            login = author.get("login")
        elif isinstance(author, str):
            login = author
        else:
            login = None
        if not isinstance(body, str):
            continue
        # Signal 1 — explicit exit-transition verb (preferred). Take the
        # first hit and short-circuit; the explicit verb wins over the
        # implicit worker audit comment when both are present.
        parsed = cm.parse_exit_transition(
            body, login, trusted_authors=trusted_authors, log=lambda _m: None
        )
        if parsed is not None:
            return True, "exit-transition recorded"
        # Signal 2 — worker self-transition audit comment. Keep
        # scanning so signal 1 still wins if it shows up later in the
        # comment stream, but remember that we saw signal 2.
        if (
            body.startswith(_RESEARCH_WORKER_DONE_PREFIX)
            and login in trusted_authors
        ):
            worker_done_seen = True

    if worker_done_seen:
        return True, "worker self-transition to=done"
    return False, None


def _has_exit_transition_comment(
    repo: str,
    number: int,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    log: Callable[[str], None],
) -> bool:
    """Backwards-compatible boolean wrapper around :func:`_research_close_signal`.

    Pre-existing callers / tests that only need a yes-or-no answer
    continue to work. New code should use :func:`_research_close_signal`
    directly so the reason suffix is preserved in the audit trail.
    """
    satisfied, _reason = _research_close_signal(
        repo, number, list_comments, trusted_authors, log
    )
    return satisfied


def _process_open_done(
    *,
    issue: dict[str, Any],
    repo: str,
    state: DispatcherState,
    report: RunReport,
    post_comment: PostCommentFn,
    close_issue: CloseIssueFn,
    list_comments: ListCommentsFn,
    trusted_authors: frozenset[str],
    dry_run: bool,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_iso,
) -> None:
    """Close OPEN issues at ``sm:done`` once their exit gate is satisfied (issue #174).

    The ``art:research_note`` worker flips ``sm:selected → sm:done``
    directly without producing a PR, so the canonical close path
    (:func:`_process_reviewing` → merged PR → ``gh issue close``) never
    fires for these tasks. Without this handler the issue stays in the
    open list forever and the work looks "stuck" from the viewer's
    lens even though the vault note exists.

    Behaviour for ``art:research_note`` issues:

      * If a trusted close-signal comment is present (see
        :func:`_research_close_signal` — either ``[SM] exit-transition=
        <value>`` or the worker's own ``[SM] transition from=selected
        to=done`` audit comment) → close the issue and emit a
        ``[SM] transition from=done to=done reason=...`` audit comment
        recording the close. Clears the ``exit_required_posted`` ledger
        entry.
      * If missing → post the ``[SM] exit-transition-required`` reminder
        once (deduped via the state ledger + a defensive comment scan
        so a state-file reset doesn't re-spam) and stay.

    The two-signal gate (#195 follow-up to #174): the original #174
    design required the explicit ``[SM] exit-transition=<value>`` verb,
    but no producer in this codebase emits it — workers post the
    ``[SM] transition from=selected to=done`` audit comment per the
    ``(sm:selected, art:research_note)`` dispatch row. Without the
    fallback, the close path was dead-on-arrival and every research-note
    completion required ``gh issue close`` by hand (#105, #178, #179,
    #180 on 2026-05-13).

    For any other artifact (``art:code`` / ``art:config_change`` /
    ``art:experiment``) an OPEN-at-``sm:done`` issue is a state-machine
    aberration — the close should have happened on the
    ``sm:reviewing → sm:done`` transition. Log the surprise and skip;
    a human picks it up. We do NOT auto-close art:code without the
    PR-merged + CI-green pedigree the canonical path enforces.
    """
    number = issue["number"]
    names = _label_names(issue)
    art_labels = [n for n in names if n in ART_LABEL_WHITELIST]
    if not art_labels:
        log(
            f"[sm-dispatcher] open-done skip #{number}: no whitelisted art:* label "
            f"({names!r})"
        )
        return
    art_label = sorted(art_labels)[0]

    if art_label != "art:research_note":
        log(
            f"[sm-dispatcher] open-done skip #{number}: OPEN at {DONE_SM_LABEL} with "
            f"{art_label} — expected the canonical sm:reviewing → sm:done path "
            f"to have closed this; leaving for human review"
        )
        return

    # art:research_note — gate on a trusted close-signal comment. Two
    # shapes are accepted (see :func:`_research_close_signal`):
    #
    #   1. ``[SM] exit-transition=<value>`` — explicit, preferred,
    #      carries disseminate/spawn-code/both metadata. Issue #174.
    #   2. ``[SM] transition from=selected to=done reason=...`` — the
    #      worker's own audit comment. Per #195, this is the only signal
    #      any producer in this codebase actually emits, so the close
    #      path closes on it; otherwise the migration story is "manual
    #      close forever" and that defeats the auto-sweep.
    try:
        has_signal, signal_reason = _research_close_signal(
            repo, number, list_comments, trusted_authors, log
        )
    except GHCommandError:
        # Fatal gh error (auth / rate limit) — re-raised by helper.
        raise

    if has_signal:
        suffix = signal_reason or "exit-transition recorded"
        reason = f"art:research_note + {suffix}"
        body = render_transition_comment(DONE_SM_LABEL, DONE_SM_LABEL, reason)
        if dry_run:
            log(
                f"[sm-dispatcher] DRY-RUN would close #{number}: "
                f"art:research_note + {suffix}"
            )
            report.research_closed += 1
            return
        try:
            close_issue(repo, number)
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(
                f"[sm-dispatcher] open-done failed to close #{number}: {exc}"
            )
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                raise
            return
        state.clear_exit_required(number)
        report.research_closed += 1
        log(
            f"[sm-dispatcher] open-done closed #{number}: "
            f"art:research_note + {suffix}"
        )
        return

    # No exit-transition yet — post the reminder once.
    if state.has_exit_required(number):
        log(
            f"[sm-dispatcher] open-done #{number}: still waiting on exit-transition "
            f"(reminder already posted)"
        )
        return

    # Defensive comment-prefix scan: catches the state-file-reset case
    # where the ledger entry was lost but the reminder is already on
    # the issue. Without this, a wiped state file would re-spam the
    # comment on every open research_note + done issue.
    try:
        existing = list_comments(repo, number)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] failed to scan comments for #{number} before "
            f"posting exit-transition-required: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    for item in existing:
        body_text = item.get("body")
        author = item.get("author")
        if isinstance(author, dict):
            login = author.get("login")
        elif isinstance(author, str):
            login = author
        else:
            login = None
        if (
            isinstance(body_text, str)
            and body_text.startswith(EXIT_TRANSITION_REQUIRED_PREFIX)
            and login in trusted_authors
        ):
            # Adopt the on-issue evidence as the dedup signal even
            # though our local ledger was empty.
            state.mark_exit_required(number)
            log(
                f"[sm-dispatcher] open-done #{number}: exit-transition-required "
                f"already on issue (ledger reset); marking and skipping"
            )
            return

    reminder = render_exit_transition_required_comment(number, timestamp=now_iso())
    if dry_run:
        log(
            f"[sm-dispatcher] DRY-RUN would post exit-transition-required on "
            f"#{number}"
        )
        report.exit_required_posted += 1
        return
    try:
        post_comment(repo, number, reminder)
    except GHCommandError as exc:
        log(
            f"[sm-dispatcher] open-done failed to post exit-transition-required "
            f"on #{number}: {exc}"
        )
        if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
            raise
        return
    state.mark_exit_required(number)
    report.exit_required_posted += 1
    log(
        f"[sm-dispatcher] open-done #{number}: posted exit-transition-required "
        f"(art:research_note + {DONE_SM_LABEL} + OPEN)"
    )


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    list_issues: ListIssuesFn | None = None,
    list_stale_closed: ListIssuesFn | None = None,
    list_open_done: ListIssuesFn | None = None,
    post_comment: PostCommentFn = gh_post_comment,
    edit_labels: EditLabelsFn = gh_edit_labels,
    close_issue: CloseIssueFn = gh_close_issue,
    find_linked_pr: FindLinkedPRFn = gh_find_linked_pr,
    pr_merge_status: PRMergeStatusFn = gh_get_pr_merge_status,
    pr_mergeable: PRMergeableFn | None = None,
    master_ci_status: MasterCIStatusFn = gh_get_master_ci_status,
    has_live_spawn: Callable[[int], bool] | None = None,
    live_spawn_dir: Callable[[int], pathlib.Path | None] | None = None,
    count_running: Callable[[], int] | None = None,
    spawn: Callable[[dict[str, Any], str, str], str | None] | None = None,
    has_live_thinking_spawn: Callable[[int], bool] | None = None,
    count_running_thinking: Callable[[], int] | None = None,
    spawn_thinking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    has_live_speaking_spawn: Callable[[int], bool] | None = None,
    count_running_speaking: Callable[[], int] | None = None,
    spawn_speaking: Callable[[dict[str, Any], str, str], str | None] | None = None,
    spawn_rebase: Callable[[dict[str, Any], str, str, str], str | None] | None = None,
    attempt_rebase: Callable[[str], dict[str, Any]] | None = None,
    enable_rebase: bool = True,
    get_issue: Callable[[int], dict[str, Any] | None] | None = None,
    proactive_reap: Callable[[], tuple[int, int]] | None = None,
    enable_spawn: bool = True,
    max_concurrent_spawns: int = MAX_CONCURRENT_SPAWNS,
    max_concurrent_thinking_spawns: int = MAX_CONCURRENT_THINKING_SPAWNS,
    max_concurrent_speaking_spawns: int = MAX_CONCURRENT_SPEAKING_SPAWNS,
    post_merge_cleanup: PostMergeCleanupFn | None = None,
    enable_cleanup: bool = True,
    worker_repo_path: pathlib.Path = WORKER_REPO_PATH,
    pr_files: PRFilesFn | None = None,
    verify_pr: VerifyFn | None = None,
    enable_verify: bool = True,
    list_comments: ListCommentsFn | None = None,
    notes_dir: pathlib.Path = NEEDS_STUDY_HINT_DIR,
    research_dir: pathlib.Path = RESEARCH_NOTES_DIR,
    trusted_authors: frozenset[str] = TRUSTED_AUTHORS,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    now_iso: Callable[[], str] = _now_iso,
) -> tuple[int, RunReport]:
    """Run one dispatcher pass. Returns ``(exit_code, report)``.

    Exit codes:
      0  poll completed (zero or more comments posted; state saved)
      1  ``gh`` failed in a way we can't recover from this pass —
         auth, rate limit, transport error. State NOT written;
         s6 supervisor will retry on the next cadence.
    """
    if list_issues is None:
        list_issues = gh_list_sm_issues
    if list_stale_closed is None:
        list_stale_closed = gh_list_stale_closed_sm_issues
    if list_open_done is None:
        list_open_done = gh_list_open_done_sm_issues
    if list_comments is None:
        list_comments = gh_list_issue_comments
    if enable_spawn:
        # Default to live production wiring when the caller hasn't
        # provided test fixtures. enable_spawn=False is the test escape
        # hatch — leaves has_live_spawn / count_running / spawn as
        # None, so :func:`_process_selected` short-circuits the spawn
        # branch.
        if has_live_spawn is None:
            def has_live_spawn(number: int) -> bool:
                return has_live_spawn_for_issue(number, SPAWN_DIR, log=log)
        if live_spawn_dir is None:
            def live_spawn_dir(number: int) -> pathlib.Path | None:
                return find_live_spawn_dir_for_issue(number, SPAWN_DIR)
        if count_running is None:
            def count_running() -> int:
                return count_running_spawns(SPAWN_DIR, log=log)
        if spawn is None:
            def spawn(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )
        if get_issue is None:
            def get_issue(number: int) -> dict[str, Any] | None:
                return gh_get_issue(repo, number)
        if proactive_reap is None:
            def proactive_reap() -> tuple[int, int]:
                return proactive_reap_dead_spawns(
                    SPAWN_DIR, get_issue=get_issue, log=log
                )
        # Sub-issue 7 (#186): SM v2 thinking + speaking lane bindings.
        # Each lane has its own spawn dir, concurrency cap, and audit
        # prefix so they don't share dedup / capacity with the v1
        # worker pool.
        if has_live_thinking_spawn is None:
            def has_live_thinking_spawn(number: int) -> bool:
                return has_live_thinking_spawn_for_issue(
                    number, SM_THINKING_SPAWN_DIR, log=log
                )
        if count_running_thinking is None:
            def count_running_thinking() -> int:
                return count_running_thinking_spawns(
                    SM_THINKING_SPAWN_DIR, log=log
                )
        if spawn_thinking is None:
            def spawn_thinking(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_thinking_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )
        if has_live_speaking_spawn is None:
            def has_live_speaking_spawn(number: int) -> bool:
                return has_live_speaking_spawn_for_issue(
                    number, SM_SPEAKING_SPAWN_DIR, log=log
                )
        if count_running_speaking is None:
            def count_running_speaking() -> int:
                return count_running_speaking_spawns(
                    SM_SPEAKING_SPAWN_DIR, log=log
                )
        if spawn_speaking is None:
            def spawn_speaking(
                issue: dict[str, Any], art_label: str, repo: str
            ) -> str | None:
                return spawn_speaking_agent(
                    issue,
                    art_label,
                    repo,
                    post_comment=post_comment,
                    log=log,
                    now_iso=now_iso,
                )

    # Issue #127 — bind the production cleanup callable when enabled and
    # not explicitly injected. Tests opt out with ``enable_cleanup=False``
    # (mirrors the ``enable_spawn=False`` escape hatch) or pass a fake.
    if enable_cleanup and post_merge_cleanup is None and not dry_run:
        def post_merge_cleanup(branch: str | None, issue_number: int) -> None:
            _post_merge_cleanup(
                repo_path=worker_repo_path,
                branch=branch,
                issue_number=issue_number,
                log=log,
            )

    # Issue #128 — bind the production verifier + PR-files fetcher when
    # the caller hasn't injected fakes. ``enable_verify=False`` and the
    # ``ALICE_VERIFY_ENABLED`` env var both flip the gate off, in which
    # case ``_process_reviewing`` receives ``verify_pr=None`` and goes
    # straight from CI-green to ``sm:done`` (pre-#128 behavior). The
    # env-var path is the operational kill-switch; the kwarg path is
    # the test escape hatch.
    if enable_verify and verify_pr is None and _verify_enabled():
        if pr_files is None:
            pr_files = gh_get_pr_files
        verify_pr = default_verifier
    elif not enable_verify or not _verify_enabled():
        # Operator/test explicitly disabled — None signals "skip the
        # whole gate" to ``_process_reviewing``.
        verify_pr = None

    # Issue #173 — bind the production auto-rebase callables. The
    # ``enable_rebase=False`` flag and the absence of an injected
    # ``spawn_rebase`` (with ``enable_spawn=False``) both leave the
    # CONFLICTING handler a silent no-op, matching the existing test
    # escape-hatch shape for ``_process_reviewing``.
    if enable_rebase and not dry_run:
        if pr_mergeable is None:
            pr_mergeable = gh_get_pr_mergeable
        if attempt_rebase is None:
            def attempt_rebase(branch: str) -> dict[str, Any]:
                return _attempt_auto_rebase(
                    branch=branch,
                    repo_path=worker_repo_path,
                    log=log,
                )
        if enable_spawn and spawn_rebase is None:
            def spawn_rebase(
                issue: dict[str, Any],
                repo: str,
                branch: str,
                reason: str,
            ) -> str | None:
                return spawn_rebase_agent(
                    issue,
                    repo,
                    branch,
                    reason,
                    log=log,
                )
    else:
        # Disabled: leave all three None so _handle_conflicting_pr no-ops.
        pr_mergeable = None
        attempt_rebase = None
        spawn_rebase = None

    report = RunReport()

    # Issue #142 — proactive sweep of stale ``active/`` spawn dirs.
    # Without this, dead dirs only get reaped when a new spawn for the
    # same issue fires (via ``has_live_spawn_for_issue``), so they
    # accumulate visibly in /running and /runs after their issue closes.
    # Best-effort: a failure here must not block the main poll.
    if proactive_reap is not None:
        try:
            proactive_reap()
        except OSError as exc:
            log(f"[sm-dispatcher] proactive-reap failed: {exc}")

    try:
        issues = list_issues(repo)
    except GHCommandError as exc:
        if exc.looks_like_auth_failure:
            log(f"[sm-dispatcher] auth failure listing {repo}: {exc}")
        elif exc.looks_like_rate_limit:
            log(f"[sm-dispatcher] rate-limited listing {repo}: {exc}")
        else:
            log(f"[sm-dispatcher] failed to list {repo}: {exc}")
        # Do NOT write partial state. The s6 supervisor retries.
        return 1, report

    state = load_state(state_path)
    report.polled = len(issues)

    fatal_exit = False
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            log(f"[sm-dispatcher] skipping issue with non-integer number: {number!r}")
            continue

        sm_label = _current_sm_label(issue)
        if sm_label is None:
            # Either zero or >1 whitelisted ``sm:*`` labels (or only
            # non-canonical ones like ``sm:bogus``). Treated as a
            # trust-filter rejection — same v0 semantics, just hoisted
            # to the outer loop now that we route by label.
            names = _label_names(issue)
            sm_labels_seen = [n for n in names if n.startswith("sm:")]
            log(
                f"[sm-dispatcher] skipping #{number}: "
                f"expected exactly one whitelisted sm:* label, got {sm_labels_seen!r}"
            )
            report.skipped_trust += 1
            continue

        try:
            if sm_label == ACTIVE_SM_LABEL:
                _process_selected(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    has_live_spawn=has_live_spawn,
                    count_running=count_running,
                    spawn=spawn,
                    max_concurrent_spawns=max_concurrent_spawns,
                    has_live_thinking_spawn=has_live_thinking_spawn,
                    count_running_thinking=count_running_thinking,
                    spawn_thinking=spawn_thinking,
                    max_concurrent_thinking_spawns=max_concurrent_thinking_spawns,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                    get_issue=get_issue,
                )
            elif sm_label == REVIEWING_SM_LABEL:
                _process_reviewing(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    close_issue=close_issue,
                    find_linked_pr=find_linked_pr,
                    pr_merge_status=pr_merge_status,
                    master_ci_status=master_ci_status,
                    pr_files=pr_files,
                    verify_pr=verify_pr,
                    post_merge_cleanup=post_merge_cleanup,
                    pr_mergeable=pr_mergeable,
                    attempt_rebase=attempt_rebase,
                    spawn_rebase=spawn_rebase,
                    has_live_spawn=has_live_spawn,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == NEEDS_STUDY_SM_LABEL:
                _process_needs_study(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    notes_dir=notes_dir,
                    research_dir=research_dir,
                    trusted_authors=trusted_authors,
                    art_whitelist=ART_LABEL_WHITELIST,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DRAFT_SM_LABEL:
                _process_draft(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    art_whitelist=ART_LABEL_WHITELIST,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == DESIGNING_SM_LABEL:
                _process_designing(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DESIGN_REVIEW_SM_LABEL:
                _process_design_review(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            elif sm_label == DESIGNED_SM_LABEL:
                _process_designed(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    live_spawn_dir=live_spawn_dir,
                    has_live_speaking_spawn=has_live_speaking_spawn,
                    count_running_speaking=count_running_speaking,
                    spawn_speaking=spawn_speaking,
                    max_concurrent_speaking_spawns=max_concurrent_speaking_spawns,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == COMPACTING_SM_LABEL:
                _process_compacting(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    list_comments=list_comments,
                    has_live_spawn=has_live_spawn,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                )
            elif sm_label == BUILDING_SM_LABEL:
                _process_building(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    dry_run=dry_run,
                    log=log,
                )
            else:
                # Phase 1.5 doesn't act on validating / done / rejected /
                # blocked. Listed for visibility only.
                log(f"[sm-dispatcher] #{number} at {sm_label} — no action this phase")
        except GHCommandError as exc:
            # Auth/rate-limit re-raised from inner handlers — bail.
            fatal_exit = True
            log(f"[sm-dispatcher] fatal gh error: {exc}")
            break

    # Phase 1.6 — sweep pass: catch closed issues that still carry a
    # non-terminal ``sm:*`` label and route them to a terminal state.
    # Runs only if the open-issue pass didn't bail with a fatal gh
    # error; the sweep is best-effort and shouldn't override a fatal
    # signal from the primary poll.
    if not fatal_exit:
        try:
            stale_issues = list_stale_closed(repo)
        except GHCommandError as exc:
            # The sweep is a defense-in-depth pass; failing to list
            # closed issues is not fatal to the primary loop. Log and
            # continue so dedup state still saves.
            if exc.looks_like_auth_failure:
                log(f"[sm-dispatcher] sweep auth failure listing {repo}: {exc}")
                fatal_exit = True
            elif exc.looks_like_rate_limit:
                log(f"[sm-dispatcher] sweep rate-limited listing {repo}: {exc}")
                fatal_exit = True
            else:
                log(f"[sm-dispatcher] sweep failed to list closed {repo}: {exc}")
            stale_issues = []
        for issue in stale_issues:
            number = issue.get("number")
            if not isinstance(number, int):
                log(
                    f"[sm-dispatcher] sweep skip issue with non-integer "
                    f"number: {number!r}"
                )
                continue
            try:
                _process_stale_closed(
                    issue=issue,
                    repo=repo,
                    report=report,
                    post_comment=post_comment,
                    edit_labels=edit_labels,
                    find_linked_pr=find_linked_pr,
                    pr_merge_status=pr_merge_status,
                    master_ci_status=master_ci_status,
                    dry_run=dry_run,
                    log=log,
                )
            except GHCommandError as exc:
                fatal_exit = True
                log(f"[sm-dispatcher] fatal gh error during sweep: {exc}")
                break

    # Issue #174 — open-done sweep: OPEN issues at ``sm:done`` are the
    # art:research_note close-stragglers. The worker flipped the label
    # but no ``gh issue close`` ever fired (no PR pedigree means
    # ``_process_reviewing`` never owned the close). The handler
    # enforces the ``[SM] exit-transition`` gate and closes the issue.
    # Best-effort, same as the closed-stale sweep.
    if not fatal_exit:
        try:
            open_done_issues = list_open_done(repo)
        except GHCommandError as exc:
            if exc.looks_like_auth_failure:
                log(
                    f"[sm-dispatcher] open-done sweep auth failure listing "
                    f"{repo}: {exc}"
                )
                fatal_exit = True
            elif exc.looks_like_rate_limit:
                log(
                    f"[sm-dispatcher] open-done sweep rate-limited listing "
                    f"{repo}: {exc}"
                )
                fatal_exit = True
            else:
                log(
                    f"[sm-dispatcher] open-done sweep failed to list "
                    f"{repo}: {exc}"
                )
            open_done_issues = []
        for issue in open_done_issues:
            number = issue.get("number")
            if not isinstance(number, int):
                log(
                    f"[sm-dispatcher] open-done sweep skip issue with non-integer "
                    f"number: {number!r}"
                )
                continue
            try:
                _process_open_done(
                    issue=issue,
                    repo=repo,
                    state=state,
                    report=report,
                    post_comment=post_comment,
                    close_issue=close_issue,
                    list_comments=list_comments,
                    trusted_authors=trusted_authors,
                    dry_run=dry_run,
                    log=log,
                    now_iso=now_iso,
                )
            except GHCommandError as exc:
                fatal_exit = True
                log(
                    f"[sm-dispatcher] fatal gh error during open-done sweep: {exc}"
                )
                break

    if fatal_exit:
        # Persist what we did manage so dedup state for any successful
        # hello posts isn't lost.
        if not dry_run:
            save_state(state_path, state)
        return 1, report

    if not dry_run:
        save_state(state_path, state)

    log(
        f"[sm-dispatcher] done — polled={report.polled} "
        f"posted={report.posted} "
        f"transitioned={report.transitioned} "
        f"swept={report.swept} "
        f"spawned={report.spawned} "
        f"hinted={report.hinted} "
        f"cleaned_up={report.cleaned_up} "
        f"verify_pass={report.verify_pass} "
        f"verify_skip={report.verify_skip} "
        f"verify_failed={report.verify_failed} "
        f"rebase_pushed={report.rebase_pushed} "
        f"rebase_spawned={report.rebase_spawned} "
        f"rebase_escalated={report.rebase_escalated} "
        f"research_closed={report.research_closed} "
        f"exit_required={report.exit_required_posted} "
        f"skipped_dedup={report.skipped_dedup} "
        f"skipped_trust={report.skipped_trust}"
    )
    return 0, report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the State Machine v0/v1.5/v2 dispatcher."
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repo in <org>/<name> form (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_DIR / DEFAULT_STATE_FILE),
        help="path to sm-dispatcher-state.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the comments/transitions that would be made, "
        "don't touch GitHub or state",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    exit_code, _ = run(
        repo=args.repo,
        state_path=pathlib.Path(args.state),
        dry_run=args.dry_run,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
