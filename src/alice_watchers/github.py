"""GitHub repo watcher — emits inner/notes/ entries for PR activity.

One pass per invocation. Reads ``github_watcher`` from
``alice-mind/config/alice.config.json``, polls each watched repo via the
``gh`` CLI, diffs against persisted "seen" markers under
``/state/worker/gh-watcher-state.json``, and writes one markdown note per
unseen event into ``inner/notes/`` tagged ``github`` (or
``github-watcher-error`` when auth blows up).

Why poll, not webhooks: latency budget is generous (~15 min worst case,
per the spec issue) and polling needs no public ingress, no webhook
secret, and no signature verification path. ``gh`` is already in the
worker image and ``GH_TOKEN`` is already wired through ``alice.env``.

Events covered (priority order from spec issue #4):
  * Reviews (approved / changes_requested / dismissed) on watched PRs
  * Review comments (inline) on watched PRs
  * Issue comments on PRs (PR conversation comments)
  * New PRs opened on a watched repo
  * PR state transitions (open → merged / closed)
  * Check-run failures on PR head commits

Out of scope for first pass (per the issue): standalone-issue comments,
acting on events, multi-page pagination beyond ~50 PRs/repo, draft skip,
diff payload (notes carry URL + metadata only — owner can fetch on demand).
"""

from __future__ import annotations

import argparse
import datetime as dt
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
DEFAULT_STATE_FILE = "gh-watcher-state.json"

# Cap each per-repo "seen IDs" list to keep state bounded. Old comment IDs
# never reappear, so dropping the oldest 1000 first is safe. The cap is a
# soft ceiling — sized for "active project, ~50 PRs/year of comments" — not
# a security boundary.
SEEN_ID_CAP = 1000

# Pull recent PRs per poll. Sorted by updated_at desc, this picks up
# everything that changed since the last poll for any reasonably-sized
# project. Pagination is explicitly out of scope for the first cut.
RECENT_PR_LIMIT = 50

# Re-fire the auth-error note at most once per this many seconds. Without
# dedup the supervisor (running every 5 min) would spam thinking with the
# same loud message indefinitely.
AUTH_ERROR_NOTE_INTERVAL_SECONDS = 6 * 3600


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GHCommandError(RuntimeError):
    """Raised when ``gh api`` exits non-zero. Carries stderr for the note."""

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


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------


# Trust ladder for author_association. Stamped on every issue / PR /
# review / comment by GitHub. We trust people the repo or org has
# *deliberately* admitted (OWNER / COLLABORATOR / MEMBER). CONTRIBUTOR
# (= had a previous PR merged) is intentionally NOT trusted by default
# — a merged patch isn't the same as a "this person is on the project"
# decision. Override via ``github_watcher.trusted_associations``.
DEFAULT_TRUSTED_ASSOCIATIONS = ("OWNER", "COLLABORATOR", "MEMBER")


@dataclass
class WatcherConfig:
    enabled: bool = False
    repos: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 300  # informational; cadence is enforced by s6
    trusted_associations: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_TRUSTED_ASSOCIATIONS)
    )


def load_config(mind_dir: pathlib.Path) -> WatcherConfig:
    """Read ``github_watcher`` block from alice.config.json.

    Missing file or missing block ⇒ disabled (empty repo list). The
    runtime ships disabled-by-default; the user opts in by adding repos
    in their mind's config.
    """
    cfg_path = mind_dir / "config" / "alice.config.json"
    if not cfg_path.is_file():
        return WatcherConfig()
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return WatcherConfig()
    block = (cfg or {}).get("github_watcher") or {}
    repos = [r.strip() for r in (block.get("repos") or []) if isinstance(r, str) and r.strip()]
    enabled = bool(block.get("enabled", bool(repos)))
    interval = int(block.get("poll_interval_seconds") or 300)
    raw_trust = block.get("trusted_associations")
    if isinstance(raw_trust, list) and raw_trust:
        trust = frozenset(
            str(s).strip().upper() for s in raw_trust if str(s).strip()
        )
    else:
        trust = frozenset(DEFAULT_TRUSTED_ASSOCIATIONS)
    return WatcherConfig(
        enabled=enabled,
        repos=repos,
        poll_interval_seconds=interval,
        trusted_associations=trust,
    )


