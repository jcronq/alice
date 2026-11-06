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

# Marker Speaking embeds in issue bodies it files autonomously, so the
# watcher can suppress the resulting ``new_issue`` event and avoid the
# redundant thinking-analysis → attempt-issue-fix loop on Alice's own
# tickets (issue #226). The string is short, fixed, and HTML-comment
# wrapped so it survives the GitHub renderer untouched and won't collide
# with any natural body content. Issue #226 documents the full flow.
# The companion writer lives in ``alice_speaking.github`` so both sides
# import the same constant.
SELF_FILED_MARKER = "<!-- alice-self-filed -->"


def _is_self_filed(issue: dict[str, Any]) -> bool:
    """True iff the issue body carries the Alice self-filed marker.

    Case-sensitive substring check — the marker is a fixed HTML comment
    chosen so it can't collide with natural body content. Missing /
    non-string bodies count as not self-filed.
    """
    body = issue.get("body")
    if not isinstance(body, str):
        return False
    return SELF_FILED_MARKER in body


# Prefix the SM v2 dispatcher puts on its lifecycle comments (hello /
# transition). They carry no new information for thinking, so the watcher
# suppresses them regardless of author (issue #615).
SM_COMMENT_PREFIX = "[SM]"


def _is_own_activity(author_login: str | None, own_login: str) -> bool:
    """True iff ``author_login`` is Alice's own GitHub identity (case-insensitive)."""
    if not author_login or not own_login:
        return False
    return author_login.strip().lower() == own_login.strip().lower()


def _is_sm_protocol_comment(body: str | None) -> bool:
    """True iff the comment body begins with the ``[SM]`` protocol prefix."""
    if not isinstance(body, str):
        return False
    return body.lstrip().upper().startswith(SM_COMMENT_PREFIX)


def _login(obj: dict[str, Any]) -> str | None:
    return (obj.get("user") or {}).get("login")


# Label the SM v2 dispatcher uses as the lifecycle entry point. The
# dispatcher's GitHub query (``src/alice_forge/dispatcher/gh.py``) filters
# by ``label:sm:draft,sm:needs_study,...`` — an unlabeled trusted-author
# issue is invisible to the dispatcher and stalls indefinitely. The
# watcher applies this on ``new_issue`` intake so every fresh trusted
# issue enters the SM lifecycle automatically. Re-stated here rather
# than imported from ``alice_forge.dispatcher.constants`` to keep the
# watcher decoupled from the dispatcher package.
SM_DRAFT_LABEL = "sm:draft"
SM_LABEL_PREFIX = "sm:"
# Label whose arrival means "a design is waiting on Jason's review". The
# watcher turns the transition into a ``kind: decide`` item for the status
# site's Review page (issue #616).
SM_DESIGN_REVIEW_LABEL = "sm:design_review"


def _extract_label_names(labels: Any) -> list[str]:
    """Pull ``name`` strings out of the GitHub-API labels payload.

    ``gh api`` returns labels as a list of dicts ``{"id":..., "name":...,
    "color":...}``. Be tolerant of bare strings (older fixtures / future
    schema drift) and skip anything that doesn't yield a usable name.
    """
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for entry in labels:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        elif isinstance(entry, str) and entry:
            names.append(entry)
    return names


