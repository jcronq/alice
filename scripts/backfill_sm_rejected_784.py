#!/usr/bin/env python3
"""Backfill script for issue #784 Part B.

Audits ``sm:rejected`` issues closed in the last 2 weeks and re-labels
any whose linked PR actually merged. The pre-#784 Phase 1.6 sweeper
flipped these to ``sm:rejected`` when master CI was red at merge time,
silently killing completed work (see #613, #614, #616, #617, #618,
#620, #621, #656 for the observed pattern).

Usage::

    # Dry-run (default): print what would change, don't label.
    python scripts/backfill_sm_rejected_784.py

    # Act for real.
    python scripts/backfill_sm_rejected_784.py --apply

    # Different repo / lookback window.
    python scripts/backfill_sm_rejected_784.py \\
        --repo jcronq/cozyhem-engine --lookback-days 7

Exit codes:
    0  completed (zero or more labels re-written)
    1  gh CLI failed or couldn't parse a response

Idempotent — a re-run on already-corrected issues finds nothing to do.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


log = logging.getLogger("backfill_sm_rejected_784")


DEFAULT_REPO = "jcronq/alice"
DEFAULT_LOOKBACK_DAYS = 14


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Invoke ``gh`` with ``args`` and return stdout. Raises on error."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def list_rejected_issues(
    repo: str,
    *,
    since: datetime,
    runner: Callable[[list[str]], str] = _run_gh,
) -> list[dict[str, Any]]:
    """Return closed issues labelled ``sm:rejected`` updated since ``since``.

    Uses ``gh issue list --search`` so GitHub does the filtering.
    """
    iso_since = since.strftime("%Y-%m-%d")
    args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "closed",
        "--search",
        f"is:issue label:sm:rejected closed:>={iso_since}",
        "--json",
        "number,title,labels,closedAt,url",
        "--limit",
        "200",
    ]
    stdout = runner(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return []
    return payload


def find_merged_pr_for_issue(
    repo: str,
    issue_number: int,
    *,
    runner: Callable[[list[str]], str] = _run_gh,
) -> dict[str, Any] | None:
    """Return the first MERGED PR that references ``issue_number``, or None.

    Uses ``gh pr list --search "linked:issue"`` to enumerate PRs with a
    "Closes #N" linkage; then filters client-side by
    ``closingIssuesReferences``.
    """
    args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--search",
        "linked:issue",
        "--json",
        "number,url,state,mergedAt,closingIssuesReferences",
        "--limit",
        "200",
    ]
    stdout = runner(args)
    if not stdout.strip():
        return None
    payload = json.loads(stdout)
    if not isinstance(payload, list):
        return None
    for pr in payload:
        if (pr.get("state") or "").upper() != "MERGED":
            continue
        refs = pr.get("closingIssuesReferences") or []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("number") == issue_number:
                return pr
    return None


def relabel_issue(
    repo: str,
    issue_number: int,
    *,
    merged_pr_number: int,
    runner: Callable[[list[str]], str] = _run_gh,
) -> None:
    """Flip ``sm:rejected`` → ``sm:done`` on one issue and post an audit note."""
    runner(
        [
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--add-label",
            "sm:done",
            "--remove-label",
            "sm:rejected",
        ]
    )
    body = (
        f"[SM] backfill #784: PR #{merged_pr_number} actually merged; "
        "re-labelling sm:rejected → sm:done. The pre-#784 Phase 1.6 "
        "sweeper flipped this to sm:rejected when master CI was red "
        "at merge time, which the fix in this PR corrects. See "
        "jcronq/alice#784 for the full rollout.\n"
    )
    runner(
        [
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo,
            "--body",
            body,
        ]
    )


def _label_names(issue: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in issue.get("labels") or []:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                out.add(name)
        elif isinstance(entry, str):
            out.add(entry)
    return out


def run_backfill(
    repo: str,
    *,
    lookback_days: int,
    apply: bool,
    runner: Callable[[list[str]], str] = _run_gh,
    now: datetime | None = None,
) -> dict[str, int]:
    """Audit + (optionally) relabel. Returns counters dict."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)
    issues = list_rejected_issues(repo, since=since, runner=runner)
    counters = {"scanned": len(issues), "misrejected": 0, "relabelled": 0, "skipped": 0}

    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            counters["skipped"] += 1
            continue
        labels = _label_names(issue)
        # Belt-and-suspenders: if sm:done is already there for some
        # reason (manual fixup), skip.
        if "sm:done" in labels:
            log.info(
                "#%d: already carries sm:done alongside sm:rejected — "
                "manual intervention, skipping",
                number,
            )
            counters["skipped"] += 1
            continue
        pr = find_merged_pr_for_issue(repo, number, runner=runner)
        if pr is None:
            # Correctly rejected: no merged PR for this issue.
            continue
        pr_number = pr.get("number")
        if not isinstance(pr_number, int):
            counters["skipped"] += 1
            continue
        counters["misrejected"] += 1
        log.info(
            "#%d: mis-labelled sm:rejected (PR #%d merged at %s) — "
            "%s re-label",
            number,
            pr_number,
            pr.get("mergedAt") or "<unknown>",
            "APPLYING" if apply else "DRY-RUN would",
        )
        if apply:
            try:
                relabel_issue(repo, number, merged_pr_number=pr_number, runner=runner)
                counters["relabelled"] += 1
            except RuntimeError as exc:
                log.error("#%d: re-label failed: %s", number, exc)
                counters["skipped"] += 1
    return counters


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_sm_rejected_784",
        description=(
            "Backfill sm:rejected issues mis-labelled by the pre-#784 "
            "Phase 1.6 sweeper (master CI red → sm:rejected even though "
            "the PR merged)."
        ),
    )
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="Days back to scan for closed sm:rejected issues (default 14).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually re-label mis-rejected issues. Default is dry-run "
            "which only prints what would change."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    try:
        counters = run_backfill(
            args.repo,
            lookback_days=args.lookback_days,
            apply=args.apply,
        )
    except (RuntimeError, json.JSONDecodeError) as exc:
        log.error("backfill failed: %s", exc)
        return 1
    log.info(
        "backfill complete (%s): %s",
        "APPLIED" if args.apply else "DRY-RUN",
        json.dumps(counters, sort_keys=True),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