def is_trusted_association(association: str | None, trusted: frozenset[str]) -> bool:
    """Author is trusted iff their author_association is in the trust set.

    Missing / unknown association ⇒ untrusted. Empty trust set means
    "trust nothing," not "trust everything" — the operator has to opt
    out explicitly by listing every association they want.
    """
    if not association:
        return False
    return association.strip().upper() in trusted


def load_state(state_path: pathlib.Path) -> dict[str, Any]:
    """Load watcher state. Returns an empty skeleton on first run."""
    if not state_path.is_file():
        return {"version": 1, "repos": {}}
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        # Corrupt state file: log via stderr and start fresh. Re-firing
        # historical events once is acceptable; staying broken isn't.
        print(f"[gh-watcher] state at {state_path} is corrupt — resetting", file=sys.stderr)
        return {"version": 1, "repos": {}}
    if data.get("version") != 1:
        return {"version": 1, "repos": {}}
    return data


def save_state(state_path: pathlib.Path, state: dict[str, Any]) -> None:
    """Atomically replace the state file. Cap "seen ID" lists per repo."""
    for repo_state in (state.get("repos") or {}).values():
        for key in (
            "seen_review_ids",
            "seen_review_comment_ids",
            "seen_issue_comment_ids",
            "seen_check_run_ids",
            "seen_standalone_issue_comment_ids",
        ):
            ids = repo_state.get(key) or []
            if len(ids) > SEEN_ID_CAP:
                repo_state[key] = ids[-SEEN_ID_CAP:]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_path.parent, prefix=".gh-watcher-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, state_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _empty_repo_state() -> dict[str, Any]:
    return {
        "seen_review_ids": [],
        "seen_review_comment_ids": [],
        "seen_issue_comment_ids": [],
        "seen_standalone_issue_comment_ids": [],
        "seen_check_run_ids": [],
        "pr_state": {},
        "issue_state": {},
        # ``first_run`` primes the PR-side seen-ID sets on the very first
        # poll; ``issues_primed`` is the same idea for the issue-side, but
        # versioned independently so an existing-state file (written before
        # issue support shipped) gets primed silently on the first poll
        # after the upgrade rather than dumping every historical issue.
        "first_run": True,
        "issues_primed": False,
    }


# ---------------------------------------------------------------------------
# gh CLI shim
# ---------------------------------------------------------------------------


def gh_api(path: str, *, gh_bin: str = "gh") -> Any:
    """Call ``gh api <path>`` and return parsed JSON.

    Trivial wrapper so tests can inject a fake. Path is everything after
    ``api`` — e.g. ``"repos/owner/repo/pulls?state=open&sort=updated"``.
    """
    args = [gh_bin, "api", path]
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GHCommandError(returncode=-1, stderr=str(exc), args=args) from exc
    if result.returncode != 0:
        raise GHCommandError(
            returncode=result.returncode,
            stderr=result.stderr or result.stdout,
            args=args,
        )
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass
class Event:
    # Kinds:
    #   new_pr, pr_state, review, review_comment, issue_comment, check_failure
    #     — PR-scoped events. ``number`` is the PR number.
    #   new_issue, issue_state, standalone_issue_comment
    #     — issue-scoped events. ``number`` is the issue number.
    kind: str
    repo: str
    number: int
    title: str
    url: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Per-repo poll
# ---------------------------------------------------------------------------


