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
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Union

from alice_core.kernel import AgentKernel, KernelSpec
from alice_core.sdk_compat import _short, looks_like_missing_session as _looks_like_missing_session

from . import compaction as compaction_module
from . import config as config_module
from . import principals as principals_module
from . import render as render_module
from . import session_state
from . import tools as tools_module
from .config import Config
from .dedup import DedupStore
from .events import EventLogger
from .handlers import CompactionArmer, SessionHandler
from .principals import AddressBook
from .quiet_hours import QueuedMessage, QuietQueue, is_quiet_hours
from .signal_client import SignalClient, SignalEnvelope
from .tools.messaging import SELF_RECIPIENT, ResolvedRecipient
from .transports import (
    CLITransport,
    ChannelRef,
    DiscordTransport,
    InboundMessage,
    OutboundMessage,
    SignalTransport,
)
from .transports.base import SIGNAL_CAPS
from .turn_log import TurnLog, new_turn


log = logging.getLogger("alice_speaking")


SURFACE_POLL_SECONDS = 5.0
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


@dataclass
class SignalEvent:
    envelope: SignalEnvelope
    sender_name: str


@dataclass
class SurfaceEvent:
    path: pathlib.Path


@dataclass
class EmergencyEvent:
    path: pathlib.Path


@dataclass
class CLIEvent:
    """A message that came in over the CLI transport (Unix socket).

    Mirrors :class:`SignalEvent` but for the local CLI transport.
    Carries the full :class:`InboundMessage` so the handler can find the
    reply channel without extra plumbing.
    """

    message: InboundMessage


@dataclass
class DiscordEvent:
    """A message that came in over the Discord transport.

    Same shape as :class:`CLIEvent` — the inbound :class:`InboundMessage`
    carries everything the handler needs. Discord channels are durable
    (DM history persists), unlike CLI's ephemeral sockets.
    """

    message: InboundMessage


