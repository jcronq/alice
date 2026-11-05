#!/usr/bin/env python3
"""Poll GitHub for the SM design-phase queue and cache it for the status site.

Writes ``<ALICE_MIND>/inner/status/.thinking-queue.json`` atomically as
``{"synced_at": <epoch>, "items": [{number,title,labels,updatedAt}, ...]}``.
``status_site.server`` renders it into the Overview page's Thinking queue.
Needs an authenticated ``gh`` CLI, same as ``gh_state_mirror``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = "jcronq/alice"
LABELS = (
    "sm:designing",
    "sm:needs_study",
    "sm:design_review",
    "sm:designed",
    "sm:compacting",
    "sm:blocked",
)
CACHE_NAME = ".thinking-queue.json"


def status_dir() -> Path:
    mind = os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind"))
    return Path(os.environ.get("STATUS_DIR", str(Path(mind) / "inner" / "status")))


def gh_issue_list(label: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list", "--repo", REPO, "--label", label,
            "--state", "open", "--limit", "100",
            "--json", "number,title,labels,updatedAt",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list --label {label} failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def collect(fetch=gh_issue_list) -> list[dict]:
    """One entry per issue, even if it carries several ``sm:*`` labels."""
    by_number: dict[int, dict] = {}
    for label in LABELS:
        for issue in fetch(label):
            by_number.setdefault(int(issue["number"]), {
                "number": int(issue["number"]),
                "title": issue.get("title", ""),
                "labels": [
                    l["name"] if isinstance(l, dict) else l for l in issue.get("labels") or []
                ],
                "updatedAt": issue.get("updatedAt", ""),
            })
    return sorted(by_number.values(), key=lambda i: i["number"])


def write_cache(directory: Path, items: list[dict], now: float | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / CACHE_NAME
    payload = {"synced_at": time.time() if now is None else now, "items": items}
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return dest


def main() -> int:
    try:
        write_cache(status_dir(), collect())
    except Exception as exc:  # leave the previous cache; the site shows it as stale
        print(f"thinking_queue_poller: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
