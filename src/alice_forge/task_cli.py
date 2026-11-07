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

from .sm.cli import add_sm_subparser, cmd_sm
from .task_store import (
    InvalidState,
    InvalidTransition,
    ReindexReport,
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

    # ---- reindex
    pr = sub.add_parser(
        "reindex",
        help=(
            "Rebuild index.jsonl from task-*/task.yaml. "
            "Use --dry-run to preview changes."
        ),
    )
    pr.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the diff (added/removed/changed/errors) without "
            "writing index.jsonl."
        ),
    )

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

    # ---- db — read-only joins over the derived alice-state.db
    # (issue #789). The DB is populated by the memory worker's
    # ``derive_tasks_workers_events`` step; this CLI is a pure
    # reader, so ``--root`` is irrelevant here (the DB lives under
    # ``inner/state/`` not ``inner/tasks/``). All db subcommands
    # default to --json output because they feed status-site and
    # dispatcher checks downstream.
    pdb = sub.add_parser(
        "db",
        help=(
            "Read joins over alice-state.db (derived index over "
            "tasks/workers/events). Issue #789 PR 1."
        ),
    )
    pdb.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override alice-state.db path. Default: "
            "$ALICE_MIND_DIR/inner/state/alice-state.db, else "
            "~/alice-mind/inner/state/alice-state.db."
        ),
    )
    db_sub = pdb.add_subparsers(dest="db_command", required=True)

    db_state = db_sub.add_parser(
        "state",
        help=(
            "One-shot cross-store snapshot for a 'jcronq/alice#784' "
            "style qualifier. Returns task + workers + recent events."
        ),
    )
    db_state.add_argument(
        "qualifier",
        help="GH qualifier, e.g. jcronq/alice#784.",
    )

    db_workers = db_sub.add_parser(
        "workers", help="Recent background-worker handles."
    )
    db_workers.add_argument(
        "--outcome",
        default=None,
        help="Filter by outcome (pr_opened | no_change | error | ...).",
    )
    db_workers.add_argument(
        "--kind",
        default=None,
        help="Filter by worker kind.",
    )
    db_workers.add_argument(
        "--limit", type=int, default=20,
        help="Row cap. Default 20.",
    )

    db_tasks = db_sub.add_parser(
        "tasks", help="List tasks, optionally filtered by GH issue."
    )
    db_tasks.add_argument(
        "--gh-issue",
        default=None,
        help="Filter by 'jcronq/alice#784' style qualifier.",
    )
    db_tasks.add_argument(
        "--status",
        default=None,
        help="Filter by exact SM v2 status.",
    )
    db_tasks.add_argument(
        "--limit", type=int, default=50,
        help="Row cap. Default 50.",
    )

    db_events = db_sub.add_parser(
        "events", help="Recent events.jsonl rows (derived index)."
    )
    db_events.add_argument(
        "--type",
        default=None,
        help="Filter by event type.",
    )
    db_events.add_argument(
        "--since",
        default=None,
        help="ISO-8601 lower bound on event ts.",
    )
    db_events.add_argument(
        "--limit", type=int, default=20,
        help="Row cap. Default 20.",
    )

    # ---- db item <item_id> — one item + current SM state + last few
    # transitions + external refs. The item_id is the human-readable
    # surrogate written by the #790 seeder: ``gh:jcronq/alice#790``
    # or ``local:task-0760``.
    db_item = db_sub.add_parser(
        "item",
        help=(
            "Print an item + its state + last 5 transitions + "
            "external refs. SM v2 (#790)."
        ),
    )
    db_item.add_argument(
        "item_id",
        help=(
            "Item surrogate id — ``gh:jcronq/alice#790`` for GH "
            "issues, ``local:task-0760`` for local tasks."
        ),
    )
    db_item.add_argument(
        "--transition-limit",
        type=int,
        default=5,
        help="How many recent transitions to include. Default 5.",
    )

    # ---- db transitions <item_id> — full transition history for
    # one item. Separate from ``db item`` so the common case (a quick
    # peek) stays small and the full-audit path is explicit.
    db_transitions = db_sub.add_parser(
        "transitions",
        help="Full transition history for one item_id (oldest-first).",
    )
    db_transitions.add_argument(
        "item_id", help="Item surrogate id (see ``db item`` --help)."
    )
    db_transitions.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on rows returned. Omit for the full audit trail.",
    )

    # ---- deps — item_dependencies CRUD (issue #833). The DB must be
    # present; the three subcommands are thin wrappers over
    # :mod:`alice_state_db.dependencies`. Edge kinds:
    # ``blocks`` | ``follows`` | ``supersedes``.
    pdeps = sub.add_parser(
        "deps",
        help=(
            "Manage item_dependencies (blocks/follows/supersedes). "
            "Writes to alice-state.db. Issue #833."
        ),
    )
    pdeps.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override alice-state.db path. Default: "
            "$ALICE_MIND_DIR/inner/state/alice-state.db, else "
            "~/alice-mind/inner/state/alice-state.db."
        ),
    )
    deps_sub = pdeps.add_subparsers(dest="deps_command", required=True)

    deps_add = deps_sub.add_parser(
        "add", help="Add a dependency edge. Idempotent, cycle-checked."
    )
    deps_add.add_argument(
        "item_id",
        help="The dependent item (e.g. gh:jcronq/alice#833).",
    )
    deps_add.add_argument(
        "depends_on_item_id",
        help="The prerequisite item (e.g. gh:jcronq/alice#823).",
    )
    deps_add.add_argument(
        "--kind",
        default="blocks",
        choices=["blocks", "follows", "supersedes"],
        help="Edge kind. Default: blocks.",
    )

    deps_list = deps_sub.add_parser(
        "list", help="List direct prerequisites of an item."
    )
    deps_list.add_argument(
        "item_id", help="The dependent item."
    )
    deps_list.add_argument(
        "--kind",
        default="blocks",
        choices=["blocks", "follows", "supersedes"],
        help="Edge kind to filter. Default: blocks.",
    )

    deps_remove = deps_sub.add_parser(
        "remove", help="Remove a dependency edge. Idempotent."
    )
    deps_remove.add_argument(
        "item_id", help="The dependent item."
    )
    deps_remove.add_argument(
        "depends_on_item_id", help="The prerequisite item."
    )
    deps_remove.add_argument(
        "--kind",
        default="blocks",
        choices=["blocks", "follows", "supersedes"],
        help="Edge kind. Default: blocks.",
    )

    # ---- sm — DB-primary state-machine verbs (issue #825). Replaces
    # the ``[SM] <verb>`` comment convention on GitHub. All writes
    # route through :mod:`alice_forge.sm.store`; the handlers live in
    # :mod:`alice_forge.sm.cli` so this parser stays navigable.
    add_sm_subparser(sub)

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


