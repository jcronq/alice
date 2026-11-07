"""``task sm ...`` subcommand handlers — DB-primary state machine CLI.

This module wires argparse subcommands to :mod:`alice_forge.sm.store`,
giving Jason / speaking / thinking a terminal path for the SM v2
verbs that previously had to travel through `[SM] <verb>` comments on
GitHub. Issue #825 (PR-2 of the #823 "deprecate SM-on-GitHub" chain).

Verb coverage (from #825's "Required interfaces" block):

* ``task sm transition <item_id> <to_state> [--reason TEXT]
  [--actor ACTOR]`` — generic edge.
* ``task sm amend <item_id> --body TEXT [--author NAME]`` —
  append an ``amendments`` row (no state change).
* ``task sm block <item_id> --reason TEXT`` — shorthand → ``sm:blocked``.
* ``task sm unblock <item_id>`` — restore prior state from the
  ``transitions`` tail (sm.store.unblock_to_prior).
* ``task sm select <item_id>`` — shorthand → ``sm:selected``.
* ``task sm reject <item_id> --reason TEXT`` — shorthand → ``sm:rejected``.
* ``task sm done <item_id>`` — shorthand → ``sm:done``.
* ``task sm show <item_id>`` — print current state + last 5 transitions.
* ``task sm dep add|ls`` — item-dependencies passthroughs (issue #833).

Actor defaulting: ``--actor`` defaults to :func:`os.getlogin` narrowed
to ``jason`` / ``alice`` — any other login (common on containers
where ``whoami`` is ``alice`` but could also be ``root`` or
``speaking``) requires an explicit ``--actor`` so the audit trail
carries intent rather than host accident. All writes route through
:mod:`alice_forge.sm.store`; direct DB mutation is forbidden by the
#823 design.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional

from alice_forge.sm.states import SMState
from alice_forge.sm.store import (
    ActorNotTrustedError,
    IllegalTransitionError,
    ItemNotFoundError,
    amend,
    transition,
    unblock_to_prior,
)

#: CLI-level actor narrowing. ``sm.store.ALLOWED_ACTORS`` is wider
#: (includes ``speaking``, ``thinking``, ``dispatcher``, ...) because
#: those are legitimate internal callers. The CLI narrows the default
#: to the human principals so an ad-hoc ``task sm select ...`` from a
#: shell carries the right author without the operator having to
#: remember which service name is current. Pass ``--actor`` explicitly
#: to override.
CLI_DEFAULT_ACTORS: frozenset[str] = frozenset({"jason", "alice"})


def _resolve_actor(explicit: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the actor for an SM write.

    Returns ``(actor, error)``. ``error`` is ``None`` on success; a
    hint string otherwise — the caller prints it to stderr and exits
    non-zero without opening the DB.

    Precedence:
    1. ``--actor`` explicit → verbatim, no narrowing (lets internal
       callers pass ``speaking`` etc; store-side trust check still
       runs).
    2. ``os.getlogin()`` → used iff it's in :data:`CLI_DEFAULT_ACTORS`.
    3. Otherwise, demand ``--actor``.
    """
    if explicit is not None:
        return explicit, None
    try:
        login = os.getlogin()
    except OSError:
        login = os.environ.get("USER") or ""
    if login in CLI_DEFAULT_ACTORS:
        return login, None
    return None, (
        f"error: could not infer actor (login={login!r} not in "
        f"{sorted(CLI_DEFAULT_ACTORS)!r}) — pass --actor to override."
    )


def _parse_state(label: str) -> tuple[Optional[SMState], Optional[str]]:
    """Parse a state label (``sm:selected``) into an :class:`SMState`.

    Returns ``(state, error)``; ``state`` is ``None`` and ``error``
    is a hint string if the label does not round-trip.
    """
    state = SMState.from_label(label)
    if state is None:
        return None, (
            f"error: unknown state {label!r} — expected one of "
            f"{sorted(s.label for s in SMState)!r}."
        )
    return state, None


# ---------------------------------------------------------------------------
# argparse wiring


