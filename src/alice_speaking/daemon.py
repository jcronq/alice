"""Speaking Alice's outer loop.

Reads envelopes from signal-cli, drives each through the Claude Agent SDK, and
sends the reply back through signal-cli. Serial turn processing — Alice is one
mind juggling both senders, not a per-sender worker pool.

One Agent SDK session per process lifetime: fresh on start, resumed across
turns within the same run. Session ID does not persist across process restarts
(by design — continuity is maintained by turn-log replay when we add that,
not by resuming potentially-stale Claude sessions).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import signal as _signal
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from . import config as config_module
from .config import AllowedSender, Config
from .dedup import DedupStore
from .signal_client import SignalClient, SignalEnvelope
from .turn_log import TurnLog, new_turn


log = logging.getLogger("alice_speaking")


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
        self.session_id: Optional[str] = None
        self._stop = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        # Claude Agent SDK subprocess inherits this env var → OAuth → Max subscription.
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.cfg.oauth_token

        loop = asyncio.get_event_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)

        try:
            log.info("waiting for signal-cli at %s", self.cfg.signal_api)
            await self.signal.wait_ready()
            log.info("daemon ready; listening")

            receive_task = asyncio.create_task(self._receive_loop(), name="receive")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop")
            done, _ = await asyncio.wait(
                {receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                log.info("stop requested; draining")
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            else:
                log.warning("receive loop exited before stop requested")
                stop_task.cancel()

            if self._in_flight:
                log.info("draining %d in-flight turns", len(self._in_flight))
                await asyncio.gather(*self._in_flight, return_exceptions=True)
        finally:
            await self.signal.aclose()
            log.info("shutdown complete")

    async def _receive_loop(self) -> None:
        async for env in self.signal.receive():
            if self._stop.is_set():
                return
            if env.source not in self.cfg.allowed_senders:
                log.info("ignoring envelope from %s", env.source)
                continue
            if self.dedup.seen(env.timestamp):
                log.debug("duplicate ts=%d; skipping", env.timestamp)
                continue
            self.dedup.mark(env.timestamp)

            sender = self.cfg.allowed_senders[env.source]
            await self._drive_turn(env, sender)

    async def _drive_turn(self, env: SignalEnvelope, sender: AllowedSender) -> None:
        task = asyncio.create_task(
            self._turn(env, sender), name=f"turn-{env.timestamp}"
        )
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        # Serialize: Alice is one mind. Owner's and Friend's messages are not
        # processed in parallel — she reads one, responds, then the next.
        await task

    async def _turn(self, env: SignalEnvelope, sender: AllowedSender) -> None:
        await self.signal.start_typing(env.source)
        reply: Optional[str] = None
        error: Optional[str] = None
        try:
            reply = await self._generate_reply(env, sender)
            if reply:
                await self.signal.send(env.source, reply)
                log.info("replied to %s (%d chars)", sender.name, len(reply))
            else:
                log.warning("empty reply for %s; nothing sent", sender.name)
                error = "empty_reply"
        except Exception as exc:  # noqa: BLE001 — user-facing error reporting
            log.exception("turn failed for %s", sender.name)
            error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await self.signal.send(
                    env.source,
                    f"Hit an error ({type(exc).__name__}). Session preserved — reply to retry.",
                )
        finally:
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

    async def _generate_reply(
        self, env: SignalEnvelope, sender: AllowedSender
    ) -> str:
        now = datetime.datetime.now().astimezone()
        stamp = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        prompt = f"[Signal from {sender.name} | {stamp}]\n\n{env.body}"

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
        kwargs: dict = {
            "model": self.cfg.speaking.get("model"),
            "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
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