def _apply_sm_draft_label(
    repo: str,
    number: int,
    current_labels: list[str],
    *,
    gh_bin: str = "gh",
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> bool:
    """Best-effort: stamp ``sm:draft`` on a freshly-emitted issue.

    Returns True iff the label was actually applied. Idempotency: if the
    issue already carries any ``sm:*`` label it's already inside the SM
    lifecycle and we leave it alone (don't clobber ``sm:needs_study`` or
    later states a human pre-set).

    Failures (gh missing, network blip, label-doesn't-exist-yet) are
    logged and swallowed — labeling must never block the ``new_issue``
    note emission, which is the primary product of this loop.
    """
    if any(lbl.startswith(SM_LABEL_PREFIX) for lbl in current_labels):
        return False
    args = [
        gh_bin,
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--add-label",
        SM_DRAFT_LABEL,
    ]
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"[gh-watcher] {repo}#{number}: sm:draft label apply failed — {exc}")
        return False
    if result.returncode != 0:
        log(
            f"[gh-watcher] {repo}#{number}: sm:draft label apply exited "
            f"{result.returncode} — {(result.stderr or result.stdout).strip()[:200]}"
        )
        return False
    return True


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
        # Rate-limit 403s are self-healing (window resets ~1 hr) and must
        # not be misclassified as auth failures — otherwise the watcher
        # emits a `github-watcher-auth-failed` note for a transient
        # condition. All `gh` calls on a host share one user bucket, so
        # rate-limit hits are recurring; see
        # research/2026-07-23-gh-watcher-rate-limit-403-misdiagnosis.md
        # and research/2026-07-09-gh-watcher-rate-limit-root-cause.md.
        if self.returncode == 403 or "403" in msg:
            if any(
                n in msg
                for n in ("rate limit", "rate-limit", "abuse detection")
            ):
                return False
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
    own_login: str = "alice"


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
    repos = [
        r.strip()
        for r in (block.get("repos") or [])
        if isinstance(r, str) and r.strip()
    ]
    enabled = bool(block.get("enabled", bool(repos)))
    interval = int(block.get("poll_interval_seconds") or 300)
    raw_trust = block.get("trusted_associations")
    if isinstance(raw_trust, list) and raw_trust:
        trust = frozenset(str(s).strip().upper() for s in raw_trust if str(s).strip())
    else:
        trust = frozenset(DEFAULT_TRUSTED_ASSOCIATIONS)
    raw_own = block.get("own_login")
    own_login = (
        raw_own.strip() if isinstance(raw_own, str) and raw_own.strip() else "alice"
    )
    return WatcherConfig(
        enabled=enabled,
        repos=repos,
        poll_interval_seconds=interval,
        trusted_associations=trust,
        own_login=own_login,
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
        print(
            f"[gh-watcher] state at {state_path} is corrupt — resetting",
            file=sys.stderr,
        )
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
    fd, tmp = tempfile.mkstemp(
        dir=state_path.parent, prefix=".gh-watcher-", suffix=".json"
    )
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
        # ``{issue number: [label names]}`` as of the last poll — the
        # edge detector for ``→ sm:design_review`` transitions. A missing
        # entry (legacy state, or an issue we've never seen) is silent.
        "issue_labels": {},
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
    #   new_issue, issue_state, standalone_issue_comment, design_review
    #     — issue-scoped events. ``number`` is the issue number.
    #     ``design_review`` fires once when an open issue gains
    #     ``sm:design_review``; ``run`` turns it into a status-site
    #     decide item rather than an ``inner/notes/`` entry.
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
    own_login: str = "alice",
    api: Callable[[str], Any] = gh_api,
    label_apply: Callable[[str, int, list[str]], bool] = _apply_sm_draft_label,
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
    seen_review_comment_ids: set[int] = set(
        repo_state.get("seen_review_comment_ids") or []
    )
    seen_issue_comment_ids: set[int] = set(
        repo_state.get("seen_issue_comment_ids") or []
    )
    seen_standalone_issue_comment_ids: set[int] = set(
        repo_state.get("seen_standalone_issue_comment_ids") or []
    )
    seen_check_run_ids: set[int] = set(repo_state.get("seen_check_run_ids") or [])
    pr_state: dict[str, str] = dict(repo_state.get("pr_state") or {})
    issue_state_map: dict[str, str] = dict(repo_state.get("issue_state") or {})
    issue_labels_map: dict[str, list[str]] = dict(repo_state.get("issue_labels") or {})

    # ---- PRs ---------------------------------------------------------------
    pulls = (
        api(
            f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={RECENT_PR_LIMIT}"
        )
        or []
    )

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
            if _is_own_activity(_login(c), own_login) or _is_sm_protocol_comment(
                c.get("body")
            ):
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
                    api(f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100")
                    or {}
                )
            except GHCommandError:
                check_runs = {}
            for run in check_runs.get("check_runs") or []:
                if run.get("status") != "completed":
                    continue
                if run.get("conclusion") not in (
                    "failure",
                    "timed_out",
                    "action_required",
                ):
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
    issues = (
        api(
            f"repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page={RECENT_PR_LIMIT}"
        )
        or []
    )

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
        #
        # Self-filed gate (issue #226): when Speaking files an issue
        # autonomously via ``alice_speaking.github.create_issue`` she
        # embeds ``SELF_FILED_MARKER`` in the body. The whole point of
        # the watcher firing on a new issue is to route it through
        # thinking → attempt-issue-fix; for issues Speaking already
        # initiated, that loop is pure redundancy plus user-facing
        # noise. State is still tracked so a later genuine state
        # transition (if it ever matters) isn't lost.
        self_filed = _is_self_filed(issue)
        own_authored = _is_own_activity(_login(issue), own_login)
        if not first_run and issues_primed and author_trusted and not self_filed:
            if own_authored:
                pass
            elif prev_state and prev_state != new_state:
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
            # Best-effort SM v2 intake: stamp ``sm:draft`` so the
            # dispatcher's labeled-issue query can see the issue. This is
            # deliberately *decoupled* from the ``new_issue`` event above:
            # that ``elif`` only fires on an unseen→open transition seen
            # *after* priming, so issues that were already open when the
            # repo was first wired into the watcher (recorded as "open"
            # during the prime pass — e.g. a repo onboarded with a
            # pre-existing backlog like cozyhem-engine) never hit it and
            # would stall forever at no label, invisible to the
            # dispatcher's ``label:sm:*`` query. Gating the stamp on
            # open + trusted + non-self-filed (rather than the
            # unseen→open transition) back-fills that priming gap.
            #
            # ``_apply_sm_draft_label`` is idempotent — it no-ops if any
            # ``sm:*`` label is already present — so re-stamping a fresh
            # issue we just emitted a ``new_issue`` for is harmless, and
            # we never replay a historical ``new_issue`` note just to fix
            # a missing label. Failures are logged inside the labeler and
            # must not tank the pass. Issues #247/#258/#260/#261 are the
            # structural backlog that motivated this.
            if new_state == "open":
                try:
                    label_apply(
                        repo,
                        n,
                        _extract_label_names(issue.get("labels")),
                    )
                except Exception:  # noqa: BLE001
                    # Defensive: even a buggy injected labeler must not
                    # tank the watcher pass. Already-logged failures
                    # come back as False; this catches programming
                    # errors in label_apply itself.
                    pass
        issue_state_map[str(n)] = new_state

        # ``→ sm:design_review`` edge. Fires once per transition: the
        # label set is persisted every poll, so a still-labeled issue
        # never re-fires. Silent while priming and when we have no prior
        # label snapshot for the issue.
        current_labels = _extract_label_names(issue.get("labels"))
        prev_labels = issue_labels_map.get(str(n))
        if (
            not first_run
            and issues_primed
            and new_state == "open"
            and prev_labels is not None
            and SM_DESIGN_REVIEW_LABEL in current_labels
            and SM_DESIGN_REVIEW_LABEL not in prev_labels
        ):
            events.append(
                Event(
                    kind="design_review",
                    repo=repo,
                    number=n,
                    title=title,
                    url=url,
                    payload={"issue": issue},
                )
            )
        issue_labels_map[str(n)] = sorted(set(current_labels))

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
            if _is_own_activity(_login(c), own_login) or _is_sm_protocol_comment(
                c.get("body")
            ):
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
    repo_state["issue_labels"] = issue_labels_map
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
        slug = _slugify(f"github-{repo_slug}-pr{event.number}-{event.payload['to']}")
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
        slug = _slugify(f"github-{repo_slug}-issue{event.number}-{event.payload['to']}")
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


def write_note(
    notes_dir: pathlib.Path, slug: str, body: str, *, tag: str = "github"
) -> pathlib.Path:
    """Write one note in the same shape as ``append_note`` so thinking
    drains it through the normal pipeline."""
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{_stamp_utc()}-{slug}.md"
    header = f"# note — {_now_local_iso()}\ntag: {tag}\n\n"
    path.write_text(header + body.rstrip() + "\n")
    return path


# ---------------------------------------------------------------------------
# Design-review decide items (issue #616)
# ---------------------------------------------------------------------------

_DESIGN_READY_NOTE_RE = re.compile(r"note=\[\[([^\]\s]+)\]\]")
_DESIGN_NOTE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")
# Same lookup order as the dispatcher's ``_resolve_design_note_path``
# (re-stated to keep the watcher decoupled from alice_forge).
_DESIGN_NOTE_DIRS = ("cortex-memory/designs", "cortex-memory/research")
# Matches ``status_site.server.REVIEW_SLUG_MAX`` so the note's file stem
# survives the server's slug sanitiser unchanged.
_REVIEW_SLUG_MAX = 60


def _design_review_slug(repo: str, number: int) -> str:
    return _slugify(
        f"design-review-{repo.replace('/', '-')}-{number}", max_len=_REVIEW_SLUG_MAX
    )


def _find_design_ready_comment(
    comments: list[dict[str, Any]], trusted: frozenset[str]
) -> dict[str, Any] | None:
    """Newest trusted ``[SM] design-ready`` comment (audit echoes skipped)."""
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str) or not body.startswith("[SM] design-ready"):
            continue
        if body.startswith("[SM] design-ready-audit"):
            continue
        if not is_trusted_association(c.get("author_association"), trusted):
            continue
        return c
    return None


