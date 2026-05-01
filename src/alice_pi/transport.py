"""Async subprocess + JSONL line reader for pi-coding-agent.

Spawns ``pi`` (or ``$ALICE_PI_BIN``) with the constructed argv,
streams stdout line-by-line, and yields parsed JSON objects. Pi
emits one JSON object per newline in ``--mode json``.

Non-JSON lines (rare; pi can write a non-JSON header on auth
errors before the session opens) are skipped silently — the
translator's ``error`` event handles real failure cases.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator, Optional, Sequence


__all__ = ["stream_pi_events", "PI_BIN", "pi_bin"]


# Buffer cap for the line-by-line JSONL reader. asyncio.StreamReader
# defaults to 64KiB per line, which a single pi ``message_end`` event
# (full message content + usage + nested partials) can blow through —
# observed "Separator is found, but chunk is longer than limit"
# exceptions on real wakes that wrote large note bodies. 10 MiB
# matches the cap claude_agent_sdk's subprocess transport uses for
# the same reason.
_PI_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024


def pi_bin() -> str:
    """Return the pi binary path. Read fresh from the environment
    every call so test fixtures + runtime config changes take
    effect without a module reload."""
    return os.environ.get("ALICE_PI_BIN", "pi")


# Back-compat module-level name. New callers should prefer
# :func:`pi_bin` (or read ``ALICE_PI_BIN`` directly) so the test
# pattern of monkeypatching the env var works without import
# bookkeeping.
PI_BIN = pi_bin()


async def stream_pi_events(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """Spawn pi; yield parsed JSONL events from stdout.

    Caller is responsible for cancellation (wrap in asyncio.timeout).
    Non-zero exit is surfaced as RuntimeError after the stream
    drains, with stderr captured for diagnostics.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=_PI_STDOUT_BUFFER_LIMIT,
    )
    assert proc.stdout is not None and proc.stderr is not None

    try:
        async for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Pi writes a "Shell cwd was reset to ..." line
                # post-exit; skip non-JSON noise.
                continue
    finally:
        rc = await proc.wait()
        if rc != 0:
            stderr_bytes = await proc.stderr.read()
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"pi exited {rc}: {stderr[:1000]}"
            )
