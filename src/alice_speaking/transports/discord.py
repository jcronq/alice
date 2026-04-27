"""DiscordTransport: Discord under the Transport interface.

Phase 3b scope is intentionally narrow: **DMs only**. Guild channels are a
future expansion (capability advertisement is the same; routing is the
only difference). The bot must:

- be granted the **Message Content Intent** in the Discord developer
  portal (it is privileged — disabled by default).
- be DM-able by the principal (defaults to "friends only" on a personal
  account; if the bot is in a shared guild with the principal, DMs
  open up automatically).

ACL: same address-book rule as every other transport. The daemon's
``_discord_producer`` filters via
``address_book.is_allowed("discord", str(user.id))``. Inbound from
unknown discord users is dropped silently — same as Signal.

Outbound rendering uses :data:`DISCORD_CAPS` (markdown="limited" — Discord
supports headers, bold, italics, code blocks, but not tables; max 1900
bytes/message under the 2000 hard cap so we have headroom). The render
pipeline strips/splits per those caps before delivery.

Lifecycle: ``start()`` kicks off ``client.start(token)`` as a background
task; ``stop()`` calls ``client.close()``. The transport blocks
``messages()`` consumers until inbound arrives — same shape as
:class:`CLITransport`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator, Optional

import discord

from .base import (
    DISCORD_CAPS,
    Capabilities,
    ChannelRef,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


def _default_intents() -> discord.Intents:
    """Minimum intents to receive DM text content.

    ``message_content`` is privileged (must be toggled on in the Discord
    developer portal). ``dm_messages`` is non-privileged and on by default
    in ``Intents.default()``, but we set it explicitly so the requirement
    is visible at the call site.
    """
    intents = discord.Intents.default()
    intents.dm_messages = True
    intents.message_content = True
    return intents


class DiscordTransport:
    """Transport adapter for Discord (DMs only in Phase 3b)."""

    name = "discord"
    caps: Capabilities = DISCORD_CAPS

    def __init__(
        self,
        *,
        token: str,
        intents: Optional[discord.Intents] = None,
        inbox_size: int = 64,
    ) -> None:
        if not token:
            raise ValueError("DiscordTransport requires a non-empty token")
        self._token = token
        self._intents = intents if intents is not None else _default_intents()
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=inbox_size)
        self._client: Optional[discord.Client] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        # Cache of user-id (str) → discord.User so outbound DMs don't need
        # a fresh fetch_user round-trip every time. Populated on inbound
        # and on first outbound.
        self._user_cache: dict[str, discord.abc.User] = {}

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        """Connect the Discord client. Returns once the bot has finished
        the handshake (``on_ready``) so the daemon's ``daemon_ready``
        event can fire only after the transport is actually live.
        """
        client = discord.Client(intents=self._intents)

        @client.event
        async def on_ready() -> None:
            log.info(
                "discord ready: logged in as %s (id=%s)",
                client.user,
                getattr(client.user, "id", "?"),
            )
            self._ready.set()

        @client.event
        async def on_message(msg: discord.Message) -> None:
            await self._on_message(msg)

        self._client = client
        self._task = asyncio.create_task(
            client.start(self._token), name="discord-client"
        )
        # Wait for the handshake. If the start task dies before we see
        # ready (bad token, network), surface the error.
        ready_task = asyncio.create_task(self._ready.wait(), name="discord-ready-wait")
        done, _pending = await asyncio.wait(
            {ready_task, self._task}, return_when=asyncio.FIRST_COMPLETED
        )
        if self._task in done and not self._ready.is_set():
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task
            # Re-raise whatever the client raised
            self._task.result()
        # Otherwise discard the ready task — it already fired.

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    # ------------------------------------------------------------------
    # Inbound

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            yield await self._inbox.get()

    async def _on_message(self, msg: discord.Message) -> None:
        client = self._client
        if client is None or msg.author.id == getattr(client.user, "id", None):
            return
        # Phase 3b: DMs only. Guild messages are dropped silently. When
        # we add guild support the discriminator will be the channel kind,
        # not the message origin.
        if not isinstance(msg.channel, discord.DMChannel):
            return
        if not msg.content.strip() and not msg.attachments:
            return
        author = msg.author
        # Discord usernames have changed: ``global_name`` is the new
        # display name (introduced 2023). Fall back to legacy ``name`` for
        # bots / older clients that haven't migrated.
        display_name = (
            getattr(author, "global_name", None) or author.name
        )
        principal = Principal(
            transport="discord",
            native_id=str(author.id),
            display_name=display_name,
        )
        # ``address`` for outbound DMs is the user-id (we can ``fetch_user``
        # to recover the User object).
        origin = ChannelRef(
            transport="discord", address=str(author.id), durable=True
        )
        inbound = InboundMessage(
            principal=principal,
            origin=origin,
            text=msg.content,
            timestamp=msg.created_at.timestamp(),
            metadata={"discord_message_id": str(msg.id)},
        )
        self._user_cache[str(author.id)] = author
        try:
            self._inbox.put_nowait(inbound)
        except asyncio.QueueFull:
            log.warning(
                "discord inbox full; dropping message from %s",
                principal.display_name,
            )

    # ------------------------------------------------------------------
    # Outbound

    async def send(self, out: OutboundMessage) -> None:
        """Render Alice's text per :data:`DISCORD_CAPS` and DM each chunk.

        Multi-chunk messages get an ``(i/N)`` prefix to mirror the Signal
        convention. Attachments are accepted but logged-and-dropped for
        now — Phase 4 cleanup territory.
        """
        from ..render import render

        if self._client is None:
            raise RuntimeError("DiscordTransport.send before start()")

        chunks = render(out.text, self.caps)
        if not chunks:
            log.debug("discord send: render produced no chunks; nothing to do")
            return

        if out.attachments:
            log.warning(
                "discord send: ignoring %d attachment(s); not yet implemented",
                len(out.attachments),
            )

        user = await self._resolve_user(out.destination.address)
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            payload = f"({i}/{total}) {chunk}" if total > 1 else chunk
            await user.send(payload)

    async def _resolve_user(self, native_id: str) -> discord.abc.User:
        cached = self._user_cache.get(native_id)
        if cached is not None:
            return cached
        assert self._client is not None
        try:
            user = await self._client.fetch_user(int(native_id))
        except (ValueError, discord.HTTPException) as exc:
            raise RuntimeError(
                f"discord: cannot resolve user id {native_id!r}: {exc}"
            ) from exc
        self._user_cache[native_id] = user
        return user

    async def typing(self, channel: ChannelRef, on: bool) -> None:
        """Best-effort typing indicator.

        discord.py exposes typing as a context manager
        (``async with channel.typing()``) which doesn't fit our on/off
        model — discord auto-clears the indicator after ~10s anyway, so
        the heartbeat pattern Signal uses isn't useful here. We trigger
        a single ``trigger_typing`` on ``on=True`` and no-op on ``off``.
        """
        if not on or self._client is None:
            return
        try:
            user = await self._resolve_user(channel.address)
            dm = user.dm_channel or await user.create_dm()
            await dm.trigger_typing()
        except Exception as exc:  # noqa: BLE001
            log.debug("discord typing indicator failed: %s", exc)
