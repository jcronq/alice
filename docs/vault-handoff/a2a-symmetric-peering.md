---
title: A2A symmetric peering
kind: reference
created: 2026-11-23
status: draft-pending-memory-worker
tags: [a2a, transports, grand-alice, infra]
---

# A2A symmetric peering

Two Alices can initiate conversations with each other over Google's
A2A protocol — either end POSTs, either end replies. Before this,
A2A was inbound-only (Alice-as-server); outbound was a stub.

## Shape

- Both sides run the same `A2ATransport`. One class, two directions.
- Routing: `principals.yaml` lists the peer under
  `transport: a2a, address: http://<peer>:7878`. `send_message(recipient=
  "grand-alice")` resolves through the address book, hits
  `A2ATransport.send`, and branches to the client path on URL-shaped
  destinations.
- Auth: shared-secret `X-Alice-Peer-Token` header per request.
  Configured in `${ALICE_MIND_DIR}/config/a2a-peers.yaml` with tokens
  sourced from env vars so secrets stay out of the vault.
- Inbound: the server resolves the token on each request to a
  principal id (e.g. `main-alice`) so turn logs show "from main-alice",
  not the single catch-all "a2a" principal.

## Deploy

Main Alice (aimax1) and grand-alice (10.200.0.2) hold mirrored config:

- `ALICE_A2A_IDENTITY_TOKEN` = this instance's outbound token.
- `ALICE_A2A_PEER_TOKEN_<PEER>` = the other instance's token (same
  value as her `ALICE_A2A_IDENTITY_TOKEN`).

Rolling back = empty the env vars; the transport reverts to Phase-1
inbound-only automatically.

## Test

`scripts/a2a_mutual_ping.py` on either host. Exit 0 = success.

## Code

- `src/alice_speaking/transports/a2a.py` — server + outbound branching.
- `src/alice_speaking/transports/a2a_client.py` — the thin outbound
  wrapper over `a2a-sdk`'s JSON-RPC client.
- `src/alice_speaking/infra/a2a_peers.py` — peers YAML + env resolver.

## Related

- [[design-retrieval-protocol]] — the vault-side query model that
  consumes a peer's reply has nothing to do with A2A, but both are
  "cross-process trust boundaries"; keep an eye on the pattern.
- The [[projects/grandparents-alice-deployment]] project doc owns
  the grand-alice side's deploy particulars (WG topology, container
  layout).
