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
from datetime import datetime, timedelta, timezone
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


def _atomic_write(note_path: Path, content: str) -> None:
    """Write `content` to `note_path` via tempfile + rename (atomic on same fs).

    The tempfile is created next to the destination so ``os.replace`` is
    a same-directory rename (atomic on POSIX). When ``repo`` contains a
    slash (``"jcronq/alice"``), the per-owner subdirectory under
    ``GH_STATE_DIR`` is created on demand so the rename target's parent
    always exists.
    """
    GH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=note_path.parent, suffix=".tmp")
    try:
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.replace(tmp_path, note_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def write_deferred(
    repo: str,
    number: int,
    reason: str,
    deferred_by: str,
    title: str = "",
) -> Path:
    """Write a ``type: deferred`` gh-state note.

    Used by Speaking or Thinking when an issue should not be dispatched
    (e.g. blocked on missing dependency, target code not yet on master,
    requires human decision). The dispatcher reads this state and
    skips writing a fresh dispatch surface until it is explicitly lifted.

    Args:
        repo: ``"owner/name"`` slug, matching the gh CLI form.
        number: issue number.
        reason: short human-readable explanation of why the issue is on hold.
        deferred_by: ``"speaking"``, ``"thinking"``, or a username like
            ``"jcronq"`` — captures who put the hold in place.
        title: optional issue title; included verbatim in the frontmatter
            and heading when provided.

    Returns:
        The path to the written note.
    """
    note_path = GH_STATE_DIR / f"{repo}-{number}.md"
    now = datetime.now(timezone.utc).isoformat()
    title_text = (title or "").strip()
    title_heading = f"{repo}#{number} — {title_text}" if title_text else f"{repo}#{number}"
    # Escape any embedded double-quotes in reason so the YAML stays valid.
    safe_reason = reason.replace('"', '\\"')
    content = (
        f'---\n'
        f'slug: gh-state-{repo}-{number}\n'
        f'title: {title_heading}\n'
        f'tags: [gh-state]\n'
        f'note_type: gh-state\n'
        f'repo: {repo}\n'
        f'number: {number}\n'
        f'type: deferred\n'
        f'issue_number: {number}\n'
        f'reason: "{safe_reason}"\n'
        f'deferred_by: {deferred_by}\n'
        f'deferred_at: "{now}"\n'
        f'updated_at: "{now}"\n'
        f'---\n\n'
        f'# {title_heading}\n\n'
        f'Deferred. {reason}.\n'
    )
    _atomic_write(note_path, content)
    return note_path


def read_state(repo: str, number: int) -> dict | None:
    """Return a small dict describing the current gh-state for an issue/PR.

    Returns ``None`` if no gh-state note exists. Otherwise returns a dict
    with at least ``{"type": ..., "path": Path}`` plus any of ``state``,
    ``merged``, ``draft``, ``reason``, ``deferred_by`` parsed from the
    frontmatter when present. The parser is intentionally tiny: it looks
    for ``key: value`` lines inside the leading ``---`` block, since the
    notes we write are flat YAML.

    The dispatcher uses this to skip writing a dispatch surface when an
    issue is already in flight (``type: pr``, ``state: open``) or has
    been put on hold (``type: deferred``).
    """
    note_path = GH_STATE_DIR / f"{repo}-{number}.md"
    if not note_path.exists():
        return None
    try:
        text = note_path.read_text()
    except OSError:
        return None
    # Strip frontmatter block.
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end].strip("\n")
    state: dict = {"path": note_path}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"')
        if key in {"type", "state", "merged", "draft", "reason", "deferred_by", "deferred_at", "repo", "number", "issue_number"}:
            state[key] = value
    return state


def is_deferred(repo: str, number: int) -> bool:
    """Return True iff a gh-state note marks this issue as ``type: deferred``."""
    s = read_state(repo, number)
    return bool(s and s.get("type") == "deferred")


def write_dispatched_inflight(
    repo: str,
    number: int,
    worker_id: str,
    title: str = "",
) -> Path:
    """Write a ``type: dispatched-in-flight`` gh-state note.

    Used by Speaking at worker-spawn time to close the dispatcher race
    window described in [[2026-05-19-dispatch-race-gap]]: when a worker
    has been spawned but hasn't yet pushed a branch / opened a PR, the
    issue has no GitHub-visible trace, so Thinking's dispatcher scan
    would otherwise produce a duplicate ``attempt-issue-fix`` surface.
    Thinking reads this state via :func:`is_dispatched_inflight` and
    skips writing a dispatch surface for in-flight work.

    The record is ephemeral. Two cleanup paths cover the lifecycle:

    * **Normal path** — when the worker pushes a branch and opens a PR,
      the next cron pass overwrites the dispatched-inflight note with a
      ``type: pr`` note. No explicit teardown needed.
    * **Stuck-worker path** — if the worker crashes or hangs and never
      opens a PR, the cleanup pass in :func:`main` removes any
      dispatched-inflight note whose ``created`` timestamp is older than
      :data:`DISPATCHED_INFLIGHT_TIMEOUT_HOURS` (4h by default), letting
      the dispatcher re-process the issue on the next cycle.

    Args:
        repo: ``"owner/name"`` slug, matching the gh CLI form.
        number: issue number.
        worker_id: the subagent / worker identifier that Speaking
            spawned. Gives Speaking attribution in the audit trail.
        title: optional issue title; included verbatim in the
            frontmatter and heading when provided.

    Returns:
        The path to the written note.
    """
    note_path = GH_STATE_DIR / f"{repo}-{number}.md"
    now = datetime.now(timezone.utc).isoformat()
    title_text = (title or "").strip()
    title_heading = f"{repo}#{number} — {title_text}" if title_text else f"{repo}#{number}"
    content = (
        f'---\n'
        f'slug: gh-state-{repo}-{number}\n'
        f'title: {title_heading}\n'
        f'tags: [gh-state]\n'
        f'note_type: gh-state\n'
        f'repo: {repo}\n'
        f'number: {number}\n'
        f'type: dispatched-in-flight\n'
        f'issue_number: {number}\n'
        f'worker_id: {worker_id}\n'
        f'created: "{now}"\n'
        f'updated: "{now}"\n'
        f'---\n\n'
        f'# {title_heading}\n\n'
        f'Dispatched-in-flight. Worker `{worker_id}`. Created {now}.\n'
    )
    _atomic_write(note_path, content)
    return note_path


