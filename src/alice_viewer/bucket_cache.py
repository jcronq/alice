"""Disk cache for per-time-bucket narrative summaries.

One JSON file per (bucket_seconds, bucket_start_int) key, stored under
`$ALICE_VIEWER_CACHE_DIR` (default `~/.local/state/alice/viewer-cache/buckets/`).

Each file carries its own content_hash so the reader can detect when the
underlying events have changed and regenerate — hot buckets (the "now" end of
any window) invalidate naturally as new events land in them.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass


CACHE_TTL_SECONDS = 7 * 86400  # 7 days


def cache_dir() -> pathlib.Path:
    override = os.environ.get("ALICE_VIEWER_CACHE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".local/state/alice/viewer-cache/buckets"


@dataclass
class BucketSummary:
    bucket_start: int
    bucket_seconds: int
    content_hash: str
    event_count: int
    summary: str
    cost_usd: float
    duration_ms: int
    generated_at: float


def _path_for(bucket_seconds: int, bucket_start: int) -> pathlib.Path:
    return cache_dir() / f"{bucket_seconds}s" / f"{bucket_start}.json"


def read(bucket_seconds: int, bucket_start: int, content_hash: str) -> BucketSummary | None:
    path = _path_for(bucket_seconds, bucket_start)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("content_hash") != content_hash:
        return None
    if time.time() - (raw.get("generated_at") or 0) > CACHE_TTL_SECONDS:
        return None
    try:
        return BucketSummary(**raw)
    except TypeError:
        return None


def write(summary: BucketSummary) -> None:
    path = _path_for(summary.bucket_seconds, summary.bucket_start)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(summary), ensure_ascii=False, default=str))
    tmp.replace(path)


def purge_expired() -> int:
    root = cache_dir()
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for p in root.rglob("*.json"):
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if now - (raw.get("generated_at") or 0) > CACHE_TTL_SECONDS:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
