# Thinker Watchdog — Phase 1

## Why this exists

The thinking process has hung twice in 24 hours under the same pattern:
the Python wake completes its logical work (writes the wake summary to
`inner/thoughts/<date>/HHMMSS-wake.md`), but the process never exits —
`/proc/<pid>/wchan` reports `ep_poll`, the kernel-SDK stream transport
has leaked a socket fd. While the process clings to
`/state/worker/thinking.lock`, the s6 supervisor's next `flock -n`
skips, and the cadence stalls silently. Yesterday: 21 minutes before
manual intervention. Today: ~30 minutes. No alerting, no recovery, just
a quiet cessation of thinking.

Phase 1 is the deterministic floor: a separate s6 longrun
(`alice-thinker-watchdog`) ticks every 30s, applies five rules, and
either kills the hung process (so s6 can start a fresh wake on the next
cadence) or clears an orphan lock file. No model in the loop. No
heuristics that risk killing a wake that's still doing real work.

Phase 2 will layer an LLM judgment call on top of the deterministic
rules for the gray cases. Hooks are reserved (the `Decision` enum, the
intervention return shape) but no model call ships in this PR.

## Layout

| Piece | Path |
|-------|------|
| Watchdog logic | `src/alice_thinking/watchdog.py` |
| CLI shim       | `bin/alice-thinker-watchdog` (copied to `/usr/local/bin/`) |
| s6 longrun     | `sandbox/worker/s6/alice-thinker-watchdog/` |
| Tests          | `tests/test_thinker_watchdog.py` |

The watchdog is purely additive. It does not manage the thinker. It
does not modify `wake.py`, the modes, the phase router, or the
existing s6 supervisor. It reads `/proc/locks`, kills hung processes,
and emits to `memory/events.jsonl`.

## Decision rules

One tick reads four signals:

1. **The flock holder.** Stat `/state/worker/thinking.lock` for its
   inode, then walk `/proc/locks` for a FLOCK ADVISORY WRITE entry on
   that inode. The PID we find is the thinker (the supervisor's
   `flock` invocation exec's the python wrapper, so the holder is the
   python process directly).
2. **The wake-file mtime.** Most recent file under
   `inner/thoughts/<today>/*.md`. `wake.py` writes this at logical-work
   completion — it's the heartbeat.
3. **The supervisor's interval file.** `/state/worker/next-thinking-interval-seconds`
   carries the cadence (5/10/20/40 min in sleep with backoff, 5 min in
   active). Fallback: 600s.
4. **The lock file's existence.** Set up by the supervisor at startup.

Five branches:

| Holder PID | Wake age | Lock file | Decision |
|-----------|----------|-----------|----------|
| None       | —        | absent    | `IDLE` — nothing to do |
| None       | —        | present   | `ORPHAN_LOCK` — clear the file |
| Live       | <M       | present   | `WORKING` — leave it alone |
| Live       | ≥M       | present   | `STUCK` — SIGTERM, then SIGKILL |
| Live       | (no wake file) | present | `WORKING` if process is young, else `STUCK` |

The "no wake file yet" branch keys off `/proc/<pid>` mtime — Linux
sets the dirent at fork, so it's a reliable wall-clock age. A process
that's been running longer than M without producing a wake file is
hung in early init, which has the same recovery as a late-cleanup
hang.

## Staleness threshold M

```
M = 5 × current_cadence + 60
```

| Cadence            | M       | Notes |
|--------------------|---------|-------|
| 300s (active 5min) | 1560s   | 26 min — covers native Stage B workflow runs |
| 600s (fallback)    | 3060s   | 51 min |
| 1200s (sleep 20min)| 6060s   | 101 min |
| 2400s (sleep 40min)| 12060s  | 201 min — generous for Stage D synthesis |

