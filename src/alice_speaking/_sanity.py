"""One-shot verification that the Agent SDK reaches Claude via Alice's OAuth token.

Run as: `uv run python -m alice_speaking._sanity`
"""

import asyncio
import os
import pathlib
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

ALICE_ENV = pathlib.Path.home() / ".config" / "alice" / "alice.env"
MARKER = "SDK-OAUTH-OK"


def load_oauth_token() -> None:
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    if not ALICE_ENV.is_file():
        sys.exit(f"CLAUDE_CODE_OAUTH_TOKEN not set and {ALICE_ENV} not found")
    for raw in ALICE_ENV.read_text().splitlines():
        line = raw.strip()
        if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = line.split("=", 1)[1].strip()
            return
    sys.exit(f"CLAUDE_CODE_OAUTH_TOKEN not found in {ALICE_ENV}")


async def main() -> int:
    load_oauth_token()
    opts = ClaudeAgentOptions(
        model="claude-sonnet-4-5",
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
        print("OK — Agent SDK + OAuth verified")
        return 0
    print("FAIL — unexpected response")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