def _read_design_note(mind_dir: pathlib.Path, slug: str) -> str | None:
    if not _DESIGN_NOTE_SLUG_RE.match(slug):
        return None
    for rel in _DESIGN_NOTE_DIRS:
        candidate = mind_dir / rel / f"{slug}.md"
        if candidate.is_file():
            try:
                return candidate.read_text(errors="replace")
            except OSError:
                return None
    return None


def write_design_review_item(
    mind_dir: pathlib.Path,
    event: Event,
    *,
    api: Callable[[str], Any],
    trusted: frozenset[str],
) -> pathlib.Path | None:
    """Write the ``kind: decide`` note the status site's Review page renders.

    The plan body is embedded (design note if it resolves, else the
    ``[SM] design-ready`` comment itself) so ``status_site`` can render
    offline without ``gh``. Returns ``None`` when no plan could be found —
    the item is skipped rather than shown empty.
    """
    try:
        comments = api(f"repos/{event.repo}/issues/{event.number}/comments?per_page=100") or []
    except GHCommandError:
        return None
    ready = _find_design_ready_comment(comments, trusted)
    if ready is None:
        return None
    ready_body = ready["body"]
    plan: str | None = None
    note_slug = ""
    m = _DESIGN_READY_NOTE_RE.search(ready_body)
    if m:
        note_slug = m.group(1)
        plan = _read_design_note(mind_dir, note_slug)
    if plan is None:
        plan = ready_body
    # Plan notes may carry their own frontmatter; drop it so ours is the
    # only block the server parses.
    if plan.startswith("---\n"):
        end = plan.find("\n---\n", 4)
        if end >= 0:
            plan = plan[end + len("\n---\n") :]

    slug = _design_review_slug(event.repo, event.number)
    title = " ".join(event.title.replace('"', "'").split())
    front = (
        "---\n"
        "kind: decide\n"
        "needs: review\n"
        f"repo: {event.repo}\n"
        f"issue: {event.number}\n"
        f'title: "{title}"\n'
        f"url: {event.url}\n"
        f"design_note: {note_slug}\n"
        f"created: {_now_local_iso()}\n"
        "---\n"
    )
    heading = f"# Design review — {event.repo}#{event.number}: {title}\n\n"
    out_dir = mind_dir / "inner" / "status" / "notes"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.md"
    tmp = out_dir / f".{slug}.tmp"
    tmp.write_text(front + "\n" + heading + plan.strip() + "\n")
    os.replace(tmp, path)
    return path


