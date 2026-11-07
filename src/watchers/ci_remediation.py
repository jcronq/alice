"""CI remediation watcher — spawn a repair-or-retire worker when master CI goes red.

Designed to run as an s6-supervised one-shot on the same cadence as the
other watchers: each invocation does one pass, checks master CI state
on every configured repo, decides whether to spawn a remediation
worker, and exits. Singleton enforcement is handled externally by
``flock`` in the s6 run script.

Policy (per repo, one tick):

  * Call ``gh run list --branch master --limit 1 --json conclusion,status,headSha,databaseId,url``.
  * If ``conclusion == success``: clear any in-flight remediation
    entry for a different SHA (success surface), and drop the
    per-SHA failure counter for the newly-green SHA.
  * If ``conclusion == failure``:
      - If an in-flight entry exists for this same SHA: check
        liveness (PID alive and within the wall-clock cap). If
        dead/expired, mark it failed, increment the per-SHA
        failure counter, clear the entry.
      - If the per-SHA failure counter has reached the escalation
        threshold (2), drop a ``deliver-to-jason`` surface and skip
        spawning.
      - Otherwise, dedup against an open remediation PR by branch
        name (``gh pr list --author <alice> --head
        ci-remediation/<short-sha>-*``). If none, spawn.
      - Enforce the global ``max_concurrent_workers`` cap.
  * In-progress / queued runs are silent — the watcher waits for a
    conclusion before acting.

State lives at ``/state/worker/ci-remediation-state.json``. Surfaces
land in ``inner/surface/`` (``priority: action`` for spawn / success,
``deliver-to-jason: true`` when escalating).

See jcronq/alice#782 for the design discussion.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Defaults + paths
# ---------------------------------------------------------------------------

DEFAULT_MIND = pathlib.Path("/home/alice/alice-mind")
DEFAULT_STATE_DIR = pathlib.Path("/state/worker")
DEFAULT_STATE_FILE = "ci-remediation-state.json"
DEFAULT_WORKER_SCRATCH_ROOT = pathlib.Path("/state/worker/ci-remediation-worker")

# Max consecutive worker failures on the same red SHA before escalating
# to Jason. The third attempt is the signal that the test suite is
# fighting the fix — wake the human rather than burn more tokens.
DEFAULT_ESCALATION_THRESHOLD = 2

# Default wall-clock cap (seconds) for a single remediation worker.
DEFAULT_WALL_CLOCK_CAP = 5400

# Default polling interval (seconds). The s6 supervisor re-reads this
# on every cycle so a config change applies without a restart.
DEFAULT_POLL_INTERVAL_SECONDS = 180

# Default concurrency cap across all configured repos. Two in-flight
# workers is the ceiling for v1: one for the live red commit, one
# in-flight head-of-line from a prior red push.
DEFAULT_MAX_CONCURRENT_WORKERS = 2

# Branch-name prefix the worker uses when opening its PR. The watcher
# uses this prefix in ``gh pr list --head`` dedup lookups.
BRANCH_PREFIX = "ci-remediation"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GHCommandError(RuntimeError):
    """Raised when a ``gh`` invocation exits non-zero."""

    def __init__(self, returncode: int, stderr: str, args: list[str]) -> None:
        super().__init__(f"gh exited {returncode}: {stderr.strip()[:400]}")
        self.returncode = returncode
        self.stderr = stderr
        self.args = args


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WatcherConfig:
    enabled: bool = False
    repos: list[str] = field(default_factory=list)
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    max_concurrent_workers: int = DEFAULT_MAX_CONCURRENT_WORKERS
    wall_clock_cap_seconds: int = DEFAULT_WALL_CLOCK_CAP
    escalation_threshold: int = DEFAULT_ESCALATION_THRESHOLD
    own_login: str = "jcronq"


def load_config(mind_dir: pathlib.Path) -> WatcherConfig:
    """Read the ``ci_remediation`` block from alice.config.json.

    Missing file / missing block ⇒ disabled. New behavior: operators
    opt in explicitly by flipping ``enabled: true`` after they've seen
    the first remediation PR.
    """
    cfg_path = mind_dir / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return WatcherConfig()
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return WatcherConfig()
    block = (cfg or {}).get("ci_remediation") or {}
    repos = [
        r.strip()
        for r in (block.get("repos") or [])
        if isinstance(r, str) and r.strip()
    ]
    # ``own_login`` is reused from the github_watcher block when the
    # ci_remediation block doesn't set it. Keeps the two watchers from
    # drifting on identity.
    own_login_block = block.get("own_login")
    if not isinstance(own_login_block, str) or not own_login_block.strip():
        gh_block = (cfg or {}).get("github_watcher") or {}
        own_login_block = gh_block.get("own_login") or "jcronq"
    return WatcherConfig(
        enabled=bool(block.get("enabled", False)),
        repos=repos,
        poll_interval_seconds=int(
            block.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS
        ),
        max_concurrent_workers=int(
            block.get("max_concurrent_workers") or DEFAULT_MAX_CONCURRENT_WORKERS
        ),
        wall_clock_cap_seconds=int(
            block.get("wall_clock_cap_seconds") or DEFAULT_WALL_CLOCK_CAP
        ),
        escalation_threshold=int(
            block.get("escalation_threshold") or DEFAULT_ESCALATION_THRESHOLD
        ),
        own_login=str(own_login_block).strip() or "jcronq",
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _empty_repo_state() -> dict[str, Any]:
    return {
        # In-flight remediation worker, or None. Shape:
        # {head_sha, spawned_at, pid, scratch_dir, branch,
        #  pr_url (set when we observe it), wall_clock_cap_seconds}
        "current_remediation": None,
        # Consecutive-failure count per red SHA. Cleared when the SHA
        # goes green or another SHA lands on top of it.
        "failure_counts": {},
        # Last SHA we observed master CI landing on, regardless of
        # conclusion. Used for logging / "what changed" diagnostics.
        "last_observed_sha": None,
        # Last SHA we observed a successful conclusion for.
        "last_success_sha": None,
        # Set of SHAs where we've already escalated, so we only
        # notify-Jason once per red SHA no matter how many ticks pass.
        "escalated_shas": [],
    }


def load_state(state_path: pathlib.Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {"version": 1, "repos": {}}
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        print(
            f"[ci-remediation] state at {state_path} is corrupt — resetting",
            file=sys.stderr,
        )
        return {"version": 1, "repos": {}}
    if data.get("version") != 1:
        return {"version": 1, "repos": {}}
    return data


def save_state(state_path: pathlib.Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=state_path.parent, prefix=".ci-remediation-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, state_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# gh CLI shims
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Run ``gh <args>`` and return stdout. Raise GHCommandError on non-zero."""
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