def poll_repo(
    repo: str,
    repo_state: dict[str, Any],
    *,
    trusted: frozenset[str] = frozenset(DEFAULT_TRUSTED_ASSOCIATIONS),
    api: Callable[[str], Any] = gh_api,
) -> list[Event]:
    """Poll one repo, mutate ``repo_state`` in place, return new events.

    Trust gating (via ``author_association`` from the GitHub API):
      * PR reviews, inline review comments, check failures — always fire,
        regardless of ``author_association``. Reviews are inherently
        constructive and CI is impersonal.
      * PR conversation comments, new issues, issue state changes, and
        standalone issue comments — gated. Only authors whose association
        is in ``trusted`` produce notes. We still mark the IDs as seen so
        the rando's drive-by comment doesn't get re-evaluated forever.

    The "first run" pass primes seen-ID sets without emitting any events,
    so adding a repo to the config doesn't dump weeks of historical
    activity into ``inner/notes/`` on the first poll.
    """
    first_run = bool(repo_state.get("first_run"))
    # ``issues_primed`` is the issue-side equivalent of ``first_run``,
    # versioned independently so a state file written before issue
    # support shipped (where the key is missing entirely) primes silently
    # on the first poll post-upgrade rather than emitting a note per
    # existing trusted-author issue. Default False ⇒ "haven't primed yet."
    issues_primed = bool(repo_state.get("issues_primed", False))
    events: list[Event] = []

    seen_review_ids: set[int] = set(repo_state.get("seen_review_ids") or [])
    seen_review_comment_ids: set[int] = set(repo_state.get("seen_review_comment_ids") or [])
    seen_issue_comment_ids: set[int] = set(repo_state.get("seen_issue_comment_ids") or [])
    seen_standalone_issue_comment_ids: set[int] = set(
        repo_state.get("seen_standalone_issue_comment_ids") or []
    )
    seen_check_run_ids: set[int] = set(repo_state.get("seen_check_run_ids") or [])
    pr_state: dict[str, str] = dict(repo_state.get("pr_state") or {})
    issue_state_map: dict[str, str] = dict(repo_state.get("issue_state") or {})

    # ---- PRs ---------------------------------------------------------------
    pulls = api(
        f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={RECENT_PR_LIMIT}"
    ) or []

    for pr in pulls:
        n = pr.get("number")
        if not isinstance(n, int):
            continue
        title = pr.get("title") or ""
        url = pr.get("html_url") or f"https://github.com/{repo}/pull/{n}"

        # PR state transitions (open → merged/closed). A PR is "merged"
        # when its state == closed AND merged_at is set; otherwise just
        # "closed". Don't emit on first run — only on actual transitions.
        new_state = _classify_pr_state(pr)
        prev_state = pr_state.get(str(n))
        if not first_run and prev_state and prev_state != new_state:
            events.append(
                Event(
                    kind="pr_state",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"from": prev_state, "to": new_state, "pr": pr},
                )
            )
        elif not first_run and prev_state is None and new_state == "open":
            # We're seeing this open PR for the first time *after* the
            # initial prime — treat as a new PR.
            events.append(
                Event(
                    kind="new_pr",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"pr": pr},
                )
            )
        pr_state[str(n)] = new_state

        # Skip detail fetches for closed PRs that haven't changed state —
        # most of the time they're just sitting in the recent-list because
        # their merge moved them into the window. Open PRs always get a
        # full detail pass.
        if new_state != "open" and prev_state == new_state:
            continue

        # Reviews — always fire (low-volume, high-signal; randos rarely review).
        try:
            reviews = api(f"repos/{repo}/pulls/{n}/reviews?per_page=100") or []
        except GHCommandError:
            reviews = []
        for r in reviews:
            rid = r.get("id")
            if not isinstance(rid, int) or rid in seen_review_ids:
                continue
            seen_review_ids.add(rid)
            if first_run:
                continue
            events.append(
                Event(
                    kind="review",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"review": r},
                )
            )

        # Inline review comments — always fire.
        try:
            review_comments = api(f"repos/{repo}/pulls/{n}/comments?per_page=100") or []
        except GHCommandError:
            review_comments = []
        for c in review_comments:
            cid = c.get("id")
            if not isinstance(cid, int) or cid in seen_review_comment_ids:
                continue
            seen_review_comment_ids.add(cid)
            if first_run:
                continue
            events.append(
                Event(
                    kind="review_comment",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"comment": c},
                )
            )

        # PR conversation comments — trust-gated. Marked as seen even when
        # untrusted so the drive-by isn't re-evaluated next poll.
        try:
            issue_comments = api(f"repos/{repo}/issues/{n}/comments?per_page=100") or []
        except GHCommandError:
            issue_comments = []
        for c in issue_comments:
            cid = c.get("id")
            if not isinstance(cid, int) or cid in seen_issue_comment_ids:
                continue
            seen_issue_comment_ids.add(cid)
            if first_run:
                continue
            if not is_trusted_association(c.get("author_association"), trusted):
                continue
            events.append(
                Event(
                    kind="issue_comment",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"comment": c},
                )
            )

        # Failed CI checks on the head commit. Only "completed + failure"
        # is interesting at first cut; in_progress / queued spam every
        # poll. ``conclusion`` may be missing on still-running checks.
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha:
            try:
                check_runs = (
                    api(
                        f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100"
                    )
                    or {}
                )
            except GHCommandError:
                check_runs = {}
            for run in (check_runs.get("check_runs") or []):
                if run.get("status") != "completed":
                    continue
                if run.get("conclusion") not in ("failure", "timed_out", "action_required"):
                    continue
                run_id = run.get("id")
                if not isinstance(run_id, int) or run_id in seen_check_run_ids:
                    continue
                seen_check_run_ids.add(run_id)
                if first_run:
                    continue
                events.append(
                    Event(
                        kind="check_failure",
                        repo=repo,
                        number=n,
                        title=title,
                        url=url,
                        payload={"check_run": run},
                    )
                )

    # ---- Standalone issues -------------------------------------------------
    # /repos/{repo}/issues returns BOTH issues and PRs; PRs carry a
    # ``pull_request`` key, pure issues don't. We filter the PRs out here
    # since the /pulls loop above already covers them.
    issues = api(
        f"repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page={RECENT_PR_LIMIT}"
    ) or []

    for issue in issues:
        if issue.get("pull_request"):
            continue
        n = issue.get("number")
        if not isinstance(n, int):
            continue
        title = issue.get("title") or ""
        url = issue.get("html_url") or f"https://github.com/{repo}/issues/{n}"
        author_assoc = issue.get("author_association")
        author_trusted = is_trusted_association(author_assoc, trusted)

        new_state = "open" if issue.get("state") == "open" else "closed"
        prev_state = issue_state_map.get(str(n))

        # State + new-issue events: emit only when the issue's *author* is
        # trusted AND we've finished priming the issue side. An untrusted
        # person opening an issue is silent; if you later want visibility,
        # ask Alice — her own comment from inside the worker won't make
        # the issue "trusted" automatically, but you can always look
        # directly via gh.
        if not first_run and issues_primed and author_trusted:
            if prev_state and prev_state != new_state:
                events.append(
                    Event(
                        kind="issue_state",
                        repo=repo,
                        number=n,
                        title=title,
                        url=url,
                        payload={"from": prev_state, "to": new_state, "issue": issue},
                    )
                )
            elif prev_state is None and new_state == "open":
                events.append(
                    Event(
                        kind="new_issue",
                        repo=repo,
                        number=n,
                        title=title,
                        url=url,
                        payload={"issue": issue},
                    )
                )
        issue_state_map[str(n)] = new_state

        # Same skip-the-detail-fetch shortcut as PRs: closed-and-unchanged
        # issues don't need their comments re-pulled.
        if new_state != "open" and prev_state == new_state:
            continue

        try:
            comments = api(f"repos/{repo}/issues/{n}/comments?per_page=100") or []
        except GHCommandError:
            comments = []
        for c in comments:
            cid = c.get("id")
            if not isinstance(cid, int) or cid in seen_standalone_issue_comment_ids:
                continue
            seen_standalone_issue_comment_ids.add(cid)
            if first_run or not issues_primed:
                continue
            if not is_trusted_association(c.get("author_association"), trusted):
                continue
            events.append(
                Event(
                    kind="standalone_issue_comment",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"comment": c, "issue_title": title},
                )
            )

    repo_state["seen_review_ids"] = sorted(seen_review_ids)
    repo_state["seen_review_comment_ids"] = sorted(seen_review_comment_ids)
    repo_state["seen_issue_comment_ids"] = sorted(seen_issue_comment_ids)
    repo_state["seen_standalone_issue_comment_ids"] = sorted(
        seen_standalone_issue_comment_ids
    )
    repo_state["seen_check_run_ids"] = sorted(seen_check_run_ids)
    repo_state["pr_state"] = pr_state
    repo_state["issue_state"] = issue_state_map
    repo_state["first_run"] = False
    repo_state["issues_primed"] = True
    repo_state["last_poll_at"] = _now_iso()
    return events


