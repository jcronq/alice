"""Spawn machinery for the dispatcher.

Three concurrent spawn lanes share the same on-disk shape but distinct
concurrency caps and audit prefixes:

* v1 code-worker pool (:func:`spawn_agent`) — claude-cli backed,
  ``art:config_change`` / ``art:research_note`` / ``art:experiment``.
  Also reused by :func:`spawn_rebase_agent` (issue #173 Tier 2).
* SM v2 thinking-design lane (:func:`spawn_thinking_agent`,
  issue #156) — claude-agent-sdk Opus, per-issue design notes.
* SM v2 speaking-build lane (:func:`spawn_speaking_agent`,
  issue #184) — claude-agent-sdk Opus, per-issue build dispatch.

The :data:`SPAWN_MAP` table in :mod:`alice_forge.dispatcher.constants`
selects which lane handles each ``(sm_state, art_label)`` combination.

Shared helpers (live spawn detection, reaping, session-JSONL capture,
proactive sweep) sit at the top of the module; each lane's
``compose_*``/``render_*``/``spawn_*`` trio follows.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable

from alice_forge.dispatcher.constants import (
    ACTIVE_SM_LABEL,
    ART_LABEL_WHITELIST,
    BASE_BRANCH,
    CLAUDE_BIN_FALLBACK,
    CLAUDE_BIN_PREFERRED,
    CLAUDE_PROJECTS_DIR,
    DESIGN_REVIEWER_PHASE,
    DESIGN_REVIEWER_RUNTIME_LABEL,
    DESIGN_REVIEWER_SHIM_MODULE,
    DESIGN_REVIEWER_SPAWN_STARTED_PREFIX,
    PYTHON_BIN_FALLBACK,
    PYTHON_BIN_PREFERRED,
    SESSION_ID_FILENAME,
    SESSION_JSONL_FILENAME,
    SM_DESIGN_REVIEWER_SPAWN_DIR,
    SM_SPEAKING_SPAWN_DIR,
    SM_THINKING_SPAWN_DIR,
    SPAWN_DIR,
    SPAWN_STARTED_PREFIX,
    SPEAKING_BUILD_COMPLETE_PREFIX,
    SPEAKING_PHASE_PER_ISSUE_BUILD,
    SPEAKING_RUNTIME_LABEL,
    SPEAKING_SHIM_MODULE,
    SPEAKING_SPAWN_STARTED_PREFIX,
    TERMINAL_SM_LABELS,
    THINKING_PHASE_PER_ISSUE_DESIGN,
    THINKING_RUNTIME_LABEL,
    THINKING_SHIM_MODULE,
    THINKING_SPAWN_STARTED_PREFIX,
    WORKER_REPO_PATH,
    _now_iso,
)
from alice_forge.dispatcher.errors import GHCommandError
from alice_forge.dispatcher.gh import gh_post_comment
from alice_forge.dispatcher.trust import _author_login, _current_sm_label, _label_names
from alice_forge.dispatcher.types import PostCommentFn


def _current_spawn_map() -> dict[tuple[str, str], dict[str, str]]:
    """Return the dispatcher module's live ``SPAWN_MAP`` attribute.

    Tests override ``SPAWN_MAP`` via ``mock.patch.object(sm, "SPAWN_MAP", ...)``
    (see ``test_phase2_unrecognized_artifact_label_skips_spawn``). Pre-split,
    every consumer of ``SPAWN_MAP`` inside ``dispatcher.py`` resolved the
    name via a global lookup in the dispatcher module's own ``__dict__``,
    so the patch took effect. After the split, naive callers in this
    submodule (and in ``handlers/``) would bind ``SPAWN_MAP`` from
    ``constants`` at import time — invisible to the patch. This
    indirection routes each access back through
    ``sys.modules['alice_forge.dispatcher']``, restoring the contract.
    """
    return sys.modules["alice_forge.dispatcher"].SPAWN_MAP


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


def count_running_design_reviewer_spawns(
    spawn_dir: pathlib.Path = SM_DESIGN_REVIEWER_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Mirror of :func:`count_running_spawns` scoped to the design-reviewer lane.

    Issue #344. The design-reviewer lane has its own concurrency cap
    (:data:`MAX_CONCURRENT_DESIGN_REVIEWER_SPAWNS`) — short-lived Opus
    review calls run in parallel without draining build/design capacity.
    """
    return count_running_spawns(spawn_dir, log=log)


