"""status-reply THREAD_ID "text" — post Alice's reply into a status-page item thread.

Threads live in $STATUS_DIR/threads/<id>.jsonl (default ~/alice-mind/inner/status)
and render under the matching item on the status site. THREAD_ID is the item's
slug (the lowercased bold title), the same id the status-reply surface names.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from status_site.server import now_iso, thread_id


def status_dir() -> Path:
    env = os.environ.get("STATUS_DIR")
    if env:
        return Path(env)
    mind = os.environ.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "status"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not args[1].strip():
        print(__doc__, file=sys.stderr)
        return 2
    tid = thread_id(args[0], "")
    if tid == "general" and not args[0].strip():
        print("status-reply: empty thread id", file=sys.stderr)
        return 2
    d = status_dir() / "threads"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{tid}.jsonl", "a") as f:
        f.write(
            json.dumps({"from": "alice", "at": now_iso(), "text": args[1].strip()})
            + "\n"
        )
    print(f"posted to thread {tid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