def gh_master_run(repo: str, *, gh_bin: str = "gh") -> dict[str, Any] | None:
    """Return the most recent master CI run as a dict, or ``None`` if there is none.

    The returned dict is the first element of ``gh run list --branch
    master --limit 1`` with the fields this watcher consumes. Each
    field may be absent if the run hasn't produced it yet (a queued
    run has no ``conclusion``), so callers must tolerate missing keys.
    """
    out = _run_gh(
        [
            gh_bin,
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            "master",
            "--limit",
            "1",
            "--json",
            "conclusion,status,headSha,databaseId,url,displayTitle,createdAt",
        ]
    )
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def gh_open_remediation_pr(
    repo: str,
    short_sha: str,
    *,
    own_login: str,
    gh_bin: str = "gh",
) -> str | None:
    """Return the URL of the newest open remediation PR for ``short_sha``.

    The watcher names worker branches ``ci-remediation/<short-sha>-*``
    (the ``*`` is the worker-launch timestamp so a re-dispatched worker
    gets a fresh branch). An open PR on any of those branches, authored
    by Alice, counts as the in-flight remediation.
    """
    head_pattern = f"{BRANCH_PREFIX}/{short_sha}"
    out = _run_gh(
        [
            gh_bin,
            "pr",
            "list",
            "--repo",
            repo,
            "--author",
            own_login,
            "--state",
            "open",
            "--search",
            f"head:{head_pattern}",
            "--json",
            "url,headRefName,number",
            "--limit",
            "5",
        ]
    )
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for pr in data:
        if not isinstance(pr, dict):
            continue
        head = pr.get("headRefName") or ""
        url = pr.get("url")
        if isinstance(head, str) and head.startswith(head_pattern) and isinstance(url, str):
            return url
    return None


