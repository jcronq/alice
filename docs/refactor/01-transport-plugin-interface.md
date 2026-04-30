# 01 — Transport Plugin Interface + Daemon Decomposition

## Problem

### The transport "abstraction" is a fiction

`src/alice_speaking/transports/` looks like a plugin system: a `base.py`
with what reads as a protocol, plus one file per transport (`signal.py`,
`cli.py`, `discord.py`, `a2a.py`). It isn't.

`src/alice_speaking/daemon.py` (1914 lines) tells the real story:

- **Lines 113-167:** every transport has its own event dataclass —
  `SignalEvent`, `CLIEvent`, `DiscordEvent`, `A2AEvent`,
  `SurfaceEvent`, `EmergencyEvent` — defined at the top of `daemon.py`.
  The transport class in `transports/<name>.py` doesn't own its event.

- **Lines 560-576:** the consumer loop dispatches via `isinstance` ladder:

  ```
  if isinstance(event, SignalEvent):
      ...
      await self._handle_signal(batch)
  elif isinstance(event, CLIEvent):
      await self._handle_cli(event)
  elif isinstance(event, DiscordEvent):
      await self._handle_discord(event)
  elif isinstance(event, A2AEvent):
      await self._handle_a2a(event)
  elif isinstance(event, SurfaceEvent):
      await self._handle_surface(event)
  elif isinstance(event, EmergencyEvent):
      await self._handle_emergency(event)
  ```

- **`_handle_signal`, `_handle_cli`, `_handle_discord`, `_handle_a2a`,
  `_handle_surface`, `_handle_emergency`** are all methods of
  `SpeakingDaemon`, sharing `self` state (`self.events`, `self.kernel`,
  `self._outbox_destination`, `self._compaction_pending`, the principal
  book, the address book, the config). The handlers are not transport
  code — they're daemon code that happens to be parameterized by transport.

To add one transport today, an engineer touches **five layers**:

1. New `*Event` dataclass in `daemon.py` (top of file).
2. New producer task in `SpeakingDaemon.run()`.
3. New `elif isinstance(...)` branch in `SpeakingDaemon._consumer()`.
4. New `_handle_<transport>` method (~80-200 lines) inside `SpeakingDaemon`.
5. New `<transport>.py` in `transports/` with the actual transport class.

That's not a plugin interface. That's a god-class with five hooks.

### `SpeakingDaemon` is a 1914-line god-class

`grep "^def \|^async def \|^class "` on `daemon.py` returns 9 top-level
defs. The bulk lives inside one class. By concern:

- **Producer wiring** — Signal-RPC subscriber, CLI socket listener,
  Discord listener, A2A server, surface-watcher loop, emergency-watcher
  loop. Each is a method on the class.
- **Consumer dispatch** — the isinstance ladder.
- **Six handlers** (`_handle_signal` through `_handle_emergency`),
  each ~80-200 lines, each building its own prompt, calling the kernel,
  routing the outbox, deciding whether to send.
- **Shared services** — config hot-reload (`_maybe_reload_config`),
  signal coalescing (`_drain_signal_batch`), outbox-target tracking
  (`_send_message`, `_outbox_destination`), compaction gating
  (`_compaction_pending`, `_run_compaction`), principal lookup, kernel
  ownership.

Every concern shares `self`. Every change touches the class. Tests have
to construct the whole thing (or a heavy fake of it) to exercise any
single handler.

### Surfaces and emergencies aren't transports

`SurfaceEvent` and `EmergencyEvent` are dispatched the same way as
transports but they aren't transports — surfaces are files dropped by
Thinking into `inner/surface/`; emergencies are sentinel files with a
specific filename pattern. They're internal one-way feeds, not 2-way
channels with humans/agents. Treating them as transports muddles the
abstraction.

## Goal

After this plan:

- **Add a transport = one new file under `transports/` + one line in a
  registry.** No edits to `daemon.py`. No new dataclass at the top of
  `daemon.py`. No new branch in the consumer loop.
- **`daemon.py` is < 700 lines.** All six `_handle_*` methods have moved
  out. `daemon.py` keeps only the queue + registry + consumer loop +
  shared services.
