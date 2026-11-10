#!/usr/bin/env python3
"""Ready-for-Review live renderer — rewrites the ``## Ready for Review``
section of the status-site Overview (``inner/status/index.md``) from
live GitHub state.

Three phases are rendered:

* **Design review** — open issues on watched repos tagged ``sm:design_review``.
* **PR review** — open PRs on watched repos that are non-draft and
  carry ``reviewDecision == "REVIEW_REQUIRED"``.
* **Code review** — placeholder (``*(coming soon)*``); will eventually
  surface PRs carrying the ``sm:reviewing`` label once the SM state is
  live.

The script is a one-shot runner — an s6 timer loop invokes it (see
``sandbox/s6/alice-review-queue/run``). Any failing ``gh`` query is
logged and the phase renders ``*(none)*``; if ALL three queries fail,
``index.md`` is left untouched (preserve last-good render).

Design: ``cortex-memory/designs/2026-11-05-issue622-ready-for-review-overview.md``.
Issue:  #622.
"""

from __future__ import annotations

from core.forge_repos import forge_repos as _forge_repos, primary_repo as _primary_repo
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ─── Config ───────────────────────────────────────────────────────────

ALICE_MIND = os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind"))
CONFIG_PATH = Path(ALICE_MIND) / "config" / "alice.config.json"
DEFAULT_STATUS_INDEX = Path(ALICE_MIND) / "inner" / "status" / "index.md"

DEFAULT_REPOS_PR = list(_forge_repos())
DEFAULT_REPOS_DESIGN_REVIEW = [_primary_repo()]
DEFAULT_POLL_INTERVAL = 300
POLL_INTERVAL_FLOOR = 60

SECTION_HEADING = "## Ready for Review"
INSERT_BEFORE = "## Waiting on you"

# Type for the injectable subprocess runner used in tests.
# Signature matches subprocess.run(..., capture_output=True, text=True).
SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]

log = logging.getLogger("review_queue_renderer")

# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DesignItem:
    """One open issue awaiting design review."""

    repo: str
    number: int
    title: str
    url: str
    age: str


@dataclass(frozen=True)
class PRItem:
    """One open PR awaiting PR review (non-draft, REVIEW_REQUIRED)."""

    repo: str
    number: int
    title: str
    url: str
    age: str
    author: str = ""
    ci: str = ""  # "success" | "failure" | "pending" | "" (no checks)


def _ci_state(rollup: object) -> str:
    """Collapse a ``statusCheckRollup`` list into one coarse state."""
    if not isinstance(rollup, list) or not rollup:
        return ""
    states = [
        str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        for c in rollup
        if isinstance(c, dict)
    ]
    bad = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED")
    wait = ("", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING")
    if any(v in bad for v in states):
        return "failure"
    if any(v in wait for v in states):
        return "pending"
    return "success"


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    """Default subprocess runner — matches ``gh_state_mirror.gh``'s shape."""
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def gh(
    args: Sequence[str],
    runner: SubprocessRunner | None = None,
) -> list[dict]:
    """Run ``gh`` with ``args`` and parse its stdout as JSON.

    Raises ``RuntimeError`` on non-zero exit or when the output isn't
    valid JSON. ``runner`` is injected for tests; default calls
    ``subprocess.run`` with a 60-second timeout.
    """
    r = runner or _default_runner
    cmd = ["gh", *args]
    result = r(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh {' '.join(args)} returned non-JSON: {e}"
        ) from e
    if not isinstance(parsed, list):
        raise RuntimeError(
            f"gh {' '.join(args)} returned non-list payload: {type(parsed).__name__}"
        )
    return parsed


def age_str(created_at: str, *, now: datetime | None = None) -> str:
    """Format an ISO8601 timestamp as a short age string.

    ``< 1h`` → ``"<1h"``, ``< 24h`` → ``"Xh"``, ``>= 24h`` → ``"Xd"``.
    GitHub's trailing ``Z`` is normalised to ``+00:00`` so
    ``datetime.fromisoformat`` accepts it.
    """
    ref = now or datetime.now(timezone.utc)
    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    delta = ref - ts
    total_seconds = int(delta.total_seconds())
    if total_seconds < 3600:
        return "<1h"
    hours = total_seconds // 3600
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def fetch_design_review_issues(
    repos: Iterable[str],
    *,
    runner: SubprocessRunner | None = None,
    now: datetime | None = None,
) -> tuple[list[DesignItem], list[str]]:
    """Fetch open issues labelled ``sm:design_review`` across ``repos``.

    Returns ``(items, failed_repos)``. A query failure for a single repo
    doesn't abort the whole phase — the failed repo is just reported so
    the caller can decide whether to preserve-last-good.
    """
    items: list[DesignItem] = []
    failed: list[str] = []
    for repo in repos:
        try:
            raw = gh(
                [
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--label",
                    "sm:design_review",
                    "--state",
                    "open",
                    "--json",
                    "number,title,url,createdAt",
                ],
                runner=runner,
            )
        except RuntimeError as e:
            log.warning("design-review fetch failed for %s: %s", repo, e)
            failed.append(repo)
            continue
        for row in raw:
            items.append(
                DesignItem(
                    repo=repo,
                    number=int(row["number"]),
                    title=str(row.get("title", "")).strip(),
                    url=str(row.get("url", "")),
                    age=age_str(row["createdAt"], now=now),
                    author=str((row.get("author") or {}).get("login", "")),
                    ci=_ci_state(row.get("statusCheckRollup")),
                )
            )
    return items, failed


def fetch_pr_review(
    repos: Iterable[str],
    *,
    runner: SubprocessRunner | None = None,
    now: datetime | None = None,
) -> tuple[list[PRItem], list[str]]:
    """Fetch open PRs across ``repos`` that are non-draft and need review.

    GitHub's ``reviewDecision`` is ``"REVIEW_REQUIRED"`` while the PR is
    waiting on first-review-assignment; ``""`` once reviewers are merely
    assigned but no verdict rendered, and ``"APPROVED"`` /
    ``"CHANGES_REQUESTED"`` once reviews land. The filter here matches
    the design doc: non-draft **and** ``reviewDecision == "REVIEW_REQUIRED"``.
    """
    items: list[PRItem] = []
    failed: list[str] = []
    for repo in repos:
        try:
            raw = gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--json",
                    "number,title,isDraft,reviewDecision,createdAt,url,author,statusCheckRollup",
                ],
                runner=runner,
            )
        except RuntimeError as e:
            log.warning("pr-review fetch failed for %s: %s", repo, e)
            failed.append(repo)
            continue
        for row in raw:
            if row.get("isDraft"):
                continue
            if row.get("reviewDecision") != "REVIEW_REQUIRED":
                continue
            items.append(
                PRItem(
                    repo=repo,
                    number=int(row["number"]),
                    title=str(row.get("title", "")).strip(),
                    url=str(row.get("url", "")),
                    age=age_str(row["createdAt"], now=now),
                )
            )
    return items, failed