def is_dispatched_inflight(repo: str, number: int) -> bool:
    """Return True iff a gh-state note marks this issue as ``type: dispatched-in-flight``."""
    s = read_state(repo, number)
    return bool(s and s.get("type") == "dispatched-in-flight")


# Timeout (hours) for ``type: dispatched-in-flight`` notes. After this
# window the cleanup pass in :func:`main` removes the note so the
# dispatcher can re-process the issue. The race window the record exists
# to close is seconds-to-minutes; 4h is generous for any legitimate
# long-running worker without leaving a permanent suppression on a
# crashed dispatch.
DISPATCHED_INFLIGHT_TIMEOUT_HOURS = 4


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
        # gh CLI deprecated and removed the `merged` boolean field; the
        # replacement `mergedAt` is None for unmerged PRs and an ISO
        # timestamp once a merge has happened. We keep the internal
        # `merged` boolean shape so downstream consumers and frontmatter
        # readers don't have to change.
        merged = item.get("mergedAt") is not None
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

    _atomic_write(note_path, content)


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
                "--json", "number,title,state,isDraft,mergedAt,baseRefName,updatedAt,createdAt"
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

        # ── Dispatched-in-flight timeout cleanup ──────────────────────
        # Ephemeral records written by Speaking at worker-spawn time
        # (see :func:`write_dispatched_inflight`). If a worker is stuck
        # and never opens a PR, the record should expire so the
        # dispatcher can re-process the issue. Timeout:
        # :data:`DISPATCHED_INFLIGHT_TIMEOUT_HOURS` from ``created``.
        #
        # Runs BEFORE the deferred/dispatched-in-flight preservation
        # check below so surviving notes still get preserved on this
        # pass but stale ones are dropped. See
        # [[2026-05-19-dispatched-inflight-write-path]].
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=DISPATCHED_INFLIGHT_TIMEOUT_HOURS
            )
            for note_path in list(GH_STATE_DIR.glob(f"{repo}-*.md")):
                if not note_path.exists():
                    continue
                try:
                    content = note_path.read_text()
                except FileNotFoundError:
                    continue
                if "type: dispatched-in-flight" not in content:
                    continue
                # Parse the `created` field from frontmatter.
                # Format: created: "2026-05-19T20:10:00+00:00"
                for line in content.splitlines():
                    if line.strip().startswith("created:"):
                        created_str = line.partition(":")[2].strip().strip('"')
                        try:
                            created = datetime.fromisoformat(created_str)
                        except ValueError:
                            # Malformed timestamp — treat as expired so the
                            # record doesn't wedge the dispatcher forever.
                            _log(
                                f"[{_ts()}] Removing malformed dispatched-inflight: "
                                f"{note_path} (created={created_str!r})\n"
                            )
                            note_path.unlink()
                            removed += 1
                            break
                        if created < cutoff:
                            _log(
                                f"[{_ts()}] Removing stale dispatched-inflight: "
                                f"{note_path} (created {created_str}, "
                                f"timeout {DISPATCHED_INFLIGHT_TIMEOUT_HOURS}h)\n"
                            )
                            note_path.unlink()
                            removed += 1
                        break
        except Exception as e:
            _log(
                f"[{_ts()}] ERROR during dispatched-inflight cleanup for {repo}: {e}\n"
            )
            errors += 1

        # Cleanup: remove notes for closed issues/PRs
        # Strategy: check what's currently open via API, compare to what's on disk.
        # This is cheaper than calling `gh issue/PR view` per file.
        try:
            # Build set of open issue numbers
            open_issues = {i["number"] for i in issues}
            open_prs = {p["number"] for p in prs}

            for note_path in list(GH_STATE_DIR.glob(f"{repo}-*.md")):
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

                # Deferred and dispatched-in-flight notes are written by
                # Speaking/Thinking, not by this script. They have no
                # GitHub-side counterpart, so the open-issues/open-PRs
                # comparison below would always treat them as stale.
                # Preserve them unconditionally — the only way to clear a
                # deferred state is an explicit lift; dispatched-in-flight
                # is cleared by the timeout pass above (or overwritten by
                # the next mirror cycle once the worker pushes a PR). See
                # [[2026-05-19-stale-cycle-dispatcher-gap]] and
                # [[2026-05-19-dispatched-inflight-write-path]].
                if "type: deferred" in content or "type: dispatched-in-flight" in content:
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