- **Surfaces and emergencies live in `internal/`,** parallel to but
  separate from `transports/`. Same registration shape, different
  conceptual category.
- **Producer task lifecycle is explicit and uniform.** The daemon's `run()`
  iterates registered sources, collects their producer tasks, supervises
  them with the same start/stop semantics for all.
- **Existing tests still pass unchanged.** Behavior is preserved.

## Design

### The `Transport` protocol

```
class Transport(Protocol):
    name: str                                      # "signal" | "cli" | "discord" | "a2a"
    event_type: type[Event]                        # the dataclass this transport produces

    def producer(self, ctx: DaemonContext) -> asyncio.Task | None:
        """Start a long-running task that pushes events into ctx.queue.
        Return the task (so the daemon can supervise it) or None if this
        transport has no producer (e.g. is wired only as an outbound sink).
        """

    async def handle(self, ctx: DaemonContext, event: Event) -> None:
        """Process one event. Owns the prompt build + kernel call + outbox
        send for events of `event_type`."""
```

`Event` is a marker base class (or a `Protocol` — TBD; see open
questions). The point is: each event class lives next to its handler in
the same module. `SignalEvent` lives in `transports/signal.py`, not
`daemon.py`.

### The `InternalSource` protocol

Same shape as `Transport` minus the outbound-sink concept. Lives in
`internal/`:

```
class InternalSource(Protocol):
    name: str
    event_type: type[Event]

    def producer(self, ctx: DaemonContext) -> asyncio.Task | None: ...
    async def handle(self, ctx: DaemonContext, event: Event) -> None: ...
```

The runtime treats Transport and InternalSource the same way — both
register, both produce, both handle. The split exists for clarity, not
mechanism. (The dispatcher's registry stores both; reads `event_type` →
handler regardless.)

### The `DaemonContext` facade

The handlers need access to: the kernel, the principal book, the address
book, the events emitter, the outbox destination, the compaction trigger,
the queue (for re-entrancy), the config. Today they reach through `self`.
After: they take a `DaemonContext` parameter that exposes those services
as a narrow read interface.

```
@dataclass
class DaemonContext:
    queue: asyncio.Queue
    kernel: AgentKernel
    principals: PrincipalBook
    address_book: AddressBook
    events: EventEmitter
    config: Config
    outbox: OutboxRouter           # encapsulates _send_message + _outbox_destination
    compaction: CompactionTrigger  # encapsulates _compaction_pending + _run_compaction

    # Per-turn helpers (not state):
    def fresh_correlation_id(self) -> str: ...
```

The handlers don't mutate ctx fields. State that changes per turn —
like the outbox destination — lives in `OutboxRouter` / `CompactionTrigger`,
each a small class with a clear purpose. This is where the god-class breaks
into pieces.

### The dispatcher

```
class TurnDispatcher:
    def __init__(self, registry: SourceRegistry, ctx: DaemonContext): ...

    async def run(self) -> None:
        """Consumer loop. Pulls events, looks up handlers, dispatches.
        Replaces SpeakingDaemon._consumer."""
        # Run all StartupSource handlers exactly once before entering the
        # event loop. See "Session-start pipeline" below.
        await self._run_startup()
        while True:
            event = await self.ctx.queue.get()
            try:
                await self._dispatch(event)
            except Exception:
                self.ctx.events.log.exception("dispatch error: %s", type(event).__name__)
            finally:
                self.ctx.queue.task_done()

    async def _dispatch(self, event: Event) -> None:
        # Compaction runs BEFORE the event handler when the policy says so.
        # Compaction is **event-aware** — see "Compaction policy" below.
        # Plain `pending()` is wrong: a DEEP-depth SignalEvent should defer
        # compaction so the design thread isn't disrupted at the worst
        # possible moment.
        if self.ctx.compaction.should_run(event):
            await self.ctx.compaction.run()
        source = self._registry.lookup(type(event))
        if source is None:
            self.ctx.events.log.warning("no handler for event type: %s", type(event).__name__)
            return
        await source.handle(self.ctx, event)
```

### Compaction policy (replaces unconditional pending-check)

