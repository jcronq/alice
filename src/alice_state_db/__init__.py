"""Unified SQLite derived-index over Alice's filesystem state (issue #789).

PR 1 ships the minimum useful shape:

* ``tasks`` — one row per ``inner/tasks/task-NNNN/task.yaml``.
* ``workers`` — one row per background sub-agent handle, derived from
  ``inner/notes/worker-<bg_id>.json`` + ``worker-<bg_id>-done.json``
  dispatch/complete marker pairs that Speaking writes on each spawn.
* ``events`` — tail of ``memory/events.jsonl``, byte-offset
  incremental.
* ``derive_runs`` — one row per derive pass; feeds incremental
  mtime/offset gating on the next call.

The DB is **derived**: files remain source of truth. Blow
``alice-state.db`` away and the next derive pass rebuilds it. Readers
open it ``read_only``; the memory worker is the sole writer.

Later PRs (#789's PR 2 and PR 3) add ``gh_issues``, ``gh_prs``,
``surfaces``, and ``vault_notes`` + FTS. This file deliberately
stops short of those — they're separate dispatches.

Module layout:

* :mod:`alice_state_db.schema` — DDL + ``apply_schema(conn)`` idempotent
  bootstrap.
* :mod:`alice_state_db.derive` — ``derive_tasks_workers_events(mind,
  db_path)`` runs the incremental pass.
* This module — read helpers (:func:`connect`, :func:`tasks_for_issue`,
  :func:`workers_for_task`, :func:`state_of`).
"""

from __future__ import annotations

from .gh_poll import PollReport, derive_gh_poll
from .read_api import (
    DEFAULT_DB_PATH,
    WorkerRow,
    connect,
    events_for_issue,
    events_for_task,
    item_detail,
    item_transitions,
    state_of,
    tasks_for_issue,
    workers_for_bg_id,
    workers_for_task,
)
from .schema import apply_schema
from .sm_seed import (
    SmSeedReport,
    derive_sm_seed,
    gh_issue_id,
    gh_pr_id,
    local_task_id,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "PollReport",
    "SmSeedReport",
    "WorkerRow",
    "apply_schema",
    "connect",
    "derive_gh_poll",
    "derive_sm_seed",
    "events_for_issue",
    "events_for_task",
    "gh_issue_id",
    "gh_pr_id",
    "item_detail",
    "item_transitions",
    "local_task_id",
    "state_of",
    "tasks_for_issue",
    "workers_for_bg_id",
    "workers_for_task",
]
