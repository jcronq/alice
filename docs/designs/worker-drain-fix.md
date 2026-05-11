# Worker Drain Fix — Unified Outer-Timeout / Liveness Guard

**Status:** design — awaiting Jason's call on whether to commission the structural work.
**Scope:** both hemispheres (speaking daemon + thinking wake). The two manifestations are the same failure class and want the same structural answer.
**Related:**
- `cortex-memory/research/2026-05-11-alice-worker-drain-hang.md` — today's manifestation (speaking).
- `cortex-memory/research/2026-05-07-thinking-wake-hang-failure-mode.md` — the May 7 manifestation (thinking).
- `cortex-memory/research/2026-05-07-wake-robustness-outer-timeout-design.md` — the prior design that addressed thinking-side only. This doc subsumes it and extends to speaking.
- `docs/designs/thinker-watchdog.md` — the partial recovery layer that did land (kills hung wakes after `5×cadence+60s`).

## Problem

The worker container has two long-lived Python processes — the speaking daemon (one process, lifetime = container lifetime, holds `/state/worker/lease`) and the thinking wake (one process per cadence tick, holds `/state/worker/thinking.lock` for the duration of the wake). Both have been observed reaching the same wedge:

1. The logical work completes cleanly (drain finishes / wake finishes, terminal log line prints).
2. The Python interpreter does not exit. `wchan: ep_poll`, single thread, RSS quiet, holding the flock.
3. Nothing else can run on that hemisphere until somebody intervenes.

The May 7 thinking-side write-up tracked this to a likely leaked socket fd in the kernel SDK's stream transport — when the SDK's `claude` CLI subprocess hands back control after a heavy turn, *something* in the stream-json transport teardown doesn't close, and the interpreter sits in a final `epoll_wait` waiting on a fd that will never fire.

The 2026-05-11 speaking-side manifestation is the same shape:

- Blue's drain reached "drain complete" (the log line at `src/alice_speaking/daemon.py:820`).
- Blue's Python did not exit.
- `s6-rc change-state` blocked. Green blocked on `flock -x /state/worker/lease`. Deploy stalled.
- `docker stop --time=10` (SIGKILL via the engine) cleared it.

Pre-existing bug — blue was running pre-deploy code, so the 1B deploy didn't introduce it. It's been there at least since the speaking daemon was rewritten as a Python process; we just haven't noticed because `bin/alice-deploy`'s detached cleanup job (`detach_old_container_cleanup`, 30-minute SIGKILL fallback) silently masks it on every deploy that hits it.

## Why the existing mitigations are insufficient

