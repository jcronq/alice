# A2A symmetric client transport

## Problem

Phase 1 of the A2A transport (PRs #915, #916, module docstring in
`src/alice_speaking/transports/a2a.py`) is inbound-only — Alice is an
A2A *server*; peers POST text tasks to her and stream replies. That
asymmetry is the wrong shape for the main-alice ↔ grand-alice pairing:
either Alice should be able to initiate. A grandparents-home instance
nudging main Alice when something interesting happens, or main Alice
pinging grand-alice to ask "Carolyn home yet?", both need the same
request/response shape just pointed the other way.

## Design

Symmetric peering built on the same `A2ATransport`, with a thin
client-side companion.

### Routing

`principals.yaml` grows an `a2a` channel per peer:

```yaml
grand-alice:
  display_name: Grand Alice (grandparents-home)
  channels:
    - transport: a2a
      address: "http://10.200.0.2:7878"
      preferred: true
```

When `send_message(recipient="grand-alice", ...)` fires:

1. `_resolve_recipient` → `address_book.preferred_channel("grand-alice")`
   → `ChannelRef(transport="a2a", address="http://10.200.0.2:7878")`.
2. Daemon `_send_message` → `OutboxRouter.dispatch` → lookup by
   `channel.transport == "a2a"` → `A2ATransport.send(...)`.
3. `A2ATransport.send` inspects `destination.address`. URL prefix →
   outbound branch (new); plain task id → server-side per-task
   outbox (existing Phase-1 path).
4. The outbound branch delegates to `A2AClient.send_text(url=...,
   text=...)` which wraps the a2a-sdk's JSON-RPC client, POSTs, and
   streams the reply back.

A2A is already in `_QUEUEABLE_TRANSPORTS = {"signal", "discord",
"gmail"}`'s *exclusion* set — quiet hours never hold an A2A send, so
the mutual-ping path stays snappy regardless of wall clock.

### Auth

One shared-secret HTTP header per request: `X-Alice-Peer-Token`. Each
Alice carries two inputs:

- `identity_token` — what WE present on outbound. Default env var
  `ALICE_A2A_IDENTITY_TOKEN`.
- `peer_tokens: dict[token → principal_id]` — inbound-side map. An
  inbound request whose token matches an entry resolves the sender to
  that principal. Missing / unknown tokens fall back to the existing
  catch-all `a2a` principal so legacy deployments keep working.

Config file: `${ALICE_MIND_DIR}/config/a2a-peers.yaml`.

```yaml
identity_token_env: ALICE_A2A_IDENTITY_TOKEN   # our outbound token source

peers:
  grand-alice:
    display_name: "Grand Alice (grandparents-home instance)"
    token_env: ALICE_A2A_PEER_TOKEN_GRAND_ALICE   # inbound-side token source
```

Env vars live in `alice.env` / the systemd unit / wherever the deploy
already keeps secrets — they never land in the vault or the repo. A
token rotation = change the value on both ends + restart the daemons.

The server-side `_AliceExecutor.execute` reads the header from
`request.call_context.state['headers']` (a2a-sdk's
`DefaultServerCallContextBuilder` already stashes them there) and
threads the resolved principal id through `_make_principal(token)`
before pushing the `InboundMessage` onto the inbox.

### Client shape

`A2AClient` is deliberately thin. One module-scope instance per
`A2ATransport`; one `a2a-sdk` `Client` per peer URL, cached. We call
`ClientFactory.create` against a `minimal_agent_card` (JSON-RPC only)
so startup skips the `.well-known/agent-card.json` round-trip — both
Alices run the same build, the card is predictable, and card
resolution over the WireGuard mesh is a round-trip we'd rather spare
on every send.

Timeout: 60s default, overrideable via `ALICE_A2A_CLIENT_TIMEOUT` so
slow-link WG tunnels can relax it without a code change. The stream
loop aggregates every `artifact_update` into a single reply text,
stops on the first terminal `TaskState` (COMPLETED / CANCELED /
FAILED / REJECTED), and returns an `A2AReply` dataclass with the
text, state, chunk count, and error / timeout flags. Errors never
raise through `send()` — the caller (`OutboxRouter`) sees 0 chunks
and the standard "transport dropped" event fires; `log.warning`
carries the operator trail.

### Reply funnel

