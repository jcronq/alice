#!/usr/bin/env python3
"""Append (or dedupe) an entry to inner/state/auto-commit-failures.jsonl.

Bounds the growth of the failure log by:

1. Truncating the `output` field to 2 KB so a repeated 40-KB lint stderr
   stops dominating the file.
2. Collapsing consecutive failures with matching `exit_code` and `files`
   into a single record (`count` + `last_seen_at`), so a hook that fails
   the same way 85x/day produces one line, not 85.

Invoked by bin/alice-mind-autopush as a child process. Kept dependency-free
(stdlib only) so it can run in any Python 3.8+ environment the autopush
timer happens to land in.

Closes jcronq/alice#403.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

OUTPUT_MAX_BYTES = 2048
TRUNCATION_MARKER = "\n...[truncated, {n} bytes omitted]"


def truncate_output(output: str) -> str:
    """Cap `output` at OUTPUT_MAX_BYTES, appending a marker if truncated."""
    if len(output) <= OUTPUT_MAX_BYTES:
        return output
    omitted = len(output) - OUTPUT_MAX_BYTES
    return output[:OUTPUT_MAX_BYTES] + TRUNCATION_MARKER.format(n=omitted)


def _read_last_line(path: Path) -> Optional[str]:
    """Return the last non-empty line of `path`, or None if file is empty/missing."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return None
            # Scan backwards for the last newline. Cap at 1 MB to avoid
            # pathological reads on a corrupt file with no newlines.
            chunk_size = min(size, 1024 * 1024)
            fh.seek(size - chunk_size)
            tail = fh.read(chunk_size)
        text = tail.decode("utf-8", errors="replace")
        # Strip a single trailing newline, then take the last line.
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return None
        last_nl = text.rfind("\n")
        if last_nl == -1:
            return text
        return text[last_nl + 1 :]
    except OSError:
        return None


def _atomic_rewrite_last_line(path: Path, new_last_line: str) -> None:
    """Rewrite `path` replacing its final line with `new_last_line`.

    Uses a temp file + os.rename so a crash mid-write can't corrupt the log.
    """
    with path.open("rb") as fh:
        data = fh.read()
    # Split off the trailing newline so we can cleanly find the last record.
    text = data.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    last_nl = text.rfind("\n")
    head = "" if last_nl == -1 else text[: last_nl + 1]
    new_body = head + new_last_line + "\n"

    # Write to a sibling temp file in the same dir, then atomic rename.
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(new_body)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        os.rename(tmp_path, path)
    except Exception:
        # Best-effort cleanup; re-raise so caller sees the failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def log_failure(
    log_path: Path,
    timestamp: str,
    exit_code: int,
    files: str,
    output: str,
) -> dict[str, Any]:
    """Append (or dedupe-merge) a failure entry. Returns the entry as written.

    Dedup rule: if the existing last line of `log_path` has the same
    `exit_code` and `files`, merge into it (increment `count`, set
    `last_seen_at`, keep original `timestamp` and `output`). Otherwise
    append a fresh entry with `count: 1`.

    Malformed last line → treat as no match; append normally.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    truncated = truncate_output(output)

    last_line = _read_last_line(log_path)
    prev: Optional[dict[str, Any]] = None
    if last_line is not None:
        try:
            candidate = json.loads(last_line)
            if isinstance(candidate, dict):
                prev = candidate
        except (json.JSONDecodeError, ValueError):
            prev = None

    if (
        prev is not None
        and prev.get("exit_code") == exit_code
        and prev.get("files") == files
    ):
        # Merge into the existing trailing entry.
        merged = dict(prev)
        merged["count"] = int(prev.get("count", 1)) + 1
        merged["last_seen_at"] = timestamp
        # Preserve original `timestamp` and `output` (output is already truncated).
        new_line = json.dumps(merged, ensure_ascii=False)
        _atomic_rewrite_last_line(log_path, new_line)
        return merged

    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "exit_code": exit_code,
        "files": files,
        "output": truncated,
        "count": 1,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", required=True, type=Path)
    parser.add_argument("--timestamp", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--files", required=True)
    parser.add_argument(
        "--output-stdin",
        action="store_true",
        help="Read `output` body from stdin (avoids argv-size limits).",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    if args.output_stdin:
        output = sys.stdin.read()
    elif args.output is not None:
        output = args.output
    else:
        parser.error("must pass --output or --output-stdin")

    log_failure(
        log_path=args.log_path,
        timestamp=args.timestamp,
        exit_code=args.exit_code,
        files=args.files,
        output=output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