def gh_failing_checks(
    repo: str,
    head_sha: str,
    *,
    gh_bin: str = "gh",
) -> list[dict[str, Any]]:
    """Best-effort: list failed/timed-out check-runs on ``head_sha``.

    Used only to flesh out the worker prompt. Returns an empty list on
    any gh error — the worker can still run with the SHA alone.
    """
    try:
        out = _run_gh(
            [
                gh_bin,
                "run",
                "list",
                "--repo",
                repo,
                "--commit",
                head_sha,
                "--status",
                "completed",
                "--json",
                "conclusion,displayTitle,name,url,databaseId",
                "--limit",
                "20",
            ]
        )
    except GHCommandError:
        return []
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    failing: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        if row.get("conclusion") in ("failure", "timed_out", "action_required"):
            failing.append(row)
    return failing


# ---------------------------------------------------------------------------
# Worker spawn
# ---------------------------------------------------------------------------


CI_REMEDIATION_PROMPT_TEMPLATE = """\
You are a CI-remediation worker. Master CI on {repo} is red at commit {head_sha}.
Your job is to either repair or retire the failing tests so master goes green.

Red commit: {head_sha}
Failing checks reported by `gh run list`:
{failing_checks_block}

Steps, in order:

1. Clone / update the repo at the standard checkout path for {repo}.
2. Create an isolated git worktree at `/tmp/ci-remediation-{short_sha}` on a
   fresh branch `{branch}`:

       git worktree add /tmp/ci-remediation-{short_sha} -b {branch} origin/master

   Operate entirely inside that worktree. Do not touch the primary
   checkout — concurrent workers share refs, not working trees.

3. Run `pytest -q` (or the project's canonical test command — see
   `PROJECT_PROFILE.md`) and record the real failure list. Treat the
   `gh run list` block above as hinting, not ground truth.

4. For EACH failing test, make a repair-or-retire decision:
   * REPAIR: the test encodes behavior that still matters; fix the
     code / test so it passes.
   * RETIRE: the test encodes behavior that no longer applies
     (deleted feature, obsolete fixture, racy / flaky test with no
     signal). Delete the test (not the production code) and leave a
     one-line comment in the commit message explaining why.

   When uncertain, REPAIR. RETIRE is a one-way action; prefer a
   conservative fix.

5. Commit with a message of shape
   `ci: repair/retire master-red tests (closes task-0720 if applicable)`.
   Do NOT bypass hooks — no `--no-verify`, no `--no-gpg-sign`. If a
   hook fails, fix the underlying issue and create a new commit.

6. Push the branch to origin.

7. Open a draft pull request with this exact command. Do not modify
   the flags:

       gh pr create --draft \\
         --title "ci: auto-remediate master-red at {short_sha}" \\
         --body "Automated master-CI remediation for commit {head_sha}.

   Triggered by the ci-remediation watcher (jcronq/alice#782).
   Per-test decisions (repaired vs retired) in the commit log.

   Please review before merging.

   Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

   🤖 Generated with [Claude Code](https://claude.com/claude-code)"

8. Return the PR URL on success, or a short error string describing
   what blocked the remediation.

Constraints, non-negotiable:
- The `--draft` flag in step 7 is required. Never pass `--no-draft`.
  Never call `gh pr merge`. A human reviews before merge.
- Do not push to master.
- Do not amend commits; create new ones on hook failure.
- Budget: {wall_clock_cap_minutes} minutes wall-clock. If you can't
  finish, stop and open the draft PR anyway with a WIP note so the
  next attempt has a starting point.
"""


