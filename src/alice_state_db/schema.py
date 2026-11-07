"""DDL for the PR 1 subset of ``alice-state.db``.

Only four tables land in this PR:

* ``tasks`` — derived from ``inner/tasks/task-*/task.yaml``.
* ``workers`` — derived from ``inner/notes/worker-*.json`` pairs.
* ``events`` — tail of ``memory/events.jsonl``.
* ``derive_runs`` — bookkeeping for the derive step.

The full schema sketch in issue #789 lists ``notes``, ``surfaces``,
``vault_notes``, ``gh_issues``, ``gh_prs``, and FTS virtual tables.
Those are intentionally NOT created here — later PRs own them. This
keeps the PR 1 wire shape tight and the idempotency surface small.

``apply_schema`` is idempotent: every ``CREATE`` uses ``IF NOT
EXISTS`` so running derive twice (or against an existing file)
never errors.
"""

from __future__ import annotations

import sqlite3

# Column choices follow the design sketch in issue #789 closely but
# stay a strict subset — nothing outside PR 1 scope. Keep these as
# string constants so other modules can introspect (``task db ddl``
# in a later PR will dump them verbatim).

DDL_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,
    actor           TEXT,
    artifact_type   TEXT,
    source          TEXT,
    gh_repo         TEXT,
    gh_issue_number INTEGER,
    created         TEXT,
    updated         TEXT,
    tags_json       TEXT NOT NULL DEFAULT '[]',
    task_yaml_path  TEXT NOT NULL,
    task_dir_path   TEXT NOT NULL,
    mtime_ns        INTEGER NOT NULL
);
"""

DDL_TASKS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_gh ON tasks(gh_repo, gh_issue_number);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated);",
]

DDL_WORKERS = """
CREATE TABLE IF NOT EXISTS workers (
    bg_id           TEXT PRIMARY KEY,
    kind            TEXT,
    label           TEXT,
    description     TEXT,
    repo            TEXT,
    gh_issue        INTEGER,
    task_id         TEXT,
    surface_path    TEXT,
    pr_url          TEXT,
    commit_sha      TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    outcome         TEXT,
    dispatch_path   TEXT NOT NULL,
    done_path       TEXT,
    stdout_path     TEXT,
    worktree_path   TEXT,
    mtime_ns        INTEGER NOT NULL
);
"""

DDL_WORKERS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_workers_task ON workers(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_workers_gh ON workers(repo, gh_issue);",
    "CREATE INDEX IF NOT EXISTS idx_workers_outcome ON workers(outcome);",
    "CREATE INDEX IF NOT EXISTS idx_workers_started ON workers(started_at);",
]

DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    line_id         INTEGER PRIMARY KEY,
    byte_offset     INTEGER NOT NULL,
    ts              TEXT,
    type            TEXT,
    subject         TEXT,
    source          TEXT,
    repo            TEXT,
    gh_issue_number INTEGER,
    pr_number       INTEGER,
    task_id         TEXT,
    body_json       TEXT NOT NULL
);
"""

DDL_EVENTS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);",
    "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);",
    "CREATE INDEX IF NOT EXISTS idx_events_gh ON events(repo, gh_issue_number);",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);",
]

DDL_DERIVE_RUNS = """
CREATE TABLE IF NOT EXISTS derive_runs (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    step            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    rows_touched    INTEGER NOT NULL DEFAULT 0,
    tasks_upserted  INTEGER NOT NULL DEFAULT 0,
    workers_upserted INTEGER NOT NULL DEFAULT 0,
    events_appended INTEGER NOT NULL DEFAULT 0,
    last_event_offset INTEGER NOT NULL DEFAULT 0,
    errors_json     TEXT NOT NULL DEFAULT '[]'
);
"""

DDL_DERIVE_RUNS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_derive_runs_step ON derive_runs(step, started_at);",
]


# ---------------------------------------------------------------------------
# SM v2 substrate tables (issue #790, Step 1)
#
# The design in #790 adds a unified item + state-machine + transitions
# ledger on top of the PR-1 ``tasks``/``workers``/``events`` surface.
# Step 1 (this PR) only ADDS these tables and SEEDS them from existing
# GH mirror files + ``inner/tasks/`` — no reader flips, no GH writes.
#
# * ``items`` — unified rows across (``gh:issue``, ``gh:pr``,
#   ``local:task``, ``local:todo``, ``vault:note``). ``item_id`` is the
#   human-readable surrogate (``gh:jcronq/alice#790``, ``local:task-0760``);
#   natural keys stay greppable, which was the single loudest #790
#   open question.
# * ``item_external_refs`` — zero-or-many handles per item (GH PR link,
#   worker bg-id, vault slug). Composite PK keeps seed idempotent.
# * ``state_machine`` — current state per item, one row.
# * ``transitions`` — append-only history; FK back from
#   ``state_machine.last_transition_id`` so readers can grab the
#   latest audit entry in one join.
# * ``amendments`` — comment / note updates. Reserved for Step 2's
#   two-way sync; Step 1 writes them from existing GH comments when
#   a fetcher is wired, otherwise the table stays empty.