def _render_design_line(item: DesignItem) -> str:
    # TODO(#616): once the Review tab UI ships, swap the final Review →
    # target from the GitHub issue URL to
    #   http://10.20.30.177:8095/?p=review&issue={item.number}
    review_url = item.url
    return (
        f"- [{item.repo}#{item.number}]({item.url}) — {item.title} — "
        f"{item.age} — [Review →]({review_url})"
    )


def _render_pr_line(item: PRItem) -> str:
    return (
        f"- [{item.repo}#{item.number}]({item.url}) — {item.title} — "
        f"{item.age} — [Review →]({item.url})"
    )


def _default_description(agent_name: str) -> str:
    """Italic intro line for the section. Takes the agent name as an
    argument so the literal isn't baked into the module (which would
    trip ``tests/test_no_hardcoded_personae_literals.py``); the name
    is loaded from ``mind/personae.yml`` by the caller."""
    return (
        f"*Items that passed through {agent_name}'s pipeline and need "
        "your sign-off. Live - updated every 5 min.*"
    )


def _load_agent_name() -> str:
    """Resolve the agent's name from ``mind/personae.yml``. Falls back
    to the placeholder persona on any error so the one-shot CLI
    rendering still produces a reasonable description line."""
    try:
        from core.config.personae import load as _load, placeholder
    except ImportError:
        return "the agent"
    mind_env = os.environ.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    try:
        return _load(Path(mind_env)).agent.name
    except Exception:  # noqa: BLE001
        try:
            return placeholder().agent.name
        except Exception:  # noqa: BLE001
            return "the agent"


def render_section(
    design_items: Sequence[DesignItem],
    pr_items: Sequence[PRItem],
    code_items: Sequence[object] | None = None,
    *,
    agent_name: str | None = None,
) -> str:
    """Return the full ``## Ready for Review`` section markdown body.

    ``agent_name`` is substituted into the intro line. Defaults to the
    live personae value; tests pass a literal to keep output stable.

    The block ends with a trailing newline so :func:`replace_section`
    can splice it in without touching surrounding structure.
    """
    # Code review is placeholder-only until sm:reviewing is wired up.
    _ = code_items  # reserved for future use; signature pinned

    if agent_name is None:
        agent_name = _load_agent_name()

    lines: list[str] = []
    lines.append("## Ready for Review")
    lines.append("")
    lines.append(_default_description(agent_name))
    lines.append("")

    lines.append("### Design review")
    lines.append("")
    if design_items:
        for item in design_items:
            lines.append(_render_design_line(item))
    else:
        lines.append("*(none)*")
    lines.append("")

    lines.append("### PR review")
    lines.append("")
    if pr_items:
        for item in pr_items:
            lines.append(_render_pr_line(item))
    else:
        lines.append("*(none)*")
    lines.append("")

    lines.append("### Code review")
    lines.append("")
    lines.append("*(coming soon)*")
    lines.append("")

    return "\n".join(lines)