def _classify_pr_state(pr: dict[str, Any]) -> str:
    if pr.get("state") == "open":
        return "open"
    if pr.get("merged_at"):
        return "merged"
    return "closed"


# ---------------------------------------------------------------------------
# Note rendering
# ---------------------------------------------------------------------------


def _slugify(s: str, max_len: int = 40) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return (s or "note")[:max_len]


def _stamp_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")[:-3]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _now_local_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _truncate(s: str | None, cap: int = 1500) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + "\n…(truncated)"


def render_note(event: Event) -> tuple[str, str]:
    """Return ``(slug, body)`` for an event. Body excludes the header line.

    Slug feeds the filename; body is appended after the standard note
    header that mirrors ``alice_speaking.tools.inner.append_note``.
    """
    repo_slug = event.repo.replace("/", "-")
    pr_ref = f"{event.repo}#{event.number}"

    if event.kind == "review":
        review = event.payload["review"]
        author = (review.get("user") or {}).get("login") or "unknown"
        state = (review.get("state") or "unknown").lower()
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-review-{state}")
        body = (
            f"PR review on {pr_ref} ({event.title})\n"
            f"author: {author}\n"
            f"state: {state}\n"
            f"submitted_at: {review.get('submitted_at')}\n"
            f"url: {review.get('html_url') or event.url}\n"
            "\n"
            f"{_truncate(review.get('body'))}\n"
        )
        return slug, body

    if event.kind == "review_comment":
        c = event.payload["comment"]
        author = (c.get("user") or {}).get("login") or "unknown"
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-comment")
        body = (
            f"PR review comment on {pr_ref} ({event.title})\n"
            f"author: {author}\n"
            f"path: {c.get('path')}:{c.get('line') or c.get('original_line')}\n"
            f"created_at: {c.get('created_at')}\n"
            f"url: {c.get('html_url') or event.url}\n"
            "\n"
            f"{_truncate(c.get('body'))}\n"
        )
        return slug, body

    if event.kind == "issue_comment":
        c = event.payload["comment"]
        author = (c.get("user") or {}).get("login") or "unknown"
        assoc = c.get("author_association") or "unknown"
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-comment")
        body = (
            f"PR conversation comment on {pr_ref} ({event.title})\n"
            f"author: {author} ({assoc})\n"
            f"created_at: {c.get('created_at')}\n"
            f"url: {c.get('html_url') or event.url}\n"
            "\n"
            f"{_truncate(c.get('body'))}\n"
        )
        return slug, body

    if event.kind == "new_pr":
        pr = event.payload["pr"]
        author = (pr.get("user") or {}).get("login") or "unknown"
        is_draft = pr.get("draft", False)
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-opened")
        body = (
            f"New PR opened on {event.repo}: #{event.number} {event.title}\n"
            f"author: {author}\n"
            f"draft: {is_draft}\n"
            f"created_at: {pr.get('created_at')}\n"
            f"url: {event.url}\n"
            "\n"
            f"{_truncate(pr.get('body'))}\n"
        )
        return slug, body

    if event.kind == "pr_state":
        slug = _slugify(
            f"github-{repo_slug}-pr{event.number}-{event.payload['to']}"
        )
        body = (
            f"PR state change on {pr_ref} ({event.title})\n"
            f"transition: {event.payload['from']} → {event.payload['to']}\n"
            f"url: {event.url}\n"
        )
        return slug, body

    if event.kind == "check_failure":
        run = event.payload["check_run"]
        name = run.get("name") or "unknown"
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-ci-{name}")
        body = (
            f"CI check failed on {pr_ref} ({event.title})\n"
            f"check: {name}\n"
            f"conclusion: {run.get('conclusion')}\n"
            f"completed_at: {run.get('completed_at')}\n"
            f"url: {run.get('html_url') or event.url}\n"
            "\n"
            f"{_truncate((run.get('output') or {}).get('summary'))}\n"
        )
        return slug, body

    if event.kind == "new_issue":
        issue = event.payload["issue"]
        author = (issue.get("user") or {}).get("login") or "unknown"
        assoc = issue.get("author_association") or "unknown"
        slug = _slugify(f"github-{repo_slug}-issue{event.number}-opened")
        body = (
            f"New issue opened on {event.repo}: #{event.number} {event.title}\n"
            f"author: {author} ({assoc})\n"
            f"created_at: {issue.get('created_at')}\n"
            f"url: {event.url}\n"
            "\n"
            f"{_truncate(issue.get('body'))}\n"
        )
        return slug, body

    if event.kind == "issue_state":
        slug = _slugify(
            f"github-{repo_slug}-issue{event.number}-{event.payload['to']}"
        )
        body = (
            f"Issue state change on {event.repo}#{event.number} ({event.title})\n"
            f"transition: {event.payload['from']} → {event.payload['to']}\n"
            f"url: {event.url}\n"
        )
        return slug, body

    if event.kind == "standalone_issue_comment":
        c = event.payload["comment"]
        author = (c.get("user") or {}).get("login") or "unknown"
        assoc = c.get("author_association") or "unknown"
        slug = _slugify(f"github-{repo_slug}-issue{event.number}-comment")
        body = (
            f"Issue comment on {event.repo}#{event.number} ({event.title})\n"
            f"author: {author} ({assoc})\n"
            f"created_at: {c.get('created_at')}\n"
            f"url: {c.get('html_url') or event.url}\n"
            "\n"
            f"{_truncate(c.get('body'))}\n"
        )
        return slug, body

    raise ValueError(f"unknown event kind: {event.kind}")


