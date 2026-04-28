---
title: Communication channels
aliases: [channels, signal vs discord, comms]
tags: [reference]
created: 2026-04-24
---

# Communication channels

> **tl;dr** Signal is the primary live channel. Discord now has scaffolding (Phase 3b + 4c of the I/O abstraction rework, green 2026-04-27) — a receive path exists in the daemon, but it is not yet in active use. Access control for all transports is governed by `principals.yaml`.

## ACL — principals.yaml

`~/alice-mind/config/principals.yaml` is the single source of truth for access control across all transports (Signal, CLI, Discord). It maps contact names to transport-specific addresses and defines who is allowed to send to Alice. Daemon restart picks up `principals.yaml` changes — no rebuild required.

## Signal (primary)

Signal is the primary communication channel with [[owner]] and [[friend]]. The [[alice-speaking]] daemon listens on Signal and sends replies through it. See [[signal-cli]] for technical details.

**`recipient='self'` routing caveat:** `send_message(recipient='self')` routes the reply back to the same channel the current inbound message arrived on. This only works during an active inbound turn. For surface-triggered voicings (no active inbound turn from a specific channel), use a named recipient (e.g., `"owner"`) — `recipient='self'` will not route correctly.

## Discord (scaffolded — not yet active)

Discord scaffolding was added to alice-speaking as part of the I/O abstraction rework (Phase 3b + 4c, green 2026-04-27). **A receive path now exists in the daemon** — the producer is implemented and the inbound envelope carries `transport: "discord"`. However, Discord is dormant pending `DISCORD_BOT_TOKEN` — not yet in active use.

**Address scheme:** Discord recipients are addressed as `user:<discord_user_id>` for DMs and `channel:<guild_channel_id>` for guild posts. These are the canonical forms for the `recipient` field when Discord is live.

**Quiet hours:** Quiet hours (22:00–07:00 ET) apply to Discord surface-triggered sends, matching Signal behavior. Inbound replies and emergencies bypass quiet hours on Discord (same policy as Signal).

Rules (when it becomes actively monitored):
- Only respond to messages from [[owner]] on Discord.
- Ignore all messages from anyone else.

## Related

- [[signal-cli]]
- [[alice-speaking]]
- [[signal-formatting]] — channel-specific formatting rules for Signal
- [[communication-style]] — behavioral policy (tone, terseness) that applies across all channels
- [[owner]]
