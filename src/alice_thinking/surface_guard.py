"""Surface guard — validate flash-surface claims before Thinking fires them.

Thinking's sandbox is ``~/alice-mind/``. She cannot see ``alice-speaking/``,
``cozyhem/``, or live GitHub state beyond what's in the vault. When writing a
``priority: flash`` surface, she may reference files or state she can't
verify — producing false alarms (precedent: 2026-05-18 missed-reply-gh-push).

This module provides :func:`should_fire`, a one-liner gate the surface writer
calls before emitting a flash surface. On any unverifiable claim it returns
``(False, reason)`` so the caller can downgrade to ``priority: insight``.
Insight-tier surfaces pass through unguarded.

Resolves design from issue #242. See
``cortex-memory/research/2026-05-18-surface-guard-design.md``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

SANDBOX = Path.home() / "alice-mind"

# Module-level memo for gh-backed checks. Keyed by (check_type, repo, ...).
# Cleared explicitly via ``_reset_cache`` from tests; in production the
# module is re-imported once per wake so the TTL is effectively wake-scoped.
_gh_cache: dict[tuple, Any] = {}


def _reset_cache() -> None:
    """Test helper — drop the gh memo between cases."""
    _gh_cache.clear()


def should_fire(
    target: str,
    evidence: Any,
    priority: str = "flash",
) -> tuple[bool, str]:
    """Validate that a surface's claims are checkable from the sandbox.

    Parameters
    ----------
    target
        What the surface is about (e.g. ``"file-issue"``, ``"check-state"``).
        Currently informational — included in reasons for grep-ability.
    evidence
        ``dict`` shorthand ``{claim_label: path}`` (all entries treated as
        ``file_exists`` checks), or ``list[dict]`` of structured checks where
        each dict has a ``type`` key (``file_exists`` / ``gh_issue_state`` /
        ``gh_branch_exists`` / ``daily_contains``).
    priority
        ``"flash"`` enforces the guard. Anything else (``"insight"``,
        ``"routine"``, ...) passes through.

    Returns
    -------
    (can_fire, reason)
        ``can_fire=True`` when all evidence is verifiable. ``False`` on the
        first failing check, with a reason naming the offending claim.
    """
    if priority != "flash":
        return True, "non-flash surface — no guard needed"

    if isinstance(evidence, dict):
        checks = [
            {"type": "file_exists", "path": v, "claim": k}
            for k, v in evidence.items()
        ]
    elif isinstance(evidence, list):
        checks = list(evidence)
    else:
        return False, f"evidence must be dict or list, got {type(evidence).__name__}"

    if not checks:
        # A flash surface with no evidence cannot be verified — fail closed.
        return False, "flash surface has no evidence to verify"

    for check in checks:
        check_type = check.get("type", "file_exists")

        if check_type == "file_exists":
            raw = check.get("path")
            if raw is None:
                return False, "file_exists check missing 'path'"
            path = Path(raw)
            if not path.is_absolute():
                path = SANDBOX / path
            else:
                # Reject absolute paths that escape the sandbox — Thinking
                # can't observe anything outside ``~/alice-mind/``, so a
                # claim about /home/alice/foo or /etc/whatever is by
                # definition unverifiable from her vantage point.
                try:
                    path.resolve().relative_to(SANDBOX.resolve())
                except ValueError:
                    return (
                        False,
                        f"file claim '{check.get('claim', raw)}': "
                        f"{path} is outside sandbox ({SANDBOX})",
                    )
            if not path.exists():
                return (
                    False,
                    f"file claim '{check.get('claim', raw)}': "
                    f"{path} does not exist in sandbox",
                )

        elif check_type == "gh_issue_state":
            repo = check.get("repo")
            numbers = check.get("numbers", [])
            if not repo:
                return False, "gh_issue_state check missing 'repo'"
            cache_key = ("gh_issue_state", repo)
            existing = _gh_cache.get(cache_key)
            if existing is None:
                try:
                    result = subprocess.run(
                        [
                            "gh", "issue", "list",
                            "--repo", repo,
                            "--state", "all",
                            "--json", "number,state",
                            "--limit", "500",
                        ],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode != 0:
                        return (
                            False,
                            f"gh_issue_state: gh exited {result.returncode}: "
                            f"{result.stderr.strip()[:200]}",
                        )
                    existing = {i["number"] for i in json.loads(result.stdout)}
                    _gh_cache[cache_key] = existing
                except Exception as exc:  # noqa: BLE001 — fail closed
                    return False, f"gh_issue_state: github unavailable — {exc}"
            missing = [n for n in numbers if n not in existing]
            if missing:
                return False, f"gh_issue_state: issues {missing} not found in {repo}"

        elif check_type == "gh_branch_exists":
            repo = check.get("repo")
            branch = check.get("branch")
            if not repo or not branch:
                return False, "gh_branch_exists check missing 'repo' or 'branch'"
            cache_key = ("gh_branch_exists", repo, branch)
            cached = _gh_cache.get(cache_key)
            if cached is None:
                try:
                    result = subprocess.run(
                        ["gh", "api", f"repos/{repo}/branches/{branch}"],
                        capture_output=True, timeout=10,
                    )
                    cached = result.returncode == 0
                    _gh_cache[cache_key] = cached
                except Exception as exc:  # noqa: BLE001 — fail closed
                    return False, f"gh_branch_exists: github unavailable — {exc}"
            if not cached:
                return False, f"gh_branch_exists: branch {branch} not found in {repo}"

        elif check_type == "daily_contains":
            from datetime import date
            today = date.today().strftime("%Y-%m-%d")
            daily = SANDBOX / "cortex-memory" / "dailies" / f"{today}.md"
            keyword = check.get("keyword", "")
            if not keyword:
                return False, "daily_contains check missing 'keyword'"
            if not daily.exists() or keyword not in daily.read_text():
                return False, f"daily_contains: '{keyword}' not in today's daily"

        else:
            return False, f"unknown check type: {check_type!r}"

    return True, "all claims verifiable"


__all__ = ["should_fire", "SANDBOX"]