def has_live_design_reviewer_spawn_for_issue(
    issue_number: int,
    spawn_dir: pathlib.Path = SM_DESIGN_REVIEWER_SPAWN_DIR,
    *,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Returns True when a live design-reviewer spawn exists for this issue.

    Mirrors :func:`has_live_speaking_spawn_for_issue`. Consults only
    :data:`SM_DESIGN_REVIEWER_SPAWN_DIR` so a live thinking-design or
    speaking-build spawn on the same issue does not satisfy this check.
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


def _resolve_agent_spec(spawn_config: dict[str, str]) -> Any:
    """Return the :class:`AgentSpec` for the row's ``agent_spec`` name.

    Phase 4 of #194 (#321): the ``agent_spec`` field is now mandatory
    on every SPAWN_MAP row that reaches :func:`compose_spawn_prompt`,
    and the registered spec is the *only* source of behavioral rules
    for the v1 worker prompt. Missing or unknown names raise
    :class:`KeyError` — there is no longer a legacy inline-field
    fallback path to drop into. Callers that need to test compose
    behavior must pass a row with a registered ``agent_spec``.
    """
    try:
        name = spawn_config["agent_spec"]
    except KeyError as exc:
        raise KeyError(
            "SPAWN_MAP row is missing required ``agent_spec`` field "
            "(Phase 4 of #194: behavioral rules now flow exclusively "
            "through core.agent_library.default_registry)"
        ) from exc
    if not name:
        raise KeyError(
            "SPAWN_MAP row carries an empty ``agent_spec`` field "
            "(Phase 4 of #194: every row must name a registered "
            "AgentSpec)"
        )
    # Lazy import so this module stays importable in test paths that
    # stub out the registry.
    from core.agent_library import default_registry

    return default_registry.get(name)


# Persona-axis values whose v1 worker prompt frames the task as
# "produce a research note." Anything else falls through to the
# code-edit framing. Centralised so a future persona (e.g. a
# documentation-writer flavor) can opt into the research framing by
# declaring the right persona on its :class:`AgentSpec` without
# adding another branch here.
_RESEARCH_FRAMING_PERSONAS: frozenset[str] = frozenset({"research-writer"})


def compose_spawn_prompt(
    issue: dict[str, Any],
    spawn_config: dict[str, str],
) -> str:
    """Render the full prompt text fed to the spawned ``claude`` agent.

    The prompt embeds the issue body verbatim, the artifact label, the
    issue source (author identity), and the registered
    :class:`AgentSpec`'s assembled system prompt (the rendered
    ``## Constraint: <id>`` blocks from each
    :attr:`AgentSpec.behavioral_constraints` rule).

    Phase 4 of #194 (#321): the inline ``system_prompt_role`` /
    ``instruction_trailer`` fields on the SPAWN_MAP row are gone —
    every worker-pool row now references a registered AgentSpec via
    ``agent_spec``, and behavioral rules flow exclusively through the
    spec's :meth:`AgentSpec.assembled_system_prompt`. The role tag in
    the prompt header derives from :attr:`AgentSpec.name`; the task
    framing branches on :attr:`AgentSpec.persona` (research-writer →
    "produce the research note" framing, everything else → "implement
    the change" framing).

    Raises :class:`KeyError` when the row is missing a recognised
    ``agent_spec`` — :func:`_resolve_agent_spec` enforces the
    invariant.
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

    # Hard lookup — every SPAWN_MAP row that reaches compose must
    # carry a registered ``agent_spec``. KeyError propagates so a
    # misconfigured row fails loud rather than silently producing a
    # prompt without behavioral rules.
    spec = _resolve_agent_spec(spawn_config)

    role = spec.name
    # Rendered behavioral-rule blocks. ``None`` when the spec has no
    # constraints AND no base ``append_system_prompt`` — render as the
    # empty string so the prompt body still parses, but a registered
    # worker spec without rules is almost certainly a misconfig.
    trailer = spec.assembled_system_prompt() or ""

    if spec.persona in _RESEARCH_FRAMING_PERSONAS:
        task_framing = (
            "Your task: produce the research note described above. "
            "Read prior art in the vault, write the note with proper "
            "frontmatter and wikilinks, then post the SM transition "
            "comment when finished."
        )
    else:
        task_framing = (
            "Your task: implement the change described above. Read the "
            "relevant code first, write a focused diff, run tests, and "
            "open a PR."
        )

    # The agent name itself is intentionally left out of the literal
    # prompt — the SM task is repo-anchored, not persona-anchored, and
    # the runtime persona system owns identity rendering. The role
    # label (the spec's ``name``: ``code-worker`` / ``config-worker``
    # / ``research-writer`` / ...) carries the behavioral framing.
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
    spawn_config = _current_spawn_map().get((sm_state, art_label))
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
         ``python -m alice_forge.thinking_shim --spawn-dir <dir> --session-id <uuid>``
         with stdout/stderr to log files, ``start_new_session=True`` so
         the agent survives the dispatcher exiting.
      6. Write PID to ``pidfile``.

    Returns the ``spawn_id`` on success, or ``None`` if the issue number
    isn't an integer (defensive — the dispatcher's main loop already
    filters those out). Does NOT wait for the spawned subprocess.

    The wire-up into ``_process_selected`` lands in sub-issue 7 (the
    SPAWN_MAP cutover); this issue ships only the machinery. The real
    entrypoint replaces :mod:`alice_forge.thinking_shim` in sub-issue 3.
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
        f"issue: #{number}\n"
        f"phase: {SPEAKING_PHASE_PER_ISSUE_BUILD}\n"
        f"art: {art_label}\n"
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
         ``python -m alice_forge.speaking_shim --spawn-dir <dir> --session-id <uuid> --mode build``
         with stdout/stderr to log files, ``start_new_session=True`` so
         the agent survives the dispatcher exiting.
      6. Write PID to ``pidfile``.

    Returns the ``spawn_id`` on success, or ``None`` if the issue number
    isn't an integer (defensive — the dispatcher's main loop already
    filters those out). Does NOT wait for the spawned subprocess.

    The wire-up into ``_process_designed`` lands in a separate sub-issue
    once spawn + entrypoint are tested in isolation; this issue ships
    only the machinery. The real entrypoint replaces
    :mod:`alice_forge.speaking_shim`.
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
# Issue #344 — design-reviewer spawn (sm:design_review phase)
# ---------------------------------------------------------------------------


def compose_design_reviewer_spawn_prompt(
    issue: dict[str, Any],
    *,
    design_note_path: str | pathlib.Path,
) -> str:
    """Compose the prompt the design-reviewer CLI loads from its
    spawn dir.

    Frontmatter mirrors the speaking-build prompt (``issue:`` / ``phase:`` /
    ``art:`` / ``design_note:``) so the CLI can reuse the same
    :func:`alice_speaking.cli.perissue.parse_frontmatter` helper. The
    body is intentionally short — the per-issue-design-review.md prompt
    that the CLI assembles for the kernel carries the real instructions.
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
        f"---\n"
        f"issue: #{number}\n"
        f"phase: {DESIGN_REVIEWER_PHASE}\n"
        f"art: {art_label}\n"
        f"design_note: {design_note_path}\n"
        f"---\n"
        f"You are a design-reviewer agent on SM task #{number}.\n"
        f"\n"
        f"Issue: #{number}\n"
        f"Title: {title}\n"
        f"Source: {source_label}\n"
        f"Artifact type: {art_label}\n"
        f"Design note: {design_note_path}\n"
        f"\n"
        f"Issue body:\n"
        f"{body}\n"
        f"\n"
        f"Read the design note path above. The CLI loads it for you and "
        f"composes the full per-issue-design-review.md prompt. End your "
        f"response with one line: `[SM] design-approved ...` or "
        f"`[SM] design-revise reason=\"...\" ...`.\n"
    )


