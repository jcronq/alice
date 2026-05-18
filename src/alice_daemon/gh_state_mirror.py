#!/usr/bin/env python3
"""
GitHub state mirror — cron job that polls GitHub and writes thin notes
into cortex-memory/gh-state/ to give thinking visibility into in-flight PRs.

Runs every 15 minutes. Needs `gh` CLI authenticated (via `gh auth login`
or a GITHUB_TOKEN env var).

Tracked repos: cozyhem-engine, alice (configurable).
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────
REPOS = ["jcronq/cozyhem-engine", "jcronq/alice"]
ALICE_MIND = os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind"))
GH_STATE_DIR = Path(ALICE_MIND) / "cortex-memory" / "gh-state"
LOG_FILE = Path(ALICE_MIND) / "inner" / "state" / "gh-state-mirror.log"
# ──────────────────────────────────────────────────────────────────────


def gh(*args: str) -> str:
    """Run gh CLI and return stdout."""
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def write_note_atomic(repo: str, number: int, item: dict) -> None:
    """Write or update a thin state note. Uses temp-file + rename for atomicity."""
    note_path = GH_STATE_DIR / f"{repo}-{number}.md"
    now = datetime.now(timezone.utc).isoformat()

    if item.get("_type") == "issue":
        state = item.get("state", "open")
        title = item.get("title", "").strip()
        content = (
            f'---\n'
            f'slug: gh-state-{repo}-{number}\n'
            f'title: {repo}#{number} — {title}\n'
            f'tags: [gh-state]\n'
            f'note_type: gh-state\n'
            f'repo: {repo}\n'
            f'number: {number}\n'
            f'type: issue\n'
            f'state: {state}\n'
            f'updated_at: "{now}"\n'
            f'---\n\n'
            f'# {repo}#{number} — {title}\n\n'
            f"Issue {state}. Created {item.get('createdAt', 'unknown')}. "
            f"Last updated {item.get('updatedAt', 'unknown')}.\n"
        )
    else:  # PR
        state = item.get("state", "open")
        merged = item.get("merged", False)
        draft = item.get("isDraft", False)
        base_branch = item.get("baseRefName", "unknown")
        title = item.get("title", "").strip()
        draft_label = "Draft " if draft else ""
        content = (
            f'---\n'
            f'slug: gh-state-{repo}-{number}\n'
            f'title: {repo}#{number} — {title}\n'
            f'tags: [gh-state]\n'
            f'note_type: gh-state\n'
            f'repo: {repo}\n'
            f'number: {number}\n'
            f'type: pr\n'
            f'state: {state}\n'
            f'merged: {str(merged).lower()}\n'
            f'draft: {str(draft).lower()}\n'
            f'base_branch: {base_branch}\n'
            f'updated_at: "{now}"\n'
            f'---\n\n'
            f'# {repo}#{number} — {title}\n\n'
            f"{draft_label}PR #{state}. Base: `{base_branch}`. "
            f"Merged: {str(merged).lower()}.\n"
        )

    GH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=GH_STATE_DIR, suffix=".tmp")
    try:
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.replace(tmp_path, note_path)  # atomic on same filesystem
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    os.makedirs(GH_STATE_DIR, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    created = 0
    updated = 0
    removed = 0
    errors = 0

    for repo in REPOS:
        # Fetch open issues
        try:
            issues_json = gh(
                "issue", "list", "--repo", repo, "--state", "open",
                "--json", "number,title,state,createdAt,updatedAt"
            )
            issues = json.loads(issues_json)
        except (RuntimeError, json.JSONDecodeError) as e:
            _log(f"[{_ts()}] ERROR fetching issues for {repo}: {e}")
            errors += 1
            continue

        for issue in issues:
            number = issue["number"]
            note_path = GH_STATE_DIR / f"{repo}-{number}.md"
            if note_path.exists():
                updated += 1
            else:
                created += 1
            write_note_atomic(repo, number, {**issue, "_type": "issue"})

        # Fetch open PRs
        try:
            prs_json = gh(
                "pr", "list", "--repo", repo, "--state", "open",
                "--json", "number,title,state,isDraft,merged,baseRefName,updatedAt,createdAt"
            )
            prs = json.loads(prs_json)
        except (RuntimeError, json.JSONDecodeError) as e:
            _log(f"[{_ts()}] ERROR fetching PRs for {repo}: {e}")
            errors += 1
            continue

        for pr in prs:
            number = pr["number"]
            note_path = GH_STATE_DIR / f"{repo}-{number}.md"
            if note_path.exists():
                updated += 1
            else:
                created += 1
            write_note_atomic(repo, number, {**pr, "_type": "pr"})

        # Cleanup: remove notes for closed issues/PRs
        # Strategy: check what's currently open via API, compare to what's on disk.
        # This is cheaper than calling `gh issue/PR view` per file.
        try:
            # Build set of open issue numbers
            open_issues = {i["number"] for i in issues}
            open_prs = {p["number"] for p in prs}

            for note_path in GH_STATE_DIR.glob(f"{repo}-*.md"):
                if not note_path.exists():
                    continue
                stem = note_path.stem  # e.g. "cozyhem-engine-227"
                parts = stem.rsplit("-", 1)
                if len(parts) != 2:
                    # Can't parse — orphan, remove it
                    removed += 1
                    note_path.unlink()
                    continue
                try:
                    num = int(parts[-1])
                except ValueError:
                    removed += 1
                    note_path.unlink()
                    continue

                try:
                    with open(note_path) as f:
                        content = f.read()
                except FileNotFoundError:
                    continue

                if "type: issue" in content:
                    if num not in open_issues:
                        removed += 1
                        note_path.unlink()
                elif "type: pr" in content:
                    if num not in open_prs:
                        removed += 1
                        note_path.unlink()

        except Exception as e:
            _log(f"[{_ts()}] ERROR during cleanup for {repo}: {e}")
            errors += 1

    timestamp = _ts()
    summary = f"[{timestamp}] Mirror run: created={created}, updated={updated}, removed={removed}, errors={errors}\n"
    _log(summary)
    print(summary.strip(), file=sys.stderr)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(msg)


if __name__ == "__main__":
    main()