def write_note(notes_dir: pathlib.Path, slug: str, body: str, *, tag: str = "github") -> pathlib.Path:
    """Write one note in the same shape as ``append_note`` so thinking
    drains it through the normal pipeline."""
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{_stamp_utc()}-{slug}.md"
    header = f"# note — {_now_local_iso()}\ntag: {tag}\n\n"
    path.write_text(header + body.rstrip() + "\n")
    return path


def _write_auth_error_note(
    notes_dir: pathlib.Path,
    state: dict[str, Any],
    err: GHCommandError,
) -> pathlib.Path | None:
    """Emit a loud auth-failure note, deduped to once per ~6h.

    Stored markers live on the top-level state dict (not per-repo) since
    auth is global.
    """
    last_at = state.get("auth_error_last_at")
    if last_at:
        try:
            delta = (
                dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(last_at)
            ).total_seconds()
        except ValueError:
            delta = AUTH_ERROR_NOTE_INTERVAL_SECONDS + 1
        if delta < AUTH_ERROR_NOTE_INTERVAL_SECONDS:
            return None
    state["auth_error_last_at"] = _now_iso()
    body = (
        "GitHub watcher auth failed — `gh api` is rejecting the token.\n"
        "Until this is fixed I can't see PR/review/comment activity on "
        "the watched repos. Run `gh auth login` (or refresh `GH_TOKEN` "
        "in alice.env, then bounce the worker) and the next poll will pick up.\n"
        "\n"
        f"stderr: {_truncate(err.stderr, 600)}\n"
    )
    return write_note(notes_dir, "github-watcher-auth-failed", body, tag="github-watcher-error")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(
    *,
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    api: Callable[[str], Any] = gh_api,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> int:
    """Run one watcher pass. Returns the desired process exit code.

    Exit codes:
      0  poll completed (zero or more notes written)
      0  watcher disabled / no repos configured (no-op is success)
      1  auth failure (note written, supervisor will retry next cadence)
      2  unexpected error (logged; supervisor retries)
    """
    cfg = load_config(mind_dir)
    if not cfg.enabled or not cfg.repos:
        log("[gh-watcher] disabled or no repos configured — exiting clean")
        return 0
    notes_dir = mind_dir / "inner" / "notes"
    state = load_state(state_path)
    repos_state = state.setdefault("repos", {})
    notes_written = 0
    saw_auth_error = False

    for repo in cfg.repos:
        repo_state = repos_state.setdefault(repo, _empty_repo_state())
        try:
            events = poll_repo(
                repo,
                repo_state,
                trusted=cfg.trusted_associations,
                api=api,
            )
        except GHCommandError as exc:
            log(f"[gh-watcher] {repo}: gh failed — {exc}")
            if exc.looks_like_auth_failure:
                _write_auth_error_note(notes_dir, state, exc)
                saw_auth_error = True
                # Skip remaining repos — auth is global, retrying just spams logs.
                break
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"[gh-watcher] {repo}: unexpected error — {type(exc).__name__}: {exc}")
            continue
        for event in events:
            slug, body = render_note(event)
            path = write_note(notes_dir, slug, body)
            notes_written += 1
            log(f"[gh-watcher] {repo}: wrote {path.name} ({event.kind})")

    save_state(state_path, state)
    log(f"[gh-watcher] done — {notes_written} note(s) written")
    return 1 if saw_auth_error else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One pass of Alice's GitHub watcher.")
    parser.add_argument("--mind", default=str(DEFAULT_MIND), help="alice-mind path")
    parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_DIR / DEFAULT_STATE_FILE),
        help="path to gh-watcher-state.json",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    return run(
        mind_dir=pathlib.Path(args.mind),
        state_path=pathlib.Path(args.state),
    )


if __name__ == "__main__":
    sys.exit(main())