def render_design_reviewer_spawn_started_comment(
    number: int,
    art_label: str,
    spawn_id: str,
    *,
    phase: str = DESIGN_REVIEWER_PHASE,
    runtime: str = DESIGN_REVIEWER_RUNTIME_LABEL,
    timestamp: str | None = None,
) -> str:
    """Produce the literal ``[SM] design-reviewer-spawn-started ...`` audit comment."""
    ts = timestamp or _now_iso()
    return (
        f"{DESIGN_REVIEWER_SPAWN_STARTED_PREFIX} task=#{number} "
        f"artifact={art_label} phase={phase} runtime={runtime} "
        f"spawn_id={spawn_id} ts={ts}"
    )


def spawn_design_reviewer_agent(
    issue: dict[str, Any],
    art_label: str,
    repo: str,
    *,
    design_note_path: str | pathlib.Path,
    spawn_dir: pathlib.Path = SM_DESIGN_REVIEWER_SPAWN_DIR,
    python_bin: str | None = None,
    shim_module: str = DESIGN_REVIEWER_SHIM_MODULE,
    phase: str = DESIGN_REVIEWER_PHASE,
    runtime: str = DESIGN_REVIEWER_RUNTIME_LABEL,
    post_comment: PostCommentFn = gh_post_comment,
    popen: Callable[..., Any] = subprocess.Popen,
    now_iso: Callable[[], str] = _now_iso,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    clock: Callable[[], float] | None = None,
    new_session_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> str | None:
    """Spawn a per-issue design-reviewer for an sm:design_review issue.

    Issue #344. Sibling of :func:`spawn_speaking_agent` and
    :func:`spawn_thinking_agent`. The on-disk shape is identical
    (``<spawn_dir>/<spawn_id>/`` with ``prompt.txt`` / ``pidfile`` /
    ``stdout.log`` / ``stderr.log`` / ``session_id``) but the lane is
    separate: distinct concurrency cap, distinct audit prefix, distinct
    spawn dir, distinct CLI entrypoint.

    Caller must pass ``design_note_path`` — the design-ready audit
    comment carries the slug, and the dispatcher resolves it to a real
    filesystem path before spawning (same plumbing as
    :func:`spawn_speaking_agent`).

    Returns the ``spawn_id`` on success, or ``None`` if the issue number
    isn't an integer. Does NOT wait for the spawned subprocess.
    """
    if clock is None:
        clock = time.time

    number = issue.get("number")
    if not isinstance(number, int):
        log(
            f"[sm-dispatcher] cannot spawn design-reviewer on "
            f"non-integer issue number: {number!r}"
        )
        return None

    if python_bin is None:
        python_bin = resolve_python_bin()

    spawn_id = f"spawn-{number}-{int(clock())}"
    work_dir = spawn_dir / spawn_id
    work_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = compose_design_reviewer_spawn_prompt(
        issue, design_note_path=design_note_path
    )
    prompt_path = work_dir / "prompt.txt"
    prompt_path.write_text(prompt_text)

    session_id = new_session_id()
    (work_dir / SESSION_ID_FILENAME).write_text(session_id)

    body = render_design_reviewer_spawn_started_comment(
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
            f"[sm-dispatcher] failed to post design-reviewer-spawn-started "
            f"on #{number}: {exc} — aborting spawn"
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
        f"[sm-dispatcher] spawned design-reviewer {spawn_id} (pid={pid}) "
        f"on #{number} art={art_label} phase={phase} "
        f"session_id={session_id}"
    )
    return spawn_id
