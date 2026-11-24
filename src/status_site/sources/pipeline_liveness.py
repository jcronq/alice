"""Liveness probe for SM pipeline rows (slice 2b re-scoped).

The status-site's *In flight* tier is defined by the SM label set
(``building`` / ``reviewing``) — see
:data:`status_site.sources.pipeline.INFLIGHT_STATES`. Those labels lie
when a worker dies silently: the label stays, the agent is gone, and
the tier shows ghost tasks until someone manually sweeps them. This
module cross-checks each ``in_flight`` row against at least one live
signal before admitting it to the tier. Rows with no live signal get
demoted to ``blocked`` with a short ``blocked_reason`` string so the
Blocked tier can display *why* the task was demoted.

Live signals — ANY one is sufficient to mark a row live:

1. **Worktree** — the issue number appears in the branch or path of a
   ``git worktree list --porcelain`` block. The dispatcher names the
   worktree it hands to a worker after the issue number
   (``feat/858-…``, ``alice-858-wt/``), so a surviving worktree is
   strong evidence that a worker is / was mid-work.
2. **Process** — a ``claude`` / ``python … worker`` / ``python …
   agent`` line in ``ps aux`` has the issue number in its argv. False
   positives (a shell whose scrollback mentions the number) are tolerable;
   false *negatives* just demote to ``blocked``, which is the safe
   direction.
3. **Active spawn directory** — ``/state/worker/sm-dispatcher-spawns/
   spawn-<N>-*`` or ``/state/worker/sm-speaking-spawns/spawn-<N>-*``
   exists outside the ``.finished/`` subdir (the dispatcher moves
   spawns under ``.finished/`` on completion).

The caller builds a single :class:`LivenessScan` per render tick and
hands it to every :func:`is_task_live` call in the same tick: the
scan does one ``ps``, one worktree list, and one directory glob
regardless of how many rows are checked. Scan collection is ~10-30ms
on a busy host; the per-row check is pure string matching and
contributes <1ms per row.

Pure function, no writes. On any I/O error the scan degrades to
empty state (no live signal found for every row — the safe demotion
direction).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from status_site.sources.running_now import repo_dir_from_env

SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


# ──────────────────────────────────────────────────────────────────────────── #
# Worktree + ps helpers — inlined here as part of the PR #2 cleanup. These
# used to live in ``status_site.sources.running_now`` back when that module
# derived rows from ``git worktree list`` + ``ps aux``. PR #2 rewrote
# ``running_now`` to join SM tasks against the subagent event logs; the
# worktree / ps helpers survived because this pipeline-liveness probe still
# cross-checks GitHub-issue rows against live worker signals (a worktree
# named after the issue number, a ``claude`` / ``worker.py`` process
# mentioning it, or a spawn-N-* dir under ``/state/worker/sm-*-spawns``).
# Keeping them local to the one caller means ``running_now`` can shed its
# shell-exec surface entirely.
# ──────────────────────────────────────────────────────────────────────────── #


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str  # branch short name, no refs/heads/ prefix


_WT_PATH_RE = re.compile(r"^worktree (.+)$", re.M)
_WT_BRANCH_RE = re.compile(r"^branch (.+)$", re.M)
_ISSUE_IN_BRANCH_RE = re.compile(r"(?:^|[-/])(\d{2,6})(?:[-/]|$)")


def parse_worktrees(porcelain: str) -> list[Worktree]:
    """Parse ``git worktree list --porcelain`` output.

    Blocks are separated by blank lines. The main repo's block carries
    ``bare`` or no ``branch`` line (detached-at-fetch) — skipped, since
    this section is about active feature worktrees.
    """
    out: list[Worktree] = []
    for block in porcelain.split("\n\n"):
        path_m = _WT_PATH_RE.search(block)
        branch_m = _WT_BRANCH_RE.search(block)
        if not path_m or not branch_m:
            continue
        branch = branch_m.group(1).strip()
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        out.append(Worktree(path=path_m.group(1).strip(), branch=branch))
    return out


def _issue_from_branch(branch: str) -> str:
    """Pull the trailing issue number out of ``feat/638-foo``,
    ``auto-fix/issue-376``, ``fix/620-blah`` and similar. Returns
    ``""`` when no plausible issue number is embedded.
    """
    m = _ISSUE_IN_BRANCH_RE.search(branch)
    return m.group(1) if m else ""


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)


def read_worktree_output(
    repo_dir: Path,
    runner: SubprocessRunner | None = None,
) -> str:
    """Run ``git worktree list --porcelain`` in ``repo_dir``. Empty on failure."""
    r = runner or _default_runner
    try:
        result = r(["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def read_ps_output(runner: SubprocessRunner | None = None) -> str:
    """Run ``ps aux``. Empty string on failure."""
    r = runner or _default_runner
    try:
        result = r(["ps", "aux"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


#: Spawn roots the dispatcher / speaking use for subagent spawns. Each
#: spawn directory is named ``spawn-<issue-number>-<epoch>``; a
#: ``.finished/`` subdir holds completed spawns, which we ignore.
DEFAULT_SPAWN_ROOTS: tuple[Path, ...] = (
    Path("/state/worker/sm-dispatcher-spawns"),
    Path("/state/worker/sm-speaking-spawns"),
)

#: Process lines we consider "a worker is running". Narrow enough to
#: skip random ``bash`` / system daemons while catching the two
#: subagent runtimes Alice uses today (claude-agent-sdk + the
#: in-repo ``*_worker.py`` / ``*_agent.py`` scripts).
_WORKER_LINE_RE = re.compile(
    r"\bclaude\b|python.*\b(?:worker|agent)\b", re.I
)

#: Spawn dir name → issue number. ``spawn-858-1794104709`` → ``858``.
_SPAWN_ISSUE_RE = re.compile(r"^spawn-(\d+)-\d+$")


def _issue_token_present(text: str, issue: str) -> bool:
    """True iff ``issue`` appears in ``text`` bordered by non-digits.

    Prevents ``858`` matching inside ``1858`` / ``8580`` — the issue
    number is a word boundary against other digits, but may be adjacent
    to letters or punctuation (``feat/858-foo``, ``issue-858``).
    """
    if not issue or not text:
        return False
    pat = re.compile(rf"(?<!\d){re.escape(issue)}(?!\d)")
    return bool(pat.search(text))


def _collect_active_spawn_issues(roots: Iterable[Path]) -> frozenset[str]:
    """Scan ``roots/spawn-<N>-*`` (not ``.finished/``) → set of issue numbers.

    A root that doesn't exist, or any OS error on a single root, is
    swallowed and treated as empty — the dispatcher may not have run
    yet on a fresh host. The caller sees an empty set for that root.
    """
    out: set[str] = set()
    for root in roots:
        try:
            entries = list(root.iterdir())
        except (OSError, FileNotFoundError):
            continue
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                # ``.finished/`` and any other dot-dir sit here; skip.
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            m = _SPAWN_ISSUE_RE.match(name)
            if m:
                out.add(m.group(1))
    return frozenset(out)


@dataclass(frozen=True)
class LivenessScan:
    """One-tick snapshot of the live signals used to probe SM rows.

    Built once per render tick and shared across every
    :func:`is_task_live` call in that tick, so the scan's cost stays
    O(1) per render — not O(rows).
    """

    ps_output: str
    worktrees: tuple[Worktree, ...]
    active_spawn_issues: frozenset[str]

    @property
    def is_observable(self) -> bool:
        """True iff this scan can plausibly see worker signals.

        The status-site renderer deploys inside a container that may
        not have ``git worktree list`` output or
        ``/state/worker/sm-*-spawns/`` bind-mounted; in that case
        every row looks not-live purely because the scan can't *see*
        anything. We treat such scans as uninformative and fall back
        to the label-only partition rather than demoting every
        in-flight row to blocked.

        A scan counts as observable when it has at least one worktree
        OR at least one active spawn directory. ``ps_output`` alone
        isn't enough — it's non-empty on every host but reliably
        omits worker processes when the renderer runs in a sandboxed
        PID namespace.
        """
        return bool(self.worktrees) or bool(self.active_spawn_issues)

    @classmethod
    def collect(
        cls,
        *,
        repo_dir: Path | None = None,
        spawn_roots: Iterable[Path] = DEFAULT_SPAWN_ROOTS,
        runner: SubprocessRunner | None = None,
    ) -> "LivenessScan":
        """Build a fresh scan by reading the host's live state.

        ``repo_dir`` defaults to :func:`running_now.repo_dir_from_env`
        (``$GIT_REPO_DIR`` or ``/home/alice/alice``). ``runner`` lets
        tests inject fake ``ps`` / ``git worktree list`` output.
        """
        if repo_dir is None:
            repo_dir = repo_dir_from_env()
        porcelain = read_worktree_output(repo_dir, runner=runner)
        return cls(
            ps_output=read_ps_output(runner=runner),
            worktrees=tuple(parse_worktrees(porcelain)),
            active_spawn_issues=_collect_active_spawn_issues(spawn_roots),
        )

    @classmethod
    def empty(cls) -> "LivenessScan":
        """A scan with no signals — every row probes as not-live.

        Useful in tests and as a safe default when scan collection
        fails.
        """
        return cls(
            ps_output="",
            worktrees=(),
            active_spawn_issues=frozenset(),
        )


def _match_worktree(issue: str, worktrees: Iterable[Worktree]) -> str:
    """Worktree evidence string, or ``""`` when no worktree matches."""
    for wt in worktrees:
        if _issue_from_branch(wt.branch) == issue:
            return f"worktree {wt.path} on {wt.branch}"
        if _issue_token_present(wt.path, issue):
            return f"worktree {wt.path}"
    return ""


def _match_process(issue: str, ps_output: str) -> str:
    """Process evidence string, or ``""`` when no matching line exists."""
    for line in ps_output.splitlines():
        if not _WORKER_LINE_RE.search(line):
            continue
        if _issue_token_present(line, issue):
            short = " ".join(line.split())
            return f"process {short[:80]}"
    return ""


def is_task_live(row: dict, scan: LivenessScan) -> tuple[bool, str]:
    """Is ``row`` backed by a live worker process / worktree / spawn?

    Returns ``(True, evidence)`` on the first matching signal, else
    ``(False, "no live signal found")``. ``evidence`` is a short
    string suitable for a UI reason badge or log line.

    Cheap: all I/O happens once in :meth:`LivenessScan.collect`; this
    function is pure string matching against the cached scan.
    """
    number = row.get("number")
    if not number:
        return False, "no issue number — cannot probe"
    issue = str(number)

    hit = _match_worktree(issue, scan.worktrees)
    if hit:
        return True, hit

    hit = _match_process(issue, scan.ps_output)
    if hit:
        return True, hit

    if issue in scan.active_spawn_issues:
        return True, f"active spawn-{issue}-*"

    return False, "no live signal found"