# Telemetry event kinds — high-volume, low-conversation signal (new PRs,
# PR state transitions, CI failures, issue open/close). These bypass the
# inner/notes/ → thinking pipeline and land directly in events.jsonl so
# the inbox stays focused on true conversation events (reviews, comments,
# new issues) that actually want thinking's attention. See task-0603.
TELEMETRY_KINDS = frozenset(
    {"new_pr", "pr_state", "check_failure", "issue_state"}
)


def write_event(
    events_path: pathlib.Path,
    *,
    kind: str,
    body: str,
    extra: dict | None = None,
) -> None:
    """Append one JSON line to ``events.jsonl`` for a telemetry event.

    Line shape:

        {"ts": <iso8601 with tz>, "type": "github_<kind>", "repo": <repo>,
         "kind": <kind>, "body": <as-emitted>, ...extra}

    Failures are logged to stderr and swallowed — this mirrors
    ``write_note``'s posture (a broken events.jsonl write must not crash
    the whole poll pass, which would prevent the state file save and
    force a re-poll on the next cadence).
    """
    extra = dict(extra or {})
    # Repo comes off extra so callers only pass it once; empty-string
    # fallback keeps the record schema-consistent even in malformed
    # call sites rather than crashing on KeyError.
    repo = str(extra.pop("repo", "") or "")
    record: dict[str, Any] = {
        # Same timestamp idiom as stage_b._now_iso_tz — local-time
        # ISO-8601 with offset, matching the events.jsonl convention
        # produced by ``event-log`` and the memory worker.
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "type": f"github_{kind}",
        "repo": repo,
        "kind": kind,
        "body": body,
        **extra,
    }
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[gh-watcher] failed to append event {kind} to {events_path}: {exc}",
            file=sys.stderr,
        )


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
                dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_at)
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
    return write_note(
        notes_dir, "github-watcher-auth-failed", body, tag="github-watcher-error"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(
    *,
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    api: Callable[[str], Any] = gh_api,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    label_apply: Callable[[str, int, list[str]], bool] = _apply_sm_draft_label,
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
    # Telemetry sink for new_pr / pr_state / check_failure / issue_state.
    # These skip the inner/notes/ pipeline entirely — thinking doesn't
    # need to read every closed PR or CI failure, but a structured log
    # is still useful for auditing / "when did X last happen" queries.
    events_path = mind_dir / "memory" / "events.jsonl"
    state = load_state(state_path)
    repos_state = state.setdefault("repos", {})
    notes_written = 0
    events_written = 0
    saw_auth_error = False

    for repo in cfg.repos:
        repo_state = repos_state.setdefault(repo, _empty_repo_state())
        try:
            events = poll_repo(
                repo,
                repo_state,
                trusted=cfg.trusted_associations,
                own_login=cfg.own_login,
                api=api,
                label_apply=label_apply,
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
            if event.kind == "design_review":
                try:
                    path = write_design_review_item(
                        mind_dir, event, api=api, trusted=cfg.trusted_associations
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"[gh-watcher] {repo}: design-review item failed — {exc}")
                    continue
                if path is None:
                    log(
                        f"[gh-watcher] {repo}#{event.number}: design_review "
                        "edge but no design-ready plan found — skipped"
                    )
                else:
                    notes_written += 1
                    log(f"[gh-watcher] {repo}: wrote decide item {path.name}")
                continue
            slug, body = render_note(event)
            if event.kind in TELEMETRY_KINDS:
                write_event(
                    events_path,
                    kind=event.kind,
                    body=body,
                    extra={
                        "repo": event.repo,
                        "number": event.number,
                        "title": event.title,
                        "url": event.url,
                    },
                )
                events_written += 1
                log(
                    f"[gh-watcher] {repo}: appended events.jsonl entry ({event.kind})"
                )
            else:
                path = write_note(notes_dir, slug, body)
                notes_written += 1
                log(f"[gh-watcher] {repo}: wrote {path.name} ({event.kind})")

    save_state(state_path, state)
    log(
        f"[gh-watcher] done — {notes_written} note(s), "
        f"{events_written} event(s) written"
    )
    return 1 if saw_auth_error else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the agent's GitHub watcher."
    )
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
