"""Live smoke: Agent SDK reaches Claude with whatever auth is wired up.

Works for either auth mode — subscription (CLAUDE_CODE_OAUTH_TOKEN)
or api / LiteLLM (ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY).

Skipped unless the operator opts in with ``ALICE_LIVE_TESTS=1`` —
this test spends real tokens and reaches the network. Run as:

    ALICE_LIVE_TESTS=1 .venv/bin/pytest tests/test_sdk_auth_smoke.py

Plan 02 of the speaking refactor moved this from
``alice_speaking/_sanity.py`` (runnable as ``python -m
alice_speaking._sanity``) into the test suite. The runtime package
shouldn't ship a smoke entry point alongside production modules.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from alice_core.auth import ensure_auth_env


MARKER = "SDK-AUTH-OK"


@pytest.mark.skipif(
    os.environ.get("ALICE_LIVE_TESTS") != "1",
    reason="live SDK test; opt in with ALICE_LIVE_TESTS=1",
)
def test_sdk_auth_smoke() -> None:
    """One-shot verification that the SDK can reach Claude end-to-end."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    auth = ensure_auth_env()
    if auth.mode == "none":
        pytest.skip(
            "no Claude credentials wired (CLAUDE_CODE_OAUTH_TOKEN, "
            "or ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY)"
        )

    async def go() -> str:
        opts = ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            allowed_tools=[],
            system_prompt="Reply verbatim to anything the user says. No preamble.",
        )
        reply = ""
        async for msg in query(prompt=f"Reply exactly: {MARKER}", options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        reply += block.text
        return reply.strip()

    reply = asyncio.run(go())
    assert MARKER in reply, f"unexpected SDK reply: {reply!r}"