def add_sm_subparser(sub: argparse._SubParsersAction) -> None:
    """Attach the ``sm`` subcommand group to ``task``'s top-level parser.

    Call site is :func:`alice_forge.task_cli._build_parser`.
    """
    psm = sub.add_parser(
        "sm",
        help=(
            "DB-primary state-machine verbs (issue #825). Replaces "
            "[SM] <verb> comments on GitHub."
        ),
    )
    psm.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override alice-state.db path. Default: "
            "$ALICE_MIND_DIR/inner/state/alice-state.db, else "
            "~/alice-mind/inner/state/alice-state.db."
        ),
    )
    sm_sub = psm.add_subparsers(dest="sm_command", required=True)

    # ---- transition
    pt = sm_sub.add_parser(
        "transition",
        help="Generic edge: transition <item_id> to <to_state>.",
    )
    pt.add_argument("item_id", help="Item surrogate (e.g. gh:jcronq/alice#825).")
    pt.add_argument("to_state", help="Target SM label (e.g. sm:selected).")
    pt.add_argument("--reason", default=None, help="Required for arbitrary transitions.")
    pt.add_argument("--actor", default=None, help="Author of the transition.")

    # ---- amend
    pa = sm_sub.add_parser(
        "amend",
        help="Append an amendments row (no state change).",
    )
    pa.add_argument("item_id", help="Item surrogate.")
    pa.add_argument("--body", required=True, help="Amendment text.")
    pa.add_argument("--author", default=None, help="Author (defaults like --actor).")

    # ---- shorthand verbs
    pb = sm_sub.add_parser("block", help="Shorthand: transition to sm:blocked.")
    pb.add_argument("item_id")
    pb.add_argument("--reason", required=True, help="Why it's blocked.")
    pb.add_argument("--actor", default=None)

    pu = sm_sub.add_parser(
        "unblock",
        help="Restore prior state from the transitions tail.",
    )
    pu.add_argument("item_id")
    pu.add_argument("--reason", default=None)
    pu.add_argument("--actor", default=None)

    psel = sm_sub.add_parser("select", help="Shorthand: transition to sm:selected.")
    psel.add_argument("item_id")
    psel.add_argument("--reason", default=None)
    psel.add_argument("--actor", default=None)

    prej = sm_sub.add_parser("reject", help="Shorthand: transition to sm:rejected.")
    prej.add_argument("item_id")
    prej.add_argument("--reason", required=True, help="Why it's rejected.")
    prej.add_argument("--actor", default=None)

    pdone = sm_sub.add_parser("done", help="Shorthand: transition to sm:done.")
    pdone.add_argument("item_id")
    pdone.add_argument("--reason", default=None)
    pdone.add_argument("--actor", default=None)

    # ---- show
    psh = sm_sub.add_parser(
        "show",
        help="Print current state + last 5 transitions for an item.",
    )
    psh.add_argument("item_id")
    psh.add_argument(
        "--transition-limit", type=int, default=5,
        help="How many recent transitions to include. Default 5.",
    )

    # ---- dep add|ls — thin passthroughs to the issue-#833 helpers so
    # the ``task sm`` namespace feels complete. Edge kinds come from
    # alice_state_db.dependencies.VALID_KINDS.
    pdep = sm_sub.add_parser(
        "dep",
        help="Item dependencies (passthrough to task deps).",
    )
    dep_sub = pdep.add_subparsers(dest="dep_command", required=True)

    dep_add = dep_sub.add_parser("add", help="Add a dependency edge.")
    dep_add.add_argument("item_id", help="The dependent item.")
    dep_add.add_argument(
        "--depends-on", required=True, help="The prerequisite item.",
    )
    dep_add.add_argument(
        "--kind", default="blocks",
        choices=["blocks", "follows", "supersedes"],
        help="Edge kind. Default: blocks.",
    )

    dep_ls = dep_sub.add_parser("ls", help="List direct prerequisites.")
    dep_ls.add_argument("item_id", help="The dependent item.")
    dep_ls.add_argument(
        "--kind", default="blocks",
        choices=["blocks", "follows", "supersedes"],
        help="Edge kind to filter. Default: blocks.",
    )


# ---------------------------------------------------------------------------
# Handlers


