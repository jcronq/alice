"""Speaking Alice's outer loop.

Three producers feed one serial consumer:
- signal_client.receive(): user envelopes from Signal
- surface_watcher: files that thinking Alice drops into inner/surface/
- emergency_watcher: files that external monitors drop into inner/emergency/

The consumer processes one event at a time — Alice is a single mind juggling
messages and surfaced thoughts, not a parallel worker pool.

Context persistence (v3, see
cortex-memory/reference/design-unified-context-compaction.md):

- Layer 1: session_id is persisted to ``inner/state/session.json`` after
  every ResultMessage. On startup the daemon reads it back and passes
  ``resume=`` on the first turn, so the daemon wakes warm after a
  restart.
- Layer 2: if session.json is missing / corrupt, or the SDK session
  JSONL has been deleted, or resume= fails at runtime, the daemon falls
  back to a silent bootstrap turn that injects render_for_prompt of the
  recent turn_log. That turn's session_id becomes the active session.
- Compaction: after each turn, if effective context tokens (input +
  cache_read + cache_creation) exceed
  ``cfg.speaking["context_compaction_threshold"]``, a flag is set. The
  consumer runs a silent compaction turn before the next event, writes
  a 4-part summary to ``inner/state/context-summary.md``, rolls the
  session, and injects the summary + tail(5) turns on the next turn.

Outbox (v3):

- The daemon no longer auto-sends the final assistant text. Alice must
  call the ``send_message`` tool explicitly. A turn that closes without
  any send_message call logs a ``missed_reply`` event for auditing.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import pathlib
import signal as _signal
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .transports.a2a import A2ATransport
    from .transports.discord import DiscordTransport

from alice_core.auth import ensure_auth_env
from alice_core.kernel import AgentKernel, KernelSpec
from alice_core.sdk_compat import looks_like_missing_session as _looks_like_missing_session

from . import _dispatch as _dispatch_module
from . import compaction as compaction_module
from . import config as config_module
from . import factory as factory_module
from . import principals as principals_module
from . import render as render_module
from . import session_state
from . import tools as tools_module
from .config import Config
from .dedup import DedupStore
from .events import EventLogger
from .handlers import CLITraceHandler, CompactionArmer, SessionHandler
from .internal import (
    EmergencyEvent,
    EmergencyWatcher,
    SurfaceEvent,
    SurfaceWatcher,
)
from .outbox import OutboxRouter
from .principals import AddressBook
from .quiet_hours import QuietQueue, is_quiet_hours
from .signal_client import SignalClient
from .tools.messaging import SELF_RECIPIENT, ResolvedRecipient
from .transports import (
    CLITransport,
    ChannelRef,
    SignalTransport,
)
# DiscordTransport is imported lazily below, only when the daemon is actually
# configured to use Discord. Module-top ``import discord`` in transports.discord
# would otherwise crash the daemon at import time when discord.py isn't
# installed (e.g. stale worker image after a Dockerfile bump).
from .transports.base import SIGNAL_CAPS
# Per-transport event dataclasses live next to their transports
# (transport events: Phase 2; SurfaceEvent / EmergencyEvent: Phase 3).
# Daemon no longer touches them directly — the registry routes by
# ``type(event)`` and the per-transport / per-internal-source
# producers construct them. These re-imports stay only so existing
# external callers (tests, the viewer's narrative dump) keep their
# ``from alice_speaking.daemon import …Event`` paths working.
from .transports.a2a import A2AEvent
from .transports.cli import CLIEvent
from .transports.discord import DiscordEvent
from .transports.signal import SignalEvent
from .turn_log import TurnLog


log = logging.getLogger("alice_speaking")


QUIET_CHECK_SECONDS = 30.0

# Turns after which tail-trim happens when composing the summary preamble.
# Matches the design: 5 verbatim turns bridge the gap between summary
# cutoff and now.
SUMMARY_TAIL_TURNS = 5


def _format_envelope_time(timestamp_ms: int) -> str:
    """Render an envelope's millisecond Unix timestamp as a local time string.

    Used by the multi-message prompt format so Alice can see when each
    queued message arrived relative to the others.
    """
    try:
        dt = datetime.datetime.fromtimestamp(int(timestamp_ms) / 1000).astimezone()
    except (OSError, ValueError, OverflowError):
        return str(timestamp_ms)
    return dt.strftime("%-I:%M:%S %p %Z")


# Public names re-exported from this module for back-compat. The
# event types live in their owning modules (transports/* and
# internal/*) — see the import block above. Listed here so the
# re-exports are intentional, not accidental.
__all__ = [
    "A2AEvent",
    "CLIEvent",
    "DiscordEvent",
    "EmergencyEvent",
    "SignalEvent",
    "SpeakingDaemon",
    "SurfaceEvent",
]


class SpeakingDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Phase 3: AddressBook is the unified ACL + display-name + recipient
        # resolution surface. Loaded from principals.yaml when present;
        # synthesized from ALLOWED_SENDERS + the daemon's own uid as a
        # migration shim when it isn't.
        self.address_book: AddressBook = principals_module.load(
            yaml_path=cfg.principals_path,
            fallback_signal_senders=cfg.allowed_senders_fallback,
            fallback_cli_uid=os.getuid(),
        )
        # Signal is opt-in. Without SIGNAL_ACCOUNT in alice.env we skip the
        # transport entirely and let CLI / Discord (if configured) carry
        # conversation. The daemon still runs.
        self.signal: Optional[SignalClient] = (
            SignalClient(
                api=cfg.signal_api,
                account=cfg.signal_account,
                log_path=cfg.signal_log_path,
                offset_path=cfg.offset_path,
            )
            if cfg.signal_account
            else None
        )
        self.signal_transport: Optional[SignalTransport] = (
            SignalTransport(signal_client=self.signal) if self.signal else None
        )
        self.dedup = DedupStore(cfg.seen_path)
        self.turns = TurnLog(cfg.turn_log_path)
        self.events = EventLogger(cfg.event_log_path)
        self.quiet_queue = QuietQueue(cfg.mind_dir / "inner" / "state" / "quiet-queue.jsonl")

        # State directory — used for session.json and context-summary.md.
        self._state_dir = cfg.mind_dir / "inner" / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._session_path = self._state_dir / "session.json"
        self._summary_path = self._state_dir / "context-summary.md"

        # Session identity (Layer 1): pre-populate from disk if present,
        # drop it if the underlying SDK JSONL is gone.
        self.session_id: Optional[str] = None
        persisted = session_state.read(self._session_path)
        if persisted is not None:
            if session_state.sdk_session_exists(cfg.work_dir, persisted.session_id):
                self.session_id = persisted.session_id
                log.info(
                    "loaded persisted session %s (saved_at=%s)",
                    persisted.session_id,
                    persisted.saved_at,
                )
            else:
                log.warning(
                    "persisted session %s has no SDK JSONL; starting cold",
                    persisted.session_id,
                )
                session_state.clear(self._session_path)

        # MCP tools — build AFTER we know signal/session state because the
        # send_message sender closure needs self.signal plus the did-send
        # tracker below.
        self.mcp_servers, self.custom_tool_names = tools_module.build(
            cfg,
            address_book=self.address_book,
            sender=self._send_message,
        )

        # Compaction policy + state. Phase 6b of plan 01 replaced
        # the bare ``self._compaction_pending`` flag with a
        # CompactionTrigger that owns ``should_run(event)`` and the
        # actual run orchestration. Both consumers go through it.
        self.compaction = compaction_module.CompactionTrigger()
        # Per-turn did-send tracker. Set back to False at the start of each
        # call to _run_turn(); flipped to True by _send_message when Alice
        # explicitly sends. Used to flag missed_reply events.
        self._turn_did_send: bool = False
        # Per-turn outbound text capture. Set to None at start of each turn;
        # _send_message records the most recent outbound text here so the
        # turn_log entry can attach it (Layer 2 bootstrap relies on this).
        self._turn_last_outbound: Optional[str] = None
        # Current turn kind — set by _handle_signal/_handle_surface/
        # _handle_emergency/_handle_cli at entry, reset in finally.
        # _send_message uses this to decide whether to honor quiet hours:
        # signal + cli + emergency turns bypass (the user is waiting on
        # an answer); surface turns honor (Alice-initiated thoughts wait
        # for morning).
        self._current_turn_kind: Optional[str] = None
        # Reply channel for the current turn — set by handlers at entry,
        # cleared in finally. _send_message uses this when Alice picks
        # recipient='self' to dispatch back over the originating
        # transport. None outside of a turn or for surface/emergency
        # turns where there's no inbound channel.
        self._current_reply_channel: Optional[ChannelRef] = None
        # Display name for the principal whose turn we're inside. Used
        # by ``_emit_send_event`` so e.g. cli_send's sender_name reads
        # the principal's display name rather than the opaque conn_id
        # from the ChannelRef. None outside of an inbound conversational turn.
        self._current_principal_display_name: Optional[str] = None
        # When set, the very next turn will prepend this text as a
        # bootstrap preamble (Layer 2 restart OR post-compaction summary
        # injection).
        self._pending_preamble: Optional[str] = None
        # One-shot consumer startup guard.
        self._consumer_started: bool = False

        # CLI transport — optional, falls back to no-op if disabled.
        # Constructed here so it shares the daemon's lifecycle and can
        # see _current_reply_channel via _send_message. ACL + display
        # name come from the address book.
        self.cli_transport: Optional[CLITransport] = (
            CLITransport(
                socket_path=cfg.cli_socket_path,
                is_allowed=lambda uid: self.address_book.is_allowed("cli", uid),
                principal_name_for=lambda uid: self.address_book.display_name_for(
                    "cli", uid
                ),
            )
            if cfg.cli_enabled
            else None
        )

        # Discord transport — optional. Constructed only when a bot token
        # is configured; absent token = transport stays None and existing
        # deploys keep working unchanged. The import itself is lazy: the
        # transport module top-imports ``discord``, which would otherwise
        # crash the daemon at startup when the optional dep is missing.
        self.discord_transport: Optional["DiscordTransport"] = None
        if cfg.discord_bot_token:
            from .transports.discord import DiscordTransport
            self.discord_transport = DiscordTransport(token=cfg.discord_bot_token)

        # A2A transport — optional. Constructed only when explicitly
        # enabled in alice.env. Import is lazy so worker images that
        # don't ship a2a-sdk (e.g. minimal builds) start fine.
        self.a2a_transport: Optional["A2ATransport"] = None
        if cfg.a2a_enabled:
            from .transports.a2a import A2ATransport
            self.a2a_transport = A2ATransport(
                port=cfg.a2a_port,
                host=cfg.a2a_host,
                principal_name=cfg.a2a_principal,
                external_url=cfg.a2a_external_url or None,
            )

        # Heterogeneous event queue: each producer pushes its own
        # event type, the registry routes by ``type(event)`` (Phase 3
        # of plan 01). No Union annotation — the closed set lives in
        # the registry, not here.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        # Phase 3 / 5 of plan 01: dispatcher routes by event type
        # via a registry instead of an isinstance ladder. Signal is
        # intentionally omitted — its events flow through the
        # transport's own inbox (Phase 2a), never the main queue.
        # Watchers are constructed up here because the daemon also
        # reaches them directly for archive bookkeeping; the factory
        # registers them by reference.
        self._surface_watcher = SurfaceWatcher(cfg.mind_dir)
        self._emergency_watcher = EmergencyWatcher(cfg.mind_dir)
        self._registry = factory_module.build_registry(
            cfg,
            transports=(
                self.cli_transport,
                self.discord_transport,
                self.a2a_transport,
            ),
            surface_watcher=self._surface_watcher,
            emergency_watcher=self._emergency_watcher,
        )
        # Phase 6a of plan 01: outbound dispatch + quiet-queue
        # routing + canonical send-event emission live in
        # :class:`OutboxRouter`. Daemon's ``_send_message`` becomes
        # a thin facade that resolves recipient → channel and
        # delegates here.
        self.outbox = OutboxRouter(
            transport_for=lambda name: {
                "signal": self.signal_transport,
                "cli": self.cli_transport,
                "discord": self.discord_transport,
                "a2a": self.a2a_transport,
            }.get(name),
            address_book=self.address_book,
            events=self.events,
            quiet_queue=self.quiet_queue,
            speaking_cfg=cfg.speaking,
        )
        # Phase 2a of plan 01 introduced a second consumer (Signal's
        # per-transport batch loop runs alongside the main consumer).
        # Both must serialise on shared kernel state — _run_turn,
        # session_id, _current_turn_kind, etc. — so each turn-runner
        # acquires this lock around the pre-turn services + handler
        # body. Phase 6 replaces the lock with a TurnDispatcher that
        # owns the same invariant explicitly.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._config_path = cfg.mind_dir / "config" / "alice.config.json"
        self._config_mtime: float = (
            self._config_path.stat().st_mtime if self._config_path.is_file() else 0.0
        )

    # ------------------------------------------------------------------
    # Lifecycle

    async def run(self) -> None:
        # Resolve auth from alice.env + os.environ. ensure_auth_env() sets
        # the right vars on os.environ so the Agent SDK's CLI subprocess
        # inherits either subscription (CLAUDE_CODE_OAUTH_TOKEN) or
        # api-mode (ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY) credentials.
        ensure_auth_env()

        loop = asyncio.get_event_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)

        self.events.emit(
            "daemon_start",
            model=self.cfg.speaking.get("model"),
            session_id=self.session_id,
            compaction_threshold=self.cfg.speaking.get("context_compaction_threshold"),
            bootstrap_turns=self.cfg.speaking.get("context_bootstrap_turns"),
        )
        try:
            if self.signal is not None and self.signal_transport is not None:
                log.info("waiting for signal-cli at %s", self.cfg.signal_api)
                await self.signal.wait_ready()
                await self.signal_transport.start()
            else:
                log.info("signal disabled (no SIGNAL_ACCOUNT); skipping signal-cli")
            log.info("daemon ready; listening")
            self.events.emit("daemon_ready", signal_api=self.cfg.signal_api)

            # If quiet hours ended while we were down, drain the queue first.
            if not is_quiet_hours(self.cfg.speaking) and self.quiet_queue.size() > 0:
                await self._drain_quiet_queue(reason="startup")

            # Prime the Layer 2 bootstrap preamble if we don't have a
            # session to resume. The consumer picks it up on the first turn.
            self._prime_bootstrap_preamble()

            # Phase 5 of plan 01: every event-producing source owns
            # its own producer task, including the surface and
            # emergency watchers. Daemon supervises them under
            # uniform start/cancel semantics; the only thing left
            # daemon-private is the quiet-hours queue watcher (a
            # cross-cutting concern, not an event source).
            ctx = _dispatch_module.DaemonContext(self)

            # Startup phase: best-effort one-shot tasks that prime
            # ``ctx`` with mind-state (surface backlog, fitness
            # registry, meso-cycle, cortex-index freshness). Each
            # source is fail-soft per-source, so a missing mind
            # file or a kernel-side OSError doesn't block boot.
            await factory_module.run_startup_phase(self._registry, ctx)

            producers: list[asyncio.Task] = [
                asyncio.create_task(self._quiet_watcher(), name="quiet-watch"),
            ]
            for source in self._registry.all_event_sources():
                # Transports that need a network-level handshake
                # (Discord, A2A) expose ``start()`` on the channel-
                # layer half of the Transport protocol. Internal
                # sources (SurfaceWatcher, EmergencyWatcher) don't.
                start = getattr(source, "start", None)
                if start is not None:
                    await start()
                task = source.producer(ctx)
                if task is not None:
                    producers.append(task)
            # Signal owns its own per-transport consumer loop
            # (Phase 2a) and is intentionally absent from the
            # registry; schedule it separately.
            if self.signal_transport is not None:
                task = self.signal_transport.producer(ctx)
                if task is not None:
                    producers.append(task)
            consumer = asyncio.create_task(self._consumer(), name="consumer")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop")

            done, _ = await asyncio.wait(
                {*producers, consumer, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            log.info("shutdown starting (triggered by %s)", [t.get_name() for t in done])
            for task in (*producers, consumer):
                task.cancel()
            for task in (*producers, consumer):
                with contextlib.suppress(BaseException):
                    await task
        finally:
            if self.signal_transport is not None:
                with contextlib.suppress(Exception):
                    await self.signal_transport.stop()
            if self.signal is not None:
                await self.signal.aclose()
            if self.cli_transport is not None:
                with contextlib.suppress(Exception):
                    await self.cli_transport.stop()
            if self.discord_transport is not None:
                with contextlib.suppress(Exception):
                    await self.discord_transport.stop()
            if self.a2a_transport is not None:
                with contextlib.suppress(Exception):
                    await self.a2a_transport.stop()
            self.events.emit("shutdown")
            log.info("shutdown complete")

    # ------------------------------------------------------------------
    # Producers
    #
    # All event-producing sources own their producer task in Phase 5
    # of plan 01: transports under ``transports/*`` (Phase 2),
    # internal sources under ``internal/*`` (Phase 5). Daemon's
    # ``run()`` schedules them via ``self._registry``; nothing
    # event-source-specific lives here anymore.

    # ------------------------------------------------------------------
    # Consumer

    async def _consumer(self) -> None:
        # Signal events bypass this loop — Phase 2a of plan 01 routes
        # them through SignalTransport's own per-transport inbox.
        # Everything else (CLI, Discord, A2A, surfaces, emergencies)
        # reaches the dispatcher here, and Phase 3's registry routes
        # by ``type(event)`` instead of an isinstance ladder.
        ctx = _dispatch_module.DaemonContext(self)
        while True:
            event = await self._queue.get()
            try:
                source = self._registry.lookup(type(event))
                if source is None:
                    log.warning(
                        "no handler for event type: %s", type(event).__name__
                    )
                    continue
                async with self._turn_lock:
                    await self._pre_turn(event)
                    await source.handle(ctx, event)
            except Exception:
                log.exception("consumer error handling %s", type(event).__name__)
            finally:
                self._queue.task_done()

    async def _pre_turn(self, event: object) -> None:
        """Pre-turn services run before any handler.

        Both consumers (the dispatcher main loop and SignalTransport's
        per-transport batch loop) hold ``self._turn_lock`` and call
        this so the config reload + compaction policy can't race.
        Compaction runs BEFORE any inbound event so the token check
        from the previous turn has a chance to roll the session
        before we append more context. Phase 6b of plan 01 routes
        the policy through :class:`CompactionTrigger.should_run` —
        the deferral hook lives there.
        """
        self._maybe_reload_config()
        if self.compaction.should_run(event):
            await self.compaction.run(_dispatch_module.DaemonContext(self))

    def _maybe_reload_config(self) -> None:
        """Reload alice.config.json if it has changed on disk.

        Hot-reload happens at event boundaries — each signal / surface /
        emergency turn begins with the freshest config. Alice's `write_config`
        tool therefore takes effect on her next turn, no daemon restart
        needed. Hot-reloadable: model, quiet_hours,
        working_context_token_budget, context_bootstrap_turns,
        context_compaction_threshold.
        """
        if not self._config_path.is_file():
            return
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        try:
            new_cfg = config_module.load()
        except Exception:  # noqa: BLE001
            log.exception("config reload failed; keeping current cfg")
            return
        old_speaking = self.cfg.speaking
        self.cfg = new_cfg
        self._config_mtime = mtime
        changes = {
            k: v
            for k, v in new_cfg.speaking.items()
            if old_speaking.get(k) != v
        }
        log.info("config reloaded (changes: %s)", list(changes.keys()) or "none observed")
        self.events.emit("config_reload", changes=list(changes.keys()))

    # ------------------------------------------------------------------
    # Per-event handlers (CLI, Discord, A2A, Signal, Surface, Emergency)
    # all live in :mod:`alice_speaking._dispatch` (Phase 1 of plan 01)
    # and are reached via the source registry (Phase 3) or — for Signal
    # — its per-transport consumer loop (Phase 2a). Daemon-side delegate
    # methods retired with Phase 3.

    def _build_discord_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
        """Compose the prompt for a single Discord DM. Mirrors the CLI/Signal
        prompts but advertises Discord's caps (limited markdown, 1900-byte
        chunks) so Alice writes in the right shape."""
        caps = self.discord_transport.caps if self.discord_transport else None
        cap_fragment = (
            render_module.capability_prompt_fragment("discord", caps)
            if caps is not None
            else ""
        )
        lines: list[str] = [
            f"[Discord DM from {principal_name} | {stamp}]",
            "",
            text,
            "",
            "---",
            cap_fragment,
            "",
            "To reply, call the `send_message` tool with "
            "recipient='self' (replies on the same Discord DM the "
            "message came from). Returning text alone will NOT reach "
            "the user.",
        ]
        return "\n".join(lines)

    def _build_a2a_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
        """Compose the prompt for a single A2A task. Reuses the CLI prompt
        capabilities (full markdown, large message size) — A2A consumers
        are typically other agents that handle structured output well."""
        caps = self.a2a_transport.caps if self.a2a_transport else None
        cap_fragment = (
            render_module.capability_prompt_fragment("a2a", caps)
            if caps is not None
            else ""
        )
        lines: list[str] = [
            f"[A2A task from {principal_name} | {stamp}]",
            "",
            text,
            "",
            "---",
            cap_fragment,
            "",
            "To reply, call the `send_message` tool with "
            "recipient='self' (the reply streams back over the same A2A "
            "task as text artifacts). Returning text alone will NOT reach "
            "the caller.",
        ]
        return "\n".join(lines)

    def _build_cli_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
        """Compose the prompt for a single CLI message.

        Mirrors the Signal prompt's structure but tells Alice the channel
        is local + interactive + markdown-capable, and instructs her to
        reply via send_message(recipient='self').
        """
        cli_caps = self.cli_transport.caps if self.cli_transport else None
        cap_fragment = (
            render_module.capability_prompt_fragment("cli", cli_caps)
            if cli_caps is not None
            else ""
        )
        lines: list[str] = [
            f"[CLI from {principal_name} | {stamp}]",
            "",
            text,
            "",
            "---",
            cap_fragment,
            "",
            "To reply, call the `send_message` tool with "
            "recipient='self' (replies on the same CLI socket the "
            "message came from). Returning text alone will NOT reach "
            "the user. If there's nothing useful to say, let the turn "
            "close silently — the client will see an empty response.",
        ]
        return "\n".join(lines)

    def _build_signal_prompt(
        self,
        *,
        sender_name: str,
        stamp: str,
        batch: list[SignalEvent],
    ) -> str:
        """Compose the per-turn prompt for one or more signal envelopes.

        Single-envelope batches use the simple original layout. Multi-
        envelope batches enumerate each message in arrival order with a
        per-message timestamp; attachments are listed inline under the
        message they came in with. Either way, the closing instruction
        block tells Alice how to reply via send_message.
        """
        lines: list[str] = []
        if len(batch) == 1:
            env = batch[0].envelope
            body = env.body or "(no text — see attachments below)"
            lines.extend([
                f"[Signal from {sender_name} | {stamp}]",
                "",
                body,
            ])
            if env.attachments:
                lines.append("")
                lines.append(
                    f"--- {len(env.attachments)} attachment"
                    f"{'s' if len(env.attachments) != 1 else ''} ---"
                )
                for att in env.attachments:
                    fn = f' "{att.filename}"' if att.filename else ""
                    lines.append(
                        f"- {att.path} ({att.content_type}{fn}) — "
                        f"use the Read tool to view."
                    )
        else:
            lines.extend([
                f"[Signal from {sender_name} | {stamp}]",
                f"{len(batch)} messages came in while you were busy — "
                "handle them together as one reply (or several, your call). "
                "Each is shown in arrival order:",
                "",
            ])
            for i, ev in enumerate(batch, start=1):
                env = ev.envelope
                ts_str = _format_envelope_time(env.timestamp)
                body = env.body or "(no text — see attachments below)"
                lines.append(f"--- message {i} of {len(batch)} (sent {ts_str}) ---")
                lines.append(body)
                if env.attachments:
                    for att in env.attachments:
                        fn = f' "{att.filename}"' if att.filename else ""
                        lines.append(
                            f"  attachment: {att.path} "
                            f"({att.content_type}{fn}) — Read it."
                        )
                lines.append("")
        lines.extend([
            "",
            "---",
            render_module.capability_prompt_fragment("signal", SIGNAL_CAPS),
            "",
            "To reply, call the `send_message` tool "
            "(recipient='self' to reply to the sender, a principal id "
            "from the address book to reach someone else, or an E.164 "
            "number; message=your reply text). Returning text alone "
            "will NOT send. If there's nothing to say, let the turn "
            "close silently.",
        ])
        return "\n".join(lines)

    async def _quiet_watcher(self) -> None:
        """Poll quiet-hours state; drain the queue on transition out."""
        was_quiet = is_quiet_hours(self.cfg.speaking)
        while not self._stop.is_set():
            await asyncio.sleep(QUIET_CHECK_SECONDS)
            now_quiet = is_quiet_hours(self.cfg.speaking)
            if was_quiet and not now_quiet:
                await self._drain_quiet_queue(reason="quiet-hours-ended")
            was_quiet = now_quiet

    async def _drain_quiet_queue(self, *, reason: str) -> None:
        messages = self.quiet_queue.drain()
        if not messages:
            return
        log.info("draining quiet queue (%d msgs) — %s", len(messages), reason)
        self.events.emit("quiet_queue_drain", count=len(messages), reason=reason)
        for msg in messages:
            channel = ChannelRef(
                transport=msg.transport,
                address=msg.recipient,
                durable=True,
            )
            try:
                await self._dispatch_outbound(
                    channel,
                    msg.text,
                    bypass_quiet=True,  # we're past the window already
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to send queued %s message to %s; re-queueing",
                    msg.transport,
                    msg.recipient,
                )
                self.quiet_queue.append(msg)

    # ------------------------------------------------------------------
    # Surface helpers — :func:`_dispatch.handle_surface` reaches back
    # into these via the DaemonContext proxy.

    def _archive_unresolved(self, path: pathlib.Path) -> None:
        today = datetime.date.today().isoformat()
        dest_dir = self._surface_watcher.handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + "verdict: (unresolved — Alice did not call resolve_surface)\n"
            + "action_taken: auto-archived by daemon\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info("auto-archived unresolved surface: %s", path.name)

    # ------------------------------------------------------------------
    # Emergency helpers — :func:`_dispatch.handle_emergency` reaches
    # back into these via the DaemonContext proxy. External monitors
    # drop files into inner/emergency/; emergency voice BYPASSES quiet
    # hours (the whole point). Alice voices via send_message like any
    # other turn; the daemon routes around the quiet-hours queue when
    # the sender context is "emergency".

    def _archive_emergency(
        self,
        path: pathlib.Path,
        *,
        verdict: str,
        action: str,
    ) -> None:
        today = datetime.date.today().isoformat()
        dest_dir = self._emergency_watcher.handled_dir / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        body = path.read_text()
        trailer = (
            "\n\n---\n"
            + f"resolved: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + f"verdict: {verdict}\n"
            + f"action_taken: {action}\n"
        )
        dest.write_text(body + trailer)
        path.unlink()
        log.info("emergency archived: %s (%s)", path.name, verdict)

    # ------------------------------------------------------------------
    # send_message router (closure given to tools.messaging)

    async def _send_message(
        self,
        recipient: ResolvedRecipient,
        text: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        """Dispatch send_message to the right transport and track did-send.

        Two recipient modes:

        - ``recipient == SELF_RECIPIENT`` — Alice asked to reply on the
          inbound channel. We use ``self._current_reply_channel`` directly
          and dispatch via the transport that owns it.
        - a :class:`ChannelRef` — an explicit channel resolved by the
          messaging tool (via the address book or a raw E.164 number).
          Routed through the transport identified by ``channel.transport``.

        Quiet-hours policy (signal + discord): replies to inbound turns
        and emergencies bypass the queue — the user is waiting on an
        answer. Surface-triggered sends (Alice's own thoughts) honor
        quiet hours and route through :class:`QuietQueue`. Attachments
        always bypass — the queue stores text-only payloads and dropping
        attachments there would silently ditch media.

        CLI sends never queue: the user is at a terminal waiting; quiet
        hours don't apply.
        """
        if recipient == SELF_RECIPIENT:
            channel = self._current_reply_channel
            if channel is None:
                raise RuntimeError(
                    "send_message(recipient='self') has no inbound channel "
                    "to reply on (only valid during a signal/cli/discord/"
                    "emergency turn)"
                )
        else:
            assert isinstance(recipient, ChannelRef)
            channel = recipient

        # CLI deliverability is now decided at write time inside
        # CLITransport.send: if the address book's CLI channel (uid)
        # has any live connection, the send broadcasts to all of them;
        # otherwise the transport logs and drops. The previous
        # pre-flight `durable=False` reject was too aggressive — it
        # blocked surface- and emergency-driven sends to "owner" even
        # when a TUI session was actively connected and addressable.

        emergency = getattr(self, "_emergency_bypass", False)
        # Bypass triggers: emergency-flavored turn, or we're inside an
        # inbound conversational turn whose user is waiting, or we'd have
        # to drop attachments to queue. CLI is always-bypass (interactive).
        bypass_quiet = (
            channel.transport in ("cli", "a2a")
            or emergency
            or self._current_turn_kind in ("signal", "discord", "cli", "a2a")
            or bool(attachments)
        )

        await self._dispatch_outbound(
            channel,
            text,
            attachments,
            emergency=emergency,
            bypass_quiet=bypass_quiet,
        )
        self._turn_last_outbound = text
        self._turn_did_send = True

    # ------------------------------------------------------------------
    # Unified outbound dispatch — Phase 6a of plan 01 lifted the
    # routing + quiet-queue + send-event code into
    # :class:`OutboxRouter`. Daemon-side helpers are thin facades
    # that pass the daemon's per-turn principal-display-name through.

    async def _dispatch_outbound(
        self,
        channel: ChannelRef,
        text: str,
        attachments: Optional[list[str]] = None,
        *,
        turn_id: Optional[str] = None,
        emergency: bool = False,
        bypass_quiet: bool = False,
    ) -> None:
        await self.outbox.dispatch(
            channel,
            text,
            attachments,
            turn_id=turn_id,
            emergency=emergency,
            bypass_quiet=bypass_quiet,
            principal_display_name=self._current_principal_display_name,
        )

    def _sender_name_for(self, recipient: str) -> str:
        return self.address_book.display_name_for("signal", recipient)

    # ------------------------------------------------------------------
    # Agent SDK invocation (shared by signal + surface + emergency + compaction)

    async def _run_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        outbound_recipient: Optional[str],
        silent: bool = False,
    ) -> str:
        """Execute one SDK turn through the agent kernel.

        ``silent=True`` marks the turn as internal (bootstrap or
        compaction) — no missed_reply event, no usage-threshold check,
        no session.json flap. ``outbound_recipient`` is informational
        for the missed_reply event.

        On Layer 1 failure (resume= points at a session the SDK no
        longer has) we clear self.session_id, prime the Layer 2
        bootstrap preamble, and transparently retry the same prompt
        with a fresh session.

        Returns the concatenated assistant text (useful for compaction
        turns which consume the summary).
        """
        self._turn_did_send = False
        self._turn_last_outbound = None   # reset per turn

        final_prompt = self._compose_prompt(prompt)
        spec = self._build_spec()
        handlers = self._build_handlers(silent=silent)

        kernel = AgentKernel(
            self.events,
            correlation_id=turn_id,
            silent=silent,
            # Generous so Opus's reasoning + replies aren't sliced mid-
            # sentence in the modal trace. Logs grow ~2x on busy days
            # but disk is cheap and the viewer's value depends on this.
            short_cap=4000,
        )

        try:
            result = await kernel.run(final_prompt, spec, handlers=handlers)
        except Exception as exc:  # noqa: BLE001
            # Layer 1 failure recovery: if resume= points at a stale
            # session, drop session state, prime Layer 2, and retry the
            # same prompt once with a fresh session.
            if self.session_id and _looks_like_missing_session(exc):
                self.events.emit(
                    "session_resume_failed",
                    turn_id=turn_id,
                    session_id=self.session_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                log.warning(
                    "resume=%s failed (%s); retrying with fresh session",
                    self.session_id,
                    type(exc).__name__,
                )
                self.session_id = None
                session_state.clear(self._session_path)
                self._prime_bootstrap_preamble()
                retry_prompt = self._compose_prompt(prompt)
                retry_spec = self._build_spec()
                retry_handlers = self._build_handlers(silent=silent)
                result = await kernel.run(
                    retry_prompt, retry_spec, handlers=retry_handlers
                )
            else:
                raise

        if result.is_error or result.error:
            # Kernel returned an error result (timeout, etc.). Callers
            # see an empty / partial text; the kernel already emitted
            # the specific error event. We just flow through.
            pass

        # Missed-reply observability: only meaningful when the turn was
        # supposed to be able to reach a user and Alice skipped it.
        if not silent and not self._turn_did_send:
            self.events.emit(
                "missed_reply",
                turn_id=turn_id,
                outbound_recipient=outbound_recipient,
                session_id=result.session_id,
            )

        return result.text

    def _compose_prompt(self, prompt: str) -> str:
        """Prepend the one-shot bootstrap preamble if one is pending."""
        if not self._pending_preamble:
            return prompt
        composed = f"{self._pending_preamble}\n\n{prompt}"
        self._pending_preamble = None
        return composed

    def _build_spec(self) -> KernelSpec:
        builtin_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]
        return KernelSpec(
            model=self.cfg.speaking.get("model"),
            allowed_tools=builtin_tools + self.custom_tool_names,
            mcp_servers=self.mcp_servers,
            cwd=self.cfg.work_dir,
            resume=self.session_id,
            # Adaptive thinking with summarized display so ThinkingBlocks
            # come back with non-empty text. Without display='summarized'
            # the SDK omits thinking text entirely (signature only) and
            # the viewer's trace shows empty (thought) rows.
            thinking={"type": "adaptive", "display": "summarized"},
        )

    def _build_handlers(self, *, silent: bool) -> list:
        """Compose the per-turn kernel handlers.

        Silent turns still need their session_id tracked (so the next
        turn passes ``resume=``) but skip the session.json write and the
        compaction arming — those are production-turn concerns only.
        """

        def _set_session_id(sid: str) -> None:
            self.session_id = sid

        def _arm_compaction() -> None:
            self.compaction.arm()

        handlers: list = [
            SessionHandler(
                session_path=self._session_path,
                set_session_id=_set_session_id,
                persist=not silent,
            ),
        ]
        if not silent:
            threshold = int(
                self.cfg.speaking.get(
                    "context_compaction_threshold",
                    compaction_module.DEFAULT_THRESHOLD,
                )
            )
            handlers.append(
                CompactionArmer(threshold=threshold, arm=_arm_compaction)
            )
        # CLI trace pass-through. No-op when the active channel isn't
        # CLI, so safe to install for every turn (signal/discord/surface
        # turns all silently skip). Installed for silent turns too —
        # bootstrap/compaction trace events would just have no listener.
        if self.cli_transport is not None:
            handlers.append(
                CLITraceHandler(
                    transport=self.cli_transport,
                    get_channel=lambda: self._current_reply_channel,
                )
            )
        return handlers

    # ------------------------------------------------------------------
    # Layer 2 bootstrap + compaction

    def _prime_bootstrap_preamble(self) -> None:
        """When we have no warm session, prime the next turn with a
        turn_log-derived preamble (+ compaction summary if present).

        This covers three cases:
        - Daemon start with no session.json: bootstrap from turn_log.
        - session.json pointed at a stale SDK session: same bootstrap.
        - Just rolled the session after compaction: inject summary +
          tail. (That caller also sets self.session_id = None before
          priming.)

        Empty preamble means "first boot, no turn history" — we just
        start fresh.
        """
        if self.session_id is not None:
            return
        summary = compaction_module.read_summary_if_any(self._summary_path)
        if summary:
            tail = self.turns.tail(SUMMARY_TAIL_TURNS)
            self._pending_preamble = compaction_module.build_summary_preamble(
                summary, tail
            )
            self.events.emit(
                "context_bootstrap",
                source="summary",
                tail_len=len(tail),
            )
            log.info("primed bootstrap preamble from context summary")
            return

        bootstrap_turns = int(
            self.cfg.speaking.get("context_bootstrap_turns", 20)
        )
        tail = self.turns.tail(bootstrap_turns)
        preamble = compaction_module.build_bootstrap_preamble(tail)
        if preamble:
            self._pending_preamble = preamble
            self.events.emit(
                "context_bootstrap",
                source="turn_log",
                tail_len=len(tail),
            )
            log.info(
                "primed bootstrap preamble from turn_log (%d turns)", len(tail)
            )

    # Compaction execution lives on :class:`CompactionTrigger`
    # (Phase 6b of plan 01); reach it via ``self.compaction.run``.


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = config_module.load()
    log.info("speaking alice starting (model=%s)", cfg.speaking.get("model"))
    daemon = SpeakingDaemon(cfg)
    await daemon.run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
