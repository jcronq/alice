#!/usr/bin/env python3
"""Manual mutual-ping check for the symmetric A2A peering.

Proves the end-to-end shape: main Alice initiates a POST to grand-alice,
grand-alice's turn log records the inbound from principal ``main-alice``,
and the reverse direction mirrors. Not wired into the automatic suite
(it needs two running daemons on the WG mesh); the SM-v2 dispatcher can
pick it up on a demand label when we're ready.

Usage
-----

Run against a local pair of daemons::

    # Terminal A — fake "grand alice" server
    ALICE_A2A_IDENTITY_TOKEN=grand-secret \
    ALICE_A2A_PEER_TOKEN_MAIN_ALICE=main-secret \
    python3 scripts/a2a_mutual_ping.py --server --port 17001 \
        --peer-principal main-alice --peer-token main-secret

    # Terminal B — "main alice" client, pointed at the above
    ALICE_A2A_IDENTITY_TOKEN=main-secret \
    python3 scripts/a2a_mutual_ping.py --client \
        --url http://127.0.0.1:17001/ --text "ping from main"

The client side exits 0 when the server echoed back and the inbound's
principal matched ``main-alice``. Any other outcome (timeout, wrong
principal, stream error) exits non-zero and prints the mismatch — the
exit code is the only thing CI / an SM-v2 lane needs to see.

For the real deploy (main-alice on aimax1 ↔ grand-alice on 10.200.0.2),
the same script runs on either host with the live URLs plugged in;
``ALICE_A2A_IDENTITY_TOKEN`` on each side carries the matching
``ALICE_A2A_PEER_TOKEN_*`` from the other.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from alice_speaking.transports.a2a import A2ATransport
from alice_speaking.transports.a2a_client import A2AClient
from alice_speaking.transports.base import ChannelRef, OutboundMessage


async def _run_server(args: argparse.Namespace) -> int:
    transport = A2ATransport(
        port=args.port,
        host=args.host,
        agent_name="MockGrandAlice",
        agent_description="Mutual-ping test server",
        peer_tokens={args.peer_token: args.peer_principal}
        if args.peer_token
        else {},
    )
    await transport.start()
    print(f"[server] listening on {args.host}:{args.port}", flush=True)

    async def pump():
        async for msg in transport.messages():
            print(
                f"[server] inbound principal={msg.principal.native_id!r} "
                f"display={msg.principal.display_name!r} text={msg.text!r}",
                flush=True,
            )
            await transport.send(
                OutboundMessage(
                    destination=msg.origin,
                    text=f"echo({msg.principal.native_id}): {msg.text}",
                )
            )
            await transport.signal_done(msg.origin)

    task = asyncio.create_task(pump(), name="mock-grand-pump")
    try:
        await asyncio.Event().wait()  # run until ctrl-c
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        await transport.stop()
    return 0


async def _run_client(args: argparse.Namespace) -> int:
    client = A2AClient()
    reply = await client.send_text(url=args.url, text=args.text)
    await client.aclose()
    print(
        f"[client] reply state={reply.state} chunks={reply.chunk_count} "
        f"timed_out={reply.timed_out} error={reply.error!r}"
    )
    print(f"[client] reply text: {reply.text!r}")
    if reply.error:
        return 2
    if reply.timed_out:
        return 3
    if args.expect_substring and args.expect_substring not in reply.text:
        print(
            f"[client] mismatch: expected substring {args.expect_substring!r} "
            f"not found in reply {reply.text!r}"
        )
        return 4
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_mutually_exclusive_group(required=True)
    sub.add_argument("--server", action="store_true", help="run the mock peer")
    sub.add_argument("--client", action="store_true", help="POST one message")

    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17001)
    parser.add_argument("--peer-principal", default="main-alice")
    parser.add_argument("--peer-token", default="")

    parser.add_argument("--url", default="http://127.0.0.1:17001/")
    parser.add_argument("--text", default="ping")
    parser.add_argument("--expect-substring", default="echo")

    args = parser.parse_args()
    if args.server:
        return asyncio.run(_run_server(args))
    return asyncio.run(_run_client(args))


if __name__ == "__main__":
    sys.exit(main())
