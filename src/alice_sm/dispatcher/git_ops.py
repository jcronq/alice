"""Git operations the dispatcher invokes against the worker repo.

:func:`_run_git` is the test-injectable subprocess wrapper. The two
public helpers (one private-prefixed, but both used outside this module)
are :func:`_post_merge_cleanup` and :func:`_attempt_auto_rebase`:

* ``_post_merge_cleanup`` (issue #127) restores the shared worker tree
  to ``master`` after a PR merges so the next worker reads master, not
  the departing feature branch.

* ``_attempt_auto_rebase`` (issue #173 Tier 1) tries an in-process
  rebase + force-push when a CONFLICTING PR shows up at
  ``sm:reviewing``. Failure here triggers the Tier 2 spawn (resolved
  in the spawn module) or Tier 3 escalation (audited via rendering).
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from typing import Any, Callable

from alice_sm.dispatcher.constants import BASE_BRANCH
from alice_sm.dispatcher.types import GitRunFn


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
