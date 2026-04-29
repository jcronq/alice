"""One-shot verification that the Agent SDK reaches Claude.

Works for either auth mode — subscription (CLAUDE_CODE_OAUTH_TOKEN)
or api / LiteLLM (ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY).

Run as: `uv run python -m alice_speaking._sanity`
"""

import asyncio
import sys

from alice_core.auth import ensure_auth_env
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

MARKER = "SDK-AUTH-OK"


async def main() -> int:
    auth = ensure_auth_env()
    if auth.mode == "none":
        sys.exit("no Claude credentials in env or alice.env (set CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY)")
    print(f"auth mode: {auth.mode}")
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

    reply = reply.strip()
    print(f"reply: {reply!r}")
    if MARKER in reply:
        print("OK — Agent SDK auth verified")
        return 0
    print("FAIL — unexpected response")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