def _render_prompt(
    *,
    repo: str,
    head_sha: str,
    short_sha: str,
    branch: str,
    failing_checks: list[dict[str, Any]],
    wall_clock_cap_seconds: int,
) -> str:
    if failing_checks:
        lines = []
        for row in failing_checks:
            name = row.get("name") or row.get("displayTitle") or "unknown"
            url = row.get("url") or ""
            conclusion = row.get("conclusion") or ""
            lines.append(f"  - {name} ({conclusion}): {url}")
        block = "\n".join(lines)
    else:
        block = "  (none visible via gh; discover them by running the test suite)"
    return CI_REMEDIATION_PROMPT_TEMPLATE.format(
        repo=repo,
        head_sha=head_sha,
        short_sha=short_sha,
        branch=branch,
        failing_checks_block=block,
        wall_clock_cap_minutes=max(1, wall_clock_cap_seconds // 60),
    )


def _spawn_claude_detached(
    scratch_dir: pathlib.Path, prompt: str
) -> int | None:
    """Launch a detached ``claude -p`` worker. Returns its PID or None.

    Mirrors ``alice_bug_fix_dispatcher.main._spawn_claude_detached``:
    ``start_new_session=True`` decouples the child from the watcher's
    s6 process group so an s6 restart doesn't SIGHUP the worker.
    stdout/stderr land in ``scratch_dir`` for post-mortem inspection.
    """
    stdout_path = scratch_dir / "result.json"
    stderr_path = scratch_dir / "stderr.log"
    try:
        stdout_f = open(stdout_path, "wb")
        stderr_f = open(stderr_path, "wb")
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,
            close_fds=True,
            cwd=str(scratch_dir),
        )
    except OSError:
        stdout_f.close()
        stderr_f.close()
        return None
    finally:
        try:
            stdout_f.close()
            stderr_f.close()
        except OSError:
            pass
    return proc.pid


def _pid_alive(pid: int | None) -> bool:
    """True iff ``pid`` refers to a live process. Defensive against
    missing / stale / out-of-range entries in state."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


# ---------------------------------------------------------------------------
# Surface rendering
# ---------------------------------------------------------------------------


def _now_iso_local() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")[:-3]


def _slugify(s: str, max_len: int = 60) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return (s or "surface")[:max_len]


SPAWN_SURFACE_TEMPLATE = """\
---
priority: action
type: ci-remediation-spawn
created: {date}
source: ci-remediation-watcher
repo: {repo}
head_sha: {head_sha}
short_sha: {short_sha}
run_url: {run_url}
worker_pid: {pid}
worker_branch: {branch}
spawned_at: {spawned_at}
wall_clock_cap_seconds: {wall_clock_cap_seconds}
---

# Master CI remediation worker spawned

Master CI on **{repo}** went red at `{short_sha}`. The CI remediation
watcher dispatched a draft-PR worker (pid {pid}, branch `{branch}`).

Failing CI run: {run_url}

Watcher state: `/state/worker/ci-remediation-state.json`.
Worker scratch: `{scratch_dir}`.

No action required right now — the worker will open a draft PR for
review when it's done. If it stalls past the {wall_clock_cap_minutes}-minute
cap, the next watcher tick marks it failed and (eventually) escalates.
"""


SUCCESS_SURFACE_TEMPLATE = """\
---
priority: action
type: ci-remediation-success
created: {date}
source: ci-remediation-watcher
repo: {repo}
prior_red_sha: {prior_red_sha}
new_green_sha: {new_green_sha}
run_url: {run_url}
resolved_at: {resolved_at}
---

# Master CI is green again

Master CI on **{repo}** cleared. Prior red SHA: `{prior_red_sha_short}`.
New green SHA: `{new_green_sha_short}`.

Green run: {run_url}

The in-flight remediation worker (if any) can be stood down; the
watcher has already cleared its state entry.
"""


ESCALATION_SURFACE_TEMPLATE = """\
---
priority: action
type: ci-remediation-escalation
deliver-to-jason: true
created: {date}
source: ci-remediation-watcher
repo: {repo}
head_sha: {head_sha}
short_sha: {short_sha}
run_url: {run_url}
consecutive_failures: {failures}
threshold: {threshold}
---

# CI remediation stalled — needs a human look

Master CI on **{repo}** has been red at `{short_sha}` through
{failures} consecutive remediation attempts (threshold:
{threshold}). The watcher will not spawn another worker for this
SHA until the SHA changes or you intervene.

Failing run: {run_url}

Next steps are yours: either fix it directly, extend the worker
prompt so it covers whatever's blocking the automated pass, or bump
the threshold in alice.config.json and let it try again.
"""


FAILURE_SURFACE_TEMPLATE = """\
---
priority: action
type: ci-remediation-worker-failed
created: {date}
source: ci-remediation-watcher
repo: {repo}
head_sha: {head_sha}
short_sha: {short_sha}
reason: {reason}
spawned_at: {spawned_at}
observed_at: {observed_at}
scratch_dir: {scratch_dir}
consecutive_failures: {failures}
---