The outbound-call's SSE reply lands in `log.info("a2a outbound ok ...
reply_len=%d")` and the standard `a2a_send` event (`chunk_count`
reports the peer-side chunk count). The reply text itself does not
re-enter Alice's turn as additional context — that would invert the
`send_message` call chain (the tool-call completes well before the
next kernel block). For mutual ping-pong the correlation path is
"grand-alice responds by calling her own `send_message(recipient=
main-alice, ...)`" which opens a fresh turn on main Alice — the
proper symmetric shape.

A follow-up knob (`send_message(..., capture_reply=True)`) can
append the peer's reply to the tool-result string so Alice sees the
answer inline. Skipped for v1 to keep the surface small; the hook
(`A2ATransport.send_and_capture`) is already exposed so adding it
later is a one-line prompt change.

## Deploy plan

1. Merge this PR (draft → review → merge).
2. On **aimax1** (main Alice):
   - `~/alice-mind/config/a2a-peers.yaml` — add `grand-alice` peer
     block pointing at `ALICE_A2A_PEER_TOKEN_GRAND_ALICE`.
   - `~/alice-mind/config/principals.yaml` — add `grand-alice`
     principal with `transport: a2a, address: http://10.200.0.2:7878`.
   - Secrets file / `alice.env` — add
     `ALICE_A2A_IDENTITY_TOKEN=<main-alice-secret>` and
     `ALICE_A2A_PEER_TOKEN_GRAND_ALICE=<grand-alice-secret>`.
   - `bin/alice-deploy worker` (or `docker compose up -d alice`
     if the pin set changed).
3. On **10.200.0.2** (grand-alice): mirror the config with the
   principal ids reversed (`main-alice` principal with aimax1's
   WG-reachable URL, `ALICE_A2A_PEER_TOKEN_MAIN_ALICE` populated,
   `ALICE_A2A_IDENTITY_TOKEN=<grand-alice-secret>`).
4. Run `scripts/a2a_mutual_ping.py` from either host to verify both
   directions. Or call `bin/alice "ping grand-alice"` and watch the
   turn log on the other side.
5. Roll back = empty the peer token env vars; the daemon returns to
   Phase-1 inbound-only behaviour automatically.

### Reachability

- `aimax1` → `10.200.0.2`: WireGuard mesh, `ssh aimax1 curl -s
  http://10.200.0.2:7878/.well-known/agent-card.json` from aimax1
  should return the card.
- `10.200.0.2` → aimax1: the main-alice principal's URL is the WG
  address of aimax1 (`http://10.200.0.1:7878` if the mesh is laid out
  that way) — confirm with `grand-alice: curl -s http://<main-wg-ip>:
  7878/.well-known/agent-card.json`.

Both deploys must publish port 7878 to the host interface the peer
can reach. The existing compose block for main Alice already does;
the grand-alice sandbox publishes 7878 inside the WG network.

## Test plan

- Unit — `tests/test_a2a_client.py`: URL dispatch, token→principal
  resolution, peers YAML parsing + env-var fallback, timeout env
  parsing. Deterministic — no sockets.
- Regression — `tests/test_a2a_transport.py`: existing suite passes
  unchanged (server path is untouched outside the
  `_make_principal(token)` extension).
- Live — `tests/test_a2a_transport.py::test_live_request_response_smoke`
  still covers the server-side SSE shape.
- Mutual ping — `scripts/a2a_mutual_ping.py`: run on both ends after
  rollout. Exit 0 = success; otherwise the printed `reply.error`
  pinpoints the failure.

## Open questions

- Should `send_message` surface the peer's reply inline (`capture_reply=
  True`)? Deferred — easy to add once we see how Alice uses the
  outbound path in real conversation.
- Multi-hop peering (main-alice → grand-alice → another household)?
  Out of scope. The transport doesn't know about hops; each pair is
  configured independently.
- oauth2-proxy / mTLS migration: the header-based auth path is one
  of three legitimate options the existing docstring already names.
  No work required here — swap the header-check for the proxy's
  `X-Forwarded-User` the day we grow a third-party caller.

## Memory worker handoff

The companion vault note draft lives at
`cortex-memory/reference/a2a-symmetric-peering.md` in this PR. The
memory worker is the only writer on `cortex-memory/`, so the file
is a hand-off — merge-time, someone (Speaking Alice or the memory
worker on its next cadence) picks it up and folds it into the
live vault with the proper `created` / `updated` frontmatter.
