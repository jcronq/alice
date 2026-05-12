"""State Machine v0 dispatcher — one-shot ``gh``-driven hello-commenter.

Modeled on :mod:`alice_watchers.github`. Each invocation is a single pass:

  1. Poll ``jcronq/alice`` for open issues labeled ``sm:selected``
     (``gh issue list ... --json number,title,labels,author,...``).
  2. Apply the v0 trust filter — author whitelist, exactly one ``sm:*``
     label, at least one ``art:*`` label — all from explicit allow-lists
     so a typo (``sm:building-pleaserun``) is silently dropped instead of
     producing a fuzzy match.
  3. For each unseen passing issue, post a one-time
     ``[SM] dispatcher-hello ...`` comment as audit-trail evidence and
     record the issue number in
     ``/state/worker/sm-dispatcher-state.json`` so we don't re-comment
     on the next cadence.

That's the whole v0 functionality: prove the substrate event-to-comment
pipeline works. Agent spawning, state transitions, checklists, and
amendment routing are all deferred to v1+.

The script is intended to be invoked on a cadence by s6 (later phase);
right now it runs by hand via ``python -m alice_sm.dispatcher``. The
``--dry-run`` flag prints the comments that would be posted without
touching GitHub or the state file — useful for tests and manual
verification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
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

# Pull recent open ``sm:selected`` issues per poll. The dispatcher only
# ever acts on ``sm:selected`` (the "picked up by Alice's lane" state),
# so this is bounded by the active task slate, not historical issues.
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
        "sm:selected",
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

# v0 only ever acts on ``sm:selected``. Other ``sm:*`` states will be
# handled in later phases (building → spawn agent, reviewing → post
# review-ready comment, etc.).
ACTIVE_SM_LABEL = "sm:selected"

# Schema version of the state file. Bump if the structure changes
# incompatibly.
STATE_VERSION = 1


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
    """

    version: int = STATE_VERSION
    hello_commented: list[int] = field(default_factory=list)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hello_commented": list(self.hello_commented),
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
    return DispatcherState(version=STATE_VERSION, hello_commented=numbers)


def save_state(state_path: pathlib.Path, state: DispatcherState) -> None:
    """Atomically replace the state file. Caps the seen-issue list."""
    if len(state.hello_commented) > SEEN_ISSUE_CAP:
        overflow = len(state.hello_commented) - SEEN_ISSUE_CAP
        del state.hello_commented[:overflow]
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


def gh_list_selected_issues(repo: str, *, gh_bin: str = "gh") -> list[dict[str, Any]]:
    """Return open issues labeled ``sm:selected`` in ``repo`` as a list of dicts.

    Uses ``gh issue list`` because it's the one ``gh`` subcommand that
    gives us authors, labels, and association in a single call. The
    ``--json`` field list is fixed: anything missing causes ``gh`` to
    exit non-zero, which the caller surfaces as :class:`GHCommandError`.
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
        "number,title,labels,author,createdAt",
        "--limit",
        str(RECENT_ISSUE_LIMIT),
    ]
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
        return []
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        return []
    return payload


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


# Callable aliases — tests inject fakes here without monkeypatching the
# module-level names.
ListIssuesFn = Callable[[str], list[dict[str, Any]]]
PostCommentFn = Callable[[str, int, str], None]


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


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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


def run(
    *,
    repo: str = DEFAULT_REPO,
    state_path: pathlib.Path,
    list_issues: ListIssuesFn = gh_list_selected_issues,
    post_comment: PostCommentFn = gh_post_comment,
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
    report = RunReport()
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

    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            log(f"[sm-dispatcher] skipping issue with non-integer number: {number!r}")
            continue

        if state.has_hello(number):
            report.skipped_dedup += 1
            continue

        decision = evaluate_trust(issue)
        if not decision.accepted:
            log(f"[sm-dispatcher] skipping #{number}: {decision.reason}")
            report.skipped_trust += 1
            continue

        # Guard: an accepted decision must carry an art label.
        art_label = decision.art_label or "art:unknown"
        body = render_hello_comment(number, art_label, timestamp=now_iso())

        if dry_run:
            log(f"[sm-dispatcher] DRY-RUN would post on #{number}: {body}")
            # In dry-run we deliberately do NOT mark dedup state. A
            # second dry-run pass should produce the same output so the
            # operator can re-run safely while inspecting.
            report.posted += 1
            report.posted_numbers.append(number)
            continue

        try:
            post_comment(repo, number, body)
        except GHCommandError as exc:
            log(f"[sm-dispatcher] failed to comment on #{number}: {exc}")
            # State NOT updated for this issue — we'll retry next pass.
            # But keep processing the rest of the slate; one broken
            # comment shouldn't block other tasks.
            if exc.looks_like_auth_failure or exc.looks_like_rate_limit:
                # Bail entirely — the rest of the pass will hit the same wall.
                save_state(state_path, state)
                return 1, report
            continue

        state.mark_hello(number)
        report.posted += 1
        report.posted_numbers.append(number)
        log(f"[sm-dispatcher] posted dispatcher-hello on #{number}")

    if not dry_run:
        save_state(state_path, state)

    log(
        f"[sm-dispatcher] done — polled={report.polled} "
        f"posted={report.posted} "
        f"skipped_dedup={report.skipped_dedup} "
        f"skipped_trust={report.skipped_trust}"
    )
    return 0, report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One pass of the State Machine v0 dispatcher."
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
        help="print the comments that would be posted, don't touch GitHub or state",
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
