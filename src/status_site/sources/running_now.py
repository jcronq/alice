"""Running now — live render of the "what the agent is building right
now" section.

Inputs (all injectable so tests don't call live tools):

* ``git worktree list --porcelain`` output — one block per worktree,
  with ``worktree <path>`` and ``branch <ref>`` lines. Dispatchers
  spawn worktrees to isolate concurrent builds; a worktree whose branch
  starts with ``refs/heads/feat/`` or ``refs/heads/auto-fix/`` is the
  signal that an agent is actively on it.
* ``ps aux`` output — grepped for ``claude`` to tell whether the
  worktree's build process is still alive. Matched by cwd (the tail of
  the command line usually names the worktree directory).
* Build-log tails under ``/tmp/alice-build-*``. The log convention is
  that the dispatcher appends ``PR_URL: <url>`` on success; absent
  that sentinel the section renders ``no PR yet``.
* ``inner/tasks/*.md`` — the SM task mirror the speaking daemon
  maintains. Each file's frontmatter has ``status:`` (building / done /
  blocked) and ``issue:``; matched to a worktree by branch suffix
  (``issue-<N>`` or the issue number trailing the branch).

The renderer pairs worktrees with processes + build logs + task
records and prints one line per active task, newest first.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from status_site.sources.common import staleness_footer

SOURCE_NAME = "worktree-ps-build"

# ``git worktree list --porcelain`` block parser. Each block is a
# ``worktree <path>\nHEAD <sha>\nbranch <ref>\n`` sequence; the main
# repo block lacks ``branch`` (prunable; we skip those).
_WT_PATH_RE = re.compile(r"^worktree (.+)$", re.M)
_WT_BRANCH_RE = re.compile(r"^branch (.+)$", re.M)

# ``ps aux`` → cwd heuristic: the claude process's command line usually
# ends with the worktree path (that's where dispatcher cd's before
# invoking claude). This cheap substring match sidesteps needing
# ``readlink /proc/<pid>/cwd``.
_CLAUDE_LINE_RE = re.compile(r"\bclaude\b", re.I)


SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=10
    )


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str  # branch short name, no refs/heads/ prefix


@dataclass(frozen=True)
class RunningTask:
    """One row of the rendered section."""

    branch: str
    worktree: str
    alive: bool
    pr_url: str  # empty string when no PR yet
    elapsed: str  # human-readable age ("3m", "1h 20m")
    task_status: str  # from inner/tasks/*.md frontmatter; "" if no match
    issue: str  # issue number from task file or branch; "" if unknown


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
        # Normalise refs/heads/foo → foo so comparisons stay simple.
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        out.append(Worktree(path=path_m.group(1).strip(), branch=branch))
    return out


def worktree_is_alive(worktree_path: str, ps_output: str) -> bool:
    """Heuristic: is there a ``claude`` process whose command line
    mentions the worktree path?

    The dispatcher invokes claude with ``cd <worktree> && claude ...``;
    on ``ps aux`` the worktree path appears either in the argv (when
    the invocation is a one-liner) or in the cwd column. We match on
    substring in the raw ``ps`` output. False positives are tolerable
    here — the next scan will clear them when the process actually
    exits.
    """
    for line in ps_output.splitlines():
        if not _CLAUDE_LINE_RE.search(line):
            continue
        if worktree_path in line:
            return True
    return False


_PR_URL_SENTINEL = re.compile(r"^PR_URL:\s*(\S+)\s*$", re.M)


def tail_pr_url(log_text: str) -> str:
    """Scan a build-log tail for a ``PR_URL: <url>`` sentinel.

    Last sentinel wins (so a retry that opens a new PR takes
    precedence). Returns ``""`` when none is present — the renderer
    treats that as "no PR yet".
    """
    matches = _PR_URL_SENTINEL.findall(log_text)
    return matches[-1].strip() if matches else ""


def _elapsed_str(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, rem_m = divmod(m, 60)
    return f"{h}h {rem_m}m" if rem_m else f"{h}h"


def _parse_task_file(text: str) -> dict:
    """Minimal frontmatter reader — just ``status`` and ``issue``.

    Any parse error returns ``{}`` so a malformed task file can't
    poison the whole section.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def collect_task_records(tasks_dir: Path) -> dict[str, dict]:
    """Return ``{issue_number: {status, issue, mtime, path}}`` for every
    SM task file. Keyed by issue so the worktree-join can lookup by
    the branch's trailing issue number.
    """
    out: dict[str, dict] = {}
    if not tasks_dir.is_dir():
        return out
    for p in sorted(tasks_dir.glob("*.md")):
        try:
            text = p.read_text(errors="replace")
            st = p.stat()
        except OSError:
            continue
        fm = _parse_task_file(text)
        issue = (fm.get("issue") or "").strip()
        if not issue:
            # Fall back to a number in the filename, e.g. issue-638.md.
            m = re.search(r"(\d+)", p.stem)
            if m:
                issue = m.group(1)
        if not issue:
            continue
        out[issue] = {
            "status": (fm.get("status") or "").strip(),
            "issue": issue,
            "mtime": st.st_mtime,
            "path": p,
        }
    return out


