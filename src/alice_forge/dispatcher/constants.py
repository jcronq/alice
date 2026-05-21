"""Constants for the SM dispatcher.

All module-level constants, label whitelists, audit-comment prefixes,
filesystem paths, concurrency caps, and the SPAWN_MAP live here. Other
dispatcher submodules import what they need from this module — they
never re-define a label or prefix locally.

The :func:`_now_iso` helper sits here because it's the timestamp source
for everything written into the constants-defined audit comment shapes;
moving it to its own ``_time.py`` would just create one more two-line
file for no readability win.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Defaults + constants
# ---------------------------------------------------------------------------

DEFAULT_REPO = "jcronq/alice"
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_STATE_FILE = "sm-dispatcher-state.json"

# Path to ``alice.config.json``. Mirrors :data:`alice_watchers.github.DEFAULT_MIND`
# / ``"config" / "alice.config.json"`` — kept as a constant rather than
# imported to avoid a watcher → dispatcher import cycle and to let tests
# override per-call via :func:`load_dispatcher_repos(config_path=...)`.
DEFAULT_MIND_DIR = pathlib.Path("/home/alice/alice-mind")
DEFAULT_DISPATCHER_CONFIG_PATH = DEFAULT_MIND_DIR / "config" / "alice.config.json"

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
# ``watchers.github.DEFAULT_MIND / "inner/notes"`` — kept as a
# path constant rather than imported to avoid a watcher → dispatcher
# import cycle and to let tests override per-call.
NEEDS_STUDY_HINT_DIR = pathlib.Path("/home/alice/alice-mind/inner/notes")

# Issue #235 — directory where Speaking's surface_watcher polls. The
# draft handler writes a triage surface here on the first cycle that
# encounters an ``sm:draft`` issue without a trusted ``[SM] route-to-study``
# comment, so Speaking can route the draft instead of having it sit
# silently. Mirrors ``alice_mind / "inner/surface"`` — same rationale as
# :data:`NEEDS_STUDY_HINT_DIR` for keeping it as a path constant rather
# than importing.
TRIAGE_SURFACE_DIR = pathlib.Path("/home/alice/alice-mind/inner/surface")

# Issue #235 — cap on the issue body included in the triage surface.
# Matches the auto-fix dispatch surface body budget (Part B of
# ``2026-05-05-issue-dispatcher-design``) so Speaking gets enough context
# to decide without re-fetching, while keeping the surface file small.
TRIAGE_SURFACE_BODY_CHAR_LIMIT = 1500

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
# implementation lives at :mod:`alice_forge.thinking_shim`; sub-issue 3
# replaces it with the real PhaseRunner dispatch.
THINKING_SHIM_MODULE = "alice_forge.thinking_shim"

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
# implementation lives at :mod:`alice_forge.speaking_shim`; the real
# PhaseRunner.PER_ISSUE_BUILD dispatch (with Task-tool sub-agent
# invocation) lands in the speaking sub-issue that replaces the shim.
SPEAKING_SHIM_MODULE = "alice_forge.speaking_shim"

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
# if the merged PR touched any file under ``src/viewer/``, the
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
# the viewer package, including templates and static assets. Kept
# narrow so dispatcher / speaking / sm changes don't accidentally
# trigger the viewer probe.
VERIFY_VIEWER_PATH_PREFIX = "src/viewer/"

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
# Issue #261 — per-repo dispatcher config (multi-repo support)
# ---------------------------------------------------------------------------
#
# Pre-#261 the dispatcher hardcoded ``DEFAULT_REPO`` + ``WORKER_REPO_PATH``
# and only operated on ``jcronq/alice``. Multi-repo support (e.g. wiring
# ``jcronq/cozyhem-engine`` into the same dispatcher process) needs a
# per-repo lookup of ``(slug → checkout path, labels-configured flag)``.
# We read that mapping from the same ``alice.config.json`` the github
# watcher already uses, under a new ``sm_dispatcher.repos`` block so the
# two readers stay independently configurable.
#
# Backwards compat: when the config file is missing OR the
# ``sm_dispatcher`` block is absent, :func:`load_dispatcher_repos`
# falls back to a single :class:`RepoConfig` for ``DEFAULT_REPO`` /
# ``WORKER_REPO_PATH`` with ``labels_configured=True``. The alice flow
# is byte-identical to pre-#261 in that case.


@dataclass(frozen=True)
class RepoConfig:
    """One repo's dispatcher wiring.

    * ``slug`` — full ``owner/name`` GitHub identifier (e.g. ``jcronq/alice``).
    * ``checkout_path`` — local worker checkout. Workers spawned on this
      repo's issues operate inside this tree (matches the pre-#261
      :data:`WORKER_REPO_PATH` semantics on a per-repo basis).
    * ``labels_configured`` — True iff the repo carries the SM v2 ``sm:*``
      label taxonomy. When False (``cozyhem-engine``-style), the
      dispatcher runs in "relaxed mode" and does not log the trust-filter
      rejection for issues missing the strict label set — labels become
      a Speaking/Thinking convenience rather than a gate.
    """

    slug: str
    checkout_path: pathlib.Path
    labels_configured: bool = False


def _coerce_repo_entry(entry: Any) -> RepoConfig | None:
    """Coerce one raw config dict into a :class:`RepoConfig`.

    Returns None for malformed entries (missing slug or checkout_path) so
    the caller can log and skip without crashing the whole loop. Accepts
    either a dict with explicit keys or a bare string (treated as ``slug``
    with checkout_path defaulting to ``DEFAULT_MIND_DIR.parent / <name>``
    — useful for tests but explicit dicts are preferred in production).
    """
    if isinstance(entry, str):
        slug = entry.strip()
        if "/" not in slug:
            return None
        _, name = slug.split("/", 1)
        return RepoConfig(
            slug=slug,
            checkout_path=pathlib.Path("/home/alice") / name,
            labels_configured=False,
        )
    if not isinstance(entry, dict):
        return None
    slug = entry.get("slug")
    checkout_path = entry.get("checkout_path")
    if not isinstance(slug, str) or "/" not in slug:
        return None
    if not isinstance(checkout_path, str) or not checkout_path.strip():
        return None
    labels_configured = bool(entry.get("labels_configured", False))
    return RepoConfig(
        slug=slug.strip(),
        checkout_path=pathlib.Path(checkout_path).expanduser(),
        labels_configured=labels_configured,
    )


def load_dispatcher_repos(
    *,
    config_path: pathlib.Path = DEFAULT_DISPATCHER_CONFIG_PATH,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> list[RepoConfig]:
    """Read the ``sm_dispatcher.repos`` block from alice.config.json.

    Returns a list of :class:`RepoConfig`. Missing file, unreadable file,
    or missing ``sm_dispatcher`` block ⇒ a single-element fallback list
    pointing at ``DEFAULT_REPO`` + ``WORKER_REPO_PATH`` with
    ``labels_configured=True``. This preserves pre-#261 single-repo
    behavior whenever the config is absent.

    Expected JSON shape::

        {
          "sm_dispatcher": {
            "repos": [
              {"slug": "jcronq/alice",
               "checkout_path": "/home/alice/alice",
               "labels_configured": true},
              {"slug": "jcronq/cozyhem-engine",
               "checkout_path": "/home/alice/cozyhem-engine",
               "labels_configured": false}
            ]
          }
        }

    Malformed entries are logged and skipped — a single bad row must not
    silently drop the whole repo list. If the result would be empty
    (every entry malformed), the fallback applies.
    """
    fallback = [
        RepoConfig(
            slug=DEFAULT_REPO,
            checkout_path=WORKER_REPO_PATH,
            labels_configured=True,
        )
    ]
    if not config_path.is_file():
        return fallback
    try:
        raw = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[sm-dispatcher] failed to read {config_path}: {exc}")
        return fallback
    block = (raw or {}).get("sm_dispatcher")
    if not isinstance(block, dict):
        return fallback
    entries = block.get("repos")
    if not isinstance(entries, list) or not entries:
        return fallback
    out: list[RepoConfig] = []
    for entry in entries:
        coerced = _coerce_repo_entry(entry)
        if coerced is None:
            log(f"[sm-dispatcher] skipping malformed repo entry: {entry!r}")
            continue
        out.append(coerced)
    if not out:
        return fallback
    return out