def _cmd_reindex(store: TaskStore, args: argparse.Namespace) -> int:
    """Rebuild ``index.jsonl`` from ``task-*/task.yaml``.

    Issue #781: index.jsonl had 608 lines while the task store held
    912 directories — ~33% miss rate. This subcommand walks the store
    and rewrites the index atomically, flagging any malformed task
    directories to stderr.

    Exit codes:
        0 — reindex ran cleanly (even if dry-run); zero errors.
        6 — reindex ran but some task.yaml files were missing/malformed.
            Operators should investigate the stderr output.
    """
    report: ReindexReport = store.reindex(dry_run=args.dry_run)

    # Errors go to stderr unconditionally. #781 requires that
    # orphan/malformed task directories are flagged — never silently
    # dropped from the rebuild.
    for err in report.errors:
        print(
            f"error: {err['id']}: {err['reason']} ({err['path']})",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=False))
    else:
        verb = "would write" if args.dry_run else "wrote"
        print(
            f"reindex: scanned={report.scanned} {verb}={report.written} "
            f"errors={len(report.errors)} "
            f"added={len(report.added)} removed={len(report.removed)} "
            f"changed={len(report.changed)}"
        )
        if args.dry_run:
            # The operator-facing diff. Capped per bucket so a
            # pathological case (hundreds added) doesn't spam the
            # terminal; the --json path still carries the full lists.
            def _preview(label: str, ids: list[str]) -> None:
                if not ids:
                    return
                head = ids[:20]
                suffix = "" if len(ids) == len(head) else f" (+{len(ids) - len(head)} more)"
                print(f"  {label}: {', '.join(head)}{suffix}")

            _preview("added", report.added)
            _preview("removed", report.removed)
            _preview("changed", report.changed)

    # Non-zero exit when we found malformed/missing task.yaml files so
    # schedulers and operators notice. A clean dry-run still returns 0.
    return 6 if report.errors else 0


def _cmd_close(store: TaskStore, args: argparse.Namespace) -> int:
    record = store.close(
        args.id,
        reason=args.reason,
        merge_ref=args.merge_ref,
        actor=args.actor,
    )
    _print_record(record, as_json=args.json)
    return 0


# ---------------------------------------------------------------------------
# db subcommand — read-only joins over alice-state.db (issue #789 PR 1)
#
# These handlers import ``alice_state_db`` lazily so the rest of the
# CLI keeps working even if the sibling package is missing (e.g. on
# very old container images that predate this PR).


