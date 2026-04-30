"""DiscordTransport: Discord under the Transport interface.

Supports both **DMs** and **guild text channels** (Phase 4c). The bot
must:

- be granted the **Message Content Intent** in the Discord developer
  portal (privileged — disabled by default).
- be DM-able by the principal (defaults to "friends only" on a personal
  account; if the bot is in a shared guild with the principal, DMs
  open up automatically).
- be a member of any guild whose text channels Alice should reach.

Address scheme
==============

Discord channel addresses use a two-letter prefix so the transport can
distinguish "send a DM to user X" from "post in channel Y":

- ``user:<discord-user-id>`` — opens (or reuses) a DM with that user.
- ``channel:<discord-channel-id>`` — posts in that text channel.

For back-compat with Phase 3b principals.yaml entries, a bare numeric
id (no prefix) is treated as ``user:<id>``.

ACL
===

For DMs: the user's principal must exist in the address book and be
``allowed=True`` — same shape as Signal/CLI.

For guild messages: the user's principal must exist AND the principal
must list the originating channel id (``channel:<channel-id>``) in
their channels. This lets the address book gate at channel granularity
(the owner can talk in ``#alice-room`` but not ``#general``) without a
separate top-level channel allowlist.

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
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import discord

from .base import (
    DISCORD_CAPS,
    Capabilities,
    ChannelRef,
    DaemonContext,
    InboundMessage,
    OutboundMessage,
    Principal,
)


log = logging.getLogger(__name__)


@dataclass
class DiscordEvent:
    """An inbound Discord message wrapped for the dispatcher.

    Same shape as :class:`CLIEvent` — the inbound :class:`InboundMessage`
    carries everything the handler needs. Discord channels are durable
    (DM history persists), unlike CLI's ephemeral sockets. Re-exported
    from ``alice_speaking.daemon`` for back-compat.
    """

    message: InboundMessage


def _default_intents() -> discord.Intents:
    """Minimum intents to receive DM and guild text content.

    ``message_content`` is privileged (must be toggled on in the Discord
    developer portal). ``dm_messages`` and ``guild_messages`` are
    non-privileged and on by default in ``Intents.default()``, but we
    set them explicitly so the requirements are visible at the call
    site.
    """
    intents = discord.Intents.default()
    intents.dm_messages = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


def _parse_address(address: str) -> tuple[str, int]:
    """Split a Discord channel address into ``(kind, id)``.

    ``kind`` is ``"user"`` or ``"channel"``. Bare numeric ids are
    treated as ``"user"`` for back-compat with Phase 3b address-book
    entries (which used the discord user id directly, no prefix).
    """
    if ":" in address:
        kind, _, raw = address.partition(":")
        kind = kind.strip().lower()
    else:
        kind, raw = "user", address
    if kind not in {"user", "channel"}:
        raise ValueError(
            f"discord address kind must be 'user' or 'channel', got {kind!r}"
        )
    try:
        return kind, int(raw)
    except ValueError as exc:
        raise ValueError(
            f"discord address id must be numeric, got {raw!r}"
        ) from exc


class DiscordTransport:
    """Transport adapter for Discord (DMs only in Phase 3b)."""

    name = "discord"
    caps: Capabilities = DISCORD_CAPS
    event_type = DiscordEvent

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
        if not msg.content.strip() and not msg.attachments:
            return
        author = msg.author
        # Discord usernames have changed: ``global_name`` is the new
        # display name (introduced 2023). Fall back to legacy ``name`` for
        # bots / older clients that haven't migrated.
        display_name = (
            getattr(author, "global_name", None) or author.name
        )
        # Principal native_id always uses the user prefix — it's the
        # *who*, not the *where*. Origin uses user: for DMs, channel:
        # for guild messages — that's how the daemon's _send_message
        # decides whether `recipient='self'` opens a DM or posts back
        # in the same channel.
        principal_address = f"user:{author.id}"
        is_dm = isinstance(msg.channel, discord.DMChannel)
        if is_dm:
            origin_address = principal_address
        else:
            origin_address = f"channel:{msg.channel.id}"
        principal = Principal(
            transport="discord",
            native_id=principal_address,
            display_name=display_name,
        )
        origin = ChannelRef(
            transport="discord", address=origin_address, durable=True
        )
        inbound = InboundMessage(
            principal=principal,
            origin=origin,
            text=msg.content,
            timestamp=msg.created_at.timestamp(),
            metadata={
                "discord_message_id": str(msg.id),
                "discord_channel_kind": "dm" if is_dm else "guild",
                "discord_channel_id": str(msg.channel.id),
                "discord_guild_id": (
                    str(msg.guild.id) if msg.guild is not None else None
                ),
            },
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

    async def send(self, out: OutboundMessage) -> int:
        """Render Alice's text per :data:`DISCORD_CAPS` and post each
        chunk. Routes by address prefix: ``user:<id>`` opens/uses a DM
        with that user, ``channel:<id>`` posts in that text channel.

        Multi-chunk messages get an ``(i/N)`` prefix to mirror the
        Signal convention. Attachments are accepted but logged-and-
        dropped for now. Returns the chunk count delivered.
        """
        from ..domain.render import render

        if self._client is None:
            raise RuntimeError("DiscordTransport.send before start()")

        chunks = render(out.text, self.caps)
        if not chunks:
            log.debug("discord send: render produced no chunks; nothing to do")
            return 0

        if out.attachments:
            log.warning(
                "discord send: ignoring %d attachment(s); not yet implemented",
                len(out.attachments),
            )

        target = await self._resolve_destination(out.destination.address)
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            payload = f"({i}/{total}) {chunk}" if total > 1 else chunk
            await target.send(payload)
        return total

    async def _resolve_destination(
        self, address: str
    ) -> discord.abc.Messageable:
        """Map an address (``user:<id>`` or ``channel:<id>``) to the
        discord.py object whose ``.send(text)`` delivers there."""
        kind, snowflake = _parse_address(address)
        if kind == "user":
            return await self._resolve_user(str(snowflake))
        return await self._resolve_channel(snowflake)

    async def _resolve_user(self, user_id: str) -> discord.abc.User:
        cached = self._user_cache.get(user_id)
        if cached is not None:
            return cached
        assert self._client is not None
        try:
            user = await self._client.fetch_user(int(user_id))
        except (ValueError, discord.HTTPException) as exc:
            raise RuntimeError(
                f"discord: cannot resolve user id {user_id!r}: {exc}"
            ) from exc
        self._user_cache[user_id] = user
        return user

    async def _resolve_channel(self, channel_id: int) -> discord.abc.Messageable:
        assert self._client is not None
        ch = self._client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                raise RuntimeError(
                    f"discord: cannot resolve channel id {channel_id}: {exc}"
                ) from exc
        # Duck-type rather than isinstance(Messageable) — Messageable is
        # a Protocol that some channel subclasses register against in
        # ways that fail isinstance at runtime; ``.send`` callable is the
        # actual contract we need.
        if not callable(getattr(ch, "send", None)):
            raise RuntimeError(
                f"discord: channel {channel_id} is not messageable "
                f"(got {type(ch).__name__})"
            )
        return ch

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
            target = await self._resolve_destination(channel.address)
            # For DMs, ``target`` is the discord.User; we want the underlying
            # DMChannel for trigger_typing.
            if isinstance(target, discord.User):
                target = target.dm_channel or await target.create_dm()
            await target.trigger_typing()
        except Exception as exc:  # noqa: BLE001
            log.debug("discord typing indicator failed: %s", exc)

    # ------------------------------------------------------------------
    # Prompt assembly (Phase 6c of plan 01)

    def build_prompt(
        self,
        *,
        principal_name: str,
        stamp: str,
        text: str,
    ) -> str:
        """Compose the prompt for a single Discord DM.

        Body lives in
        ``alice_prompts/templates/speaking/turn.discord.md.j2``
        (Plan 04 Phase 5).
        """
        from alice_prompts import load as load_prompt
        from ..domain.render import capability_prompt_fragment

        return load_prompt(
            "speaking.turn.discord",
            principal_name=principal_name,
            stamp=stamp,
            text=text,
            capability=capability_prompt_fragment("discord", self.caps),
        )

    # ------------------------------------------------------------------
    # Dispatcher integration (Phase 2 of plan 01)

    def producer(self, ctx: DaemonContext) -> Optional[asyncio.Task]:
        """Pump :class:`InboundMessage` objects through the address-book
        ACL and onto ``ctx._queue`` as :class:`DiscordEvent` events.

        Two-level ACL (preserved verbatim from
        ``SpeakingDaemon._discord_producer``):

        1. The user (``msg.principal.native_id`` = ``user:<id>``) must
           be a known, allowed principal.
        2. For guild messages, the originating channel
           (``msg.origin.address`` = ``channel:<id>``) must be listed
           in that principal's channels.
        DMs satisfy (2) trivially since their origin matches the
        principal's own ``user:`` entry.
        """
        return asyncio.create_task(self._produce(ctx), name="discord-produce")

    async def _produce(self, ctx: DaemonContext) -> None:
        async for msg in self.messages():
            principal = ctx.address_book.lookup_by_native(
                "discord", msg.principal.native_id
            )
            if principal is None or not principal.allowed:
                log.info(
                    "ignoring discord message from unknown user %s",
                    msg.principal.native_id,
                )
                continue
            channel_kind = msg.metadata.get("discord_channel_kind")
            if channel_kind == "guild":
                channel_addr = msg.origin.address
                if not any(
                    ch.transport == "discord" and ch.address == channel_addr
                    for ch in principal.channels
                ):
                    log.info(
                        "ignoring guild discord message from %s in %s "
                        "(channel not in principal's address-book entries)",
                        msg.principal.native_id,
                        channel_addr,
                    )
                    continue
            # Refresh display name in the address book if the inbound
            # carried a richer one (Discord users can change global_name).
            ctx.address_book.learn(msg)
            await ctx._queue.put(DiscordEvent(message=msg))

    async def handle(self, ctx: DaemonContext, event: DiscordEvent) -> None:
        """Run one turn for one Discord event. Phase 2 — declared for
        protocol conformance; the daemon's consumer still calls
        :func:`_dispatch.handle_discord` directly until Phase 3."""
        from .._dispatch import handle_discord

        await handle_discord(ctx, event)
