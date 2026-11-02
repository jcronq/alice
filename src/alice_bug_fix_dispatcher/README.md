# alice_bug_fix_dispatcher

Auto-remediation pipeline for in-game bug reports from Modern MUD.
A `/bug` report in staging ends up as a `bug-<N>.md` detail file in
`inner/status/`; this dispatcher is the second half of the pipeline —
it spawns a Claude worker, tracks the resulting draft PR, and flips
the bug file's frontmatter through the lifecycle.

## Lifecycle

A `bug-<N>.md` file's `status:` frontmatter field moves through:

| status            | meaning                                                        | written by     |
|-------------------|----------------------------------------------------------------|----------------|
| `open`            | Fresh report. Awaiting dispatch.                               | ingress / dispatcher (on retry) |
| `in-progress`     | Worker dispatched. `dispatched_at` + `worker_task_id` set.     | dispatcher     |
| `in-progress` + `pr_url` | Worker opened a draft PR. Dispatcher is polling it.     | dispatcher     |
| `fixed`           | PR merged. `fix_commit` + `fix_pr` set.                        | dispatcher     |
| `dispatch-failed` | Worker never produced a PR within the stall TTL. Needs triage. | dispatcher     |
| `wontfix`         | Jason decided it isn't worth fixing. Edit by hand.             | human          |

The frontmatter fields the dispatcher reads/writes:

| field                       | written when                 | notes                                            |
|-----------------------------|------------------------------|--------------------------------------------------|
| `status`                    | every transition             | `open` → `in-progress` → `fixed`/`dispatch-failed` |
| `dispatched_at`             | on worker spawn              | UTC ISO. Idempotency key — if set, no re-spawn. |
| `worker_task_id`            | on worker spawn              | `pid:<N>` of the detached `claude -p` process.  |
| `pr_url`                    | after the worker emits it    | adopted from the worker's `result.json`.         |
| `last_check_at`             | each PR poll                 | UTC ISO. Just a heartbeat for the operator.      |
| `fix_commit`                | when PR merges               | the merge-commit SHA.                            |
| `fix_pr`                    | when PR merges               | the final PR URL.                                |
| `last_dispatch_failed_at`   | PR closed-unmerged OR stall  | UTC ISO.                                         |
| `last_failed_pr`            | PR closed-unmerged           | the URL of the attempt that didn't land.         |

Writes are atomic (`tempfile` + `os.replace`) and unknown fields are
preserved — Jason can hand-edit a bug file (adding a `summary`, a
`wontfix` reason, etc.) without the dispatcher clobbering his
changes on the next tick.

## Pause gate — ships paused

The dispatcher **ships paused**. Jason flips it live after signing
off on Wave A of the Howl content pack. The pause mechanism has two
inputs; the dispatcher is paused if **either** is active:

1. **Sentinel file** at `/home/alice/alice-mind/inner/status/.bug-fix-dispatcher-paused`.
   Create it to pause:
   ```
   touch /home/alice/alice-mind/inner/status/.bug-fix-dispatcher-paused
   ```
   Delete it to resume:
   ```
   rm /home/alice/alice-mind/inner/status/.bug-fix-dispatcher-paused
   ```
2. **Config flag** in `~/alice-mind/config/alice.config.json`:
   ```json
   { "bug_fix_dispatcher": { "wave_a_signoff_pending": true } }
   ```

The sentinel is the usual mechanism (operator-friendly, no JSON
edit). The config flag exists so a deploy that ships the sentinel-
delete can still keep the service paused via config, matching the
same pattern used by `memory_worker.enabled`.

When paused the dispatcher writes a single journal line per tick
and returns — no scan, no worker spawn, no PR polls.

## Design choice — detached workers

The dispatcher spawns workers as **detached** `claude -p` processes
(`start_new_session=True`), *not* synchronously. The reasons:

- A long-running sync worker (dialogue-fix workers routinely take
  10-20 minutes) would hold the s6 service's `flock` and block
  *every* other transition — PR polls, stall sweeps, new-bug
  dispatches — for the duration. The whole pipeline would move at
  the speed of its slowest worker.
- Detaching decouples the tick cadence from worker duration, so a
  single flaky worker can't wedge the service.
- The idempotency key (`dispatched_at` set) is read on every tick,
  so even a crashed dispatcher doesn't double-dispatch a worker.
- The spec acknowledged this trade-off and offered sync as the
  "simpler" option; the extra wire-up here is a small Popen call +
  reading `result.json` on the next tick, and it buys us the
  parallelism and resilience without the slot-blocking downside.

Worker output lands in `/state/worker/bug-fix-worker/bug-<N>/`:

- `prompt.md` — the substituted prompt the worker ran against
- `result.json` — the worker's final JSON (parsed on next tick to
  extract `pr_url`)
- `stderr.log` — worker stderr

## Stall + retry

A dispatched worker that produces no PR within the stall TTL
(default 2h, override via `--stall-hours`) is transitioned to
`dispatch-failed` on the next tick. The bug stays visible under the
"Dispatch failed (needs triage)" section of the status site's Bugs
tab so Jason can see which ones need manual attention. The
dispatcher never silently retries — a bug in `dispatch-failed` only
re-enters the pipeline when a human edits its `status:` back to
`open` (and clears `dispatched_at` so the idempotency gate re-opens).

A PR that lands closed-unmerged (Jason killed it, or the worker's
fix was wrong) *does* get retried: the dispatcher reverts the bug to
`status: open` and drops the `dispatched_at` / `worker_task_id` /
`pr_url` fields, so the next tick re-dispatches a fresh worker.
`last_dispatch_failed_at` + `last_failed_pr` stay on the file so the
previous attempt is visible to the next worker and to Jason.

## Recovering a `dispatch-failed` bug

1. Look at the dispatcher log for the bug's attempt:
   `grep "bug-<N>" /home/alice/alice-mind/inner/state/bug-fix-dispatcher.log`
2. Look at the worker scratch dir:
   `/state/worker/bug-fix-worker/bug-<N>/`. `prompt.md` is what the
   worker got; `stderr.log` is what it printed; `result.json` is
   what came back (or empty if the worker crashed).
3. To re-dispatch after resolving the underlying issue, edit the
   bug file:
   - Set `status: open`
   - Remove `dispatched_at`, `worker_task_id` and any `pr_url` /
     `last_dispatch_failed_at`.
   - Save. The next tick picks it up.

## Draft-only invariant

The dispatcher **never** passes `--draft`-ish flags to anything.
The invariant lives in exactly one place: the worker prompt
template at
`~/alice-mind/cortex-memory/reference/mud-bug-fix-worker-prompt.md`,
specifically in the literal `gh pr create --draft …` command the
worker runs verbatim (same pattern as the GitHub issue auto-fix
template). This way the "always draft, never auto-merge" rule
can't drift through this dispatcher layer.

## Running one tick

```
# From the alice container
python -m alice_bug_fix_dispatcher
```

Or via the s6-managed service, which runs on a 30s cadence:

```
# host-side (aimax1)
docker exec alice s6-rc -u change alice-bug-fix-dispatcher   # start
docker exec alice s6-rc -d change alice-bug-fix-dispatcher   # stop
docker exec alice s6-svstat /run/service/alice-bug-fix-dispatcher
```

## Tests

```
cd /home/alice/alice
.venv/bin/pytest tests/test_bug_fix_dispatcher.py
```
