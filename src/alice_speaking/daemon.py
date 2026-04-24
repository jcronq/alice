"""Speaking Alice's outer loop.

Two producers feed one serial consumer:
- signal_client.receive(): user envelopes from Signal
- surface_watcher: files that thinking Alice drops into inner/surface/

The consumer processes one event at a time — Alice is a single mind juggling
messages and surfaced thoughts, not a parallel worker pool.

One Agent SDK session per process lifetime: fresh on start, resumed across
turns within the same run.
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
from dataclasses import dataclass
from typing import Optional, Union

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import config as config_module
from . import tools as tools_module
from .config import AllowedSender, Config
from .dedup import DedupStore
from .quiet_hours import QueuedMessage, QuietQueue, is_quiet_hours
from .signal_client import SignalClient, SignalEnvelope
from .turn_log import TurnLog, new_turn


log = logging.getLogger("alice_speaking")


SURFACE_POLL_SECONDS = 5.0
QUIET_CHECK_SECONDS = 30.0


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
        self.mcp_servers, self.custom_tool_names = tools_module.build(cfg)
        self.quiet_queue = QuietQueue(cfg.mind_dir / "inner" / "state" / "quiet-queue.jsonl")
        self.session_id: Optional[str] = None
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=64)
        self._dispatched_surfaces: set[str] = set()
        self._stop = asyncio.Event()
        self._surface_dir = cfg.mind_dir / "inner" / "surface"
        self._surface_handled_dir = self._surface_dir / ".handled"
        self._emergency_dir = cfg.mind_dir / "inner" / "emergency"
        self._emergency_handled_dir = self._emergency_dir / ".handled"
        self._dispatched_emergencies: set[str] = set()

    async def run(self) -> None:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.cfg.oauth_token

        loop = asyncio.get_event_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)

        try:
            log.info("waiting for signal-cli at %s", self.cfg.signal_api)
            await self.signal.wait_ready()
            log.info("daemon ready; listening")

            # If quiet hours ended while we were down, drain the queue first.
            if not is_quiet_hours(self.cfg.speaking) and self.quiet_queue.size() > 0:
                await self._drain_quiet_queue(reason="startup")

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
        # Ensure directories exist so polling doesn't raise.
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

    # ------------------------------------------------------------------
    # Signal turn (unchanged from phase 2)

    async def _handle_signal(self, event: SignalEvent) -> None:
        env = event.envelope
        sender = event.sender
        quiet = is_quiet_hours(self.cfg.speaking)

        # Typing indicator is visible outbound → suppressed during quiet hours.
        if not quiet:
            await self.signal.start_typing(env.source)
        reply: Optional[str] = None
        error: Optional[str] = None
        try:
            now = datetime.datetime.now().astimezone()
            stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            prompt = f"[Signal from {sender.name} | {stamp}]\n\n{env.body}"
            reply = await self._run_turn(prompt)
            if reply:
                await self._send_or_queue(env.source, reply, sender.name)
            else:
                log.warning("empty reply for %s", sender.name)
                error = "empty_reply"
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed for %s", sender.name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await self._send_or_queue(
                    env.source,
                    f"Hit an error ({type(exc).__name__}). Session preserved — reply to retry.",
                    sender.name,
                )
        finally:
            if not quiet:
                await self.signal.stop_typing(env.source)
            self.turns.append(
                new_turn(
                    sender_number=env.source,
                    sender_name=sender.name,
                    inbound=env.body,
                    outbound=reply,
                    error=error,
                )
            )

    async def _send_or_queue(self, recipient: str, text: str, sender_name: str) -> None:
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
            return
        await self.signal.send(recipient, text)
        log.info("replied to %s (%d chars)", sender_name, len(text))

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
        prompt = (
            f"[Internal — a thought just surfaced from reflection: {path.name}]\n\n"
            f"{body}\n\n"
            "This is your own thought that just came to you. Decide what to do: "
            "voice it to the user, file it into memory, reply to thinking via "
            "a note (append_note), or let it pass. When you've decided, call "
            "mcp__alice__resolve_surface with the file's `id` (its filename), "
            "a short `verdict`, and `action_taken`. If you voice it, do that "
            "before calling resolve_surface."
        )
        try:
            await self._run_turn(prompt)
        except Exception:
            log.exception("surface turn failed for %s", path.name)
        finally:
            # If Alice didn't resolve (didn't call resolve_surface), archive it
            # ourselves so it doesn't sit in the surface queue forever.
            if path.is_file():
                try:
                    self._archive_unresolved(path)
                except OSError as exc:
                    log.warning("unresolved-archive failed for %s: %s", path.name, exc)
            self._dispatched_surfaces.discard(path.name)

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
    # External monitors drop files into inner/emergency/. Unlike surfaces,
    # emergency-derived outbound BYPASSES quiet hours (that's the whole point).
    # The speaking turn composes the message; the handler sends it directly
    # via the signal client rather than going through _send_or_queue.

    async def _handle_emergency(self, event: EmergencyEvent) -> None:
        path = event.path
        if not path.is_file():
            self._dispatched_emergencies.discard(path.name)
            return
        body = path.read_text()

        # Pick a primary recipient. Emergency monitors should set an explicit
        # target via frontmatter eventually; until then, first allowed sender
        # (typically Owner) gets it.
        recipient = next(iter(self.cfg.allowed_senders), None)
        if recipient is None:
            log.error("emergency %s: no allowed_senders configured", path.name)
            self._archive_emergency(path, verdict="no-recipient", action="daemon-archived")
            return

        prompt = (
            f"[EMERGENCY — signal from an external monitor: {path.name}]\n\n"
            f"{body}\n\n"
            "Review this emergency. Verify the frontmatter contains "
            "`evidence_paths` with at least one verifiable source. If the "
            "evidence is insufficient, reply empty — the daemon will archive "
            "without voicing.\n\n"
            "If the emergency is real, your reply text will be sent "
            "IMMEDIATELY to the user over Signal, bypassing quiet hours. Be "
            "concise and direct — name the emergency, the evidence, and the "
            "recommended action in one short message. This is the ONE case "
            "where you initiate voice contact without prior conversation."
        )

        voiced_text: Optional[str] = None
        verdict = "unknown"
        action = "none"
        try:
            reply = await self._run_turn(prompt)
            if reply:
                await self.signal.send(recipient, reply)
                voiced_text = reply
                verdict = "voiced"
                action = f"sent to {recipient} (bypassed quiet hours)"
                log.warning(
                    "emergency voiced to %s (%d chars): %s",
                    recipient,
                    len(reply),
                    path.name,
                )
            else:
                verdict = "downgraded"
                action = "alice returned empty reply — no evidence or false positive"
                log.info("emergency downgraded: %s", path.name)
        except Exception as exc:  # noqa: BLE001
            log.exception("emergency turn failed for %s", path.name)
            verdict = "error"
            action = f"{type(exc).__name__}: {exc}"
        finally:
            if path.is_file():
                self._archive_emergency(path, verdict=verdict, action=action, voiced=voiced_text)
            self._dispatched_emergencies.discard(path.name)

    def _archive_emergency(
        self,
        path: pathlib.Path,
        *,
        verdict: str,
        action: str,
        voiced: Optional[str] = None,
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
        if voiced:
            trailer += f"voiced_text: {voiced[:500]}\n"
        dest.write_text(body + trailer)
        path.unlink()
        log.info("emergency archived: %s (%s)", path.name, verdict)

    # ------------------------------------------------------------------
    # Agent SDK invocation (shared by signal + surface turns)

    async def _run_turn(self, prompt: str) -> str:
        options = self._build_options()
        parts: list[str] = []
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                if msg.error == "rate_limit":
                    raise RuntimeError("claude rate_limit")
                if msg.error:
                    raise RuntimeError(f"claude error: {msg.error}")
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.session_id:
                    self.session_id = msg.session_id
                if msg.is_error:
                    detail = msg.result or "unknown"
                    raise RuntimeError(f"claude result error: {detail}")
        return "".join(parts).strip()

    def _build_options(self) -> ClaudeAgentOptions:
        builtin_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
        kwargs: dict = {
            "model": self.cfg.speaking.get("model"),
            "allowed_tools": builtin_tools + self.custom_tool_names,
            "mcp_servers": self.mcp_servers,
            "cwd": str(self.cfg.work_dir),
        }
        if self.session_id:
            kwargs["resume"] = self.session_id
        return ClaudeAgentOptions(**kwargs)


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
