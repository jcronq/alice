"""Consolidated GH → ``alice-state.db`` poller (issue #790, Step 2).

One polling job that scrapes jcronq/alice (and other configured repos)
for open issues + PRs, upserts them into the SM v2 substrate tables,
and records a ``derive_runs`` row per pass.

Design points:

* **ETag-aware.** Each ``(repo, endpoint)`` pair carries a stored
  ``ETag`` from the last response. The next call sends
  ``If-None-Match: <etag>``. On a 304 we skip the body entirely — no
  rate-limit spend, no SQL. The ``gh`` CLI doesn't expose response
  headers cleanly, so we shell out to ``curl`` directly against the
  REST API with the OAuth token already in the environment
  (``GITHUB_TOKEN``, falling back to ``gh auth token``).
* **Shared backoff.** Before every poll iteration we read
  :func:`alice_forge.rate_limit.read_backoff_state`. If a backoff
  window is active (someone else observed a 429), we skip this cycle
  and record a derive_runs row with ``status='backoff'``. On a 403
  with ``X-RateLimit-Remaining: 0`` or a 429, we call
  :func:`alice_forge.rate_limit.observe_gh_error` so the backoff
  window ripples out to every other gh-caller.
* **Idempotent writes.** All writes use ``INSERT ... ON CONFLICT DO
  UPDATE`` on ``items``, ``INSERT OR IGNORE`` on
  ``item_external_refs``, and the unique index
  ``(item_id, source, source_ref)`` dedups ``transitions``. Re-running
  this poller twice in succession produces zero new rows.
* **Consolidated.** One pass pulls issues AND PRs for a repo in two
  calls; label-fan-out polling (``thinking_queue_poller`` doing 6 ×
  per-label queries) is replaced by a single ``?state=open`` fetch
  that the reader then slices locally.

**Scope**: This module is write-only to the DB tables created by
Step 1 (``items``, ``item_external_refs``, ``state_machine``,
``transitions``, ``amendments``). It does NOT modify GH labels or
comments — that's Step 4 (projection outbound). Readers flip to the
DB in Step 3 (see :mod:`status_site.sources.db_readers`).

Not wired to an s6 service in this PR — the memory-worker wake
invokes :func:`derive_gh_poll` once per 30-minute tick as a baseline
refresh. A dedicated 90 s s6 service is a follow-up (sandbox config
change, out of scope for the code-only PR).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from alice_forge.rate_limit import (
    DEFAULT_BACKOFF_PATH,
    observe_gh_error,
    read_backoff_state,
)
from status_site.sources.components import component_for_path

from .schema import apply_schema
from .sm_seed import (
    _pick_sm_label,
    _upsert_item,
    _upsert_state,
    gh_issue_id,
    gh_pr_id,
)

log = logging.getLogger(__name__)

DEFAULT_REPOS = ("jcronq/alice",)
DEFAULT_CADENCE_S = 90

#: Default path for the per-repo per-endpoint ETag cache. Lives under
#: the same state dir as the DB so backup / wipe semantics stay
#: aligned.
DEFAULT_ETAG_CACHE_PATH = (
    pathlib.Path.home()
    / "alice-mind"
    / "inner"
    / "state"
    / "gh-poll-etags.json"
)


# Narrow exception the caller observes so :func:`observe_gh_error` can
# route it to the shared backoff file. Attributes mirror the
# ``GHCommandError`` shape used by the dispatcher.
class GHPollError(RuntimeError):
    """Raised on curl / HTTP failure during a poll.

    ``looks_like_rate_limit`` is True iff the response indicates a
    primary or secondary rate-limit breach (status 403 + remaining=0,
    status 429, or a documented secondary-limit message). The shared
    backoff helper reads this attribute.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        looks_like_rate_limit: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.looks_like_rate_limit = looks_like_rate_limit


# ---------------------------------------------------------------------------
# Public dataclass — flattened into heartbeat events.


