"""Filesystem escalation tools.

Speaking's harness (Claude Code) gates the built-in Write/Edit tools through
permission prompts. The daemon has no UI surface for those prompts, so any
write to a path not on settings.json's allow list becomes a hard block.

These MCP tools sit outside the harness's gate and write to any path the
process can reach. They were added at Jason's explicit request after a
bootstrap attempt to enable WebSearch hit the Write-to-settings.json gate
with no in-band approval path.

Scope: full filesystem write/edit for the worker user. The daemon
container's filesystem mounts are the real safety boundary, not these tool
schemas. Treat every call as load-bearing — the harness gate is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def build(cfg: Config, *, personae: Personae | None = None) -> list[SdkMcpTool[Any]]:
    p = personae or placeholder_personae()
    agent = p.agent.name

    @tool(
        name="write_file",
        description=(
            f"Write content to an absolute filesystem path, creating parent "
            f"directories as needed. Bypasses the harness's built-in Write "
            f"gate, so {agent} can edit harness config (settings.json), "
            f"protected MCP source, or any other path the worker process "
            f"can reach. Existing files are overwritten. No backup is made; "
            f"caller is responsible for verifying the target path."
        ),
        input_schema={"path": str, "content": str},
    )
    async def write_file(args: dict) -> dict:
        path_str = (args.get("path") or "").strip()
        content = args.get("content")
        if not path_str:
            return _err("path required")
        if not isinstance(content, str):
            return _err("content must be a string")
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            return _err(f"path must be absolute: {path_str}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        except OSError as exc:
            return _err(f"write failed: {exc}")
        return _ok(f"wrote {len(content)} chars to {path}")

    @tool(
        name="edit_file",
        description=(
            "Replace exact-match text in a file. Like the harness's Edit "
            "tool but without the path gate. `old_string` must appear "
            "exactly once unless `replace_all` is true. Errors if the file "
            "does not exist or `old_string` is not found."
        ),
        input_schema={
            "path": str,
            "old_string": str,
            "new_string": str,
            "replace_all": bool,
        },
    )
    async def edit_file(args: dict) -> dict:
        path_str = (args.get("path") or "").strip()
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all"))
        if not path_str:
            return _err("path required")
        if not isinstance(old, str) or not old:
            return _err("old_string must be a non-empty string")
        if not isinstance(new, str):
            return _err("new_string must be a string")
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            return _err(f"path must be absolute: {path_str}")
        if not path.is_file():
            return _err(f"file not found: {path}")
        try:
            text = path.read_text()
        except OSError as exc:
            return _err(f"read failed: {exc}")
        count = text.count(old)
        if count == 0:
            return _err("old_string not found")
        if count > 1 and not replace_all:
            return _err(
                f"old_string appears {count} times; pass replace_all=true to apply to all"
            )
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            path.write_text(new_text)
        except OSError as exc:
            return _err(f"write failed: {exc}")
        return _ok(f"edited {path} ({count} replacement{'s' if count != 1 else ''})")

    return [write_file, edit_file]


__all__ = ["build"]