# CI remediation worker failed

A remediation worker on **{repo}** for `{short_sha}` ended without
clearing master. Reason: {reason}. Consecutive failures on this SHA:
{failures}.

Worker scratch: `{scratch_dir}`.

The watcher will consider spawning another attempt on the next tick
unless the consecutive-failure counter has reached the escalation
threshold.
"""


def write_surface(
    mind_dir: pathlib.Path,
    *,
    slug: str,
    body: str,
) -> pathlib.Path:
    """Drop a surface file in ``inner/surface/``. Atomic write."""
    surface_dir = mind_dir / "inner" / "surface"
    surface_dir.mkdir(parents=True, exist_ok=True)
    path = surface_dir / f"{_stamp_utc()}-{slug}.md"
    tmp = surface_dir / f".{path.name}.tmp"
    tmp.write_text(body)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Per-repo pass
# ---------------------------------------------------------------------------


@dataclass
class PollResult:
    """Return value of ``poll_repo`` — the watcher's view of this tick."""

    repo: str
    conclusion: str | None
    head_sha: str | None
    run_url: str | None
    spawned: bool = False
    worker_pid: int | None = None
    worker_branch: str | None = None
    scratch_dir: pathlib.Path | None = None
    cleared_success: bool = False
    prior_red_sha: str | None = None
    escalated: bool = False
    worker_failed_reason: str | None = None


def _short(sha: str | None) -> str:
    if not isinstance(sha, str):
        return "?"
    return sha[:7] or "?"


