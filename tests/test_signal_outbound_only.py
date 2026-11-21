"""SignalTransport outbound-only mode: receive loop stays off.

The flag lets a secondary tenant share a Signal account with a gateway
peer (which does the receiving). Without the gate, both tenants would
poll receive() and duplicate-process every inbound message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from alice_speaking.transports.signal import SignalTransport


def test_outbound_only_default_false():
    t = SignalTransport(signal_client=object())
    assert t._outbound_only is False


@pytest.mark.asyncio
async def test_outbound_only_skips_produce_task():
    t = SignalTransport(signal_client=object())
    t._outbound_only = True
    ctx = SimpleNamespace()
    run_task = asyncio.create_task(t._run(ctx))
    # Yield so _run spawns its child tasks.
    await asyncio.sleep(0)
    try:
        assert t._produce_task is None, (
            "outbound-only mode should not spawn the receive loop"
        )
        assert t._consume_task is not None, (
            "outbound-only mode still needs the consume/send loop"
        )
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_default_mode_still_spawns_produce_task():
    t = SignalTransport(signal_client=object())
    # _outbound_only defaults to False; do not touch it.
    ctx = SimpleNamespace()
    run_task = asyncio.create_task(t._run(ctx))
    await asyncio.sleep(0)
    try:
        assert t._produce_task is not None, (
            "default mode must still spawn the receive loop"
        )
        assert t._consume_task is not None
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