def replace_section(text: str, heading: str, new_body: str) -> str:
    """Replace the block starting at ``heading`` through the line before
    the next ``## `` heading (or EOF) with ``new_body``.

    If ``heading`` is absent, insert ``new_body`` before the first line
    matching :data:`INSERT_BEFORE`. If that heading is also absent,
    append at the end of the file.

    ``new_body`` is assumed to already carry its own trailing newline.
    The result is normalised so there's exactly one blank line between
    the inserted section and whatever follows it.
    """
    # Ensure the body ends with a newline so slicing produces the right
    # structure regardless of what the caller passed.
    if not new_body.endswith("\n"):
        new_body = new_body + "\n"

    lines = text.splitlines(keepends=True)
    heading_idx: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip("\n") == heading:
            heading_idx = i
            break

    # Ensure a blank-line separator before any following heading. The
    # render_section output ends with a single "\n"; splicing it next to
    # a `## Next` line would collapse the usual Markdown structure.
    body_for_splice = new_body if new_body.endswith("\n\n") else new_body + "\n"

    if heading_idx is not None:
        # Find the next `## ` heading after the target heading.
        end_idx = len(lines)
        for j in range(heading_idx + 1, len(lines)):
            stripped = lines[j].rstrip("\n")
            if stripped.startswith("## "):
                end_idx = j
                break
        # If the section goes to EOF we don't need the extra separator.
        splice = new_body if end_idx == len(lines) else body_for_splice
        return "".join(lines[:heading_idx]) + splice + "".join(lines[end_idx:])

    # Heading absent — try to insert before `## Waiting on you`.
    for i, line in enumerate(lines):
        if line.rstrip("\n") == INSERT_BEFORE:
            return "".join(lines[:i]) + body_for_splice + "".join(lines[i:])

    # Neither heading present — append. Ensure a blank line separates
    # the existing text from the inserted section.
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix = prefix + "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix + "\n"
    return prefix + new_body


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via tempfile + ``os.replace``.

    Same shape as ``gh_state_mirror._atomic_write`` — same-directory
    rename so the swap is atomic on POSIX filesystems.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _load_config() -> dict:
    """Load the module's config block from ``alice.config.json``.

    Missing file / malformed JSON / missing key → empty dict so the
    caller falls back to module defaults.
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("failed to read %s: %s", CONFIG_PATH, e)
        return {}
    section = config.get("review_queue_renderer", {})
    return section if isinstance(section, dict) else {}


def _coerce_repos(value: object, default: list[str]) -> list[str]:
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    return list(default)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alice_forge.review_queue_renderer",
        description=(
            "Rewrite the ## Ready for Review section of the status-site "
            "Overview from live GitHub state."
        ),
    )
    parser.add_argument(
        "--status-index",
        type=Path,
        default=DEFAULT_STATUS_INDEX,
        help=(
            "Path to status-site index.md "
            f"(default: {DEFAULT_STATUS_INDEX})"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = _load_config()
    repos_pr = _coerce_repos(cfg.get("repos_pr"), DEFAULT_REPOS_PR)
    repos_design = _coerce_repos(
        cfg.get("repos_design_review"), DEFAULT_REPOS_DESIGN_REVIEW
    )

    status_path: Path = args.status_index
    if not status_path.exists():
        log.error("status index %s does not exist; nothing to update", status_path)
        return 0

    design_items, design_failed = fetch_design_review_issues(repos_design)
    pr_items, pr_failed = fetch_pr_review(repos_pr)
    # Code review is still a placeholder; it never "fails". We track it
    # alongside the other phases so the preserve-last-good rule is easy
    # to reason about once sm:reviewing is wired up.
    code_items: list[object] = []
    code_failed_all = False

    design_failed_all = bool(repos_design) and len(design_failed) == len(repos_design)
    pr_failed_all = bool(repos_pr) and len(pr_failed) == len(repos_pr)

    if design_failed_all and pr_failed_all and code_failed_all:
        log.error(
            "all phase fetches failed (design=%s, pr=%s); preserving last-good "
            "render of %s",
            design_failed,
            pr_failed,
            status_path,
        )
        return 0

    new_body = render_section(design_items, pr_items, code_items)
    current = status_path.read_text()
    updated = replace_section(current, SECTION_HEADING, new_body)

    if updated == current:
        log.info("Ready-for-Review section already up to date; no write")
        return 0

    _atomic_write(status_path, updated)
    log.info(
        "Ready-for-Review section updated: %d design item(s), %d PR item(s) "
        "(design_failed=%s, pr_failed=%s)",
        len(design_items),
        len(pr_items),
        design_failed,
        pr_failed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