Event = Union[SignalEvent, SurfaceEvent, EmergencyEvent, CLIEvent, DiscordEvent]


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
        self.signal = SignalClient(
            api=cfg.signal_api,
            account=cfg.signal_account,
            log_path=cfg.signal_log_path,
            offset_path=cfg.offset_path,
        )
        # Phase 2: SignalTransport wraps the SignalClient under the
        # Transport interface. Phase 2a constructs it; Phase 2b cuts
        # outbound dispatch over to it.
        self.signal_transport = SignalTransport(signal_client=self.signal)
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

        # Compaction bookkeeping.
        self._compaction_pending: bool = False
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
        # deploys keep working unchanged.
        self.discord_transport: Optional[DiscordTransport] = (
            DiscordTransport(token=cfg.discord_bot_token)
            if cfg.discord_bot_token
            else None
        )

        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=64)
        self._dispatched_surfaces: set[str] = set()
        self._stop = asyncio.Event()
        self._surface_dir = cfg.mind_dir / "inner" / "surface"
        self._surface_handled_dir = self._surface_dir / ".handled"
        self._emergency_dir = cfg.mind_dir / "inner" / "emergency"
        self._emergency_handled_dir = self._emergency_dir / ".handled"
        self._dispatched_emergencies: set[str] = set()
        self._config_path = cfg.mind_dir / "config" / "alice.config.json"
        self._config_mtime: float = (
            self._config_path.stat().st_mtime if self._config_path.is_file() else 0.0
        )

    # ------------------------------------------------------------------
    # Lifecycle

    async def run(self) -> None:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.cfg.oauth_token

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
            log.info("waiting for signal-cli at %s", self.cfg.signal_api)
            await self.signal.wait_ready()
            await self.signal_transport.start()
            log.info("daemon ready; listening")
            self.events.emit("daemon_ready", signal_api=self.cfg.signal_api)

            # If quiet hours ended while we were down, drain the queue first.
            if not is_quiet_hours(self.cfg.speaking) and self.quiet_queue.size() > 0:
                await self._drain_quiet_queue(reason="startup")

            # Prime the Layer 2 bootstrap preamble if we don't have a
            # session to resume. The consumer picks it up on the first turn.
            self._prime_bootstrap_preamble()

            producers = [
                asyncio.create_task(self._signal_producer(), name="sig-produce"),
                asyncio.create_task(self._surface_producer(), name="sur-produce"),
                asyncio.create_task(self._emergency_producer(), name="emg-produce"),
                asyncio.create_task(self._quiet_watcher(), name="quiet-watch"),
            ]
            if self.cli_transport is not None:
                await self.cli_transport.start()
                producers.append(
                    asyncio.create_task(self._cli_producer(), name="cli-produce")
                )
            if self.discord_transport is not None:
                await self.discord_transport.start()
                producers.append(
                    asyncio.create_task(
                        self._discord_producer(), name="discord-produce"
                    )
                )
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
            with contextlib.suppress(Exception):
                await self.signal_transport.stop()
            await self.signal.aclose()
            if self.cli_transport is not None:
                with contextlib.suppress(Exception):
                    await self.cli_transport.stop()
            if self.discord_transport is not None:
                with contextlib.suppress(Exception):
                    await self.discord_transport.stop()
            self.events.emit("shutdown")
            log.info("shutdown complete")

    # ------------------------------------------------------------------
    # Producers

    async def _signal_producer(self) -> None:
        async for env in self.signal.receive():
            if not self.address_book.is_allowed("signal", env.source):
                log.info("ignoring envelope from %s", env.source)
                continue
            if self.dedup.seen(env.timestamp):
                log.debug("duplicate ts=%d; skipping", env.timestamp)
                continue
            self.dedup.mark(env.timestamp)
            sender_name = self.address_book.display_name_for("signal", env.source)
            await self._queue.put(
                SignalEvent(envelope=env, sender_name=sender_name)
            )

    async def _surface_producer(self) -> None:
        """Watch ``inner/surface/`` for new .md surfaces and queue them.

        Flat-file only. Files in subdirs of ``inner/surface/`` (other than
        ``.handled/``) will not be picked up — the glob is non-recursive.
        Producers of surfaces (thinking Alice, manual injects) must drop
        files at the top level of the surface directory.
        """
        self._surface_dir.mkdir(parents=True, exist_ok=True)
        self._surface_handled_dir.mkdir(parents=True, exist_ok=True)
        # One-shot drift check — warn if surfaces are stranded in subdirs
        for entry in self._surface_dir.iterdir():
            if entry.is_dir() and entry.name not in (".handled",):
                md_count = len(list(entry.glob("*.md")))
                if md_count:
                    log.warning(
                        "surface drift: %d .md file(s) in subdir %s — "
                        "_surface_producer is non-recursive; these will not dispatch. "
                        "Move them to flat inner/surface/ format.",
                        md_count, entry.name,
                    )
        while not self._stop.is_set():
            try:
                for path in sorted(self._surface_dir.glob("*.md")):
                    if path.name.startswith(".") or path.name in self._dispatched_surfaces:
                        continue
                    self._dispatched_surfaces.add(path.name)
                    log.info("surface detected: %s", path.name)
                    await self._queue.put(SurfaceEvent(path=path))
            except OSError as exc:
                log.warning("surface poll error: %s", exc)
            await asyncio.sleep(SURFACE_POLL_SECONDS)

    async def _emergency_producer(self) -> None:
        self._emergency_dir.mkdir(parents=True, exist_ok=True)
        self._emergency_handled_dir.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            try:
                for path in sorted(self._emergency_dir.glob("*.md")):
                    if path.name.startswith(".") or path.name in self._dispatched_emergencies:
                        continue
                    self._dispatched_emergencies.add(path.name)
                    log.warning("EMERGENCY detected: %s", path.name)
                    await self._queue.put(EmergencyEvent(path=path))
            except OSError as exc:
                log.warning("emergency poll error: %s", exc)
            await asyncio.sleep(SURFACE_POLL_SECONDS)

    async def _cli_producer(self) -> None:
        """Pump InboundMessages from the CLI transport onto the consumer queue."""
        assert self.cli_transport is not None
        async for msg in self.cli_transport.messages():
            await self._queue.put(CLIEvent(message=msg))

    async def _discord_producer(self) -> None:
        """Pump InboundMessages from the Discord transport, ACL-gated.

        Inbound from a Discord user not in the address book is dropped
        silently — same rule as the Signal producer.
        """
        assert self.discord_transport is not None
        async for msg in self.discord_transport.messages():
            if not self.address_book.is_allowed("discord", msg.principal.native_id):
                log.info(
                    "ignoring discord message from unknown user %s",
                    msg.principal.native_id,
                )
                continue
            # Refresh display name in the address book if the inbound
            # carried a richer one (Discord users can change global_name).
            self.address_book.learn(msg)
            await self._queue.put(DiscordEvent(message=msg))

    # ------------------------------------------------------------------
    # Consumer

    async def _consumer(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                self._maybe_reload_config()
                # Compaction runs BEFORE any inbound event so the token
                # check from the previous turn has a chance to roll the
                # session before we append more context.
                if self._compaction_pending:
                    await self._run_compaction()

                if isinstance(event, SignalEvent):
                    # Coalesce any other queued signals from the same
                    # sender so Alice handles a burst in one turn instead
                    # of N back-to-back ones — same UX as Claude Code's
                    # input queue while a turn is mid-flight.
                    batch = self._drain_signal_batch(event)
                    await self._handle_signal(batch)
                elif isinstance(event, CLIEvent):
                    await self._handle_cli(event)
                elif isinstance(event, DiscordEvent):
                    await self._handle_discord(event)
                elif isinstance(event, SurfaceEvent):
                    await self._handle_surface(event)
                elif isinstance(event, EmergencyEvent):
                    await self._handle_emergency(event)
            except Exception:
                log.exception("consumer error handling %s", type(event).__name__)
            finally:
                self._queue.task_done()

    def _drain_signal_batch(self, head: SignalEvent) -> list["SignalEvent"]:
        """Pull all currently-queued SignalEvents from head's source into a
        batch. Non-matching events (from a different sender, or surfaces /
        emergencies) get put back on the queue in their original order — so
        the next consumer iteration sees them unchanged.

        Best-effort coalescing: anything that arrives during the turn this
        batch produces will hit the next consumer iteration. Like Claude
        Code, queued input applies to the NEXT turn, not the current one.
        """
        batch: list[SignalEvent] = [head]
        held: list = []
        while True:
            try:
                ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if (
                isinstance(ev, SignalEvent)
                and ev.envelope.source == head.envelope.source
            ):
                batch.append(ev)
            else:
                held.append(ev)
        for ev in held:
            self._queue.put_nowait(ev)
        return batch

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
    # Signal turn — no auto-capture; Alice replies via send_message.

    async def _handle_signal(self, batch: list[SignalEvent]) -> None:
        """Process a batch of one or more SignalEvents from the same sender.

        All events in the batch share the same source + sender (caller
        guarantees, via :meth:`_drain_signal_batch`). One kernel turn
        handles the whole batch; the prompt enumerates each message in
        arrival order, with timestamps + attachments.
        """
        if not batch:
            return
        head = batch[0]
        sender_name = head.sender_name
        source = head.envelope.source
        quiet = is_quiet_hours(self.cfg.speaking)
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()

        all_attachments = [a for ev in batch for a in ev.envelope.attachments]
        total_chars = sum(len(ev.envelope.body) for ev in batch)
        inbound_preview = " ┃ ".join(
            _short(ev.envelope.body, 200) for ev in batch if ev.envelope.body
        ) or f"({len(all_attachments)} attachment(s), no text)"

        self.events.emit(
            "signal_turn_start",
            turn_id=turn_id,
            sender_name=sender_name,
            sender_number=source,
            message_count=len(batch),
            inbound_chars=total_chars,
            inbound=_short(inbound_preview, 600),
            attachments=[
                {
                    "id": a.id,
                    "path": str(a.path),
                    "content_type": a.content_type,
                    "filename": a.filename,
                }
                for a in all_attachments
            ],
            quiet=quiet,
        )
        if len(batch) > 1:
            log.info(
                "batched %d signal messages from %s into one turn",
                len(batch),
                sender_name,
            )

        error: Optional[str] = None
        prev_kind = self._current_turn_kind
        prev_channel = self._current_reply_channel
        self._current_turn_kind = "signal"
        channel = ChannelRef(transport="signal", address=source, durable=True)
        self._current_reply_channel = channel
        # Replies to inbound bypass quiet hours — Owner expects an answer
        # when he asks something, regardless of the clock. Typing indicator
        # fires too so he sees Alice working.
        await self.signal_transport.typing(channel, True)
        try:
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            prompt = self._build_signal_prompt(
                sender_name=sender_name, stamp=stamp, batch=batch
            )
            await self._run_turn(prompt, turn_id=turn_id, outbound_recipient=source)
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed for %s", sender_name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                # Signal turn errors bypass the quiet queue too — same rule
                # applies to error notices as to replies.
                await self.signal_transport.send(
                    OutboundMessage(
                        destination=channel,
                        text=f"Hit an error ({type(exc).__name__}). Session preserved — reply to retry.",
                    )
                )
        finally:
            self._current_turn_kind = prev_kind
            self._current_reply_channel = prev_channel
            await self.signal_transport.typing(channel, False)
            # One turn_log entry per envelope so the inbound audit trail
            # is preserved regardless of batch size. Only the LAST envelope
            # in the batch carries the outbound text — earlier envelopes
            # get None so render_for_prompt() doesn't emit duplicate
            # `[alice]` lines for what was a single reply.
            for i, ev in enumerate(batch):
                self.turns.append(
                    new_turn(
                        sender_number=ev.envelope.source,
                        sender_name=sender_name,
                        inbound=ev.envelope.body,
                        outbound=(
                            self._turn_last_outbound
                            if i == len(batch) - 1
                            else None
                        ),
                        error=error,
                    )
                )
            self.events.emit(
                "signal_turn_end",
                turn_id=turn_id,
                sender_name=sender_name,
                message_count=len(batch),
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )

    # ------------------------------------------------------------------
    # CLI turn — local-socket transport for terminal users + agents.

    async def _handle_cli(self, event: CLIEvent) -> None:
        """Run one turn for a CLI message and signal completion to the
        client when done.

        CLI is conversational like Signal but ephemeral — the client
        connection may have closed by the time the turn finishes. The
        :class:`CLITransport` handles missing-writer cases by logging
        and dropping; we don't need to detect them here.
        """
        assert self.cli_transport is not None
        msg = event.message
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()

        self.events.emit(
            "cli_turn_start",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            display_name=msg.principal.display_name,
            inbound_chars=len(msg.text),
            inbound=_short(msg.text, 600),
        )

        prev_kind = self._current_turn_kind
        prev_channel = self._current_reply_channel
        self._current_turn_kind = "cli"
        self._current_reply_channel = msg.origin
        error: Optional[str] = None
        try:
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            prompt = self._build_cli_prompt(
                principal_name=msg.principal.display_name,
                stamp=stamp,
                text=msg.text,
            )
            await self._run_turn(
                prompt,
                turn_id=turn_id,
                outbound_recipient=f"cli:{msg.principal.native_id}",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("cli turn failed for %s", msg.principal.display_name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await self.cli_transport.signal_error(msg.origin, error)
        finally:
            # Always tell the client the turn ended, even if Alice never
            # called send_message — they need {"type":"done"} to know to
            # prompt the user again. Errors are sent above, so we only
            # send "done" on the success path.
            if error is None:
                with contextlib.suppress(Exception):
                    await self.cli_transport.signal_done(msg.origin)
            self._current_turn_kind = prev_kind
            self._current_reply_channel = prev_channel
            self.events.emit(
                "cli_turn_end",
                turn_id=turn_id,
                principal_id=msg.principal.native_id,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )

    # ------------------------------------------------------------------
    # Discord turn — DMs only in Phase 3b.

    async def _handle_discord(self, event: DiscordEvent) -> None:
        """Run one turn for a Discord DM. Same shape as
        :meth:`_handle_cli` but the channel is durable, so a missed
        send_message just shows up as silence to the user (no
        ``signal_done`` analog — Discord clients don't have a pending
        prompt to clear)."""
        assert self.discord_transport is not None
        msg = event.message
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()

        self.events.emit(
            "discord_turn_start",
            turn_id=turn_id,
            principal_id=msg.principal.native_id,
            display_name=msg.principal.display_name,
            inbound_chars=len(msg.text),
            inbound=_short(msg.text, 600),
        )

        prev_kind = self._current_turn_kind
        prev_channel = self._current_reply_channel
        self._current_turn_kind = "discord"
        self._current_reply_channel = msg.origin
        await self.discord_transport.typing(msg.origin, True)
        error: Optional[str] = None
        try:
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            prompt = self._build_discord_prompt(
                principal_name=msg.principal.display_name,
                stamp=stamp,
                text=msg.text,
            )
            await self._run_turn(
                prompt,
                turn_id=turn_id,
                outbound_recipient=f"discord:{msg.principal.native_id}",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("discord turn failed for %s", msg.principal.display_name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await self.discord_transport.send(
                    OutboundMessage(
                        destination=msg.origin,
                        text=(
                            f"Hit an error ({type(exc).__name__}). "
                            "Session preserved — reply to retry."
                        ),
                    )
                )
        finally:
            self._current_turn_kind = prev_kind
            self._current_reply_channel = prev_channel
            self.events.emit(
                "discord_turn_end",
                turn_id=turn_id,
                principal_id=msg.principal.native_id,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )

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
            "(recipient='jason' or 'katie' or an E.164 number, "
            "message=your reply text). Returning text alone will NOT "
            "send. If there's nothing to say, let the turn close silently.",
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
    # Surface turn

    async def _handle_surface(self, event: SurfaceEvent) -> None:
        path = event.path
        if not path.is_file():
            # Already handled by someone else (race). Nothing to do.
            self._dispatched_surfaces.discard(path.name)
            return
        body = path.read_text()
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()
        self.events.emit(
            "surface_dispatch",
            turn_id=turn_id,
            surface_id=path.name,
            chars=len(body),
            body=_short(body),
        )
        prompt = (
            f"[Internal — a thought just surfaced from reflection: {path.name}]\n\n"
            f"{body}\n\n"
            "This is your own thought that just came to you. Decide what to do: "
            "voice it to the user via the `send_message` tool, file it into "
            "memory, reply to thinking via a note (append_note), or let it "
            "pass. When you've decided, call mcp__alice__resolve_surface with "
            "the file's `id` (its filename), a short `verdict`, and "
            "`action_taken`. If you voice it, call send_message BEFORE "
            "resolve_surface."
        )
        error: Optional[str] = None
        prev_kind = self._current_turn_kind
        self._current_turn_kind = "surface"
        try:
            # Surface turns don't have a single inbound recipient; the
            # ``outbound_recipient`` is informational only. Quiet hours
            # apply here — Alice's own thoughts wait for morning.
            await self._run_turn(prompt, turn_id=turn_id, outbound_recipient=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("surface turn failed for %s", path.name)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._current_turn_kind = prev_kind
            if path.is_file():
                try:
                    self._archive_unresolved(path)
                except OSError as exc:
                    log.warning("unresolved-archive failed for %s: %s", path.name, exc)
            self._dispatched_surfaces.discard(path.name)
            self.events.emit(
                "surface_turn_end",
                turn_id=turn_id,
                surface_id=path.name,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )

    def _archive_unresolved(self, path: pathlib.Path) -> None:
        today = datetime.date.today().isoformat()
        dest_dir = self._surface_handled_dir / today
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
    # Emergency turn
    #
    # External monitors drop files into inner/emergency/. Emergency voice
    # BYPASSES quiet hours — that's the whole point. Alice voices via
    # send_message like any other turn; the daemon routes around the
    # quiet-hours queue when the sender context is "emergency".

    async def _handle_emergency(self, event: EmergencyEvent) -> None:
        path = event.path
        if not path.is_file():
            self._dispatched_emergencies.discard(path.name)
            return
        body = path.read_text()
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()
        self.events.emit(
            "emergency_dispatch",
            turn_id=turn_id,
            emergency_id=path.name,
            chars=len(body),
            body=_short(body),
        )

        emergency_channel = self.address_book.emergency_recipient()
        if emergency_channel is None:
            log.error(
                "emergency %s: no signal-capable principal in address book",
                path.name,
            )
            self.events.emit(
                "emergency_no_recipient",
                turn_id=turn_id,
                emergency_id=path.name,
            )
            self._archive_emergency(path, verdict="no-recipient", action="daemon-archived")
            return
        recipient = emergency_channel.address

        prompt = (
            f"[EMERGENCY — signal from an external monitor: {path.name}]\n\n"
            f"{body}\n\n"
            "Review this emergency. Verify the frontmatter contains "
            "`evidence_paths` with at least one verifiable source. If the "
            "evidence is insufficient, do NOT call send_message — let the "
            "turn close and the daemon will archive it as downgraded.\n\n"
            "If the emergency is real, call `send_message` to voice it. "
            "Your send_message call during an emergency bypasses quiet "
            "hours automatically. Be concise and direct — name the "
            "emergency, the evidence, and the recommended action in one "
            "short message."
        )

        # For this turn only, flip the emergency bypass so _send_message
        # sends directly even during quiet hours, and label the turn
        # kind so other guards know we're in emergency.
        was_emergency = getattr(self, "_emergency_bypass", False)
        prev_kind = self._current_turn_kind
        prev_channel = self._current_reply_channel
        self._emergency_bypass = True
        self._current_turn_kind = "emergency"
        # Emergency reply channel = the address book's emergency
        # recipient. recipient='self' on an emergency turn routes here.
        self._current_reply_channel = emergency_channel
        verdict = "unknown"
        action = "none"
        try:
            await self._run_turn(
                prompt, turn_id=turn_id, outbound_recipient=recipient
            )
            if self._turn_did_send:
                verdict = "voiced"
                action = f"sent to {recipient} via send_message (bypassed quiet hours)"
                self.events.emit(
                    "emergency_voiced",
                    turn_id=turn_id,
                    emergency_id=path.name,
                    recipient=recipient,
                )
            else:
                verdict = "downgraded"
                action = "alice did not call send_message — no evidence or false positive"
                self.events.emit(
                    "emergency_downgraded",
                    turn_id=turn_id,
                    emergency_id=path.name,
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("emergency turn failed for %s", path.name)
            verdict = "error"
            action = f"{type(exc).__name__}: {exc}"
            self.events.emit(
                "emergency_error",
                turn_id=turn_id,
                emergency_id=path.name,
                error=action,
            )
        finally:
            self._emergency_bypass = was_emergency
            self._current_turn_kind = prev_kind
            self._current_reply_channel = prev_channel
            if path.is_file():
                self._archive_emergency(path, verdict=verdict, action=action)
            self._dispatched_emergencies.discard(path.name)
            self.events.emit(
                "emergency_turn_end",
                turn_id=turn_id,
                emergency_id=path.name,
                verdict=verdict,
                duration_ms=int((time.time() - started) * 1000),
            )

    def _archive_emergency(
        self,
        path: pathlib.Path,
        *,
        verdict: str,
        action: str,
    ) -> None:
        today = datetime.date.today().isoformat()
        dest_dir = self._emergency_handled_dir / today
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

        # CLI deliverability — refuse explicit sends to dead ephemeral
        # channels rather than dropping them silently.
        if (
            channel.transport == "cli"
            and not channel.durable
            and recipient != SELF_RECIPIENT
        ):
            raise RuntimeError(
                "cannot send to ephemeral cli channel outside its "
                "originating turn — use recipient='self' during the "
                "inbound CLI turn instead"
            )

        emergency = getattr(self, "_emergency_bypass", False)
        # Bypass triggers: emergency-flavored turn, or we're inside an
        # inbound conversational turn whose user is waiting, or we'd have
        # to drop attachments to queue. CLI is always-bypass (interactive).
        bypass_quiet = (
            channel.transport == "cli"
            or emergency
            or self._current_turn_kind in ("signal", "discord", "cli")
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
    # Unified outbound dispatch
    #
    # One function = one path = one event emission. Replaces the old
    # branched-by-transport routing in ``_send_message`` (which had two
    # ``signal_send`` emit sites with subtly different field shapes) and
    # the signal-only ``_send_or_queue``.

    def _transport_for(self, name: str):
        return {
            "signal": self.signal_transport,
            "cli": self.cli_transport,
            "discord": self.discord_transport,
        }.get(name)

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
        """Deliver ``text`` to ``channel``. Honors quiet hours (when not
        bypassed) for durable transports; emits the canonical
        ``<transport>_send`` event after delivery."""
        transport = self._transport_for(channel.transport)
        if transport is None:
            raise RuntimeError(
                f"transport {channel.transport!r} is not available "
                "(disabled or not configured)"
            )

        # Queueable transports: signal, discord. CLI never queues.
        queueable = channel.transport in ("signal", "discord")
        if queueable and not bypass_quiet and is_quiet_hours(self.cfg.speaking):
            self.quiet_queue.append(
                QueuedMessage(
                    transport=channel.transport,
                    recipient=channel.address,
                    text=text,
                    queued_at=time.time(),
                )
            )
            sender_name = self.address_book.display_name_for(
                channel.transport, channel.address
            )
            log.info(
                "quiet hours: queued %s reply for %s (%d chars); queue size=%d",
                channel.transport,
                sender_name,
                len(text),
                self.quiet_queue.size(),
            )
            self.events.emit(
                "quiet_queue_enter",
                turn_id=turn_id,
                transport=channel.transport,
                recipient=channel.address,
                sender_name=sender_name,
                text_len=len(text),
                queue_size=self.quiet_queue.size(),
            )
            return

        if attachments and channel.transport != "signal":
            log.warning(
                "ignoring %d attachment(s) for %s reply; transport doesn't "
                "support outbound files yet",
                len(attachments),
                channel.transport,
            )
            attachments = None

        chunk_count = await transport.send(
            OutboundMessage(
                destination=channel,
                text=text,
                attachments=list(attachments) if attachments else [],
            )
        )
        log.info(
            "%s send to %s (%d chars%s)",
            "emergency" if emergency else "reply",
            channel.address,
            len(text),
            f", {len(attachments)} attachment(s)" if attachments else "",
        )
        self._emit_send_event(
            channel=channel,
            text_len=len(text),
            chunk_count=chunk_count,
            attachment_count=len(attachments) if attachments else 0,
            emergency=emergency,
            bypassed_quiet=is_quiet_hours(self.cfg.speaking),
            turn_id=turn_id,
        )

    def _emit_send_event(
        self,
        *,
        channel: ChannelRef,
        text_len: int,
        chunk_count: int,
        attachment_count: int,
        emergency: bool,
        bypassed_quiet: bool,
        turn_id: Optional[str],
    ) -> None:
        """Single canonical ``<transport>_send`` event shape across all
        transports. ``bypassed_quiet`` is ``True`` only when delivery
        happened despite the wall-clock being inside the quiet window
        (i.e. a real bypass took effect, not just "we sent at 3pm")."""
        sender_name = self.address_book.display_name_for(
            channel.transport, channel.address
        )
        self.events.emit(
            f"{channel.transport}_send",
            turn_id=turn_id,
            recipient=channel.address,
            sender_name=sender_name,
            text_len=text_len,
            chunk_count=chunk_count,
            attachment_count=attachment_count,
            emergency=emergency,
            bypassed_quiet=bypassed_quiet,
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
        builtin_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
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
            self._compaction_pending = True

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

    async def _run_compaction(self) -> None:
        """Run a silent compaction turn, write the summary, roll the
        session."""
        turn_id = f"compact-{uuid.uuid4().hex[:8]}"
        self.events.emit("context_compaction_start", turn_id=turn_id)
        try:
            summary = await self._run_turn(
                compaction_module.COMPACTION_PROMPT,
                turn_id=turn_id,
                outbound_recipient=None,
                silent=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("compaction turn failed; leaving session intact")
            self.events.emit(
                "context_compaction_error",
                turn_id=turn_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._compaction_pending = False
            return

        if not summary:
            log.warning("compaction turn returned empty summary; rolling anyway")
            summary = "(compaction produced no summary — session rolled on empty)"

        try:
            compaction_module.write_summary(self._summary_path, summary)
        except OSError:
            log.exception("failed to write %s", self._summary_path)

        # Roll.
        old_sid = self.session_id
        self.session_id = None
        session_state.clear(self._session_path)
        self._compaction_pending = False
        self.events.emit(
            "context_compaction",
            turn_id=turn_id,
            summary_len=len(summary),
            previous_session_id=old_sid,
        )
        self.events.emit("session_roll", previous_session_id=old_sid)

        # Prime the next real turn with the summary injection.
        self._prime_bootstrap_preamble()


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