DDL_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    item_id         TEXT PRIMARY KEY,
    item_type       TEXT NOT NULL,
    title           TEXT NOT NULL,
    created_at      TEXT,
    updated_at      TEXT,
    author          TEXT,
    summary         TEXT,
    source_path     TEXT
);
"""

DDL_ITEMS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type);",
    "CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);",
]

DDL_ITEM_EXTERNAL_REFS = """
CREATE TABLE IF NOT EXISTS item_external_refs (
    item_id             TEXT NOT NULL,
    ref_type            TEXT NOT NULL,
    ref_target_item_id  TEXT NOT NULL,
    ref_source          TEXT,
    created_at          TEXT,
    PRIMARY KEY (item_id, ref_type, ref_target_item_id),
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
"""

DDL_ITEM_EXTERNAL_REFS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_refs_target ON item_external_refs(ref_target_item_id);",
    "CREATE INDEX IF NOT EXISTS idx_refs_type ON item_external_refs(ref_type);",
]

DDL_STATE_MACHINE = """
CREATE TABLE IF NOT EXISTS state_machine (
    item_id             TEXT PRIMARY KEY,
    state               TEXT NOT NULL,
    entered_at          TEXT,
    last_transition_id  INTEGER,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
"""

DDL_STATE_MACHINE_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_sm_state ON state_machine(state);",
]

DDL_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS transitions (
    transition_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    actor           TEXT,
    reason          TEXT,
    ts              TEXT,
    source          TEXT,
    source_ref      TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
"""

# ``source_ref`` is a free-form string carrying an identity that makes
# a transition unique within its emission source — a GH comment id,
# the originating task's transitions.jsonl line hash, etc. Paired with
# ``item_id`` + ``ts`` it gives the seeder an idempotent dedup key.
DDL_TRANSITIONS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_transitions_item ON transitions(item_id, ts);",
    "CREATE INDEX IF NOT EXISTS idx_transitions_source ON transitions(source);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_transitions_source_ref ON transitions(item_id, source, source_ref) WHERE source_ref IS NOT NULL;",
]

DDL_AMENDMENTS = """
CREATE TABLE IF NOT EXISTS amendments (
    amendment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL,
    author          TEXT,
    body            TEXT NOT NULL,
    ts              TEXT,
    source          TEXT,
    source_ref      TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE
);
"""

DDL_AMENDMENTS_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_amendments_item ON amendments(item_id, ts);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_amendments_source_ref ON amendments(item_id, source, source_ref) WHERE source_ref IS NOT NULL;",
]


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create the PR 1 tables + indexes idempotently.

    Called on every open from both the derive pass and the read
    helpers — cheap enough to run unconditionally and means a
    freshly-deleted DB is auto-re-created the next time anyone
    touches it. WAL mode is set here too so concurrent readers never
    block the single writer (the memory worker's derive step).
    """
    # WAL + a few pragma tunings. ``synchronous=NORMAL`` is safe under
    # WAL and is ~3x faster than FULL for the write-heavy derive pass;
    # the DB is derived anyway so a torn write on power loss is
    # recovered by the next derive run.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute(DDL_TASKS)
    for stmt in DDL_TASKS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_WORKERS)
    for stmt in DDL_WORKERS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_EVENTS)
    for stmt in DDL_EVENTS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_DERIVE_RUNS)
    for stmt in DDL_DERIVE_RUNS_INDEX:
        conn.execute(stmt)

    # SM v2 substrate (issue #790 Step 1 — ADD + SEED only)
    conn.execute(DDL_ITEMS)
    for stmt in DDL_ITEMS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_ITEM_EXTERNAL_REFS)
    for stmt in DDL_ITEM_EXTERNAL_REFS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_STATE_MACHINE)
    for stmt in DDL_STATE_MACHINE_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_TRANSITIONS)
    for stmt in DDL_TRANSITIONS_INDEX:
        conn.execute(stmt)
    conn.execute(DDL_AMENDMENTS)
    for stmt in DDL_AMENDMENTS_INDEX:
        conn.execute(stmt)

    conn.commit()