def _cmd_db(store: TaskStore, args: argparse.Namespace) -> int:
    try:
        from alice_state_db import connect
        from alice_state_db import read_api
    except ImportError as exc:  # pragma: no cover
        print(
            f"error: alice_state_db not importable ({exc}) — rebuild the "
            "container or add src/alice_state_db to PYTHONPATH.",
            file=sys.stderr,
        )
        return 5

    db_path = None
    if args.db_path:
        db_path = args.db_path

    try:
        with connect(db_path=db_path) as conn:
            if args.db_command == "state":
                payload = read_api.state_of(conn, args.qualifier)
                print(json.dumps(payload, sort_keys=False, default=str))
                return 0

            if args.db_command == "workers":
                rows = read_api.list_workers(
                    conn,
                    outcome=args.outcome,
                    kind=args.kind,
                    limit=args.limit,
                )
                print(json.dumps(rows, sort_keys=False, default=str))
                return 0

            if args.db_command == "tasks":
                rows = read_api.list_tasks(
                    conn,
                    gh_issue=args.gh_issue,
                    status=args.status,
                    limit=args.limit,
                )
                print(json.dumps(rows, sort_keys=False, default=str))
                return 0

            if args.db_command == "events":
                rows = read_api.list_events(
                    conn,
                    type=args.type,
                    since=args.since,
                    limit=args.limit,
                )
                print(json.dumps(rows, sort_keys=False, default=str))
                return 0

            if args.db_command == "item":
                payload = read_api.item_detail(
                    conn,
                    args.item_id,
                    transition_limit=args.transition_limit,
                )
                if payload is None:
                    print(
                        f"error: item not found: {args.item_id}",
                        file=sys.stderr,
                    )
                    return 2
                print(json.dumps(payload, sort_keys=False, default=str))
                return 0

            if args.db_command == "transitions":
                rows = read_api.item_transitions(
                    conn, args.item_id, limit=args.limit
                )
                print(json.dumps(rows, sort_keys=False, default=str))
                return 0
    except ValueError as exc:
        # Raised by _parse_gh_qualifier for malformed state queries.
        print(f"error: {exc}", file=sys.stderr)
        return 6

    # argparse enforces db_command is required, so this is unreachable.
    print(f"error: unknown db subcommand {args.db_command!r}", file=sys.stderr)
    return 5


def _cmd_deps(store: TaskStore, args: argparse.Namespace) -> int:
    """Dispatch ``task deps add|list|remove`` (issue #833).

    Thin wrapper over :mod:`alice_state_db.dependencies`. The store
    arg is unused — kept in the signature so the dispatcher table
    can call this uniformly with the other handlers. The DB path
    resolves exactly like ``task db``'s ``--db-path`` override.
    """
    try:
        from alice_state_db import (
            DependencyError,
            add_dependency,
            blockers_for,
            connect,
            remove_dependency,
        )
    except ImportError as exc:  # pragma: no cover
        print(
            f"error: alice_state_db not importable ({exc}) — rebuild the "
            "container or add src/alice_state_db to PYTHONPATH.",
            file=sys.stderr,
        )
        return 5

    db_path = args.db_path or None
    # Open RW for every ``deps`` subcommand so ``apply_schema`` runs and
    # the ``item_dependencies`` table exists even on a DB that pre-dates
    # this PR. ``list`` doesn't modify anything but the table-creation
    # side-effect of ``apply_schema`` is what makes the first-ever
    # ``task deps list`` call usable on a brown-field container (the
    # alternative — ``read_only=True`` — would raise "no such table"
    # until the memory worker's derive pass next ran).
    try:
        with connect(db_path=db_path, read_only=False) as conn:
            from alice_state_db import apply_schema

            apply_schema(conn)

            if args.deps_command == "add":
                try:
                    add_dependency(
                        conn,
                        args.item_id,
                        args.depends_on_item_id,
                        kind=args.kind,
                    )
                except DependencyError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 7
                if args.json:
                    print(
                        json.dumps(
                            {
                                "item_id": args.item_id,
                                "depends_on_item_id": args.depends_on_item_id,
                                "kind": args.kind,
                                "status": "added",
                            }
                        )
                    )
                else:
                    print(
                        f"added: {args.item_id} -> "
                        f"{args.depends_on_item_id} (kind={args.kind})"
                    )
                return 0

            if args.deps_command == "list":
                prereqs = blockers_for(conn, args.item_id, kind=args.kind)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "item_id": args.item_id,
                                "kind": args.kind,
                                "depends_on": prereqs,
                            }
                        )
                    )
                    return 0
                if not prereqs:
                    print(
                        f"(no {args.kind} dependencies for {args.item_id})"
                    )
                    return 0
                print(f"{args.item_id} {args.kind}:")
                for prereq in prereqs:
                    print(f"  depends on {prereq}")
                return 0

            if args.deps_command == "remove":
                remove_dependency(
                    conn,
                    args.item_id,
                    args.depends_on_item_id,
                    kind=args.kind,
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "item_id": args.item_id,
                                "depends_on_item_id": args.depends_on_item_id,
                                "kind": args.kind,
                                "status": "removed",
                            }
                        )
                    )
                else:
                    print(
                        f"removed: {args.item_id} -> "
                        f"{args.depends_on_item_id} (kind={args.kind})"
                    )
                return 0
    except DependencyError as exc:
        # Defensive: _validate_kind raises inside remove/add. argparse's
        # choices filter should catch unknown kinds first, but if a
        # caller patches the CLI we still surface a clean error.
        print(f"error: {exc}", file=sys.stderr)
        return 7

    # argparse enforces deps_command is required, so this is unreachable.
    print(
        f"error: unknown deps subcommand {args.deps_command!r}",
        file=sys.stderr,
    )
    return 5


_HANDLERS = {
    "create": _cmd_create,
    "update": _cmd_update,
    "list": _cmd_list,
    "view": _cmd_view,
    "close": _cmd_close,
    "reindex": _cmd_reindex,
    "db": _cmd_db,
    "deps": _cmd_deps,
    "sm": cmd_sm,
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
