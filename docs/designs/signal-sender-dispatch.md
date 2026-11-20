# Signal sender-based dispatch

**Status:** shipped — `feat/signal-sender-dispatch`.
**Related:** `cortex-memory/research/2026-04-29-a2a-transport-design`.

## Problem

Jason's shared Alice Signal account (`+15853109661`) accepts inbound
messages from a growing set of family members. Some of those senders
(Carolyn and Doug Cronquist — Jason's parents) belong to a separate
Alice deployment on their own network (`grandparents-home`,
`10.200.0.2` over WireGuard) with its own vault, principals, and
tool set. Handling their messages on the main Alice conflates two
people's contexts and leaks family memory into Jason's vault.

## Solution

An inbound Signal envelope from a configured sender is forwarded to
a peer Alice over A2A JSON-RPC instead of landing on the local
kernel's inbox. The peer processes the task, calls `send_message`
with the original Signal number, and her reply reaches the sender
via her outbound Signal transport — which is itself pointed at the
main container's signal-cli RPC endpoint (`SIGNAL_API=http://10.200.0.1:8080`).

## Wire shape

```
          Signal
            |
            v
    +---------------+          A2A POST /
    | aimax SigTx   |  -------------------------->  grandparents' Alice :7878
    | _produce()    |          message/send             (A2A server)
    +---------------+                                       |
                                                            | send_message
                                                            v
                                                     grandparents' SigTx
                                                            |
                                                            v
                                                 aimax signal-cli :8080
                                                            |
                                                            v
                                                          Signal
                                                          (reply to Carolyn)
```

The reverse-path is a plain Signal outbound that happens to go
through the main container's RPC because grandparents' box can't
reach Signal's servers directly on its own (and even if it could,
we'd rather keep all Signal credentials pinned to one host).

## Config (aimax — this container)

`~/alice-mind/config/alice.config.json`:

```json
{
  "signal": {
    "dispatch": {
      "forwards": [
        {
          "senders": ["+14357091396"],
          "target": "http://10.200.0.2:7878",
          "label": "grandparents-home"
        }
      ]
    }
  }
}
```

Fields:
- `senders` — list of E.164 Signal numbers to forward. Senders not
  in the list hit the normal local path.
- `target` — base URL of the peer's A2A JSON-RPC endpoint (same host
  the A2A agent-card is served from).
- `label` — human-readable tag included in the forward payload's
  metadata and surfaced in the peer's kernel prompt.

Hot-reloads with the standard sentinel
(`echo '{"type":"hot"}' > /state/worker/reload-requested`).

## Config (grandparents' box — receiver)

In `~/.config/alice/alice.env` on `10.200.0.2`:

```
ALICE_A2A_ENABLED=true
SIGNAL_API=http://10.200.0.1:8080
```

Add a `docker-compose.override.yml` exposing A2A so aimax's WG peer
can reach it:

```yaml
services:
  alice:
    ports:
      - "7878:7878"
```

Add a principal entry for the relay in
`~/alice-mind/config/principals.yaml`:

```yaml
principals:
  - id: aimax-signal-relay
    display_name: "aimax signal relay"
    transports:
      a2a:
        address: a2a
        allowed: true
```

(The default `ALICE_A2A_PRINCIPAL=a2a` maps all inbound A2A traffic
to this single principal in Phase 1 — identity routing is a follow-up.)

Restart her container after each change (`docker compose restart alice`).

## How the receiver routes the reply

The forward payload carries `signal_sender`, `signal_sender_name`,
`signal_timestamp` (ms), and `dispatch_label` in both
`MessageSendParams.message.metadata` and `MessageSendParams.metadata`.
The A2A executor copies those keys onto the `InboundMessage.metadata`
it hands to the kernel, and `handle_a2a` feeds them to
`A2ATransport.build_prompt`.

When `signal_sender` is present the `turn.a2a.md.j2` template
switches to the forwarded-Signal framing:

> This message was forwarded to you over A2A because another Alice
> relay is routing {{name}}'s Signal messages to you. To reply, call
> the `send_message` tool with `recipient='+14357091396'` — your
> outbound Signal transport will deliver the reply back to them over
> Signal. Do NOT reply with `recipient='self'`.

The peer's `send_message` tool then resolves the E.164 recipient to
a `ChannelRef(transport="signal", address="+14357091396")` the same
way any direct Signal outbound would, and her SignalClient POSTs to
the configured `SIGNAL_API` — which is the main container's
signal-cli RPC. aimax's signal-cli sends the reply out over the
shared account; the sender sees it as an ordinary reply.

## Failure handling

The forward POST is single-attempt (3s connect / 10s read). On any
exception (connection refused, 5xx, timeout, etc.) we log a WARNING
and fall through to the standard local path so the envelope lands
on Alice's inbox and she can reply herself. Silent drops would
strand the sender; a visible local reply is the correct degraded
mode. The operator sees the failure in `speaking.log`:

```
signal forward failed (sender=+14357091396 target=http://10.200.0.2:7878 label=grandparents-home): ConnectError('Connection refused'); falling back to local handling
```

## What's deliberately out of scope

- Multi-target routing (one sender → many peers). One rule, one target.
- Bidirectional A2A (Alice initiating to a peer Alice). Phase 1 is
  inbound-only on the receiver and inbound-only on the forwarder.
- Retry / queue for failed forwards. Fallback to local is the fallback.
- Per-sender principal mapping on the receiver. Still a single
  `ALICE_A2A_PRINCIPAL` in Phase 1.
