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
- Compaction: after each turn, if usage.input_tokens exceeds
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
from . import session_state
from . import tools as tools_module
from .config import AllowedSender, Config
from .dedup import DedupStore
from .events import EventLogger
from .handlers import CompactionArmer, SessionHandler
from .quiet_hours import QueuedMessage, QuietQueue, is_quiet_hours
from .signal_client import SignalClient, SignalEnvelope
from .turn_log import TurnLog, new_turn


log = logging.getLogger("alice_speaking")


SURFACE_POLL_SECONDS = 5.0
QUIET_CHECK_SECONDS = 30.0

# Turns after which tail-trim happens when composing the summary preamble.
# Matches the design: 5 verbatim turns bridge the gap between summary
# cutoff and now.
SUMMARY_TAIL_TURNS = 5


@dataclass
class SignalEvent:
    envelope: SignalEnvelope
    sender: AllowedSender


@dataclass
class SurfaceEvent:
    path: pathlib.Path


@dataclass
class EmergencyEvent:
    path: pathlib.Path


Event = Union[SignalEvent, SurfaceEvent, EmergencyEvent]


class SpeakingDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.signal = SignalClient(
            api=cfg.signal_api,
            account=cfg.signal_account,
            log_path=cfg.signal_log_path,
            offset_path=cfg.offset_path,
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
            cfg, sender=self._send_message
        )

        # Compaction bookkeeping.
        self._compaction_pending: bool = False
        # Per-turn did-send tracker. Set back to False at the start of each
        # call to _run_turn(); flipped to True by _send_message when Alice
        # explicitly sends. Used to flag missed_reply events.
        self._turn_did_send: bool = False
        # When set, the very next turn will prepend this text as a
        # bootstrap preamble (Layer 2 restart OR post-compaction summary
        # injection).
        self._pending_preamble: Optional[str] = None
        # One-shot consumer startup guard.
        self._consumer_started: bool = False

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
            await self.signal.aclose()
            self.events.emit("shutdown")
            log.info("shutdown complete")

    # ------------------------------------------------------------------
    # Producers

    async def _signal_producer(self) -> None:
        async for env in self.signal.receive():
            if env.source not in self.cfg.allowed_senders:
                log.info("ignoring envelope from %s", env.source)
                continue
            if self.dedup.seen(env.timestamp):
                log.debug("duplicate ts=%d; skipping", env.timestamp)
                continue
            self.dedup.mark(env.timestamp)
            sender = self.cfg.allowed_senders[env.source]
            await self._queue.put(SignalEvent(envelope=env, sender=sender))

    async def _surface_producer(self) -> None:
        self._surface_dir.mkdir(parents=True, exist_ok=True)
        self._surface_handled_dir.mkdir(parents=True, exist_ok=True)
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
                    await self._handle_signal(event)
                elif isinstance(event, SurfaceEvent):
                    await self._handle_surface(event)
                elif isinstance(event, EmergencyEvent):
                    await self._handle_emergency(event)
            except Exception:
                log.exception("consumer error handling %s", type(event).__name__)
            finally:
                self._queue.task_done()

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

    async def _handle_signal(self, event: SignalEvent) -> None:
        env = event.envelope
        sender = event.sender
        quiet = is_quiet_hours(self.cfg.speaking)
        turn_id = uuid.uuid4().hex[:12]
        started = time.time()

        self.events.emit(
            "signal_turn_start",
            turn_id=turn_id,
            sender_name=sender.name,
            sender_number=env.source,
            inbound_chars=len(env.body),
            inbound=_short(env.body),
            quiet=quiet,
        )

        if not quiet:
            await self.signal.start_typing(env.source)
        error: Optional[str] = None
        try:
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            prompt = (
                f"[Signal from {sender.name} | {stamp}]\n\n"
                f"{env.body}\n\n"
                "To reply, call the `send_message` tool "
                "(recipient='jason' or 'katie' or an E.164 number, "
                "message=your reply text). Returning text alone will NOT "
                "send. If there's nothing to say, let the turn close silently."
            )
            await self._run_turn(prompt, turn_id=turn_id, outbound_recipient=env.source)
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed for %s", sender.name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                # Errors still go through send_or_queue directly because
                # the failing turn couldn't complete a send_message call.
                await self._send_or_queue(
                    env.source,
                    f"Hit an error ({type(exc).__name__}). Session preserved — reply to retry.",
                    sender.name,
                    turn_id=turn_id,
                )
        finally:
            if not quiet:
                await self.signal.stop_typing(env.source)
            self.turns.append(
                new_turn(
                    sender_number=env.source,
                    sender_name=sender.name,
                    inbound=env.body,
                    # outbound is no longer captured at this layer — Alice
                    # invokes send_message herself. We leave outbound=None
                    # here; the turn_log becomes a record of inbound +
                    # error only. (Rich outbound observability lives in
                    # the event log via signal_send events.)
                    outbound=None,
                    error=error,
                )
            )
            self.events.emit(
                "signal_turn_end",
                turn_id=turn_id,
                sender_name=sender.name,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )

    async def _send_or_queue(
        self,
        recipient: str,
        text: str,
        sender_name: str,
        *,
        turn_id: Optional[str] = None,
    ) -> None:
        if is_quiet_hours(self.cfg.speaking):
            self.quiet_queue.append(
                QueuedMessage(
                    recipient=recipient,
                    text=text,
                    queued_at=time.time(),
                )
            )
            log.info(
                "quiet hours: queued reply for %s (%d chars); queue size=%d",
                sender_name,
                len(text),
                self.quiet_queue.size(),
            )
            self.events.emit(
                "quiet_queue_enter",
                turn_id=turn_id,
                recipient=recipient,
                sender_name=sender_name,
                text_len=len(text),
                queue_size=self.quiet_queue.size(),
            )
            return
        await self.signal.send(recipient, text)
        log.info("replied to %s (%d chars)", sender_name, len(text))
        self.events.emit(
            "signal_send",
            turn_id=turn_id,
            recipient=recipient,
            sender_name=sender_name,
            text_len=len(text),
        )

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
            try:
                await self.signal.send(msg.recipient, msg.text)
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to send queued message to %s; re-queueing", msg.recipient
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
        try:
            # Surface turns don't have a single inbound recipient; the
            # ``outbound_recipient`` is informational only.
            await self._run_turn(prompt, turn_id=turn_id, outbound_recipient=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("surface turn failed for %s", path.name)
            error = f"{type(exc).__name__}: {exc}"
        finally:
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

        recipient = next(iter(self.cfg.allowed_senders), None)
        if recipient is None:
            log.error("emergency %s: no allowed_senders configured", path.name)
            self.events.emit(
                "emergency_no_recipient",
                turn_id=turn_id,
                emergency_id=path.name,
            )
            self._archive_emergency(path, verdict="no-recipient", action="daemon-archived")
            return

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
        # sends directly even during quiet hours.
        was_emergency = getattr(self, "_emergency_bypass", False)
        self._emergency_bypass = True
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

    async def _send_message(self, recipient: str, text: str) -> None:
        """Send Signal text and update the daemon's did-send tracker.

        This is the closure passed to the send_message MCP tool. The tool
        handles name-to-number resolution; by the time we land here the
        recipient is already an E.164 number. During emergency turns the
        daemon flips ``_emergency_bypass`` True so quiet-hours queuing is
        skipped — everything else flows through ``_send_or_queue``.
        """
        if getattr(self, "_emergency_bypass", False):
            await self.signal.send(recipient, text)
            log.info("emergency send to %s (%d chars)", recipient, len(text))
            self.events.emit(
                "signal_send",
                recipient=recipient,
                sender_name=self._sender_name_for(recipient),
                text_len=len(text),
                emergency=True,
            )
        else:
            await self._send_or_queue(
                recipient,
                text,
                self._sender_name_for(recipient),
            )
        self._turn_did_send = True

    def _sender_name_for(self, recipient: str) -> str:
        sender = self.cfg.allowed_senders.get(recipient)
        return sender.name if sender else recipient

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

        final_prompt = self._compose_prompt(prompt)
        spec = self._build_spec()
        handlers = self._build_handlers(silent=silent)

        kernel = AgentKernel(
            self.events,
            correlation_id=turn_id,
            silent=silent,
            short_cap=2000,
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