@dataclasses.dataclass
class PollReport:
    """Counts + status per :func:`derive_gh_poll` call."""

    status: str = "ok"  # 'ok' | 'backoff' | 'rate_limited' | 'error'
    repos_polled: int = 0
    issues_seen: int = 0
    prs_seen: int = 0
    items_upserted: int = 0
    states_upserted: int = 0
    transitions_inserted: int = 0
    amendments_inserted: int = 0
    not_modified_responses: int = 0
    http_calls: int = 0
    rate_limit_hits: int = 0
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repos_polled": self.repos_polled,
            "issues_seen": self.issues_seen,
            "prs_seen": self.prs_seen,
            "items_upserted": self.items_upserted,
            "states_upserted": self.states_upserted,
            "transitions_inserted": self.transitions_inserted,
            "amendments_inserted": self.amendments_inserted,
            "not_modified_responses": self.not_modified_responses,
            "http_calls": self.http_calls,
            "rate_limit_hits": self.rate_limit_hits,
            "error_count": len(self.errors),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# ETag cache — small JSON file on disk.


def _read_etag_cache(path: pathlib.Path) -> dict[str, str]:
    """Return ``{cache_key: etag}`` from ``path`` (or ``{}``).

    Missing / unreadable / malformed → empty dict so the next call
    just fetches without any If-None-Match header. Defence in depth
    against a truncated write from a crashed prior run.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_etag_cache(path: pathlib.Path, cache: dict[str, str]) -> None:
    """Atomic write of the ETag cache. Missing parent → create.

    Writes go through a tempfile + ``os.replace`` so a crash mid-write
    never leaves a torn file (same shape as
    ``gh_state_mirror._atomic_write`` elsewhere in the tree).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Best-effort — if we can't persist the cache we still ran the
        # poll successfully, just lose the 304 savings next cycle.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# HTTP fetch helpers — curl wrapper.

#: Type for an injectable subprocess runner. Tests pass a fake so no
#: network IO fires.
Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _resolve_token(
    env: dict[str, str], gh_token_runner: Optional[Runner] = None
) -> Optional[str]:
    """Pick up the GitHub token from env or shell out to ``gh auth token``.

    Checked in order: ``GH_TOKEN``, ``GITHUB_TOKEN``, ``gh auth token``.
    Returns ``None`` when no token can be resolved — the caller then
    falls back to unauthenticated requests (which still work for
    public repos, just with the IP-address 60/hr floor).
    """
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        v = env.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if gh_token_runner is None:
        gh_token_runner = _default_runner
    try:
        result = gh_token_runner(["gh", "auth", "token"])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