def _open_conn(db_path: Optional[str]):
    """Yield an RW connection + apply schema so cold DBs work.

    Mirrors :func:`alice_forge.task_cli._cmd_deps` — reusing a
    private import to avoid a circular top-level dep between the
    forge CLI and the state-db package.
    """
    from alice_state_db import apply_schema, connect

    return connect(db_path=db_path, read_only=False), apply_schema


def _print_transition_result(
    args: argparse.Namespace,
    item_id: str,
    to_state: SMState,
    transition_id: int,
) -> None:
    if args.json:
        print(
            json.dumps(
                {
                    "item_id": item_id,
                    "state": to_state.label,
                    "transition_id": transition_id,
                }
            )
        )
    else:
        print(f"{item_id}: -> {to_state.label} (transition_id={transition_id})")


def _do_transition(
    args: argparse.Namespace,
    item_id: str,
    to_state: SMState,
    *,
    reason: Optional[str],
    require_reason: bool = False,
) -> int:
    actor, err = _resolve_actor(args.actor)
    if err is not None:
        print(err, file=sys.stderr)
        return 8
    if require_reason and not reason:
        print(
            f"error: --reason is required for this transition.",
            file=sys.stderr,
        )
        return 9

    cm, apply_schema = _open_conn(args.db_path)
    with cm as conn:
        apply_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            tid = transition(
                conn=conn,
                item_id=item_id,
                to=to_state,
                actor=actor,
                reason=reason,
            )
        except ActorNotTrustedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 10
        except ItemNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 11
        except IllegalTransitionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 12
    _print_transition_result(args, item_id, to_state, tid)
    return 0


def _cmd_sm_transition(args: argparse.Namespace) -> int:
    state, err = _parse_state(args.to_state)
    if err is not None:
        print(err, file=sys.stderr)
        return 13
    # Note: the issue text frames --reason as "required for arbitrary
    # transition" but acceptance test #1 explicitly omits it
    # (``task sm transition item-X sm:selected --actor jason``). The
    # concrete test wins; reason is encouraged via docstring + help
    # text, not argparse-required. Shorthand ``reject`` / ``block``
    # still require --reason because those verbs' acceptance semantics
    # mandate a rationale.
    return _do_transition(
        args, args.item_id, state,
        reason=args.reason,
        require_reason=False,
    )


def _cmd_sm_block(args: argparse.Namespace) -> int:
    return _do_transition(
        args, args.item_id, SMState.BLOCKED,
        reason=args.reason, require_reason=True,
    )


def _cmd_sm_select(args: argparse.Namespace) -> int:
    return _do_transition(
        args, args.item_id, SMState.SELECTED,
        reason=args.reason, require_reason=False,
    )


def _cmd_sm_reject(args: argparse.Namespace) -> int:
    return _do_transition(
        args, args.item_id, SMState.REJECTED,
        reason=args.reason, require_reason=True,
    )


def _cmd_sm_done(args: argparse.Namespace) -> int:
    return _do_transition(
        args, args.item_id, SMState.DONE,
        reason=args.reason, require_reason=False,
    )


def _cmd_sm_unblock(args: argparse.Namespace) -> int:
    actor, err = _resolve_actor(args.actor)
    if err is not None:
        print(err, file=sys.stderr)
        return 8

    cm, apply_schema = _open_conn(args.db_path)
    with cm as conn:
        apply_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            tid = unblock_to_prior(
                conn=conn,
                item_id=args.item_id,
                actor=actor,
                reason=args.reason,
            )
        except ActorNotTrustedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 10
        except ItemNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 11
        except IllegalTransitionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 12
        # Re-read state to show the restored label.
        row = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (args.item_id,),
        ).fetchone()
        restored_label = row[0] if row is not None else "?"

    if args.json:
        print(
            json.dumps(
                {
                    "item_id": args.item_id,
                    "state": restored_label,
                    "transition_id": tid,
                }
            )
        )
    else:
        print(
            f"{args.item_id}: unblocked -> {restored_label} "
            f"(transition_id={tid})"
        )
    return 0