def poll_repo(
    repo: str,
    repo_state: dict[str, Any],
    *,
    cfg: WatcherConfig,
    now: dt.datetime,
    scratch_root: pathlib.Path,
    global_inflight: int,
    master_run_fn: Callable[[str], dict[str, Any] | None],
    open_pr_fn: Callable[[str, str], str | None],
    failing_checks_fn: Callable[[str, str], list[dict[str, Any]]],
    spawn_fn: Callable[[pathlib.Path, str], int | None],
    log: Callable[[str], None],
) -> PollResult:
    """One pass over ``repo``. Mutates ``repo_state`` in place."""
    try:
        run = master_run_fn(repo)
    except GHCommandError as exc:
        log(f"[ci-remediation] {repo}: gh run list failed — {exc}")
        return PollResult(repo=repo, conclusion=None, head_sha=None, run_url=None)

    if run is None:
        log(f"[ci-remediation] {repo}: no master run visible via gh")
        return PollResult(repo=repo, conclusion=None, head_sha=None, run_url=None)

    conclusion = run.get("conclusion") if isinstance(run.get("conclusion"), str) else None
    status = run.get("status") if isinstance(run.get("status"), str) else None
    head_sha = run.get("headSha") if isinstance(run.get("headSha"), str) else None
    run_url = run.get("url") if isinstance(run.get("url"), str) else None
    repo_state["last_observed_sha"] = head_sha

    result = PollResult(
        repo=repo,
        conclusion=conclusion,
        head_sha=head_sha,
        run_url=run_url,
    )

    # If the latest run hasn't concluded yet (queued / in_progress),
    # no action — we wait for GitHub to decide before dispatching.
    if conclusion not in ("failure", "success", "timed_out", "action_required"):
        log(
            f"[ci-remediation] {repo}: latest master run is {status or conclusion} "
            f"({_short(head_sha)}); waiting"
        )
        return result

    # ---- Green path -------------------------------------------------------
    if conclusion == "success":
        inflight = repo_state.get("current_remediation")
        prior_red_sha = inflight.get("head_sha") if isinstance(inflight, dict) else None
        if inflight is not None:
            log(
                f"[ci-remediation] {repo}: master green at "
                f"{_short(head_sha)} — clearing in-flight worker for "
                f"{_short(prior_red_sha)}"
            )
            repo_state["current_remediation"] = None
            result.cleared_success = True
            result.prior_red_sha = prior_red_sha
        # Clear per-SHA failure counts for the SHA that just went green.
        if isinstance(head_sha, str):
            repo_state.get("failure_counts", {}).pop(head_sha, None)
            # If the newly-green SHA had been escalated, let future
            # reds re-trigger escalation flow cleanly.
            escalated = repo_state.get("escalated_shas") or []
            if head_sha in escalated:
                repo_state["escalated_shas"] = [s for s in escalated if s != head_sha]
        repo_state["last_success_sha"] = head_sha
        return result

    # ---- Red path --------------------------------------------------------
    # Any non-success conclusion is treated as "red" for remediation
    # purposes.
    if not isinstance(head_sha, str) or not head_sha:
        log(f"[ci-remediation] {repo}: red run reported no headSha — skipping")
        return result

    inflight = repo_state.get("current_remediation")
    if isinstance(inflight, dict):
        spawned_at = inflight.get("spawned_at")
        pid = inflight.get("pid")
        try:
            spawned_dt = (
                dt.datetime.fromisoformat(spawned_at)
                if isinstance(spawned_at, str)
                else None
            )
        except ValueError:
            spawned_dt = None
        age = (now - spawned_dt).total_seconds() if spawned_dt else None
        cap = int(inflight.get("wall_clock_cap_seconds") or cfg.wall_clock_cap_seconds)

        same_sha = inflight.get("head_sha") == head_sha

        if same_sha and _pid_alive(pid) and (age is None or age < cap):
            log(
                f"[ci-remediation] {repo}: worker pid {pid} still alive on "
                f"{_short(head_sha)} (age {int(age) if age is not None else '?'}s); "
                "no action"
            )
            return result

        # The worker is either dead, expired, or working on a now-stale
        # SHA. Mark it failed.
        if age is not None and age >= cap:
            reason = "wall-clock cap exceeded"
        elif not _pid_alive(pid):
            reason = "worker process exited without clearing master"
        else:
            reason = "master advanced to a new red SHA mid-flight"
        failure_sha = inflight.get("head_sha") or head_sha
        counts = repo_state.setdefault("failure_counts", {})
        counts[failure_sha] = int(counts.get(failure_sha, 0)) + 1
        result.worker_failed_reason = reason
        log(
            f"[ci-remediation] {repo}: worker for {_short(failure_sha)} marked "
            f"failed — {reason}; failures now {counts[failure_sha]}"
        )
        # Clear the in-flight record so we can either spawn again (if
        # under threshold) or escalate.
        repo_state["current_remediation"] = None
        inflight = None

    # Escalation check.
    counts = repo_state.setdefault("failure_counts", {})
    escalated = set(repo_state.get("escalated_shas") or [])
    if counts.get(head_sha, 0) >= cfg.escalation_threshold:
        if head_sha not in escalated:
            escalated.add(head_sha)
            repo_state["escalated_shas"] = sorted(escalated)
            result.escalated = True
            log(
                f"[ci-remediation] {repo}: escalating {_short(head_sha)} — "
                f"{counts[head_sha]} consecutive failures at or above threshold "
                f"{cfg.escalation_threshold}"
            )
        else:
            log(
                f"[ci-remediation] {repo}: already escalated {_short(head_sha)}; "
                "skipping spawn"
            )
        return result

    # Dedup: is there already an open PR on a worker branch for this SHA?
    try:
        existing_pr = open_pr_fn(repo, _short(head_sha))
    except GHCommandError as exc:
        log(f"[ci-remediation] {repo}: open-PR dedup check failed — {exc}")
        existing_pr = None
    if existing_pr:
        log(
            f"[ci-remediation] {repo}: open remediation PR already exists "
            f"({existing_pr}) — not spawning"
        )
        return result

    # Concurrency cap.
    if global_inflight >= cfg.max_concurrent_workers:
        log(
            f"[ci-remediation] {repo}: max_concurrent_workers "
            f"({cfg.max_concurrent_workers}) reached — not spawning"
        )
        return result

    # ---- Spawn -----------------------------------------------------------
    short_sha = _short(head_sha)
    branch = f"{BRANCH_PREFIX}/{short_sha}-{int(now.timestamp())}"
    scratch_dir = scratch_root / f"{repo.replace('/', '-')}-{short_sha}-{int(now.timestamp())}"
    try:
        scratch_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"[ci-remediation] {repo}: cannot create scratch dir {scratch_dir}: {exc}")
        return result

    failing = failing_checks_fn(repo, head_sha)
    prompt = _render_prompt(
        repo=repo,
        head_sha=head_sha,
        short_sha=short_sha,
        branch=branch,
        failing_checks=failing,
        wall_clock_cap_seconds=cfg.wall_clock_cap_seconds,
    )
    try:
        (scratch_dir / "prompt.md").write_text(prompt)
    except OSError as exc:
        log(f"[ci-remediation] {repo}: cannot write prompt: {exc}")
        return result

    pid = spawn_fn(scratch_dir, prompt)
    if pid is None:
        log(f"[ci-remediation] {repo}: worker spawn failed")
        counts[head_sha] = int(counts.get(head_sha, 0)) + 1
        result.worker_failed_reason = "spawn failed"
        return result

    repo_state["current_remediation"] = {
        "head_sha": head_sha,
        "spawned_at": now.isoformat(timespec="seconds"),
        "pid": pid,
        "scratch_dir": str(scratch_dir),
        "branch": branch,
        "pr_url": None,
        "wall_clock_cap_seconds": cfg.wall_clock_cap_seconds,
        "run_url": run_url,
    }
    result.spawned = True
    result.worker_pid = pid
    result.worker_branch = branch
    result.scratch_dir = scratch_dir
    log(
        f"[ci-remediation] {repo}: spawned remediation worker pid {pid} "
        f"for {short_sha} on branch {branch}"
    )
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(
    *,
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path = DEFAULT_WORKER_SCRATCH_ROOT,
    master_run_fn: Callable[[str], dict[str, Any] | None] | None = None,
    open_pr_fn: Callable[[str, str], str | None] | None = None,
    failing_checks_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
    spawn_fn: Callable[[pathlib.Path, str], int | None] | None = None,
    now: dt.datetime | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> int:
    """One watcher pass. Exit code is the desired process exit."""
    cfg = load_config(mind_dir)
    if not cfg.enabled or not cfg.repos:
        log("[ci-remediation] disabled or no repos configured — exiting clean")
        return 0

    now = now or dt.datetime.now().astimezone()
    state = load_state(state_path)
    repos_state = state.setdefault("repos", {})

    master_run_fn = master_run_fn or gh_master_run
    open_pr_fn = open_pr_fn or (
        lambda repo, short_sha: gh_open_remediation_pr(
            repo, short_sha, own_login=cfg.own_login
        )
    )
    failing_checks_fn = failing_checks_fn or gh_failing_checks
    spawn_fn = spawn_fn or _spawn_claude_detached

    # Count global in-flight workers for the concurrency cap. A worker
    # counts as in-flight if its state entry is present AND the pid is
    # still alive AND the wall-clock cap hasn't expired. Stale entries
    # are counted as zero so a crashed worker doesn't permanently block
    # the next dispatch.
    def _count_global_inflight() -> int:
        n = 0
        for repo_state in repos_state.values():
            inflight = repo_state.get("current_remediation")
            if not isinstance(inflight, dict):
                continue
            pid = inflight.get("pid")
            if not _pid_alive(pid):
                continue
            spawned_at = inflight.get("spawned_at")
            try:
                spawned_dt = (
                    dt.datetime.fromisoformat(spawned_at)
                    if isinstance(spawned_at, str)
                    else None
                )
            except ValueError:
                spawned_dt = None
            cap = int(
                inflight.get("wall_clock_cap_seconds") or cfg.wall_clock_cap_seconds
            )
            if spawned_dt and (now - spawned_dt).total_seconds() >= cap:
                continue
            n += 1
        return n

    results: list[PollResult] = []
    for repo in cfg.repos:
        repo_state = repos_state.setdefault(repo, _empty_repo_state())
        # Backfill newly-added fields on legacy state.
        for key, default in _empty_repo_state().items():
            repo_state.setdefault(key, default)
        try:
            result = poll_repo(
                repo,
                repo_state,
                cfg=cfg,
                now=now,
                scratch_root=scratch_root,
                global_inflight=_count_global_inflight(),
                master_run_fn=master_run_fn,
                open_pr_fn=open_pr_fn,
                failing_checks_fn=failing_checks_fn,
                spawn_fn=spawn_fn,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[ci-remediation] {repo}: unexpected error — {type(exc).__name__}: {exc}")
            continue
        results.append(result)

    save_state(state_path, state)

    # Surface emission is after save_state so a file-write hiccup on
    # the surface side doesn't lose state progression.
    date = now.strftime("%Y-%m-%d")
    for result in results:
        if result.cleared_success:
            body = SUCCESS_SURFACE_TEMPLATE.format(
                date=date,
                repo=result.repo,
                prior_red_sha=result.prior_red_sha or "unknown",
                prior_red_sha_short=_short(result.prior_red_sha),
                new_green_sha=result.head_sha or "unknown",
                new_green_sha_short=_short(result.head_sha),
                run_url=result.run_url or "",
                resolved_at=now.isoformat(timespec="seconds"),
            )
            slug = _slugify(
                f"ci-remediation-success-{result.repo}-{_short(result.head_sha)}"
            )
            path = write_surface(mind_dir, slug=slug, body=body)
            log(f"[ci-remediation] {result.repo}: wrote success surface {path.name}")

        if result.spawned:
            body = SPAWN_SURFACE_TEMPLATE.format(
                date=date,
                repo=result.repo,
                head_sha=result.head_sha or "",
                short_sha=_short(result.head_sha),
                run_url=result.run_url or "",
                pid=result.worker_pid or 0,
                branch=result.worker_branch or "",
                spawned_at=now.isoformat(timespec="seconds"),
                wall_clock_cap_seconds=cfg.wall_clock_cap_seconds,
                wall_clock_cap_minutes=max(1, cfg.wall_clock_cap_seconds // 60),
                scratch_dir=str(result.scratch_dir) if result.scratch_dir else "",
            )
            slug = _slugify(
                f"ci-remediation-spawn-{result.repo}-{_short(result.head_sha)}"
            )
            path = write_surface(mind_dir, slug=slug, body=body)
            log(f"[ci-remediation] {result.repo}: wrote spawn surface {path.name}")

        if result.worker_failed_reason and not result.escalated:
            head_sha = result.head_sha or "unknown"
            counts = (repos_state.get(result.repo) or {}).get("failure_counts", {})
            body = FAILURE_SURFACE_TEMPLATE.format(
                date=date,
                repo=result.repo,
                head_sha=head_sha,
                short_sha=_short(result.head_sha),
                reason=result.worker_failed_reason,
                spawned_at="",  # not threaded back; look at prior spawn surface
                observed_at=now.isoformat(timespec="seconds"),
                scratch_dir="",
                failures=int(counts.get(head_sha, 0)),
            )
            slug = _slugify(
                f"ci-remediation-worker-failed-{result.repo}-{_short(result.head_sha)}"
            )
            path = write_surface(mind_dir, slug=slug, body=body)
            log(f"[ci-remediation] {result.repo}: wrote failure surface {path.name}")

        if result.escalated:
            head_sha = result.head_sha or "unknown"
            counts = (repos_state.get(result.repo) or {}).get("failure_counts", {})
            body = ESCALATION_SURFACE_TEMPLATE.format(
                date=date,
                repo=result.repo,
                head_sha=head_sha,
                short_sha=_short(result.head_sha),
                run_url=result.run_url or "",
                failures=int(counts.get(head_sha, 0)),
                threshold=cfg.escalation_threshold,
            )
            slug = _slugify(
                f"ci-remediation-escalation-{result.repo}-{_short(result.head_sha)}"
            )
            path = write_surface(mind_dir, slug=slug, body=body)
            log(
                f"[ci-remediation] {result.repo}: wrote escalation surface "
                f"{path.name} (deliver-to-jason)"
            )

    log(f"[ci-remediation] done — {len(results)} repo(s) polled")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the CI remediation watcher."
    )
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_DIR / DEFAULT_STATE_FILE),
        help="path to ci-remediation-state.json",
    )
    parser.add_argument(
        "--scratch-root",
        default=str(DEFAULT_WORKER_SCRATCH_ROOT),
        help="root dir for per-worker scratch subdirs",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    return run(
        mind_dir=pathlib.Path(args.mind),
        state_path=pathlib.Path(args.state),
        scratch_root=pathlib.Path(args.scratch_root),
    )


if __name__ == "__main__":
    sys.exit(main())
