"""status-reply / status-chat / waiting-ask — status-site CLIs.

``status-reply THREAD_ID "text"`` appends to a per-item thread at
``$STATUS_DIR/threads/<id>.jsonl`` (default ``~/alice-mind/inner/status``) and
renders under the matching item on the status site. THREAD_ID is the item's
slug (the lowercased bold title), the same id the status-reply surface names.

``status-chat "text"`` appends to the general chat log at
``$STATUS_DIR/.chat.jsonl`` and renders on the status site's Chat tab, which
has no item or thread context.

``waiting-ask "text"`` / ``waiting-ask --resolve ID`` / ``waiting-ask --list``
manages structured asks on the Waiting-on-you section, writing to
``$STATUS_DIR/asks.jsonl``. See
``cortex-memory/designs/2026-11-05-issue638-overview-auto-render.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from status_site.server import now_iso, thread_id
from status_site.sources.common import (
    asks_path as _asks_path,
    new_ask_id,
    read_asks,
    status_dir_from_env,
)


def status_dir() -> Path:
    env = os.environ.get("STATUS_DIR")
    if env:
        return Path(env)
    mind = os.environ.get("ALICE_MIND") or os.path.expanduser("~/alice-mind")
    return Path(mind) / "inner" / "status"


def main(argv: list[str] | None = None) -> int:
    """status-reply THREAD_ID "text" — post Alice's reply into an item thread."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not args[1].strip():
        print(main.__doc__, file=sys.stderr)
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


def main_chat(argv: list[str] | None = None) -> int:
    """status-chat "text" — post Alice's reply into the general Chat tab log."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].strip():
        print(main_chat.__doc__, file=sys.stderr)
        return 2
    d = status_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".chat.jsonl", "a") as f:
        f.write(
            json.dumps({"from": "alice", "at": now_iso(), "text": args[0].strip()})
            + "\n"
        )
    print("posted to chat")
    return 0


# ─── waiting-ask ─────────────────────────────────────────────────────


def _write_asks(path: Path, records: list[dict]) -> None:
    """Rewrite ``asks.jsonl`` atomically. Used for ``--resolve`` only;
    plain appends keep using an ``a`` open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    tmp.replace(path)


def main_waiting_ask(argv: list[str] | None = None) -> int:
    """Append / resolve / list waiting-on-you asks.

    Usage:
      waiting-ask "<text>"           append a new pending ask (prints id)
      waiting-ask --resolve <id>     mark an ask resolved
      waiting-ask --list             print unresolved asks, oldest first
    """
    parser = argparse.ArgumentParser(
        prog="waiting-ask",
        description=(
            "Manage structured asks on the status-site "
            "Waiting-on-you section. Writes to $STATUS_DIR/asks.jsonl."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--resolve",
        metavar="ID",
        help="Mark the ask with this id resolved.",
    )
    group.add_argument(
        "--list",
        action="store_true",
        dest="list_pending",
        help="Print unresolved asks, oldest first, then exit.",
    )
    parser.add_argument(
        "text",
        nargs="?",
        help="Ask text. Required unless --resolve or --list is used.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    sdir = status_dir_from_env()
    path = _asks_path(sdir)

    if args.list_pending:
        records = read_asks(path)
        pending = [r for r in records if not r.get("resolved")]
        pending.sort(key=lambda r: str(r.get("created") or ""))
        if not pending:
            print("(no pending asks)")
            return 0
        for r in pending:
            print(f"{r.get('id', '?')}: {r.get('text', '').strip()}")
        return 0

    if args.resolve:
        ask_id = str(args.resolve).strip()
        if not ask_id:
            print("waiting-ask: --resolve requires an id", file=sys.stderr)
            return 2
        records = read_asks(path)
        if not records:
            # Missing file: nothing to resolve, but the idempotent
            # error is the same as "id not found".
            print(f"waiting-ask: no pending ask with id {ask_id}", file=sys.stderr)
            return 1
        changed = False
        for r in records:
            if r.get("id") == ask_id and not r.get("resolved"):
                r["resolved"] = True
                r["resolved_at"] = now_iso()
                changed = True
        if not changed:
            # Idempotent: resolving an already-resolved ask is a no-op
            # that exits 0; a missing id exits 1 so callers can tell.
            for r in records:
                if r.get("id") == ask_id and r.get("resolved"):
                    print(f"{ask_id} already resolved")
                    return 0
            print(f"waiting-ask: no pending ask with id {ask_id}", file=sys.stderr)
            return 1
        _write_asks(path, records)
        print(f"resolved {ask_id}")
        return 0

    text = (args.text or "").strip()
    if not text:
        parser.print_help(sys.stderr)
        return 2
    record = {
        "id": new_ask_id(),
        "text": text,
        "created": now_iso(),
        "resolved": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    print(record["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