| Layer | Behavior | Why it isn't enough |
|---|---|---|
| `S6_KILL_GRACETIME=1800000` (30 min) | s6 waits 30 min after SIGTERM before SIGKILL. | The 30-min ceiling is there because a legitimate drain (in-flight Claude turn + wake) can take minutes; trading off correctness vs. fast recovery. With no shorter trigger, every drain-hang costs up to 30 min of stalled cadence/blocked deploy. |
| `bin/alice-deploy detach_old_container_cleanup` (`CLEANUP_TIMEOUT=1800`) | Background subshell waits up to 30 min for the old container to exit, then `docker rm -f`. | (a) **Masks the bug** — most deploys appear clean because this fires before anyone notices. (b) Only triggers on deploy. A drain-hang outside a deploy (which can't happen for speaking since drain only runs on SIGTERM, but *can* happen for thinking on every cadence tick) gets no help here. |
| `alice-thinker-watchdog` (s6 longrun, 30 s tick) | Detects `stuck` thinking wakes by lock-holder + wake-file mtime; SIGTERM, then SIGKILL after 30 s. | Thinking only. Multiplier was bumped 2× → 5× to cover legitimate Stage B workflow runs (`memory/events.jsonl`-confirmed false positives), so active-mode threshold is now 26 min. Better than nothing but the recovery window is large. Speaking has no equivalent. |
| In-daemon drain timeout (`ALICE_SPEAKING_DRAIN_TIMEOUT` env, `daemon.py:770-805`) | Bounds how long the daemon will wait for `_queue.join()` and the in-flight turn during drain. | Governs the *inside* of the drain loop only. After "drain complete" prints (line 820), the finally block runs, `asyncio.run` tears down the event loop, the interpreter exits — and *that* tail is what's hanging. The in-daemon timeout cannot catch a hang in the interpreter's own shutdown. |

The May 7 design says it explicitly: "No `signal` handler or `atexit` in Python can help if Python itself is stuck in `epoll_wait`. The timeout must be at the process-orchestration layer."

## Root cause: what's actually leaking

Honest answer: not confirmed for either hemisphere. The strongest evidence is May 7's diagnostics — `/proc/<pid>/wchan: ep_poll`, threads 1, a socket fd open with no enumerated peer. Plausible suspects, ranked:

1. **Kernel SDK's `claude` CLI subprocess transport.** `claude_agent_sdk._internal.transport.SubprocessCLITransport` spawns a Node.js subprocess (`/usr/local/bin/claude`) and pipes stdio. If the subprocess doesn't EOF its stdout/stderr at shutdown, the SDK's `Query._read_messages` task never exits naturally, and the async generator in `query()`'s `finally: await query.close()` may stall on `transport.close()` if the child doesn't honor it. The MCP control protocol (hooks, can_use_tool, sdk_mcp_servers) keeps additional control-request handler tasks alive that have to be cancelled at teardown — `Query.close` does cancel `_child_tasks` and `_read_task` (`claude_agent_sdk/_internal/query.py:856`), but a wedge there would manifest exactly as described.

2. **`httpx.AsyncClient` leftover sockets in `SignalRPC`.** `infra/signal_rpc.py:80` opens an `httpx.AsyncClient` for signal-cli's JSON-RPC. `aclose()` is called in `daemon.py`'s finally, but if a `start_typing` heartbeat task fired a request that's still in flight at SIGTERM, the cancellation path through `httpx` has been observed to leak pool sockets in the wild (anyio/httpx interaction with cancel scopes). Lower probability — the cancel path looks correct on inspection.

3. **Discord transport teardown.** `discord.py`'s `client.close()` has historically been a source of `task was destroyed but it is pending!` warnings and occasional hangs; pinned dep is `>=2.4`. Not in play for thinking, only speaking.

4. **MCP server child tasks.** Speaking builds 6+ MCP servers via `tools_module.build`. The SDK runs SDK-type servers (`type=sdk`) in-process; STDIO/HTTP types would fork. Right now everything is `type=sdk` (a quick grep confirms no stdio servers), so this is unlikely the proximate cause but worth ruling out.

5. **The fire-and-forget `cue_runner._spawn_bump` task** (`retrieval/cue_runner.py:744`). Untracked, not awaited at drain. `asyncio.run` will cancel it during loop close, but if the body's inside a `tokio`-style transport, cancellation may not free the fd. Probably noise — would manifest more often given how frequently cue lookups run.

We could probably narrow this down with `py-spy dump` + `strace -p` on a wedged worker. The May 7 note's Investigation Plan calls that out. We have not done it yet because (a) the hang is intermittent — heavy-work-correlated, not deterministic — and (b) the deploy mask has kept the urgency low.

**Implication for the design:** we don't need the root cause to ship the structural floor. The structural floor is the same regardless of which fd is leaking, and ships independently of root-cause work.

## Design

Three layers, smallest-first. Each ships independently; later layers are additive.

### Layer 1 — Bounded drain at the s6 level

The structural answer for both hemispheres is "the process-orchestration layer enforces a maximum drain time, then escalates to SIGKILL." Two concrete changes:

#### Speaking — bash drain timeout in `sandbox/worker/s6/alice-speaking/run`

The existing inner wait loop:

```bash
while :; do
    rc=0
    wait "$child" 2>/dev/null || rc=$?
    kill -0 "$child" 2>/dev/null || break
done
```

waits indefinitely. Replace with a bounded variant that, *once `shutdown` is set*, gives Python a hard wall-clock budget to exit before SIGKILL:

```bash
on_signal() {
    shutdown=1
    drain_deadline=$(( $(date +%s) + ${ALICE_SPEAKING_DRAIN_GRACE:-600} ))
    if [[ -n "$child" ]]; then
        kill -TERM "$child" 2>/dev/null || true
    fi
}

# Inside the wait loop, once shutdown is set:
while :; do
    rc=0
    wait "$child" 2>/dev/null || rc=$?
    kill -0 "$child" 2>/dev/null || break
    if [[ -n "$shutdown" ]] && (( $(date +%s) > drain_deadline )); then
        log "drain deadline exceeded; SIGKILL → $child"
        kill -KILL "$child" 2>/dev/null || true
    fi
done
```

`ALICE_SPEAKING_DRAIN_GRACE` defaults to 600s (10 min) — comfortably above the observed clean-drain budget (`ALICE_SPEAKING_DRAIN_TIMEOUT` defaults already bound the in-loop work, and 10 min is well inside the existing 30-min `S6_KILL_GRACETIME` ceiling). Overridable per-deploy via env so `alice-deploy worker` can pass a tighter budget if a deploy has higher urgency.

Resulting behavior: the longest a drain-hang can block the lease is `ALICE_SPEAKING_DRAIN_GRACE` seconds (down from 30 min). `bin/alice-deploy`'s `detach_old_container_cleanup` becomes a backstop instead of the primary recovery mechanism.

#### Thinking — `timeout --kill-after` wrapping `alice-think`

Apply Phase 1 of `2026-05-07-wake-robustness-outer-timeout-design.md` essentially verbatim:

```bash
# bin/alice-think — new wrapper, replacing the current exec
exec /usr/bin/timeout --kill-after="${ALICE_THINK_GRACE:-60}" \
    "${ALICE_THINK_MAX_SECONDS:-1800}" \
    "$VENV_PY" -m alice_thinking "$@"
```

`ALICE_THINK_MAX_SECONDS=1800` (30 min) is the wall-clock budget for one wake. SIGTERM at the budget; SIGKILL 60s later. The s6 supervisor's `flock` releases when the timeout-wrapped subprocess exits.

This **does not replace** `alice-thinker-watchdog` — the watchdog still catches the cases where the supervisor's own loop wedges before the python invocation, and it still emits structured `thinker_watchdog_intervention` events into `memory/events.jsonl` for diagnosis. With the outer timeout in place, the watchdog's 5× cadence multiplier can probably tighten back down (a stuck wake is now bounded by `ALICE_THINK_MAX_SECONDS`), but that's a follow-on tuning question.

**Risk:** none beyond the existing 30-min ceiling. The budgets are loose enough that legitimate wakes/drains never trip them.

### Layer 2 — Speaking-side watchdog (parity with thinking)

Add `alice-speaker-watchdog` — an s6 longrun analogous to `alice-thinker-watchdog`. Ticks every 30s. Rules:

| Lease held? | Speaking process alive? | Last `daemon_ready` or activity heartbeat | Decision |
|---|---|---|---|
| No | No | — | `IDLE` |
| Yes | No | — | `ORPHAN_LEASE` — clear the lock file (rare, recovery from a half-exited daemon) |
| Yes | Yes | within `M` | `WORKING` |
| Yes | Yes | beyond `M` | `STUCK` — SIGTERM, then SIGKILL after 30s |

Heartbeat candidates: mtime of `/state/speaking.log`, last `event_log` write, or an explicit `daemon_alive` event written every N seconds by the consumer loop. The explicit heartbeat is cleanest — `/state/speaking.log` may go quiet during idle periods that are legitimate.

This is genuinely Layer 2 work — not necessary if Layer 1 ships, since the s6 grace timer covers the drain path. The watchdog catches the *steady-state* version of the same wedge: a speaking daemon that hangs without first having received SIGTERM (e.g. a deadlock during a turn). We've not observed this in the wild yet; ship Layer 2 only if it surfaces.

### Layer 3 — Root-cause trace

The structural floor masks the leak. The leak should still be fixed at the source. Per the May 7 design:

1. Provoke a hang. Heavy-work wake / Discord-heavy speaking turn / etc. Easier on thinking — schedule a Stage D synthesis with several research notes and a deliberately-long context. Easier on speaking — run `dispatch_background_task` with a sub-agent that does heavy I/O and then SIGTERM the daemon mid-completion.
2. Before any kill: `py-spy dump --pid <PID>` to see the Python stack. `strace -p <PID>` to confirm the syscall is `epoll_wait`. `ls -la /proc/<PID>/fd/` and `lsof -p <PID>` to enumerate the open fds; the leaked socket should show up as a TCP/Unix peer-less entry.
3. Pin the leak to (a) the kernel SDK subprocess transport, (b) `httpx` connection pool, (c) the local Qwen client, or (d) something Alice-specific. Track upstream if it's the SDK.
4. Either upstream the fix or wrap the SDK call in an explicit teardown shield (`async with kernel:` style) that we control.

This is the proper fix. It is deferred because: the floor closes the visible incident class, the leak is intermittent (likely heavy-work-correlated), and tracing requires a worker willing to reproduce — which is a session, not a code change.

## Why a design doc instead of shipping

Two reasons:

1. **The fix touches deploy-critical surface.** Speaking's s6 run script governs the worker lease — bugs there can leave the lease unreleased on a clean shutdown, or fail to drain at all. Jason should look at the bash before it lands.
2. **Layer 1 is a floor, not a root-cause fix.** That's a real choice. We could instead spend the time on Layer 3 (root-cause trace) and skip the structural floor. The argument for the floor is that it closes today's pain (deploy stalls, blocked cadence) immediately and cheaply, while keeping Layer 3 available; the argument against is that masking the leak makes it harder to provoke and trace.

The recommendation here is "ship Layer 1, defer Layer 2 until needed, schedule a Layer 3 debug session." But that's Jason's call.

## Test plan (if Layer 1 ships)

- Unit test: harness that spawns a bash subprocess running the new `run` script logic against a Python decoy that sleeps in `epoll_wait` forever after writing "drain complete". Assert the wrapper SIGKILLs the decoy at the configured grace and exits within `grace + 5s`. Lives in `tests/test_speaker_drain_grace.py` (host-only, no s6).
- Smoke test: full container deploy, `docker kill --signal=SIGTERM`, assert the container exits within `ALICE_SPEAKING_DRAIN_GRACE + S6_KILL_GRACETIME_FLOOR`. Hand-run, not in CI.
- For thinking: existing `tests/test_alice_think.py` (if any) extends with a fake `python` that hangs; assert `timeout` kills it. Pure-bash test.

## Rollback

Layer 1 is two single-file changes (one bash script in `sandbox/worker/s6/alice-speaking/run`, one `bin/alice-think` shim). Rollback = revert the commit. No state migration, no schema change, no other consumer.