def _cmd_sm_amend(args: argparse.Namespace) -> int:
    author, err = _resolve_actor(args.author)
    if err is not None:
        print(err, file=sys.stderr)
        return 8

    cm, apply_schema = _open_conn(args.db_path)
    with cm as conn:
        apply_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            aid = amend(
                conn=conn,
                item_id=args.item_id,
                body=args.body,
                author=author,
            )
        except ActorNotTrustedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 10
        except sqlite3.IntegrityError as exc:
            # No source_ref is passed here so a conflict would need a
            # concurrent writer racing on the same partial index —
            # unlikely but worth a clean message rather than a stack.
            print(f"error: amendment integrity conflict: {exc}", file=sys.stderr)
            return 14

    if args.json:
        print(json.dumps({"item_id": args.item_id, "amendment_id": aid}))
    else:
        print(f"{args.item_id}: amendment_id={aid}")
    return 0


def _cmd_sm_show(args: argparse.Namespace) -> int:
    from alice_state_db import connect, item_detail

    # read-only is fine for show; connect() auto-creates an empty DB
    # if one doesn't exist yet, so the first-ever invocation prints a
    # clean "not found" rather than an OSError.
    with connect(db_path=args.db_path, read_only=True) as conn:
        payload = item_detail(
            conn, args.item_id, transition_limit=args.transition_limit
        )
    if payload is None:
        print(f"error: item not found: {args.item_id}", file=sys.stderr)
        return 11

    if args.json:
        print(json.dumps(payload, sort_keys=False, default=str))
        return 0

    state = payload.get("state") or {}
    print(f"# {payload.get('item_id')}: {payload.get('title', '')}")
    print(f"state:       {state.get('state', '(none)')}")
    print(f"entered_at:  {state.get('entered_at', '')}")
    print()
    print(f"## Recent transitions (last {args.transition_limit}, newest-first)")
    transitions_rows = payload.get("recent_transitions") or []
    if not transitions_rows:
        print("  (no transitions)")
        return 0
    for t in transitions_rows:
        frm = t.get("from_state") or "(none)"
        to = t.get("to_state") or "(?)"
        actor = t.get("actor") or "?"
        reason = t.get("reason") or ""
        ts = t.get("ts") or ""
        print(f"  {ts:25s}  {frm:16s} -> {to:16s}  by {actor:10s}  {reason}")
    return 0


def _cmd_sm_dep(args: argparse.Namespace) -> int:
    """Passthrough to the existing ``task deps`` plumbing."""
    from alice_state_db import (
        DependencyError,
        add_dependency,
        apply_schema,
        blockers_for,
        connect,
    )

    with connect(db_path=args.db_path, read_only=False) as conn:
        apply_schema(conn)
        try:
            if args.dep_command == "add":
                add_dependency(
                    conn, args.item_id, args.depends_on, kind=args.kind
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "item_id": args.item_id,
                                "depends_on_item_id": args.depends_on,
                                "kind": args.kind,
                                "status": "added",
                            }
                        )
                    )
                else:
                    print(
                        f"added: {args.item_id} -> "
                        f"{args.depends_on} (kind={args.kind})"
                    )
                return 0

            if args.dep_command == "ls":
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
        except DependencyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 7

    print(
        f"error: unknown dep subcommand {args.dep_command!r}",
        file=sys.stderr,
    )
    return 5


# ---------------------------------------------------------------------------
# Dispatch


_SM_HANDLERS = {
    "transition": _cmd_sm_transition,
    "amend": _cmd_sm_amend,
    "block": _cmd_sm_block,
    "unblock": _cmd_sm_unblock,
    "select": _cmd_sm_select,
    "reject": _cmd_sm_reject,
    "done": _cmd_sm_done,
    "show": _cmd_sm_show,
    "dep": _cmd_sm_dep,
}


def cmd_sm(store, args: argparse.Namespace) -> int:
    """Top-level ``task sm`` dispatcher.

    Signature matches the existing ``_HANDLERS`` table in
    :mod:`alice_forge.task_cli` — the unused ``store`` arg keeps the
    uniform call shape. All ``task sm`` verbs operate on
    ``alice-state.db`` rather than the filesystem task store, so the
    forge-level :class:`TaskStore` isn't touched.
    """
    handler = _SM_HANDLERS.get(args.sm_command)
    if handler is None:
        print(f"error: unknown sm subcommand {args.sm_command!r}", file=sys.stderr)
        return 5
    return handler(args)
