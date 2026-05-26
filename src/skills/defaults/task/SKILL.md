---
name: task
description: Use when starting a non-trivial new thread (worker dispatch, multi-file investigation, deferred follow-up), changing the status of in-flight work, or answering "what's open" / "what's in flight" / "any tasks tracking X" — operates on the SM v2 task store at ~/alice-mind/inner/tasks/ via the `task` CLI. Don't create a task for pure conversational replies. Don't bypass the CLI by hand-editing files.
metadata:
  scope: both
trigger_keywords: [task, open work, in flight, todo, tracking, thread, lose track, what's open, status, tasks]
---

# task

The SM v2 task tracker. Records every non-trivial thread Alice is
working on so nothing gets lost between Signal turns, dispatcher
surfaces, and PR merges. Backed by `~/alice-mind/inner/tasks/`
(per-task `task.yaml` + `transitions.jsonl`, plus an `index.jsonl`
roll-up).

The state machine and full schema live in
`~/alice-mind/inner/tasks/SCHEMA.md` and
`cortex-memory/research/2026-05-11-idea-task-state-machine-v2.md`.
`task --help` (and per-subcommand `--help`) is the canonical source
for invocation shape.

## When to invoke

**Create a task whenever a new thread is going to outlive the current turn.** Specifically:

- A worker subagent dispatch (auto-fix or otherwise).
- A multi-file investigation Jason asked about that won't finish in
  this reply.
- A deferred follow-up ("look at X later" → log it now, work on it
  later).
- A design surfacing from thinking that Alice has decided to engage
  with.

**Don't create a task for**:

- A simple conversational reply with no side-effect.
- Logging a meal, workout, or weight (those have their own skills).
- Bookkeeping a memory note (use `append_note` for that).

**Update an existing task** when its status changes — worker spawned,
PR opened, validation finished, blocked on Jason, etc.

**Query tasks** when Jason or thinking asks "what's open", "what's in
flight", "any work tracking X", or anything similar. The default
"open" filter excludes done/rejected.

## Operations

The CLI ships at `~/alice-tools/bin/task` (preferred path; check
`bin/task` in the alice repo too) and exposes five subcommands:

```
task create --title TITLE [--tags T,T,...] [--actor speaking|thinking|jason]
            [--artifact-type research_note|code|experiment|config_change]
            [--source SRC] [--reason R]

task update <id> --status draft|selected|building|reviewing|blocked|validating|done|rejected
                 [--actor ACTOR] [--reason R] [--merge-ref REF]
                 [--validation-evidence E] [--unblocked-by U]

task list [--status STATE|open] [--tag TAG] [--actor ACTOR]
task view <id>
task close <id> [--reason R] [--merge-ref REF] [--actor ACTOR]
```

Add `--json` to any subcommand for machine-readable output.

## State machine cheat-sheet

Happy path: `draft → selected → building → reviewing → done`
(research/experiment) or `draft → selected → building → reviewing →
validating → done` (code/config — CI gate). Auto-fix path uses the
self-merge shortcut: `building → done` with `--merge-ref <PR URL>`.

`blocked` requires `--unblocked-by` describing what must change.
`validating → done` requires `--validation-evidence` describing what
was checked.

The full edge list is in `~/alice-mind/inner/tasks/SCHEMA.md`. The
CLI rejects invalid transitions with a clear error.

## Read-back via Signal

Jason or thinking asking "what tasks are open" / "what's in flight"
should trigger:

```
task list --status open
```

Then summarise the result back. Keep it terse — `task-NNNN  status
title` per line is enough unless he asks for detail.

## Don't

- Don't hand-edit files under `inner/tasks/` — the CLI keeps
  `task.yaml`, `transitions.jsonl`, and `index.jsonl` in sync via a
  file lock. Direct edits will drift.
- Don't create a task and immediately close it in the same turn just
  because it's done. If the thread doesn't outlive the turn, it
  doesn't need a task.
- Don't open a draft transition without a reason. The transitions
  log is the audit trail; an empty `reason` makes it useless.