# Header-dump regex. ``curl -D -`` writes headers to stdout in the
# form ``HTTP/2 304\r\nETag: "W/\"abc\""\r\n...``. We only care about
# the status line and the ``ETag`` header; everything else is noise.
_HTTP_STATUS_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})", re.MULTILINE)
_ETAG_RE = re.compile(r"^ETag:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_RETRY_AFTER_RE = re.compile(
    r"^Retry-After:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_RL_REMAINING_RE = re.compile(
    r"^X-RateLimit-Remaining:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclasses.dataclass(frozen=True)
class _HTTPResult:
    status: int
    etag: Optional[str]
    body: Optional[str]
    rate_limit_remaining: Optional[int]
    retry_after: Optional[str]


def _parse_curl_output(stdout: str) -> _HTTPResult:
    """Split ``curl -D - -s <url>`` output into headers + body.

    curl writes response headers (because of ``-D -``) BEFORE the body,
    separated by a blank line. On a 304 the body is empty.
    """
    # Headers / body split. Multi-response chains (redirects) rejoin
    # cleanly: the last blank-line separator is the one we want.
    parts = stdout.split("\r\n\r\n") if "\r\n\r\n" in stdout else stdout.split("\n\n")
    header_block = ""
    body = ""
    if len(parts) >= 2:
        header_block = parts[0]
        body = "\n\n".join(parts[1:])
    else:
        header_block = parts[0]
        body = ""

    status_match = _HTTP_STATUS_RE.search(header_block)
    status = int(status_match.group(1)) if status_match else 0
    etag_match = _ETAG_RE.search(header_block)
    etag = etag_match.group(1).strip() if etag_match else None
    rl_match = _RL_REMAINING_RE.search(header_block)
    rl_remaining = int(rl_match.group(1)) if rl_match else None
    ra_match = _RETRY_AFTER_RE.search(header_block)
    retry_after = ra_match.group(1).strip() if ra_match else None

    return _HTTPResult(
        status=status,
        etag=etag,
        body=body if body.strip() else None,
        rate_limit_remaining=rl_remaining,
        retry_after=retry_after,
    )


def fetch_github(
    url: str,
    *,
    token: Optional[str],
    etag: Optional[str] = None,
    runner: Runner = _default_runner,
    user_agent: str = "alice-state-db-poller/1.0",
) -> _HTTPResult:
    """Issue one authenticated REST call, returning headers + body.

    ``curl -D -`` dumps response headers to stdout; we parse both.
    Raises :class:`GHPollError` on connection failure or 5xx; a 304 is
    returned normally (body is ``None``). A 403 with
    ``X-RateLimit-Remaining: 0`` or a 429 is raised as
    ``GHPollError(..., looks_like_rate_limit=True)`` so the caller can
    hand it to :func:`observe_gh_error`.
    """
    cmd = [
        "curl",
        "-s",
        "-D",
        "-",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        f"User-Agent: {user_agent}",
    ]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if etag:
        cmd += ["-H", f"If-None-Match: {etag}"]
    cmd.append(url)

    try:
        result = runner(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GHPollError(f"curl invocation failed: {exc!r}") from exc
    if result.returncode != 0:
        raise GHPollError(
            f"curl exit={result.returncode} url={url} stderr={(result.stderr or '').strip()[:200]}"
        )
    parsed = _parse_curl_output(result.stdout or "")
    if parsed.status == 0:
        raise GHPollError(
            f"curl returned no HTTP status for {url} (stdout sample: {(result.stdout or '')[:120]!r})"
        )

    # Rate-limit signals.
    if parsed.status == 403 and (
        parsed.rate_limit_remaining == 0
        or "rate limit" in (parsed.body or "").lower()
    ):
        raise GHPollError(
            f"GitHub rate-limited (403) for {url}",
            status_code=403,
            looks_like_rate_limit=True,
        )
    if parsed.status == 429:
        raise GHPollError(
            f"GitHub rate-limited (429) for {url}",
            status_code=429,
            looks_like_rate_limit=True,
        )
    # 5xx — not a rate limit, but worth raising so the caller records it.
    if 500 <= parsed.status < 600:
        raise GHPollError(
            f"GitHub returned {parsed.status} for {url}",
            status_code=parsed.status,
        )
    return parsed


# ---------------------------------------------------------------------------
# Poll body — issues + PRs per repo.


def _upsert_gh_pr_state(
    conn: sqlite3.Connection,
    report: PollReport,
    *,
    item_id: str,
    pr_row: dict[str, Any],
) -> None:
    """Fold a PR row into ``state_machine`` + ``item_external_refs``.

    PR lifecycle is simpler than an issue's — ``state`` ∈
    ``{"open", "closed"}`` plus a ``merged`` bool. We map:

    * draft=True → ``pr:draft``
    * open + non-draft → ``pr:open``
    * closed + merged → ``pr:merged``
    * closed + !merged → ``pr:closed``
    """
    state_str = str(pr_row.get("state") or "open").lower()
    merged = bool(pr_row.get("merged"))
    draft = bool(pr_row.get("draft"))
    if state_str == "closed":
        pr_state = "pr:merged" if merged else "pr:closed"
    elif draft:
        pr_state = "pr:draft"
    else:
        pr_state = "pr:open"

    _upsert_state(
        conn,
        _SeedShim(report),  # type: ignore[arg-type]
        item_id=item_id,
        state=pr_state,
        entered_at=str(pr_row.get("updated_at") or ""),
    )

    # Link back to the issue when PR body contains a Closes / Fixes
    # directive that references an issue number. We stay conservative:
    # only ``Closes #NNN`` style on the same repo.
    body = str(pr_row.get("body") or "")
    repo = _repo_from_pr_url(pr_row.get("html_url") or "")
    for m in re.finditer(
        r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", body, flags=re.IGNORECASE
    ):
        if not repo:
            continue
        target = gh_issue_id(repo, int(m.group(1)))
        conn.execute(
            """
            INSERT OR IGNORE INTO item_external_refs
                (item_id, ref_type, ref_target_item_id, ref_source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_id,
                "closes",
                target,
                "pr-body",
                _utc_now_iso(),
            ),
        )


def _repo_from_pr_url(url: str) -> Optional[str]:
    """Extract ``owner/repo`` from ``https://github.com/owner/repo/pull/NNN``."""
    m = re.search(r"github\.com/([\w.\-]+/[\w.\-]+)/(?:pull|issues)/\d+", url)
    return m.group(1) if m else None


class _SeedShim:
    """Trampoline so we can reuse the ``SmSeedReport``-shaped upsert
    helpers from :mod:`alice_state_db.sm_seed` against this module's
    :class:`PollReport`. Only the fields the helpers touch need to
    exist on the attribute surface; the ``PollReport`` happens to share
    the same names.
    """

    __slots__ = ("inner",)

    def __init__(self, report: PollReport) -> None:
        self.inner = report

    # The helpers do ``report.items_upserted += 1`` etc. Delegate.

    @property
    def items_upserted(self) -> int:  # pragma: no cover — property used implicitly
        return self.inner.items_upserted

    @items_upserted.setter
    def items_upserted(self, v: int) -> None:
        self.inner.items_upserted = v

    @property
    def states_upserted(self) -> int:  # pragma: no cover
        return self.inner.states_upserted

    @states_upserted.setter
    def states_upserted(self, v: int) -> None:
        self.inner.states_upserted = v

    @property
    def refs_upserted(self) -> int:  # pragma: no cover
        return 0

    @refs_upserted.setter
    def refs_upserted(self, v: int) -> None:  # pragma: no cover
        pass

    @property
    def transitions_inserted(self) -> int:  # pragma: no cover
        return self.inner.transitions_inserted

    @transitions_inserted.setter
    def transitions_inserted(self, v: int) -> None:
        self.inner.transitions_inserted = v

    @property
    def amendments_inserted(self) -> int:  # pragma: no cover
        return self.inner.amendments_inserted

    @amendments_inserted.setter
    def amendments_inserted(self, v: int) -> None:
        self.inner.amendments_inserted = v


def poll_repo_issues(
    conn: sqlite3.Connection,
    repo: str,
    *,
    token: Optional[str],
    etag: Optional[str],
    runner: Runner,
    report: PollReport,
) -> Optional[str]:
    """Fetch open issues for ``repo``, upsert items + SM state.

    Returns the new ETag to store, or ``None`` when the response was a
    304 (``not_modified``). Raises :class:`GHPollError` on failure.
    """
    url = (
        f"https://api.github.com/repos/{repo}/issues"
        "?state=open&per_page=100&sort=updated&direction=desc"
    )
    report.http_calls += 1
    result = fetch_github(url, token=token, etag=etag, runner=runner)

    if result.status == 304:
        report.not_modified_responses += 1
        return etag  # retain the existing etag

    if result.status != 200 or result.body is None:
        report.errors.append(
            {
                "kind": "issues_fetch",
                "path": repo,
                "reason": f"status={result.status}",
            }
        )
        return result.etag or etag

    try:
        payload = json.loads(result.body)
    except json.JSONDecodeError as exc:
        report.errors.append(
            {
                "kind": "issues_json",
                "path": repo,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        return result.etag or etag

    if not isinstance(payload, list):
        report.errors.append(
            {
                "kind": "issues_shape",
                "path": repo,
                "reason": f"expected list, got {type(payload).__name__}",
            }
        )
        return result.etag or etag

    for issue in payload:
        if not isinstance(issue, dict):
            continue
        # GH's ``/issues`` endpoint returns pull requests inside the
        # issue stream too — identified by the presence of a
        # ``pull_request`` key. Skip them here; PRs are handled in a
        # separate pass via ``/pulls``.
        if "pull_request" in issue:
            continue

        number = issue.get("number")
        if not isinstance(number, int):
            continue
        item_id = gh_issue_id(repo, number)
        title = str(issue.get("title") or "").strip() or f"{repo}#{number}"
        created_at = issue.get("created_at") if isinstance(
            issue.get("created_at"), str
        ) else None
        updated_at = issue.get("updated_at") if isinstance(
            issue.get("updated_at"), str
        ) else None
        user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
        author = user.get("login") if isinstance(user.get("login"), str) else None
        body = issue.get("body") if isinstance(issue.get("body"), str) else ""
        labels_raw = issue.get("labels") or []

        report.issues_seen += 1

        # Upsert item row.
        _upsert_item(
            conn,
            _SeedShim(report),  # type: ignore[arg-type]
            item_id=item_id,
            item_type="gh:issue",
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            author=author,
            summary=(body[:500] if body else None),
            source_path=f"gh:{repo}#{number}",
        )

        # Derive SM state from labels. Falls back to a neutral
        # ``sm:unknown`` when the issue has no sm:* label — don't
        # write ``draft`` as a fallback because that would clobber a
        # real sm:draft set by the dispatcher if it races.
        label_names: list[str] = []
        for label in labels_raw:
            if isinstance(label, str):
                label_names.append(label)
            elif isinstance(label, dict):
                name = label.get("name")
                if isinstance(name, str):
                    label_names.append(name)
        sm_state = _pick_sm_label(label_names) or "sm:unknown"
        _upsert_state(
            conn,
            _SeedShim(report),  # type: ignore[arg-type]
            item_id=item_id,
            state=sm_state,
            entered_at=updated_at,
        )

        # Issue #821: scan ``component:<name>`` labels so the pipeline
        # board has a fallback when no PR is linked yet. No extra GH
        # call — the labels are already in the payload. Uses the same
        # ``touches_component`` ref_type as the PR-files path; the
        # reader picks PR-files over issue-label when both are present.
        _sync_issue_component_labels(
            conn, item_id=item_id, label_names=label_names
        )

    return result.etag or etag


def _sync_issue_component_labels(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    label_names: list[str],
) -> None:
    """Fan out ``component:<name>`` issue labels into ``item_external_refs``.

    One ``INSERT OR IGNORE`` per label, keyed on the composite primary
    key ``(item_id, ref_type, ref_target_item_id)`` so re-polls are
    free. ``ref_source='issue-label'`` is the convention the reader
    uses to know this row came from the label fallback rather than a
    PR-files scan.
    """
    now = _utc_now_iso()
    for name in label_names:
        if not name.startswith("component:"):
            continue
        comp = name[len("component:") :].strip()
        if not comp:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO item_external_refs
                (item_id, ref_type, ref_target_item_id, ref_source, created_at)
            VALUES (?, 'touches_component', ?, 'issue-label', ?)
            """,
            (item_id, f"component:{comp}", now),
        )


def _sync_pr_file_components(
    conn: sqlite3.Connection,
    report: PollReport,
    *,
    repo: str,
    pr_number: int,
    pr_item_id: str,
    closes_targets: list[str],
    token: Optional[str],
    runner: Runner,
) -> None:
    """Fetch ``pulls/<n>/files`` and fan out component tags per issue (#821).

    For each file the PR touches, map the path to a ``src/`` component
    bucket via :func:`component_for_path`. Dedup to a set, then write
    one ``item_external_refs`` row per ``(issue_item_id, component)``
    pair with ``ref_source='pr-files:<pr_item_id>'`` — namespacing the
    source so later cleanup (not implemented here) can
    ``DELETE WHERE ref_source='pr-files:<pr_item_id>'`` before
    re-inserting if a PR's file list shrinks.

    Shares :func:`fetch_github`'s curl/ETag/backoff plumbing so the
    files endpoint counts toward the same rate-limit observation and
    gets the same User-Agent / auth headers as the main poll calls.
    Does NOT cache its own ETag — the caller already gated on
    ``updated_at`` changing, so a second call this cycle would be
    redundant anyway.
    """
    url = (
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
        "?per_page=100"
    )
    report.http_calls += 1
    result = fetch_github(url, token=token, runner=runner)
    if result.status != 200 or result.body is None:
        report.errors.append(
            {
                "kind": "pr_files_fetch",
                "path": f"{repo}#{pr_number}",
                "reason": f"status={result.status}",
            }
        )
        return

    try:
        payload = json.loads(result.body)
    except json.JSONDecodeError as exc:
        report.errors.append(
            {
                "kind": "pr_files_json",
                "path": f"{repo}#{pr_number}",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        return

    if not isinstance(payload, list):
        return

    components: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str):
            continue
        comp = component_for_path(filename)
        if comp:
            components.add(comp)

    if not components:
        return

    now = _utc_now_iso()
    ref_source = f"pr-files:{pr_item_id}"
    for issue_item_id in closes_targets:
        for comp in components:
            # ``WHERE EXISTS`` guards against the FK ``item_external_refs
            # (item_id) REFERENCES items(item_id)`` when the PR's
            # ``Closes #N`` target isn't tracked locally yet (e.g.
            # already-closed issue the poller skipped). FK violations
            # aren't suppressed by ``OR IGNORE``, so this is the way.
            conn.execute(
                """
                INSERT OR IGNORE INTO item_external_refs
                    (item_id, ref_type, ref_target_item_id, ref_source, created_at)
                SELECT ?, 'touches_component', ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM items WHERE item_id = ?)
                """,
                (issue_item_id, f"component:{comp}", ref_source, now, issue_item_id),
            )


def poll_repo_prs(
    conn: sqlite3.Connection,
    repo: str,
    *,
    token: Optional[str],
    etag: Optional[str],
    runner: Runner,
    report: PollReport,
) -> Optional[str]:
    """Fetch open PRs for ``repo`` and upsert ``gh:pr`` items.

    Same return / error contract as :func:`poll_repo_issues`.
    """
    url = (
        f"https://api.github.com/repos/{repo}/pulls"
        "?state=open&per_page=100&sort=updated&direction=desc"
    )
    report.http_calls += 1
    result = fetch_github(url, token=token, etag=etag, runner=runner)

    if result.status == 304:
        report.not_modified_responses += 1
        return etag

    if result.status != 200 or result.body is None:
        report.errors.append(
            {
                "kind": "prs_fetch",
                "path": repo,
                "reason": f"status={result.status}",
            }
        )
        return result.etag or etag

    try:
        payload = json.loads(result.body)
    except json.JSONDecodeError as exc:
        report.errors.append(
            {
                "kind": "prs_json",
                "path": repo,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )
        return result.etag or etag

    if not isinstance(payload, list):
        return result.etag or etag

    for pr in payload:
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        item_id = gh_pr_id(repo, number)
        title = str(pr.get("title") or "").strip() or f"{repo} pr#{number}"
        created_at = pr.get("created_at") if isinstance(
            pr.get("created_at"), str
        ) else None
        updated_at = pr.get("updated_at") if isinstance(
            pr.get("updated_at"), str
        ) else None
        user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        author = user.get("login") if isinstance(user.get("login"), str) else None

        report.prs_seen += 1

        # Capture the stored ``updated_at`` BEFORE the upsert overwrites
        # it — needed to decide whether to re-fetch the PR's file list
        # for the #821 component-derivation cache.
        old_updated_at_row = conn.execute(
            "SELECT updated_at FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        old_updated_at = (
            str(old_updated_at_row[0] or "") if old_updated_at_row else ""
        )

        _upsert_item(
            conn,
            _SeedShim(report),  # type: ignore[arg-type]
            item_id=item_id,
            item_type="gh:pr",
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            author=author,
            source_path=f"gh:pr:{repo}#{number}",
        )

        # PR state is stored as a pr:* pseudo-state rather than one of
        # the sm:* states (PRs don't carry the sm lifecycle; the
        # linked issue does). Readers that want the PR's open/merged
        # bit use this column directly.
        pr_state = pr.get("state")
        merged_at = pr.get("merged_at")
        if pr_state == "closed":
            resolved = "pr:merged" if merged_at else "pr:closed"
        elif pr.get("draft"):
            resolved = "pr:draft"
        else:
            resolved = "pr:open"
        _upsert_state(
            conn,
            _SeedShim(report),  # type: ignore[arg-type]
            item_id=item_id,
            state=resolved,
            entered_at=updated_at,
        )

        # ``Closes #NNN`` scan — link PR → issue. Collect targets as we
        # go so the #821 component-file sync can reuse them.
        body = str(pr.get("body") or "")
        closes_targets: list[str] = []
        for m in re.finditer(
            r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", body, flags=re.IGNORECASE
        ):
            target = gh_issue_id(repo, int(m.group(1)))
            closes_targets.append(target)
            conn.execute(
                """
                INSERT OR IGNORE INTO item_external_refs
                    (item_id, ref_type, ref_target_item_id, ref_source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, "closes", target, "pr-body", _utc_now_iso()),
            )

        # Issue #821: cache PR-file-derived components on the *issue*
        # item_ids this PR closes. Bounded to PRs that both changed
        # since last poll AND carry a ``Closes #N`` directive — so a
        # repo with 100 unrelated PRs costs zero extra HTTP calls.
        if (
            closes_targets
            and resolved in ("pr:open", "pr:draft", "pr:merged")
            and (updated_at or "") != old_updated_at
        ):
            try:
                _sync_pr_file_components(
                    conn,
                    report,
                    repo=repo,
                    pr_number=number,
                    pr_item_id=item_id,
                    closes_targets=closes_targets,
                    token=token,
                    runner=runner,
                )
            except GHPollError as exc:
                # One PR's files endpoint failing must not poison the
                # rest of the poll pass. Rate-limit observations
                # bubble up to the outer loop (which stops the fleet
                # on primary RL); everything else is recorded and we
                # keep going.
                if exc.looks_like_rate_limit:
                    raise
                report.errors.append(
                    {
                        "kind": "pr_files_fetch",
                        "path": f"{repo}#{number}",
                        "reason": str(exc),
                    }
                )

        # Cache the merged_at timestamp (used by done_today readers)
        # as a non-indexed amendment with source='pr-merged-at'. This
        # keeps the merged-at timestamp queryable without adding a
        # new column in Step 2 (schema evolution is Step 5).
        if merged_at and isinstance(merged_at, str):
            conn.execute(
                """
                INSERT OR IGNORE INTO amendments
                    (item_id, author, body, ts, source, source_ref)
                VALUES (?, ?, ?, ?, 'pr-merged-at', ?)
                """,
                (item_id, author, merged_at, merged_at, f"{item_id}@{merged_at}"),
            )

    return result.etag or etag


# ---------------------------------------------------------------------------
# Public entry point.


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_gh_poll(
    mind: pathlib.Path,
    db_path: Optional[pathlib.Path] = None,
    *,
    repos: Optional[list[str]] = None,
    etag_cache_path: Optional[pathlib.Path] = None,
    backoff_path: pathlib.Path = DEFAULT_BACKOFF_PATH,
    env: Optional[dict[str, str]] = None,
    runner: Runner = _default_runner,
) -> PollReport:
    """Run one consolidated poll pass against GitHub.

    Args:
        mind: Root of the ``~/alice-mind`` tree (used to derive the
            default DB path).
        db_path: Override for ``alice-state.db`` location.
        repos: List of ``owner/repo`` strings. Defaults to
            :data:`DEFAULT_REPOS`.
        etag_cache_path: Override for the ETag cache file.
        backoff_path: Override for the shared backoff file (tests).
        env: Environment dict (defaults to :data:`os.environ`).
        runner: Injectable subprocess runner.

    Returns:
        :class:`PollReport` with per-repo counts + status.
    """
    started_mono = time.monotonic()
    started_at = _utc_now_iso()

    mind = pathlib.Path(mind)
    if db_path is None:
        db_path = mind / "inner" / "state" / "alice-state.db"
    if etag_cache_path is None:
        etag_cache_path = mind / "inner" / "state" / "gh-poll-etags.json"
    repos = list(repos or DEFAULT_REPOS)
    env = env if env is not None else dict(os.environ)

    report = PollReport()

    # Pre-flight: if a shared backoff window is active, skip this
    # pass entirely. We don't wait — this poller is a cron / wake
    # tick, and the next tick will try again. Recording ``status =
    # backoff`` in the derive_runs row makes the skip visible.
    try:
        state = read_backoff_state(backoff_path)
        from datetime import datetime as _dt

        if state.backoff_until:
            try:
                until = _dt.fromisoformat(state.backoff_until.replace("Z", "+00:00"))
                if until > _dt.now(timezone.utc):
                    report.status = "backoff"
                    _record_run(
                        db_path, started_at, _utc_now_iso(), "backoff", report
                    )
                    report.duration_ms = int(
                        (time.monotonic() - started_mono) * 1000
                    )
                    return report
            except ValueError:
                pass
    except Exception as exc:  # noqa: BLE001 — telemetry-only path
        report.errors.append(
            {
                "kind": "backoff_read",
                "path": str(backoff_path),
                "reason": f"{type(exc).__name__}: {exc}",
            }
        )

    token = _resolve_token(env, gh_token_runner=runner)
    cache = _read_etag_cache(etag_cache_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)

        for repo in repos:
            report.repos_polled += 1

            for endpoint, fn in (
                ("issues", poll_repo_issues),
                ("pulls", poll_repo_prs),
            ):
                key = f"{repo}:{endpoint}"
                etag = cache.get(key)
                try:
                    new_etag = fn(
                        conn,
                        repo,
                        token=token,
                        etag=etag,
                        runner=runner,
                        report=report,
                    )
                except GHPollError as exc:
                    if exc.looks_like_rate_limit:
                        report.rate_limit_hits += 1
                        # Ripple the hit out to every other gh-caller.
                        try:
                            observe_gh_error(exc, path=backoff_path)
                        except Exception as inner:  # noqa: BLE001
                            report.errors.append(
                                {
                                    "kind": "backoff_write",
                                    "path": str(backoff_path),
                                    "reason": f"{type(inner).__name__}: {inner}",
                                }
                            )
                        report.status = "rate_limited"
                        # Stop polling further repos / endpoints — the
                        # window belongs to the whole fleet now.
                        conn.commit()
                        _record_run(
                            db_path,
                            started_at,
                            _utc_now_iso(),
                            report.status,
                            report,
                        )
                        _write_etag_cache(etag_cache_path, cache)
                        report.duration_ms = int(
                            (time.monotonic() - started_mono) * 1000
                        )
                        return report
                    report.errors.append(
                        {
                            "kind": f"{endpoint}_error",
                            "path": repo,
                            "reason": str(exc),
                        }
                    )
                    continue

                if new_etag:
                    cache[key] = new_etag

        conn.commit()
    finally:
        conn.close()

    # Persist the ETag cache before recording the run row.
    _write_etag_cache(etag_cache_path, cache)

    if report.errors and not report.items_upserted:
        report.status = "error"

    _record_run(db_path, started_at, _utc_now_iso(), report.status, report)
    report.duration_ms = int((time.monotonic() - started_mono) * 1000)
    return report


def _record_run(
    db_path: pathlib.Path,
    started_at: str,
    finished_at: str,
    status: str,
    report: PollReport,
) -> None:
    """Append a ``derive_runs`` row for this poll pass.

    Separate connection so we can fire it even on the early-exit paths
    (backoff / rate-limited); the main connection may already be
    closed when this runs.
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return
    try:
        apply_schema(conn)
        conn.execute(
            """
            INSERT INTO derive_runs (
                step, started_at, finished_at, rows_touched,
                tasks_upserted, workers_upserted, events_appended,
                last_event_offset, errors_json
            ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
            """,
            (
                f"derive_gh_poll:{status}",
                started_at,
                finished_at,
                report.items_upserted
                + report.states_upserted
                + report.transitions_inserted
                + report.amendments_inserted,
                json.dumps(report.errors),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        # Non-fatal — the poll ran; losing a telemetry row is better
        # than crashing the memory-worker wake.
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass


def main(argv: Optional[list[str]] = None) -> int:
    """One-shot CLI runner: ``python -m alice_state_db.gh_poll``.

    Convenient for s6 timer services and operator-triggered refreshes.
    Prints the :class:`PollReport` as JSON on stdout.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m alice_state_db.gh_poll",
        description=(
            "Poll GitHub issues + PRs for configured repos and upsert "
            "into alice-state.db. Honors the shared rate-limit backoff."
        ),
    )
    parser.add_argument(
        "--mind",
        default=str(pathlib.Path.home() / "alice-mind"),
        help="alice-mind root (default: ~/alice-mind)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help=(
            "Repo to poll as <org>/<name>. Repeatable; "
            f"defaults to {', '.join(DEFAULT_REPOS)}."
        ),
    )
    args = parser.parse_args(argv)
    mind = pathlib.Path(args.mind)
    repos = args.repos or list(DEFAULT_REPOS)
    report = derive_gh_poll(mind, repos=repos)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.status != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