_ISSUE_IN_BRANCH_RE = re.compile(r"(?:^|[-/])(\d{2,6})(?:[-/]|$)")


def _issue_from_branch(branch: str) -> str:
    """Pull the trailing issue number out of ``feat/638-foo``,
    ``auto-fix/issue-376``, ``fix/620-blah`` and similar. Returns
    ``""`` when no plausible issue number is embedded.
    """
    m = _ISSUE_IN_BRANCH_RE.search(branch)
    return m.group(1) if m else ""


def build_running_tasks(
    worktrees: Iterable[Worktree],
    ps_output: str,
    log_entries: Iterable[tuple[str, float]],  # (log_text, start_epoch)
    task_records: dict[str, dict],
    *,
    now: float | None = None,
) -> list[RunningTask]:
    """Assemble ``RunningTask`` rows by joining worktrees with ps,
    build-logs, and task records.

    ``log_entries`` is a list of ``(contents, start_epoch)`` tuples,
    one per build-log file. The join to a worktree is loose: a log is
    attributed to a worktree when its contents mentions the worktree
    path. If no log mentions the worktree, ``pr_url`` is empty and the
    elapsed time comes from the worktree's checkout mtime — not fatal.
    """
    if now is None:
        now = time.time()
    out: list[RunningTask] = []
    for wt in worktrees:
        alive = worktree_is_alive(wt.path, ps_output)
        pr_url = ""
        start = now
        for text, started in log_entries:
            if wt.path in text:
                url = tail_pr_url(text)
                if url:
                    pr_url = url
                start = min(start, started)
        issue = _issue_from_branch(wt.branch)
        task = task_records.get(issue, {}) if issue else {}
        out.append(
            RunningTask(
                branch=wt.branch,
                worktree=wt.path,
                alive=alive,
                pr_url=pr_url,
                elapsed=_elapsed_str(max(0.0, now - start)),
                task_status=str(task.get("status") or ""),
                issue=issue,
            )
        )
    return out


def render(
    tasks: Sequence[RunningTask],
    *,
    now: datetime | None = None,
) -> str:
    """Render the Running now region body (no heading, no markers).

    One bullet per task, newest-first (callers pass sorted). Trailing
    staleness footer is always present; the one line uses the shape
    from the design doc::

        - <branch>: <alive|dead>, <PR_URL|no PR yet>, elapsed <t>
    """
    if not tasks:
        body = "*(nothing building right now)*"
    else:
        lines: list[str] = []
        for t in tasks:
            # Dashboard-style pill: ``alive`` is ``active`` (warn),
            # ``dead`` is ``blocked`` (bad). Classes are vendored from
            # ``widgets.css``; GFM passes the raw span through.
            if t.alive:
                pill = '<span class="fx-pill active">alive</span>'
            else:
                pill = '<span class="fx-pill blocked">dead</span>'
            pr = t.pr_url or "no PR yet"
            lines.append(
                f"- `{t.branch}`: {pill}, {pr}, elapsed {t.elapsed}"
            )
        body = "\n".join(lines)
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)


# ─── Live-source helpers (called by the server's background poller) ─


def read_worktree_output(
    repo_dir: Path,
    runner: SubprocessRunner | None = None,
) -> str:
    """Run ``git worktree list --porcelain`` in ``repo_dir``. Returns
    empty string on failure so the renderer renders ``(nothing)``.
    """
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


def read_build_logs(
    glob_root: Path = Path("/tmp"),
    pattern: str = "alice-build-*/build-*.log",
) -> list[tuple[str, float]]:
    """Read every ``/tmp/alice-build-*/build-*.log`` tail (last 4 KB).

    Returns ``[(contents, start_epoch), ...]`` — ``start_epoch`` comes
    from the log file's mtime, which is close enough to "when did this
    build start" for the elapsed-time column.
    """
    out: list[tuple[str, float]] = []
    try:
        for p in glob_root.glob(pattern):
            if not p.is_file():
                continue
            try:
                st = p.stat()
                with open(p, "rb") as f:
                    f.seek(max(0, st.st_size - 4096))
                    tail = f.read().decode(errors="replace")
            except OSError:
                continue
            # Oldest mtime in the chain approximates the build start.
            # On a long-running build the file is still being appended,
            # so st_ctime (inode creation) would be more accurate on
            # Linux — but ``PR_URL:`` marks completion, and that's the
            # only piece we semantically need from the log.
            start = getattr(st, "st_ctime", st.st_mtime)
            out.append((tail, start))
    except OSError:
        return out
    return out


def repo_dir_from_env(env: dict[str, str] | None = None) -> Path:
    """``$GIT_REPO_DIR``, defaulting to ``/home/alice/alice`` per the
    design. Lets the running_now source be pointed at another checkout
    in tests or a different host layout.
    """
    e = dict(os.environ if env is None else env)
    return Path(e.get("GIT_REPO_DIR", "/home/alice/alice"))
