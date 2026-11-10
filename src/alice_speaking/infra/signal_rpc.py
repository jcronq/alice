"""Thin async client for signal-cli in daemon mode.

Sends go through signal-cli's JSON-RPC over HTTP. Receives come from tailing
signal-cli's stdout log (the daemon's HTTP mode has no push endpoint, and the
log is a durable record the bash bridge has proven reliable against).

Typing indicators are a heartbeat task per recipient — Signal's indicator has
a ~15s TTL, so we refresh every 10s.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx


log = logging.getLogger(__name__)

# signal-cli's message length cap (approximate). Match the bash bridge.
_CHUNK_LIMIT = 4000
_TYPING_REFRESH_SECONDS = 10

# -- Note-to-Self loop guards (see SignalRPC._allow_self_note) ----------------
# Alice sends replies to the same account she receives Note-to-Self on, so a
# naive "accept our own sync'd messages" would let her answer herself forever.
# Every guard below is independent; any one of them drops the message.
_SELF_SENT_TS_MEMORY = 512         # timestamps of our own sends to remember
_SELF_SENT_TEXT_MEMORY = 64        # recent outbound self-texts to remember
_SELF_SENT_TEXT_WINDOW_S = 120.0   # how long an outbound text counts as "ours"
_SELF_RATE_LIMIT = 5               # max accepted self-notes ...
_SELF_RATE_WINDOW_S = 60.0         # ... per this many seconds (circuit breaker)

# Where signal-cli stores attachment files. Inside the worker container
# the host's signal-cli data dir is bind-mounted at /host-home/.local/share/...
# Override via SIGNAL_ATTACHMENTS_DIR for non-default deployments.
DEFAULT_ATTACHMENTS_DIR = pathlib.Path(
    os.environ.get(
        "SIGNAL_ATTACHMENTS_DIR",
        "/host-home/.local/share/signal-cli/attachments",
    )
)


@dataclass
class Attachment:
    """One inbound media attachment from a Signal message.

    ``path`` is a filesystem path Alice's tools can ``Read`` (image PDF,
    audio, etc. — Claude Code's Read tool handles common formats). The
    file is the raw blob signal-cli stored on disk; signal-cli generates
    the ``id`` filename and (sometimes) preserves the original
    ``filename`` and ``content_type`` from the sender.
    """

    id: str
    path: pathlib.Path
    content_type: str = "application/octet-stream"
    filename: Optional[str] = None
    size: Optional[int] = None


@dataclass
class SignalEnvelope:
    timestamp: int
    source: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    # True when this came from a Note-to-Self sync (sentMessage to our own
    # account) rather than a normal dataMessage from another account.
    from_self_sync: bool = False


class SignalRPC:
    def __init__(
        self,
        api: str,
        account: str,
        log_path: pathlib.Path,
        offset_path: pathlib.Path,
        note_to_self: bool = False,
    ) -> None:
        self.api = api.rstrip("/")
        self.account = account
        self.note_to_self = note_to_self
        # Loop-guard state (only used when note_to_self is on).
        self._sent_ts: collections.deque[int] = collections.deque(
            maxlen=_SELF_SENT_TS_MEMORY
        )
        self._sent_texts: collections.deque[tuple[float, str]] = collections.deque(
            maxlen=_SELF_SENT_TEXT_MEMORY
        )
        self._seen_self_ts: collections.deque[int] = collections.deque(
            maxlen=_SELF_SENT_TS_MEMORY
        )
        self._accepted_at: collections.deque[float] = collections.deque(
            maxlen=_SELF_RATE_LIMIT
        )
        self.log_path = log_path
        self.offset_path = offset_path
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3, read=10, write=10, pool=10)
        )
        self._typing: dict[str, asyncio.Task[None]] = {}

    async def aclose(self) -> None:
        for task in list(self._typing.values()):
            task.cancel()
        for task in list(self._typing.values()):
            with contextlib.suppress(BaseException):
                await task
        self._typing.clear()
        await self._http.aclose()

    async def wait_ready(self, timeout_seconds: float = 60.0) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        last_error: Optional[Exception] = None
        while loop.time() < deadline:
            try:
                await self._rpc("version", {})
                log.info("signal-cli daemon reachable at %s", self.api)
                return
            except httpx.HTTPError as exc:
                last_error = exc
            await asyncio.sleep(1)
        raise TimeoutError(
            f"signal-cli daemon not reachable at {self.api} after {timeout_seconds}s: {last_error}"
        )

    async def send(
        self,
        recipient: str,
        text: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        """Send a Signal message, optionally with one or more attachments.

        ``attachments`` is a list of filesystem paths visible **inside the
        signal-cli daemon's container**. Cross-container path resolution
        is the caller's responsibility — see ``tools/messaging.py`` for
        the spool-dir copy strategy that the MCP tool uses.

        Attachments only ride along on the FIRST chunk when the message
        is split — sending them on every chunk would multiply the upload
        and look like duplicate media to the recipient.
        """
        chunks = _chunk(text, _CHUNK_LIMIT)
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            payload = f"({i}/{total}) {chunk}" if total > 1 else chunk
            params: dict[str, Any] = {
                "account": self.account,
                "message": payload,
                "recipients": [recipient],
            }
            if attachments and i == 1:
                params["attachments"] = list(attachments)
            resp = await self._rpc(
                "send",
                params,
                # ``speaking-`` prefix lets the outbound-capture proxy
                # (signal_outbound_proxy) tell Speaking's own sends from
                # automations/scripts sharing the account.
                request_id=f"speaking-send-{i}",
            )
            if self.note_to_self and _same_account(recipient, self.account):
                self._remember_self_send(payload, resp)

    async def send_typing(self, recipient: str) -> None:
        try:
            await self._rpc(
                "sendTyping",
                {"account": self.account, "recipients": [recipient]},
                request_id="typing",
            )
        except httpx.HTTPError:
            # Typing is best-effort; don't fail the turn because Signal is sulking.
            pass

    async def send_reaction(
        self,
        recipient: str,
        target_author: str,
        target_timestamp: int,
        emoji: str,
    ) -> None:
        """Send an emoji reaction to a prior message.

        ``target_author`` + ``target_timestamp`` together identify the
        message being reacted to. For 1:1 inbound, the author is the
        sender themselves and the recipient (where the reaction is
        delivered) is the same number.
        """
        await self._rpc(
            "sendReaction",
            {
                "account": self.account,
                "emoji": emoji,
                "targetAuthor": target_author,
                "targetTimestamp": int(target_timestamp),
                "recipient": [recipient],
            },
            request_id="react",
        )

    async def start_typing(self, recipient: str) -> None:
        """Kick off a 10s typing heartbeat for the recipient."""
        await self.stop_typing(recipient)
        self._typing[recipient] = asyncio.create_task(
            self._typing_heartbeat(recipient), name=f"typing-{recipient}"
        )

    async def stop_typing(self, recipient: str) -> None:
        task = self._typing.pop(recipient, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def receive(self) -> AsyncIterator[SignalEnvelope]:
        """Yield envelopes from the signal-cli log forever. Durable across restarts
        via the offset file."""
        offset = self._load_offset()
        async for line in self._tail_from(offset):
            env = _parse_envelope(
                line, account=self.account if self.note_to_self else None
            )
            if env is None:
                continue
            if env.from_self_sync and not self._allow_self_note(env):
                continue
            yield env

    # -- Note-to-Self loop guards ----------------------------------------------

    def _remember_self_send(self, payload: str, resp: Any) -> None:
        """Record an outbound message addressed to our own account so its
        echo (if signal-cli ever syncs it back) is recognised and dropped."""
        self._sent_texts.append((time.monotonic(), payload))
        result = resp.get("result") if isinstance(resp, dict) else None
        ts = result.get("timestamp") if isinstance(result, dict) else None
        if isinstance(ts, int):
            self._sent_ts.append(ts)

    def _allow_self_note(self, env: SignalEnvelope, *, now: Optional[float] = None) -> bool:
        """Decide whether a Note-to-Self sync is a real message from the owner.

        Drops (never accepts) when ANY of these hold, in order:
          1. its timestamp is one we got back from our own ``send`` RPC;
          2. its text equals something we sent to ourselves within the
             last ``_SELF_SENT_TEXT_WINDOW_S`` seconds (covers the race
             where the echo is tailed before ``send`` returns);
          3. we already accepted this timestamp (replayed log / duplicate);
          4. more than ``_SELF_RATE_LIMIT`` self-notes were accepted in the
             last ``_SELF_RATE_WINDOW_S`` seconds (circuit breaker: if a
             loop slips past 1-3, it is capped instead of runaway).
        """
        now = time.monotonic() if now is None else now
        if env.timestamp in self._sent_ts:
            log.info("note-to-self: dropping echo of our own send (ts=%s)", env.timestamp)
            return False
        for sent_at, text in self._sent_texts:
            if now - sent_at <= _SELF_SENT_TEXT_WINDOW_S and text == env.body:
                log.info("note-to-self: dropping echo of our own text (ts=%s)", env.timestamp)
                return False
        if env.timestamp in self._seen_self_ts:
            log.info("note-to-self: dropping duplicate (ts=%s)", env.timestamp)
            return False
        while self._accepted_at and now - self._accepted_at[0] > _SELF_RATE_WINDOW_S:
            self._accepted_at.popleft()
        if len(self._accepted_at) >= _SELF_RATE_LIMIT:
            log.warning(
                "note-to-self: rate limit hit (%d in %.0fs); dropping ts=%s — "
                "possible reply loop",
                _SELF_RATE_LIMIT, _SELF_RATE_WINDOW_S, env.timestamp,
            )
            return False
        self._accepted_at.append(now)
        self._seen_self_ts.append(env.timestamp)
        return True

    # -- internals -------------------------------------------------------------

    async def _rpc(self, method: str, params: dict, *, request_id: str = "rpc") -> dict:
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
            "params": params,
        }
        r = await self._http.post(f"{self.api}/api/v1/rpc", json=body)
        r.raise_for_status()
        return r.json()

    async def _typing_heartbeat(self, recipient: str) -> None:
        try:
            while True:
                await self.send_typing(recipient)
                await asyncio.sleep(_TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise

    def _load_offset(self) -> int:
        try:
            return int(self.offset_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.offset_path.with_suffix(".tmp")
        tmp.write_text(str(offset))
        tmp.replace(self.offset_path)

    async def _tail_from(self, start_offset: int) -> AsyncIterator[str]:
        current = start_offset
        # If the log isn't there yet, wait patiently — daemon may still be warming up.
        while not self.log_path.exists():
            await asyncio.sleep(0.5)

        # Reset if saved offset overshoots (log was truncated since last run).
        if current > self.log_path.stat().st_size:
            log.warning("offset %d > log size; resetting to 0", current)
            current = 0
            self._save_offset(0)

        while True:
            try:
                size = self.log_path.stat().st_size
            except FileNotFoundError:
                await asyncio.sleep(0.5)
                continue

            if size < current:
                log.warning("log shrank (rotation?); resetting offset")
                current = 0

            if size == current:
                await asyncio.sleep(0.2)
                continue

            with self.log_path.open("rb") as f:
                f.seek(current)
                buf = f.read(size - current)

            current = size
            self._save_offset(current)

            for line in buf.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    yield line


# -- module-level helpers (testable in isolation) ------------------------------


def _chunk(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        head = remaining[:limit]
        cut = len(head)
        # Prefer splitting on a paragraph break so chunks read naturally.
        para_idx = head.rfind("\n\n")
        if para_idx > 0:
            cut = para_idx
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    return parts


def _same_account(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip() == b.strip()


def _parse_envelope(line: str, *, account: Optional[str] = None) -> Optional[SignalEnvelope]:
    """Parse one signal-cli log line into an envelope.

    Returns ``None`` for non-message lines (typing receipts, sync messages
    with no dataMessage, etc.) or malformed JSON. A message qualifies if
    it has a source, a timestamp, and EITHER text body OR at least one
    attachment — image-only messages are valid inbound; we don't drop
    them just because the body is empty.

    When ``account`` is given (Note-to-Self enabled) one extra shape is
    accepted: a ``syncMessage.sentMessage`` that the owner sent from another
    device of OUR account to OUR account. Everything else in
    ``syncMessage`` (sent to someone else, group sends, read receipts,
    contacts) is still ignored. The result is flagged ``from_self_sync`` so
    the caller applies the loop guards.
    """
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    env = data.get("envelope") or {}
    data_msg = env.get("dataMessage") or {}
    source = env.get("source") or env.get("sourceNumber")
    ts = env.get("timestamp")
    from_self_sync = False
    if not data_msg and account:
        sent = (env.get("syncMessage") or {}).get("sentMessage")
        if sent and _is_note_to_self(env, sent, account):
            data_msg = sent
            ts = sent.get("timestamp", ts)
            from_self_sync = True
    body = data_msg.get("message") or ""
    attachments = _parse_attachments(data_msg.get("attachments") or [])
    if not source or ts is None:
        return None
    if not body and not attachments:
        # Nothing to act on — ignore the envelope.
        return None
    return SignalEnvelope(
        timestamp=int(ts),
        source=str(source),
        body=str(body),
        attachments=attachments,
        from_self_sync=from_self_sync,
    )


def _is_note_to_self(env: dict, sent: dict, account: str) -> bool:
    """True only for a 1:1 message from our own account to our own account.

    Strict on purpose: the envelope source AND the sentMessage destination
    must both be our account, it must not be a group message, and when both
    UUIDs are present they must agree.
    """
    source = env.get("sourceNumber") or env.get("source")
    dest = sent.get("destinationNumber") or sent.get("destination")
    if not (_same_account(str(source or ""), account) and _same_account(str(dest or ""), account)):
        return False
    if sent.get("groupInfo") or sent.get("groupV2"):
        return False
    src_uuid, dest_uuid = env.get("sourceUuid"), sent.get("destinationUuid")
    if src_uuid and dest_uuid and src_uuid != dest_uuid:
        return False
    return True


def _parse_attachments(raw: list) -> list[Attachment]:
    """Convert signal-cli's dataMessage.attachments list into Attachment objects.

    signal-cli writes each attachment to ``<attachments_dir>/<id>``. The
    JSON entry carries the id, content type, and (sometimes) the original
    filename. Missing fields fall back to safe defaults.
    """
    out: list[Attachment] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        attachment_id = entry.get("id")
        if not attachment_id:
            continue
        out.append(
            Attachment(
                id=str(attachment_id),
                path=DEFAULT_ATTACHMENTS_DIR / str(attachment_id),
                content_type=str(
                    entry.get("contentType") or "application/octet-stream"
                ),
                filename=entry.get("filename") or None,
                size=entry.get("size") if isinstance(entry.get("size"), int) else None,
            )
        )
    return out


# Back-compat: the class was named ``SignalClient`` until plan 02
# renamed it to ``SignalRPC`` to disambiguate from
# ``transports/signal.py`` (the inbound transport that USES this
# JSON-RPC adapter). External callers using the old class name
# still work.
SignalClient = SignalRPC