`CompactionTrigger.should_run(event) -> bool` encapsulates more than
the pending flag. Today's design (`daemon.py:557-558`) runs compaction
unconditionally before every event. That's wrong for in-flight design
threads: if compaction's pending and the next event is a DEEP-depth
SignalEvent (mid-design conversation), running compaction rolls the
session right before the next design message arrives, disrupting
thread continuity at the worst possible moment.

The fix (full design in
`cortex-memory/research/2026-04-29-compaction-deep-thread-deferral.md`):

```
class CompactionTrigger:
    MAX_DEFERRAL_TURNS = 5   # cap to prevent context overflow

    def __init__(self, ...):
        self._pending: bool = False
        self._deferred_turns: int = 0

    def arm(self) -> None:
        """Set pending=True (called when threshold crossed)."""

    def should_run(self, event: Event) -> bool:
        if not self._pending:
            return False
        # Only SignalEvents carry deep-thread context. Surfaces, CLI,
        # emergencies, A2A — never defer for them.
        if isinstance(event, SignalEvent):
            depth = compute_depth(event)  # SessionDepthSignal
            if depth.level >= DepthLevel.DEEP and self._deferred_turns < self.MAX_DEFERRAL_TURNS:
                self._deferred_turns += 1
                return False
        # Either non-Signal, or shallow Signal, or cap reached: fire.
        self._deferred_turns = 0
        return True

    async def run(self) -> None:
        ...
        self._pending = False
```

Cap of 5 deferred turns ≈ 5–10 minutes of deep work before compaction
runs regardless. Prevents context overflow on indefinitely DEEP threads.

### Signal-batch coalescing — fix the smell, don't preserve it

`_drain_signal_batch` (daemon.py:582-609) pulls non-Signal events off
the shared queue and re-puts them so the Signal handler can coalesce
a burst before calling the kernel. **This breaks the
"add a transport = one new file + one line" promise** — a developer
adding a new transport has to know "don't write a burst that arrives
while Signal is batching, or Signal will requeue your events in an
unspecified order." That's a hidden contract.

Two clean options; pick one in Phase 2a (between today's Phase 2
and Phase 3):

