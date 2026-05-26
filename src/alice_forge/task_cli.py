"""``task`` CLI — argparse over :mod:`alice_forge.task_store`.

The CLI is the kernel-agnostic invocation surface promised by
issue #375: Speaking, Thinking, and ad-hoc Bash all reach the SM v2
task store through the same five subcommands.

Usage examples::

    task create --title "Investigate Plex wedging" --tags plex,investigation
    task update task-0019 --status building --reason "spawned worker bg-abc"
    task list --status open --tag auto-fix
    task view task-0019
    task close task-0019 --merge-ref https://github.com/jcronq/alice/pull/376

The CLI is also accessible as ``python -m alice_forge.task_cli``, which
is what the ``bin/task`` shell wrapper invokes.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .task_store import (
    InvalidState,
    InvalidTransition,
    TaskNotFound,
    TaskRecord,
    TaskStore,
    TaskStoreError,
    VALID_STATES,
    default_root,
)


# ---------------------------------------------------------------------------
# Argument parsing


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="task",
        description=(
            "SM v2 task tracker — operates on ~/alice-mind/inner/tasks/. "
            "Subcommand schema is the single source of truth for the "
            "store; the SKILL.md body points here for canonical docs."
        ),
    )
    p.add_argument(
        "--root",
        default=None,
        help=(
            "Override inner/tasks/ root. Default: $TASKS_DIR, then "
            "$ALICE_MIND_DIR/inner/tasks, then ~/alice-mind/inner/tasks."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # ---- create
    pc = sub.add_parser(
        "create", help="Allocate a new task in status=draft."
    )
    pc.add_argument("--title", required=True, help="Short task title.")
    pc.add_argument(
        "--actor",
        default="speaking",
        choices=["speaking", "thinking", "jason", "alice"],
        help="Who initiated the task. Default: speaking.",
    )
    pc.add_argument(
        "--artifact-type",
        default="code",
        choices=["research_note", "code", "experiment", "config_change"],
        help="What the task produces. Default: code.",
    )
    pc.add_argument(
        "--source",
        default="speaking",
        help=(
            "Origin of the task — 'speaking', 'thinking', 'jason', or a "
            "free-form ref (e.g. '<repo>#<N>'). Default: speaking."
        ),
    )
    pc.add_argument(
        "--tags",
        default="",
        help="Comma-separated tags (e.g. plex,investigation,auto-fix).",
    )
    pc.add_argument(
        "--reason",
        default=None,
        help="Reason for the initial draft transition (logged to transitions.jsonl).",
    )

    # ---- update
    pu = sub.add_parser("update", help="Transition a task to a new status.")
    pu.add_argument("id", help="Task id (task-NNNN).")
    pu.add_argument(
        "--status",
        required=True,
        choices=sorted(VALID_STATES),
        help="Target status.",
    )
    pu.add_argument(
        "--actor",
        default="speaking",
        choices=["speaking", "thinking", "jason", "alice"],
        help="Who is making the transition. Default: speaking.",
    )
    pu.add_argument(
        "--reason",
        default=None,
        help="One-line reason for the transition (logged).",
    )
    pu.add_argument(
        "--merge-ref",
        default=None,
        help=(
            "PR or commit URL — required for building → done (self-merge "
            "shortcut) and recommended for validating transitions."
        ),
    )
    pu.add_argument(
        "--validation-evidence",
        default=None,
        help="What was checked. Required for validating → done.",
    )
    pu.add_argument(
        "--unblocked-by",
        default=None,
        help=(
            "What must change for unblocking. Required for any → blocked "
            "(SM v2 §Required Transition Fields)."
        ),
    )

    # ---- list
    pl = sub.add_parser("list", help="Filter index.jsonl entries.")
    pl.add_argument(
        "--status",
        default=None,
        help=(
            "Filter by exact status, OR 'open' as shorthand for "
            "'not done and not rejected'."
        ),
    )
    pl.add_argument("--tag", default=None, help="Filter by tag.")
    pl.add_argument("--actor", default=None, help="Filter by actor.")

    # ---- view
    pv = sub.add_parser("view", help="Print task.yaml + transitions.jsonl.")
    pv.add_argument("id", help="Task id (task-NNNN).")

    # ---- close
    pcl = sub.add_parser(
        "close", help="Sugar over update --status done."
    )
    pcl.add_argument("id", help="Task id (task-NNNN).")
    pcl.add_argument(
        "--reason", default=None, help="Why the task is being closed."
    )
    pcl.add_argument(
        "--merge-ref",
        default=None,
        help="PR or commit URL — required for building → done.",
    )
    pcl.add_argument(
        "--actor",
        default="speaking",
        choices=["speaking", "thinking", "jason", "alice"],
        help="Who is closing the task. Default: speaking.",
    )

    return p


# ---------------------------------------------------------------------------
# Output formatting


def _print_record(record: TaskRecord, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(record.to_yaml_dict(), sort_keys=False))
        return
    print(f"{record.id}  [{record.status}]  {record.title}")
    if record.tags:
        print(f"  tags: {', '.join(record.tags)}")
    if record.merge_ref:
        print(f"  merge_ref: {record.merge_ref}")


def _print_index_entries(entries: list[dict], *, as_json: bool) -> None:
    if as_json:
        for entry in entries:
            print(json.dumps(entry))
        return
    if not entries:
        print("(no tasks match)")
        return
    # Sort newest-first by updated timestamp.
    for entry in sorted(
        entries, key=lambda e: e.get("updated", ""), reverse=True
    ):
        status = entry.get("status", "?")
        tags = ",".join(entry.get("tags") or [])
        tag_blob = f"  [{tags}]" if tags else ""
        print(
            f"{entry.get('id', '?')}  {status:11s}  "
            f"{entry.get('title', '')}{tag_blob}"
        )


# ---------------------------------------------------------------------------
# Subcommand handlers


def _cmd_create(store: TaskStore, args: argparse.Namespace) -> int:
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    record = store.create(
        title=args.title,
        actor=args.actor,
        artifact_type=args.artifact_type,
        source=args.source,
        tags=tags,
        reason=args.reason,
    )
    _print_record(record, as_json=args.json)
    return 0


def _cmd_update(store: TaskStore, args: argparse.Namespace) -> int:
    record = store.update(
        args.id,
        status=args.status,
        actor=args.actor,
        reason=args.reason,
        merge_ref=args.merge_ref,
        validation_evidence=args.validation_evidence,
        unblocked_by=args.unblocked_by,
    )
    _print_record(record, as_json=args.json)
    return 0


def _cmd_list(store: TaskStore, args: argparse.Namespace) -> int:
    status_arg = args.status
    open_only = False
    if status_arg == "open":
        status_arg = None
        open_only = True
    entries = store.list(
        status=status_arg,
        tag=args.tag,
        actor=args.actor,
        open_only=open_only,
    )
    _print_index_entries(entries, as_json=args.json)
    return 0


def _cmd_view(store: TaskStore, args: argparse.Namespace) -> int:
    record = store.load(args.id)
    transitions = store.transitions(args.id)
    if args.json:
        out = {
            "task": record.to_yaml_dict(),
            "transitions": transitions,
        }
        print(json.dumps(out, sort_keys=False))
        return 0
    print(f"# {record.id}: {record.title}")
    print(f"status:        {record.status}")
    print(f"actor:         {record.actor}")
    print(f"artifact_type: {record.artifact_type}")
    print(f"source:        {record.source}")
    print(f"created:       {record.created}")
    print(f"updated:       {record.updated}")
    if record.tags:
        print(f"tags:          {', '.join(record.tags)}")
    if record.merge_ref:
        print(f"merge_ref:     {record.merge_ref}")
    print()
    print("## Transitions")
    for t in transitions:
        frm = t.get("from") or "(none)"
        to = t.get("to")
        reason = t.get("reason", "")
        print(f"  {t.get('ts'):25s}  {frm:11s} → {to:11s}  {reason}")
    return 0


def _cmd_close(store: TaskStore, args: argparse.Namespace) -> int:
    record = store.close(
        args.id,
        reason=args.reason,
        merge_ref=args.merge_ref,
        actor=args.actor,
    )
    _print_record(record, as_json=args.json)
    return 0


_HANDLERS = {
    "create": _cmd_create,
    "update": _cmd_update,
    "list": _cmd_list,
    "view": _cmd_view,
    "close": _cmd_close,
}


# ---------------------------------------------------------------------------
# Entry point


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI. Returns an exit code (0 on success, non-zero on error)."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = args.root or default_root()
    store = TaskStore(root)
    handler = _HANDLERS[args.command]
    try:
        return handler(store, args)
    except TaskNotFound as exc:
        print(f"error: task not found: {exc}", file=sys.stderr)
        return 2
    except InvalidTransition as exc:
        print(f"error: invalid transition: {exc}", file=sys.stderr)
        return 3
    except InvalidState as exc:
        print(f"error: invalid state: {exc}", file=sys.stderr)
        return 4
    except TaskStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