The factor of 5 was chosen so a native workflow (`alice_thinking.workflows.stage_b`)
with LLM subroutines that legitimately fan out to 15-20 minutes
doesn't get false-positive killed. The earlier 2x value (660s active)
was sized for prompt-driven wakes only; once the typed workflow
shipped (PR #19), 660s started clipping healthy runs.

Phase 2 (per-step heartbeat in the workflow) is the structural fix
that lets us tighten this back down — when each step touches a
heartbeat file, the watchdog can detect "no progress in N seconds"
much sooner than "no completed wake file in N seconds." Until that
ships, 5x is the safe floor — still beats the 21-minute manual
intervention windows we hit before the watchdog existed.

The `+60s` cushion absorbs filesystem mtime granularity and clock-skew
artifacts.

A **failure mode this avoids**: the supervisor's interval file gets
stomped to `60` (corrupt write), M becomes 180, watchdog murders a
healthy 4-minute Stage C wake. Mitigation: `current_cadence_seconds()`
falls back to 600 on any non-positive / unparseable read.

## Intervention

```
SIGTERM → wait 30s polling /proc/<pid> → SIGKILL if still alive.
```

30s mirrors the s6 standard for graceful shutdown — long enough for
Python's atexit/finally chains, short enough that a hung process
doesn't keep blocking the cadence past one wake interval. We poll
`/proc/<pid>` (not `kill 0`) because the watchdog runs as a different
uid than the thinker; the proc-dir check is permission-free.

Telemetry written to `memory/events.jsonl`:

```json
{
  "ts": 1736355600.0,
  "event": "thinker_watchdog_intervention",
  "pid": 12345,
  "wchan": "ep_poll",
  "wake_file": "/home/alice/alice-mind/inner/thoughts/2026-05-08/120000-wake.md",
  "wake_file_age_seconds": 970.4,
  "cadence_seconds": 300,
  "stale_threshold_seconds": 1560,
  "sigterm_sent": true,
  "sigterm_sufficient": false,
  "sigkill_sent": true,
  "elapsed_seconds": 30.0,
  "exited": true
}
```

`sigterm_sufficient: false` is the headline alert — Phase 2's LLM
judgment loop will key off that signal to decide whether the SDK leak
needs an upstream bug report.

Orphan-lock clears emit a separate `thinker_watchdog_orphan_lock`
event so dashboards don't conflate "we killed something" with "we
deleted a stale file".

## Why we don't touch wake.py

The spec allowed adding heartbeat-touch hooks at logical-work
checkpoints inside `wake.py` if it was a 1-2 line change. It's not —
the wake's logical-work surface is spread across the active mode,
sleep modes, design pipeline, conflict resolution, and the kernel
adapter. Each would need its own touch call, and any drift in the
list would manifest as false-positive intervention.

The wake-file mtime is sufficient. `wake.py` already writes that file
at the right moment (logical-work completion), and the watchdog's M
already absorbs the latency between "thinking done" and "file
flushed".

## Phase 2 — LLM judgment hooks

The deterministic rules cannot distinguish "hung" from "slow but
working" without knowing what the wake is doing. Phase 2 layers a
local-model call — given the wake-file contents, the wchan, the
process age, and the recent event log — to ask "is this still
working?" before escalating to SIGTERM.

Reserved hooks:

* `Decision` is a string enum so adding `Decision.SOFT_STUCK` is
  additive, not breaking.
* `intervene()`'s return dict has room for a `judgment` field.
* `run_tick()` is the single chokepoint where the LLM call slots in,
  between `is_stuck()` and `intervene()`.

Phase 2 ships in a separate PR. Wiring it in requires the local-model
sidecar (Strix Halo) to be reachable from the worker container, which
is its own infra task.

## Operational notes

* The watchdog runs as the `alice` user (s6-setuidgid in the run
  script), same uid as the thinker. SIGTERM/SIGKILL on a same-uid
  process never hits permission errors.
* The 30s tick is fixed — explicit per spec. Don't replace with
  inotify; the watchdog must fire even when nothing changes on disk
  (a hung process holding a stale wake file is exactly that case).
* If the watchdog itself crashes, s6 restarts the longrun. The
  `main()` exit code is non-zero only on unexpected exceptions, so
  a normal "stuck-and-handled" tick is exit 0.
* The watchdog never raises on observability writes — `memory/events.jsonl`
  on a full disk just becomes a no-op append. The recovery path
  must not depend on logging working.