- **Option A — Per-transport queue.** `SignalTransport` gets its own
  `asyncio.Queue`. The dispatcher routes events to the per-transport
  queue (or a shared "main" queue if the transport doesn't have one).
  The transport drains its own queue for batching. No other
  transport's events are touched. Simpler, fully self-contained.
- **Option B — Pre-dispatch coalescer.** `TurnDispatcher._dispatch`
  consults a type-specific coalescer (registered alongside the
  handler) before routing. Signal registers a coalescer that returns
  the next-Signal-from-same-source if any are queued; others don't.
  More symmetric but adds a coalescer abstraction.

**Recommendation: Option A.** The asymmetry between "Signal coalesces
bursts; everyone else doesn't" is real and worth modeling explicitly.
Per-transport queue makes that explicit. Phase 2a slots cleanly between
"protocol exists" (Phase 2) and "registry dispatches" (Phase 3).

### Session-start pipeline (`StartupSource`)

Speaking starts each session cold on five categories of external state
that today are read either via prose-driven habits or not at all:
unhandled surfaces, the prebrief registry, fitness meso-state, vault
L1, and cortex-index freshness. Full design in
`cortex-memory/research/2026-04-29-speaking-session-start-pipeline.md`.

This work runs **once at session start, before the consumer loop
begins accepting events**. It doesn't fit anywhere in the slimmed
`SpeakingDaemon`:

- Prepending to `run()` runs on every daemon restart, not just
  cold-start, with no way for individual readers to opt out of restarts.
- `__init__` can't be async without breaking composability.
- Hand-coding it in `run()` reproduces the god-class pattern at a
  smaller scale.

The clean fit is a sibling protocol to `Transport` and `InternalSource`:

```
class StartupSource(Protocol):
    name: str

    async def run_once(self, ctx: DaemonContext) -> None:
        """Execute the startup task. Called exactly once before the
        dispatcher enters its event loop. Failures are logged; they
        don't block the daemon from starting (the source is best-
        effort)."""
```

Five startup sources, in order:

1. `SurfaceScanStartup` — scan `inner/surface/{today,yesterday}/`
   for unhandled surfaces; queue insight-priority for after first
   user message; emit flash-priority directly.
2. `PrebriefRegistryStartup` — read `memory/fitness/PHASE1-PREBRIEF-REGISTRY.md`,
   note overdue items.
3. `MesoStateStartup` — read `memory/fitness/MESO-STATE.md`, compute
   current week.
4. `CortexL1Startup` — load top-N high-access vault notes (deferred
   to plan 04 / cue runner integration).
5. `CortexIndexFreshnessStartup` — `build_index.py --check` and
   rebuild if stale.

Steps 1 and 2 are blocking-critical (time-sensitive consequences
with no fallback). Steps 3–5 are enrichment.

Steps 1, 2, 3 can ship as `StartupSource` instances **immediately on
plan 01 Phase 5** (alongside `internal/` work — same registration
shape). Steps 4–5 wait for plan 04 (prompts/cue-runner integration).

### Why a separate `StartupSource` and not "internal source that fires once"

`InternalSource.producer` returns a long-running task. A startup
task has different semantics: runs once, completes, doesn't produce
events into the queue (it modifies `ctx` directly — adds a
"prebrief items" state, queues a deferred surface-scan event, etc.).
Forcing them into the producer/handler shape would require fake
events and special-case task-completion handling. Separate protocol
keeps both clean.

### The slimmed `SpeakingDaemon`

After this plan:

```
class SpeakingDaemon:
    def __init__(self, cfg: Config, ctx: DaemonContext, registry: SourceRegistry): ...

    async def run(self) -> None:
        producers = []
        for source in self._registry.all():
            task = source.producer(self.ctx)
            if task is not None:
                producers.append(task)
        dispatcher = TurnDispatcher(self._registry, self.ctx)
        await asyncio.gather(dispatcher.run(), *producers)
```

Plus `_maybe_reload_config()`. Everything else moves.

Target line count: **< 700.** (Today: 1914.)

### Alternatives considered

- **Polymorphic dispatch via `Event.handle(self, ctx)`.** Cleaner-looking
  but couples the event dataclass to the handler — same coupling we're
  trying to break. The registry approach lets transports compose
  handlers (e.g. shared signal/discord prompt-builder) without inheritance
  gymnastics.

- **Single `Source` protocol covering both Transport and InternalSource.**
  Looks neater but loses the conceptual distinction. Surfaces and
  emergencies do not have outbound; transports do. We'd have to fake
  out the outbound on `InternalSource` instances. Two protocols with the
  same shape today, allowed to diverge cleanly tomorrow.

- **Move handlers to standalone modules (no class wrapper).** Tempting,
  but the handlers DO have transport-specific state — Discord needs the
  bot client, A2A needs the AgentExecutor, Signal needs the JSON-RPC
  cursor. A class per transport keeps that state in scope.

- **Big-bang rewrite.** Rejected because it would not satisfy the
  cross-cutting principle that every phase leaves the agent runnable
  and tested.

## Phases

### Phase 1 — Extract `_handle_*` to module functions

**Goal:** Move the bulk out of `SpeakingDaemon` without changing the
dispatch shape. The class shrinks; the consumer's isinstance ladder
becomes a thin call site.

**Changes:**
- Add `src/alice_speaking/_dispatch.py` with module-level async
  functions: `handle_signal_batch`, `handle_cli`, `handle_discord`,
  `handle_a2a`, `handle_surface`, `handle_emergency`. Bodies are the
  current `_handle_*` methods, but every `self.X` access becomes
  `ctx.X` against a temporary `DaemonContext` (a thin dataclass that
  exposes the same attributes the methods used).
- `SpeakingDaemon._handle_*` methods become one-liners that delegate.
- Tests don't change.

**Validation:** `pytest tests/test_daemon.py tests/test_compaction.py
tests/test_signal_batching.py tests/test_signal_attachments.py
tests/test_a2a_transport.py tests/test_discord_transport.py
tests/test_messaging.py`

**Exit criteria:** all green; daemon.py loses ~700 lines (handlers gone);
no behavior change.

---

### Phase 2 — Define `Transport`, `Event`, and `StartupSource` types

**Goal:** Codify the protocol. Each transport class (signal, cli,
discord, a2a) gains `name`, `event_type`, `producer()`, `handle()` —
where `handle()` calls into the Phase-1 module function. Define
`StartupSource` and `InternalSource` siblings.

**Changes:**
- `transports/base.py` gets the new `Transport` Protocol, the marker
  `Event` base, and the `DaemonContext` dataclass (today the temporary
  one from Phase 1; promote to public).
- `internal/base.py` gets `InternalSource` Protocol (sibling).
- `startup/base.py` gets `StartupSource` Protocol — different shape
  from Transport/InternalSource (no producer, no event_type, no
  handler; just `async run_once(ctx)`). See "Session-start pipeline"
  in Design.
- Each transport file (`signal.py`, `cli.py`, `discord.py`, `a2a.py`)
  gains the four protocol members. `producer()` returns the per-transport
  long-running task that today is set up directly in `SpeakingDaemon.run()`.
  `handle()` is a one-liner: `await handle_<name>(ctx, event)`.
- `SpeakingDaemon.run()` still wires producers manually — registry
  lookup comes in Phase 3.

**Validation:** `pytest` (full suite) plus a new
`tests/test_transport_protocol.py`:

- `test_each_transport_implements_protocol` — every transport class
  passes a `runtime_checkable` Protocol assertion.
- `test_event_type_is_dataclass` — every transport's `event_type` is a
  dataclass with the fields the dispatcher relies on.
- `test_startup_source_protocol_distinct_from_transport` —
  `StartupSource` does not extend `Transport`; both are sibling
  protocols.

**Exit criteria:** full test suite green; transports advertise the
protocol but daemon hasn't switched to registry dispatch yet.

---

### Phase 2a — Fix signal-batch re-entrancy

**Goal:** Eliminate `_drain_signal_batch`'s reach-into-shared-queue
behavior before registry dispatch lands. Signal coalescing keeps
working from the user's perspective.

**Changes:** Implement Option A from Design (per-transport queue):
- `SignalTransport` instantiates its own `asyncio.Queue` for inbound
  signal events.
- `SignalTransport.producer()` reads from the signal RPC and pushes
  to its own queue (not the daemon's main queue).
- `SignalTransport.handle()` is now driven by the signal queue —
  drains a coalesced burst from its own queue, calls the kernel.
- The dispatcher's main queue holds only non-Signal events. Signal's
  per-queue handler runs as a peer to the main consumer (asyncio.gather).
- `_drain_signal_batch` is deleted from `daemon.py`.

**Validation:** `tests/test_signal_batching.py` keeps passing
(behaviorally same coalesced-burst handling); add:
- `test_burst_does_not_disturb_other_transports` — push a burst of
  Signal + interleaved Discord events; assert Discord events arrive
  in their original order, signal coalesces normally.

**Exit criteria:** No transport reaches into another transport's
queue or the dispatcher's queue. Signal batching still works.

---

### Phase 3 — Registry-based dispatch

**Goal:** Kill the isinstance ladder. The consumer's `_dispatch` looks up
the source by event type.

**Changes:**
- `transports/registry.py` (new): `SourceRegistry` with `register(source)`
  / `register_internal(source)` / `register_startup(source)` /
  `lookup(event_type) -> Source | None` / `all_event_sources()` /
  `all_startup_sources()`.
- `alice_speaking/factory.py` (new, or extend `__init__`): builds the
  registry from config (which transports are enabled) and returns it.
  Wires Signal only if `SIGNAL_ACCOUNT` is set, A2A only if
  `A2A_ENABLE=1`, etc. — same gating as today, just centralized.
- `SpeakingDaemon._consumer` becomes the new `TurnDispatcher.run`,
  driven by `registry.lookup(type(event))`. **`_dispatch` consults
  `ctx.compaction.should_run(event)`, not `pending()`** —
  see "Compaction policy" in Design.
- The isinstance ladder is deleted.
- `TurnDispatcher.run()` calls `_run_startup()` before entering the
  consumer loop. `_run_startup()` iterates
  `registry.all_startup_sources()` and awaits each `run_once(ctx)`.

**Validation:** `pytest` (full suite) plus extensions to
`tests/test_transport_protocol.py`:

- `test_registry_dispatches_by_event_type` — register a fake source,
  push its event into the queue, assert its `handle()` ran.
- `test_unknown_event_type_logs_warning` — push an unregistered event
  type, assert no exception, assert warning logged.

**Exit criteria:** full suite green; `daemon.py` has no `isinstance`
ladder; the test for unknown-type doesn't crash the consumer loop.

---

### Phase 4 — Move event dataclasses out of `daemon.py`

**Goal:** Each `*Event` dataclass lives next to its handler, in the
transport module.

**Changes:**
- `SignalEvent` → `transports/signal.py`.
- `CLIEvent` → `transports/cli.py`.
- `DiscordEvent` → `transports/discord.py`.
- `A2AEvent` → `transports/a2a.py`.
- `SurfaceEvent`, `EmergencyEvent` stay in `daemon.py` for now — they
  move in Phase 5.
- `daemon.py` imports the four it still needs only for type hints; or
  uses `from __future__ import annotations` and drops the imports
  entirely.

**Validation:** `pytest` (full suite). No new tests — moves are mechanical.

**Exit criteria:** `daemon.py` line 113-167 area shrinks; no test changes.

---

### Phase 5 — Internal sources + startup sources subpackages

**Goal:** Surfaces and emergencies become first-class internal sources.
Session-start tasks become first-class startup sources. Both register
through the same registry but live in different subpackages.

**Changes:**
- New `src/alice_speaking/internal/` package:
  - `__init__.py`
  - `base.py` — `InternalSource` Protocol (sibling of `Transport`).
    Already defined in Phase 2.
  - `surfaces.py` — `SurfaceEvent` dataclass, `SurfaceWatcher` class
    with `producer()` (the inotify/poll loop) and `handle()` (calls
    the Phase-1 `handle_surface`).
  - `emergency.py` — `EmergencyEvent` dataclass, `EmergencyWatcher`
    class with `producer()` (sentinel file watcher) and `handle()`
    (calls the Phase-1 `handle_emergency`).
- New `src/alice_speaking/startup/` package:
  - `__init__.py`
  - `base.py` — `StartupSource` Protocol. Already defined in Phase 2.
  - `surface_scan.py` — `SurfaceScanStartup`. Reads
    `inner/surface/{today,yesterday}/`, classifies by priority,
    emits flash items as direct events into the queue, queues
    insight items into a "deferred surface backlog" on `ctx`.
  - `prebrief_registry.py` — `PrebriefRegistryStartup`. Reads
    `memory/fitness/PHASE1-PREBRIEF-REGISTRY.md`, exposes overdue
    items via `ctx.prebrief_state`.
  - `meso_state.py` — `MesoStateStartup`. Reads
    `memory/fitness/MESO-STATE.md`, computes current week, exposes
    via `ctx.meso_state`.
  - `cortex_index_freshness.py` — runs `build_index.py --check`,
    rebuilds if stale.
  - (`cortex_l1.py` deferred to plan 04 / cue runner integration.)
- `factory.py` registers all internal + startup sources alongside
  transports.
- Pipeline middleware that gated surfaces during quiet hours
  (currently somewhere in `_handle_surface`) moves with the handler.

**Validation:** `pytest` (full suite) — existing `tests/test_daemon.py`
covers surface + emergency dispatch.

**Exit criteria:** `daemon.py` no longer contains `SurfaceEvent`,
`EmergencyEvent`, surface-watcher producer, or emergency-watcher
producer. All in `internal/`.

---

### Phase 6 — Final daemon shrink + shared-service extraction

**Goal:** `daemon.py` < 700 lines. Shared services (`OutboxRouter`,
`CompactionTrigger`) get their own files.

**Changes:**
- `OutboxRouter` (new file in `alice_speaking/outbox.py` or domain/):
  encapsulates the `_send_message` closure + `_outbox_destination`
  tracking. Handlers call `ctx.outbox.send(...)` and
  `ctx.outbox.last_destination()`.
- `CompactionTrigger` (new file `alice_speaking/pipeline/compaction.py`,
  refactored from existing `compaction.py`): encapsulates the pending
  flag + the run logic. Dispatcher calls `ctx.compaction.pending()` /
  `ctx.compaction.run()`.
- `SpeakingDaemon` becomes the slim version from the design section:
  `__init__`, `run`, `_maybe_reload_config`. Everything else delegated.

**Validation:** `pytest` (full suite). New tests:

- `tests/test_outbox.py` — `OutboxRouter` honors `recipient="self"`
  routing rules without the daemon scaffolding.
- `tests/test_compaction_trigger.py` — pending/run lifecycle isolated
  from the daemon (existing `test_compaction.py` covers the prompt;
  this covers the trigger).

**Exit criteria:** `daemon.py` < 700 lines; all tests green; no
behavioral change in `bin/alice -p "ping"`.

---

## Tests

### Existing tests this plan must keep green

- `tests/test_daemon.py` (17K) — exercises producer/consumer + handlers.
  Phase 1 should not change a single assertion in this file.
- `tests/test_compaction.py` — compaction handler.
- `tests/test_signal_batching.py` — coalesced-burst handling.
  Phase 1 must preserve `_drain_signal_batch` semantics exactly.
- `tests/test_signal_attachments.py` — media handling on Signal.
- `tests/test_a2a_transport.py` — A2A inbound. Phase 4 (event dataclass
  move) must not break it.
- `tests/test_discord_transport.py` — Discord inbound. Same.
- `tests/test_messaging.py` — `send_message` tool. Phase 6
  (`OutboxRouter` extraction) must preserve.
- `tests/test_principals.py` — address book. Untouched by this plan.
- `tests/test_kernel.py` — kernel observer pattern. Untouched.

### New tests this plan introduces

- `tests/test_transport_protocol.py` (Phase 2):
  - `test_each_transport_implements_protocol` — Signal, CLI, Discord,
    A2A all pass `isinstance(t, Transport)` (runtime-checkable).
  - `test_event_type_is_dataclass` — each transport's
    `event_type` is a dataclass.
  - `test_event_module_colocated` — each transport's `event_type`
    lives in the same module as the transport class.

- Extensions in same file (Phase 3):
  - `test_registry_dispatches_by_event_type` — fake source registered;
    its event dispatches to its handler.
  - `test_unknown_event_type_logs_warning` — unknown event type doesn't
    crash dispatcher.
  - `test_internal_source_registered_alongside_transports` — Phase 5;
    SurfaceWatcher + EmergencyWatcher show up in registry alongside
    transports.

- `tests/test_outbox.py` (Phase 6):
  - `test_self_recipient_routes_to_last_destination`
  - `test_named_recipient_uses_principal_book`
  - `test_quiet_hours_blocks_surface_sends`

- `tests/test_compaction_trigger.py` (Phase 6):
  - `test_compaction_runs_before_next_event_when_pending`
  - `test_pending_flag_clears_after_compaction`
  - `test_should_run_returns_false_for_deep_signal_event`
  - `test_should_run_returns_true_for_shallow_signal_when_pending`
  - `test_should_run_returns_true_for_non_signal_when_pending`
  - `test_deferral_caps_at_max_deferral_turns`
  - `test_deferred_turns_resets_after_run`

- `tests/test_startup_pipeline.py` (Phase 5):
  - `test_startup_sources_run_in_registration_order`
  - `test_startup_failure_logs_does_not_block_daemon`
  - `test_startup_runs_exactly_once_before_consumer_loop`
  - `test_surface_scan_classifies_flash_vs_insight`
  - `test_prebrief_registry_loads_overdue_items`

- `tests/test_module_boundaries.py` (cross-cutting, per plan 00):
  - `test_each_transport_file_has_one_transport_class`
  - `test_each_internal_file_has_one_internal_source_class`
  - `test_each_startup_file_has_one_startup_source_class`

## Risks & non-goals

### Risks

- **Producer-task lifecycles.** Producer tasks today are started inside
  `SpeakingDaemon.run()` and supervised by `asyncio.gather`. After this
  refactor, sources own their producers. The daemon must still cancel
  them on shutdown. Preserve clean cancellation by collecting all
  producer tasks into one supervisor — same as today, just shifted.

- **Signal batching is a design smell to fix, not preserve.** Phase 2a
  moves Signal to its own per-transport queue (Option A in Design),
  eliminating the shared-queue reach-in. Until Phase 2a lands, the
  legacy behavior is in place; tests must keep covering the
  mid-batch-surface case during the transition.

- **`_outbox_destination` is per-turn state.** Today it's set during
  `_handle_*` and read by the `send_message` tool callback. Phase 6
  needs to make sure `OutboxRouter` is the same instance the tool
  callback closes over. Pass it via `ctx.outbox`, not by reconstruction.

- **`compaction_pending` is process-lifetime state, not per-turn.**
  Same lifetime concern. `CompactionTrigger` is created once and lives
  with the daemon.

- **Discord transport has guild-channel allowlist gating** that today
  lives in the producer (`daemon.py:528 region`). When the producer
  moves to `transports/discord.py`, that gating moves with it. Verify
  via `tests/test_discord_transport.py`.

### Non-goals

- **No transport API surface changes** — transports keep speaking the
  same way to the outside world. Signal-RPC client unchanged. CLI
  socket protocol unchanged. Discord intents unchanged. A2A endpoints
  unchanged.
- **No queue-impl change** — still `asyncio.Queue`. No bus, no broker.
- **No new transports** — adding the next one (e.g. email) is post-refactor
  work that this refactor *enables*.
- **No event-bus pubsub** — the registry dispatches each event to one
  source. Multi-handler / fan-out is not in scope.
- **No persistence of in-flight events.** If the daemon dies mid-event,
  the event is lost. Same as today.

## Open questions

1. **`Event` base — class or Protocol?**
   Class is simpler (`@dataclass class Event` with no fields, then
   `class SignalEvent(Event)`). Protocol allows duck-typing but defeats
   `isinstance()` registry lookup. Recommendation: **empty marker
   dataclass**, since the registry uses `type(event)` keys and
   inheritance is a non-issue.

2. **Should `Transport.producer()` be sync (returning a Task) or async
   (returning the awaitable directly)?**
   Today's code is mixed. Recommendation: **sync, returns
   `asyncio.Task | None`.** The daemon kicks off the task; sources
   that don't have a producer (outbound-only, or driven by
   external callbacks like Discord) return None.

3. **Where does the `factory.py` live?**
   Could be `alice_speaking/factory.py` or `alice_speaking/__init__.py`.
   Recommendation: dedicated `factory.py` so `__init__.py` stays a
   re-export point.

4. **Should the `internal/` subpackage be `sources/` instead?**
   "Internal" reads as "private"; "sources" reads as "feeds." Pick one
   before Phase 5. Recommendation: **`internal/`** because it conveys
   the conceptual distinction from transports (no outbound).

5. **Compaction-before-event invariant.** The dispatcher consults
   `ctx.compaction.should_run(event)` (event-aware deferral — see
   "Compaction policy"). For surface and emergency events, `should_run`
   returns the value of `pending()` directly (no deferral semantics).
   Confirm we don't accidentally double-compact during a surface burst.

6. **`SessionDepthSignal` as a Phase 3 prerequisite.** The compaction
   `should_run(event)` policy depends on `compute_depth(event)`
   producing a `SessionDepthSignal` (DEEP / STANDARD / SHALLOW / SKIP).
   That signal lives in
   `cortex-memory/research/2026-04-29-session-depth-signal-unified.md`
   and is independent of this plan but bundles with it. **Either the
   depth signal lands before Phase 3, or Phase 3 ships with depth
   computed inline as a stub** (every event = STANDARD, deferral
   never triggers) and the deferral hooks turn on once the signal
   lands.

7. **StartupSource failure semantics.** Should a startup source
   failing (e.g. `cortex_index_freshness.py` errors during rebuild)
   block the daemon from starting? Recommendation: **no, log and
   continue.** Startup work is best-effort; the daemon must come up.
   Document explicitly.

These seven want resolution before Phase 1 starts.
